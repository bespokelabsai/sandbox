"""Control-plane errors and safe provider-error serialization."""

from __future__ import annotations

import re
from typing import Any

from bespokelabs.sandbox.exceptions import (
    ErrorCode,
    ErrorOutcome,
    SandboxCreationError,
    SandboxError,
    SandboxExecutionError,
)

_SAFE_PROVIDER_CONTEXT_KEYS = frozenset(
    {
        "cleanup_status",
        "exit_code",
        "provider_code",
        "provider_resource_id",
        "provider_status",
    }
)
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:/-]{1,255}$")

_PUBLIC_MESSAGES = {
    ErrorCode.AUTHENTICATION: "Provider authentication failed.",
    ErrorCode.BACKEND_NOT_INSTALLED: "Provider backend is unavailable.",
    ErrorCode.CONFIGURATION: "Provider configuration is invalid.",
    ErrorCode.CONNECTION: "Provider connection failed.",
    ErrorCode.CREATION_FAILED: "Provider sandbox creation failed.",
    ErrorCode.EXECUTION_FAILED: "Provider execution failed.",
    ErrorCode.NOT_FOUND: "Provider resource was not found.",
    ErrorCode.TIMEOUT: "Provider request timed out.",
}


def normalize_provider_error(
    exc: Exception, *, backend: str, op: str
) -> SandboxError:
    """Return a structured provider error without exposing an opaque message."""
    if isinstance(exc, SandboxError):
        if exc.backend is None:
            exc.backend = backend
        if exc.op is None:
            exc.op = op
        if op == "create" and exc.outcome is ErrorOutcome.UNKNOWN:
            exc.retryable = False
        return exc
    error_type = (
        SandboxCreationError if op == "create" else SandboxExecutionError
    )
    return error_type(
        "Provider sandbox operation failed.",
        backend=backend,
        op=op,
        outcome=ErrorOutcome.UNKNOWN,
    )


def provider_error_payload(exc: SandboxError) -> dict[str, Any]:
    """Serialize only stable fields and allowlisted, non-secret context."""
    code = exc.code if isinstance(exc.code, ErrorCode) else ErrorCode.UNKNOWN
    context: dict[str, Any] = {}
    for key, value in exc.context.items():
        if key not in _SAFE_PROVIDER_CONTEXT_KEYS:
            continue
        if key in {"exit_code", "provider_status"}:
            if isinstance(value, int) and not isinstance(value, bool):
                context[key] = value
            continue
        if key == "cleanup_status":
            if value in {"not_found", "deleted", "unknown"}:
                context[key] = value
            continue
        if isinstance(value, str) and _SAFE_IDENTIFIER.fullmatch(value):
            lowered = value.lower()
            if not any(
                marker in lowered
                for marker in ("bsk_live_", "api_key", "password", "secret")
            ):
                context[key] = value
    return {
        "message": _PUBLIC_MESSAGES.get(code, "Provider operation failed."),
        "code": code.value,
        "backend": exc.backend,
        "op": exc.op,
        "retryable": bool(exc.retryable),
        "outcome": exc.outcome.value,
        "context": context,
    }


class ControlPlaneError(Exception):
    """Base class for control-plane failures."""


class AuthenticationError(ControlPlaneError):
    """The supplied API key is missing or invalid."""


class AuthorizationError(ControlPlaneError):
    """The API key does not grant the requested operation."""


class NotFoundError(ControlPlaneError):
    """A tenant-owned resource could not be found."""


class ConflictError(ControlPlaneError):
    """The requested operation conflicts with current state."""


class PolicyDeniedError(ControlPlaneError):
    """An organization policy denied an operation before provider access."""

    def __init__(
        self,
        message: str,
        *,
        policy: str,
        current: str | int | None = None,
        limit: str | int | None = None,
    ) -> None:
        super().__init__(message)
        self.policy = policy
        self.current = current
        self.limit = limit

    def payload(self) -> dict[str, object]:
        return {
            "message": str(self),
            "code": "policy_denied",
            "policy": self.policy,
            "retryable": False,
            "current": self.current,
            "limit": self.limit,
        }
