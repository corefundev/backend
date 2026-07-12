-- LEG-3 #431: история версий юр. документов. Каждое сохранение снапшотит
-- НОВОЕ состояние; аудит согласий ссылается на номера версий — текст
-- принятой версии обязан быть восстановим (доказуемость акцепта).
CREATE TABLE IF NOT EXISTS legal_document_revisions (
    id          BIGSERIAL PRIMARY KEY,
    doc_id      TEXT NOT NULL,
    version     INTEGER NOT NULL,
    title       TEXT NOT NULL,
    content     TEXT NOT NULL,
    requires_reconsent BOOLEAN NOT NULL DEFAULT FALSE,
    change_summary TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by  TEXT,
    UNIQUE (doc_id, version)
);
CREATE INDEX IF NOT EXISTS idx_legal_revisions_doc
    ON legal_document_revisions (doc_id, version DESC);

-- Бэкфилл: текущее состояние каждого документа = его текущая версия.
-- Тексты УЖЕ перезаписанных версий невосстановимы (снапшоты начинаются
-- с этой миграции) — честное ограничение, задокументировано в #431.
INSERT INTO legal_document_revisions
    (doc_id, version, title, content, change_summary, created_at, created_by)
SELECT doc_id, version, title, content, change_summary, updated_at, updated_by
FROM legal_documents
ON CONFLICT (doc_id, version) DO NOTHING;
