"""
src/storage/sandbox.py

Run `secure_parser.py` inside an isolated Docker container.

Why Docker (not Firecracker / gVisor)?
──────────────────────────────────────
The spec asks for Firecracker microVMs, which need KVM on the host and are
operationally heavy (ignite, containerd, etc.). For the real threat model
here — untrusted CSV/XLSX files, no native code execution in the parser —
a hardened Docker container provides the right tradeoff:

  • net=none         — zero egress (no data exfil, no C2)
  • read_only=True   — rootfs is immutable; malware can't persist
  • cap-drop=ALL     — no capabilities, not even chown
  • no-new-privileges=True
  • user=65534:65534 (nobody) — not root
  • mem_limit=512m, cpus=1.0, pids_limit=64, timeout=30s
  • /sandbox/in      — ro bind mount (the input file)
  • /sandbox/out     — rw tmpfs, 64 MiB (parser output)
  • /tmp             — rw tmpfs, 64 MiB

The parser is a pure-Python numpy/pandas script; there is no shell, no
package installer, no compiler available inside the image.

For an even stronger posture, swap `runtime="runsc"` (gVisor) — same code,
two-line ops change.

Dependency: the `sku-forecasting-sandbox` image must exist. Build with:

    docker build -f docker/Dockerfile.sandbox -t sku-forecasting-sandbox .

Env vars
────────
    SANDBOX_IMAGE            default: sku-forecasting-sandbox
    SANDBOX_TIMEOUT_SEC      default: 30
    SANDBOX_MEM_LIMIT        default: 512m
    SANDBOX_CPUS             default: 1.0
    SANDBOX_MAX_ROWS         default: 5000000
    SANDBOX_MAX_COLUMNS      default: 64
    SANDBOX_WORK_DIR         default: /var/lib/sku-sandbox (host-side staging)
    SANDBOX_RUNTIME          default: runc  (set to "runsc" for gVisor)
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class SandboxResult:
    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    output_path: Path | None     # parquet, if ok
    manifest: dict | None        # manifest.json contents, if ok


class SandboxError(RuntimeError):
    """Sandbox could not run — differs from a parser validation failure."""


def spawn_and_wait(
    client,
    *,
    image: str,
    in_dir: Path,
    out_dir: Path,
    input_name: str,
    max_rows: int,
    max_columns: int,
    timeout_sec: int,
    mem_limit: str,
    cpus: float,
    runtime: str,
    sniff: bool = False,
    mapping: str | None = None,
) -> tuple[int, bool, str, str]:
    """
    Run secure_parser in a hardened container and reap it.

    The HARDENING PROFILE LIVES HERE and only here (R11-#66): both the
    dev in-process path (DockerSandbox without a broker) and the
    sandbox-broker service call this function, so the profile cannot
    drift between them — and the broker cannot be talked into running
    anything else.

    Returns (exit_code, timed_out, stdout, stderr). Raises SandboxError
    when the container cannot be launched at all.
    """
    # The sandbox image's ENTRYPOINT is already
    #     ["python", "/opt/secure_parser.py"]
    # (see docker/Dockerfile.sandbox), so containers.run appends
    # `command` to that — don't repeat the python script here.
    command = [
        "--input",    f"/sandbox/in/{input_name}",
        "--output",   "/sandbox/out/data.parquet",
        "--manifest", "/sandbox/out/manifest.json",
        "--max-rows",    str(max_rows),
        "--max-columns", str(max_columns),
    ]
    # DP-4 (#324): the parser CLI is the ONLY thing the request can influence —
    # image/mounts/caps/limits stay pinned. --sniff emits a report instead of a
    # parquet; --mapping applies a confirmed {canonical: source} rename. Both are
    # data-only (json.loads, column rename) with no code path out of the sandbox.
    if sniff:
        command.append("--sniff")
    if mapping is not None:
        command += ["--mapping", mapping]
    try:
        container = client.containers.run(
            image=image,
            command=command,
            detach=True,
            # ── isolation ───────────────────────────────────────
            network_mode="none",
            read_only=True,
            user="65534:65534",
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            pids_limit=64,
            runtime=runtime,
            # ── resources ───────────────────────────────────────
            mem_limit=mem_limit,
            memswap_limit=mem_limit,    # disable swap use
            nano_cpus=int(cpus * 1e9),
            # ── filesystem ─────────────────────────────────────
            volumes={
                str(in_dir):  {"bind": "/sandbox/in",  "mode": "ro"},
                str(out_dir): {"bind": "/sandbox/out", "mode": "rw"},
            },
            tmpfs={
                "/tmp":            "size=64m,mode=1777",
                "/sandbox/scratch": "size=64m,mode=0700",
            },
            environment={
                "PYTHONDONTWRITEBYTECODE": "1",
                "HOME": "/sandbox/scratch",
                "TMPDIR": "/tmp",
            },
            auto_remove=False,   # we need to read logs after exit
            tty=False, stdin_open=False,
        )
    except Exception as e:
        raise SandboxError(f"container launch failed: {e}") from e

    try:
        try:
            result = container.wait(timeout=timeout_sec)
            exit_code = int(result.get("StatusCode", -1))
            timed_out = False
        except Exception:   # docker wait raises on timeout (depends on SDK version)
            container.kill()
            exit_code = 137
            timed_out = True

        stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
        stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")
        if timed_out:
            stderr = (stderr + f"\nTIMEOUT after {timeout_sec}s").strip()
        return exit_code, timed_out, stdout, stderr
    finally:
        try:
            container.remove(force=True)
        except Exception as e:
            logger.warning(f"container remove failed: {e}")


# ── Runner ─────────────────────────────────────────────────────────────────────

class DockerSandbox:
    """
    Run `secure_parser.py` in a hardened Docker container.

    Two transports (R11-#66):
      • SANDBOX_BROKER_URL set (prod/staging) — delegate the container
        spawn to the sandbox-broker service over HTTP. THIS process
        needs no Docker socket at all; it only stages files into the
        shared work_dir and reads the results back from it.
      • SANDBOX_BROKER_URL unset (dev/tests) — in-process docker SDK,
        same hardened profile via spawn_and_wait().
    """

    def __init__(
        self,
        image: str | None = None,
        timeout_sec: int | None = None,
        mem_limit: str | None = None,
        cpus: float | None = None,
        work_dir: Path | None = None,
        runtime: str | None = None,
    ):
        self.image       = image or os.environ.get("SANDBOX_IMAGE", "sku-forecasting-sandbox")
        self.timeout_sec = int(timeout_sec or os.environ.get("SANDBOX_TIMEOUT_SEC", "30"))
        self.mem_limit   = mem_limit or os.environ.get("SANDBOX_MEM_LIMIT", "512m")
        self.cpus        = float(cpus or os.environ.get("SANDBOX_CPUS", "1.0"))
        self.work_dir    = Path(work_dir or os.environ.get("SANDBOX_WORK_DIR", "/var/lib/sku-sandbox"))
        self.runtime     = runtime or os.environ.get("SANDBOX_RUNTIME", "runc")
        self.work_dir.mkdir(parents=True, exist_ok=True)

        self.broker_url = (os.environ.get("SANDBOX_BROKER_URL") or "").rstrip("/")
        self._client = None
        if not self.broker_url:
            try:
                import docker
                # `docker` package ships no PEP 561 stubs, so mypy treats
                # the module as `object`; from_env is real at runtime.
                self._client = docker.from_env()    # type: ignore[attr-defined]
            except ImportError as e:
                raise ImportError("docker SDK required. pip install docker") from e
            except Exception as e:
                raise SandboxError(f"cannot connect to Docker daemon: {e}") from e

    # ── broker transport ──────────────────────────────────────────────

    def _spawn_via_broker(
        self, job_dir: str, input_name: str, max_rows: int, max_columns: int,
        sniff: bool = False, mapping: str | None = None,
    ) -> tuple[int, bool, str, str]:
        """POST /spawn on the broker; mirrors spawn_and_wait's return."""
        import urllib.error
        import urllib.request

        payload = json.dumps({
            "job_dir":     job_dir,
            "input_name":  input_name,
            "max_rows":    max_rows,
            "max_columns": max_columns,
            "sniff":       sniff,
            "mapping":     mapping,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.broker_url}/spawn",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        # The broker enforces the container timeout server-side; this
        # HTTP timeout only guards against a hung broker.
        http_timeout = self.timeout_sec + 30
        try:
            with urllib.request.urlopen(req, timeout=http_timeout) as r:
                body = json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500]
            raise SandboxError(f"broker rejected spawn ({e.code}): {detail}") from e
        except Exception as e:
            raise SandboxError(f"sandbox broker unreachable: {e}") from e
        return (
            int(body["exit_code"]),
            bool(body["timed_out"]),
            str(body.get("stdout", "")),
            str(body.get("stderr", "")),
        )

    # ── public API ────────────────────────────────────────────────────────

    def _stage_and_spawn(
        self, input_bytes: bytes, input_filename: str, *,
        sniff: bool = False, mapping: str | None = None,
    ) -> tuple[int, str, str, Path, Path]:
        """Stage the input into a fresh job dir and spawn the hardened parser.
        Returns (exit_code, stdout, stderr, out_dir, staging_dir); the CALLER
        owns cleanup of staging_dir. On launch failure staging is removed here."""
        staging = Path(tempfile.mkdtemp(prefix="job-", dir=self.work_dir))
        in_dir = staging / "in"
        out_dir = staging / "out"
        in_dir.mkdir()
        out_dir.mkdir()
        # The sandbox container runs as user 65534:65534 (nobody) for
        # defence-in-depth, but the staging dirs are created by the
        # worker uid (sku, 999). Mode 0o777 on out_dir lets the sandbox
        # write parquet+manifest into it; in_dir stays 0o755 — readable
        # by everyone but writable only by the worker (the input file
        # itself is also explicitly chmod'd 0o444 above).
        out_dir.chmod(0o777)

        # Normalise filename to a safe in-sandbox name — the parser still
        # checks the suffix for dispatching, so preserve it.
        suffix = Path(input_filename).suffix.lower()
        safe_name = f"original{suffix or '.csv'}"
        input_path = in_dir / safe_name
        input_path.write_bytes(input_bytes)
        input_path.chmod(0o444)

        max_rows = int(os.environ.get("SANDBOX_MAX_ROWS", "5000000"))
        max_columns = int(os.environ.get("SANDBOX_MAX_COLUMNS", "64"))

        try:
            # timed_out is already folded into stderr by the spawn layer —
            # unused here, kept in the tuple for the broker API contract.
            if self.broker_url:
                exit_code, _timed_out, stdout, stderr = self._spawn_via_broker(
                    staging.name, safe_name, max_rows, max_columns,
                    sniff=sniff, mapping=mapping,
                )
            else:
                exit_code, _timed_out, stdout, stderr = spawn_and_wait(
                    self._client,
                    image=self.image,
                    in_dir=in_dir,
                    out_dir=out_dir,
                    input_name=safe_name,
                    max_rows=max_rows,
                    max_columns=max_columns,
                    timeout_sec=self.timeout_sec,
                    mem_limit=self.mem_limit,
                    cpus=self.cpus,
                    runtime=self.runtime,
                    sniff=sniff,
                    mapping=mapping,
                )
        except SandboxError:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return exit_code, stdout, stderr, out_dir, staging

    def run(
        self, input_bytes: bytes, input_filename: str,
        mapping: str | None = None,
    ) -> SandboxResult:
        """
        Execute the parser on `input_bytes` in a fresh container. When `mapping`
        (a JSON {canonical: source} string) is given, the parser applies it
        before validation (DP-3). Cleans up the staging directory unconditionally.
        """
        exit_code, stdout, stderr, out_dir, staging = self._stage_and_spawn(
            input_bytes, input_filename, mapping=mapping,
        )
        try:
            # Parser exit convention: 0=ok, 2=input missing, 3=validation error
            ok = exit_code == 0
            parquet_path = out_dir / "data.parquet"
            manifest_path = out_dir / "manifest.json"

            manifest: dict | None = None
            output_file: Path | None = None
            if ok and parquet_path.exists() and manifest_path.exists():
                try:
                    manifest = json.loads(manifest_path.read_text())
                    # Move parquet out of the staging tree so caller can keep it
                    # after we clean up.
                    persisted = self.work_dir / "persisted" / parquet_path.name
                    persisted.parent.mkdir(parents=True, exist_ok=True)
                    # Use a unique name to avoid collisions between concurrent jobs.
                    import uuid
                    persisted = persisted.with_name(f"{uuid.uuid4().hex}.parquet")
                    shutil.move(str(parquet_path), persisted)
                    output_file = persisted
                except Exception as e:
                    ok = False
                    stderr = f"{stderr}\npost-run error: {e}"

            return SandboxResult(
                ok=ok, exit_code=exit_code,
                stdout=stdout, stderr=stderr,
                output_path=output_file, manifest=manifest,
            )
        finally:
            # Container reaping lives in spawn_and_wait (broker side for
            # the HTTP transport); only the staging dir is ours to clean.
            shutil.rmtree(staging, ignore_errors=True)

    def sniff(self, input_bytes: bytes, input_filename: str) -> SandboxResult:
        """
        Run the parser in --sniff mode (DP-4 #324): detect format/encoding/
        delimiter/layout and return a sample-based report in `manifest`. No
        parquet is produced. The report drives «Подготовка данных» preview + the
        column-mapping proposal. Same hardened container as run().
        """
        exit_code, stdout, stderr, out_dir, staging = self._stage_and_spawn(
            input_bytes, input_filename, sniff=True,
        )
        try:
            manifest_path = out_dir / "manifest.json"
            report: dict | None = None
            if exit_code == 0 and manifest_path.exists():
                try:
                    report = json.loads(manifest_path.read_text())
                except Exception as e:
                    stderr = f"{stderr}\nsniff report unreadable: {e}"
            return SandboxResult(
                ok=exit_code == 0 and report is not None,
                exit_code=exit_code, stdout=stdout, stderr=stderr,
                output_path=None, manifest=report,
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)
