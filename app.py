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
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

import pg_store
from rag_backend import get_answer, process_new_document, rag_system


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


def allowed_file(filename):
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


@app.before_request
def assign_request_id():
    g.request_id = uuid.uuid4().hex[:8]


@app.after_request
def log_request(response):
    logger.info("%s %s -> %d", request.method, request.path, response.status_code)
    return response


@app.route('/')
def home():
    return render_template('index.html')


@app.route('/health')
def health():
    """Liveness probe: 200 whenever the process is up. Reports readiness as info."""
    return jsonify({"status": "ok", "ready": rag_system.is_ready})


@app.route('/ready')
def ready():
    """Readiness probe: 200 only when the database is reachable. Reports the
    active document if one is loaded."""
    db_ok = pg_store.healthcheck()
    active = pg_store.get_active_document() if db_ok else None
    payload = {
        "database": "ok" if db_ok else "unavailable",
        "document": active["name"] if active else None,
    }
    return jsonify(payload), (200 if db_ok else 503)


@app.route('/documents')
def documents():
    return jsonify({"documents": pg_store.list_documents()})


@app.route('/ask', methods=['POST'])
def ask():
    data = request.get_json(silent=True) or {}
    query = data.get('query', '').strip()
    if not query:
        return jsonify({"error": "No query provided"}), 400

    result = get_answer(query)
    return jsonify(result)


@app.route('/upload', methods=['POST'])
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
