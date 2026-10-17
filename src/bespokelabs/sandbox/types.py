"""Shared value types for sandbox configuration, results, and usage."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from bespokelabs.sandbox.exceptions import SandboxConfigurationError

if TYPE_CHECKING:
    from bespokelabs.sandbox.workspace import Manifest


@dataclass
class SandboxConfig:
    """Normalized configuration passed to every backend adapter.

    Not all backends honour every field:
      - cpu / memory_mb: Daytona, Tensorlake, Modal, Docker, RunPod
      - gpu: Modal (e.g. ``"L4"`` or ``"H100:2"``), RunPod (exact GPU
        type ID, optionally followed by a count, e.g.
        ``"NVIDIA H100 80GB HBM3:2"``)
      - disk_mb: Daytona and RunPod (container disk)
      - image: Modal, Daytona, RunPod (OCI image), Docker
               (e.g. "python:3.12-slim"),
               Tensorlake (project-scoped image name, e.g. "tensorlake/ubuntu-minimal")
      - template: E2B and RunPod
      - app_name: Modal only
      - allow_internet: Tensorlake, Daytona, Docker
      - snapshot_id: Tensorlake, Modal
      - env_vars: all backends
      - timeout_secs: all backends (local and ray use subprocess timeout)
      - timeout_secs_explicit: whether ``timeout_secs`` was supplied by the
        caller rather than inherited from a preset or from the default above.
        Set by ``Sandbox(...)``; only Daytona reads it, because there
        timeout_secs becomes ttl_minutes -- a hard wall-clock destroy -- and a
        value nobody asked for must not silently cap a sandbox's lifetime.
      - workdir: Local, Safehouse (host directory used as the sandbox root),
                 Tensorlake, Daytona, RunPod (command working directory;
                 Tensorlake defaults to /tmp, RunPod to /workspace)
      - backend_options: provider-specific escape hatch, merged last into the
        backend's underlying create call (Docker containers.run, Modal
        Sandbox.create, E2B Sandbox.create, Tensorlake create_and_connect,
        Daytona create params, RunPod REST create payload). Ignored by
        local/safehouse/ray.
    """

    backend: str
    cpu: float = 1.0
    memory_mb: int = 1024
    disk_mb: int | None = None
    gpu: str | None = None
    timeout_secs: int = 600
    timeout_secs_explicit: bool = False
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
            cache_creation_tokens=self.cache_creation_tokens
            + other.cache_creation_tokens,
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


# -- Jobs ----------------------------------------------------------------------

_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class JobState(str, Enum):
    """Lifecycle state of a job submitted with ``SandboxClient.submit_job``.

    ``succeeded`` and ``failed`` come from the command's own exit code
    (``failed`` includes a command stopped by its ``timeout_secs``).
    ``cancelled`` follows ``cancel_job``.  ``unknown`` is terminal and means
    the outcome can no longer be established -- the sandbox or provider job
    is gone, or the process vanished without recording an exit code.  It is
    never a guess at success; ``JobStatus.reason`` says which case it is.
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


TERMINAL_JOB_STATES = frozenset(
    {
        JobState.SUCCEEDED,
        JobState.FAILED,
        JobState.CANCELLED,
        JobState.UNKNOWN,
    }
)


@dataclass
class JobSpec:
    """A command to run to completion, detached from the submitting process.

    Field names and meanings match :class:`SandboxConfig` and
    ``Sandbox(...)``; the backend notes there apply here too.  Differences:

      - command: an argv list (run without a shell) or a shell string (run
        with ``sh -c``).
      - env_vars: literal values exported for the command only.  Names must
        be valid shell identifiers.
      - timeout_secs: the command's own run-time limit.  On sandbox
        backends the sandbox lives an extra hour so outputs stay readable.
      - labels: provider labels/tags where the backend has them (Daytona
        labels, Docker labels, Modal tags, E2B metadata, Cloud Batch job and
        VM labels); ignored elsewhere.
      - workdir: the command's working directory (created if missing).  On
        the local backend it is the sandbox root, as for ``Sandbox``.
      - files / workspace: seeded into the sandbox before the command
        starts, exactly like ``Sandbox(files=..., workspace=...)``.
        Unsupported on ``gcp_batch``.
      - backend_options: merged into the provider create call on sandbox
        backends; see the README for the ``gcp_batch`` keys.
    """

    command: str | list[str]
    image: str | None = None
    snapshot_id: str | None = None
    template: str | None = None
    env_vars: dict[str, str] = field(default_factory=dict)
    cpu: float = 1.0
    memory_mb: int = 1024
    disk_mb: int | None = None
    gpu: str | None = None
    timeout_secs: int = 3600
    allow_internet: bool = True
    labels: dict[str, str] = field(default_factory=dict)
    workdir: str | None = None
    app_name: str | None = None
    files: dict[str, bytes | str] | None = None
    workspace: Manifest | None = None
    backend_options: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.command, str):
            if not self.command.strip():
                raise SandboxConfigurationError("JobSpec.command is empty")
        elif isinstance(self.command, (list, tuple)):
            if not self.command or not all(
                isinstance(part, str) for part in self.command
            ):
                raise SandboxConfigurationError(
                    "JobSpec.command must be a shell string or a non-empty "
                    "list of strings"
                )
            self.command = list(self.command)
        else:
            raise SandboxConfigurationError(
                "JobSpec.command must be a shell string or a list of strings"
            )
        if self.image and self.snapshot_id:
            raise SandboxConfigurationError(
                "JobSpec takes an image or a snapshot_id, not both"
            )
        for name in ("cpu", "memory_mb", "timeout_secs"):
            value = getattr(self, name)
            if isinstance(value, bool) or not value > 0:
                raise SandboxConfigurationError(
                    f"JobSpec.{name} must be positive, got {value!r}"
                )
        if self.disk_mb is not None and not self.disk_mb > 0:
            raise SandboxConfigurationError(
                f"JobSpec.disk_mb must be positive, got {self.disk_mb!r}"
            )
        for key, value in self.env_vars.items():
            if not isinstance(key, str) or not _ENV_NAME_RE.fullmatch(key):
                raise SandboxConfigurationError(
                    f"JobSpec.env_vars name {key!r} is not a valid variable "
                    "name"
                )
            if not isinstance(value, str):
                raise SandboxConfigurationError(
                    f"JobSpec.env_vars[{key!r}] must be a string"
                )
        for key, value in self.labels.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise SandboxConfigurationError(
                    "JobSpec.labels keys and values must be strings"
                )


@dataclass
class JobStatus:
    """A job's state as last observed from its provider.

    ``exit_code`` is the command's exit code once it is known.
    ``started_at`` / ``finished_at`` are timezone-aware UTC times when the
    provider reports them.  ``reason`` is a short machine-readable cause for
    non-success terminal states (e.g. ``"timeout"``, ``"sandbox_gone"``,
    ``"process_lost"``, ``"job_not_found"``).  ``details`` carries
    provider-specific facts such as the sandbox id or the raw provider state.
    """

    job_id: str
    state: JobState
    exit_code: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def done(self) -> bool:
        """Whether the job reached a terminal state."""
        return self.state in TERMINAL_JOB_STATES
