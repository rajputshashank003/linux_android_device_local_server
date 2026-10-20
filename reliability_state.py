"""Small, private, bounded state journal for the Termux service supervisor."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
import tempfile
import threading
import time

DEFAULT_STATE_PATH = Path.home() / ".local" / "state" / "myforever" / "reliability.json"
MAX_EVENTS = 100


def _now() -> float:
    return round(time.time(), 3)


class ReliabilityState:
    """Persist only operational metadata; never accept credentials or request bodies."""

    def __init__(self, path: Path | str = DEFAULT_STATE_PATH):
        self.path = Path(path).expanduser()
        self._lock = threading.RLock()
        self._write_error = None
        self._unsafe = False
        self._data = self._load()

    @staticmethod
    def _empty() -> dict:
        return {
            "version": 1, "startedAt": None, "lastSuccessfulHealthCheck": None,
            "lastUnexpectedExit": None, "restartCounts": {"backend": 0, "tunnel": 0},
            "lastSuccessfulRecovery": None,
            "serviceState": {"overall": "unknown", "backend": "unknown", "database": "unknown", "tunnel": "unknown"},
            "managedPids": {"backend": None, "proxy": None, "mdns": None, "tunnel": None},
            "networkState": {"classification": "unverified", "wifi": None, "internet": None,
                             "tunnel": None, "publicEndpoint": None, "checkedAt": None},
            "memoryState": {"availableBytes": None, "pressureAvg10": None, "oomKills": None,
                             "checkedAt": None, "pressure": "unavailable"},
            "processes": {}, "recoveryAttemptsExceeded": [], "events": [],
        }

    def _load(self) -> dict:
        data = self._empty()
        try:
            info = self.path.lstat()
            if (not self.path.is_file() or self.path.is_symlink() or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077):
                self._unsafe = True
                self._write_error = "UnsafeStateFile"
                return data
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and loaded.get("version") == 1:
                data.update({key: loaded[key] for key in data.keys() & loaded.keys()})
                data["events"] = list(data.get("events") or [])[-MAX_EVENTS:]
        except (OSError, ValueError, TypeError):
            pass
        return data

    def snapshot(self) -> dict:
        with self._lock:
            result = json.loads(json.dumps(self._data))
            result["persistenceAvailable"] = self._write_error is None
            result["persistenceError"] = self._write_error
            return result

    def _persist_safely(self) -> None:
        try:
            self._write()
            self._write_error = None
        except OSError as exc:
            # A full or unavailable filesystem must never take down the supervisor.
            self._write_error = type(exc).__name__

    def _write(self) -> None:
        if self._unsafe:
            raise OSError("unsafe state file; refusing to overwrite")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            existing = self.path.lstat()
            if (not self.path.is_file() or self.path.is_symlink() or existing.st_uid != os.getuid()
                    or stat.S_IMODE(existing.st_mode) & 0o077):
                self._unsafe = True
                raise OSError("unsafe state file; refusing to overwrite")
        except FileNotFoundError:
            pass
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        fd, temporary = tempfile.mkstemp(prefix=".reliability-", suffix=".tmp", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(self._data, output, separators=(",", ":"), ensure_ascii=True)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            try:
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def update(self, **fields) -> None:
        allowed = {"startedAt", "lastSuccessfulHealthCheck", "lastUnexpectedExit",
                   "lastSuccessfulRecovery", "serviceState", "networkState", "memoryState",
                   "processes", "managedPids", "recoveryAttemptsExceeded"}
        with self._lock:
            for key, value in fields.items():
                if key not in allowed:
                    continue
                if key == "serviceState" and isinstance(value, dict):
                    current = self._data["serviceState"]
                    current.update({name: item for name, item in value.items() if item != "unchanged"})
                else:
                    self._data[key] = value
            self._persist_safely()

    def update_service_component(self, component: str, status: str) -> None:
        if component not in ("backend", "database", "tunnel"):
            raise ValueError("Unsupported service component")
        with self._lock:
            state = self._data["serviceState"]
            state[component] = status
            if state.get("backend") in ("starting", "recovering", "unhealthy"):
                state["overall"] = "RECOVERING"
            elif state.get("backend") == "stopped":
                state["overall"] = "OFFLINE"
            elif (state.get("backend") != "healthy" or state.get("database") not in ("healthy", "not_applicable")
                  or state.get("tunnel") in ("degraded", "recovering", "stopping")
                  or (self._data.get("networkState") or {}).get("classification") != "healthy"):
                state["overall"] = "DEGRADED"
            else:
                state["overall"] = "HEALTHY"
            self._persist_safely()

    def increment_restart(self, service: str) -> int:
        if service not in ("backend", "tunnel"):
            raise ValueError("Unsupported service name")
        with self._lock:
            counts = self._data["restartCounts"]
            counts[service] = int(counts.get(service, 0)) + 1
            self._persist_safely()
            return counts[service]

    def record_event(self, severity: str, service: str, event: str, message: str,
                     *, timestamp: float | None = None) -> None:
        if severity not in ("INFO", "WARN", "ERROR"):
            severity = "INFO"
        if service not in {"supervisor", "backend", "database", "network", "tunnel", "memory", "boot"}:
            service = "supervisor"
        row = {"at": round(timestamp if timestamp is not None else _now(), 3),
               "severity": severity, "service": service[:16],
               "event": str(event)[:40], "message": str(message)[:180]}
        with self._lock:
            self._data["events"].append(row)
            self._data["events"] = self._data["events"][-MAX_EVENTS:]
            self._persist_safely()
