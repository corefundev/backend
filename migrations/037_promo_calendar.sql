-- #570 PC-1: «Календарь акций» — справочник known-future промо (вариант B).
-- Календарь = отдельная сущность per-dataset (НЕ колонка снапшота: снапшот
-- обрывается на date_max истории продаж и будущих дат не содержит).
-- Повторная загрузка ЗАМЕЩАЕТ календарь целиком (atomic swap в реестре).

CREATE TABLE IF NOT EXISTS sku_promo_calendars (
    calendar_id     TEXT PRIMARY KEY,
    client_id       TEXT NOT NULL,
    dataset_id      TEXT NOT NULL,
    filename        TEXT NOT NULL,
    -- pending_review: загружен и провалидирован, ждёт «Применить»
    -- active: единственный действующий календарь датасета
    -- replaced: замещён более поздним apply
    -- discarded: кандидат отброшен (новая загрузка до apply)
    status          TEXT NOT NULL DEFAULT 'pending_review'
                    CHECK (status IN ('pending_review','active','replaced','discarded')),
    report          JSONB NOT NULL DEFAULT '{}'::jsonb,
    rows_accepted   INTEGER NOT NULL DEFAULT 0,
    date_min        DATE,
    date_max        DATE,
    source_key      TEXT,                -- сырой файл в untrusted-зоне (аудит)
    uploaded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_at      TIMESTAMPTZ,
    replaced_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_promo_calendars_dataset
    ON sku_promo_calendars (dataset_id, status);

-- Частичный уникальный индекс: у датасета не более ОДНОГО активного
-- календаря — инвариант atomic swap на уровне БД, не только кода.
CREATE UNIQUE INDEX IF NOT EXISTS uq_promo_calendars_one_active
    ON sku_promo_calendars (dataset_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS sku_promo_events (
    id              BIGSERIAL PRIMARY KEY,
    calendar_id     TEXT NOT NULL REFERENCES sku_promo_calendars(calendar_id)
                    ON DELETE CASCADE,
    client_id       TEXT NOT NULL,
    dataset_id      TEXT NOT NULL,
    sku             TEXT,                -- ровно одно из sku|category (CHECK)
    category        TEXT,
    date_from       DATE NOT NULL,
    date_to         DATE NOT NULL,
    depth_pct       REAL,                -- v1 моделью не используется; формат стабилен
    name            TEXT,
    CHECK (date_from <= date_to),
    CHECK ((sku IS NULL) <> (category IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_promo_events_calendar
    ON sku_promo_events (calendar_id);
CREATE INDEX IF NOT EXISTS idx_promo_events_dataset_dates
    ON sku_promo_events (dataset_id, date_from, date_to);

-- Тип загрузки: продажи или файл календаря акций — конвейер (AV, воркеры,
-- FSM) единый, ветвится только обработка после скана.
ALTER TABLE sku_uploads ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'sales';
