"""
tests/unit/test_r11_97_observability_hardening.py

R11-#97 tranche 1 — the stateless observability services (exporters +
tempo/loki/promtail) run read-only / network-only and need NO Linux
capabilities, so they must declare cap_drop: ALL + no-new-privileges.

This is the low-risk first tranche of #97; the data tier (postgres/redis/
clamav/minio) and the Lockbox-root family (backup/alertmanager/prometheus/
postgres-exporter) land in follow-up PRs because they need per-service
cap_add for entrypoint privilege drops. Airflow is excluded (slated for
teardown #90).

Assertions parse the YAML (never raw-text grep — the R11-M12 false-pass
lesson).
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((ROOT / "docker" / "docker-compose.yml").read_text())

# Tranche 1: stateless exporters + log/trace stores. All read config (ro),
# write only their own data volume (if any), and serve over the network.
OBSERVABILITY_STATELESS = [
    "ssl-exporter", "rq-exporter", "pushgateway", "node-exporter",
    "tempo", "loki", "promtail",
]


def test_observability_stateless_drops_all_caps_and_no_new_privileges():
    for name in OBSERVABILITY_STATELESS:
        svc = COMPOSE["services"][name]
        assert svc.get("cap_drop") == ["ALL"], f"{name}: cap_drop must be [ALL]"
        assert "no-new-privileges:true" in (svc.get("security_opt") or []), (
            f"{name}: missing no-new-privileges"
        )


def test_observability_stateless_adds_no_capabilities_back():
    # These services need ZERO caps — none should re-add a capability
    # (unlike the privilege-dropping infra services grafana/mlflow that
    # need SETUID/SETGID, or cadvisor's SYS_PTRACE).
    for name in OBSERVABILITY_STATELESS:
        svc = COMPOSE["services"][name]
        assert not svc.get("cap_add"), f"{name}: must not cap_add (tranche 1)"


def test_promtail_keeps_readonly_docker_socket():
    # promtail needs the docker socket for service discovery; the cap_drop
    # must NOT have forced its removal, and it must stay read-only.
    vols = COMPOSE["services"]["promtail"].get("volumes") or []
    sock = [v for v in vols if "/var/run/docker.sock" in v]
    assert sock, "promtail must keep the docker.sock mount for SD"
    assert all(v.endswith(":ro") for v in sock), (
        "promtail docker.sock must stay read-only"
    )


def test_pushgateway_init_pre_owns_volume_so_cap_dropped_pushgateway_can_persist():
    # R11-#97 durable fix: pushgateway runs cap_drop ALL as `nobody` and so
    # can't write its --persistence.file to a root-owned fresh volume nor
    # chown it. A one-shot init container pre-owns the volume to 65534 with
    # the single CHOWN cap, and pushgateway waits for it to complete. Without
    # this, a recreate on a fresh/root-owned volume loses the in-memory series
    # → BackupStale fires (staging 2026-06-18).
    svcs = COMPOSE["services"]
    init = svcs.get("pushgateway-init")
    assert init is not None, "pushgateway-init must exist to pre-own the volume"
    assert init.get("cap_drop") == ["ALL"]
    assert init.get("cap_add") == ["CHOWN"], "init needs ONLY CHOWN (minimal)"
    assert "no-new-privileges:true" in (init.get("security_opt") or [])
    assert init.get("restart") == "no", "init is a one-shot"
    assert init.get("command") == ["chown", "-R", "65534:65534", "/data"]
    assert any(v.startswith("pushgateway_data:") for v in (init.get("volumes") or [])), (
        "init must mount the pushgateway_data volume it chowns"
    )
    # pushgateway must gate on the init completing.
    dep = (svcs["pushgateway"].get("depends_on") or {}).get("pushgateway-init")
    assert dep == {"condition": "service_completed_successfully"}, (
        "pushgateway must wait for pushgateway-init to complete"
    )
    # Both mount the same volume at /data.
    pg_vols = svcs["pushgateway"].get("volumes") or []
    assert any(v == "pushgateway_data:/data" for v in pg_vols)


def test_postgres_exporter_hardened_no_caps():
    # R11-#97 — postgres-exporter already runs as USER nobody
    # (Dockerfile.postgres-exporter) and is stateless (no data volume), so it
    # gets cap_drop ALL + no-new-privileges with NO cap_add and needs no
    # privilege-drop entrypoint change.
    svc = COMPOSE["services"]["postgres-exporter"]
    assert svc.get("cap_drop") == ["ALL"], "postgres-exporter: cap_drop must be [ALL]"
    assert "no-new-privileges:true" in (svc.get("security_opt") or [])
    assert not svc.get("cap_add"), "postgres-exporter needs no caps back"
    assert not svc.get("volumes"), "postgres-exporter is stateless (no volume)"


def test_prometheus_hardened_no_init_needed():
    # R11-#97 — prometheus already runs as USER nobody (Dockerfile.prometheus,
    # which also `chown -R nobody:nobody /prometheus`), so a fresh prometheus_data
    # volume inherits nobody ownership via Docker copy-on-first-mount → no *-init
    # container needed (unlike upstream pushgateway). Just cap_drop ALL +
    # no-new-privileges, no cap_add (TSDB written to an owned volume; port 9090
    # is unprivileged).
    svcs = COMPOSE["services"]
    svc = svcs["prometheus"]
    assert svc.get("cap_drop") == ["ALL"], "prometheus: cap_drop must be [ALL]"
    assert "no-new-privileges:true" in (svc.get("security_opt") or [])
    assert not svc.get("cap_add"), "prometheus needs no caps back"
    assert "prometheus-init" not in svcs, (
        "prometheus needs no *-init — the image pre-owns /prometheus"
    )


def test_alertmanager_hardened_no_init_needed():
    # R11-#97 — like prometheus: already USER nobody (Dockerfile.alertmanager
    # also `chown -R nobody:nobody /alertmanager`), so a fresh alertmanager_data
    # volume inherits nobody ownership via Docker copy-on-first-mount → no *-init.
    # cap_drop ALL + no-new-privileges, no cap_add.
    svcs = COMPOSE["services"]
    svc = svcs["alertmanager"]
    assert svc.get("cap_drop") == ["ALL"], "alertmanager: cap_drop must be [ALL]"
    assert "no-new-privileges:true" in (svc.get("security_opt") or [])
    assert not svc.get("cap_add"), "alertmanager needs no caps back"
    assert "alertmanager-init" not in svcs, (
        "alertmanager needs no *-init — the image pre-owns /alertmanager"
    )


def test_backup_caps_dropped_stays_root():
    # R11-#97 — backup is the only genuine root service. dcron's crond binary
    # is mode 0700 root:root, so it CANNOT run as non-root ("crond: Permission
    # denied"). Hardening is Level 1: keep root, cap_drop ALL + no-new-privileges,
    # but cap_add SETGID,SETUID — dcron's per-job ChangeUser (setgid+initgroups+
    # setuid) needs them or it silently runs ZERO jobs (validated on staging).
    # No non-root drop, no backup-init.
    svcs = COMPOSE["services"]
    bk = svcs["backup"]
    assert bk.get("cap_drop") == ["ALL"], "backup: cap_drop must be [ALL]"
    assert "no-new-privileges:true" in (bk.get("security_opt") or [])
    assert sorted(bk.get("cap_add") or []) == ["SETGID", "SETUID"], (
        "backup needs SETGID+SETUID for dcron's per-job ChangeUser (else 0 jobs run)"
    )
    assert "backup-init" not in svcs, "no backup-init — root writes its own volumes"

    # Dockerfile must NOT have dropped to nobody (dcron crond is root-only).
    df = (ROOT / "docker" / "Dockerfile.backup").read_text()
    assert "USER nobody" not in df, (
        "backup must stay root — dcron crond binary is 0700 root, non-root fails"
    )
    assert "crontab -u nobody" not in df, "crontab stays under root's spool"


def test_redis_runs_nonroot_with_init_volume_preown():
    # R11-#97 data-tier — redis dropped from root to user 999:1000 with
    # cap_drop ALL + no-new-privileges, NO cap_add (redis-server on the
    # unprivileged 6379, writes only its own /data → no caps). redis-init
    # pre-owns the volume to 999:1000 (existing dump.rdb was root-owned because
    # redis used to run as root) so the cap-dropped redis can write its RDB.
    svcs = COMPOSE["services"]
    r = svcs["redis"]
    assert str(r.get("user")) == "999:1000", "redis must run as redis (999:1000)"
    assert r.get("cap_drop") == ["ALL"], "redis: cap_drop must be [ALL]"
    assert "no-new-privileges:true" in (r.get("security_opt") or [])
    assert not r.get("cap_add"), "redis needs no caps (non-root, unprivileged port, own volume)"
    dep = (r.get("depends_on") or {}).get("redis-init")
    assert dep == {"condition": "service_completed_successfully"}, (
        "redis must wait for redis-init to chown its volume"
    )

    init = svcs.get("redis-init")
    assert init is not None, "redis-init must exist to pre-own redis_data"
    assert init.get("cap_drop") == ["ALL"] and init.get("cap_add") == ["CHOWN"]
    assert init.get("restart") == "no"
    assert any("999:1000" in str(x) for x in (init.get("entrypoint") or [])), (
        "redis-init must chown to redis 999:1000"
    )
    assert any(v.startswith("redis_data:") for v in (init.get("volumes") or []))


def test_clamav_caps_dropped_stays_root_with_drop_caps():
    # R11-#97 data-tier — clamav stays root (its /init chowns the db volume and
    # creates /run/clamav before clamd+freshclam drop to the clamav user via
    # their own `--user=clamav`). Hardening = cap_drop ALL + no-new-privileges
    # + the minimal cap_add empirically required by the real startup (validated
    # on staging — fresh-volume clamd reached clamdcheck OK, daemons ran as the
    # clamav user; dropping any one cap broke startup):
    #   CHOWN+FOWNER  — /init chown -R + install -o/-m on /var/lib/clamav, /run/clamav
    #   DAC_OVERRIDE  — clamd/freshclam open clamav-owned 0640 logfiles while root (pre-drop)
    #   SETGID+SETUID — the --user=clamav privilege drop (setgid+initgroups+setuid)
    svcs = COMPOSE["services"]
    cv = svcs["clamav"]
    assert cv.get("cap_drop") == ["ALL"], "clamav: cap_drop must be [ALL]"
    assert "no-new-privileges:true" in (cv.get("security_opt") or [])
    assert sorted(cv.get("cap_add") or []) == [
        "CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"
    ], "clamav needs exactly CHOWN,DAC_OVERRIDE,FOWNER,SETGID,SETUID for its root /init + --user drop"
    assert "clamav-init" not in svcs, (
        "no clamav-init — the image's root /init pre-owns the db volume itself"
    )


def test_postgres_caps_dropped_stays_root_with_drop_caps():
    # R11-#97 data-tier — postgres stays root (its entrypoint chain edits
    # pg_hba + chowns/inits PGDATA, then su-exec/gosu drops to the postgres
    # user). Hardening = cap_drop ALL + no-new-privileges + the minimal cap_add
    # the real startup needs, determined by an empirical cap-subtraction matrix
    # on staging (throwaway docker-postgres:custom, fresh-initdb + restart paths;
    # dropping any of DAC_OVERRIDE/SETGID/SETUID exits the container):
    #   DAC_OVERRIDE  — root traverses the 0700 postgres-owned PGDATA
    #   SETGID+SETUID — the su-exec/gosu drop to the postgres user
    #   CHOWN+FOWNER  — preserve the official entrypoint's boot-time PGDATA
    #                   ownership/perms self-heal (DR-restore safety net)
    svcs = COMPOSE["services"]
    pg = svcs["postgres"]
    assert pg.get("cap_drop") == ["ALL"], "postgres: cap_drop must be [ALL]"
    assert "no-new-privileges:true" in (pg.get("security_opt") or [])
    assert sorted(pg.get("cap_add") or []) == [
        "CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"
    ], "postgres needs exactly CHOWN,DAC_OVERRIDE,FOWNER,SETGID,SETUID (root entrypoint + gosu drop)"
    assert "postgres-init" not in svcs, (
        "no postgres-init — the root entrypoint chowns/inits PGDATA itself"
    )
    # Volume must still be mounted (the cap set is what lets root reach it).
    assert any(v.startswith("postgres_data:") for v in (pg.get("volumes") or [])), (
        "postgres must keep its data volume"
    )


def test_already_hardened_services_unchanged():
    # Guard the previously-hardened set so this sweep doesn't regress them.
    for name in ("api", "worker", "scan-worker", "process-worker", "migrate",
                 "sandbox-broker", "pgbouncer", "mlflow", "grafana",
                 "docker-socket-proxy", "autoheal", "cadvisor"):
        svc = COMPOSE["services"][name]
        assert svc.get("cap_drop") == ["ALL"], f"{name} regressed"
        assert "no-new-privileges:true" in (svc.get("security_opt") or [])
