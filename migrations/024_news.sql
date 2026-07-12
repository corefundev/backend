-- 024 (NEWS-1 #341, epic #340): news / product-updates content type.
-- Shares the CMS foundation discipline (src/cms/): sanitized HTML cache,
-- tamper-evident revisions, record_event audit (no manual audit_log rows).
-- Live-gating is a PREDICATE (status/publish_at/expire_at) — no cron.

CREATE TABLE IF NOT EXISTS news_posts (
    id             TEXT PRIMARY KEY,                -- uuid4 hex
    slug           TEXT NOT NULL UNIQUE,
    title          TEXT NOT NULL,
    summary        TEXT NOT NULL DEFAULT '',
    body_md        TEXT NOT NULL,
    body_html      TEXT NOT NULL,                   -- sanitized cache (src/cms)
    category       TEXT NOT NULL,                   -- release/improvement/maintenance/tip/announcement
    status         TEXT NOT NULL DEFAULT 'draft',   -- draft/published/archived
    pinned         BOOLEAN NOT NULL DEFAULT FALSE,
    importance     TEXT NOT NULL DEFAULT 'normal',  -- normal/important
    publish_at     TIMESTAMPTZ,                     -- future = scheduled
    expire_at      TIMESTAMPTZ,
    author_admin_id TEXT NOT NULL,
    published_at   TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- live-gating + сортировка ленты (pinned desc, publish_at desc)
CREATE INDEX IF NOT EXISTS idx_news_posts_live
    ON news_posts (status, publish_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_posts_pinned
    ON news_posts (pinned) WHERE pinned;

CREATE TABLE IF NOT EXISTS news_post_revisions (
    id              BIGSERIAL PRIMARY KEY,
    post_id         TEXT NOT NULL REFERENCES news_posts(id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    body_md         TEXT NOT NULL,
    editor_admin_id TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_news_revisions_post
    ON news_post_revisions (post_id, created_at DESC);

CREATE TABLE IF NOT EXISTS news_reads (
    client_id TEXT NOT NULL,
    post_id   TEXT NOT NULL REFERENCES news_posts(id) ON DELETE CASCADE,
    read_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (client_id, post_id)
);
