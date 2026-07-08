"""
DP-4 (#324): the hardened parser command carries the new --sniff / --mapping
toggles into the container. The hardening PROFILE (image/mounts/caps/limits)
lives in spawn_and_wait and is unchanged — only the parser CLI args differ.
"""
from __future__ import annotations

from src.storage import sandbox


class _FakeContainer:
    def wait(self, timeout=None):
        return {"StatusCode": 0}

    def logs(self, stdout=True, stderr=False):
        return b""

    def remove(self, force=True):
        pass


class _FakeContainers:
    def __init__(self):
        self.last = None

    def run(self, image, command, **kw):
        self.last = {"image": image, "command": command, "kw": kw}
        return _FakeContainer()


class _FakeClient:
    def __init__(self):
        self.containers = _FakeContainers()


def _command(tmp_path, **extra):
    client = _FakeClient()
    (tmp_path / "in").mkdir(exist_ok=True)
    (tmp_path / "out").mkdir(exist_ok=True)
    sandbox.spawn_and_wait(
        client, image="img", in_dir=tmp_path / "in", out_dir=tmp_path / "out",
        input_name="original.csv", max_rows=100, max_columns=10, timeout_sec=5,
        mem_limit="256m", cpus=1.0, runtime="runc", **extra,
    )
    return client.containers.last["command"]


def test_default_command_has_no_new_flags(tmp_path):
    cmd = _command(tmp_path)
    assert "--sniff" not in cmd and "--mapping" not in cmd


def test_sniff_flag_appended(tmp_path):
    cmd = _command(tmp_path, sniff=True)
    assert "--sniff" in cmd
    assert "--mapping" not in cmd


def test_mapping_flag_appended_with_value(tmp_path):
    payload = '{"date": "Дата", "sku": "Артикул", "sales": "Кол-во"}'
    cmd = _command(tmp_path, mapping=payload)
    assert "--mapping" in cmd
    assert cmd[cmd.index("--mapping") + 1] == payload


def test_hardening_profile_unchanged(tmp_path):
    # the profile knobs must still be pinned regardless of the new toggles
    client = _FakeClient()
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()
    sandbox.spawn_and_wait(
        client, image="img", in_dir=tmp_path / "in", out_dir=tmp_path / "out",
        input_name="original.csv", max_rows=100, max_columns=10, timeout_sec=5,
        mem_limit="256m", cpus=1.0, runtime="runc", sniff=True, mapping='{"x": "y"}',
    )
    kw = client.containers.last["kw"]
    assert kw["network_mode"] == "none"
    assert kw["read_only"] is True
    assert kw["user"] == "65534:65534"
    assert kw["cap_drop"] == ["ALL"]
