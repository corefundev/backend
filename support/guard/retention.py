"""SUP-5 (#508): retention диалогов (152-ФЗ). Удаляет строки старше
DIALOG_RETENTION_DAYS (дефолт 90). Запуск — суточным cron.

    VECTORDB_PASSWORD=... python3 retention.py
"""
from __future__ import annotations

import os
import sys

import psycopg2

DAYS = int(os.environ.get("DIALOG_RETENTION_DAYS", "90"))


def main() -> int:
    pw = os.environ.get("VECTORDB_PASSWORD")
    if not pw:
        sys.exit("VECTORDB_PASSWORD is required")
    conn = psycopg2.connect(
        f"postgresql://supbot:{pw}@127.0.0.1:5433/supbot")
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM dialogs WHERE ts < now() - interval '%s days'"
                    % DAYS)
        deleted = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    print(f"retention: deleted {deleted} dialogs older than {DAYS}d")
    return 0


if __name__ == "__main__":
    sys.exit(main())
