"""
tests/unit/test_alerts_split.py

Regression guard for the 2026-05-27 split of `docker/prometheus/alerts.yml`
into a shared file (all-clusters) plus a `alerts.production.yml` for rules
that reference prod-only scrape targets.

Why these guarantees matter
---------------------------

Before the split, three warning alerts (`PgReplicaLagHigh`,
`BackupMirrorStale`, `BaseBackupMirrorStale`) chronically fired on staging
because their `or absent(metric)` defence branched into "alert" any time the
underlying scrape target was absent (which is the by-design state for
postgres-replica + ssl-pg-tls + off-region backup mirror on staging — staging
is non-customer-facing, no DR posture).

The fix is per-env rule-file mounting: prod compose bind-mounts the whole
`prometheus/` directory so both files are loaded; staging's
`!override volumes:` block lists ONLY the shared `alerts.yml` file, so the
prod-only file is automatically excluded even though it lives in the same
directory on disk.

A wrong move that would regress this:
    • someone re-adds a replica/backup-mirror rule into `alerts.yml`
      → staging starts firing again
    • someone removes the prod-only `rule_files:` entry from `prometheus.yml`
      → prod silently loses those alerts
    • someone changes staging compose's `!override` to mount the directory
      → staging picks up the prod-only file, alerts fire chronically

Each of those is caught by one of the tests below.
"""
from __future__ import annotations

from pathlib import Path

import yaml

_BACKEND = Path(__file__).resolve().parents[2]
_PROM_DIR = _BACKEND / "docker" / "prometheus"
_SHARED = _PROM_DIR / "alerts.yml"
_PROD_ONLY = _PROM_DIR / "alerts.production.yml"

# These rule names MUST live in alerts.production.yml ONLY. They reference
# scrape jobs / metrics that exist only on the prod cluster by design.
PROD_ONLY_RULES = frozenset({
    # postgres-replica streaming metrics (`job="postgres-replica"`).
    "PgReplicaLagHigh",
    "PgReplicaWalReceiverDown",
    "PgReplicaScrapeFailed",
    # TLS cert expiry for the primary→replica WAL stream (`job="ssl-pg-tls"`).
    "PgReplicationCertExpiringSoon",
    "PgReplicationCertExpired",
    # Off-region backup mirror staleness (Selectel `uz-2`, R7-9). Mirror is
    # prod-only — staging has no off-region copy by design.
    "BackupMirrorStale",
    "BaseBackupMirrorStale",
})


def _collect_alert_names(path: Path) -> set[str]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for group in data.get("groups", []):
        for rule in group.get("rules", []):
            if "alert" in rule:
                names.add(rule["alert"])
    return names


def test_shared_alerts_yml_has_no_prod_only_rules():
    """alerts.yml is loaded on BOTH prod and staging. None of the 7
    prod-only rules may appear here — if they do, staging will start
    firing chronic false-positive warnings again."""
    shared_names = _collect_alert_names(_SHARED)
    leaked = sorted(PROD_ONLY_RULES & shared_names)
    assert not leaked, (
        f"Prod-only alert rules leaked back into the shared alerts.yml: "
        f"{leaked}. These rules reference scrape jobs / metrics that are "
        f"absent on staging by design (postgres-replica, ssl-pg-tls, "
        f"off-region backup mirror). Move them to alerts.production.yml."
    )


def test_production_alerts_yml_has_exactly_the_prod_only_rules():
    """alerts.production.yml exists to hold these 7 rules and ONLY these
    7 rules. Adding unrelated rules here would mean staging silently
    loses coverage on rules that should run everywhere."""
    prod_names = _collect_alert_names(_PROD_ONLY)
    assert prod_names == PROD_ONLY_RULES, (
        f"alerts.production.yml contents drifted.\n"
        f"  expected (exactly): {sorted(PROD_ONLY_RULES)}\n"
        f"  actual:             {sorted(prod_names)}\n"
        f"  unexpected extras:  {sorted(prod_names - PROD_ONLY_RULES)}\n"
        f"  missing:            {sorted(PROD_ONLY_RULES - prod_names)}"
    )


def test_prod_prometheus_yml_loads_both_rule_files():
    """The prod prometheus config must `rule_files:` BOTH files —
    otherwise prod itself silently loses the replica/cert/mirror
    alerts after the split."""
    cfg = yaml.safe_load((_PROM_DIR / "prometheus.yml").read_text(encoding="utf-8"))
    rule_files = cfg.get("rule_files", [])
    assert "/etc/prometheus/alerts.yml" in rule_files, rule_files
    assert "/etc/prometheus/alerts.production.yml" in rule_files, (
        f"prod prometheus.yml is missing alerts.production.yml from "
        f"rule_files — prod will silently drop the 7 prod-only rules. "
        f"Current rule_files: {rule_files}"
    )


def test_staging_prometheus_yml_loads_only_the_shared_file():
    """The staging prometheus config must NOT list the prod-only rule
    file — even if the directory bind-mount started exposing it. The
    `rule_files:` declaration is the second line of defence (the first
    being the compose `!override volumes:` block)."""
    staging_cfg_path = _BACKEND / "docker" / "prometheus.staging.yml"
    cfg = yaml.safe_load(staging_cfg_path.read_text(encoding="utf-8"))
    rule_files = cfg.get("rule_files", [])
    assert "/etc/prometheus/alerts.yml" in rule_files, rule_files
    assert "/etc/prometheus/alerts.production.yml" not in rule_files, (
        f"staging prometheus.staging.yml lists alerts.production.yml in "
        f"rule_files — that's wrong; staging would re-load the 3 chronic "
        f"warning alerts that the 2026-05-27 split removed. "
        f"Current rule_files: {rule_files}"
    )


def test_staging_compose_override_lists_only_the_shared_file():
    """The compose-side guard: `docker-compose.staging.yml`'s
    `volumes: !override` for prometheus must mount the shared alerts.yml
    as a single FILE (not the parent directory), so alerts.production.yml
    is not even present inside the staging container.

    Reading the compose with PyYAML loses the `!override` tag (we'd need
    a custom loader to handle docker-compose's tag extensions), so we
    do a text-level check that's resilient to formatting changes.
    """
    text = (_BACKEND / "docker" / "docker-compose.staging.yml").read_text(encoding="utf-8")
    assert "./prometheus/alerts.yml:/etc/prometheus/alerts.yml:ro" in text, (
        "staging compose doesn't mount the shared alerts.yml as a single "
        "file — re-add the `!override` mount or staging will lose alerts."
    )
    # If anyone changes staging to bind-mount the directory, the prod-only
    # rule file would become visible inside the container.
    forbidden = "./prometheus:/etc/prometheus:ro"
    in_staging_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("prometheus:"):
            in_staging_block = True
            continue
        if in_staging_block:
            # Detect block exit by encountering another top-level service
            # (2-space indent followed by name + colon, not a list/key).
            if line.startswith("  ") and not line.startswith("   ") \
                    and stripped.endswith(":") and not stripped.startswith("#") \
                    and not stripped.startswith("-"):
                in_staging_block = False
                continue
            assert forbidden not in line, (
                "staging compose mounts the prometheus/ DIRECTORY — this "
                "would expose alerts.production.yml inside the staging "
                "container. Mount alerts.yml as a single file instead."
            )


def test_no_rule_was_dropped_silently_in_the_split():
    """Cross-file totals must equal the pre-split count + any net add.
    This is the "did we accidentally delete a rule that wasn't supposed
    to move?" guard.

    If someone genuinely adds or removes a rule, they must update the
    expected count here in the same PR — that surfaces a deliberate
    decision in code review."""
    shared = _collect_alert_names(_SHARED)
    prod_only = _collect_alert_names(_PROD_ONLY)

    # Pre-split alerts.yml had 26 + 7 prod-only-now-extracted = 33 rules.
    # After the split: 26 in shared + 7 in prod-only = 33. Updating this
    # constant requires conscious justification.
    EXPECTED_TOTAL = 33

    total = len(shared) + len(prod_only)
    assert total == EXPECTED_TOTAL, (
        f"Total alert-rule count changed: {total} (was {EXPECTED_TOTAL}).\n"
        f"  shared (alerts.yml):           {len(shared)} rules\n"
        f"  prod-only (alerts.production): {len(prod_only)} rules\n"
        f"If this change is intentional, update EXPECTED_TOTAL here AND "
        f"document the new rule (or deletion) in docs/audit/."
    )

    # Overlap MUST be empty — a rule named in both files would mean
    # prod evaluates it twice and staging only once, leading to weird
    # double-firing on prod alertmanager.
    overlap = shared & prod_only
    assert not overlap, f"rule names appear in BOTH files: {sorted(overlap)}"
