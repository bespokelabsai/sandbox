"""Protocols implemented by sandbox backend clients and sessions."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from bespokelabs.sandbox.types import (
    FileInfo,
    JobSpec,
    JobStatus,
    SandboxConfig,
    SandboxListing,
    SandboxResult,
    SnapshotInfo,
)


@runtime_checkable
class SandboxBackendSession(Protocol):
    """A live sandbox instance on one backend."""

    @property
    def provider_resource_id(self) -> str | None:
        """Provider-owned resource ID, or ``None`` for local-only sessions."""
        ...

    def execute_code(self, code: str, language: str) -> SandboxResult:
        ...

    def execute_command(
        self, command: str, args: list[str] | None
    ) -> SandboxResult:
        ...

    def list_files(self, path: str) -> list[FileInfo]:
        ...

    def read_file(self, path: str) -> bytes:
        ...

    def write_file(self, path: str, content: bytes | str) -> None:
        ...

    def upload_file(self, local_path: str, remote_path: str) -> None:
        ...

    def download_file(self, remote_path: str, local_path: str) -> None:
        ...

    def snapshot(self) -> SnapshotInfo:
        ...

    def session_state(self) -> dict:
        ...

    def destroy(self) -> None:
        ...


@runtime_checkable
class SandboxBackendClient(Protocol):
    """Factory for sandbox sessions on one backend.

    Construction verifies the backend is usable (SDK importable,
    platform requirements met) without doing network I/O.  Provider
    connections are established lazily in create() and reused across
    calls, so one client can cheaply create many sessions.

    resume() reattaches to a running sandbox from the payload produced
    by a session's session_state().  Backends without reattach support
    raise FeatureNotSupportedError from both methods.
    """

    def create(self, config: SandboxConfig) -> SandboxBackendSession:
        ...

    def resume(self, data: dict) -> SandboxBackendSession:
        ...


@runtime_checkable
class SandboxJobBackend(Protocol):
    """A backend client that runs detached jobs natively (e.g. ``gcp_batch``).

    ``SandboxClient``'s job methods raise ``FeatureNotSupportedError`` on a
    backend client without these methods.

    ``submit_job`` returns a job id that any process can pass to the other
    methods; it must carry everything needed to find the job again.
    ``cancel_job`` stops the job and returns its status after the request:
    terminal where cancellation is synchronous, or ``running`` with a
    ``cancelling`` reason where the provider cancels asynchronously.
    """

    def submit_job(self, spec: JobSpec) -> str:
        ...

    def job_status(self, job_id: str) -> JobStatus:
        ...

    def job_logs(self, job_id: str, tail: int | None) -> str:
        ...

    def cancel_job(self, job_id: str) -> JobStatus:
        ...


@runtime_checkable
class SandboxListingBackend(Protocol):
    """A backend client that can list its running sandboxes by label.

    ``list_sandboxes`` returns every running sandbox carrying *all* of the
    given labels, across every page the provider returns, or raises: a
    partial list is never returned.  ``SandboxClient.list_sandboxes`` raises
    ``FeatureNotSupportedError`` on a backend client without it.
    """

    def list_sandboxes(self, labels: dict[str, str]) -> list[SandboxListing]:
        ...
