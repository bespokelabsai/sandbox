from __future__ import annotations

import os
import pathlib
import platform
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
    BackendNotInstalledError,
    FeatureNotSupportedError,
    SandboxCreationError,
    SandboxExecutionError,
)
from bespokelabs.sandbox.types import FileInfo, SandboxConfig, SandboxResult, SnapshotInfo


class SafehouseAdapter:
    """Sandbox backed by agent-safehouse on macOS.

    Like the local adapter but wraps all process execution through
    ``safehouse`` for OS-level filesystem and network restrictions via
    macOS sandbox-exec.  File helper methods (read_file, write_file, …)
    operate directly on the host filesystem since the restriction only
    applies to sandboxed subprocesses.

    Requires:
        - macOS (uses Apple sandbox-exec)
        - ``safehouse`` CLI installed (brew install eugene1g/safehouse/agent-safehouse)
    """

    def __init__(self) -> None:
        self._workdir: str | None = None
        self._timeout: int = 600
        self._env: dict[str, str] | None = None
        self._owns_workdir: bool = True
        self._safehouse_bin: str | None = None

    # -- Lifecycle -------------------------------------------------------------

    def create(self, config: SandboxConfig) -> None:
        if platform.system() != "Darwin":
            raise SandboxCreationError(
                "The safehouse backend requires macOS (uses sandbox-exec)"
            )

        safehouse = shutil.which("safehouse")
        if not safehouse:
            raise BackendNotInstalledError(
                "safehouse CLI not found. Install it with:\n"
                "  brew install eugene1g/safehouse/agent-safehouse"
            )
        self._safehouse_bin = safehouse

        try:
            if config.workdir:
                self._workdir = os.path.abspath(config.workdir)
                os.makedirs(self._workdir, exist_ok=True)
                self._owns_workdir = False
            else:
                self._workdir = tempfile.mkdtemp(prefix="sandbox_safehouse_")
                self._owns_workdir = True
            self._timeout = config.timeout_secs
            self._env = {**os.environ, **(config.env_vars or {})}
            self._env["SANDBOX_ROOT"] = self._workdir
            self._env["HOME"] = self._workdir
        except (SandboxCreationError, BackendNotInstalledError):
            raise
        except Exception as exc:
            raise SandboxCreationError(f"Failed to create safehouse sandbox: {exc}") from exc

    def destroy(self) -> None:
        try:
            if self._owns_workdir and self._workdir and os.path.exists(self._workdir):
                shutil.rmtree(self._workdir, ignore_errors=True)
        except Exception:
            pass
        self._workdir = None

    # -- Execution -------------------------------------------------------------

    def execute_code(self, code: str, language: str = "python") -> SandboxResult:
        resolved = self._resolve_interpreter(language)
        if is_python_language(language):
            code = PYTHON_PREAMBLE + (
                "exec(compile(%r, \"<sandbox>\", \"exec\"), globals())\n" % code
            )
        cmd = self._safehouse_cmd() + [resolved, "-c", code]
        return self._run(cmd)

    def execute_command(self, command: str, args: list[str] | None = None) -> SandboxResult:
        if args:
            if (command in ("sh", "bash", "zsh")
                    and len(args) >= 2 and args[0] == "-c"):
                shell_cmd = rewrite_redirects(args[1])
                inner = ["bash", "-c", SHELL_PRELUDE + shell_cmd] + [
                    self._resolve_path(a) if os.path.isabs(a) else a
                    for a in args[2:]
                ]
            else:
                inner = [command] + [
                    self._resolve_path(a) if os.path.isabs(a) else a
                    for a in args
                ]
        else:
            inner = ["bash", "-c", SHELL_PRELUDE + rewrite_redirects(command)]

        cmd = self._safehouse_cmd() + inner
        return self._run(cmd)

    # -- File operations (direct host access) ----------------------------------

    def list_files(self, path: str = "/") -> list[FileInfo]:
        try:
            resolved = self._resolve_path(path)
            p = pathlib.Path(resolved)
            if not p.exists():
                raise SandboxExecutionError(f"Safehouse list_files failed: path '{path}' does not exist")
            if not p.is_dir():
                raise SandboxExecutionError(f"Safehouse list_files failed: '{path}' is not a directory")
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
            raise SandboxExecutionError(f"Safehouse list_files failed: {exc}") from exc

    def read_file(self, path: str) -> bytes:
        try:
            resolved = self._resolve_path(path)
            return pathlib.Path(resolved).read_bytes()
        except Exception as exc:
            raise SandboxExecutionError(f"Safehouse read_file failed: {exc}") from exc

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
            raise SandboxExecutionError(f"Safehouse write_file failed: {exc}") from exc

    def upload_file(self, local_path: str, remote_path: str) -> None:
        try:
            resolved = self._resolve_path(remote_path)
            dest = pathlib.Path(resolved)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local_path, dest)
        except Exception as exc:
            raise SandboxExecutionError(f"Safehouse upload_file failed: {exc}") from exc

    def download_file(self, remote_path: str, local_path: str) -> None:
        try:
            resolved = self._resolve_path(remote_path)
            shutil.copy2(resolved, local_path)
        except Exception as exc:
            raise SandboxExecutionError(f"Safehouse download_file failed: {exc}") from exc

    def snapshot(self) -> SnapshotInfo:
        raise FeatureNotSupportedError(
            "Snapshots are not supported by the safehouse backend"
        )

    # -- Internal --------------------------------------------------------------

    def _safehouse_cmd(self) -> list[str]:
        """Build the safehouse prefix for wrapping a command."""
        assert self._safehouse_bin and self._workdir
        return [
            self._safehouse_bin,
            f"--workdir={self._workdir}",
            "--env",
            "--",
        ]

    def _run(self, cmd: list[str]) -> SandboxResult:
        """Execute a command list and return a SandboxResult."""
        try:
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
                stderr=f"Execution timed out after {self._timeout}s",
                exit_code=124,
            )
        except Exception as exc:
            raise SandboxExecutionError(f"Safehouse execution failed: {exc}") from exc

    def _resolve_interpreter(self, language: str) -> str:
        if language in ("python", "python3"):
            env_path = (self._env or {}).get("PATH", os.environ.get("PATH", ""))
            alt = "python3" if language == "python" else "python"
            if shutil.which(language, path=env_path):
                return language
            if shutil.which(alt, path=env_path):
                return alt
        return language

    def _resolve_path(self, path: str) -> str:
        if os.path.isabs(path):
            path = path.lstrip("/")
        return os.path.join(self._workdir, path)

    def _to_sandbox_path(self, host_path: pathlib.Path) -> str:
        rel = host_path.relative_to(self._workdir)
        return f"/{rel}"
