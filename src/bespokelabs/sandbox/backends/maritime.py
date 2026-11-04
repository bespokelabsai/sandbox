"""Maritime sandbox backend.

Talks to the Maritime Sandboxes REST API with the standard library only, so
the backend needs no extra.  The control plane (``api.maritime.sh/v1``)
creates, lists and ends sandboxes with an API key; every sandbox then has its
own data plane URL for commands and files, authorized by that sandbox's
secret.
"""

from __future__ import annotations

import json
import math
import os
import pathlib
import posixpath
import shlex
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from typing import Any

from bespokelabs.sandbox.exceptions import (
    FeatureNotSupportedError,
    SandboxAuthenticationError,
    SandboxConfigurationError,
    SandboxConnectionError,
    SandboxCreationError,
    SandboxError,
    SandboxExecutionError,
    SandboxNotFoundError,
    SandboxTimeoutError,
)
from bespokelabs.sandbox.types import (
    ExposedPort,
    FileInfo,
    NetworkPolicy,
    SandboxConfig,
    SandboxListing,
    SandboxResult,
    SandboxSessionState,
    SnapshotInfo,
)

_BACKEND = "maritime"
_DEFAULT_API_URL = "https://api.maritime.sh"
_DEFAULT_TEMPLATE = "base"
_DEFAULT_HOME = "/home/user"
_API_TIMEOUT_SECS = 60.0
# The API runs a foreground command for at most an hour.
_MAX_COMMAND_SECS = 3600
_MIN_LIFETIME_SECS = 60
_MAX_LIFETIME_SECS = 86400
_MAX_VCPU = 16
_MIN_MEMORY_MIB = 1024
_MAX_MEMORY_MIB = 65536
_MIN_DISK_GIB = 5
_LIST_PAGE_SIZE = 100

# Error codes the API documents as safe to retry.
_RETRYABLE_CODES = frozenset(
    {
        "rate_limited",
        "concurrency_limit",
        "out_of_capacity",
        "no_ready_machine",
        "sandbox_starting",
        "pause_failed",
        "internal_error",
    }
)


class _MaritimeApiError(Exception):
    """An unsuccessful response from the Maritime API."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        body: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.body = body or {}


def _request(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    payload: Any = None,
    raw: bytes | None = None,
    timeout: float = _API_TIMEOUT_SECS,
) -> bytes:
    """Send one request and return the body, raising on any non-2xx reply."""
    data = None
    all_headers = {"Accept": "application/json", **headers}
    if raw is not None:
        data = raw
        all_headers["Content-Type"] = "application/octet-stream"
    elif payload is not None:
        data = json.dumps(payload).encode()
        all_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=data, headers=all_headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        text = exc.read().decode(errors="replace").strip()
        try:
            body = json.loads(text) if text else {}
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        message = body.get("message") or text or str(exc.reason)
        raise _MaritimeApiError(
            f"Maritime API returned HTTP {exc.code}: {message}",
            status_code=exc.code,
            code=body.get("code"),
            body=body,
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _MaritimeApiError(
            f"Could not connect to the Maritime API: {exc}"
        ) from exc


def _json(body: bytes) -> dict:
    if not body:
        return {}
    try:
        value = json.loads(body)
    except ValueError as exc:
        raise _MaritimeApiError(
            "Maritime API returned a response that is not JSON"
        ) from exc
    if not isinstance(value, dict):
        raise _MaritimeApiError("Maritime API returned an unexpected response")
    return value


def _classify(
    exc: _MaritimeApiError, *, op: str, default: type[SandboxError]
) -> SandboxError:
    """Map an API error onto the SDK's exception types."""
    kwargs: dict[str, Any] = {"backend": _BACKEND, "op": op}
    if exc.code:
        kwargs["context"] = {"code": exc.code}
    retryable = exc.code in _RETRYABLE_CODES or exc.status_code in (
        429,
        502,
        503,
    )
    if exc.status_code is None:
        return SandboxConnectionError(str(exc), retryable=True, **kwargs)
    if exc.status_code in (401, 403):
        return SandboxAuthenticationError(str(exc), **kwargs)
    if exc.status_code == 404 or (exc.code or "").endswith("_not_found"):
        return SandboxNotFoundError(str(exc), **kwargs)
    if exc.code == "command_timeout" or exc.status_code == 504:
        return SandboxTimeoutError(str(exc), **kwargs)
    if exc.code == "invalid_request" and op == "create":
        return SandboxConfigurationError(str(exc), **kwargs)
    return default(str(exc), retryable=retryable, **kwargs)


def _network_payload(policy: NetworkPolicy) -> dict:
    if policy.mode == "public":
        return {"egress": "all"}
    if policy.mode == "none":
        return {"egress": "none"}
    if policy.cidrs:
        raise FeatureNotSupportedError(
            "Maritime allowlists take host names (pypi.org, *.pypi.org), "
            "not CIDR ranges",
            backend=_BACKEND,
            op="network",
        )
    return {"egress": "allowlist", "hosts": list(policy.domains)}


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    # Python 3.10 rejects fractional seconds longer than six digits.
    if "." in text:
        head, _, tail = text.partition(".")
        digits = "".join(ch for ch in tail if ch.isdigit())
        zone = tail[len(digits) :]
        text = f"{head}.{digits[:6]}{zone}"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


class _ControlPlane:
    """The account-level API: sandboxes, ports and snapshots."""

    def __init__(self, *, api_key: str, base_url: str) -> None:
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._base = base_url.rstrip("/") + "/v1"

    def call(
        self,
        method: str,
        path: str,
        payload: Any = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> dict:
        body = _request(
            method,
            self._base + path,
            headers={**self._headers, **(headers or {})},
            payload=payload,
        )
        return _json(body)


def _api_settings() -> tuple[str, str]:
    api_key = os.environ.get("MARITIME_API_KEY", "").strip()
    if not api_key:
        raise SandboxAuthenticationError(
            "MARITIME_API_KEY environment variable is not set",
            backend=_BACKEND,
            op="create",
        )
    base_url = os.environ.get("MARITIME_API_URL", "").strip()
    return api_key, base_url or _DEFAULT_API_URL


class MaritimeClient:
    """Factory for Maritime sandboxes.

    Reads ``MARITIME_API_KEY`` (and optionally ``MARITIME_API_URL``) on the
    first create() or resume(); construction does no network I/O.
    """

    supports_dockerfile = False
    network_modes = frozenset({"public", "none", "allowlist"})
    network_option_keys = frozenset({"network"})

    def __init__(self) -> None:
        # Only the standard library is needed, so there is nothing to import.
        self._control: _ControlPlane | None = None

    def _ensure_control(self) -> _ControlPlane:
        if self._control is None:
            api_key, base_url = _api_settings()
            self._control = _ControlPlane(api_key=api_key, base_url=base_url)
        return self._control

    def create(self, config: SandboxConfig) -> MaritimeSession:
        payload = _create_payload(config)
        control = self._ensure_control()
        try:
            # The idempotency key makes a retried create return the same
            # sandbox instead of starting a second one.
            sandbox = control.call(
                "POST",
                "/sandboxes",
                payload,
                headers={"Idempotency-Key": f"bespoke-{uuid.uuid4().hex}"},
            )
        except _MaritimeApiError as exc:
            raise _classify(
                exc, op="create", default=SandboxCreationError
            ) from exc
        if not sandbox.get("id") or not sandbox.get("dataPlaneUrl"):
            raise SandboxCreationError(
                "Maritime created a sandbox without an id or data plane URL",
                backend=_BACKEND,
                op="create",
            )
        return MaritimeSession(
            control=control,
            sandbox=sandbox,
            workdir=config.workdir,
            env_vars=dict(config.env_vars),
            command_timeout_secs=config.timeout_secs,
        )

    def resume(self, data: dict) -> MaritimeSession:
        sandbox_id = data.get("sandbox_id")
        if not sandbox_id:
            raise SandboxConfigurationError(
                "Maritime session state has no sandbox_id",
                backend=_BACKEND,
                op="resume",
            )
        control = self._ensure_control()
        try:
            sandbox = control.call(
                "GET", f"/sandboxes/{urllib.parse.quote(sandbox_id)}"
            )
        except _MaritimeApiError as exc:
            raise _classify(
                exc, op="resume", default=SandboxCreationError
            ) from exc
        if sandbox.get("state") in ("ended", "error"):
            raise SandboxNotFoundError(
                f"Maritime sandbox '{sandbox_id}' is {sandbox.get('state')}",
                backend=_BACKEND,
                op="resume",
            )
        # A paused sandbox wakes on its first data plane call.
        return MaritimeSession(
            control=control,
            sandbox=sandbox,
            workdir=data.get("workdir"),
            env_vars=dict(data.get("env_vars") or {}),
            command_timeout_secs=int(
                data.get("command_timeout_secs") or _MAX_COMMAND_SECS
            ),
        )

    def list_sandboxes(self, labels: dict[str, str]) -> list[SandboxListing]:
        """Every live sandbox whose metadata carries all of *labels*.

        Pass labels at creation with ``backend_options={"metadata": {...}}``.
        Maritime filters on metadata server-side; each page is followed to
        the end, and a sandbox whose own metadata does not match is dropped.
        """
        control = self._ensure_control()
        query: list[tuple[str, str]] = [
            ("limit", str(_LIST_PAGE_SIZE)),
            *(
                ("state", state)
                for state in (
                    "starting",
                    "running",
                    "pausing",
                    "paused",
                    "resuming",
                )
            ),
            *((f"metadata[{key}]", value) for key, value in labels.items()),
        ]
        listings: list[SandboxListing] = []
        cursor: str | None = None
        while True:
            params = list(query) + ([("cursor", cursor)] if cursor else [])
            try:
                page = control.call(
                    "GET", "/sandboxes?" + urllib.parse.urlencode(params)
                )
            except _MaritimeApiError as exc:
                raise _classify(
                    exc, op="list", default=SandboxExecutionError
                ) from exc
            for sandbox in page.get("data") or []:
                sandbox_id = sandbox.get("id")
                if not sandbox_id:
                    raise SandboxExecutionError(
                        "Maritime listed a sandbox without an id",
                        backend=_BACKEND,
                        op="list",
                    )
                own = {
                    str(k): str(v)
                    for k, v in (sandbox.get("metadata") or {}).items()
                }
                if any(own.get(k) != v for k, v in labels.items()):
                    continue
                listings.append(
                    SandboxListing(
                        provider_resource_id=str(sandbox_id),
                        labels=own,
                        state=sandbox.get("state"),
                        created_at=_parse_time(sandbox.get("createdAt")),
                        session_state=SandboxSessionState(
                            backend=_BACKEND,
                            data={"sandbox_id": str(sandbox_id)},
                        ),
                    )
                )
            cursor = page.get("nextCursor")
            if not cursor:
                return listings


def _create_payload(config: SandboxConfig) -> dict:
    if config.gpu:
        raise FeatureNotSupportedError(
            "Maritime sandboxes have no GPUs",
            backend=_BACKEND,
            op="create",
        )
    if config.dockerfile:
        raise FeatureNotSupportedError(
            "Maritime cannot build a Dockerfile; pass a Maritime template "
            "as template=, or a saved snapshot as snapshot_id=",
            backend=_BACKEND,
            op="create",
        )
    if config.image:
        raise FeatureNotSupportedError(
            "Maritime does not boot OCI images; pass a Maritime template "
            "(base, code-interpreter, desktop-linux) as template=, or a "
            "saved snapshot as snapshot_id=",
            backend=_BACKEND,
            op="create",
        )
    sources = [
        value for value in (config.snapshot_id, config.template) if value
    ]
    if len(sources) > 1:
        raise SandboxConfigurationError(
            "Pass either snapshot_id or template to Maritime, not both",
            backend=_BACKEND,
            op="create",
        )
    vcpu = min(max(math.ceil(config.cpu), 1), _MAX_VCPU)
    # Memory is set in whole GiB.
    memory_mib = min(
        max(math.ceil(config.memory_mb / 1024) * 1024, _MIN_MEMORY_MIB),
        _MAX_MEMORY_MIB,
    )
    payload: dict[str, Any] = {
        "from": sources[0] if sources else _DEFAULT_TEMPLATE,
        "vcpu": vcpu,
        "memoryMib": memory_mib,
        "metadata": {"created_by": "bespokelabs-sandbox"},
    }
    if config.disk_mb:
        payload["diskGib"] = max(
            math.ceil(config.disk_mb / 1024), _MIN_DISK_GIB
        )
    if config.network is not None:
        payload["network"] = _network_payload(config.network)
    elif not config.allow_internet:
        payload["network"] = {"egress": "none"}
    if config.env_vars:
        payload["env"] = dict(config.env_vars)
    if config.timeout_secs_explicit:
        # An explicit timeout is a hard deadline for the sandbox.
        payload["timeLimitSeconds"] = min(
            max(int(config.timeout_secs), _MIN_LIFETIME_SECS),
            _MAX_LIFETIME_SECS,
        )
    options = dict(config.backend_options or {})
    metadata = options.pop("metadata", None)
    if metadata:
        payload["metadata"].update(
            {str(k): str(v) for k, v in dict(metadata).items()}
        )
    payload.update(options)
    return payload


class MaritimeSession:
    """One live Maritime sandbox."""

    def __init__(
        self,
        *,
        control: _ControlPlane,
        sandbox: dict,
        workdir: str | None,
        env_vars: dict[str, str],
        command_timeout_secs: int,
    ) -> None:
        self._control = control
        self._sandbox_id: str | None = str(sandbox["id"])
        self._data_plane = str(sandbox["dataPlaneUrl"]).rstrip("/")
        self._secret = str(sandbox.get("secret") or "")
        self._workdir = workdir
        self._env_vars = env_vars
        self._command_timeout_secs = min(
            max(int(command_timeout_secs), 1), _MAX_COMMAND_SECS
        )

    @property
    def provider_resource_id(self) -> str | None:
        """Return the Maritime sandbox id while it is attached."""
        return self._sandbox_id

    # -- Transport ---------------------------------------------------------

    def _alive_id(self, op: str) -> str:
        if self._sandbox_id is None:
            raise SandboxNotFoundError(
                "This Maritime sandbox was destroyed",
                backend=_BACKEND,
                op=op,
            )
        return self._sandbox_id

    def _data(
        self,
        method: str,
        path: str,
        *,
        op: str,
        payload: Any = None,
        raw: bytes | None = None,
        timeout: float = _API_TIMEOUT_SECS,
    ) -> bytes:
        self._alive_id(op)
        try:
            return _request(
                method,
                self._data_plane + path,
                headers={"X-Maritime-Token": self._secret},
                payload=payload,
                raw=raw,
                timeout=timeout,
            )
        except _MaritimeApiError as exc:
            raise _classify(exc, op=op, default=SandboxExecutionError) from exc

    def _abs(self, path: str) -> str:
        if path.startswith("/"):
            return path
        return posixpath.join(self._workdir or _DEFAULT_HOME, path)

    def _file_url(self, path: str, endpoint: str = "/files") -> str:
        return (
            endpoint + "?" + urllib.parse.urlencode({"path": self._abs(path)})
        )

    # -- Execution ---------------------------------------------------------

    def execute_code(
        self, code: str, language: str = "python"
    ) -> SandboxResult:
        interpreter = "python3" if language == "python" else language
        return self.execute_command(interpreter, ["-c", code])

    def execute_command(
        self, command: str, args: list[str] | None = None
    ) -> SandboxResult:
        command_line = command if not args else shlex.join([command, *args])
        body: dict[str, Any] = {
            # The API runs a string through /bin/sh; bash matches the other
            # backends' shell.
            "cmd": ["bash", "-c", command_line],
            "timeoutSeconds": self._command_timeout_secs,
        }
        if self._workdir:
            body["cwd"] = self._workdir
        if self._env_vars:
            body["env"] = dict(self._env_vars)
        result = _json(
            self._data(
                "POST",
                "/commands",
                op="exec",
                payload=body,
                timeout=self._command_timeout_secs + 30,
            )
        )
        exit_code = result.get("exitCode")
        if exit_code is None:
            # Killed by a signal: report it the way a shell would.
            exit_code = 137 if result.get("signal") == "SIGKILL" else 1
        return SandboxResult(
            stdout=result.get("stdout") or "",
            stderr=result.get("stderr") or "",
            exit_code=int(exit_code),
        )

    # -- Files -------------------------------------------------------------

    def list_files(self, path: str = "/") -> list[FileInfo]:
        entries: list[FileInfo] = []
        cursor: str | None = None
        while True:
            params = {"path": self._abs(path)}
            if cursor:
                params["cursor"] = cursor
            page = _json(
                self._data(
                    "GET",
                    "/files/list?" + urllib.parse.urlencode(params),
                    op="list_files",
                )
            )
            for entry in page.get("data") or []:
                is_dir = entry.get("type") == "dir"
                entries.append(
                    FileInfo(
                        path=str(entry.get("path")),
                        is_dir=is_dir,
                        size=None if is_dir else entry.get("sizeBytes"),
                    )
                )
            cursor = page.get("nextCursor")
            if not cursor:
                return entries

    def read_file(self, path: str) -> bytes:
        return self._data("GET", self._file_url(path), op="read_file")

    def write_file(self, path: str, content: bytes | str) -> None:
        data = content.encode() if isinstance(content, str) else content
        self._data("PUT", self._file_url(path), op="write_file", raw=data)

    def upload_file(self, local_path: str, remote_path: str) -> None:
        try:
            data = pathlib.Path(local_path).read_bytes()
        except OSError as exc:
            raise SandboxExecutionError(
                f"Cannot read local file {local_path}: {exc}",
                backend=_BACKEND,
                op="upload_file",
            ) from exc
        self._data(
            "PUT", self._file_url(remote_path), op="upload_file", raw=data
        )

    def download_file(self, remote_path: str, local_path: str) -> None:
        data = self._data("GET", self._file_url(remote_path), op="download")
        try:
            pathlib.Path(local_path).write_bytes(data)
        except OSError as exc:
            raise SandboxExecutionError(
                f"Cannot write local file {local_path}: {exc}",
                backend=_BACKEND,
                op="download_file",
            ) from exc

    # -- Control plane -----------------------------------------------------

    def _control_call(
        self, method: str, suffix: str, payload: Any = None, *, op: str
    ) -> dict:
        sandbox_id = self._alive_id(op)
        try:
            return self._control.call(
                method,
                f"/sandboxes/{urllib.parse.quote(sandbox_id)}{suffix}",
                payload,
            )
        except _MaritimeApiError as exc:
            raise _classify(exc, op=op, default=SandboxExecutionError) from exc

    def expose_port(self, port: int) -> ExposedPort:
        """Return a private URL for *port*.

        The URL carries the sandbox's access token in its query string, so
        treat it as a secret.
        """
        result = self._control_call(
            "POST",
            "/urls",
            {"port": int(port), "token": True},
            op="expose_port",
        )
        return ExposedPort(port=int(port), url=str(result["url"]))

    def set_network(self, policy: NetworkPolicy) -> None:
        self._control_call(
            "PATCH",
            "",
            {"network": _network_payload(policy)},
            op="set_network",
        )

    def snapshot(self) -> SnapshotInfo:
        """Save the sandbox's disk as a named snapshot.

        Pass the returned ``snapshot_id`` as ``snapshot_id=`` to start a new
        sandbox from it.  Snapshots outlive the sandbox.
        """
        name = f"bespoke-{uuid.uuid4().hex[:16]}"
        result = self._control_call(
            "POST", "/save", {"name": name}, op="snapshot"
        )
        return SnapshotInfo(
            snapshot_id=str(result.get("name") or name),
            backend=_BACKEND,
            created_at=result.get("createdAt"),
        )

    def session_state(self) -> dict:
        state: dict[str, Any] = {
            "sandbox_id": self._alive_id("session_state"),
            "command_timeout_secs": self._command_timeout_secs,
        }
        if self._workdir:
            state["workdir"] = self._workdir
        if self._env_vars:
            state["env_vars"] = dict(self._env_vars)
        return state

    def destroy(self) -> None:
        if self._sandbox_id is None:
            return
        try:
            self._control_call("DELETE", "", op="destroy")
        except SandboxNotFoundError:
            pass
        # Kept on failure, so a later destroy() can retry.
        self._sandbox_id = None
