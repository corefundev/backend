"""
src/storage/scanner.py

Thin wrapper around ClamAV's `clamd` daemon for streaming virus scans.

We use the INSTREAM protocol (no file path crosses the network) so the
scanner container can be fully isolated from shared volumes. Bytes go in,
verdict comes back.

Protocol reference: https://docs.clamav.net/manual/Usage/Configuration.html#clamd

Env vars
────────
    CLAMAV_HOST           default: clamav
    CLAMAV_PORT           default: 3310
    CLAMAV_SOCKET         optional unix socket path (preferred over TCP if set)
    CLAMAV_TIMEOUT_SEC    default: 60
    CLAMAV_MAX_BYTES      default: 52428800 (50 MiB) — files larger than this
                          are rejected before being sent (clamd stream_max is
                          25 MiB by default; bump StreamMaxLength in clamd.conf
                          if you need larger scans).
"""
from __future__ import annotations

import logging
import os
import socket
import struct
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class ScanVerdict(str, Enum):
    CLEAN    = "clean"
    INFECTED = "infected"
    ERROR    = "error"


@dataclass(frozen=True)
class ScanResult:
    verdict: ScanVerdict
    signature: str | None     # virus name when INFECTED
    raw_response: str         # full clamd line (for logs/audit)


class ScannerError(RuntimeError):
    """Scanner could not produce a verdict — caller must NOT treat as clean."""


# ── Low-level clamd client ────────────────────────────────────────────────────

class ClamdClient:
    """
    Minimal clamd client — just what we need: PING, VERSION, INSTREAM.

    We deliberately don't use the `pyclamd` or `clamd` PyPI packages: both
    are unmaintained (last release in 2020/2018) and the protocol is
    trivial.
    """

    _CHUNK = 16 * 1024

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        socket_path: str | None = None,
        timeout_sec: float | None = None,
    ):
        self.host = host or os.environ.get("CLAMAV_HOST", "clamav")
        self.port = int(port if port is not None else os.environ.get("CLAMAV_PORT", "3310"))
        self.socket_path = socket_path or os.environ.get("CLAMAV_SOCKET")
        self.timeout = float(timeout_sec or os.environ.get("CLAMAV_TIMEOUT_SEC", "60"))

    # ── connection ────────────────────────────────────────────────────────

    def _connect(self) -> socket.socket:
        if self.socket_path:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            sock.connect(self.socket_path)
            return sock
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect((self.host, self.port))
        return sock

    def _command(self, cmd: bytes) -> bytes:
        with self._connect() as sock:
            sock.sendall(b"z" + cmd + b"\0")  # "z" prefix → null-terminated reply
            chunks = []
            while True:
                buf = sock.recv(4096)
                if not buf:
                    break
                chunks.append(buf)
            return b"".join(chunks).rstrip(b"\0")

    # ── public API ────────────────────────────────────────────────────────

    def ping(self) -> bool:
        try:
            return self._command(b"PING").strip() == b"PONG"
        except OSError:
            return False

    def version(self) -> str:
        return self._command(b"VERSION").decode("utf-8", errors="replace").strip()

    def instream(self, data: bytes) -> str:
        """
        Stream `data` to clamd and return the raw response line
        (e.g. "stream: OK" or "stream: Eicar-Test-Signature FOUND").
        """
        with self._connect() as sock:
            sock.sendall(b"zINSTREAM\0")
            # Send in 16 KiB chunks prefixed by a big-endian 4-byte length.
            view = memoryview(data)
            for i in range(0, len(view), self._CHUNK):
                chunk = bytes(view[i : i + self._CHUNK])
                sock.sendall(struct.pack("!I", len(chunk)) + chunk)
            # Zero-length chunk = end of stream.
            sock.sendall(struct.pack("!I", 0))
            resp = b""
            while True:
                buf = sock.recv(4096)
                if not buf:
                    break
                resp += buf
        return resp.rstrip(b"\0").decode("utf-8", errors="replace").strip()


# ── High-level scan() function ────────────────────────────────────────────────

def scan_bytes(data: bytes, client: ClamdClient | None = None) -> ScanResult:
    """
    Scan `data` with clamd. Returns ScanResult.

    Raises ScannerError on transport failure — the caller MUST NOT interpret
    a transport error as "clean". Default-deny is the whole point.
    """
    max_bytes = int(os.environ.get("CLAMAV_MAX_BYTES", str(50 * 1024 * 1024)))
    if len(data) > max_bytes:
        return ScanResult(
            verdict=ScanVerdict.ERROR,
            signature=None,
            raw_response=f"file too large: {len(data)} > {max_bytes}",
        )

    client = client or ClamdClient()
    try:
        raw = client.instream(data)
    except (OSError, socket.timeout) as e:
        raise ScannerError(f"clamd transport failed: {e}") from e

    # Expected responses (clamd docs):
    #   "stream: OK"
    #   "stream: <SIGNATURE> FOUND"
    #   "stream: <REASON> ERROR"
    if raw.endswith("OK"):
        return ScanResult(ScanVerdict.CLEAN, None, raw)
    if raw.endswith("FOUND"):
        # "stream: Eicar-Test-Signature FOUND" → signature = "Eicar-Test-Signature"
        body = raw.removeprefix("stream:").strip().removesuffix("FOUND").strip()
        return ScanResult(ScanVerdict.INFECTED, body or "unknown", raw)
    if "ERROR" in raw:
        return ScanResult(ScanVerdict.ERROR, None, raw)

    # Unexpected line — treat as ERROR, not CLEAN.
    return ScanResult(ScanVerdict.ERROR, None, raw)
