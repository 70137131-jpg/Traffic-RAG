# Traffic RAG — Production Roadmap

A migration plan from the current local demo to a reliable, production-ready
system.

## Scope & constraints (the decisions this plan is built on)

- **Audience:** Internal single-tenant tool — a handful of known users, one org,
  one active knowledge base at a time. We deliberately do **not** build
  multi-tenancy, per-tenant isolation, or org management.
- **Infra style:** Managed / serverless — minimize ops. Prefer managed Postgres
  and managed model APIs over self-hosted vector databases and self-hosted model
  servers.
- **Provider:** Already committed to Google Gemini. Stay single-provider where
  possible (one API key, one bill, one SDK).

These constraints let us make the system dramatically simpler than a generic
"production RAG" design. Most of the complexity in the current code exists to
work around being an in-process demo; managed services remove it rather than
add to it.

---

## Where the current system hurts

Grounded in the current code (`app.py`, `rag_backend.py`):

| # | Problem | Location | Why it blocks production |
|---|---------|----------|--------------------------|
| 1 | Global single-document singleton | `rag_backend.py` `rag_system` | An upload swaps the active doc for *everyone*; no multi-document support |
| 2 | One process-wide `RLock` around all of `answer()` | `RAGSystem._lock` | Caps the system at **one concurrent query**, including the multi-second LLM call |
| 3 | Embedder + cross-encoder run in-process on CPU | `initialize()` | ~18s cold start; model serving coupled to web serving; can't scale independently |
| 4 | Ephemeral state (BM25 + doc list) rebuilt per start | `initialize()` | No durability; every restart re-does work |
| 5 | Unpinned dependencies | `requirements.txt` | Non-reproducible builds; silent breakage over time |
| 6 | Deprecated LangChain imports | `langchain_community.*` | On sunset paths; will break on future upgrades |
| 7 | Flask dev server via `app.run()` | `app.py` | Not a production server |
| 8 | Chunks have no max size | `_split_document()` header split | A long header-less section becomes one huge chunk → poor retrieval |
| 9 | Naive hybrid fusion + exact-string dedup | `_hybrid_search()` | Fragile; RRF is the standard fix |
| 10 | No timeout/retry/fallback on Gemini | `answer()` | Unbounded behavior on slow/failed LLM calls |
| 11 | `print()` logging, no metrics/tracing/health | throughout | Not operable — can't debug or monitor in prod |
| 12 | No auth, rate limiting, or eval harness | — | No access control; no way to measure quality |

Note: the retrieval *pipeline design* (chunk → hybrid → rerank → generate) is
sound and worth keeping. The problems are the scaffolding around it.

---

## Target architecture

The key move: **make the app stateless and push all state into managed
Postgres.** For a single-tenant internal tool on serverless, one managed
Postgres instance can serve as the document store, the semantic index
(`pgvector`), *and* the keyword index (native full-text `tsvector`) — replacing
the singleton, the ephemeral Chroma collection, and the in-process BM25 index in
one stroke.

```
                         ┌─────────────────────────────┐
  Browser ── HTTPS ────► │  Serverless container        │
                         │  (Cloud Run / equivalent)    │
                         │  gunicorn/uvicorn, stateless │
                         └───────────┬─────────────────┘
                                     │
            ┌────────────────────────┼────────────────────────┐
            ▼                        ▼                         ▼
   ┌─────────────────┐   ┌───────────────────────┐   ┌──────────────────┐
   │ Gemini APIs     │   │ Managed Postgres      │   │ (Optional)       │
   │ - embeddings    │   │ - documents (metadata)│   │ managed reranker │
   │ - generation    │   │ - chunks + pgvector   │   │ (Cohere / Jina)  │
   │                 │   │ - tsvector (keyword)  │   │                  │
   └─────────────────┘   └───────────────────────┘   └──────────────────┘
```

### Component decisions

**Vector + keyword store → managed Postgres with `pgvector` + `tsvector`.**
One managed database (e.g. Neon or Supabase Postgres) holds:
- `documents` — id, name, content hash, uploaded_at, status, active flag.
- `chunks` — document_id, chunk text, header metadata (Title/Section/Subsection),
  `embedding vector`, and a generated `tsvector` column for keyword search.

This directly replaces flaws #1, #4, and the ephemeral Chroma store. Both arms of
hybrid search become SQL queries: pgvector `<=>` for semantic, `websearch_to_tsquery`
+ `ts_rank` for keyword. The app holds no retrieval state, so the global `RLock`
(#2) disappears and requests scale horizontally on serverless.

Why not Chroma server mode / Qdrant / Pinecone? All viable, but for
single-tenant + managed + "already need a metadata DB anyway," a single Postgres
is fewer services to run and bill. Revisit only if corpus size or latency
outgrows pgvector (not a concern for a traffic-laws-sized KB).

**Embeddings → Google `text-embedding-004` (Gemini) via API.**
Removes the in-process sentence-transformer (#3), the ~18s cold start, and keeps
you single-provider/single-key. Serverless-friendly (no model weights in the
image, no GPU). Trade-off: a network hop per embedding and per query — negligible
at this scale, and batched at ingest time.

**Reranker → managed rerank API (Cohere Rerank or Jina), or drop it.**
The in-process cross-encoder (#3) is the worst fit for serverless (heavy weights,
CPU-bound, big image). Two options:
- *Keep reranking:* call a managed rerank API on the top ~10 hybrid candidates.
  Highest quality; adds one dependency + one API key.
- *Drop reranking:* rely on pgvector + tsvector fused with RRF. Simpler and
  cheaper; for a small internal KB this is often "good enough."

Recommendation: **ship without the reranker first** (simpler Phase 1), add a
managed reranker in Phase 3 only if evaluation shows retrieval quality needs it.

**Generation → keep Gemini (`gemini-2.5-flash`), add timeout + retry + fallback.**
Wrap the call with a bounded timeout, retries with backoff, and a graceful
degraded message (fixes #10).

**Serving → serverless container (Cloud Run or equivalent), gunicorn/uvicorn.**
Stateless handlers, scale-to-zero, autoscaling. Because state lives in Postgres,
multiple instances are safe (fixes #2, #7). Note: **the current in-memory BM25
would be inconsistent across autoscaled instances** — moving keyword search into
Postgres `tsvector` is what makes serverless viable, not just nicer.

**Uploads → synchronous with a status field (single-tenant, low volume).**
At this scale a full job queue (Celery/RQ) is over-engineering. Store an
`ingesting | ready | failed` status on the `documents` row; the upload endpoint
kicks off chunk+embed+insert and the UI polls status. Revisit a real queue only
if documents get large enough that ingest exceeds request timeouts.

---

## Retrieval pipeline fixes (independent of infra)

- **Cap chunk size (#8):** after `MarkdownHeaderTextSplitter`, run a
  `RecursiveCharacterTextSplitter` (e.g. 1000/200) over each header chunk so a
  long header-less section can't become one giant chunk. Preserve header
  metadata on the sub-chunks.
- **Reciprocal Rank Fusion (#9):** replace interleave+exact-dedup with RRF over
  the semantic and keyword result lists (`score = Σ 1/(k + rank)`, k≈60). ~10
  lines, and dedup by chunk id instead of exact string.
- **Config-driven knobs:** move model IDs, top-k, chunk size, and RRF-k out of
  code constants into env/config so they're tunable without a deploy.

---

## Reliability & operations

- **Health/readiness:** add `/health` (process up) and `/ready` (Postgres +
  Gemini reachable) endpoints for the platform's probes.
- **Structured logging (#11):** replace `print()` with JSON logs carrying a
  per-request id; log retrieval counts, latencies, and model errors.
- **Metrics & tracing:** request rate/latency/error metrics; trace the
  retrieve→rerank→generate stages (OpenTelemetry, or LangSmith for LLM-native
  tracing). Even for an internal tool, you need to answer "why was that answer
  slow/wrong."
- **Timeouts & retries:** on both the Gemini call and Postgres queries; fail soft
  with the existing `{"answer": ..., "sources": []}` contract.
- **Secrets:** `GOOGLE_API_KEY` and the Postgres URL come from the platform's
  secret manager, never from a committed `.env`. Rotate any key ever committed.

---

## Security (right-sized for single-tenant internal)

Deliberately lightweight — no per-tenant isolation needed:

- **AuthN:** front the app with SSO / an identity-aware proxy (e.g. IAP) or, at
  minimum, an authenticated reverse proxy. No public unauthenticated access.
- **Rate limiting:** a simple per-user/IP limit to bound Gemini spend.
- **Upload validation:** keep the extension + 2 MB limit; add content-type
  sniffing and treat uploaded text as untrusted (it flows into the LLM prompt —
  be aware of prompt-injection from document content).
- **CORS:** lock to the app's own origin.

---

## Evaluation & testing (currently zero)

- **Golden Q&A set:** a small fixture of question → expected-answer/expected-source
  pairs for the traffic-laws KB.
- **RAG eval harness:** ragas or promptfoo to score retrieval hit-rate and answer
  faithfulness/relevance; run in CI on changes to chunking, fusion, or prompts.
- **Regression gate:** fail CI if retrieval hit-rate or faithfulness drops below a
  threshold. This is what lets you change the pipeline safely.

---

## Phased plan (highest ROI first)

### Phase 0 — Stabilize ✅ DONE
- Pinned dependencies in `requirements.txt` + full `requirements.lock`.
- Migrated off deprecated LangChain imports (`langchain-huggingface`; Chroma later removed entirely in Phase 1).
- gunicorn + `Procfile`; `app.run()` is dev-only.
- Request-scoped structured logging + `/health`.
- Gemini call: timeout + tenacity retries that skip permanent errors.
- Chunk-size cap after header split + RRF fusion; config-driven knobs.

### Phase 1 — De-globalize onto Postgres ✅ DONE
- Schema applied idempotently at boot via `pg_store.ensure_schema()`; reference DDL in `migrations/001_init.sql`. Extension bootstrap handled up front to avoid a concurrent-`CREATE EXTENSION` race.
- `documents` + `chunks` (with `embedding vector` + generated `tsvector`); partial unique index enforces one active document.
- Retrieval rewritten as SQL: pgvector semantic + `tsvector` keyword, fused with RRF **in a single query** (`pg_store.hybrid_search`).
- Singleton doc-state and the global `RLock` removed — `RAGService` is stateless over Postgres; verified 20 concurrent retrievals + shared state across 2 gunicorn workers.
- Uploads ingest into Postgres with a `status` field; content-hash reuse avoids re-embedding identical docs.
- **Embeddings:** kept pluggable (`EMBEDDING_PROVIDER=local|google`). Default stays local HuggingFace so the pipeline is testable now; flip to Google `text-embedding-004` (set `EMBEDDING_DIM=768`, re-ingest) once the `GOOGLE_API_KEY` is rotated.
- **Deferred to Phase 2:** provision the *managed* Postgres (Neon/Supabase) and point `DATABASE_URL` at it — the code is DB-agnostic and already runs on standard Postgres+pgvector (validated locally on pg16 + pgvector 0.8.5). UI status-polling on upload is still a small front-end follow-up.

### Phase 2 — Serverless deploy & harden ✅ code DONE (deploy pending an account)
- **`Dockerfile`** — lean production image: CPU-only torch, embedding+reranker models **baked in** and run offline (`HF_HUB_OFFLINE=1`), non-root user, `$PORT` bind, `HEALTHCHECK`. `.dockerignore` keeps the context small.
- **App hardening** (verified locally under gunicorn): security headers (nosniff/frame/referrer/HSTS), configurable **CORS lockdown**, **per-IP rate limiting** (flask-limiter; probes exempt; 429 confirmed), optional **`APP_API_KEY`** gate for the JSON API, and **ProxyFix** for running behind the platform proxy.
- **Robustness fixes found during verification:** schema is ensured at **startup** (so `/ready` & `/documents` work before the first `/ask`), and `EMBEDDING_DEVICE` lets forking servers avoid the macOS Metal/MPS `fork()` crash (Linux is CPU-only, so unaffected).
- **`/ready`** probe (DB reachability + active doc) — done in Phase 1.
- **[docs/DEPLOYMENT.md](DEPLOYMENT.md)** — Cloud Run + Neon/Supabase Postgres + Secret Manager + IAP walkthrough and full env reference.
- **Pending (needs an account, not code):** build/push the image, provision managed Postgres, wire secrets, deploy behind IAP. The image couldn't be built locally (no Docker here); the `Dockerfile` is written but unbuilt.

### Phase 3 — Operate & measure ✅ core DONE
- **Eval harness** ([eval/run_eval.py](../eval/run_eval.py)) — golden Q&A set ([eval/dataset.json](../eval/dataset.json), 12 cases across all 7 titles) scoring retrieval hit@1/hit@k/MRR against the real pgvector pipeline. Runs **without the LLM** (retrieval-only), so it works despite the leaked key; optional `--with-answers` scores answer faithfulness once a key is live. **Regression gate:** exits non-zero if hit@k < threshold (verified: exit 0 pass / exit 1 fail). Local run: hit@3 = 1.00, hit@1 = 0.92, MRR = 0.958.
- **Metrics** — Prometheus `/metrics` (prometheus-flask-exporter): per-endpoint request latency + counts, plus a custom `rag_ask_total{outcome}` counter. Exempt from rate limiting/auth.
- **Distributed tracing** ([tracing.py](../tracing.py)) — OpenTelemetry, opt-in via `OTEL_ENABLED` (no-op otherwise). Spans wrap each pipeline stage (`rag.embed_query` → `rag.hybrid_search` → `rag.rerank` → `rag.generate`) nested under the Flask request span, with exceptions recorded on the span. OTLP exporter for production (point at any collector), console exporter for local debugging; probes/metrics excluded. Verified locally via the console exporter.
- **CI** ([.github/workflows/ci.yml](../.github/workflows/ci.yml)) — spins up `pgvector/pgvector:pg16`, runs the eval gate on every push/PR, and **builds the Docker image** (the build validation that couldn't run locally without Docker).
- **Managed reranker:** not added — eval shows the local reranker already gives hit@3 = 1.00, so it isn't needed (as the roadmap predicted).
- **Remaining (needs cloud creds, not code):** wire registry push + deploy into CI; stand up a trace collector/dashboard (Tempo/Honeycomb/etc.) and set `OTEL_EXPORTER_OTLP_ENDPOINT`.

---

## What we intentionally are NOT building

Given single-tenant/internal, these would be over-engineering now:
- Multi-tenancy, per-tenant vector isolation, org/user management.
- A self-hosted vector database or self-hosted model-serving tier.
- A distributed job queue (synchronous ingest with status is enough).
- Multiple LLM providers / gateway routing.

Each can be added later if scale changes — the Postgres-centric, stateless design
doesn't block any of them.

---

## Open decisions to confirm before Phase 1

1. **Managed Postgres provider** — Neon vs. Supabase (both give managed Postgres +
   pgvector; Supabase also bundles auth if you want it).
2. **Serverless platform** — Google Cloud Run (natural fit with the Google/Gemini
   stack) vs. another container platform you already use.
3. **Reranker** — ship without it first (recommended), or wire a managed reranker
   from day one.
4. **Auth mechanism** — SSO/IAP vs. Supabase Auth vs. a simple reverse proxy.
