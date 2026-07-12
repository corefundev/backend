"""LEG-3 #431 (инкремент 1) — reconsent-семантика хранилища (PG, CI).

Свойства: флаг ставит reconsent_required_since = НОВАЯ версия;
редакционное сохранение сигнал не затирает; change_summary обновляется
каждым сохранением.
"""
from __future__ import annotations

import os
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from src.storage.legal import LegalDocumentStore  # noqa: E402

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="integration test needs DATABASE_URL (CI postgres service)",
)


@pytest.fixture()
def store():
    st = LegalDocumentStore(DATABASE_URL)
    doc_id = f"itest-{uuid.uuid4().hex[:8]}"
    yield st, doc_id
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DELETE FROM legal_documents WHERE doc_id = %s", (doc_id,))
    conn.close()


def test_reconsent_since_set_only_by_flag_and_sticky(store):
    st, doc_id = store
    d1 = st.upsert(doc_id, "T", "v1")
    assert d1.version == 1 and d1.reconsent_required_since is None

    d2 = st.upsert(doc_id, "T", "v2", requires_reconsent=True,
                   change_summary="новые цели обработки")
    assert d2.version == 2 and d2.reconsent_required_since == 2
    assert d2.change_summary == "новые цели обработки"

    d3 = st.upsert(doc_id, "T", "v3 editorial")   # без флага
    assert d3.version == 3
    assert d3.reconsent_required_since == 2       # сигнал НЕ затёрт
    assert d3.change_summary is None              # summary обновился (пустой)

    d4 = st.upsert(doc_id, "T", "v4", requires_reconsent=True,
                   change_summary="ещё раз")
    assert d4.reconsent_required_since == 4       # передвинут новым флагом


def test_first_version_with_flag(store):
    st, doc_id = store
    d1 = st.upsert(doc_id, "T", "v1", requires_reconsent=True)
    assert d1.version == 1 and d1.reconsent_required_since == 1
