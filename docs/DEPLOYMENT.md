# Deployment (Phase 2)

How to run Traffic RAG in production: a container on a serverless platform, a
managed Postgres, secrets from a secret manager, and SSO in front. Written for
Google Cloud Run (natural fit with the Gemini stack) but the container is
portable to any serverless container host (Fly, Render, Railway, ECS).

## Prerequisites

- A managed **Postgres with pgvector**: Neon or Supabase (both support pgvector).
  Get its connection string → `DATABASE_URL`.
- A **Gemini API key** (`GOOGLE_API_KEY`). Rotate the previously-leaked one.
- The container image (built from the repo `Dockerfile`).

## 1. Provision managed Postgres

Neon or Supabase — create a project and a database. Enable pgvector:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

The app also runs `ensure_schema()` at startup and creates the extension/tables
itself, so this is only needed if your provider restricts `CREATE EXTENSION` to a
manual step. The schema is in [../migrations/001_init.sql](../migrations/001_init.sql).

Copy the connection string (SSL is typically required, e.g.
`postgresql://user:pass@host/db?sslmode=require`).

## 2. Build & push the image

```bash
# Cloud Run via Artifact Registry
gcloud builds submit --tag REGION-docker.pkg.dev/PROJECT/REPO/traffic-rag
```

The `Dockerfile` bakes the embedding + reranker models into the image and runs
fully offline (`HF_HUB_OFFLINE=1`), so cold starts do no model network I/O. It
runs as a non-root user and uses CPU-only torch.

## 3. Store secrets (do NOT put them in env vars in the clear)

```bash
printf '%s' "$GOOGLE_API_KEY" | gcloud secrets create google-api-key --data-file=-
printf '%s' "$DATABASE_URL"   | gcloud secrets create database-url   --data-file=-
```

## 4. Deploy to Cloud Run

```bash
gcloud run deploy traffic-rag \
  --image REGION-docker.pkg.dev/PROJECT/REPO/traffic-rag \
  --region REGION \
  --set-secrets "GOOGLE_API_KEY=google-api-key:latest,DATABASE_URL=database-url:latest" \
  --set-env-vars "TRUSTED_PROXY_COUNT=1,ENABLE_HSTS=1,EMBEDDING_DEVICE=cpu" \
  --cpu 2 --memory 2Gi \
  --min-instances 0 --max-instances 4 \
  --no-allow-unauthenticated
```

Notes:
- `--min-instances 0` = scale to zero. The first request after idle pays a cold
  start (model load from the in-image cache, a few seconds on CPU).
- `--memory 2Gi` gives headroom for torch + the two models. Tune down if it fits.
- Cloud Run injects `PORT` (8080); the container binds to it. Workers default to
  `WEB_CONCURRENCY` (2). With scale-to-zero + in-memory rate limiting, keep
  workers modest or move rate-limit storage to Redis (see below).

## 5. Authentication (SSO for the browser UI)

The app has a **browser UI**, so authenticate the user's session at the platform
edge rather than with an app-level key:

- **Cloud Run + IAP:** put the service behind an external HTTPS load balancer with
  Identity-Aware Proxy, granting `roles/iap.httpsResourceAccessor` to your users.
  `--no-allow-unauthenticated` blocks unauthenticated traffic in the meantime.
- The optional **`APP_API_KEY`** gate (protects `/ask`, `/upload`, `/documents`)
  is for *programmatic* API clients, not the browser UI — enabling it would
  require the key in browser requests. Use IAP for humans; `APP_API_KEY` only if
  you expose the JSON API to scripts.

## 6. Configuration reference

| Concern | Env var(s) | Production value |
|---|---|---|
| Database | `DATABASE_URL` | managed Postgres URL (from Secret Manager) |
| LLM | `GOOGLE_API_KEY` | rotated key (from Secret Manager) |
| Behind proxy | `TRUSTED_PROXY_COUNT` | `1` (Cloud Run) |
| HTTPS | `ENABLE_HSTS` | `1` |
| Device | `EMBEDDING_DEVICE` | `cpu` |
| CORS | `CORS_ALLOWED_ORIGINS` | your frontend origin, or unset for same-origin |
| Rate limits | `RATE_LIMIT_ASK`, `RATE_LIMIT_UPLOAD`, `RATE_LIMIT_DEFAULT` | tune to expected load |
| Rate-limit store | `RATE_LIMIT_STORAGE_URI` | `redis://…` if running >1 instance |
| Workers | `WEB_CONCURRENCY` | `2`+ |

## Operational notes

- **Probes:** `/health` (liveness, always 200 when up) and `/ready` (503 until the
  DB is reachable) are exempt from rate limiting and auth. Point Cloud Run's
  startup/liveness checks at them.
- **Embeddings provider:** default is local (in-image, 384-dim). To use Google
  `text-embedding-004` instead, set `EMBEDDING_PROVIDER=google` and
  `EMBEDDING_DIM=768`, then **re-ingest** documents (the vector column dimension
  must match). Costs a network hop per query but removes the model from the image.
- **Multi-instance rate limiting:** the default in-memory limiter is per-instance.
  With `--max-instances > 1`, point `RATE_LIMIT_STORAGE_URI` at a shared Redis
  (e.g. Upstash) so limits are global.
- **Uploads are ephemeral on serverless.** `/upload` writes the file to the
  container's local disk only to parse it; the durable copy is the chunks/embeddings
  in Postgres. A restart loses the local file but not the ingested document.

## What this does NOT include (future phases)

- Eval harness + CI regression gate (Phase 3).
- Metrics/tracing dashboards (Phase 3).
- Managed reranker (Phase 3, only if eval shows it's needed).
- A CI/CD pipeline (build → test → eval → deploy).
