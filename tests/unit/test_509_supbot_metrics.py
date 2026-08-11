"""#509: экспортер метрик и лимитер эскалаций (support/api/metrics.py) +
контракты конфигурации (парсинг, не substring — правило config-тестов):
scrape-job supbot только в prod-prometheus, алерты supbot только в
alerts.production.yml.
"""
import importlib.util
from pathlib import Path

import yaml

spec = importlib.util.spec_from_file_location(
    "supbot_metrics", Path("support/api/metrics.py"))
metrics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metrics)


def test_counter_with_labels_renders():
    r = metrics.Registry()
    r.inc("supbot_requests_total", {"outcome": "answered"})
    r.inc("supbot_requests_total", {"outcome": "answered"})
    r.inc("supbot_requests_total", {"outcome": "escalated"})
    out = r.render()
    assert 'supbot_requests_total{outcome="answered"} 2.0' in out
    assert 'supbot_requests_total{outcome="escalated"} 1.0' in out


def test_histogram_buckets_cumulative():
    r = metrics.Registry()
    r.observe("supbot_request_seconds", 1.5)
    r.observe("supbot_request_seconds", 25.0)
    out = r.render()
    assert 'supbot_request_seconds_bucket{le="2.0"} 1.0' in out
    assert 'supbot_request_seconds_bucket{le="30.0"} 2.0' in out
    assert 'supbot_request_seconds_bucket{le="+Inf"} 2.0' in out
    assert "supbot_request_seconds_count 2" in out
    assert "supbot_request_seconds_sum 26.5" in out


def test_limiter_dedupes_session_and_caps_hourly():
    lim = metrics.EscalationNotifyLimiter(session_window=600, hourly_cap=3)
    assert lim.allow("s1", now=0.0)
    assert not lim.allow("s1", now=10.0)      # та же сессия в окне
    assert lim.allow("s1", now=700.0)          # окно вышло
    assert lim.allow("s2", now=701.0)
    assert not lim.allow("s3", now=702.0)      # часовой кэп (3)
    assert lim.allow("s4", now=4400.0)         # окно часа съехало


def test_scrape_job_prod_only():
    prod = yaml.safe_load(Path("docker/prometheus/prometheus.yml").read_text())
    jobs = [j["job_name"] for j in prod["scrape_configs"]]
    assert "supbot" in jobs
    staging = yaml.safe_load(
        Path("docker/prometheus/prometheus.staging.yml").read_text())
    sjobs = [j["job_name"] for j in staging.get("scrape_configs", [])]
    assert "supbot" not in sjobs, "staging не имеет supbot — job там ложный"


def test_supbot_alerts_prod_file_only():
    prod = yaml.safe_load(
        Path("docker/prometheus/alerts.production.yml").read_text())
    names = {g["name"] for g in prod["groups"]}
    assert "supbot" in names
    grp = next(g for g in prod["groups"] if g["name"] == "supbot")
    alerts = {r["alert"] for r in grp["rules"]}
    assert {"SupbotDown", "SupbotHighEscalationShare",
            "SupbotSlowP95"} <= alerts
    shared = yaml.safe_load(Path("docker/prometheus/alerts.yml").read_text())
    shared_names = {g["name"] for g in shared["groups"]}
    assert "supbot" not in shared_names, (
        "supbot-алерты в общем файле зашумят staging (класс из "
        "test_alerts_split)")
