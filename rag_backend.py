import hashlib
import logging
import os
import threading

logger = logging.getLogger(__name__)


def _enable_hf_offline_if_cached():
    """Enable HF_HUB_OFFLINE before any HuggingFace import when both models are
    already cached locally, so cold starts skip network HEAD checks (faster,
    quieter logs). No-op if the user set HF_HUB_OFFLINE explicitly or a model is
    not cached yet, so first-run downloads still work.

    Must run before importing sentence_transformers / langchain_huggingface and
    uses only the filesystem: importing huggingface_hub to inspect the cache
    would read the flag too early for a later change to take effect.
    """
    if os.environ.get("HF_HUB_OFFLINE"):
        return
    cache_root = (
        os.environ.get("HF_HUB_CACHE")
        or os.environ.get("HUGGINGFACE_HUB_CACHE")
        or os.path.join(
            os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface"),
            "hub",
        )
    )

    def _cached(name):
        candidates = [name] if "/" in name else [name, "sentence-transformers/" + name]
        for repo in candidates:
            snapshots = os.path.join(cache_root, "models--" + repo.replace("/", "--"), "snapshots")
            if os.path.isdir(snapshots) and any(os.scandir(snapshots)):
                return True
        return False

    emb = os.environ.get("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
    rer = os.environ.get("RERANKER_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2")
    if _cached(emb) and _cached(rer):
        os.environ["HF_HUB_OFFLINE"] = "1"
        logger.info("HuggingFace models cached; enabled HF_HUB_OFFLINE for faster startup.")


_enable_hf_offline_if_cached()

from langchain_core.output_parsers import StrOutputParser  # noqa: E402
from langchain_core.prompts import ChatPromptTemplate  # noqa: E402
from langchain_google_genai import ChatGoogleGenerativeAI  # noqa: E402
from langchain_huggingface import HuggingFaceEmbeddings  # noqa: E402
from langchain_text_splitters import (  # noqa: E402
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)
from tenacity import (  # noqa: E402
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

import pg_store  # noqa: E402


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        logger.warning("Invalid int for %s; using default %s", name, default)
        return default


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        logger.warning("Invalid float for %s; using default %s", name, default)
        return default


def _env_bool(name, default):
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


# --- Configuration (env-overridable so these can be tuned without a code change) ---
DEFAULT_DOCUMENT = os.environ.get("DEFAULT_DOCUMENT", "traffic_laws.md")
EMBEDDING_PROVIDER = os.environ.get("EMBEDDING_PROVIDER", "local").lower()  # local | google
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
GOOGLE_EMBEDDING_MODEL = os.environ.get("GOOGLE_EMBEDDING_MODEL", "models/text-embedding-004")
# Torch device for the local embedder/reranker: "" (auto), "cpu", "cuda", "mps".
# Forking servers (gunicorn) on macOS must use "cpu" — initializing Metal/MPS in a
# forked worker crashes. Production (Linux) is CPU-only, so auto == cpu there.
EMBEDDING_DEVICE = os.environ.get("EMBEDDING_DEVICE", "").strip()
# Vector dimension must match the embedding model: 384 for all-MiniLM-L6-v2,
# 768 for Google text-embedding-004.
EMBEDDING_DIM = _env_int("EMBEDDING_DIM", 768 if EMBEDDING_PROVIDER == "google" else 384)

RERANKER_MODEL = os.environ.get("RERANKER_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2")
RERANK_ENABLED = _env_bool("RERANK_ENABLED", True)
LLM_MODEL = os.environ.get("LLM_MODEL", "gemini-2.5-flash")

CHUNK_SIZE = _env_int("CHUNK_SIZE", 1000)
CHUNK_OVERLAP = _env_int("CHUNK_OVERLAP", 200)

ARM_TOP_K = _env_int("RETRIEVAL_ARM_TOP_K", 10)   # depth of each retrieval arm (SQL)
HYBRID_CANDIDATES = _env_int("HYBRID_CANDIDATES", 10)  # kept after RRF, before rerank
RERANK_TOP_K = _env_int("RERANK_TOP_K", 3)        # final chunks sent to the LLM
RRF_K = _env_int("RRF_K", 60)                     # Reciprocal Rank Fusion constant

LLM_TIMEOUT = _env_float("LLM_TIMEOUT", 30.0)     # seconds per LLM request
LLM_MAX_ATTEMPTS = _env_int("LLM_MAX_ATTEMPTS", 3)  # total tries (1 + retries)


# Substrings that mark a permanent LLM failure (bad/leaked/invalid key, malformed
# request). Retrying these wastes time and quota, so the retry policy skips them
# and lets the error propagate straight to answer()'s fail-soft handler.
_NON_RETRYABLE_LLM_TOKENS = (
    "PERMISSION_DENIED",
    "UNAUTHENTICATED",
    "INVALID_ARGUMENT",
    "NOT_FOUND",
    "FAILED_PRECONDITION",
    "API_KEY_INVALID",
    "API KEY NOT VALID",
    "REPORTED AS LEAKED",
)


def _exc_chain(exc):
    chain, seen, current = [], set(), exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _is_retryable_llm_error(exc):
    """Retry transient LLM errors (timeouts, 429/5xx) but not permanent ones."""
    text = " ".join(str(e) for e in _exc_chain(exc)).upper()
    return not any(token in text for token in _NON_RETRYABLE_LLM_TOKENS)


class RAGService:
    """Stateless-over-Postgres RAG service.

    Holds only read-only, shareable resources (embedder, reranker, LLM chain,
    connection pool). All document/chunk state lives in Postgres, so requests are
    safe to run concurrently and across workers — there is no per-document mutable
    state and no lock around answering.
    """

    def __init__(self):
        self.embedder = None
        self.reranker = None
        self.llm = None
        self.rag_chain = None
        self._ready = False
        self._init_lock = threading.Lock()

    @property
    def is_ready(self):
        return self._ready

    def _ensure_ready(self):
        """Lazily build models/schema once. The lock only guards one-time init,
        never the per-request answer path."""
        if self._ready:
            return True
        with self._init_lock:
            if self._ready:
                return True
            try:
                self.embedder = self._build_embedder()
                if RERANK_ENABLED and self.reranker is None:
                    from sentence_transformers import CrossEncoder
                    self.reranker = CrossEncoder(RERANKER_MODEL, device=EMBEDDING_DEVICE or None)
                if self.llm is None:
                    self.llm = ChatGoogleGenerativeAI(
                        model=LLM_MODEL, temperature=0,
                        timeout=LLM_TIMEOUT, max_retries=0,
                    )
                    self.rag_chain = self._build_chain()
                pg_store.ensure_schema(EMBEDDING_DIM)
            except Exception:
                logger.exception("Failed to initialize RAG service")
                return False

            # Seed the default document if nothing is active yet.
            if pg_store.get_active_document() is None and os.path.exists(DEFAULT_DOCUMENT):
                try:
                    self._ingest(DEFAULT_DOCUMENT)
                except Exception:
                    logger.exception("Failed to ingest default document %s", DEFAULT_DOCUMENT)

            self._ready = True
            return True

    @staticmethod
    def _build_embedder():
        if EMBEDDING_PROVIDER == "google":
            from langchain_google_genai import GoogleGenerativeAIEmbeddings
            logger.info("Using Google embeddings: %s", GOOGLE_EMBEDDING_MODEL)
            return GoogleGenerativeAIEmbeddings(model=GOOGLE_EMBEDDING_MODEL)
        logger.info("Using local embeddings: %s (device=%s)", EMBEDDING_MODEL, EMBEDDING_DEVICE or "auto")
        model_kwargs = {"device": EMBEDDING_DEVICE} if EMBEDDING_DEVICE else {}
        return HuggingFaceEmbeddings(model_name=EMBEDDING_MODEL, model_kwargs=model_kwargs)

    # ---- Ingestion -------------------------------------------------------

    def ingest_document(self, file_path):
        """Public entry: ingest/activate a document. Returns True on success."""
        if not self._ensure_ready():
            return False
        try:
            return self._ingest(file_path)
        except Exception:
            logger.exception("Failed to ingest document %s", file_path)
            return False

    def _ingest(self, file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            raw_text = f.read()
        content_hash = self._content_hash(raw_text)
        name = os.path.basename(file_path)

        # Reuse already-ingested identical content instead of re-embedding.
        existing = pg_store.get_document_by_hash(content_hash)
        if existing and existing["status"] == "ready":
            logger.info("Reusing already-ingested document %s (id=%s)", name, existing["id"])
            pg_store.set_active_document(existing["id"])
            return True

        docs = self._split_document(file_path, raw_text)
        if not docs:
            logger.warning("No chunks were created from %s", file_path)
            return False

        doc_id = pg_store.begin_document(name, content_hash)
        try:
            embeddings = self.embedder.embed_documents([d.page_content for d in docs])
            rows = [
                (i, d.page_content, d.metadata, embeddings[i])
                for i, d in enumerate(docs)
            ]
            pg_store.insert_chunks(doc_id, rows)
            pg_store.finalize_document(doc_id, len(rows))
            pg_store.set_active_document(doc_id)
        except Exception as exc:
            pg_store.fail_document(doc_id, exc)
            raise
        logger.info("Ingested %s (id=%s, %d chunks).", name, doc_id, len(docs))
        return True

    # ---- Answering -------------------------------------------------------

    def answer(self, query):
        if not self._ensure_ready():
            return self._msg("System not initialized. Check the database connection and logs.")

        active = pg_store.get_active_document()
        if active is None:
            return self._msg("No document is loaded. Upload a .md or .txt file first.")

        try:
            retrieved = self._retrieve(active["id"], query)
            if not retrieved:
                return self._msg("I couldn't find any relevant information in the document.")
            response = self._invoke_chain(
                self._format_docs(retrieved), query, active["name"]
            )
        except Exception:
            logger.exception("Error answering query")
            return self._msg("I encountered an error while answering. Please try again.")

        return {
            "answer": response,
            "sources": [
                {"content": r["content"], "metadata": r["metadata"]} for r in retrieved
            ],
        }

    def _retrieve(self, document_id, query):
        return self.rank_candidates(document_id, query, RERANK_TOP_K)

    def rank_candidates(self, document_id, query, top_k):
        """Hybrid search + optional rerank, returning the top_k ranked chunks.

        Used by answering (top_k=RERANK_TOP_K) and by the eval harness (larger
        top_k, to measure rank-based metrics like MRR).
        """
        query_embedding = self.embedder.embed_query(query)
        candidates = pg_store.hybrid_search(
            document_id, query, query_embedding,
            arm_k=ARM_TOP_K, rrf_k=RRF_K, candidates=HYBRID_CANDIDATES,
        )
        if not candidates:
            return []
        if not RERANK_ENABLED or self.reranker is None:
            return candidates[:top_k]
        pairs = [[query, c["content"]] for c in candidates]
        scores = self.reranker.predict(pairs)
        ranked = sorted(zip(candidates, scores), key=lambda item: item[1], reverse=True)
        return [c for c, _score in ranked[:top_k]]

    @retry(
        reraise=True,
        stop=stop_after_attempt(LLM_MAX_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception(_is_retryable_llm_error),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
    def _invoke_chain(self, context, question, doc_name):
        return self.rag_chain.invoke(
            {"context": context, "question": question, "doc_name": doc_name}
        )

    # ---- Helpers ---------------------------------------------------------

    def _split_document(self, file_path, raw_text):
        size_splitter = RecursiveCharacterTextSplitter(
            chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP
        )
        if file_path.lower().endswith(".md"):
            header_splitter = MarkdownHeaderTextSplitter(
                headers_to_split_on=[
                    ("#", "Title"),
                    ("##", "Section"),
                    ("###", "Subsection"),
                ]
            )
            return size_splitter.split_documents(header_splitter.split_text(raw_text))
        return size_splitter.create_documents([raw_text])

    def _build_chain(self):
        template = """
You are a helpful assistant. You are currently answering questions based on the document: "{doc_name}".
Answer the question based ONLY on the following context.
If the answer is not in the context, say "I couldn't find that information in the uploaded document."

Context:
{context}

Question: {question}
"""
        prompt = ChatPromptTemplate.from_template(template)
        return prompt | self.llm | StrOutputParser()

    @staticmethod
    def _format_docs(rows):
        return "\n\n".join(r["content"] for r in rows)

    @staticmethod
    def _content_hash(raw_text):
        return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

    @staticmethod
    def _msg(text):
        return {"answer": text, "sources": []}


rag_system = RAGService()


def process_new_document(file_path):
    """Public API to ingest and activate a document."""
    return rag_system.ingest_document(file_path)


def get_answer(query):
    return rag_system.answer(query)
