from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class SandboxConfig:
    """Normalized configuration passed to every backend adapter.

    Not all backends honour every field:
      - cpu / memory_mb: Daytona, Tensorlake, Modal, Docker
      - disk_mb: Daytona only (image-based sandboxes)
      - image: Modal, Daytona (OCI image), Docker (e.g. "python:3.12-slim"),
               Tensorlake (project-scoped image name, e.g. "tensorlake/ubuntu-minimal")
      - template: E2B only
      - app_name: Modal only
      - allow_internet: Tensorlake, Daytona, Docker
      - snapshot_id: Tensorlake, Modal
      - env_vars: all backends
      - timeout_secs: all backends (local and ray use subprocess timeout)
      - workdir: Local, Safehouse (host directory used as the sandbox root),
                 Tensorlake (command working directory; defaults to /tmp)
      - backend_options: provider-specific escape hatch, merged last into the
        backend's underlying create call (Docker containers.run, Modal
        Sandbox.create, E2B Sandbox.create, Tensorlake create_and_connect,
        Daytona create params). Ignored by local/safehouse/ray.
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
    workdir: str | None = None
    backend_options: dict = field(default_factory=dict)


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


@dataclass
class SandboxSessionState:
    """Serializable handle to a *running* sandbox.

    Produced by Sandbox.session_state() and consumed by
    SandboxClient.resume() / Sandbox.resume(), including from another
    process.  ``data`` is a backend-specific JSON-safe payload (e.g. a
    container or sandbox id).  Unlike a snapshot, this does not save
    state — it reattaches to a sandbox that is still alive.
    """

    backend: str
    data: dict

    def to_json(self) -> str:
        return json.dumps({"backend": self.backend, "data": self.data})

    @classmethod
    def from_json(cls, raw: str) -> SandboxSessionState:
        obj = json.loads(raw)
        return cls(backend=obj["backend"], data=obj["data"])
