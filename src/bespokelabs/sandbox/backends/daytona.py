from __future__ import annotations

import os
import pathlib
import shlex

from bespokelabs.sandbox.exceptions import (
    BackendNotInstalledError,
    FeatureNotSupportedError,
    SandboxCreationError,
    SandboxExecutionError,
)
from bespokelabs.sandbox.types import FileInfo, SandboxConfig, SandboxResult, SnapshotInfo


class DaytonaAdapter:
    def __init__(self) -> None:
        self._client: object = None
        self._sandbox: object = None

    def create(self, config: SandboxConfig) -> None:
        try:
            from daytona import Daytona, DaytonaConfig  # type: ignore[import-untyped]
        except ImportError:
            raise BackendNotInstalledError(
                "Daytona SDK not installed. Run: pip install bespokelabs-sandbox[daytona]"
            )

        api_key = os.environ.get("DAYTONA_API_KEY")
        if not api_key:
            raise SandboxCreationError("DAYTONA_API_KEY environment variable is not set")

        daytona_config = DaytonaConfig(
            api_key=api_key,
            api_url=os.environ.get("DAYTONA_API_URL", "https://app.daytona.io/api"),
            target=os.environ.get("DAYTONA_TARGET", "us"),
        )
        self._client = Daytona(daytona_config)

        try:
            params = self._build_params(config)
            if params is not None:
                self._sandbox = self._client.create(params)
            else:
                self._sandbox = self._client.create()
        except Exception as exc:
            raise SandboxCreationError(f"Failed to create Daytona sandbox: {exc}") from exc

    @staticmethod
    def _build_params(config: SandboxConfig) -> object | None:
        """Build the appropriate Daytona params object for the config."""
        from daytona import CreateSandboxFromImageParams, CreateSandboxFromSnapshotParams  # type: ignore[import-untyped]
        from daytona.common.sandbox import Resources  # type: ignore[import-untyped]

        common: dict = {}
        if config.env_vars:
            common["env_vars"] = config.env_vars

        if config.image:
            # Build resources if non-default cpu or memory is specified
            resources_kwargs: dict = {}
            if config.cpu != 1.0:
                resources_kwargs["cpu"] = int(config.cpu)
            if config.memory_mb != 1024:
                # Daytona SDK expects memory in GiB
                resources_kwargs["memory"] = max(1, config.memory_mb // 1024)
            if config.disk_mb is not None:
                # Daytona SDK expects disk in GiB
                resources_kwargs["disk"] = max(1, config.disk_mb // 1024)

            resources = Resources(**resources_kwargs) if resources_kwargs else None
            return CreateSandboxFromImageParams(image=config.image, resources=resources, **common)

        if config.snapshot_id:
            return CreateSandboxFromSnapshotParams(snapshot=config.snapshot_id, **common)

        if common:
            return CreateSandboxFromSnapshotParams(**common)

        return None

    def execute_code(self, code: str, language: str = "python") -> SandboxResult:
        try:
            response = self._sandbox.process.code_run(code)
            return SandboxResult(
                stdout=getattr(response, "result", "") or "",
                stderr="",
                exit_code=getattr(response, "exit_code", 0) or 0,
            )
        except Exception as exc:
            raise SandboxExecutionError(f"Daytona code execution failed: {exc}") from exc

    def execute_command(self, command: str, args: list[str] | None = None) -> SandboxResult:
        try:
            full_cmd = command if not args else f"{command} {' '.join(shlex.quote(a) for a in args)}"
            response = self._sandbox.process.exec(full_cmd)
            return SandboxResult(
                stdout=getattr(response, "result", "") or "",
                stderr="",
                exit_code=getattr(response, "exit_code", 0) or 0,
            )
        except Exception as exc:
            raise SandboxExecutionError(f"Daytona command execution failed: {exc}") from exc

    def list_files(self, path: str = "/") -> list[FileInfo]:
        try:
            entries = self._sandbox.fs.list_files(path)
            return [
                FileInfo(
                    path=getattr(e, "name", str(e)),
                    is_dir=getattr(e, "is_dir", False),
                    size=getattr(e, "size", None),
                )
                for e in entries
            ]
        except Exception as exc:
            raise SandboxExecutionError(f"Daytona list_files failed: {exc}") from exc

    def read_file(self, path: str) -> bytes:
        try:
            content = self._sandbox.fs.download_file(path)
            return content if isinstance(content, bytes) else content.encode()
        except Exception as exc:
            raise SandboxExecutionError(f"Daytona read_file failed: {exc}") from exc

    def write_file(self, path: str, content: bytes | str) -> None:
        try:
            data = content if isinstance(content, bytes) else content.encode()
            self._sandbox.fs.upload_file(data, path)
        except Exception as exc:
            raise SandboxExecutionError(f"Daytona write_file failed: {exc}") from exc

    def upload_file(self, local_path: str, remote_path: str) -> None:
        try:
            data = pathlib.Path(local_path).read_bytes()
            self._sandbox.fs.upload_file(data, remote_path)
        except Exception as exc:
            raise SandboxExecutionError(f"Daytona upload_file failed: {exc}") from exc

    def download_file(self, remote_path: str, local_path: str) -> None:
        try:
            data = self._sandbox.fs.download_file(remote_path)
            content = data if isinstance(data, bytes) else data.encode()
            pathlib.Path(local_path).write_bytes(content)
        except Exception as exc:
            raise SandboxExecutionError(f"Daytona download_file failed: {exc}") from exc

    def snapshot(self) -> SnapshotInfo:
        raise FeatureNotSupportedError(
            "Snapshots are not supported by the Daytona backend via this SDK"
        )

    def destroy(self) -> None:
        try:
            if self._sandbox and self._client:
                self._client.delete(self._sandbox)
        except Exception:
            pass
        self._sandbox = None
