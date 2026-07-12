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

# LEG-3 #431: документы, участвующие в контуре повторного согласия
RECONSENT_DOC_IDS = ("terms", "privacy", "consent", "pdn")


@dataclass
class LegalDocument:
    doc_id:     str
    title:      str
    content:    str
    version:    int
    updated_at: datetime
    updated_by: Optional[str]
    # LEG-3 #431: версия, начиная с которой требуется пере-согласие
    # (None = никогда не требовалось); краткое «что изменилось»
    reconsent_required_since: Optional[int] = None
    change_summary: Optional[str] = None


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
                "SELECT doc_id, title, content, version, updated_at, updated_by, "
                "       reconsent_required_since, change_summary "
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
        requires_reconsent: bool = False,
        change_summary: Optional[str] = None,
    ) -> LegalDocument:
        """Insert or update. Bumps version on every save.

        LEG-3 #431: requires_reconsent=True помечает НОВУЮ версию как
        требующую пере-согласия (reconsent_required_since = новая
        версия). Редакционные сохранения (False) сигнал НЕ затирают —
        колонка остаётся прежней. change_summary обновляется всегда.
        """
        with self._conn() as conn, conn.cursor(cursor_factory=self._extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO legal_documents
                    (doc_id, title, content, version, updated_at, updated_by,
                     reconsent_required_since, change_summary)
                VALUES (%s, %s, %s, 1,  NOW(), %s,
                        CASE WHEN %s THEN 1 ELSE NULL END, %s)
                ON CONFLICT (doc_id) DO UPDATE
                  SET title       = EXCLUDED.title,
                      content     = EXCLUDED.content,
                      version     = legal_documents.version + 1,
                      updated_at  = NOW(),
                      updated_by  = EXCLUDED.updated_by,
                      reconsent_required_since = CASE WHEN %s
                          THEN legal_documents.version + 1
                          ELSE legal_documents.reconsent_required_since END,
                      change_summary = EXCLUDED.change_summary
                RETURNING doc_id, title, content, version, updated_at, updated_by,
                          reconsent_required_since, change_summary
                """,
                (doc_id, title, content, updated_by,
                 requires_reconsent, change_summary, requires_reconsent),
            )
            row = cur.fetchone()
            # LEG-3 #431: снапшот НОВОГО состояния в той же транзакции —
            # текст каждой версии восстановим (доказуемость акцепта)
            cur.execute(
                """
                INSERT INTO legal_document_revisions
                    (doc_id, version, title, content,
                     requires_reconsent, change_summary, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (doc_id, version) DO NOTHING
                """,
                (doc_id, row["version"], title, content,
                 requires_reconsent, change_summary, updated_by),
            )
            return LegalDocument(**row)

    def list_revisions(self, doc_id: str, limit: int = 100) -> list[dict]:
        with self._conn() as conn, conn.cursor(
                cursor_factory=self._extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT version, title, requires_reconsent, change_summary,
                       created_at, created_by
                FROM legal_document_revisions
                WHERE doc_id = %s ORDER BY version DESC LIMIT %s
                """,
                (doc_id, limit),
            )
            out = []
            for r in cur.fetchall():
                d = dict(r)
                d["created_at"] = d["created_at"].isoformat()
                out.append(d)
            return out

    def get_revision(self, doc_id: str, version: int) -> Optional[dict]:
        with self._conn() as conn, conn.cursor(
                cursor_factory=self._extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT version, title, content, requires_reconsent,
                       change_summary, created_at, created_by
                FROM legal_document_revisions
                WHERE doc_id = %s AND version = %s
                """,
                (doc_id, version),
            )
            r = cur.fetchone()
            if not r:
                return None
            d = dict(r)
            d["created_at"] = d["created_at"].isoformat()
            return d


_store: Optional[LegalDocumentStore] = None


def get_legal_store() -> LegalDocumentStore:
    """Lazy singleton — re-uses one connection-builder for the app lifetime."""
    global _store
    if _store is None:
        _store = LegalDocumentStore()
    return _store
