"""
Regression test for Round-4 Phase 11 Tier 6c followup (2026-05-17).

Operational hygiene — cd_deploy.sh must prune old/unused docker images
after a successful deploy to prevent disk-fill from CD layer churn.

The HostDiskFillingUp alert at 83% on api.testcore.ru on 2026-05-17
was triggered by 89 GB of reclaimable image layers (Trivy build
images + per-deploy GHCR pulls + abandoned R4-* tags). Recovering
6.3 GB via manual `docker image prune` brought the host back to 49%.

This test pins the safety invariants: prune happens AFTER the
image-identity assertions (rollback path mustn't be sabotaged) and
uses `until=72h` so multiple recent deploys remain pull-able for
emergency rollback.
"""
from __future__ import annotations

from pathlib import Path


_BACKEND = Path(__file__).resolve().parents[2]


def test_cd_deploy_prunes_old_images_after_deploy_success():
    """cd_deploy.sh must invoke `docker image prune` with `until=72h`
    filter after the image-identity assertions pass."""
    text = (_BACKEND / "scripts" / "cd_deploy.sh").read_text()
    assert "docker image prune" in text, (
        "cd_deploy.sh must include `docker image prune` to manage disk"
    )
    assert 'until=72h' in text, (
        "prune must use `until=72h` to keep recent images for rollback"
    )
    # `-af` for unused + force (no interactive confirm).
    assert "prune -af" in text, "prune must be `-af` (all-unused + force)"


def test_prune_runs_after_image_identity_assertions():
    """The prune step must come AFTER the worker image-identity
    assertion + rollback path (lines that bail with `exit 4` /
    `exit 3`). Pruning before rollback would destroy PREV_API_IMAGE
    that rollback() depends on."""
    text = (_BACKEND / "scripts" / "cd_deploy.sh").read_text()
    prune_idx = text.find("docker image prune")
    worker_assert_idx = text.find("worker image mismatch")
    assert prune_idx > 0 and worker_assert_idx > 0
    assert prune_idx > worker_assert_idx, (
        "prune must run AFTER image-identity assertions — pruning "
        "before would sabotage rollback (R4-20 followup)"
    )


def test_prune_failure_does_not_break_deploy():
    """A transient docker daemon issue during prune must not
    fail-mark the whole deploy after the app is already live. Look
    for the `|| true` fault-tolerant tail."""
    text = (_BACKEND / "scripts" / "cd_deploy.sh").read_text()
    # Find the prune block.
    prune_idx = text.find("docker image prune")
    end_idx = text.find("\n\n", prune_idx)
    block = text[prune_idx:end_idx] if end_idx > prune_idx else text[prune_idx:]
    assert "|| true" in block, (
        "prune must tolerate failure (`|| true`) so a transient docker "
        "issue doesn't fail-mark an otherwise-successful deploy"
    )
