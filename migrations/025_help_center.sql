-- 025 (HC-1 #333, epic #332): Help Center (база знаний).
-- Тот же CMS-фундамент, что у новостей (src/cms sanitizer, ревизии,
-- record_event-аудит на API-слое). PII-free by design: feedback хранит
-- только hash сессии голосовавшего, search-лог — только строку запроса.

CREATE TABLE IF NOT EXISTS help_categories (
    id          TEXT PRIMARY KEY,               -- uuid4 hex
    slug        TEXT NOT NULL,
    locale      TEXT NOT NULL DEFAULT 'ru',
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    icon        TEXT NOT NULL DEFAULT '',
    sort_order  INTEGER NOT NULL DEFAULT 0,
    parent_id   TEXT REFERENCES help_categories(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (slug, locale)
);
CREATE INDEX IF NOT EXISTS idx_help_categories_sort
    ON help_categories (sort_order);

CREATE TABLE IF NOT EXISTS help_articles (
    id              TEXT PRIMARY KEY,           -- uuid4 hex
    slug            TEXT NOT NULL,
    locale          TEXT NOT NULL DEFAULT 'ru',
    category_id     TEXT NOT NULL REFERENCES help_categories(id) ON DELETE RESTRICT,
    title           TEXT NOT NULL,
    body_md         TEXT NOT NULL,
    body_html       TEXT NOT NULL,              -- sanitized cache (src/cms)
    excerpt         TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'draft',   -- draft/published/archived
    sort_order      INTEGER NOT NULL DEFAULT 0,
    seo_title       TEXT NOT NULL DEFAULT '',
    seo_description TEXT NOT NULL DEFAULT '',
    author_admin_id TEXT NOT NULL,
    view_count      BIGINT NOT NULL DEFAULT 0,  -- агрегат, не per-user (HC-6)
    published_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- HC-5: FTS по русской морфологии (title весомее тела)
    search_tsv      TSVECTOR GENERATED ALWAYS AS (
        setweight(to_tsvector('russian', coalesce(title, '')), 'A') ||
        setweight(to_tsvector('russian', coalesce(body_md, '')), 'B')
    ) STORED,
    UNIQUE (slug, locale)
);
CREATE INDEX IF NOT EXISTS idx_help_articles_category
    ON help_articles (category_id, status, sort_order);
CREATE INDEX IF NOT EXISTS idx_help_articles_status
    ON help_articles (status);
CREATE INDEX IF NOT EXISTS idx_help_articles_tsv
    ON help_articles USING GIN (search_tsv);

CREATE TABLE IF NOT EXISTS help_article_revisions (
    id              BIGSERIAL PRIMARY KEY,
    article_id      TEXT NOT NULL REFERENCES help_articles(id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    body_md         TEXT NOT NULL,
    editor_admin_id TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_help_revisions_article
    ON help_article_revisions (article_id, created_at DESC);

CREATE TABLE IF NOT EXISTS help_article_feedback (
    id          BIGSERIAL PRIMARY KEY,
    article_id  TEXT NOT NULL REFERENCES help_articles(id) ON DELETE CASCADE,
    helpful     BOOLEAN NOT NULL,
    comment     TEXT,                            -- опционально, PII не просим
    voter_hash  TEXT NOT NULL,                   -- hash сессии; IP НЕ храним
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (article_id, voter_hash)              -- один голос на сессию
);

CREATE TABLE IF NOT EXISTS help_search_log (
    id            BIGSERIAL PRIMARY KEY,
    query         TEXT NOT NULL,
    results_count INTEGER NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_help_search_zero
    ON help_search_log (results_count, created_at DESC);
