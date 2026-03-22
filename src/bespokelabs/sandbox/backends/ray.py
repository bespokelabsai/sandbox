from __future__ import annotations

import os
import pathlib
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
                    self.env = {**os.environ, **env_vars} if env_vars else None

                def execute_code(self, code: str, language: str) -> dict:
                    try:
                        result = subprocess.run(
                            [language, "-c", code],
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
