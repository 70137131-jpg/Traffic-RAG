-- Traffic RAG — Phase 1 schema (Postgres + pgvector).
-- Reference DDL for manual provisioning. The app also applies this idempotently
-- at startup via pg_store.ensure_schema(), which substitutes the embedding
-- dimension from EMBEDDING_DIM (default 384 for all-MiniLM-L6-v2; use 768 for
-- Google text-embedding-004). Keep the :dim below in sync if applying manually.

CREATE EXTENSION IF NOT EXISTS vector;

-- One row per ingested document. State (not process memory) lives here, so any
-- number of stateless app workers share the same active document.
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
);

-- At most one active document at a time (single-tenant).
CREATE UNIQUE INDEX IF NOT EXISTS one_active_document
    ON documents ((is_active)) WHERE is_active;

CREATE TABLE IF NOT EXISTS chunks (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id BIGINT NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    content     TEXT    NOT NULL,
    metadata    JSONB   NOT NULL DEFAULT '{}',
    embedding   vector(384),           -- dimension substituted by ensure_schema()
    -- Keyword-search vector, kept in sync with content automatically.
    tsv         tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    UNIQUE (document_id, chunk_index)
);

-- Keyword arm (BM25-like) and semantic arm indexes.
CREATE INDEX IF NOT EXISTS chunks_tsv_idx  ON chunks USING GIN (tsv);
CREATE INDEX IF NOT EXISTS chunks_hnsw_idx ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_document_idx ON chunks (document_id);
