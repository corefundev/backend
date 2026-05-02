# DB Migrations

Versioned SQL migrations applied by `scripts/migrate.py` (run as the
`migrate` compose service before `api`/`worker` start).

## Rules

- One migration = one numbered SQL file: `NNN_short_description.sql`.
  Numbers are zero-padded 3 digits; ordering is lexicographic.
- Each file is **a single PostgreSQL transaction**. The runner wraps
  the whole file in `BEGIN ... COMMIT`. Never put `COMMIT;` inside a
  file — that breaks rollback semantics.
- Migrations are **applied at most once**: the runner records each
  applied filename in `_db_migrations` and skips ones already present.
- Migrations are **never edited** after they ship to a deployed
  environment. To change schema, write a new migration with a higher
  number.
- DDL must be **forward-only**: down-migrations are not supported. If
  you need to undo something, add a new migration that does the undo.

## Concurrency

The runner takes a session-level `pg_advisory_lock(54129001)` before
applying anything, so two parallel `migrate` containers serialize
naturally — second one sees applied versions, runs nothing.

## Local backfill

Existing prod databases already contain the tables created by the
old in-process DDL. The first migration run records them as applied
so the runner doesn't try to re-create.
