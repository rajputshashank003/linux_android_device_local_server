"""Small, conservative defaults for local execution-engine diagnostics."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DiagnosticConfig:
    """Resource bounds used by local probes; no deployment limits are implied."""

    probe_timeout_seconds: float = 5.0
    http_timeout_seconds: float = 3.0
    output_limit_bytes: int = 16 * 1024
    temporary_prefix: str = "myforever-exec-check-"

    @property
    def state_directory(self) -> Path:
        return Path.home() / ".local" / "share" / "myforever" / "execution-engine"


DEFAULT_CONFIG = DiagnosticConfig()
