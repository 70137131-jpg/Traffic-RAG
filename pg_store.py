"""Postgres + pgvector data layer for Traffic RAG (Phase 1).

All document/chunk state lives in Postgres so the app layer is stateless and any
number of workers share the same active document. Retrieval is a single SQL query
that fuses a pgvector semantic arm and a tsvector keyword arm with Reciprocal Rank
Fusion; the app layer only reranks (optional) and generates.
"""

import atexit
import logging
import os
import threading

import numpy as np
import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql:///traffic_rag_dev")

_pool = None
_pool_lock = threading.Lock()


def _configure(conn):
    # The extension is created once in get_pool() before any pool connection is
    # opened, so register_vector normally just works. The retry is a safety net.
    try:
        register_vector(conn)
    except Exception:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.commit()
        register_vector(conn)


def get_pool():
    """Lazily create the shared connection pool (safe across threads/workers)."""
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            # Ensure the vector extension exists up front, on a single dedicated
            # connection, so concurrent pool connections don't race on CREATE
            # EXTENSION (IF NOT EXISTS is not safe under concurrent DDL).
            with psycopg.connect(DATABASE_URL) as boot:
                boot.execute("CREATE EXTENSION IF NOT EXISTS vector")
                boot.commit()
            pool = ConnectionPool(
                conninfo=DATABASE_URL,
                min_size=1,
                max_size=int(os.environ.get("DB_POOL_MAX", "8")),
                configure=_configure,
                kwargs={"row_factory": dict_row},
                open=True,
            )
            atexit.register(pool.close)
            _pool = pool
    return _pool


def ensure_schema(embedding_dim):
    """Create the extension, tables, and indexes if absent (idempotent).

    The embedding column dimension is set from embedding_dim so the schema matches
    whatever embedding model is configured.
    """
    dim = int(embedding_dim)
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                name         TEXT        NOT NULL,
                content_hash TEXT        NOT NULL UNIQUE,
                status       TEXT        NOT NULL DEFAULT 'ingesting'
                             CHECK (status IN ('ingesting', 'ready', 'failed')),
                is_active    BOOLEAN     NOT NULL DEFAULT FALSE,
                chunk_count  INTEGER     NOT NULL DEFAULT 0,
                error        TEXT,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS one_active_document "
            "ON documents ((is_active)) WHERE is_active"
        )
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS chunks (
                id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                document_id BIGINT NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
                chunk_index INTEGER NOT NULL,
                content     TEXT    NOT NULL,
                metadata    JSONB   NOT NULL DEFAULT '{{}}',
                embedding   vector({dim}),
                tsv         tsvector GENERATED ALWAYS AS
                            (to_tsvector('english', content)) STORED,
                UNIQUE (document_id, chunk_index)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON chunks USING GIN (tsv)")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS chunks_hnsw_idx "
            "ON chunks USING hnsw (embedding vector_cosine_ops)"
        )
        cur.execute("CREATE INDEX IF NOT EXISTS chunks_document_idx ON chunks (document_id)")
    logger.info("Schema ensured (embedding dim=%d).", dim)


def get_document_by_hash(content_hash):
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM documents WHERE content_hash = %s", (content_hash,))
        return cur.fetchone()


def begin_document(name, content_hash):
    """Create (or reset) a document row in 'ingesting' state; return its id.

    If the same content already exists, its chunks are cleared so ingestion is
    idempotent and safe to retry.
    """
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO documents (name, content_hash, status)
            VALUES (%s, %s, 'ingesting')
            ON CONFLICT (content_hash)
            DO UPDATE SET status = 'ingesting', name = EXCLUDED.name,
                          error = NULL, updated_at = now()
            RETURNING id
            """,
            (name, content_hash),
        )
        doc_id = cur.fetchone()["id"]
        cur.execute("DELETE FROM chunks WHERE document_id = %s", (doc_id,))
        return doc_id


def insert_chunks(document_id, chunks):
    """Bulk-insert chunks. Each item: (chunk_index, content, metadata, embedding)."""
    rows = [
        (document_id, idx, content, Json(metadata), np.asarray(embedding, dtype="float32"))
        for idx, content, metadata, embedding in chunks
    ]
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO chunks (document_id, chunk_index, content, metadata, embedding) "
            "VALUES (%s, %s, %s, %s, %s)",
            rows,
        )


def finalize_document(document_id, chunk_count):
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE documents SET status = 'ready', chunk_count = %s, updated_at = now() "
            "WHERE id = %s",
            (chunk_count, document_id),
        )


def fail_document(document_id, error):
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE documents SET status = 'failed', error = %s, updated_at = now() "
            "WHERE id = %s",
            (str(error)[:1000], document_id),
        )


def set_active_document(document_id):
    """Make document_id the single active document (atomic swap)."""
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute("UPDATE documents SET is_active = FALSE WHERE is_active")
        cur.execute("UPDATE documents SET is_active = TRUE WHERE id = %s", (document_id,))


def get_active_document():
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM documents WHERE is_active AND status = 'ready'")
        return cur.fetchone()


def get_document_status(content_hash):
    row = get_document_by_hash(content_hash)
    return row["status"] if row else None


def list_documents():
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, status, is_active, chunk_count, updated_at "
            "FROM documents ORDER BY updated_at DESC"
        )
        return cur.fetchall()


def hybrid_search(document_id, query_text, query_embedding, arm_k, rrf_k, candidates):
    """Fuse a semantic (pgvector) and keyword (tsvector) arm with RRF, in SQL.

    Returns up to `candidates` rows [{id, content, metadata, score}] ranked by the
    fused score, restricted to document_id.
    """
    qvec = np.asarray(query_embedding, dtype="float32")
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH semantic AS (
                SELECT id, content, metadata,
                       ROW_NUMBER() OVER (ORDER BY embedding <=> %(qvec)s) AS rank
                FROM chunks
                WHERE document_id = %(doc_id)s
                ORDER BY embedding <=> %(qvec)s
                LIMIT %(arm_k)s
            ),
            keyword AS (
                SELECT c.id, c.content, c.metadata,
                       ROW_NUMBER() OVER (ORDER BY ts_rank_cd(c.tsv, q) DESC) AS rank
                FROM chunks c, websearch_to_tsquery('english', %(qtext)s) q
                WHERE c.document_id = %(doc_id)s AND c.tsv @@ q
                ORDER BY ts_rank_cd(c.tsv, q) DESC
                LIMIT %(arm_k)s
            ),
            fused AS (
                SELECT id, content, metadata, SUM(1.0 / (%(rrf_k)s + rank)) AS score
                FROM (
                    SELECT id, content, metadata, rank FROM semantic
                    UNION ALL
                    SELECT id, content, metadata, rank FROM keyword
                ) arms
                GROUP BY id, content, metadata
            )
            SELECT id, content, metadata, score
            FROM fused
            ORDER BY score DESC
            LIMIT %(candidates)s
            """,
            {
                "qvec": qvec,
                "qtext": query_text,
                "doc_id": document_id,
                "arm_k": arm_k,
                "rrf_k": rrf_k,
                "candidates": candidates,
            },
        )
        return cur.fetchall()


def healthcheck():
    """Return True if the database is reachable."""
    try:
        with get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            return cur.fetchone() is not None
    except Exception:
        logger.exception("Database healthcheck failed")
        return False
