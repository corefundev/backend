"""
tests/lint/test_alert_metric_has_producer.py

For every sku_* metric an alert depends on, assert at least one
PRODUCER exists in src/ or scripts/. Without this, an alert can
silently NEVER fire (no code path ever pushes the metric) — the
"phantom-metric" class that recurred multiple times in this repo:

  - R10 A1/A2/A3 (2026-05-27): WorkerQueueBacklog / LokiPushFailure /
    OtpBruteForce all silently never fired because the cited metric
    name had no producer (or was renamed by an upstream version bump).
  - BackupStale (2026-05-30): the metric class itself was fine, but
    we still re-hit the "missing producer at right moment" class —
    same root cause discipline applies.

The lint scopes to the `sku_` namespace because:

  • That's the project's own metric prefix; everything else
    (`pg_*`, `rq_*`, `loki_*`, `node_*`, `container_*`, …) comes
    from third-party exporters and can't be lint-checked this way.
  • The PR #36 finding (audit_log_event_failure_total without
    sku_ prefix) is a separate naming convention drift, tracked
    separately. The strict-sku scope here is the right minimum.

A "producer" means the metric name appears as a literal in source
under src/ or scripts/. Two extra recognised patterns for dynamic
metrics:

  • Metrics ending in `_cron_fired_timestamp_seconds` are produced
    by cron_wrapper.sh via `printf "sku_%s_cron_fired_…" "$job"`;
    we recognise them if cron_wrapper.sh exists AND the per-cron
    invocation in Dockerfile.backup carries the matching <job> name.
  • Anything else: literal substring match in src/ or scripts/.

This is the structural gate that prevents the next phantom-metric
introduction from reaching prod — adding an alert on `sku_xyz_…`
fails this lint until you also add the producer.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml


_BACKEND = Path(__file__).resolve().parents[2]
_ALERT_FILES = [
    _BACKEND / "docker" / "prometheus" / "alerts.yml",
    _BACKEND / "docker" / "prometheus" / "alerts.production.yml",
]
# Code trees where producers live. NOT docker/ (alert files themselves
# would match) and NOT tests/ (test assertions reference metrics for
# coverage purposes, not as producers).
_PRODUCER_ROOTS = [
    _BACKEND / "src",
    _BACKEND / "scripts",
]

# PromQL keywords / functions / aggregators — never metric names.
_PROMQL_KEYWORDS = {
    "sum", "avg", "count", "rate", "irate", "time", "absent", "by",
    "or", "and", "on", "ignoring", "without", "offset",
    "max", "min", "quantile", "histogram_quantile",
    "if", "else", "vector", "scalar", "clamp_max", "clamp_min", "bool",
    "changes", "increase", "delta", "idelta", "deriv", "predict_linear",
    "round", "floor", "ceil", "exp", "ln", "log2", "log10", "sqrt",
    "abs", "sgn", "timestamp",
    "year", "month", "day_of_week", "hour", "minute", "days_in_month",
    "label_replace", "label_join", "topk", "bottomk",
    "group_left", "group_right", "sort", "sort_desc",
    "absent_over_time", "last_over_time", "avg_over_time",
    "max_over_time", "min_over_time", "sum_over_time", "count_over_time",
}


def _extract_metric_names(expr: str) -> set[str]:
    """Best-effort metric extraction from a PromQL expression.

    Strategy: strip label selectors (eat label values), then collect
    identifiers, dropping PromQL keywords/functions.
    """
    # Strip label-selector contents `{foo="bar"}` so label values
    # don't pollute the candidate list.
    stripped = re.sub(r"\{[^}]*\}", "", expr)
    # Strip string literals + range selectors.
    stripped = re.sub(r'"[^"]*"', "", stripped)
    stripped = re.sub(r"\[[^\]]*\]", "", stripped)
    return {
        tok
        for tok in re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]+\b", stripped)
        if tok not in _PROMQL_KEYWORDS
    }


def _sku_metrics_in_alerts() -> set[str]:
    metrics: set[str] = set()
    for path in _ALERT_FILES:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        for group in data.get("groups", []):
            for rule in group.get("rules", []):
                expr = rule.get("expr", "")
                metrics.update(_extract_metric_names(expr))
    return {m for m in metrics if m.startswith("sku_")}


def _literal_producer_files(metric: str) -> list[Path]:
    """Source files under src/ or scripts/ that literally mention the
    metric name (string-level match — captures Counter('sku_…'),
    push-curl payloads, and any other literal use)."""
    matches: list[Path] = []
    for root in _PRODUCER_ROOTS:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix not in {".py", ".sh", ".yml", ".yaml"}:
                continue
            try:
                if metric in p.read_text(encoding="utf-8", errors="ignore"):
                    matches.append(p)
            except OSError:
                continue
    return matches


def _has_dynamic_cron_fired_producer(metric: str) -> bool:
    """`sku_<JOB>_cron_fired_timestamp_seconds` is produced by
    cron_wrapper.sh as `printf "sku_%s_cron_fired…" "$job"` — never a
    literal of the full metric name. Recognise it if BOTH:

      (a) cron_wrapper.sh exists + builds the suffix pattern, AND
      (b) some crontab invokes `cron_wrapper.sh <JOB> …` so the
          rendered <JOB> matches the metric.

    Without (b) the wrapper exists but nothing actually fires it →
    metric would still be phantom."""
    suffix = "_cron_fired_timestamp_seconds"
    if not metric.endswith(suffix):
        return False
    if not metric.startswith("sku_"):
        return False
    job = metric[len("sku_"):-len(suffix)]

    wrapper = _BACKEND / "scripts" / "cron_wrapper.sh"
    if not wrapper.exists():
        return False
    if "_cron_fired_timestamp_seconds" not in wrapper.read_text(encoding="utf-8"):
        return False

    # Find a crontab/Dockerfile invocation of `cron_wrapper.sh <job> …`.
    # No extension-filter — `Dockerfile.backup` has suffix `.backup`
    # which a naive {".sh","",".yml"} allow-list misses (caught
    # 2026-05-31). docker/ + scripts/ are small enough to just read
    # every text file.
    for root in (_BACKEND / "docker", _BACKEND / "scripts"):
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except (OSError, UnicodeDecodeError):
                continue
            if re.search(rf"cron_wrapper\.sh\s+{re.escape(job)}\b", text):
                return True
    return False


def test_every_sku_metric_in_alerts_has_a_producer():
    """The structural phantom-metric gate. Adding an alert on a
    `sku_*` metric REQUIRES adding a producer somewhere under src/
    or scripts/ — this lint fails CI otherwise."""
    referenced = _sku_metrics_in_alerts()
    assert referenced, (
        "no sku_* metrics found in alerts — either the alert files "
        "moved, the regex broke, or every project alert switched to "
        "another namespace. Investigate before silently passing."
    )

    missing: list[str] = []
    for metric in sorted(referenced):
        if _literal_producer_files(metric):
            continue
        if _has_dynamic_cron_fired_producer(metric):
            continue
        missing.append(metric)

    assert not missing, (
        "the following sku_* metrics are referenced by alerts but have "
        "NO producer in src/ or scripts/ (phantom-metric class):\n  "
        + "\n  ".join(missing)
        + "\n\nEither:\n"
        "  (a) add the producer (push to pushgateway / prometheus_client "
        "Counter|Gauge|Histogram / cron_wrapper.sh invocation), OR\n"
        "  (b) remove the alert if the metric is genuinely gone (drift\n"
        "      from a removed feature).\n"
        "Silent alerts that never fire are worse than no alerts — they "
        "create false confidence."
    )
