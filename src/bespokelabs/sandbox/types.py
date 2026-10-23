"""Shared value types for sandbox configuration, results, and usage."""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass, field
from typing import Literal

from bespokelabs.sandbox.exceptions import SandboxConfigurationError

NetworkMode = Literal["public", "none", "allowlist"]


def _as_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    return tuple(value)  # type: ignore[arg-type]


@dataclass(frozen=True)
class NetworkPolicy:
    """Outbound network policy for a sandbox.

    ``public`` allows all egress, ``none`` blocks it, and ``allowlist``
    allows only ``domains`` and ``cidrs``.  Pass it as ``network=`` at
    creation or to ``Sandbox.set_network()``.  A backend that cannot enforce
    the requested policy raises instead of ignoring it.
    """

    mode: NetworkMode = "public"
    domains: tuple[str, ...] = ()
    cidrs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "domains", _as_tuple(self.domains))
        object.__setattr__(self, "cidrs", _as_tuple(self.cidrs))
        if self.mode not in ("public", "none", "allowlist"):
            raise SandboxConfigurationError(
                f"Unknown network mode {self.mode!r}; use 'public', 'none' "
                "or 'allowlist'"
            )
        if self.mode == "allowlist":
            if not self.domains and not self.cidrs:
                raise SandboxConfigurationError(
                    "An allowlist policy needs at least one domain or CIDR"
                )
        elif self.domains or self.cidrs:
            raise SandboxConfigurationError(
                "domains and cidrs apply only to mode='allowlist'"
            )
        for cidr in self.cidrs:
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError as exc:
                raise SandboxConfigurationError(
                    f"Invalid CIDR {cidr!r}: {exc}"
                ) from exc


@dataclass
class SandboxConfig:
    """Normalized configuration passed to every backend adapter.

    Not all backends honour every field:
      - cpu / memory_mb: Daytona, Tensorlake, Modal, Docker, RunPod, boxd
        (rounded up to the smallest boxd machine size)
      - gpu: Modal (e.g. ``"L4"`` or ``"H100:2"``), RunPod (exact GPU
        type ID, optionally followed by a count, e.g.
        ``"NVIDIA H100 80GB HBM3:2"``)
      - disk_mb: Daytona and RunPod (container disk)
      - image: Modal, Daytona, RunPod (OCI image), Docker
               (e.g. "python:3.12-slim"),
               Tensorlake (project-scoped image name, e.g. "tensorlake/ubuntu-minimal")
      - dockerfile / context_dir: Docker (local build), Daytona (declarative
        build), Modal (``Image.from_dockerfile``).  Local paths; the context
        defaults to the Dockerfile's directory.  Mutually exclusive with
        ``image`` and ``snapshot_id``.
      - template: E2B and RunPod
      - app_name: Modal only
      - allow_internet: legacy best-effort flag, mapped by Docker,
        Tensorlake and boxd, rejected when false on RunPod, ignored
        elsewhere.  Prefer ``network``.  When ``network`` is set this is
        derived from it.
      - network: enforced egress policy (``NetworkPolicy``). Daytona,
        Modal and boxd support every mode, Docker and Tensorlake ``public``
        and ``none``; anything a backend cannot enforce raises.
      - snapshot_id: Tensorlake, Modal, boxd
      - env_vars: all backends
      - timeout_secs: all backends (local and ray use subprocess timeout)
      - timeout_secs_explicit: whether ``timeout_secs`` was supplied by the
        caller rather than inherited from a preset or from the default above.
        Set by ``Sandbox(...)``; only Daytona and boxd read it, because
        there timeout_secs becomes a hard wall-clock destroy (ttl_minutes,
        delete_after) and a value nobody asked for must not silently cap a
        sandbox's lifetime.
      - workdir: Local, Safehouse (host directory used as the sandbox root),
                 Tensorlake, Daytona, RunPod, boxd (command working directory;
                 Tensorlake defaults to /tmp, RunPod to /workspace, boxd to
                 /home/boxd)
      - backend_options: provider-specific escape hatch, merged last into the
        backend's underlying create call (Docker containers.run, Modal
        Sandbox.create, E2B Sandbox.create, Tensorlake create_and_connect,
        Daytona create params, RunPod REST create payload, boxd
        machines.create). Ignored by local/safehouse/ray.
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
    # New fields go last so positional construction keeps working.
    dockerfile: str | None = None
    context_dir: str | None = None
    network: NetworkPolicy | None = None


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
class ExposedPort:
    """A URL that reaches a port inside the sandbox from outside it.

    Send ``headers`` with every request. Where the provider authenticates the
    URL they carry its access token, so treat them as a secret; they are left
    out of ``repr``.
    """

    port: int
    url: str
    headers: dict[str, str] = field(default_factory=dict, repr=False)


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
