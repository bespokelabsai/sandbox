from __future__ import annotations

import base64
import pathlib
import shlex

from bespokelabs.sandbox.exceptions import (
    BackendNotInstalledError,
    SandboxCreationError,
    SandboxExecutionError,
)
from bespokelabs.sandbox.types import FileInfo, SandboxConfig, SandboxResult, SnapshotInfo


class TensorlakeAdapter:
    def __init__(self) -> None:
        self._client: object = None
        self._sandbox: object = None

    def create(self, config: SandboxConfig) -> None:
        try:
            from tensorlake.sandbox import SandboxClient  # type: ignore[import-untyped]
        except ImportError:
            raise BackendNotInstalledError(
                "Tensorlake SDK not installed. Run: pip install bespokelabs-sandbox[tensorlake]"
            )

        try:
            self._client = SandboxClient()
            kwargs: dict = {
                "cpus": config.cpu,
                "memory_mb": config.memory_mb,
                "timeout_secs": config.timeout_secs,
                "allow_internet_access": config.allow_internet,
            }
            if config.image:
                kwargs["image"] = config.image
            if config.snapshot_id:
                kwargs["snapshot_id"] = config.snapshot_id
            self._sandbox = self._client.create_and_connect(**kwargs)
        except Exception as exc:
            raise SandboxCreationError(f"Failed to create Tensorlake sandbox: {exc}") from exc

    def execute_code(self, code: str, language: str = "python") -> SandboxResult:
        try:
            result = self._sandbox.run(language, ["-c", code])
            return SandboxResult(
                stdout=getattr(result, "stdout", "") or "",
                stderr=getattr(result, "stderr", "") or "",
                exit_code=getattr(result, "exit_code", 0) or 0,
            )
        except Exception as exc:
            raise SandboxExecutionError(f"Tensorlake code execution failed: {exc}") from exc

    def execute_command(self, command: str, args: list[str] | None = None) -> SandboxResult:
        try:
            cmd_args = ["-c", command] if not args else ["-c", f"{command} {' '.join(shlex.quote(a) for a in args)}"]
            result = self._sandbox.run("bash", cmd_args)
            return SandboxResult(
                stdout=getattr(result, "stdout", "") or "",
                stderr=getattr(result, "stderr", "") or "",
                exit_code=getattr(result, "exit_code", 0) or 0,
            )
        except Exception as exc:
            raise SandboxExecutionError(f"Tensorlake command execution failed: {exc}") from exc

    def list_files(self, path: str = "/") -> list[FileInfo]:
        """List files by running ls inside the sandbox (no native file API)."""
        try:
            result = self._sandbox.run("bash", ["-c", f"ls -1F {shlex.quote(path)}"])
            files: list[FileInfo] = []
            for line in (result.stdout or "").strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                is_dir = line.endswith("/")
                name = line.rstrip("*/=>@|")
                files.append(FileInfo(path=f"{path.rstrip('/')}/{name}", is_dir=is_dir))
            return files
        except Exception as exc:
            raise SandboxExecutionError(f"Tensorlake list_files failed: {exc}") from exc

    def read_file(self, path: str) -> bytes:
        """Read file by running cat inside the sandbox."""
        try:
            result = self._sandbox.run("cat", [path])
            return (result.stdout or "").encode()
        except Exception as exc:
            raise SandboxExecutionError(f"Tensorlake read_file failed: {exc}") from exc

    def write_file(self, path: str, content: bytes | str) -> None:
        """Write file via base64 pipe inside the sandbox."""
        try:
            data = content if isinstance(content, bytes) else content.encode()
            encoded = base64.b64encode(data).decode()
            self._sandbox.run("bash", ["-c", f"echo {shlex.quote(encoded)} | base64 -d > {shlex.quote(path)}"])
        except Exception as exc:
            raise SandboxExecutionError(f"Tensorlake write_file failed: {exc}") from exc

    def upload_file(self, local_path: str, remote_path: str) -> None:
        """Upload a local file by base64-encoding and writing via shell."""
        try:
            data = pathlib.Path(local_path).read_bytes()
            encoded = base64.b64encode(data).decode()
            self._sandbox.run("bash", ["-c", f"echo {shlex.quote(encoded)} | base64 -d > {shlex.quote(remote_path)}"])
        except Exception as exc:
            raise SandboxExecutionError(f"Tensorlake upload_file failed: {exc}") from exc

    def download_file(self, remote_path: str, local_path: str) -> None:
        """Download a file by base64-encoding its contents from the sandbox."""
        try:
            result = self._sandbox.run("bash", ["-c", f"base64 {shlex.quote(remote_path)}"])
            exit_code = getattr(result, "exit_code", 0) or 0
            if exit_code != 0:
                stderr = getattr(result, "stderr", "") or ""
                raise SandboxExecutionError(
                    f"Tensorlake download_file failed: remote command exited {exit_code}\nstderr={stderr}"
                )
            data = base64.b64decode(result.stdout.strip())
            pathlib.Path(local_path).write_bytes(data)
        except SandboxExecutionError:
            raise
        except Exception as exc:
            raise SandboxExecutionError(f"Tensorlake download_file failed: {exc}") from exc

    def snapshot(self) -> SnapshotInfo:
        try:
            result = self._client.snapshot_and_wait(self._sandbox.sandbox_id)
            return SnapshotInfo(
                snapshot_id=getattr(result, "snapshot_id", str(result)),
                backend="tensorlake",
            )
        except Exception as exc:
            raise SandboxExecutionError(f"Tensorlake snapshot failed: {exc}") from exc

    def destroy(self) -> None:
        try:
            if self._sandbox:
                self._sandbox.close()
        except Exception:
            pass
        self._sandbox = None
