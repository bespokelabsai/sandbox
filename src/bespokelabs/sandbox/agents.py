from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Literal

from bespokelabs.sandbox._agent_runtime import (
    build_inside_shell_script,
    build_patch_apply_command,
    normalize_sandbox_path,
    prepare_inside_command,
)
from bespokelabs.sandbox.exceptions import SandboxError
from bespokelabs.sandbox.types import FileInfo, SandboxResult

if TYPE_CHECKING:
    from bespokelabs.sandbox.sandbox import Sandbox

AgentPlacement = Literal["inside", "external"]
AgentCapability = Literal["shell", "files", "patch", "ports", "artifacts"]
AgentInputMode = Literal["stdin", "argv", "file", "none"]
AgentRunner = Callable[["AgentContext", str], Any]

DEFAULT_AGENT_CAPABILITIES: list[AgentCapability] = ["shell", "files"]
_VALID_CAPABILITIES: set[AgentCapability] = {"shell", "files", "patch", "ports", "artifacts"}
_VALID_INPUT_MODES = {"stdin", "argv", "file", "none"}
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class AgentSpec:
    """Factory namespace for concrete agent specs.

    Use :meth:`inside` for an agent process that runs in the sandbox and
    :meth:`external` for an outside runner that drives sandbox tools.
    """

    name: str
    placement: AgentPlacement
    capabilities: list[AgentCapability]

    @classmethod
    def inside(
        cls,
        *,
        name: str,
        command: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        input_mode: AgentInputMode = "stdin",
        input_path: str = "/tmp/agent-input.txt",
        capabilities: list[AgentCapability] | None = None,
    ) -> InsideAgentSpec:
        """Create a spec for an agent process that runs inside the sandbox."""
        return InsideAgentSpec(
            name=name,
            command=command,
            cwd=cwd,
            env=env or {},
            input_mode=input_mode,
            input_path=input_path,
            capabilities=DEFAULT_AGENT_CAPABILITIES if capabilities is None else capabilities,
        )

    @classmethod
    def external(
        cls,
        *,
        name: str,
        capabilities: list[AgentCapability],
        runner: AgentRunner | None = None,
    ) -> ExternalAgentSpec:
        """Create a spec for an outside agent that drives sandbox tools."""
        return ExternalAgentSpec(
            name=name,
            capabilities=capabilities,
            runner=runner,
        )

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("Use AgentSpec.inside(...) or AgentSpec.external(...)")


@dataclass
class InsideAgentSpec(AgentSpec):
    """Agent spec for a process launched inside the sandbox."""

    name: str
    command: list[str]
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    input_mode: AgentInputMode = "stdin"
    input_path: str = "/tmp/agent-input.txt"
    capabilities: list[AgentCapability] = field(default_factory=lambda: list(DEFAULT_AGENT_CAPABILITIES))
    placement: Literal["inside"] = field(default="inside", init=False)

    def __post_init__(self) -> None:
        _validate_name(self.name)
        self.command = list(self.command)
        if not self.command:
            raise ValueError("Inside agents require a command")
        self.env = dict(self.env)
        self.capabilities = _normalize_capabilities(self.capabilities)
        if self.input_mode not in _VALID_INPUT_MODES:
            raise ValueError(f"Unknown agent input_mode: {self.input_mode}")
        for key in self.env:
            if not _ENV_NAME_RE.match(key):
                raise ValueError(f"Invalid environment variable name: {key}")


@dataclass
class ExternalAgentSpec(AgentSpec):
    """Agent spec for an outside runner that drives sandbox tools."""

    name: str
    capabilities: list[AgentCapability]
    runner: AgentRunner | None = None
    placement: Literal["external"] = field(default="external", init=False)

    def __post_init__(self) -> None:
        _validate_name(self.name)
        self.capabilities = _normalize_capabilities(self.capabilities)


def _validate_name(name: str) -> None:
    if not name:
        raise ValueError("AgentSpec.name must be non-empty")


def _normalize_capabilities(capabilities: list[AgentCapability]) -> list[AgentCapability]:
    normalized = list(capabilities)
    unknown = set(normalized) - _VALID_CAPABILITIES
    if unknown:
        raise ValueError(f"Unknown agent capabilities: {', '.join(sorted(unknown))}")
    return normalized


class AgentContext:
    """Capability-checked sandbox facade for external agents."""

    def __init__(self, sandbox: Sandbox, capabilities: list[AgentCapability] | None = None) -> None:
        self._sandbox = sandbox
        self._capabilities = set(
            _normalize_capabilities(DEFAULT_AGENT_CAPABILITIES if capabilities is None else capabilities)
        )

    @property
    def sandbox(self) -> Sandbox:
        return self._sandbox

    @property
    def capabilities(self) -> set[AgentCapability]:
        return set(self._capabilities)

    def shell(self, command: str, args: list[str] | None = None) -> SandboxResult:
        self._require("shell")
        return self._sandbox.execute_command(command, args)

    def list_files(self, path: str = "/") -> list[FileInfo]:
        self._require("files")
        return self._sandbox.list_files(path)

    def read_file(self, path: str) -> bytes:
        self._require("files")
        return self._sandbox.read_file(path)

    def write_file(self, path: str, content: bytes | str) -> None:
        self._require("files")
        self._sandbox.write_file(path, content)

    def apply_patch(self, patch: str, *, strip: int = 0, patch_path: str = "/tmp/agent.patch") -> SandboxResult:
        self._require("patch")
        self._sandbox.write_file(patch_path, patch)
        return self._sandbox.execute_command(
            "bash",
            ["-c", build_patch_apply_command(patch_path=patch_path, strip=strip)],
        )

    def _require(self, capability: AgentCapability) -> None:
        if capability not in self._capabilities:
            raise SandboxError(f"Agent context does not allow '{capability}' capability")


class AgentSession:
    """Runnable view of an agent bound to a sandbox."""

    def __init__(self, sandbox: Sandbox, spec: AgentSpec) -> None:
        self._sandbox = sandbox
        self._spec = spec
        self._context = AgentContext(sandbox, spec.capabilities)

    @property
    def spec(self) -> AgentSpec:
        return self._spec

    @property
    def context(self) -> AgentContext:
        return self._context

    def run(self, prompt: str) -> Any:
        if isinstance(self._spec, ExternalAgentSpec):
            if self._spec.runner is None:
                raise SandboxError("External agent spec requires a runner to call run()")
            return self._spec.runner(self._context, prompt)
        if isinstance(self._spec, InsideAgentSpec):
            return self._run_inside(prompt, self._spec)
        raise SandboxError(f"Unknown agent spec type: {type(self._spec).__name__}")

    def _run_inside(self, prompt: str, spec: InsideAgentSpec) -> SandboxResult:
        command = prepare_inside_command(spec.command)
        mode = spec.input_mode
        input_path = normalize_sandbox_path(spec.input_path)

        if mode == "file":
            self._sandbox.write_file(input_path, prompt)

        needs_shell = bool(spec.cwd or spec.env or mode == "stdin")
        if not needs_shell:
            if mode == "argv":
                command = [*command, prompt]
            elif mode == "file":
                command = [*command, input_path]
            elif mode != "none":
                raise SandboxError(f"Unsupported inside agent input_mode: {mode}")
            return self._sandbox.execute_command(command[0], command[1:] or None)

        script = build_inside_shell_script(
            command=spec.command,
            input_mode=mode,
            prompt=prompt,
            cwd=spec.cwd,
            env=spec.env,
            input_path=input_path,
        )
        return self._sandbox.execute_command("bash", ["-c", script])
