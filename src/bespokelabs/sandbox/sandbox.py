from __future__ import annotations

import dataclasses
import json
import re
import typing
from typing import TypeVar, overload

from bespokelabs.sandbox.backends import BACKENDS
from bespokelabs.sandbox.exceptions import (
    BackendNotInstalledError,
    SandboxCreationError,
    SandboxError,
    SandboxExecutionError,
)
from bespokelabs.sandbox.presets import PRESETS, SandboxPreset, get_preset, register_preset
from bespokelabs.sandbox.protocols import SandboxBackend
from bespokelabs.sandbox.types import FileInfo, SandboxConfig, SandboxResult, SnapshotInfo

T = TypeVar("T")


class Sandbox:
    """Unified sandbox interface across Local, Safehouse, Docker, Ray, Daytona,
    Tensorlake, Modal, and E2B.

    Usage:
        with Sandbox("safehouse", timeout_secs=300) as sb:
            result = sb.execute_code('print("hello")')
            print(result.stdout)

        # With a preset:
        with Sandbox("daytona", preset="claude-code") as sb:
            sb.execute_command("claude --version")

        # Or without context manager:
        sb = Sandbox("modal", app_name="my-app")
        sb.execute_command("ls /")
        sb.destroy()
    """

    def __init__(
        self,
        backend: str,
        *,
        preset: str | SandboxPreset | None = None,
        cpu: float | None = None,
        memory_mb: int | None = None,
        disk_mb: int | None = None,
        timeout_secs: int | None = None,
        image: str | None = None,
        env_vars: dict[str, str] | None = None,
        allow_internet: bool | None = None,
        app_name: str | None = None,
        template: str | None = None,
        snapshot_id: str | None = None,
        workdir: str | None = None,
    ) -> None:
        backend = backend.lower().strip()
        if backend not in BACKENDS:
            raise SandboxError(
                f"Unknown backend '{backend}'. Choose from: {', '.join(BACKENDS)}"
            )

        # Resolve preset
        resolved_preset: SandboxPreset | None = None
        if isinstance(preset, str):
            resolved_preset = get_preset(preset)
        elif isinstance(preset, SandboxPreset):
            resolved_preset = preset

        # Merge: explicit kwargs override preset defaults
        self._config = SandboxConfig(
            backend=backend,
            cpu=cpu if cpu is not None else (resolved_preset.cpu if resolved_preset else 1.0),
            memory_mb=memory_mb if memory_mb is not None else (resolved_preset.memory_mb if resolved_preset else 1024),
            disk_mb=disk_mb,
            timeout_secs=timeout_secs if timeout_secs is not None else (resolved_preset.timeout_secs if resolved_preset else 600),
            image=image,
            env_vars={**(resolved_preset.env_vars if resolved_preset else {}), **(env_vars or {})},
            allow_internet=allow_internet if allow_internet is not None else (resolved_preset.allow_internet if resolved_preset else True),
            app_name=app_name,
            template=template,
            snapshot_id=snapshot_id,
            workdir=workdir,
        )
        self._preset = resolved_preset
        self._adapter: SandboxBackend = BACKENDS[backend]()
        self._destroyed = False

        try:
            self._adapter.create(self._config)
        except (SandboxError, BackendNotInstalledError):
            raise
        except Exception as exc:
            raise SandboxCreationError(
                f"Failed to create sandbox on '{backend}': {exc}"
            ) from exc

        # Run preset setup commands; destroy the sandbox if setup fails
        if resolved_preset and resolved_preset.setup_commands:
            try:
                self._run_preset_setup(resolved_preset)
            except Exception:
                self.destroy()
                raise

    # -- Core operations ---------------------------------------------------

    @overload
    def execute_code(self, code: str, language: str = "python") -> SandboxResult: ...

    @overload
    def execute_code(self, code: str, language: str = "python", *, return_type: type[T]) -> T: ...

    def execute_code(self, code: str, language: str = "python", *, return_type: type[T] | None = None) -> SandboxResult | T:
        """Execute a code snippet and return stdout/stderr/exit_code.

        If *return_type* is given, parse stdout as JSON and return an
        instance of that type instead of SandboxResult.  Works with
        dataclasses, Pydantic models, and any class whose ``__init__``
        accepts ``**kwargs``.
        """
        self._check_alive()
        result = self._adapter.execute_code(code, language)
        if return_type is not None:
            return _parse_result(result, return_type)
        return result

    @overload
    def execute_command(self, command: str, args: list[str] | None = None) -> SandboxResult: ...

    @overload
    def execute_command(self, command: str, args: list[str] | None = None, *, return_type: type[T]) -> T: ...

    def execute_command(self, command: str, args: list[str] | None = None, *, return_type: type[T] | None = None) -> SandboxResult | T:
        """Execute a shell command and return stdout/stderr/exit_code.

        If *return_type* is given, parse stdout as JSON and return an
        instance of that type instead of SandboxResult.  Works with
        dataclasses, Pydantic models, and any class whose ``__init__``
        accepts ``**kwargs``.
        """
        self._check_alive()
        result = self._adapter.execute_command(command, args)
        if return_type is not None:
            return _parse_result(result, return_type)
        return result

    # -- File operations ---------------------------------------------------

    def list_files(self, path: str = "/") -> list[FileInfo]:
        """List files and directories at the given path."""
        self._check_alive()
        return self._adapter.list_files(path)

    def read_file(self, path: str) -> bytes:
        """Read file contents as bytes."""
        self._check_alive()
        return self._adapter.read_file(path)

    def write_file(self, path: str, content: bytes | str) -> None:
        """Write content to a file in the sandbox."""
        self._check_alive()
        return self._adapter.write_file(path, content)

    def upload_file(self, local_path: str, remote_path: str) -> None:
        """Upload a local file into the sandbox."""
        self._check_alive()
        self._adapter.upload_file(local_path, remote_path)

    def download_file(self, remote_path: str, local_path: str) -> None:
        """Download a file from the sandbox to a local path."""
        self._check_alive()
        self._adapter.download_file(remote_path, local_path)

    # -- Lifecycle ---------------------------------------------------------

    def snapshot(self) -> SnapshotInfo:
        """Save the current sandbox state. Not all backends support this."""
        self._check_alive()
        return self._adapter.snapshot()

    def destroy(self) -> None:
        """Terminate and clean up the sandbox."""
        if not self._destroyed:
            self._adapter.destroy()
            self._destroyed = True

    # -- Context manager ---------------------------------------------------

    def __enter__(self) -> Sandbox:
        return self

    def __exit__(self, *exc: object) -> None:
        self.destroy()

    # -- Properties --------------------------------------------------------

    @property
    def backend_name(self) -> str:
        return self._config.backend

    @property
    def is_alive(self) -> bool:
        return not self._destroyed

    # -- Presets -----------------------------------------------------------

    @staticmethod
    def list_presets() -> dict[str, SandboxPreset]:
        """Return all registered presets."""
        return dict(PRESETS)

    @staticmethod
    def register_preset(preset: SandboxPreset) -> None:
        """Register a custom preset for use with the preset= parameter."""
        register_preset(preset)

    # -- Internal ----------------------------------------------------------

    def _check_alive(self) -> None:
        if self._destroyed:
            raise SandboxError("Sandbox has been destroyed")

    def _run_preset_setup(self, preset: SandboxPreset) -> None:
        """Run preset setup commands after sandbox creation."""
        for cmd in preset.setup_commands:
            result = self._adapter.execute_command(cmd)
            if result.exit_code != 0:
                raise SandboxCreationError(
                    f"Preset '{preset.name}' setup failed on command: {cmd}\n"
                    f"exit_code={result.exit_code}\nstderr={result.stderr}"
                )


# -- Structured output parsing ------------------------------------------------

# Regex to strip markdown code fences that LLMs often wrap JSON in.
_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)


def _extract_json(text: str) -> dict:
    """Best-effort extraction of a JSON object from *text*.

    Tries, in order:
    1. Direct ``json.loads`` on the stripped text.
    2. Contents of the first markdown code fence.
    3. Substring from the first ``{`` to the last ``}``.
    """
    text = text.strip()

    # 1. Direct parse
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass

    # 2. Markdown fence
    m = _FENCE_RE.search(text)
    if m:
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass

    # 3. First { … last }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass

    raise SandboxExecutionError(
        f"Could not extract JSON object from stdout:\n{text[:500]}"
    )


def _parse_result(result: SandboxResult, return_type: type[T]) -> T:
    """Parse a SandboxResult's stdout into *return_type*."""
    if result.exit_code != 0:
        raise SandboxExecutionError(
            f"Command failed (exit {result.exit_code}) before parsing return_type.\n"
            f"stderr: {result.stderr[:500]}"
        )

    data = _extract_json(result.stdout)

    try:
        # Pydantic v2
        if hasattr(return_type, "model_validate"):
            return return_type.model_validate(data)

        # Dataclass — only pass fields the dataclass declares
        if dataclasses.is_dataclass(return_type):
            field_names = {f.name for f in dataclasses.fields(return_type)}
            filtered = {k: v for k, v in data.items() if k in field_names}
            return return_type(**filtered)

        # Fallback — plain class with **kwargs init
        return return_type(**data)
    except Exception as exc:
        raise SandboxExecutionError(
            f"Failed to construct {return_type.__name__} from parsed JSON: {exc}"
        ) from exc


# -- Schema generation ---------------------------------------------------------


def json_schema(cls: type) -> str:
    """Generate a prompt instruction describing the expected JSON schema.

    Works with Pydantic v2 models (recommended) and dataclasses.
    Append the result to your prompt so the LLM knows what to return::

        prompt = f"Look up {repo}. {json_schema(RepoStats)}"
    """
    # Pydantic v2
    if hasattr(cls, "model_json_schema"):
        schema = cls.model_json_schema()
        return f"Return ONLY a JSON object matching this schema:\n{json.dumps(schema, indent=2)}"

    # Dataclass fallback
    if dataclasses.is_dataclass(cls):
        try:
            hints = typing.get_type_hints(cls)
        except Exception:
            hints = {}
        parts = [f"{f.name}: {hints.get(f.name, f.type)}" for f in dataclasses.fields(cls)]
        return "Return ONLY a JSON object with these fields: " + ", ".join(parts) + "."

    return "Return ONLY a JSON object."
