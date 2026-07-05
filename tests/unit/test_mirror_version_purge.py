"""
Selectel mirror version-purge (2026-07-05).

The mirror bucket keeps VERSIONING ON (rollback window vs malicious
delete), and Selectel cannot expire noncurrent versions server-side (it
does not persist lifecycle policies). Without a client-side version purge
every pruned object lives on as a billed noncurrent version — found as
10 GiB / 765 versions vs 1.35 GiB visible. The purge removes ALL versions
older than retention + grace per prefix; the grace (default 7d) IS the
recovery window for a deleted object.

Static pins only (the script runs against live S3; behaviour verified on
prod per the closure workflow). Assertions target argument substrings, not
full command lines — mc invocations wrap across lines (G4d lesson).
"""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path("scripts/mirror_prune.sh").read_text()


def test_version_purge_covers_all_three_prefixes():
    for prefix in ("backups/", "base/", "wal/"):
        assert re.search(rf'purge_versions "{re.escape(prefix)}"', SRC), (
            f"{prefix} versions must be purged — Selectel cannot expire them server-side"
        )


def test_purge_uses_versions_flag_and_grace_arithmetic():
    assert "--versions --older-than" in SRC.replace("\\\n", " ").replace("  ", " ") or \
           "--versions" in SRC and "--older-than" in SRC
    for var in ("BACKUPS_EXPIRE_DAYS", "BASE_EXPIRE_DAYS", "WAL_EXPIRE_DAYS"):
        assert re.search(rf"\$\(\({var} \+ VERSION_GRACE_DAYS\)\)", SRC), (
            f"purge horizon must be {var} + grace — purging AT retention would "
            "kill the rollback window; purging without arithmetic drifts from retention"
        )


def test_grace_knob_with_sane_default():
    assert 'VERSION_GRACE_DAYS="${MIRROR_VERSION_GRACE_DAYS:-7}"' in SRC


def test_object_prune_still_runs_before_version_purge():
    # The version purge complements — never replaces — the current-object
    # prune (which controls the VISIBLE retention window).
    i_prune = SRC.index('prune_prefix "wal/"')
    i_purge = SRC.index('purge_versions "wal/"')
    assert i_prune < i_purge
