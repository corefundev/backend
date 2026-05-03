"""
Unit tests for src.audit.log.

The integration-test suite covers the happy path against a real
Postgres. These tests pin the *contract* that's most easily forgotten:

  1. record_event() never raises, even when DATABASE_URL is unset or
     points at an unreachable host. Audit must never crash a request.
  2. Read helpers return empty / zero on the same failure modes
     instead of bubbling exceptions.
"""
from __future__ import annotations

import logging
import os

from src.audit import (
    record_event,
    list_for_client,
    recent_failed_logins,
    EVT_LOGIN,
)


def test_record_event_is_silent_when_database_url_missing(monkeypatch, caplog):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    caplog.set_level(logging.INFO)
    record_event(
        event_type=EVT_LOGIN, event_subtype="success",
        client_id="acme", actor_email="alice@example.com",
        success=True,
    )
    # Fallback structured log line must be present even without DB.
    assert any("audit_event=login" in r.message for r in caplog.records)


def test_record_event_is_silent_when_database_unreachable(monkeypatch, caplog):
    # Point at a closed local port. psycopg2.connect raises; we eat it.
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    caplog.set_level(logging.ERROR)
    # Must not raise — request paths depend on this.
    record_event(
        event_type=EVT_LOGIN, event_subtype="failure",
        actor_email="bob@example.com",
        success=False,
    )


def test_list_for_client_returns_empty_on_db_failure(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert list_for_client("acme") == []


def test_recent_failed_logins_returns_zero_on_db_failure(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert recent_failed_logins("alice@example.com") == 0
