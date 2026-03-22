from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import tempfile

from bespokelabs.sandbox.exceptions import (
    BackendNotInstalledError,
    FeatureNotSupportedError,
    SandboxCreationError,
    SandboxExecutionError,
)
from bespokelabs.sandbox.types import FileInfo, SandboxConfig, SandboxResult, SnapshotInfo

# Shared constants — identical to the ones in local.py.  Kept in each
# backend file so that each module is self-contained.

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


class RayAdapter:
    """Sandbox backed by a Ray cluster for distributed code execution.

    Code runs on a Ray worker via a persistent actor. Supports both
    local Ray clusters and remote clusters via base_url / RAY_ADDRESS.
    """

    def __init__(self) -> None:
        self._ray = None
        self._actor: object = None
        self._timeout: int = 600

    def create(self, config: SandboxConfig) -> None:
        try:
            import ray  # type: ignore[import-untyped]
        except ImportError:
            raise BackendNotInstalledError(
                "Ray not installed. Run: pip install bespokelabs-sandbox[ray]"
            )

        self._ray = ray
        self._timeout = config.timeout_secs

        try:
            if not ray.is_initialized():
                address = os.environ.get("RAY_ADDRESS")
                if address:
                    ray.init(address=address)
                else:
                    ray.init()

            # Create a persistent actor for this sandbox
            actor_cpu = config.cpu
            env_vars = config.env_vars or {}

            @ray.remote
            class SandboxActor:
                def __init__(self, env_vars: dict[str, str], timeout: int) -> None:
                    self.workdir = tempfile.mkdtemp(prefix="sandbox_ray_")
                    self.timeout = timeout
                    self.env = {**os.environ, **env_vars}
                    self.env["SANDBOX_ROOT"] = self.workdir
                    self.env["HOME"] = self.workdir

                def execute_code(self, code: str, language: str) -> dict:
                    resolved = self._resolve_interpreter(language)
                    if language.startswith("python"):
                        code = _PYTHON_PREAMBLE + code
                    try:
                        result = subprocess.run(
                            [resolved, "-c", code],
                            capture_output=True,
                            text=True,
                            timeout=self.timeout,
                            cwd=self.workdir,
                            env=self.env,
                        )
                        return {"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.returncode}
                    except subprocess.TimeoutExpired:
                        return {"stdout": "", "stderr": f"Execution timed out after {self.timeout}s", "exit_code": 124}

                def execute_command(self, cmd: list[str]) -> dict:
                    if len(cmd) >= 3 and cmd[0] in ("sh", "bash", "zsh") and cmd[1] == "-c":
                        # Shell string form (including nested shells from the
                        # args path): apply prelude and rewrite redirections.
                        shell_cmd = cmd[2]
                        shell_cmd = re.sub(r'(>[>]?\s*)(/[^\s])', r'\1${SANDBOX_ROOT}\2', shell_cmd)
                        shell_cmd = re.sub(r'(<\s*)(/[^\s])', r'\1${SANDBOX_ROOT}\2', shell_cmd)
                        cmd = ["bash", "-c", _SHELL_PRELUDE + shell_cmd] + [
                            self._resolve(a) if os.path.isabs(a) else a
                            for a in cmd[3:]
                        ]
                    else:
                        # Args form: resolve absolute paths in arguments.
                        cmd = [cmd[0]] + [
                            self._resolve(a) if os.path.isabs(a) else a
                            for a in cmd[1:]
                        ]
                    try:
                        result = subprocess.run(
                            cmd,
                            capture_output=True,
                            text=True,
                            timeout=self.timeout,
                            cwd=self.workdir,
                            env=self.env,
                        )
                        return {"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.returncode}
                    except subprocess.TimeoutExpired:
                        return {"stdout": "", "stderr": f"Command timed out after {self.timeout}s", "exit_code": 124}

                def _resolve_interpreter(self, language: str) -> str:
                    if language in ("python", "python3"):
                        env_path = self.env.get("PATH", os.environ.get("PATH", ""))
                        alt = "python3" if language == "python" else "python"
                        if shutil.which(language, path=env_path):
                            return language
                        if shutil.which(alt, path=env_path):
                            return alt
                    return language

                def _resolve(self, path: str) -> str:
                    if os.path.isabs(path):
                        path = path.lstrip("/")
                    return os.path.join(self.workdir, path)

                def list_files(self, path: str) -> list[dict]:
                    p = pathlib.Path(self._resolve(path))
                    if not p.exists():
                        raise FileNotFoundError(f"Path '{path}' does not exist")
                    return [
                        {
                            "path": f"/{e.relative_to(self.workdir)}",
                            "is_dir": e.is_dir(),
                            "size": e.stat().st_size if e.is_file() else None,
                        }
                        for e in sorted(p.iterdir())
                    ]

                def read_file(self, path: str) -> bytes:
                    return pathlib.Path(self._resolve(path)).read_bytes()

                def write_file(self, path: str, content: bytes) -> None:
                    p = pathlib.Path(self._resolve(path))
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(content)

                def upload_file(self, data: bytes, remote_path: str) -> None:
                    p = pathlib.Path(self._resolve(remote_path))
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(data)

                def download_file(self, remote_path: str) -> bytes:
                    return pathlib.Path(self._resolve(remote_path)).read_bytes()

                def destroy(self) -> None:
                    if os.path.exists(self.workdir):
                        shutil.rmtree(self.workdir, ignore_errors=True)

            ActorWithCpu = SandboxActor.options(num_cpus=actor_cpu)
            self._actor = ActorWithCpu.remote(env_vars, self._timeout)
        except BackendNotInstalledError:
            raise
        except Exception as exc:
            raise SandboxCreationError(f"Failed to create Ray sandbox: {exc}") from exc

    def execute_code(self, code: str, language: str = "python") -> SandboxResult:
        try:
            result = self._ray.get(self._actor.execute_code.remote(code, language))
            return SandboxResult(**result)
        except Exception as exc:
            raise SandboxExecutionError(f"Ray code execution failed: {exc}") from exc

    def execute_command(self, command: str, args: list[str] | None = None) -> SandboxResult:
        try:
            if args:
                cmd = [command] + args
            else:
                cmd = ["bash", "-c", command]
            result = self._ray.get(self._actor.execute_command.remote(cmd))
            return SandboxResult(**result)
        except Exception as exc:
            raise SandboxExecutionError(f"Ray command execution failed: {exc}") from exc

    def list_files(self, path: str = "/") -> list[FileInfo]:
        try:
            entries = self._ray.get(self._actor.list_files.remote(path))
            return [FileInfo(**e) for e in entries]
        except Exception as exc:
            raise SandboxExecutionError(f"Ray list_files failed: {exc}") from exc

    def read_file(self, path: str) -> bytes:
        try:
            return self._ray.get(self._actor.read_file.remote(path))
        except Exception as exc:
            raise SandboxExecutionError(f"Ray read_file failed: {exc}") from exc

    def write_file(self, path: str, content: bytes | str) -> None:
        try:
            data = content if isinstance(content, bytes) else content.encode()
            self._ray.get(self._actor.write_file.remote(path, data))
        except Exception as exc:
            raise SandboxExecutionError(f"Ray write_file failed: {exc}") from exc

    def upload_file(self, local_path: str, remote_path: str) -> None:
        try:
            data = pathlib.Path(local_path).read_bytes()
            self._ray.get(self._actor.upload_file.remote(data, remote_path))
        except Exception as exc:
            raise SandboxExecutionError(f"Ray upload_file failed: {exc}") from exc

    def download_file(self, remote_path: str, local_path: str) -> None:
        try:
            data = self._ray.get(self._actor.download_file.remote(remote_path))
            pathlib.Path(local_path).write_bytes(data)
        except Exception as exc:
            raise SandboxExecutionError(f"Ray download_file failed: {exc}") from exc

    def snapshot(self) -> SnapshotInfo:
        raise FeatureNotSupportedError(
            "Snapshots are not supported by the Ray backend"
        )

    def destroy(self) -> None:
        try:
            if self._actor:
                self._ray.get(self._actor.destroy.remote())
        except Exception:
            pass
        self._actor = None
