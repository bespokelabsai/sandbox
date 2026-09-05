"""HTTP client for using many sandboxes with one product API key."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any

from bespokelabs.sandbox.types import SandboxResult


class RemoteSandboxError(Exception):
    """The sandbox control plane rejected or could not serve a request."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str = "unknown",
        backend: str | None = None,
        op: str | None = None,
        retryable: bool = False,
        outcome: str = "unknown",
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.backend = backend
        self.op = op
        self.retryable = retryable
        self.outcome = outcome
        self.context = dict(context or {})


@dataclass
class RemoteSandboxResult(SandboxResult):
    """Execution output plus gateway metering information."""

    request_id: str = ""
    usage: dict[str, str] = field(default_factory=dict)


class RemoteSandboxClient:
    """Client for the hosted, multi-provider sandbox control plane.

    The one API key authenticates every sandbox created through this client;
    provider credentials stay in the control-plane deployment.
    """

    def __init__(
        self, base_url: str, api_key: str, *, timeout_secs: float = 60
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        if not api_key:
            raise ValueError("api_key must not be empty")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_secs = timeout_secs

    def create(
        self,
        backend: str,
        *,
        idempotency_key: str | None = None,
        **config: Any,
    ) -> RemoteSandbox:
        payload = {"backend": backend, **config}
        record = self._request(
            "POST",
            "/v1/sandboxes",
            payload,
            idempotency_key=idempotency_key,
        )
        return RemoteSandbox(self, record["id"], record["backend"])

    def get(self, sandbox_id: str) -> dict:
        return self._request("GET", f"/v1/sandboxes/{sandbox_id}")

    def list(self) -> list[dict]:
        return self._request("GET", "/v1/sandboxes")

    def costs(
        self,
        *,
        group_by: str = "sandbox",
        start: str | None = None,
        end: str | None = None,
    ) -> dict:
        query = {"group_by": group_by}
        if start:
            query["start"] = start
        if end:
            query["end"] = end
        return self._request(
            "GET", f"/v1/costs?{urllib.parse.urlencode(query)}"
        )

    def reconciliation(self) -> dict:
        """Return tenant-scoped provider reconciliation health."""
        return self._request("GET", "/v1/reconciliation")

    def reconcile(self, backend: str) -> dict:
        """Run a configured provider reconciler for this tenant."""
        return self._request("POST", f"/v1/reconciliation/{backend}")

    def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> Any:
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        request = urllib.request.Request(
            self._base_url + path, data=body, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self._timeout_secs
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                detail = json.loads(raw).get("detail", raw.decode())
            except (json.JSONDecodeError, UnicodeDecodeError):
                detail = str(exc)
            if isinstance(detail, dict):
                raise RemoteSandboxError(
                    str(detail.get("message", "Provider operation failed.")),
                    status_code=exc.code,
                    code=str(detail.get("code", "unknown")),
                    backend=detail.get("backend"),
                    op=detail.get("op"),
                    retryable=bool(detail.get("retryable", False)),
                    outcome=str(detail.get("outcome", "unknown")),
                    context=detail.get("context")
                    if isinstance(detail.get("context"), dict)
                    else None,
                ) from exc
            raise RemoteSandboxError(str(detail), status_code=exc.code) from exc
        except urllib.error.URLError as exc:
            raise RemoteSandboxError(
                "control plane unavailable",
                code="connection",
                op="http_request",
                retryable=True,
                outcome="unknown",
            ) from exc
        return json.loads(raw) if raw else None


class RemoteSandbox:
    """A live sandbox addressed through a :class:`RemoteSandboxClient`."""

    def __init__(
        self, client: RemoteSandboxClient, sandbox_id: str, backend: str
    ) -> None:
        self._client = client
        self.id = sandbox_id
        self.backend_name = backend
        self._destroyed = False

    def execute_code(
        self,
        code: str,
        language: str = "python",
        *,
        idempotency_key: str | None = None,
    ) -> RemoteSandboxResult:
        return self._execute(
            {"code": code, "language": language}, idempotency_key
        )

    def execute_command(
        self,
        command: str,
        args: list[str] | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> RemoteSandboxResult:
        return self._execute(
            {"command": command, "args": args}, idempotency_key
        )

    def destroy(self) -> None:
        if not self._destroyed:
            self._client._request("DELETE", f"/v1/sandboxes/{self.id}")
            self._destroyed = True

    @property
    def is_alive(self) -> bool:
        return not self._destroyed

    def __enter__(self) -> RemoteSandbox:
        return self

    def __exit__(self, *exc: object) -> None:
        self.destroy()

    def _execute(
        self, payload: dict, idempotency_key: str | None
    ) -> RemoteSandboxResult:
        if self._destroyed:
            raise RemoteSandboxError("sandbox has been destroyed")
        request_id = idempotency_key or f"req_{uuid.uuid4().hex}"
        result = self._client._request(
            "POST",
            f"/v1/sandboxes/{self.id}/execute",
            payload,
            idempotency_key=request_id,
        )
        return RemoteSandboxResult(
            stdout=result["stdout"],
            stderr=result["stderr"],
            exit_code=result["exit_code"],
            request_id=result["request_id"],
            usage=result["usage"],
        )
