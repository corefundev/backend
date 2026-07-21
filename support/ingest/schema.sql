-- SUP-1 (#504): хранилище чанков корпуса БЗ.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS kb_chunks (
    id          BIGSERIAL PRIMARY KEY,
    doc_id      TEXT NOT NULL,           -- напр. help:metodika-ocenki-tochnosti
    source      TEXT NOT NULL,           -- help | news | kb-file | plans
    title       TEXT NOT NULL,
    url_path    TEXT,                    -- /help/a/... для цитат
    chunk_no    INT  NOT NULL,
    content     TEXT NOT NULL,
    content_sha CHAR(64) NOT NULL,       -- дедуп/инкрементальность
    embedding   vector(384) NOT NULL,    -- multilingual-e5-small
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (doc_id, chunk_no)
);

-- cosine ANN; корпус мал (сотни чанков) — lists=32 достаточно
CREATE INDEX IF NOT EXISTS kb_chunks_embedding_idx
    ON kb_chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 32);

CREATE TABLE IF NOT EXISTS kb_ingest_runs (
    id          BIGSERIAL PRIMARY KEY,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    docs        INT,
    chunks      INT,
    note        TEXT
);
