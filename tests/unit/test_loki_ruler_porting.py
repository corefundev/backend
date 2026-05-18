"""
Regression tests for Loki ruler porting (2026-05-17 final).

R2-8 originally wrote three log-based security alerts in
docker/alerts.yml under Prometheus, using LogQL syntax
(`{logger_name=...} |~ "regex"`). Prometheus rejects this syntax,
so the alerts never parsed and never fired. They were removed in
the R4-20 incident fix (commit 7cf0f9a) with a marker comment
saying they must move to a Loki ruler.

This change finally ports them. Three pieces wired together:

  1. `docker/loki-rules.yml` — three alerts in correct LogQL syntax.
  2. `docker/loki-config.yaml` — `ruler:` block with alertmanager URL
     + evaluation interval.
  3. `docker/docker-compose.yml` — Loki service mounts the rules file
     at `/loki/rules/fake/loki-rules.yml` (Loki's tenant `fake` for
     auth_enabled=false).
"""
from __future__ import annotations

from pathlib import Path


_BACKEND = Path(__file__).resolve().parents[2]


def test_loki_rules_file_has_three_alerts():
    """loki-rules.yml must define JwtDecodeFailureSpike,
    OauthStateRejectedSpike, LockboxBootstrapFailed."""
    text = (_BACKEND / "docker" / "loki-rules.yml").read_text()
    for alert in (
        "JwtDecodeFailureSpike",
        "OauthStateRejectedSpike",
        "LockboxBootstrapFailed",
    ):
        assert f"alert: {alert}" in text, (
            f"loki-rules.yml must define {alert}"
        )


def test_loki_rules_use_logql_stream_syntax():
    """Each alert expression must use LogQL stream syntax
    `{logger_name=...} |~ ...` — that's the whole point of Loki ruler
    (Prometheus rejects this syntax, which is why R2-8 failed)."""
    text = (_BACKEND / "docker" / "loki-rules.yml").read_text()
    assert '{logger_name="src.auth.jwt_auth"' in text, (
        "JwtDecodeFailureSpike must filter on logger_name=src.auth.jwt_auth"
    )
    assert '{logger_name="src.auth.oauth"' in text, (
        "OauthStateRejectedSpike must filter on logger_name=src.auth.oauth"
    )
    assert '{logger_name="vault.bootstrap"' in text, (
        "LockboxBootstrapFailed must filter on logger_name=vault.bootstrap"
    )
    # `|~` is LogQL regex-match operator.
    assert "|~" in text, "rules must use LogQL `|~` regex-match"


def test_lockbox_alert_is_critical():
    """LockboxBootstrapFailed is the only critical-severity rule —
    secrets fetch failure means degraded-mode (audit-log HMAC off,
    signed-pickle off). Operator must page."""
    text = (_BACKEND / "docker" / "loki-rules.yml").read_text()
    lockbox_idx = text.find("alert: LockboxBootstrapFailed")
    next_alert = text.find("- alert:", lockbox_idx + 1)
    block = text[lockbox_idx:next_alert] if next_alert > 0 else text[lockbox_idx:]
    assert "severity: critical" in block, (
        "LockboxBootstrapFailed must be severity=critical"
    )


def test_loki_config_has_ruler_block():
    """loki-config.yaml must declare a `ruler:` block pointing at
    alertmanager:9093, otherwise the ruler is dormant and the alerts
    never fire even if the file is mounted."""
    text = (_BACKEND / "docker" / "loki-config.yaml").read_text()
    assert "ruler:" in text, "loki-config.yaml must declare ruler block"
    assert "alertmanager_url: http://alertmanager:9093" in text, (
        "ruler must target the compose alertmanager service"
    )
    assert "enable_alertmanager_v2: true" in text, (
        "ruler should use alertmanager v2 API (current default)"
    )
    # Storage path must match the mount.
    assert "directory: /loki/rules" in text, (
        "ruler.storage.local.directory must be /loki/rules to match mount path"
    )


def test_compose_mounts_loki_rules_at_tenant_fake_path():
    """Loki expects rule files under
    `<storage.local.directory>/<tenant>/`. With auth_enabled=false
    the tenant is `fake` — so the host file must mount at
    `/loki/rules/fake/loki-rules.yml`."""
    text = (_BACKEND / "docker" / "docker-compose.yml").read_text()
    loki_idx = text.find("  loki:")
    next_svc = text.find("\n  promtail:", loki_idx)
    block = text[loki_idx:next_svc]
    assert "./loki-rules.yml:/loki/rules/fake/loki-rules.yml" in block, (
        "compose must mount loki-rules.yml at /loki/rules/fake/ "
        "(Loki's tenant path for auth_enabled=false)"
    )


def test_legacy_promql_alerts_remain_removed():
    """The 3 LogQL-in-PromQL alerts removed from prometheus
    alerts.yml in commit 7cf0f9a must NOT reappear there. Their home
    is now loki-rules.yml exclusively."""
    # R6-5 (2026-05-18) — alerts.yml moved into docker/prometheus/
    # to enable directory bind-mount (fixes single-file inode trap).
    text = (_BACKEND / "docker" / "prometheus" / "alerts.yml").read_text()
    # They may appear in REMOVED comments, but not as real `- alert: X` defs.
    # Strict: search for the `- alert: <name>` heading at start of line.
    import re
    for name in ("JwtDecodeFailureSpike", "OauthStateRejectedSpike", "LockboxBootstrapFailed"):
        if not re.search(rf"^\s*- alert: {name}\b", text, re.M):
            continue
        # If we find the heading, fail.
        raise AssertionError(
            f"{name} must not re-appear as a Prometheus alert "
            "(removed in 7cf0f9a, ported to Loki ruler)"
        )
