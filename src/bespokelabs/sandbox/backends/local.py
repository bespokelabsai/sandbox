from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import tempfile

from bespokelabs.sandbox.exceptions import (
    FeatureNotSupportedError,
    SandboxCreationError,
    SandboxExecutionError,
)
from bespokelabs.sandbox.types import FileInfo, SandboxConfig, SandboxResult, SnapshotInfo

# Shell prelude injected into bash -c commands.  Defines wrapper functions
# for common file utilities so that absolute-path arguments are rewritten
# to $SANDBOX_ROOT/... before the real binary runs.
_SHELL_PRELUDE = r"""
__sb_run() {
    local cmd="$1"; shift
    local args=()
    for arg in "$@"; do
        if [[ "$arg" == /* ]]; then
            args+=("${SANDBOX_ROOT}${arg}")
        else
            args+=("$arg")
        fi
    done
    command "$cmd" "${args[@]}"
}
for __c in cat ls cp mv head tail wc grep find rm mkdir touch chmod stat file; do
    eval "$__c() { __sb_run $__c \"\$@\"; }"
done
"""

# Python preamble injected into execute_code() invocations.  Monkey-patches
# builtins.open, io.open, and common os functions so that absolute paths
# resolve under $SANDBOX_ROOT instead of the host root.  Paths that already
# point inside the sandbox (or to /dev, /proc, /sys) are left alone to
# prevent double-rebasing and to keep special files working.
_PYTHON_PREAMBLE = """\
def _sb_setup():
    import builtins, io, os
    root = os.environ.get("SANDBOX_ROOT", "")
    if not root:
        return
    pfx = root + "/"
    def rp(p):
        if isinstance(p, str) and p.startswith("/") and not (
            p.startswith((pfx, "/dev/", "/proc/", "/sys/")) or p == root
        ):
            return root + p
        if hasattr(p, "__fspath__"):
            s = os.fspath(p)
            if isinstance(s, str) and s.startswith("/") and not (
                s.startswith((pfx, "/dev/", "/proc/", "/sys/")) or s == root
            ):
                import pathlib
                return pathlib.Path(root + s)
        return p
    _orig = builtins.open
    def _open(f, *a, **k):
        return _orig(rp(f), *a, **k)
    builtins.open = io.open = _open
    for _n in ("stat", "lstat", "listdir", "scandir", "mkdir", "makedirs",
               "remove", "unlink", "rmdir", "open", "chmod"):
        _f = getattr(os, _n, None)
        if _f:
            def _w(_o=_f):
                def _fn(p, *a, **k):
                    return _o(rp(p), *a, **k)
                return _fn
            setattr(os, _n, _w())
_sb_setup()
del _sb_setup
"""


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
        if language.startswith("python"):
            code = _PYTHON_PREAMBLE + code
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
                    shell_cmd = self._rewrite_redirects(args[1])
                    cmd = ["bash", "-c", _SHELL_PRELUDE + shell_cmd] + [
                        self._resolve_path(a) if os.path.isabs(a) else a
                        for a in args[2:]
                    ]
                else:
                    cmd = [command] + [
                        self._resolve_path(a) if os.path.isabs(a) else a
                        for a in args
                    ]
            else:
                cmd = ["bash", "-c", self._wrap_command(command)]
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

    @staticmethod
    def _rewrite_redirects(command: str) -> str:
        """Rewrite absolute paths in shell redirections (>, >>, <)."""
        command = re.sub(r'(>[>]?\s*)(/[^\s])', r'\1${SANDBOX_ROOT}\2', command)
        command = re.sub(r'(<\s*)(/[^\s])', r'\1${SANDBOX_ROOT}\2', command)
        return command

    def _wrap_command(self, command: str) -> str:
        """Wrap a shell command so absolute paths resolve under the sandbox root."""
        return _SHELL_PRELUDE + self._rewrite_redirects(command)
