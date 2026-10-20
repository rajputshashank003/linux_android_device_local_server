"""Detection-only adapter for the candidate Termux PRoot-Distro runtime."""

from __future__ import annotations

import os
import platform
import shutil
import importlib.metadata
import subprocess
from dataclasses import asdict, dataclass
from typing import Any

from .config import DEFAULT_CONFIG


@dataclass
class RuntimeReport:
    name: str
    available: bool
    executable: str | None
    proot_executable: str | None
    version: str | None
    initialized: bool
    architecture: str
    termux: bool
    root: bool
    oci_image_support: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _version() -> str | None:
    """Read the installed package version without invoking a guessed CLI flag."""
    try:
        return importlib.metadata.version("proot-distro")
    except importlib.metadata.PackageNotFoundError:
        dpkg = shutil.which("dpkg-query")
        if not dpkg:
            return None
        try:
            result = subprocess.run(
                [dpkg, "-W", "-f=${Version}", "proot-distro"],
                check=False, capture_output=True, text=True,
                timeout=DEFAULT_CONFIG.probe_timeout_seconds,
                env={"PATH": os.environ.get("PATH", ""), "LANG": "C"},
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        value = result.stdout.strip()
        return value[:160] if result.returncode == 0 and value else None


def detect_runtime() -> RuntimeReport:
    """Detect installed components without installing, pulling, or running images."""
    pd = shutil.which("proot-distro")
    proot = shutil.which("proot")
    architecture = platform.machine().lower()
    is_termux = bool(os.environ.get("TERMUX_VERSION") or os.environ.get("PREFIX", "").startswith("/data/data/com.termux/"))
    is_root = hasattr(os, "geteuid") and os.geteuid() == 0
    binaries_ready = bool(pd and proot)
    initialized = False
    if binaries_ready and pd:
        try:
            help_result = subprocess.run(
                [pd, "--help"], check=False, capture_output=True, text=True,
                timeout=DEFAULT_CONFIG.probe_timeout_seconds,
                env={"PATH": os.environ.get("PATH", ""), "LANG": "C"},
            )
            initialized = help_result.returncode == 0 and bool(help_result.stdout or help_result.stderr)
        except (OSError, subprocess.TimeoutExpired):
            initialized = False
    ready = binaries_ready and initialized
    if ready:
        message = "PRoot-Distro CLI initializes and proot is present; run the OCI smoke test separately to verify an image."
    elif binaries_ready:
        message = "PRoot-Distro and proot are present, but the PRoot-Distro CLI initialization check failed."
    elif pd:
        message = "PRoot-Distro is present but its proot executable is missing."
    else:
        message = "PRoot-Distro is not installed; no container image was downloaded or executed."
    return RuntimeReport(
        name="proot-distro", available=ready, executable=pd,
        proot_executable=proot, version=_version() if pd else None,
        initialized=initialized, architecture=architecture, termux=is_termux, root=is_root,
        oci_image_support="NOT VERIFIED", message=message,
    )


def namespace_probe() -> dict[str, Any]:
    """Check whether an unprivileged user namespace can be created, if unshare exists."""
    unshare = shutil.which("unshare")
    if not unshare:
        return {"status": "NOT SUPPORTED", "message": "unshare executable is unavailable."}
    try:
        result = subprocess.run(
            [unshare, "-Ur", "true"], capture_output=True, text=True,
            timeout=DEFAULT_CONFIG.probe_timeout_seconds,
            env={"PATH": os.environ.get("PATH", ""), "LANG": "C"},
        )
    except subprocess.TimeoutExpired:
        return {"status": "FAIL", "message": "User-namespace probe timed out."}
    except OSError as exc:
        return {"status": "FAIL", "message": f"Could not run fixed unshare probe: {exc}"}
    if result.returncode == 0:
        return {"status": "PASS", "message": "Unprivileged user namespace creation succeeded."}
    detail = (result.stderr or result.stdout).strip().splitlines()
    return {"status": "FAIL", "message": detail[0][:200] if detail else f"unshare exited {result.returncode}"}
