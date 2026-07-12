# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# The working virtualenv in this repo is `venv/` (NOT `.venv/`, which is empty).
source venv/bin/activate
pip install -r requirements.txt          # pinned direct deps
# pip install -r requirements.lock        # fully reproducible (all transitive deps)

# Run — development (requires GOOGLE_API_KEY in .env or environment)
python app.py                             # http://127.0.0.1:5000
FLASK_DEBUG=1 python app.py               # Flask debug/reload

# Run — production serving (see Procfile)
gunicorn --workers 2 --timeout 120 --bind 0.0.0.0:5000 app:app
# --timeout 120 matters: the FIRST request lazily loads the embedding + reranker
# models (~15-20s) and would trip gunicorn's default 30s worker timeout.

# Health check
curl -s http://127.0.0.1:5000/health      # {"status":"ok","ready":<bool>,"document":...}
```

There is no test suite or linter configured yet (adding a RAG eval harness is Phase 3 of [docs/PRODUCTION_ROADMAP.md](docs/PRODUCTION_ROADMAP.md)). `rag_system_walkthrough.ipynb` is a standalone explainer notebook, not part of the runtime.

All models, chunk sizes, top-k values, and LLM timeout/retries are env-overridable — see `.env.example` for the full list.

## Architecture

A Flask app that answers questions against a single active document using a hybrid RAG pipeline. Two files hold all the logic:

- **[app.py](app.py)** — thin HTTP layer. Configures request-scoped structured logging (every log line carries a `[req:<id>]`), loads `.env` *before* importing `rag_backend` (the backend reads config at import time). Routes: `/` (UI), `/ask` (POST query → JSON answer + sources), `/upload` (POST `.md`/`.txt`, max 2 MB, swaps the active document), `/health` (liveness + readiness).
- **[rag_backend.py](rag_backend.py)** — the `RAGSystem` class plus a module-level singleton `rag_system` exposed via `get_answer()` and `process_new_document()`.

### Retrieval pipeline (order matters)

1. **Chunking** — `.md` files split by header (`MarkdownHeaderTextSplitter`, H1–H3 → `Title`/`Section`/`Subsection` metadata), then a `RecursiveCharacterTextSplitter` (CHUNK_SIZE/CHUNK_OVERLAP) caps chunk size while preserving header metadata; non-`.md` uses the recursive splitter directly.
2. **Hybrid search** — BM25 keyword (`rank_bm25`) + Chroma semantic (`all-MiniLM-L6-v2` embeddings), fused with **Reciprocal Rank Fusion** (`_reciprocal_rank_fusion`, keyed by `page_content`, RRF_K=60), top HYBRID_CANDIDATES.
3. **Rerank** — cross-encoder (`ms-marco-MiniLM-L-6-v2`) rescores candidates, keeps top RERANK_TOP_K.
4. **Generate** — Gemini (`gemini-2.5-flash`, temperature 0) answers from context only via a LangChain prompt chain, wrapped with a per-request timeout (LLM_TIMEOUT) and tenacity retries/backoff (LLM_MAX_ATTEMPTS) in `_invoke_chain`.

### Key design constraints

- **Global, single-document state.** `rag_system` is one process-wide singleton. Uploading a document rebuilds the vector store and *replaces* the active document for every user. This is a local single-user demo, not multi-user safe. (De-globalizing onto Postgres is Phase 1 of the roadmap.)
- **Persistent vector store.** Chroma uses a `PersistentClient(CHROMA_DIR)` with a **content-hash collection name** (`_collection_name`), so an unchanged document reuses its persisted embeddings across restarts/re-uploads instead of recomputing them. `chroma_db/` is gitignored.
- **Lazy, guarded init.** `initialize()` is guarded by an `RLock`; the reranker and LLM are created once and reused across document swaps. `answer()` triggers `initialize()` on first call if needed. Note: this same `RLock` serializes *all* of `answer()` (incl. the LLM call) — the system currently handles one query at a time (Phase 1 removes this).
- **Fail-soft.** Missing `GOOGLE_API_KEY`, missing document, empty chunks, and query/LLM errors all return a user-facing message dict `{"answer": ..., "sources": []}` rather than raising.

## Notes

- `GOOGLE_API_KEY` is required **only** for Gemini answer generation. Embeddings and the reranker run **locally** (HuggingFace/sentence-transformers), so retrieval works without it — a bad/leaked key fails only at the generation step. Keep the key in `.env` (gitignored); rotate any key ever committed.
- All models, chunk sizes, top-k values, RRF constant, and LLM timeout/retries are env-overridable config at the top of `rag_backend.py`; see `.env.example`.
- The roadmap for taking this to production lives in [docs/PRODUCTION_ROADMAP.md](docs/PRODUCTION_ROADMAP.md). Phase 0 (dependency pinning, off-deprecated LangChain imports, gunicorn, logging, health, LLM timeout/retry, RRF, chunk-size cap) is done.
