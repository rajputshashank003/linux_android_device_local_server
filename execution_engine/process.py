"""Bounded process probes; no user-provided commands are accepted."""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass

from .config import DEFAULT_CONFIG


@dataclass
class ProbeResult:
    status: str
    returncode: int | None
    stdout: str
    stderr: str
    elapsed_seconds: float
    timed_out: bool = False


def run_output_probe() -> ProbeResult:
    """Run a fixed Python snippet and bound its time and captured output."""
    source = "import sys; print('MYFOREVER_EXEC_ENGINE_PROBE_OK'); sys.exit(0)"
    start = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, "-c", source], check=False, capture_output=True,
            text=True, timeout=DEFAULT_CONFIG.probe_timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return ProbeResult("FAIL", None, _decode(exc.stdout), _decode(exc.stderr), time.monotonic() - start, True)
    except OSError as exc:
        return ProbeResult("FAIL", None, "", str(exc)[:DEFAULT_CONFIG.output_limit_bytes], time.monotonic() - start)
    stdout = result.stdout[:DEFAULT_CONFIG.output_limit_bytes]
    stderr = result.stderr[:DEFAULT_CONFIG.output_limit_bytes]
    ok = result.returncode == 0 and "MYFOREVER_EXEC_ENGINE_PROBE_OK" in stdout
    return ProbeResult("PASS" if ok else "FAIL", result.returncode, stdout, stderr, time.monotonic() - start)


def _decode(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return value[:DEFAULT_CONFIG.output_limit_bytes]


class ManagedProbe:
    """A narrowly scoped lifecycle probe using only a fixed Python sleeper."""

    def __init__(self) -> None:
        self._process: subprocess.Popen[str] | None = None

    def start(self) -> int:
        if self.status() == "running":
            raise RuntimeError("probe is already running")
        source = "import time; time.sleep(30)"
        self._process = subprocess.Popen(
            [sys.executable, "-c", source], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            text=True, close_fds=True,
        )
        return self._process.pid

    def status(self) -> str:
        if self._process is None:
            return "stopped"
        return "running" if self._process.poll() is None else "exited"

    def stop(self) -> int | None:
        process = self._process
        if process is None:
            return None
        if process.poll() is None:
            process.terminate()
            try:
                returncode = process.wait(timeout=DEFAULT_CONFIG.probe_timeout_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
                returncode = process.wait(timeout=DEFAULT_CONFIG.probe_timeout_seconds)
        else:
            returncode = process.returncode
        return returncode
