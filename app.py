import hmac
import logging
import os
import uuid
from pathlib import Path

from dotenv import load_dotenv

# Load environment variables from a local .env file (GOOGLE_API_KEY, FLASK_DEBUG,
# and the RAG_* / LLM_* tunables) before importing the RAG backend, which reads
# them at import time.
load_dotenv()

from flask import Flask, g, has_request_context, jsonify, render_template, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
from prometheus_flask_exporter import PrometheusMetrics
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

import pg_store
from rag_backend import EMBEDDING_DIM, get_answer, process_new_document, rag_system


# --- Phase 2 hardening config (env-overridable) ---
# Number of trusted reverse proxies in front of the app. Cloud Run / most PaaS
# put exactly 1. Left at 0 by default so a directly-exposed dev server does not
# trust spoofable X-Forwarded-* headers.
TRUSTED_PROXY_COUNT = int(os.environ.get("TRUSTED_PROXY_COUNT", "0"))
# Optional bearer/API-key gate for the JSON API (off by default). When set, /ask,
# /upload and /documents require the key. Intended for programmatic clients; for
# the browser UI use platform SSO/IAP instead (which authenticates the session).
APP_API_KEY = os.environ.get("APP_API_KEY", "").strip()
_PROTECTED_PATHS = ("/ask", "/upload", "/documents")
# Comma-separated allowed CORS origins. Empty => no CORS headers (same-origin only).
CORS_ALLOWED_ORIGINS = {
    o.strip() for o in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",") if o.strip()
}
ENABLE_HSTS = os.environ.get("ENABLE_HSTS", "").lower() in {"1", "true", "yes"}
RATE_LIMIT_DEFAULT = os.environ.get("RATE_LIMIT_DEFAULT", "200 per hour")
RATE_LIMIT_ASK = os.environ.get("RATE_LIMIT_ASK", "30 per minute")
RATE_LIMIT_UPLOAD = os.environ.get("RATE_LIMIT_UPLOAD", "10 per minute")
RATE_LIMIT_STORAGE_URI = os.environ.get("RATE_LIMIT_STORAGE_URI", "memory://")


class RequestIdFilter(logging.Filter):
    """Attach the current request id (or '-') to every log record."""

    def filter(self, record):
        record.request_id = g.get("request_id", "-") if has_request_context() else "-"
        return True


def configure_logging():
    """Configure structured, request-scoped logging for app and gunicorn workers.

    Done at import time (not just under __main__) so it also applies when the app
    is served by gunicorn, which imports this module rather than executing it.
    """
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler()
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [req:%(request_id)s] %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


configure_logging()
logger = logging.getLogger(__name__)

app = Flask(__name__)
BASE_DIR = Path(__file__).resolve().parent
ALLOWED_EXTENSIONS = {'.md', '.txt'}

app.config['UPLOAD_FOLDER'] = BASE_DIR / 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Trust the platform proxy's X-Forwarded-* headers (correct client IP for rate
# limiting, correct scheme for HTTPS awareness) only when configured to.
if TRUSTED_PROXY_COUNT > 0:
    app.wsgi_app = ProxyFix(
        app.wsgi_app, x_for=TRUSTED_PROXY_COUNT, x_proto=TRUSTED_PROXY_COUNT,
        x_host=TRUSTED_PROXY_COUNT, x_port=TRUSTED_PROXY_COUNT,
    )

# Per-client rate limiting to bound abuse and Gemini spend. In-memory storage is
# fine for a single instance; set RATE_LIMIT_STORAGE_URI to a shared Redis when
# running multiple instances so limits are enforced across them.
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=[RATE_LIMIT_DEFAULT],
    storage_uri=RATE_LIMIT_STORAGE_URI,
)

# Prometheus instrumentation. path=None disables the library's own /metrics route
# so we can register one that is exempt from rate limiting below. Auto-collects
# per-endpoint request latency + counts; plus a custom /ask outcome counter.
metrics = PrometheusMetrics(app, path=None, group_by="path")
ASK_OUTCOMES = Counter("rag_ask_total", "Answers served by /ask, by outcome", ["outcome"])


# Create the DB schema at startup so read endpoints (/ready, /documents) work
# before the first /ask. Tolerant of a temporarily-unavailable DB: the app still
# boots (so probes can report 503) and the schema is retried on first ingest.
try:
    pg_store.ensure_schema(EMBEDDING_DIM)
except Exception:
    logger.warning("Could not ensure DB schema at startup; will retry on first use.", exc_info=True)


def allowed_file(filename):
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


def _api_key_ok():
    """Constant-time check of the bearer/API key against APP_API_KEY."""
    provided = request.headers.get("X-API-Key") or ""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        provided = auth[len("Bearer "):]
    return bool(provided) and hmac.compare_digest(provided, APP_API_KEY)


@app.before_request
def assign_request_id():
    g.request_id = uuid.uuid4().hex[:8]


@app.before_request
def require_api_key():
    """Gate the JSON API when APP_API_KEY is configured (no-op otherwise)."""
    if not APP_API_KEY or request.method == "OPTIONS":
        return None
    if request.path in _PROTECTED_PATHS and not _api_key_ok():
        return jsonify({"error": "Unauthorized"}), 401
    return None


@app.after_request
def apply_cors(response):
    """Echo an allowed Origin (CORS lockdown); no headers => same-origin only."""
    origin = request.headers.get("Origin")
    if origin and origin in CORS_ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-API-Key"
        response.headers.add("Vary", "Origin")
    return response


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if ENABLE_HSTS:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


@app.after_request
def log_request(response):
    logger.info("%s %s -> %d", request.method, request.path, response.status_code)
    return response


@app.route('/')
def home():
    return render_template('index.html')


@app.route('/health')
@limiter.exempt
def health():
    """Liveness probe: 200 whenever the process is up. Reports readiness as info."""
    return jsonify({"status": "ok", "ready": rag_system.is_ready})


@app.route('/ready')
@limiter.exempt
def ready():
    """Readiness probe: 200 only when the database is reachable. Reports the
    active document if one is loaded."""
    db_ok = pg_store.healthcheck()
    active = None
    if db_ok:
        try:
            active = pg_store.get_active_document()
        except Exception:
            logger.exception("Failed to read active document")
    payload = {
        "database": "ok" if db_ok else "unavailable",
        "document": active["name"] if active else None,
    }
    return jsonify(payload), (200 if db_ok else 503)


@app.route('/metrics')
@limiter.exempt
@metrics.do_not_track()
def prometheus_metrics():
    """Prometheus scrape endpoint (open + unthrottled; scrape internally only)."""
    return generate_latest(), 200, {'Content-Type': CONTENT_TYPE_LATEST}


@app.route('/documents')
def documents():
    try:
        return jsonify({"documents": pg_store.list_documents()})
    except Exception:
        logger.exception("Failed to list documents")
        return jsonify({"error": "Database unavailable"}), 503


@app.route('/ask', methods=['POST'])
@limiter.limit(RATE_LIMIT_ASK)
def ask():
    data = request.get_json(silent=True) or {}
    query = data.get('query', '').strip()
    if not query:
        return jsonify({"error": "No query provided"}), 400

    result = get_answer(query)
    ASK_OUTCOMES.labels(outcome="answered" if result.get("sources") else "no_context").inc()
    return jsonify(result)


@app.route('/upload', methods=['POST'])
@limiter.limit(RATE_LIMIT_UPLOAD)
def upload_file():
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400

    if not allowed_file(file.filename):
        return jsonify({"error": "Only .md and .txt files are supported"}), 400

    filename = secure_filename(file.filename)
    if not filename:
        return jsonify({"error": "Invalid filename"}), 400

    filepath = app.config['UPLOAD_FOLDER'] / filename

    try:
        file.save(filepath)
        success = process_new_document(str(filepath))
    except Exception:
        logger.exception("Failed to process uploaded document")
        return jsonify({"error": "Failed to process document"}), 500

    if success:
        return jsonify({"message": f"Successfully loaded {filename}", "filename": filename})
    return jsonify({"error": "Failed to process document"}), 500


@app.errorhandler(RequestEntityTooLarge)
def handle_file_too_large(_error):
    return jsonify({"error": "File is too large. Maximum upload size is 2 MB."}), 413


if __name__ == '__main__':
    # Local development only. In production serve with gunicorn (see Procfile):
    #   gunicorn --workers 2 --timeout 120 --bind 0.0.0.0:$PORT app:app
    debug = os.environ.get('FLASK_DEBUG', '').lower() in {'1', 'true', 'yes'}
    app.run(debug=debug, port=5000)
