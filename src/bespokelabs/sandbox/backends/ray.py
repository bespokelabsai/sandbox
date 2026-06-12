from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile
import threading

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


class RayClient:
    """Factory for Ray-backed sandboxes.

    Importing Ray happens once at construction; the Ray runtime is
    initialized on first create() (Ray itself keeps that global) and
    shared by every session created through this client.
    """

    def __init__(self) -> None:
        try:
            import ray  # type: ignore[import-untyped]
        except ImportError as exc:
            raise BackendNotInstalledError(
                "Ray not installed. Run: pip install bespokelabs-sandbox[ray]"
            ) from exc
        self._ray = ray
        # create() may run concurrently (e.g. via AsyncSandboxClient);
        # guard the one-time global ray.init().
        self._init_lock = threading.Lock()

    def create(self, config: SandboxConfig) -> RaySession:
        ray = self._ray
        try:
            with self._init_lock:
                if not ray.is_initialized():
                    address = os.environ.get("RAY_ADDRESS")
                    if address:
                        ray.init(address=address)
                    else:
                        ray.init()

            # Create a persistent actor for this sandbox
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
                    if is_python_language(language):
                        code = PYTHON_PREAMBLE + (
                            "exec(compile(%r, \"<sandbox>\", \"exec\"), globals())\n" % code
                        )
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
                        shell_cmd = rewrite_redirects(cmd[2])
                        cmd = ["bash", "-c", SHELL_PRELUDE + shell_cmd] + [
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

            ActorWithCpu = SandboxActor.options(num_cpus=config.cpu)
            actor = ActorWithCpu.remote(env_vars, config.timeout_secs)
            return RaySession(ray=ray, actor=actor)
        except Exception as exc:
            raise SandboxCreationError(f"Failed to create Ray sandbox: {exc}") from exc


class RaySession:
    """One live sandbox actor on a Ray cluster (local or remote)."""

    def __init__(self, *, ray: object, actor: object) -> None:
        self._ray = ray
        self._actor: object = actor

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
