from __future__ import annotations

import os
import pathlib
import shlex
import shutil
import subprocess
import tempfile

from bespokelabs.sandbox.exceptions import (
    FeatureNotSupportedError,
    SandboxCreationError,
    SandboxExecutionError,
)
from bespokelabs.sandbox.types import FileInfo, SandboxConfig, SandboxResult, SnapshotInfo


class LocalAdapter:
    """Sandbox backed by local subprocess execution in a temp directory.

    No external dependencies, no Docker, no API keys.
    Code runs directly on the host in an isolated temp directory.
    """

    def __init__(self) -> None:
        self._workdir: str | None = None
        self._timeout: int = 600

    def create(self, config: SandboxConfig) -> None:
        try:
            self._workdir = tempfile.mkdtemp(prefix="sandbox_local_")
            self._timeout = config.timeout_secs
            if config.env_vars:
                self._env_vars = {**os.environ, **config.env_vars}
            else:
                self._env_vars = None
        except Exception as exc:
            raise SandboxCreationError(f"Failed to create local sandbox: {exc}") from exc

    def execute_code(self, code: str, language: str = "python") -> SandboxResult:
        try:
            result = subprocess.run(
                [language, "-c", code],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                cwd=self._workdir,
                env=self._env_vars,
            )
            return SandboxResult(
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
            )
        except subprocess.TimeoutExpired:
            return SandboxResult(
                stdout="",
                stderr=f"Execution timed out after {self._timeout}s",
                exit_code=124,
            )
        except Exception as exc:
            raise SandboxExecutionError(f"Local code execution failed: {exc}") from exc

    def execute_command(self, command: str, args: list[str] | None = None) -> SandboxResult:
        try:
            if args:
                cmd = [command] + args
            else:
                cmd = ["bash", "-c", command]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                cwd=self._workdir,
                env=self._env_vars,
            )
            return SandboxResult(
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
            )
        except subprocess.TimeoutExpired:
            return SandboxResult(
                stdout="",
                stderr=f"Command timed out after {self._timeout}s",
                exit_code=124,
            )
        except Exception as exc:
            raise SandboxExecutionError(f"Local command execution failed: {exc}") from exc

    def list_files(self, path: str = "/") -> list[FileInfo]:
        try:
            resolved = self._resolve_path(path)
            p = pathlib.Path(resolved)
            if not p.exists():
                raise SandboxExecutionError(f"Local list_files failed: path '{path}' does not exist")
            if not p.is_dir():
                raise SandboxExecutionError(f"Local list_files failed: '{path}' is not a directory")
            return [
                FileInfo(
                    path=self._to_sandbox_path(entry),
                    is_dir=entry.is_dir(),
                    size=entry.stat().st_size if entry.is_file() else None,
                )
                for entry in sorted(p.iterdir())
            ]
        except SandboxExecutionError:
            raise
        except Exception as exc:
            raise SandboxExecutionError(f"Local list_files failed: {exc}") from exc

    def read_file(self, path: str) -> bytes:
        try:
            resolved = self._resolve_path(path)
            return pathlib.Path(resolved).read_bytes()
        except Exception as exc:
            raise SandboxExecutionError(f"Local read_file failed: {exc}") from exc

    def write_file(self, path: str, content: bytes | str) -> None:
        try:
            resolved = self._resolve_path(path)
            p = pathlib.Path(resolved)
            p.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                p.write_bytes(content)
            else:
                p.write_text(content)
        except Exception as exc:
            raise SandboxExecutionError(f"Local write_file failed: {exc}") from exc

    def upload_file(self, local_path: str, remote_path: str) -> None:
        try:
            resolved = self._resolve_path(remote_path)
            dest = pathlib.Path(resolved)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local_path, dest)
        except Exception as exc:
            raise SandboxExecutionError(f"Local upload_file failed: {exc}") from exc

    def download_file(self, remote_path: str, local_path: str) -> None:
        try:
            resolved = self._resolve_path(remote_path)
            shutil.copy2(resolved, local_path)
        except Exception as exc:
            raise SandboxExecutionError(f"Local download_file failed: {exc}") from exc

    def snapshot(self) -> SnapshotInfo:
        raise FeatureNotSupportedError(
            "Snapshots are not supported by the local backend"
        )

    def destroy(self) -> None:
        try:
            if self._workdir and os.path.exists(self._workdir):
                shutil.rmtree(self._workdir, ignore_errors=True)
        except Exception:
            pass
        self._workdir = None

    # -- Internal ----------------------------------------------------------

    def _resolve_path(self, path: str) -> str:
        """Resolve a path inside the sandbox working directory.

        All paths are mapped under the sandbox workdir to prevent
        accidental access to the host filesystem.
        """
        if os.path.isabs(path):
            path = path.lstrip("/")
        return os.path.join(self._workdir, path)

    def _to_sandbox_path(self, host_path: pathlib.Path) -> str:
        """Convert a host filesystem path back to a sandbox-relative path."""
        rel = host_path.relative_to(self._workdir)
        return f"/{rel}"
