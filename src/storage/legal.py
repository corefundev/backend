"""
src/storage/legal.py

Storage for legal documents (privacy policy, ToS, etc.) — admin-edited
Markdown content surfaced via public GET and admin PUT API endpoints.

Schema: see migrations/009_legal_documents.sql.

This module follows the same pattern as src/storage/training_runs.py:
a thin psycopg2 wrapper, no ORM, no caching — legal docs are read at
most a few times per minute (signup flow + admin page).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class LegalDocument:
    doc_id:     str
    title:      str
    content:    str
    version:    int
    updated_at: datetime
    updated_by: Optional[str]


class LegalDocumentStore:
    """Postgres-backed legal documents storage."""

    def __init__(self, database_url: Optional[str] = None):
        try:
            import psycopg2
            import psycopg2.extras
            self._psycopg2 = psycopg2
            self._extras   = psycopg2.extras
        except ImportError as e:
            raise ImportError("psycopg2-binary required") from e
        self._url = database_url or os.environ.get("DATABASE_URL", "")
        if not self._url:
            raise RuntimeError("DATABASE_URL not set — cannot init LegalDocumentStore")

    def _conn(self):
        return self._psycopg2.connect(self._url)

    def get(self, doc_id: str) -> Optional[LegalDocument]:
        with self._conn() as conn, conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT doc_id, title, content, version, updated_at, updated_by "
                "FROM legal_documents WHERE doc_id = %s",
                (doc_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return LegalDocument(**row)

    def upsert(
        self,
        doc_id: str,
        title: str,
        content: str,
        updated_by: Optional[str] = None,
    ) -> LegalDocument:
        """Insert or update. Bumps version on every save."""
        with self._conn() as conn, conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO legal_documents (doc_id, title, content, version, updated_at, updated_by)
                VALUES (%s, %s, %s, 1, NOW(), %s)
                ON CONFLICT (doc_id) DO UPDATE
                  SET title       = EXCLUDED.title,
                      content     = EXCLUDED.content,
                      version     = legal_documents.version + 1,
                      updated_at  = NOW(),
                      updated_by  = EXCLUDED.updated_by
                RETURNING doc_id, title, content, version, updated_at, updated_by
                """,
                (doc_id, title, content, updated_by),
            )
            row = cur.fetchone()
            return LegalDocument(**row)


_store: Optional[LegalDocumentStore] = None


def get_legal_store() -> LegalDocumentStore:
    """Lazy singleton — re-uses one connection-builder for the app lifetime."""
    global _store
    if _store is None:
        _store = LegalDocumentStore()
    return _store
