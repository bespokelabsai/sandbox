from __future__ import annotations

import dataclasses
import json
import re
import shlex
import typing
from typing import TypeVar, overload

from bespokelabs.sandbox.agents import AgentCapability, AgentContext, AgentSession, AgentSpec
from bespokelabs.sandbox.backends import BACKENDS
from bespokelabs.sandbox.exceptions import (
    BackendNotInstalledError,
    SandboxCreationError,
    SandboxError,
    SandboxExecutionError,
)
from bespokelabs.sandbox.presets import PRESETS, SandboxPreset, get_preset, register_preset
from bespokelabs.sandbox.protocols import SandboxBackendClient, SandboxBackendSession
from bespokelabs.sandbox.types import (
    FileInfo,
    SandboxConfig,
    SandboxResult,
    SandboxSessionState,
    SnapshotInfo,
)

T = TypeVar("T")


class Sandbox:
    """A live sandbox session with a unified interface across Local, Safehouse,
    Docker, Ray, Daytona, Tensorlake, Modal, and E2B.

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

    When creating many sandboxes on one backend, build a SandboxClient
    once and call client.create(...) instead — provider-level connections
    are then reused across sandboxes.
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
        backend_options: dict | None = None,
        files: dict[str, bytes | str] | None = None,
        git_repo: str | None = None,
        git_ref: str | None = None,
        _backend_client: SandboxBackendClient | None = None,
    ) -> None:
        # _backend_client is internal: SandboxClient.create() passes its
        # cached per-provider client here so connections are reused.
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

        # Pick the preset image for this backend: tensorlake uses a
        # project-scoped name (tensorlake_image), everything else uses
        # the OCI `image` field.
        preset_image_for_backend = None
        if resolved_preset:
            preset_image_for_backend = (
                resolved_preset.tensorlake_image
                if backend == "tensorlake"
                else resolved_preset.image
            )

        # Merge: explicit kwargs override preset defaults.  Without a
        # preset, an empty SandboxPreset supplies the standard defaults.
        defaults = resolved_preset if resolved_preset is not None else SandboxPreset(name="", description="")
        self._config = SandboxConfig(
            backend=backend,
            cpu=cpu if cpu is not None else defaults.cpu,
            memory_mb=memory_mb if memory_mb is not None else defaults.memory_mb,
            disk_mb=disk_mb,
            timeout_secs=timeout_secs if timeout_secs is not None else defaults.timeout_secs,
            image=image if image is not None else preset_image_for_backend,
            env_vars={**defaults.env_vars, **(env_vars or {})},
            allow_internet=allow_internet if allow_internet is not None else defaults.allow_internet,
            app_name=app_name,
            template=template,
            snapshot_id=snapshot_id,
            workdir=workdir,
            backend_options=backend_options or {},
        )
        self._preset = resolved_preset
        backend_client = _backend_client if _backend_client is not None else BACKENDS[backend]()
        self._destroyed = False

        try:
            self._session: SandboxBackendSession = backend_client.create(self._config)
        except (SandboxError, BackendNotInstalledError):
            raise
        except Exception as exc:
            raise SandboxCreationError(
                f"Failed to create sandbox on '{backend}': {exc}"
            ) from exc

        # Skip setup commands if the preset image was used (everything
        # is already baked into the image).  Fall back to setup_commands
        # for backends that don't support images (local, ray, etc.).
        _IMAGE_BACKENDS = {"docker", "daytona", "modal", "tensorlake"}
        using_preset_image = (
            preset_image_for_backend is not None
            and self._config.image == preset_image_for_backend
            and backend in _IMAGE_BACKENDS
        )
        # Materialize the workspace, then run setup. The repo is cloned
        # first so files= can overlay onto it, and setup commands can
        # rely on both being present. Any failure destroys the sandbox.
        try:
            if git_repo:
                self._clone_repo(git_repo, git_ref)
            if files:
                for path, content in files.items():
                    self._session.write_file(path, content)
            if resolved_preset and resolved_preset.setup_commands and not using_preset_image:
                self._run_preset_setup(resolved_preset)
        except Exception:
            self.destroy()
            raise

    # -- Core operations ---------------------------------------------------

    @overload
    def execute_code(self, code: str, language: str = "python") -> SandboxResult: ...

    @overload
    def execute_code(self, code: str, language: str = "python", *, return_type: type[T]) -> T: ...

    def execute_code(
        self, code: str, language: str = "python", *, return_type: type[T] | None = None
    ) -> SandboxResult | T:
        """Execute a code snippet and return stdout/stderr/exit_code.

        If *return_type* is given, parse stdout as JSON and return an
        instance of that type instead of SandboxResult.  Works with
        dataclasses, Pydantic models, and any class whose ``__init__``
        accepts ``**kwargs``.
        """
        self._check_alive()
        result = self._session.execute_code(code, language)
        if return_type is not None:
            return _parse_result(result, return_type)
        return result

    @overload
    def execute_command(self, command: str, args: list[str] | None = None) -> SandboxResult: ...

    @overload
    def execute_command(
        self, command: str, args: list[str] | None = None, *, return_type: type[T], inject_schema: bool = ...
    ) -> T: ...

    def execute_command(
        self,
        command: str,
        args: list[str] | None = None,
        *,
        return_type: type[T] | None = None,
        inject_schema: bool = False,
    ) -> SandboxResult | T:
        """Execute a shell command and return stdout/stderr/exit_code.

        If *return_type* is given, stdout is parsed into an instance of
        that type.  Set ``inject_schema=True`` to automatically append
        the JSON schema to the last argument (useful when the last arg
        is an LLM prompt).
        """
        self._check_alive()
        if return_type is not None and inject_schema and args:
            args = list(args)
            args[-1] = args[-1] + " " + json_schema(return_type)
        result = self._session.execute_command(command, args)
        if return_type is not None:
            return _parse_result(result, return_type)
        return result

    @staticmethod
    def parse_as(text: str, return_type: type[T]) -> T:
        """Parse a string (e.g. file contents) as JSON into *return_type*.

        Useful when a tool writes output to a file rather than stdout::

            sb.execute_command("codex", args=["exec", ..., "-o", "/tmp/out.txt"])
            raw = sb.read_file("/tmp/out.txt").decode()
            stats = Sandbox.parse_as(raw, RepoStats)
        """
        result = SandboxResult(stdout=text, stderr="", exit_code=0)
        return _parse_result(result, return_type)

    # -- File operations ---------------------------------------------------

    def list_files(self, path: str = "/") -> list[FileInfo]:
        """List files and directories at the given path."""
        self._check_alive()
        return self._session.list_files(path)

    def read_file(self, path: str) -> bytes:
        """Read file contents as bytes."""
        self._check_alive()
        return self._session.read_file(path)

    def write_file(self, path: str, content: bytes | str) -> None:
        """Write content to a file in the sandbox."""
        self._check_alive()
        return self._session.write_file(path, content)

    def upload_file(self, local_path: str, remote_path: str) -> None:
        """Upload a local file into the sandbox."""
        self._check_alive()
        self._session.upload_file(local_path, remote_path)

    def download_file(self, remote_path: str, local_path: str) -> None:
        """Download a file from the sandbox to a local path."""
        self._check_alive()
        self._session.download_file(remote_path, local_path)

    # -- Lifecycle ---------------------------------------------------------

    def snapshot(self) -> SnapshotInfo:
        """Save the current sandbox state. Not all backends support this."""
        self._check_alive()
        return self._session.snapshot()

    def session_state(self) -> SandboxSessionState:
        """Serialize a reattachable handle to this *running* sandbox.

        The result is JSON-safe (see ``to_json()``/``from_json()``) and
        can be passed to ``SandboxClient.resume()`` / ``Sandbox.resume()``
        from another process while the sandbox is still alive.  Raises
        FeatureNotSupportedError on backends without reattach support
        (e.g. ray).
        """
        self._check_alive()
        return SandboxSessionState(
            backend=self._config.backend,
            data=self._session.session_state(),
        )

    # -- Agent helpers -----------------------------------------------------

    def agent(self, spec: AgentSpec) -> AgentSession:
        """Bind an agent spec to this sandbox.

        The returned AgentSession is additive: the Sandbox remains the core
        object for command execution, files, snapshots, and lifecycle.
        """
        self._check_alive()
        return AgentSession(self, spec)

    def agent_tools(self, capabilities: list[AgentCapability] | None = None) -> AgentContext:
        """Return a capability-checked context for an external agent."""
        self._check_alive()
        return AgentContext(self, capabilities)

    @classmethod
    def resume(cls, state: SandboxSessionState) -> Sandbox:
        """Reattach to a running sandbox from serialized session state."""
        return SandboxClient(state.backend).resume(state)

    def destroy(self) -> None:
        """Terminate and clean up the sandbox."""
        if not self._destroyed:
            self._session.destroy()
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
        setup_commands = preset.backend_setup_commands.get(
            self._config.backend,
            preset.setup_commands,
        )
        for cmd in setup_commands:
            result = self._session.execute_command(cmd)
            if result.exit_code != 0:
                raise SandboxCreationError(
                    f"Preset '{preset.name}' setup failed on command: {cmd}\n"
                    f"exit_code={result.exit_code}\nstderr={result.stderr}"
                )

    def _clone_repo(self, repo: str, ref: str | None) -> None:
        """Clone *repo* into the sandbox working directory.

        Requires git inside the sandbox image. The destination is the
        repository name; *ref* may be a branch or tag.
        """
        dest = repo.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1]
        cmd = "git clone --depth 1 "
        if ref:
            cmd += f"--branch {shlex.quote(ref)} "
        cmd += f"{shlex.quote(repo)} {shlex.quote(dest)}"
        result = self._session.execute_command(cmd, None)
        if result.exit_code != 0:
            raise SandboxCreationError(
                f"git clone of '{repo}' failed (exit {result.exit_code}):\n{result.stderr[:500]}"
            )

    @classmethod
    def _from_session(
        cls,
        backend: str,
        session: SandboxBackendSession,
        config: SandboxConfig,
    ) -> Sandbox:
        """Wrap an existing live backend session (used by resume)."""
        sb = object.__new__(cls)
        sb._config = config
        sb._preset = None
        sb._session = session
        sb._destroyed = False
        return sb

    @classmethod
    def _resume_with(
        cls,
        backend_name: str,
        backend_client: SandboxBackendClient,
        state: SandboxSessionState,
    ) -> Sandbox:
        """Shared resume implementation for the sync and async clients."""
        if state.backend != backend_name:
            raise SandboxError(
                f"Session state is for backend '{state.backend}', not '{backend_name}'"
            )
        try:
            session = backend_client.resume(state.data)
        except (SandboxError, BackendNotInstalledError):
            raise
        except Exception as exc:
            raise SandboxCreationError(
                f"Failed to resume sandbox on '{backend_name}': {exc}"
            ) from exc
        return cls._from_session(backend_name, session, SandboxConfig(backend=backend_name))


class SandboxClient:
    """Reusable factory for sandboxes on a single backend.

    Construction validates the backend and verifies its SDK is installed,
    without any network I/O.  Provider-level state (the Docker daemon
    connection, Daytona auth, the Ray runtime) is established on first
    create() and reused across calls, so one client can cheaply launch
    many sandboxes:

        client = SandboxClient("docker")
        for task in tasks:
            with client.create(image="python:3.12-slim") as sb:
                sb.execute_code(task)

    ``Sandbox(backend, ...)`` remains the one-step shorthand for
    ``SandboxClient(backend).create(...)``.
    """

    def __init__(self, backend: str) -> None:
        backend = backend.lower().strip()
        if backend not in BACKENDS:
            raise SandboxError(
                f"Unknown backend '{backend}'. Choose from: {', '.join(BACKENDS)}"
            )
        self._backend_name = backend
        self._backend_client: SandboxBackendClient = BACKENDS[backend]()

    @property
    def backend_name(self) -> str:
        return self._backend_name

    def create(
        self,
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
        backend_options: dict | None = None,
        files: dict[str, bytes | str] | None = None,
        git_repo: str | None = None,
        git_ref: str | None = None,
    ) -> Sandbox:
        """Create a new sandbox session.

        Accepts the same keyword arguments as ``Sandbox(...)``.
        """
        return Sandbox(
            self._backend_name,
            preset=preset,
            cpu=cpu,
            memory_mb=memory_mb,
            disk_mb=disk_mb,
            timeout_secs=timeout_secs,
            image=image,
            env_vars=env_vars,
            allow_internet=allow_internet,
            app_name=app_name,
            template=template,
            snapshot_id=snapshot_id,
            workdir=workdir,
            backend_options=backend_options,
            files=files,
            git_repo=git_repo,
            git_ref=git_ref,
            _backend_client=self._backend_client,
        )

    def resume(self, state: SandboxSessionState) -> Sandbox:
        """Reattach to a running sandbox from serialized session state.

        The state must come from a sandbox on this client's backend.
        Resume skips preset setup and workspace materialization — the
        sandbox is returned exactly as it is running.
        """
        return Sandbox._resume_with(self._backend_name, self._backend_client, state)


# -- Structured output parsing ------------------------------------------------

# Regex to strip markdown code fences that LLMs often wrap JSON in.
_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)


def _extract_json_candidates(text: str) -> list[dict]:
    """Extract all JSON dict candidates from *text*.

    Returns a list of parsed dicts, trying in order:
    1. Direct ``json.loads`` on the stripped text.
    2. Contents of markdown code fences.
    3. All ``{...}`` substrings that parse as valid JSON dicts.
    """
    text = text.strip()
    candidates: list[dict] = []

    # 1. Direct parse
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            candidates.append(obj)
            return candidates
    except (json.JSONDecodeError, ValueError):
        pass

    # 2. Markdown fence
    m = _FENCE_RE.search(text)
    if m:
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict):
                candidates.append(obj)
                return candidates
        except (json.JSONDecodeError, ValueError):
            pass

    # 3. All {…} substrings
    idx = 0
    while True:
        start = text.find("{", idx)
        if start == -1:
            break
        end = text.rfind("}")
        while end > start:
            try:
                obj = json.loads(text[start : end + 1])
                if isinstance(obj, dict):
                    candidates.append(obj)
                    break
            except (json.JSONDecodeError, ValueError):
                pass
            end = text.rfind("}", start, end)
        idx = start + 1

    return candidates


def _try_construct(data: dict, return_type: type[T]) -> T:
    """Attempt to construct *return_type* from *data*. Raises on failure."""
    if hasattr(return_type, "model_validate"):
        return return_type.model_validate(data)
    if dataclasses.is_dataclass(return_type):
        field_names = {f.name for f in dataclasses.fields(return_type)}
        filtered = {k: v for k, v in data.items() if k in field_names}
        return return_type(**filtered)
    return return_type(**data)


def _parse_result(result: SandboxResult, return_type: type[T]) -> T:
    """Parse a SandboxResult's stdout into *return_type*."""
    if result.exit_code != 0:
        raise SandboxExecutionError(
            f"Command failed (exit {result.exit_code}) before parsing return_type.\n"
            f"stderr: {result.stderr[:500]}"
        )

    candidates = _extract_json_candidates(result.stdout)
    if not candidates:
        raise SandboxExecutionError(
            f"Could not extract JSON object from stdout:\n{result.stdout[:500]}"
        )

    # Try each candidate — return the first that validates against return_type
    last_error = None
    for data in candidates:
        try:
            return _try_construct(data, return_type)
        except Exception as exc:
            last_error = exc

    raise SandboxExecutionError(
        f"Failed to construct {return_type.__name__} from parsed JSON: {last_error}"
    ) from last_error


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
        parts = []
        for f in dataclasses.fields(cls):
            tp = hints.get(f.name, f.type)
            # get_type_hints returns actual type objects; use __name__ for clean output
            name = getattr(tp, "__name__", str(tp))
            parts.append(f"{f.name} ({name})")
        return "Return ONLY a JSON object with these fields: " + ", ".join(parts) + "."

    return "Return ONLY a JSON object."
