from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SandboxConfig:
    """Normalized configuration passed to every backend adapter.

    Not all backends honour every field:
      - cpu / memory_mb: Daytona (defaults only), Tensorlake, Modal
      - disk_mb: Daytona only
      - image: Modal (modal.Image name), Daytona (OCI image)
      - template: E2B only
      - app_name: Modal only
      - allow_internet: Tensorlake, Daytona
      - snapshot_id: Tensorlake, Modal
    """

    backend: str
    cpu: float = 1.0
    memory_mb: int = 1024
    disk_mb: int | None = None
    timeout_secs: int = 600
    image: str | None = None
    env_vars: dict[str, str] = field(default_factory=dict)
    allow_internet: bool = True
    app_name: str | None = None
    template: str | None = None
    snapshot_id: str | None = None


@dataclass
class SandboxResult:
    """Normalized execution result returned by every backend."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


@dataclass
class FileInfo:
    """Metadata for a file or directory inside the sandbox."""

    path: str
    is_dir: bool = False
    size: int | None = None


@dataclass
class SnapshotInfo:
    """Reference to a saved sandbox snapshot."""

    snapshot_id: str
    backend: str
    created_at: str | None = None
