"""Async interface to sandboxes.

AsyncSandboxClient / AsyncSandbox mirror SandboxClient / Sandbox with
coroutine methods.  Backend SDKs are synchronous, so every blocking
call is offloaded to a worker thread via asyncio.to_thread — the event
loop is never blocked, and many sandboxes can be created and driven
concurrently with asyncio.gather.

Usage:
    from bespokelabs.sandbox import AsyncSandbox, AsyncSandboxClient

    async def main():
        client = AsyncSandboxClient("docker")
        async with await client.create(image="python:3.12-slim") as sb:
            result = await sb.execute_code('print("hello")')
            print(result.stdout)

        # One-step creation:
        async with await AsyncSandbox.create("local") as sb:
            await sb.execute_command("echo hi")
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TypeVar, overload

from bespokelabs.sandbox.backends import BACKENDS
from bespokelabs.sandbox.exceptions import SandboxError
from bespokelabs.sandbox.presets import SandboxPreset
from bespokelabs.sandbox.protocols import SandboxBackendClient
from bespokelabs.sandbox.sandbox import Sandbox
from bespokelabs.sandbox.types import (
    FileInfo,
    SandboxResult,
    SandboxSessionState,
    SnapshotInfo,
)

T = TypeVar("T")


class AsyncSandbox:
    """Async wrapper around a live :class:`Sandbox` session.

    Obtain instances via :meth:`AsyncSandboxClient.create` or the
    one-step :meth:`AsyncSandbox.create` classmethod.  Wrapping an
    already-created synchronous Sandbox also works:

        sb = AsyncSandbox(existing_sync_sandbox)

    Supports ``async with`` for cleanup; the sandbox is destroyed on
    exit.
    """

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox

    @classmethod
    async def create(
        cls,
        backend: str,
        *,
        preset: str | SandboxPreset | None = None,
        cpu: float | None = None,
        memory_mb: int | None = None,
        disk_mb: int | None = None,
        timeout_secs: int | None = None,
        image: str | None = None,
        env_vars: dict[str, str] | None = None,
        allow_internet: bool | None = None,
        app_name: str | None = None,
        template: str | None = None,
        snapshot_id: str | None = None,
        workdir: str | None = None,
        backend_options: dict | None = None,
        files: dict[str, bytes | str] | None = None,
        git_repo: str | None = None,
        git_ref: str | None = None,
    ) -> AsyncSandbox:
        """One-step async creation; mirrors ``Sandbox(backend, ...)``."""
        return await AsyncSandboxClient(backend).create(
            preset=preset,
            cpu=cpu,
            memory_mb=memory_mb,
            disk_mb=disk_mb,
            timeout_secs=timeout_secs,
            image=image,
            env_vars=env_vars,
            allow_internet=allow_internet,
            app_name=app_name,
            template=template,
            snapshot_id=snapshot_id,
            workdir=workdir,
            backend_options=backend_options,
            files=files,
            git_repo=git_repo,
            git_ref=git_ref,
        )

    @classmethod
    async def resume(cls, state: SandboxSessionState) -> AsyncSandbox:
        """Reattach to a running sandbox from serialized session state."""
        return await AsyncSandboxClient(state.backend).resume(state)

    # -- Core operations ---------------------------------------------------

    @overload
    async def execute_code(self, code: str, language: str = "python") -> SandboxResult: ...

    @overload
    async def execute_code(self, code: str, language: str = "python", *, return_type: type[T]) -> T: ...

    async def execute_code(
        self,
        code: str,
        language: str = "python",
        *,
        return_type: type[T] | None = None,
    ) -> SandboxResult | T:
        """Async version of :meth:`Sandbox.execute_code`."""
        return await asyncio.to_thread(
            self._sandbox.execute_code, code, language, return_type=return_type
        )

    @overload
    async def execute_command(self, command: str, args: list[str] | None = None) -> SandboxResult: ...

    @overload
    async def execute_command(
        self, command: str, args: list[str] | None = None, *, return_type: type[T], inject_schema: bool = ...
    ) -> T: ...

    async def execute_command(
        self,
        command: str,
        args: list[str] | None = None,
        *,
        return_type: type[T] | None = None,
        inject_schema: bool = False,
    ) -> SandboxResult | T:
        """Async version of :meth:`Sandbox.execute_command`."""
        return await asyncio.to_thread(
            self._sandbox.execute_command,
            command,
            args,
            return_type=return_type,
            inject_schema=inject_schema,
        )

    # -- File operations ---------------------------------------------------

    async def list_files(self, path: str = "/") -> list[FileInfo]:
        """Async version of :meth:`Sandbox.list_files`."""
        return await asyncio.to_thread(self._sandbox.list_files, path)

    async def read_file(self, path: str) -> bytes:
        """Async version of :meth:`Sandbox.read_file`."""
        return await asyncio.to_thread(self._sandbox.read_file, path)

    async def write_file(self, path: str, content: bytes | str) -> None:
        """Async version of :meth:`Sandbox.write_file`."""
        return await asyncio.to_thread(self._sandbox.write_file, path, content)

    async def upload_file(self, local_path: str, remote_path: str) -> None:
        """Async version of :meth:`Sandbox.upload_file`."""
        await asyncio.to_thread(self._sandbox.upload_file, local_path, remote_path)

    async def download_file(self, remote_path: str, local_path: str) -> None:
        """Async version of :meth:`Sandbox.download_file`."""
        await asyncio.to_thread(self._sandbox.download_file, remote_path, local_path)

    async def upload_dir(self, local_dir: str | Path, remote_dir: str, *, method: str = "auto") -> int:
        """Async version of :meth:`Sandbox.upload_dir`."""
        return await asyncio.to_thread(
            self._sandbox.upload_dir, local_dir, remote_dir, method=method
        )

    async def download_dir(self, remote_dir: str, local_dir: str | Path, *, method: str = "auto") -> int:
        """Async version of :meth:`Sandbox.download_dir`."""
        return await asyncio.to_thread(
            self._sandbox.download_dir, remote_dir, local_dir, method=method
        )

    # -- Lifecycle ---------------------------------------------------------

    async def snapshot(self) -> SnapshotInfo:
        """Async version of :meth:`Sandbox.snapshot`."""
        return await asyncio.to_thread(self._sandbox.snapshot)

    def session_state(self) -> SandboxSessionState:
        """Serialize a reattachable handle to this running sandbox.

        Synchronous — the state is assembled from in-memory handles
        without any I/O.
        """
        return self._sandbox.session_state()

    async def destroy(self) -> None:
        """Terminate and clean up the sandbox."""
        await asyncio.to_thread(self._sandbox.destroy)

    # -- Context manager ---------------------------------------------------

    async def __aenter__(self) -> AsyncSandbox:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.destroy()

    # -- Properties --------------------------------------------------------

    @property
    def backend_name(self) -> str:
        return self._sandbox.backend_name

    @property
    def is_alive(self) -> bool:
        return self._sandbox.is_alive


class AsyncSandboxClient:
    """Async counterpart of :class:`SandboxClient`.

    Construction validates the backend name without any I/O.  The
    backend SDK is imported during the first create() (in a worker
    thread), so BackendNotInstalledError surfaces there rather than at
    construction.  As with SandboxClient, provider-level connections
    are established once and reused across create() calls:

        client = AsyncSandboxClient("daytona")
        sandboxes = await asyncio.gather(*(client.create() for _ in range(10)))

    Use one client per event loop.
    """

    def __init__(self, backend: str) -> None:
        backend = backend.lower().strip()
        if backend not in BACKENDS:
            raise SandboxError(
                f"Unknown backend '{backend}'. Choose from: {', '.join(BACKENDS)}"
            )
        self._backend_name = backend
        self._backend_client: SandboxBackendClient | None = None
        self._init_lock = asyncio.Lock()

    @property
    def backend_name(self) -> str:
        return self._backend_name

    async def create(
        self,
        *,
        preset: str | SandboxPreset | None = None,
        cpu: float | None = None,
        memory_mb: int | None = None,
        disk_mb: int | None = None,
        timeout_secs: int | None = None,
        image: str | None = None,
        env_vars: dict[str, str] | None = None,
        allow_internet: bool | None = None,
        app_name: str | None = None,
        template: str | None = None,
        snapshot_id: str | None = None,
        workdir: str | None = None,
        backend_options: dict | None = None,
        files: dict[str, bytes | str] | None = None,
        git_repo: str | None = None,
        git_ref: str | None = None,
    ) -> AsyncSandbox:
        """Create a new sandbox session.

        Accepts the same keyword arguments as ``Sandbox(...)``.
        """
        backend_client = await self._get_backend_client()
        sandbox = await asyncio.to_thread(
            Sandbox,
            self._backend_name,
            preset=preset,
            cpu=cpu,
            memory_mb=memory_mb,
            disk_mb=disk_mb,
            timeout_secs=timeout_secs,
            image=image,
            env_vars=env_vars,
            allow_internet=allow_internet,
            app_name=app_name,
            template=template,
            snapshot_id=snapshot_id,
            workdir=workdir,
            backend_options=backend_options,
            files=files,
            git_repo=git_repo,
            git_ref=git_ref,
            _backend_client=backend_client,
        )
        return AsyncSandbox(sandbox)

    async def resume(self, state: SandboxSessionState) -> AsyncSandbox:
        """Reattach to a running sandbox from serialized session state."""
        backend_client = await self._get_backend_client()
        sandbox = await asyncio.to_thread(
            Sandbox._resume_with, self._backend_name, backend_client, state
        )
        return AsyncSandbox(sandbox)

    # -- Internal ----------------------------------------------------------

    async def _get_backend_client(self) -> SandboxBackendClient:
        # Double-checked so only the first construction (SDK import,
        # availability checks) is serialized; subsequent create() calls
        # proceed concurrently.
        if self._backend_client is None:
            async with self._init_lock:
                if self._backend_client is None:
                    self._backend_client = await asyncio.to_thread(
                        BACKENDS[self._backend_name]
                    )
        return self._backend_client
