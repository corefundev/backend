"""
tests/unit/test_cd_guard.py

R11-#79 — scripts/cd_guard_not_older.sh: the CD preflight that refuses to
deploy a commit OLDER than what is live on the host. Root cause it guards:
the 2026-06-06 staging WAL-archive outage, where a 27-day-old CD run rsynced
an obsolete config tree (no postgres archive_mode) onto the host.

We exercise the real script as a subprocess (behaviour, not text-matching).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[2] / "scripts" / "cd_guard_not_older.sh"


def _run(this_ts: str, marker: str):
    return subprocess.run(
        ["bash", str(GUARD), this_ts, marker],
        capture_output=True, text=True,
    )


def test_script_exists_and_executable():
    assert GUARD.exists(), GUARD
    # invocable via bash regardless of the exec bit, but we set it on commit
    assert shutil.which("bash")


def test_newer_commit_allowed():
    r = _run("2000", "abc123 1000")          # this newer than live
    assert r.returncode == 0, r.stderr
    assert "deploy may proceed" in r.stdout


def test_equal_age_allowed():
    # redeploying the exact same commit (e.g. a legit retry) is harmless
    r = _run("1500", "deadbeef 1500")
    assert r.returncode == 0, r.stderr


def test_older_commit_refused():
    r = _run("900", "abc123 1000")           # this OLDER than live → block
    assert r.returncode == 1
    assert "REFUSING" in r.stderr
    assert "older" in r.stderr.lower()


def test_empty_marker_bootstrap_allows():
    # fresh host, no marker yet — must never wedge the first deploy
    r = _run("1000", "")
    assert r.returncode == 0, r.stderr
    assert "bootstrap" in r.stdout.lower()


@pytest.mark.parametrize("marker", ["garbage-no-epoch", "onlysha", "sha notanumber"])
def test_malformed_marker_bootstrap_allows(marker):
    r = _run("1000", marker)
    assert r.returncode == 0, f"marker={marker!r} stderr={r.stderr}"
    assert "bootstrap" in r.stdout.lower()


@pytest.mark.parametrize("bad_ts", ["", "notanum", "12x3", "-5"])
def test_invalid_this_ts_is_usage_error(bad_ts):
    r = _run(bad_ts, "abc123 1000")
    assert r.returncode == 2, f"this_ts={bad_ts!r} rc={r.returncode}"


def test_marker_sha_surfaced_in_refusal():
    r = _run("900", "cafe1234beef 1000")
    assert r.returncode == 1
    assert "cafe1234beef" in r.stderr        # operator sees what's live
