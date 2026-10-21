"""Docker sandbox backend."""

from __future__ import annotations

import io
import os
import pathlib
import tarfile
import threading

from bespokelabs.sandbox.exceptions import (
    BackendNotInstalledError,
    SandboxConfigurationError,
    SandboxCreationError,
    SandboxExecutionError,
)
from bespokelabs.sandbox.types import (
    ExposedPort,
    FileInfo,
    SandboxConfig,
    SandboxResult,
    SnapshotInfo,
)

_DEFAULT_IMAGE = "python:3.12-slim"


class DockerClient:
    """Factory for Docker sandboxes.

    The daemon connection is established on first create() and reused
    for every subsequent session created through this client.
    """

    supports_dockerfile = True
    # "none" is enforced through the derived allow_internet flag.
    network_modes = frozenset({"public", "none"})
    # containers.run kwargs that would override the policy.
    network_option_keys = frozenset(
        {"network_mode", "network", "network_disabled", "networking_config"}
    )

    def __init__(self) -> None:
        try:
            import docker  # type: ignore[import-untyped]
        except ImportError as exc:
            raise BackendNotInstalledError(
                "Docker SDK not installed. Run: pip install bespokelabs-sandbox[docker]"
            ) from exc
        self._docker = docker
        self._client: object = None
        # create() may run concurrently (e.g. via AsyncSandboxClient);
        # guard the one-time daemon connection.
        self._connect_lock = threading.Lock()

    def _connect(self) -> object:
        if self._client is None:
            with self._connect_lock:
                if self._client is None:
                    try:
                        client = self._docker.from_env()
                        client.ping()
                    except Exception as exc:
                        raise SandboxCreationError(
                            f"Cannot connect to Docker daemon. Is Docker running? {exc}"
                        ) from exc
                    self._client = client
        return self._client

    def resume(self, data: dict) -> DockerSession:
        client = self._connect()
        try:
            container = client.containers.get(data["container_id"])
        except Exception as exc:
            raise SandboxCreationError(
                f"Cannot resume Docker sandbox '{data.get('container_id')}': {exc}"
            ) from exc
        return DockerSession(
            container=container, timeout=int(data.get("timeout", 600))
        )

    def create(self, config: SandboxConfig) -> DockerSession:
        self._connect()

        if config.dockerfile:
            image = self._build(config.dockerfile, config.context_dir)
        else:
            image = config.image or _DEFAULT_IMAGE
            self._ensure_image(image)

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

        if config.backend_options:
            create_kwargs.update(config.backend_options)

        try:
            container = self._client.containers.run(**create_kwargs)
        except Exception as exc:
            raise SandboxCreationError(
                f"Failed to create Docker container: {exc}"
            ) from exc

        return DockerSession(container=container, timeout=config.timeout_secs)

    def _ensure_image(self, image: str) -> None:
        try:
            self._client.images.get(image)
        except Exception:
            try:
                self._client.images.pull(image)
            except Exception as exc:
                raise SandboxCreationError(
                    f"Failed to pull Docker image '{image}': {exc}"
                ) from exc

    def _build(self, dockerfile: str, context_dir: str | None) -> str:
        """Build *dockerfile* locally and return the image id.

        The image is left untagged; the daemon's layer cache makes a rebuild
        of an unchanged Dockerfile cheap.
        """
        context = context_dir or os.path.dirname(dockerfile)
        try:
            image, _ = self._client.images.build(
                path=context, dockerfile=dockerfile, rm=True, forcerm=True
            )
        except Exception as exc:
            raise SandboxCreationError(
                f"Failed to build Docker image from '{dockerfile}': {exc}"
            ) from exc
        return image.id


class DockerSession:
    """One live Docker container."""

    def __init__(self, *, container: object, timeout: int) -> None:
        self._container: object = container
        self._timeout = timeout

    @property
    def provider_resource_id(self) -> str | None:
        """Return the Docker container ID while it is attached."""
        value = getattr(self._container, "id", None)
        return str(value) if value is not None else None

    def execute_code(
        self, code: str, language: str = "python"
    ) -> SandboxResult:
        try:
            return self._exec_with_timeout([language, "-c", code])
        except SandboxExecutionError:
            raise
        except Exception as exc:
            raise SandboxExecutionError(
                f"Docker code execution failed: {exc}"
            ) from exc

    def execute_command(
        self, command: str, args: list[str] | None = None
    ) -> SandboxResult:
        try:
            if args:
                cmd = [command] + args
            else:
                cmd = ["bash", "-c", command]
            return self._exec_with_timeout(cmd)
        except SandboxExecutionError:
            raise
        except Exception as exc:
            raise SandboxExecutionError(
                f"Docker command execution failed: {exc}"
            ) from exc

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
        for raw_line in stdout.strip().splitlines():
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split(" ", 2)
            if len(parts) < 3 or not parts[2]:
                continue
            file_type, size_str, name = parts
            files.append(
                FileInfo(
                    path=f"{path.rstrip('/')}/{name}",
                    is_dir=(file_type == "d"),
                    size=int(size_str) if size_str.isdigit() else None,
                )
            )
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
        for raw_line in stdout.strip().splitlines():
            line = raw_line.strip()
            if not line:
                continue
            is_dir = line.endswith("/")
            name = line.rstrip("*/=>@|")
            files.append(
                FileInfo(path=f"{path.rstrip('/')}/{name}", is_dir=is_dir)
            )
        return files

    def read_file(self, path: str) -> bytes:
        try:
            stream, _ = self._container.get_archive(path)
            tar_bytes = b"".join(stream)
            with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
                member = tar.getmembers()[0]
                f = tar.extractfile(member)
                if f is None:
                    raise SandboxExecutionError(
                        f"Docker read_file: '{path}' is not a regular file"
                    )
                return f.read()
        except SandboxExecutionError:
            raise
        except Exception as exc:
            raise SandboxExecutionError(
                f"Docker read_file failed: {exc}"
            ) from exc

    def write_file(self, path: str, content: bytes | str) -> None:
        try:
            data = content if isinstance(content, bytes) else content.encode()
            self._put_file(path, data)
        except Exception as exc:
            raise SandboxExecutionError(
                f"Docker write_file failed: {exc}"
            ) from exc

    def upload_file(self, local_path: str, remote_path: str) -> None:
        try:
            data = pathlib.Path(local_path).read_bytes()
            self._put_file(remote_path, data)
        except Exception as exc:
            raise SandboxExecutionError(
                f"Docker upload_file failed: {exc}"
            ) from exc

    def download_file(self, remote_path: str, local_path: str) -> None:
        try:
            content = self.read_file(remote_path)
            pathlib.Path(local_path).write_bytes(content)
        except SandboxExecutionError:
            raise
        except Exception as exc:
            raise SandboxExecutionError(
                f"Docker download_file failed: {exc}"
            ) from exc

    def expose_port(self, port: int) -> ExposedPort:
        """Return the host address of a port published at creation."""
        try:
            self._container.reload()
            bindings = (self._container.ports or {}).get(f"{port}/tcp")
        except Exception as exc:
            raise SandboxExecutionError(
                f"Docker port lookup failed: {exc}"
            ) from exc
        if not bindings:
            raise SandboxConfigurationError(
                f"Port {port} is not published; create the sandbox with "
                f"backend_options={{'ports': {{'{port}/tcp': "
                f"('127.0.0.1', None)}}}}",
                backend="docker",
                op="expose_port",
            )
        binding = bindings[0]
        host = binding.get("HostIp") or "127.0.0.1"
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        if ":" in host:
            host = f"[{host}]"
        return ExposedPort(
            port=port, url=f"http://{host}:{binding['HostPort']}"
        )

    def snapshot(self) -> SnapshotInfo:
        try:
            result = self._container.commit()
            return SnapshotInfo(
                snapshot_id=result.id,
                backend="docker",
            )
        except Exception as exc:
            raise SandboxExecutionError(
                f"Docker snapshot failed: {exc}"
            ) from exc

    def session_state(self) -> dict:
        return {"container_id": self._container.id, "timeout": self._timeout}

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
