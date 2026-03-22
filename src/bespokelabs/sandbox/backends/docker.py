from __future__ import annotations

import io
import os
import pathlib
import tarfile

from bespokelabs.sandbox.exceptions import (
    BackendNotInstalledError,
    FeatureNotSupportedError,
    SandboxCreationError,
    SandboxExecutionError,
)
from bespokelabs.sandbox.types import FileInfo, SandboxConfig, SandboxResult, SnapshotInfo

_DEFAULT_IMAGE = "python:3.12-slim"


class DockerAdapter:
    def __init__(self) -> None:
        self._client: object = None
        self._container: object = None
        self._timeout: int = 600

    def create(self, config: SandboxConfig) -> None:
        try:
            import docker  # type: ignore[import-untyped]
        except ImportError:
            raise BackendNotInstalledError(
                "Docker SDK not installed. Run: pip install bespokelabs-sandbox[docker]"
            )

        try:
            self._client = docker.from_env()
            self._client.ping()
        except Exception as exc:
            raise SandboxCreationError(
                f"Cannot connect to Docker daemon. Is Docker running? {exc}"
            ) from exc

        image = config.image or _DEFAULT_IMAGE

        try:
            self._client.images.get(image)
        except Exception:
            try:
                self._client.images.pull(image)
            except Exception as exc:
                raise SandboxCreationError(f"Failed to pull Docker image '{image}': {exc}") from exc

        create_kwargs: dict = {
            "image": image,
            "entrypoint": ["tail", "-f", "/dev/null"],
            "command": [],
            "detach": True,
            "stdin_open": True,
            "tty": False,
            "mem_limit": f"{config.memory_mb}m",
            "cpu_quota": int(config.cpu * 100_000),
            "cpu_period": 100_000,
            "network_mode": "bridge" if config.allow_internet else "none",
        }

        if config.env_vars:
            create_kwargs["environment"] = config.env_vars

        try:
            self._container = self._client.containers.run(**create_kwargs)
            self._timeout = config.timeout_secs
        except Exception as exc:
            raise SandboxCreationError(f"Failed to create Docker container: {exc}") from exc

    def execute_code(self, code: str, language: str = "python") -> SandboxResult:
        try:
            return self._exec_with_timeout([language, "-c", code])
        except SandboxExecutionError:
            raise
        except Exception as exc:
            raise SandboxExecutionError(f"Docker code execution failed: {exc}") from exc

    def execute_command(self, command: str, args: list[str] | None = None) -> SandboxResult:
        try:
            if args:
                cmd = [command] + args
            else:
                cmd = ["bash", "-c", command]
            return self._exec_with_timeout(cmd)
        except SandboxExecutionError:
            raise
        except Exception as exc:
            raise SandboxExecutionError(f"Docker command execution failed: {exc}") from exc

    def list_files(self, path: str = "/") -> list[FileInfo]:
        try:
            return self._list_files_find(path)
        except SandboxExecutionError:
            # find -printf not available (e.g. BusyBox), fall back to ls
            return self._list_files_ls(path)

    def _list_files_find(self, path: str) -> list[FileInfo]:
        result = self._container.exec_run(
            ["find", path, "-maxdepth", "1", "-printf", "%y %s %P\n"],
            demux=True,
        )
        stdout_bytes, stderr_bytes = result.output
        if result.exit_code != 0:
            stderr = (stderr_bytes or b"").decode(errors="replace")
            raise SandboxExecutionError(
                f"Docker list_files failed: find exited {result.exit_code}\nstderr={stderr}"
            )
        stdout = (stdout_bytes or b"").decode(errors="replace")
        files: list[FileInfo] = []
        for line in stdout.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(" ", 2)
            if len(parts) < 3 or not parts[2]:
                continue
            file_type, size_str, name = parts
            files.append(FileInfo(
                path=f"{path.rstrip('/')}/{name}",
                is_dir=(file_type == "d"),
                size=int(size_str) if size_str.isdigit() else None,
            ))
        return files

    def _list_files_ls(self, path: str) -> list[FileInfo]:
        result = self._container.exec_run(
            ["ls", "-1F", path],
            demux=True,
        )
        stdout_bytes, stderr_bytes = result.output
        if result.exit_code != 0:
            stderr = (stderr_bytes or b"").decode(errors="replace")
            raise SandboxExecutionError(
                f"Docker list_files failed: ls exited {result.exit_code}\nstderr={stderr}"
            )
        stdout = (stdout_bytes or b"").decode(errors="replace")
        files: list[FileInfo] = []
        for line in stdout.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            is_dir = line.endswith("/")
            name = line.rstrip("*/=>@|")
            files.append(FileInfo(path=f"{path.rstrip('/')}/{name}", is_dir=is_dir))
        return files

    def read_file(self, path: str) -> bytes:
        try:
            stream, _ = self._container.get_archive(path)
            tar_bytes = b"".join(stream)
            with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
                member = tar.getmembers()[0]
                f = tar.extractfile(member)
                if f is None:
                    raise SandboxExecutionError(f"Docker read_file: '{path}' is not a regular file")
                return f.read()
        except SandboxExecutionError:
            raise
        except Exception as exc:
            raise SandboxExecutionError(f"Docker read_file failed: {exc}") from exc

    def write_file(self, path: str, content: bytes | str) -> None:
        try:
            data = content if isinstance(content, bytes) else content.encode()
            self._put_file(path, data)
        except Exception as exc:
            raise SandboxExecutionError(f"Docker write_file failed: {exc}") from exc

    def upload_file(self, local_path: str, remote_path: str) -> None:
        try:
            data = pathlib.Path(local_path).read_bytes()
            self._put_file(remote_path, data)
        except Exception as exc:
            raise SandboxExecutionError(f"Docker upload_file failed: {exc}") from exc

    def download_file(self, remote_path: str, local_path: str) -> None:
        try:
            content = self.read_file(remote_path)
            pathlib.Path(local_path).write_bytes(content)
        except SandboxExecutionError:
            raise
        except Exception as exc:
            raise SandboxExecutionError(f"Docker download_file failed: {exc}") from exc

    def snapshot(self) -> SnapshotInfo:
        try:
            result = self._container.commit()
            return SnapshotInfo(
                snapshot_id=result.id,
                backend="docker",
            )
        except Exception as exc:
            raise SandboxExecutionError(f"Docker snapshot failed: {exc}") from exc

    def destroy(self) -> None:
        try:
            if self._container:
                self._container.remove(force=True)
        except Exception:
            pass
        self._container = None

    # -- Internal ----------------------------------------------------------

    def _exec_with_timeout(self, cmd: list[str]) -> SandboxResult:
        """Run a command in the container with a timeout enforced via a wrapper."""
        # Docker exec_run has no native timeout, so wrap with `timeout` command
        wrapped = ["timeout", str(self._timeout)] + cmd
        result = self._container.exec_run(wrapped, demux=True)
        stdout, stderr = result.output
        exit_code = result.exit_code or 0
        # `timeout` returns 124 when the command times out
        if exit_code == 124:
            return SandboxResult(
                stdout=(stdout or b"").decode(errors="replace"),
                stderr=f"Execution timed out after {self._timeout}s",
                exit_code=124,
            )
        return SandboxResult(
            stdout=(stdout or b"").decode(errors="replace"),
            stderr=(stderr or b"").decode(errors="replace"),
            exit_code=exit_code,
        )

    def _put_file(self, remote_path: str, data: bytes) -> None:
        """Write bytes to a file inside the container via put_archive."""
        dir_path = os.path.dirname(remote_path) or "/"
        file_name = os.path.basename(remote_path)

        # Ensure the parent directory exists
        if dir_path != "/":
            self._container.exec_run(["mkdir", "-p", dir_path])

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name=file_name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        buf.seek(0)
        self._container.put_archive(dir_path, buf)
