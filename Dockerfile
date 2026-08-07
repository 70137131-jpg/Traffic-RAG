# Traffic RAG — production image (Phase 2).
# Multi-stage-free but lean: CPU-only torch, models baked in so cold starts do no
# network I/O, non-root runtime user.

FROM python:3.13-slim

# - Unbuffered logs so stdout streams to the platform log collector.
# - Models baked into the image cache; run fully offline at runtime.
# - CPU device: no GPU on serverless, and forking servers must avoid MPS/CUDA init.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/opt/models \
    HF_HUB_OFFLINE=1 \
    EMBEDDING_DEVICE=cpu \
    TRUSTED_PROXY_COUNT=1 \
    ENABLE_HSTS=1 \
    PORT=8080

WORKDIR /app

# Install Python deps. Pull torch from the CPU wheel index to avoid the large
# CUDA build (smaller image, faster cold start). psycopg[binary] bundles libpq,
# so no system Postgres client packages are needed.
COPY requirements.txt .
RUN pip install --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.txt

# Pre-download the embedding + reranker models into HF_HOME at build time so the
# running container needs no HuggingFace network access (HF_HUB_OFFLINE=1 above).
RUN python - <<'PY'
from sentence_transformers import SentenceTransformer, CrossEncoder
SentenceTransformer("all-MiniLM-L6-v2")
CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
PY

COPY . .

# Run as a non-root user; ensure it can read the model cache and write uploads.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/uploads \
    && chown -R appuser:appuser /app /opt/models
USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import os,urllib.request,sys; \
    sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8080')).status==200 else 1)"

# Cloud Run / most PaaS inject $PORT. Shell form so $PORT expands. --timeout 120
# covers first-request model load; workers default to WEB_CONCURRENCY or 2.
CMD gunicorn --workers ${WEB_CONCURRENCY:-2} --timeout 120 --bind 0.0.0.0:$PORT app:app
