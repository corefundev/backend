"""
Regression tests for R5-16 — pushgateway persistence (2026-05-17).

Pushgateway state is in-memory by default. Every container recreate
(Phase 2 full restart, manual `compose up -d pushgateway`, OOM)
wiped all pushed series; metrics stayed absent until each producer's
next cron interval. Observed twice in two hours on 2026-05-17 —
BackupStale + BaseBackupStale both fired during the gap and had to
be manually re-pushed to resolve.

Fix: add `--persistence.file=/data/pushgateway.dump` + named volume
mount. Pushgateway writes state to disk and reads on startup, so
container recreate is transparent to producers and scrapers.

Source-level pin.
"""
from __future__ import annotations

from pathlib import Path


_BACKEND = Path(__file__).resolve().parents[2]


def _pushgateway_block() -> str:
    """Return the pushgateway compose block."""
    text = (_BACKEND / "docker" / "docker-compose.yml").read_text()
    idx = text.find("  pushgateway:")
    assert idx > 0, "pushgateway service must exist"
    # Block ends at the next 2-space service heading (not a sub-field).
    import re
    next_svc = re.search(r"\n  [a-z][a-z0-9_-]*:\n", text[idx + len("  pushgateway:"):])
    end = idx + len("  pushgateway:") + (next_svc.start() if next_svc else len(text))
    return text[idx:end]


def test_pushgateway_has_persistence_flag():
    """Compose must pass `--persistence.file=/data/pushgateway.dump`
    so state survives recreate."""
    block = _pushgateway_block()
    assert "--persistence.file=/data/pushgateway.dump" in block, (
        "pushgateway must pass --persistence.file=/data/pushgateway.dump "
        "(R5-16 — survive container recreate)"
    )


def test_pushgateway_persistence_interval_set():
    """`--persistence.interval=Nm` flag must be set — without it
    pushgateway only writes on shutdown, so a SIGKILL loses recent
    pushes."""
    block = _pushgateway_block()
    assert "--persistence.interval=" in block, (
        "pushgateway must set --persistence.interval so state writes "
        "periodically (not just on shutdown) — R5-16"
    )


def test_pushgateway_mounts_persistent_volume():
    """The `/data` directory inside the container must be backed by a
    named volume (not a bind mount to host nor ephemeral tmpfs)."""
    block = _pushgateway_block()
    assert "pushgateway_data:/data" in block, (
        "pushgateway must mount the named volume pushgateway_data at "
        "/data (R5-16)"
    )


def test_pushgateway_data_volume_declared():
    """The named volume `pushgateway_data` must be declared in the
    top-level `volumes:` block; otherwise compose refuses to start."""
    text = (_BACKEND / "docker" / "docker-compose.yml").read_text()
    # Find the top-level volumes: block — must be at column 0.
    import re
    vol_match = re.search(r"^volumes:\n((?:  \w[\w_-]*:\n?.*\n?)+)",
                          text, re.M)
    assert vol_match, "top-level volumes: block must exist"
    vol_block = vol_match.group(0)
    assert "pushgateway_data:" in vol_block, (
        "pushgateway_data named volume must be declared (R5-16)"
    )
