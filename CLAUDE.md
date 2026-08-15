# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# The working virtualenv in this repo is `venv/` (NOT `.venv/`, which is empty).
source venv/bin/activate
pip install -r requirements.txt          # pinned direct deps
# pip install -r requirements.lock        # fully reproducible (all transitive deps)

# Database: needs Postgres + pgvector. DATABASE_URL defaults to postgresql:///traffic_rag_dev.
createdb traffic_rag_dev                  # local dev DB (schema is auto-created at startup)
# The app runs ensure_schema() on boot (CREATE EXTENSION vector + tables); no manual migration
# needed. migrations/001_init.sql is the reference DDL for manual/managed provisioning.

# Run — development (requires GOOGLE_API_KEY in .env or environment)
python app.py                             # http://127.0.0.1:5000
FLASK_DEBUG=1 python app.py               # Flask debug/reload

# Run — production serving (see Procfile)
gunicorn --workers 2 --timeout 120 --bind 0.0.0.0:5000 app:app
# --timeout 120 matters: the FIRST request lazily loads the embedding + reranker
# models (~15-20s) and would trip gunicorn's default 30s worker timeout.

# Probes & metrics
curl -s http://127.0.0.1:5000/health      # liveness: {"status":"ok","ready":<bool>}
curl -s http://127.0.0.1:5000/ready       # readiness: DB reachable + active document (503 if DB down)
curl -s http://127.0.0.1:5000/documents   # list ingested documents + status
curl -s http://127.0.0.1:5000/metrics     # Prometheus metrics (request latency/counts, rag_ask_total)
OTEL_ENABLED=1 python app.py              # OpenTelemetry tracing (console exporter; set OTEL_EXPORTER_OTLP_ENDPOINT for a collector)

# Eval harness + CI regression gate (retrieval-only, works without a valid LLM key)
python -m eval.run_eval --k 3 --min-hit-rate 0.8   # exits non-zero if hit@k below threshold
python -m eval.run_eval --with-answers             # also score answer faithfulness (needs live key)
```

There is no test suite or linter configured yet (adding a RAG eval harness is Phase 3 of [docs/PRODUCTION_ROADMAP.md](docs/PRODUCTION_ROADMAP.md)). `rag_system_walkthrough.ipynb` is a standalone explainer notebook, not part of the runtime.

All models, chunk sizes, top-k values, embedding provider, and LLM timeout/retries are env-overridable — see `.env.example` for the full list.

## Architecture

A Flask app that answers questions against a single active document using a hybrid RAG pipeline. State lives in **Postgres + pgvector**, so the app layer is stateless and safe across concurrent requests and multiple workers. Three modules hold the logic:

- **[app.py](app.py)** — thin HTTP layer. Configures request-scoped structured logging (every log line carries a `[req:<id>]`), loads `.env` *before* importing `rag_backend` (the backend reads config at import time). Routes: `/` (UI), `/ask` (POST query → JSON answer + sources), `/upload` (POST `.md`/`.txt`, max 2 MB, ingests + activates), `/health` (liveness), `/ready` (DB probe + active doc), `/documents` (list).
- **[rag_backend.py](rag_backend.py)** — the `RAGService` class plus a module-level `rag_system` exposed via `get_answer()` and `process_new_document()`. Holds only read-only shareable resources (embedder, reranker, LLM chain); no per-document mutable state.
- **[pg_store.py](pg_store.py)** — all SQL: connection pool (with pgvector registration), `ensure_schema()`, document lifecycle (`begin_document`/`insert_chunks`/`finalize_document`/`set_active_document`), and `hybrid_search()`.

### Data model ([migrations/001_init.sql](migrations/001_init.sql))

- **`documents`** — one row per ingested doc: `content_hash` (unique), `status` (`ingesting`/`ready`/`failed`), `is_active` (a partial unique index enforces **at most one active** document), `chunk_count`.
- **`chunks`** — `content`, header `metadata` (JSONB), `embedding vector(EMBEDDING_DIM)`, and a generated `tsv tsvector` (kept in sync with `content`). GIN index on `tsv`, HNSW index on `embedding`.

### Retrieval pipeline (order matters)

1. **Chunking** — `.md` split by header (`MarkdownHeaderTextSplitter`, H1–H3 → `Title`/`Section`/`Subsection` metadata), then `RecursiveCharacterTextSplitter` (CHUNK_SIZE/CHUNK_OVERLAP) caps chunk size while preserving metadata; non-`.md` uses the recursive splitter directly. Chunks are embedded and inserted into Postgres.
2. **Hybrid search (in SQL)** — `pg_store.hybrid_search()` runs a semantic arm (pgvector cosine `<=>`) and a keyword arm (`tsvector`/`ts_rank_cd` via `websearch_to_tsquery`), each top `RETRIEVAL_ARM_TOP_K`, fused with **Reciprocal Rank Fusion** (RRF_K=60) in a single query, returning top `HYBRID_CANDIDATES`.
3. **Rerank** — cross-encoder (`ms-marco-MiniLM-L-6-v2`) rescores candidates, keeps top RERANK_TOP_K. Gated by `RERANK_ENABLED` (can be disabled for a lighter serverless footprint).
4. **Generate** — Gemini (`gemini-2.5-flash`, temperature 0) answers from context only, wrapped with a per-request timeout (LLM_TIMEOUT) and tenacity retries/backoff (LLM_MAX_ATTEMPTS) in `_invoke_chain`. Retries skip permanent errors (bad/leaked key, 4xx) via `_is_retryable_llm_error`.

### Key design constraints

- **Stateless over Postgres.** The active document and all chunks live in Postgres. Any worker/thread can answer concurrently; there is no global lock on the answer path (only a one-time `_init_lock` guarding lazy model load). Uploading swaps the single active document via an atomic DB update.
- **Content-hash reuse.** Re-ingesting identical content reuses the existing `ready` document (by `content_hash`) instead of re-embedding.
- **Pluggable embeddings.** `EMBEDDING_PROVIDER=local` (HuggingFace `all-MiniLM-L6-v2`, 384-dim, default) or `google` (`text-embedding-004`, 768-dim). Set `EMBEDDING_DIM` to match and re-ingest when switching.
- **Fail-soft.** Missing DB/document, empty chunks, and query/LLM errors return a user-facing `{"answer": ..., "sources": []}` rather than raising.

## Notes

- `GOOGLE_API_KEY` is required **only** for Gemini answer generation (and for Google embeddings if enabled). With the default local embeddings + reranker, retrieval works without it — a bad/leaked key fails only at the generation step. Rotate any key ever committed.
- All models, chunk sizes, top-k values, RRF constant, embedding provider/dim, DB URL, and LLM timeout/retries are env-overridable config at the top of `rag_backend.py` / `pg_store.py`; see `.env.example`.
- Roadmap: [docs/PRODUCTION_ROADMAP.md](docs/PRODUCTION_ROADMAP.md). **Phase 0** (deps, gunicorn, logging, health, LLM timeout/retry, RRF, chunk cap), **Phase 1** (Postgres + pgvector, stateless, SQL hybrid search), **Phase 2 code** (Dockerfile, security headers, CORS lockdown, rate limiting, optional `APP_API_KEY`, ProxyFix), and **Phase 3** (eval harness + CI regression gate, Prometheus `/metrics`, OpenTelemetry tracing) are done. Remaining is deployment only (build/push image, provision managed Postgres, IAP, wire deploy into CI) — needs cloud credentials, not code. Tracing is opt-in via `OTEL_ENABLED`; see [tracing.py](tracing.py).
- **Eval harness** ([eval/](eval/)): `python -m eval.run_eval` scores retrieval hit@k/MRR against the real pgvector pipeline and gates CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)). Retrieval-only, so it runs without a valid LLM key. Add cases to [eval/dataset.json](eval/dataset.json) when changing chunking/fusion/prompts.
- **Phase 2 hardening is env-gated** — see the "Phase 2 hardening" block in `.env.example` and [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md). Rate limiting is on by default (probes exempt); CORS, `APP_API_KEY`, HSTS, and ProxyFix are off/permissive by default so local dev and the browser UI work unchanged.
- **Local gunicorn on macOS:** set `EMBEDDING_DEVICE=cpu` — forking workers crash if the model initializes Metal/MPS. Production (Linux) is CPU-only, and the `Dockerfile` sets this already.
- **Container:** `docker build -t traffic-rag .` then run with `-e DATABASE_URL=... -e GOOGLE_API_KEY=... -p 8080:8080`. Models are baked into the image (offline at runtime).
