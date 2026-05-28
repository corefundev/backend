"""
tests/lint/test_alert_test_coverage.py

R10 G1 coverage gate — every alert rule in docker/prometheus/alerts.yml
+ alerts.production.yml MUST have at least one behavioural test case in
tests/prometheus/alerts_acceptance_test.yml.

Why this gate is the structural piece (2026-05-28): the promtool
harness alone doesn't prevent recurrence of the phantom-metric /
absent()-storm class — the NEXT person can add an alert without a
test and re-open the blind zone. This gate makes behavioural coverage
MANDATORY: adding an alert to alerts*.yml without a matching
`alertname:` in the promtool test file fails CI.

No baseline / no exceptions: the 30 pre-existing untested alerts were
backfilled in the same PR that introduced this gate. The target state
is permanent 100% coverage — if you're adding an alert, add its test
in the same PR.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

_BACKEND = Path(__file__).resolve().parents[2]
_RULE_FILES = (
    _BACKEND / "docker" / "prometheus" / "alerts.yml",
    _BACKEND / "docker" / "prometheus" / "alerts.production.yml",
)
_TEST_FILE = _BACKEND / "tests" / "prometheus" / "alerts_acceptance_test.yml"


def _all_alert_names() -> set[str]:
    names: set[str] = set()
    for rf in _RULE_FILES:
        data = yaml.safe_load(rf.read_text(encoding="utf-8"))
        for group in data.get("groups", []):
            for rule in group.get("rules", []):
                if "alert" in rule:
                    names.add(rule["alert"])
    return names


def _tested_alert_names() -> set[str]:
    """Names that appear as an `alertname:` assertion in the promtool
    test file. We parse the raw text rather than the YAML because the
    test file may legitimately have multiple `alert_rule_test` blocks
    and we only care which alert names are referenced."""
    text = _TEST_FILE.read_text(encoding="utf-8")
    return set(re.findall(r"alertname:\s*([A-Za-z_][A-Za-z0-9_]*)", text))


def test_every_alert_has_a_behavioural_test():
    """Hard gate: 100% of alerts must have ≥1 promtool test case."""
    all_names = _all_alert_names()
    tested = _tested_alert_names()
    missing = sorted(all_names - tested)
    assert not missing, (
        f"{len(missing)} alert(s) have NO behavioural test in "
        f"tests/prometheus/alerts_acceptance_test.yml:\n"
        + "\n".join(f"  - {n}" for n in missing)
        + "\n\nEvery alert must have at least one `promtool test rules` "
        "case (fires-when-X, and ideally no-fire-when-not-X). This gate "
        "is the structural fix for the phantom-metric / absent()-storm "
        "class — see test_alert_test_coverage.py docstring."
    )


def test_no_test_references_a_nonexistent_alert():
    """Reverse guard: the test file must not reference an alertname
    that no longer exists in the rule files (catches a rename that
    updated alerts.yml but left a stale test, which would silently
    pass as a no-op)."""
    all_names = _all_alert_names()
    tested = _tested_alert_names()
    orphan = sorted(tested - all_names)
    assert not orphan, (
        f"promtool test file references alert(s) that don't exist in "
        f"any rule file (renamed/removed?): {orphan}. Update or remove "
        f"the stale test case."
    )
