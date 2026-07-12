from flask import Flask, render_template, request, jsonify
from rag_backend import get_answer, process_new_document
import os
from pathlib import Path
from werkzeug.utils import secure_filename
from werkzeug.exceptions import RequestEntityTooLarge

app = Flask(__name__)
BASE_DIR = Path(__file__).resolve().parent
ALLOWED_EXTENSIONS = {'.md', '.txt'}

app.config['UPLOAD_FOLDER'] = BASE_DIR / 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)


def allowed_file(filename):
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS

@app.route('/')
def home():
    return render_template('index.html')

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
        app.logger.exception("Failed to process uploaded document")
        return jsonify({"error": "Failed to process document"}), 500

    if success:
        return jsonify({"message": f"Successfully loaded {filename}", "filename": filename})
    return jsonify({"error": "Failed to process document"}), 500


@app.errorhandler(RequestEntityTooLarge)
def handle_file_too_large(_error):
    return jsonify({"error": "File is too large. Maximum upload size is 2 MB."}), 413

if __name__ == '__main__':
    debug = os.environ.get('FLASK_DEBUG', '').lower() in {'1', 'true', 'yes'}
    app.run(debug=debug, port=5000)
