from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Literal

from bespokelabs.sandbox.backends._prelude import PYTHON_PREAMBLE, is_python_language
from bespokelabs.sandbox.exceptions import SandboxError
from bespokelabs.sandbox.types import FileInfo, SandboxResult

if TYPE_CHECKING:
    from bespokelabs.sandbox.sandbox import Sandbox

AgentPlacement = Literal["inside", "external"]
AgentCapability = Literal["shell", "files", "patch", "ports", "artifacts"]
AgentInputMode = Literal["stdin", "argv", "file", "none"]
AgentRunner = Callable[["AgentContext", str], Any]

_VALID_CAPABILITIES = {"shell", "files", "patch", "ports", "artifacts"}
_VALID_INPUT_MODES = {"stdin", "argv", "file", "none"}
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class AgentSpec:
    """Describe how an agent should use a sandbox.

    ``placement="inside"`` means the agent command runs in the sandbox.
    ``placement="external"`` means an outside runner drives the sandbox through
    an :class:`AgentContext`.
    """

    name: str
    placement: AgentPlacement
    capabilities: list[AgentCapability] = field(default_factory=lambda: ["shell", "files"])
    env: dict[str, str] = field(default_factory=dict)

    # inside-only fields
    command: list[str] | None = None
    cwd: str | None = None
    input_mode: AgentInputMode = "stdin"
    input_path: str = "/tmp/agent-input.txt"

    # external-only fields
    runner: AgentRunner | None = None

    def __post_init__(self) -> None:
        self.capabilities = list(self.capabilities)
        self.env = dict(self.env)
        if self.command is not None:
            self.command = list(self.command)
        self._validate()

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
    ) -> AgentSpec:
        """Create a spec for an agent process that runs inside the sandbox."""
        return cls(
            name=name,
            placement="inside",
            command=command,
            cwd=cwd,
            env=env or {},
            input_mode=input_mode,
            input_path=input_path,
            capabilities=["shell", "files"] if capabilities is None else capabilities,
        )

    @classmethod
    def external(
        cls,
        *,
        name: str,
        capabilities: list[AgentCapability],
        runner: AgentRunner | None = None,
    ) -> AgentSpec:
        """Create a spec for an outside agent that drives sandbox tools."""
        return cls(
            name=name,
            placement="external",
            capabilities=capabilities,
            runner=runner,
        )

    def _validate(self) -> None:
        if not self.name:
            raise ValueError("AgentSpec.name must be non-empty")
        if self.placement not in ("inside", "external"):
            raise ValueError("AgentSpec.placement must be 'inside' or 'external'")
        unknown = set(self.capabilities) - _VALID_CAPABILITIES
        if unknown:
            raise ValueError(f"Unknown agent capabilities: {', '.join(sorted(unknown))}")
        if self.input_mode not in _VALID_INPUT_MODES:
            raise ValueError(f"Unknown agent input_mode: {self.input_mode}")
        if self.placement == "inside":
            if not self.command:
                raise ValueError("Inside agents require a command")
            for key in self.env:
                if not _ENV_NAME_RE.match(key):
                    raise ValueError(f"Invalid environment variable name: {key}")
        elif self.command is not None:
            raise ValueError("External agents cannot declare an inside command")


class AgentContext:
    """Capability-checked sandbox facade for external agents."""

    def __init__(self, sandbox: Sandbox, capabilities: list[AgentCapability] | None = None) -> None:
        self._sandbox = sandbox
        self._capabilities = set(["shell", "files"] if capabilities is None else capabilities)

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
        patch_arg = shlex.quote(f"-p{strip}")
        patch_file = _shell_path(patch_path)
        return self._sandbox.execute_command("bash", ["-lc", f"patch {patch_arg} < {patch_file}"])

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
        if self._spec.placement == "external":
            if self._spec.runner is None:
                raise SandboxError("External agent spec requires a runner to call run()")
            return self._spec.runner(self._context, prompt)
        return self._run_inside(prompt)

    def _run_inside(self, prompt: str) -> SandboxResult:
        command = _prepare_inside_command(self._spec.command or [])
        mode = self._spec.input_mode

        if mode == "file":
            self._sandbox.write_file(self._spec.input_path, prompt)

        needs_shell = bool(self._spec.cwd or self._spec.env or mode == "stdin")
        if not needs_shell:
            if mode == "argv":
                command = [*command, prompt]
            elif mode == "file":
                command = [*command, self._spec.input_path]
            elif mode != "none":
                raise SandboxError(f"Unsupported inside agent input_mode: {mode}")
            return self._sandbox.execute_command(command[0], command[1:] or None)

        script = self._build_shell_script(prompt)
        return self._sandbox.execute_command("bash", ["-lc", script])

    def _build_shell_script(self, prompt: str) -> str:
        command = _prepare_inside_command(self._spec.command or [])
        lines = ["set -e"]
        if self._spec.env:
            for key, value in self._spec.env.items():
                lines.append(f"export {key}={shlex.quote(value)}")
        if self._spec.cwd:
            lines.append(f"cd {_shell_path(self._spec.cwd)}")

        command_line = " ".join(_shell_arg(part, is_command=(idx == 0)) for idx, part in enumerate(command))
        mode = self._spec.input_mode
        if mode == "stdin":
            lines.append(f"printf %s {shlex.quote(prompt)} | {command_line}")
        elif mode == "argv":
            lines.append(f"{command_line} {shlex.quote(prompt)}")
        elif mode == "file":
            lines.append(f"{command_line} {_shell_path(self._spec.input_path)}")
        elif mode == "none":
            lines.append(command_line)
        else:
            raise SandboxError(f"Unsupported inside agent input_mode: {mode}")
        return "\n".join(lines)


def _prepare_inside_command(command: list[str]) -> list[str]:
    """Prepare an inside-agent command without changing the user's spec.

    Local-style backends need the same Python path rebasing used by
    execute_code(). For inline Python commands, inject the preamble into the
    `-c` payload. On container/cloud backends SANDBOX_ROOT is unset, so the
    preamble exits without rebasing.
    """
    command = list(command)
    if len(command) < 3:
        return command
    executable = command[0].rsplit("/", 1)[-1]
    if not is_python_language(executable):
        return command
    try:
        code_index = command.index("-c") + 1
    except ValueError:
        return command
    if code_index >= len(command):
        return command
    command[code_index] = PYTHON_PREAMBLE + command[code_index]
    return command


def _shell_arg(value: str, *, is_command: bool = False) -> str:
    if value.startswith("/") and not is_command:
        return _shell_path(value)
    return shlex.quote(value)


def _shell_path(path: str) -> str:
    if not path.startswith("/"):
        return shlex.quote(path)
    escaped = path.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    return f'"${{SANDBOX_ROOT:-}}{escaped}"'
