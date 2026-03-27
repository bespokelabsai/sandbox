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


class E2BAdapter:
    def __init__(self) -> None:
        self._sandbox: object = None

    def create(self, config: SandboxConfig) -> None:
        try:
            from e2b_code_interpreter import Sandbox as E2BSandbox  # type: ignore[import-untyped]
        except ImportError:
            raise BackendNotInstalledError(
                "E2B SDK not installed. Run: pip install bespokelabs-sandbox[e2b]"
            )

        api_key = os.environ.get("E2B_API_KEY")
        if not api_key:
            raise SandboxCreationError("E2B_API_KEY environment variable is not set")

        try:
            kwargs: dict = {"timeout": config.timeout_secs}
            if config.template:
                kwargs["template"] = config.template
            if config.env_vars:
                kwargs["envs"] = config.env_vars
            self._sandbox = E2BSandbox.create(**kwargs)
        except Exception as exc:
            raise SandboxCreationError(f"Failed to create E2B sandbox: {exc}") from exc

    def execute_code(self, code: str, language: str = "python") -> SandboxResult:
        try:
            execution = self._sandbox.run_code(code)
            stdout_parts = getattr(execution.logs, "stdout", []) or []
            stderr_parts = getattr(execution.logs, "stderr", []) or []
            stdout = "\n".join(stdout_parts) if isinstance(stdout_parts, list) else str(stdout_parts)
            stderr = "\n".join(stderr_parts) if isinstance(stderr_parts, list) else str(stderr_parts)
            exit_code = 0 if execution.error is None else 1
            return SandboxResult(stdout=stdout, stderr=stderr, exit_code=exit_code)
        except Exception as exc:
            raise SandboxExecutionError(f"E2B code execution failed: {exc}") from exc

    def execute_command(self, command: str, args: list[str] | None = None) -> SandboxResult:
        try:
            full_cmd = command if not args else f"{command} {' '.join(shlex.quote(a) for a in args)}"
            result = self._sandbox.commands.run(full_cmd)
            return SandboxResult(
                stdout=getattr(result, "stdout", "") or "",
                stderr=getattr(result, "stderr", "") or "",
                exit_code=getattr(result, "exit_code", 0) or 0,
            )
        except Exception as exc:
            raise SandboxExecutionError(f"E2B command execution failed: {exc}") from exc

    def list_files(self, path: str = "/") -> list[FileInfo]:
        try:
            entries = self._sandbox.files.list(path)
            return [
                FileInfo(
                    path=getattr(e, "path", str(e)) if not isinstance(e, str) else e,
                    is_dir=getattr(e, "is_dir", False) if not isinstance(e, str) else False,
                    size=getattr(e, "size", None) if not isinstance(e, str) else None,
                )
                for e in entries
            ]
        except Exception as exc:
            raise SandboxExecutionError(f"E2B list_files failed: {exc}") from exc

    def read_file(self, path: str) -> bytes:
        try:
            content = self._sandbox.files.read(path)
            return content if isinstance(content, bytes) else content.encode()
        except Exception as exc:
            raise SandboxExecutionError(f"E2B read_file failed: {exc}") from exc

    def write_file(self, path: str, content: bytes | str) -> None:
        try:
            self._sandbox.files.write(path, content)
        except Exception as exc:
            raise SandboxExecutionError(f"E2B write_file failed: {exc}") from exc

    def upload_file(self, local_path: str, remote_path: str) -> None:
        try:
            data = pathlib.Path(local_path).read_bytes()
            self._sandbox.files.write(remote_path, data)
        except Exception as exc:
            raise SandboxExecutionError(f"E2B upload_file failed: {exc}") from exc

    def download_file(self, remote_path: str, local_path: str) -> None:
        try:
            content = self._sandbox.files.read(remote_path)
            data = content if isinstance(content, bytes) else content.encode()
            pathlib.Path(local_path).write_bytes(data)
        except Exception as exc:
            raise SandboxExecutionError(f"E2B download_file failed: {exc}") from exc

    def snapshot(self) -> SnapshotInfo:
        raise FeatureNotSupportedError(
            "Snapshots are not directly supported by the E2B code-interpreter SDK"
        )

    def destroy(self) -> None:
        try:
            if self._sandbox:
                self._sandbox.kill()
        except Exception:
            pass
        self._sandbox = None
