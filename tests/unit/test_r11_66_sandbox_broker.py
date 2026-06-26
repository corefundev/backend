"""
tests/unit/test_r11_66_sandbox_broker.py

R11-#66 — process-worker loses the raw RW docker.sock; sandbox spawns
move to the sandbox-broker service. Three layers under test:

  1. broker request hardening (path traversal, name regexes, symlink
     escape, bounded limits) with the docker spawn stubbed out;
  2. DockerSandbox's broker transport (staging round-trip, error
     handling, cleanup);
  3. compose + cd_deploy wiring (parsed YAML, not raw-text grep).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import src.storage.sandbox as sandbox_mod
import src.storage.sandbox_broker as broker_mod
from src.storage.sandbox import DockerSandbox, SandboxError

ROOT = Path(__file__).resolve().parents[2]


# ── 1. broker request hardening ─────────────────────────────────────

@pytest.fixture()
def broker(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOX_WORK_DIR", str(tmp_path))
    monkeypatch.setattr(broker_mod, "_docker_client", object())  # never used
    return TestClient(broker_mod.app)


def _mk_staging(work_dir: Path, job: str = "job-abc12345") -> Path:
    staging = work_dir / job
    (staging / "in").mkdir(parents=True)
    (staging / "out").mkdir()
    (staging / "in" / "original.csv").write_text("a,b\n1,2\n")
    return staging


def _spawn(client, **overrides):
    payload = {"job_dir": "job-abc12345", "input_name": "original.csv"}
    payload.update(overrides)
    return client.post("/spawn", json=payload)


def test_broker_rejects_traversal_job_dir(broker):
    for bad in ("../etc", "job-a/../../etc", "/etc", "job-а-кириллица", "job-" + "x" * 80):
        assert _spawn(broker, job_dir=bad).status_code == 400, bad


def test_broker_rejects_bad_input_name(broker, tmp_path):
    _mk_staging(tmp_path)
    for bad in ("../../etc/passwd", "evil.sh", "original.csv.exe", "original."):
        assert _spawn(broker, input_name=bad).status_code == 400, bad


def test_broker_404_when_staging_missing(broker):
    assert _spawn(broker).status_code == 404


def test_broker_rejects_symlink_escape(broker, tmp_path):
    outside = tmp_path.parent / "outside-job"
    (outside / "in").mkdir(parents=True)
    (outside / "out").mkdir()
    (outside / "in" / "original.csv").write_text("a\n1\n")
    (tmp_path / "job-evil1234").symlink_to(outside)
    assert _spawn(broker, job_dir="job-evil1234").status_code == 400


def test_broker_rejects_out_of_bounds_limits(broker, tmp_path):
    _mk_staging(tmp_path)
    assert _spawn(broker, max_rows=0).status_code == 422
    assert _spawn(broker, max_rows=10**9).status_code == 422
    assert _spawn(broker, max_columns=100000).status_code == 422


def test_broker_spawns_with_validated_paths(broker, tmp_path, monkeypatch):
    staging = _mk_staging(tmp_path)
    seen = {}

    def fake_spawn(client, **kw):
        seen.update(kw)
        return 0, False, "parsed", ""

    monkeypatch.setattr(broker_mod, "spawn_and_wait", fake_spawn)
    r = _spawn(broker, max_rows=1000, max_columns=8)
    assert r.status_code == 200
    body = r.json()
    assert body == {"exit_code": 0, "timed_out": False, "stdout": "parsed", "stderr": ""}
    assert seen["in_dir"] == staging.resolve() / "in"
    assert seen["out_dir"] == staging.resolve() / "out"
    assert seen["input_name"] == "original.csv"
    assert seen["max_rows"] == 1000 and seen["max_columns"] == 8


def test_broker_maps_sandbox_error_to_502(broker, tmp_path, monkeypatch):
    _mk_staging(tmp_path)

    def boom(client, **kw):
        raise SandboxError("daemon down")

    monkeypatch.setattr(broker_mod, "spawn_and_wait", boom)
    assert _spawn(broker).status_code == 502


# ── 2. DockerSandbox broker transport ───────────────────────────────

def _result_body(exit_code=0, timed_out=False, stdout="", stderr=""):
    return json.dumps({
        "exit_code": exit_code, "timed_out": timed_out,
        "stdout": stdout, "stderr": stderr,
    }).encode()


class _FakeHTTPResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_sandbox_broker_transport_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOX_BROKER_URL", "http://sandbox-broker:8100")
    box = DockerSandbox(work_dir=tmp_path)
    assert box._client is None    # no docker SDK touched in broker mode

    def fake_urlopen(req, timeout=0):
        payload = json.loads(req.data)
        # the broker writes results into the staging the worker created
        out_dir = tmp_path / payload["job_dir"] / "out"
        (out_dir / "data.parquet").write_bytes(b"PARQUET")
        (out_dir / "manifest.json").write_text('{"rows": 1}')
        assert payload["input_name"] == "original.csv"
        return _FakeHTTPResponse(_result_body(exit_code=0, stdout="ok"))

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    res = box.run(b"a,b\n1,2\n", "data.csv")
    assert res.ok and res.exit_code == 0
    assert res.manifest == {"rows": 1}
    assert res.output_path is not None and res.output_path.exists()
    # staging job dir is cleaned up, only persisted/ remains
    assert [p.name for p in tmp_path.iterdir()] == ["persisted"]


def test_sandbox_broker_rejection_raises_and_cleans(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOX_BROKER_URL", "http://sandbox-broker:8100")
    box = DockerSandbox(work_dir=tmp_path)

    import urllib.error
    import urllib.request

    def fake_urlopen(req, timeout=0):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(SandboxError):
        box.run(b"a\n1\n", "data.csv")
    assert list(tmp_path.iterdir()) == []    # staging removed on failure


def test_sandbox_local_mode_still_requires_docker(tmp_path, monkeypatch):
    monkeypatch.delenv("SANDBOX_BROKER_URL", raising=False)
    captured = {}

    def fake_spawn(client, **kw):
        captured.update(kw)
        out_dir = kw["out_dir"]
        (out_dir / "data.parquet").write_bytes(b"P")
        (out_dir / "manifest.json").write_text("{}")
        return 0, False, "", ""

    monkeypatch.setattr(sandbox_mod, "spawn_and_wait", fake_spawn)

    box = DockerSandbox.__new__(DockerSandbox)   # skip __init__ docker connect
    box.image, box.timeout_sec, box.mem_limit = "img", 30, "512m"
    box.cpus, box.runtime, box.broker_url = 1.0, "runc", ""
    box.work_dir, box._client = tmp_path, object()

    res = box.run(b"a\n1\n", "data.csv")
    assert res.ok and captured["image"] == "img"


# ── 3. compose + cd_deploy wiring (parsed, not grepped) ─────────────

def _load_yaml_ignore_tags(path: Path) -> dict:
    class _Loader(yaml.SafeLoader):
        pass

    def _construct_any(loader, node):
        if isinstance(node, yaml.MappingNode):
            return loader.construct_mapping(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        return loader.construct_scalar(node)

    # Register the EXACT compose merge tags on the subclass: another
    # test in the suite registers "!override" globally on SafeLoader
    # (returning None), and an inherited exact-tag constructor beats a
    # subclass multi-constructor — shadow it here.
    for tag in ("!override", "!reset"):
        _Loader.add_constructor(tag, _construct_any)
    _Loader.add_multi_constructor(
        "!", lambda loader, suffix, node: _construct_any(loader, node),
    )
    return yaml.load(path.read_text(), Loader=_Loader)


def test_compose_process_worker_has_no_docker_socket():
    base = yaml.safe_load((ROOT / "docker" / "docker-compose.yml").read_text())
    pw = base["services"]["process-worker"]
    assert not any("docker.sock" in v for v in pw.get("volumes", []))
    assert pw["environment"]["SANDBOX_BROKER_URL"] == "http://sandbox-broker:8100"
    assert "sandbox-broker" in pw["depends_on"]
    assert set(pw["networks"]) == {"default", "sandbox-broker"}
    # the healthcheck no longer probes the socket
    assert "docker.sock" not in json.dumps(pw["healthcheck"])


def test_compose_broker_is_the_only_sock_holder_in_app_tier():
    base = yaml.safe_load((ROOT / "docker" / "docker-compose.yml").read_text())
    broker = base["services"]["sandbox-broker"]
    assert any("/var/run/docker.sock" in v for v in broker["volumes"])
    assert broker["networks"] == ["sandbox-broker"]
    assert broker["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in broker["security_opt"]
    # the broker skips *common-env (no secrets), so the sandbox knobs it
    # needs MUST be passed explicitly — an empty SANDBOX_IMAGE makes the
    # broker fall back to a non-existent local tag (caught live 2026-06-12)
    env = broker["environment"]
    assert "sku-forecasting-sandbox" in env["SANDBOX_IMAGE"]
    assert "DATABASE_URL" not in env and "REDIS_URL" not in env
    # private network is internal (no egress)
    assert base["networks"]["sandbox-broker"]["internal"] is True
    # no other service mounts the raw RW socket. Deliberate exceptions:
    # docker-socket-proxy (the scoped-access pattern itself) and
    # promtail (docker_sd log discovery — READ-ONLY mount enforced here).
    for name, svc in base["services"].items():
        if name in ("sandbox-broker", "docker-socket-proxy"):
            continue
        vols = [str(v) for v in (svc.get("volumes") or [])]
        sock = [v for v in vols if "/var/run/docker.sock" in v]
        if name == "promtail":
            assert sock and all(v.endswith(":ro") for v in sock), (
                "promtail's docker.sock mount must stay read-only"
            )
            continue
        assert not sock, name


def test_minimal_overlay_moves_docker_group_to_broker():
    overlay = _load_yaml_ignore_tags(ROOT / "docker" / "docker-compose.minimal.yml")
    pw = overlay["services"]["process-worker"]
    assert "group_add" not in pw
    broker = overlay["services"]["sandbox-broker"]
    assert broker["group_add"] == ["988"]
    assert broker["environment"]["SANDBOX_WORK_DIR"] == "/srv/backend/sandbox-staging"
    assert "/srv/backend/sandbox-staging:/srv/backend/sandbox-staging" in broker["volumes"]
    # prod's !override depends_on must keep the broker dependency
    assert "sandbox-broker" in pw["depends_on"]


def test_cd_deploy_recreates_and_asserts_broker():
    script = (ROOT / "scripts" / "cd_deploy.sh").read_text()
    assert script.count("sandbox-broker worker scan-worker process-worker") >= 1
    assert "docker-sandbox-broker-1" in script
    assert "sandbox-broker image mismatch" in script
