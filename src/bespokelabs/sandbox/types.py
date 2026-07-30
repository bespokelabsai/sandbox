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
      - timeout_secs: all backends (local and ray use subprocess timeout;
                 Daytona maps it to ttl_minutes, rounded up)
      - workdir: Local, Safehouse (host directory used as the sandbox root),
                 Tensorlake, Daytona (command working directory; Tensorlake
                 defaults to /tmp)
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
class Usage:
    """Token and cost usage for an agent run.

    Token counts and ``llm_cost_usd`` come from the agent CLI's own report
    (e.g. Claude Code's ``total_cost_usd`` and ``usage`` block).
    ``compute_cost_usd`` is the SDK's estimate of the sandbox compute the run
    consumed (elapsed wall-clock x the backend's per-second price).

    Instances add together so per-call usages aggregate into a sandbox total
    (see :attr:`bespokelabs.sandbox.Sandbox.usage`)::

        total = run_a.usage + run_b.usage
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    llm_cost_usd: float = 0.0
    compute_cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        """All tokens billed for the run, including cache reads/writes."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_creation_tokens
        )

    @property
    def total_cost_usd(self) -> float:
        """LLM token cost plus estimated sandbox compute cost."""
        return self.llm_cost_usd + self.compute_cost_usd

    def __add__(self, other: object) -> Usage:
        if not isinstance(other, Usage):
            return NotImplemented
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_creation_tokens=self.cache_creation_tokens + other.cache_creation_tokens,
            llm_cost_usd=self.llm_cost_usd + other.llm_cost_usd,
            compute_cost_usd=self.compute_cost_usd + other.compute_cost_usd,
        )


@dataclass
class AgentRunResult:
    """Result of :meth:`bespokelabs.sandbox.Sandbox.run_agent`.

    Carries the agent's text answer (``text``) alongside the token/cost
    :class:`Usage` for the call.  The raw ``stdout``/``stderr``/``exit_code``
    of the underlying CLI invocation are preserved, and ``raw`` holds the
    parsed JSON result record when one was found.
    """

    text: str = ""
    usage: Usage = field(default_factory=Usage)
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    raw: dict | None = None


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
