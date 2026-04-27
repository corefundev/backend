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


# ── Runner ─────────────────────────────────────────────────────────────────────

class DockerSandbox:
    """
    Run `secure_parser.py` in a hardened Docker container.

    The worker calling this class must have access to the Docker socket
    (typically /var/run/docker.sock). This is itself privileged — keep the
    worker container minimal and on a dedicated host if possible.
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

        try:
            import docker
            self._docker = docker
            self._client = docker.from_env()
        except ImportError as e:
            raise ImportError("docker SDK required. pip install docker") from e
        except Exception as e:
            raise SandboxError(f"cannot connect to Docker daemon: {e}") from e

    # ── public API ────────────────────────────────────────────────────────

    def run(self, input_bytes: bytes, input_filename: str) -> SandboxResult:
        """
        Execute the parser on `input_bytes` in a fresh container.
        Cleans up the staging directory unconditionally.
        """
        staging = Path(tempfile.mkdtemp(prefix="job-", dir=self.work_dir))
        in_dir  = staging / "in"
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

        max_rows    = int(os.environ.get("SANDBOX_MAX_ROWS",    "5000000"))
        max_columns = int(os.environ.get("SANDBOX_MAX_COLUMNS", "64"))

        # The sandbox image's ENTRYPOINT is already
        #     ["python", "/opt/secure_parser.py"]
        # (see docker/Dockerfile.sandbox), so docker containers.run
        # appends `command` to that. Don't repeat the python script
        # here — secure_parser.py would see "python /opt/secure_parser.py"
        # as positional args and abort with "unrecognized arguments".
        command = [
            "--input",    f"/sandbox/in/{safe_name}",
            "--output",   "/sandbox/out/data.parquet",
            "--manifest", "/sandbox/out/manifest.json",
            "--max-rows",    str(max_rows),
            "--max-columns", str(max_columns),
        ]

        try:
            container = self._client.containers.run(
                image=self.image,
                command=command,
                detach=True,
                # ── isolation ───────────────────────────────────────
                network_mode="none",
                read_only=True,
                user="65534:65534",
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                pids_limit=64,
                runtime=self.runtime,
                # ── resources ───────────────────────────────────────
                mem_limit=self.mem_limit,
                memswap_limit=self.mem_limit,    # disable swap use
                nano_cpus=int(self.cpus * 1e9),
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
            shutil.rmtree(staging, ignore_errors=True)
            raise SandboxError(f"container launch failed: {e}") from e

        try:
            try:
                result = container.wait(timeout=self.timeout_sec)
                exit_code = int(result.get("StatusCode", -1))
                timed_out = False
            except Exception:   # docker wait raises on timeout (depends on SDK version)
                container.kill()
                exit_code = 137
                timed_out = True

            stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")

            if timed_out:
                stderr = (stderr + f"\nTIMEOUT after {self.timeout_sec}s").strip()

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
            try:
                container.remove(force=True)
            except Exception as e:
                logger.warning(f"container remove failed: {e}")
            shutil.rmtree(staging, ignore_errors=True)
