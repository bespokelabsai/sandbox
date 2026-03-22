from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile

from bespokelabs.sandbox.backends._prelude import (
    PYTHON_PREAMBLE,
    SHELL_PRELUDE,
    is_python_language,
    rewrite_redirects,
)
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

    The sandbox workdir acts as the filesystem root: absolute paths like
    /data/file.txt in both file helpers and subprocess commands resolve
    under the workdir.  For shell commands this is achieved via a bash
    prelude that wraps common utilities and rewrites redirections.  For
    Python code a startup preamble patches builtins.open and os.*
    functions so that open("/hello.txt") finds files written via
    write_file("/hello.txt", ...).
    """

    def __init__(self) -> None:
        self._workdir: str | None = None
        self._timeout: int = 600
        self._env: dict[str, str] | None = None

    def create(self, config: SandboxConfig) -> None:
        try:
            self._workdir = tempfile.mkdtemp(prefix="sandbox_local_")
            self._timeout = config.timeout_secs
            self._env = {**os.environ, **(config.env_vars or {})}
            self._env["SANDBOX_ROOT"] = self._workdir
            self._env["HOME"] = self._workdir
        except Exception as exc:
            raise SandboxCreationError(f"Failed to create local sandbox: {exc}") from exc

    def execute_code(self, code: str, language: str = "python") -> SandboxResult:
        resolved = self._resolve_interpreter(language)
        if is_python_language(language):
            code = PYTHON_PREAMBLE + (
                "exec(compile(%r, \"<sandbox>\", \"exec\"), globals())\n" % code
            )
        try:
            result = subprocess.run(
                [resolved, "-c", code],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                cwd=self._workdir,
                env=self._env,
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
                if (command in ("sh", "bash", "zsh")
                        and len(args) >= 2 and args[0] == "-c"):
                    # Nested shell: route through the prelude so that both
                    # command arguments and redirections are rebased.
                    shell_cmd = rewrite_redirects(args[1])
                    cmd = ["bash", "-c", SHELL_PRELUDE + shell_cmd] + [
                        self._resolve_path(a) if os.path.isabs(a) else a
                        for a in args[2:]
                    ]
                else:
                    cmd = [command] + [
                        self._resolve_path(a) if os.path.isabs(a) else a
                        for a in args
                    ]
            else:
                cmd = ["bash", "-c", SHELL_PRELUDE + rewrite_redirects(command)]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                cwd=self._workdir,
                env=self._env,
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

    def _resolve_interpreter(self, language: str) -> str:
        """Find a working interpreter binary, respecting the sandbox PATH.

        When the caller asks for "python" or "python3", check whether
        the requested name exists on the sandbox PATH and fall back to
        the alternative if it does not.  This keeps the zero-setup
        quickstart working on hosts that only ship python3.
        """
        if language in ("python", "python3"):
            env_path = (self._env or {}).get("PATH", os.environ.get("PATH", ""))
            alt = "python3" if language == "python" else "python"
            if shutil.which(language, path=env_path):
                return language
            if shutil.which(alt, path=env_path):
                return alt
        return language

    def _resolve_path(self, path: str) -> str:
        """Resolve a path inside the sandbox working directory."""
        if os.path.isabs(path):
            path = path.lstrip("/")
        return os.path.join(self._workdir, path)

    def _to_sandbox_path(self, host_path: pathlib.Path) -> str:
        """Convert a host filesystem path back to a sandbox-relative path."""
        rel = host_path.relative_to(self._workdir)
        return f"/{rel}"
