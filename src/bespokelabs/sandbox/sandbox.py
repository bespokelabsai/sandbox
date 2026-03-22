from __future__ import annotations

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


class Sandbox:
    """Unified sandbox interface across Daytona, Tensorlake, Modal, and E2B.

    Usage:
        with Sandbox("e2b", timeout_secs=300) as sb:
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

    def execute_code(self, code: str, language: str = "python") -> SandboxResult:
        """Execute a code snippet and return stdout/stderr/exit_code."""
        self._check_alive()
        return self._adapter.execute_code(code, language)

    def execute_command(self, command: str, args: list[str] | None = None) -> SandboxResult:
        """Execute a shell command and return stdout/stderr/exit_code."""
        self._check_alive()
        return self._adapter.execute_command(command, args)

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
