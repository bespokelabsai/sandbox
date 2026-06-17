from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    """Stable, machine-readable error codes.

    Prefer matching on the exception *type* in Python; ``code`` exists for
    logging, metrics, retry policies, and serialized/cross-process contexts
    where the class object isn't available.
    """

    UNKNOWN = "unknown"
    BACKEND_NOT_INSTALLED = "backend_not_installed"
    CONFIGURATION = "configuration"
    CREATION_FAILED = "creation_failed"
    EXECUTION_FAILED = "execution_failed"
    COMMAND_FAILED = "command_failed"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    NOT_FOUND = "not_found"
    FEATURE_NOT_SUPPORTED = "feature_not_supported"
    WORKSPACE = "workspace"


class SandboxError(Exception):
    """Base exception for all sandbox errors.

    Beyond a message, it can carry structured context so callers react
    programmatically instead of parsing strings:

    - ``code``      a stable :class:`ErrorCode` (each subtype sets a default)
    - ``backend``   which backend raised it (e.g. ``"daytona"``)
    - ``op``        the operation in flight (e.g. ``"create"``, ``"exec"``)
    - ``retryable`` whether retrying the operation might succeed
    - ``context``   extra detail (exit codes, paths, …)

    The original cause is preserved through ``raise … from`` (``__cause__``).
    Construction stays backward compatible: ``SandboxError("message")`` works
    and ``str(err)`` is just the message — a compact ``[…]`` suffix is added
    only when structured fields are set.
    """

    code: ErrorCode = ErrorCode.UNKNOWN
    retryable: bool = False

    def __init__(
        self,
        message: str = "",
        *,
        backend: str | None = None,
        op: str | None = None,
        context: dict[str, Any] | None = None,
        retryable: bool | None = None,
        code: ErrorCode | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.backend = backend
        self.op = op
        self.context: dict[str, Any] = dict(context) if context else {}
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable

    def __str__(self) -> str:
        # Backward compatible: with no structured fields, this is the message.
        parts: list[str] = []
        if self.backend:
            parts.append(f"backend={self.backend}")
        if self.op:
            parts.append(f"op={self.op}")
        if self.context:
            parts.append(", ".join(f"{k}={v}" for k, v in self.context.items()))
        if not parts:
            return self.message
        head = f"code={self.code.value}"
        if self.retryable:
            head += ", retryable"
        return f"{self.message} [{head}; {'; '.join(parts)}]"


class BackendNotInstalledError(SandboxError):
    """Required pip package for a backend is not installed."""

    code = ErrorCode.BACKEND_NOT_INSTALLED


class SandboxConfigurationError(SandboxError):
    """Invalid configuration or arguments (e.g. an unknown backend or a bad manifest entry)."""

    code = ErrorCode.CONFIGURATION


class SandboxCreationError(SandboxError):
    """Sandbox creation failed."""

    code = ErrorCode.CREATION_FAILED


class SandboxExecutionError(SandboxError):
    """Code or command execution failed."""

    code = ErrorCode.EXECUTION_FAILED


class CommandFailedError(SandboxExecutionError):
    """A command or code snippet exited non-zero.

    Carries ``exit_code`` / ``stdout`` / ``stderr`` for inspection; the exit
    code is also mirrored into ``context``.
    """

    code = ErrorCode.COMMAND_FAILED

    def __init__(
        self,
        message: str = "",
        *,
        exit_code: int | None = None,
        stdout: str | None = None,
        stderr: str | None = None,
        backend: str | None = None,
        op: str | None = None,
        context: dict[str, Any] | None = None,
        retryable: bool | None = None,
        code: ErrorCode | None = None,
    ) -> None:
        ctx = dict(context) if context else {}
        if exit_code is not None:
            ctx.setdefault("exit_code", exit_code)
        super().__init__(message, backend=backend, op=op, context=ctx, retryable=retryable, code=code)
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class SandboxTimeoutError(SandboxError):
    """An operation timed out. Retryable."""

    code = ErrorCode.TIMEOUT
    retryable = True


class SandboxConnectionError(SandboxError):
    """A transport/network failure talking to the backend. Retryable."""

    code = ErrorCode.CONNECTION
    retryable = True


class SandboxNotFoundError(SandboxError):
    """A referenced sandbox, session, or snapshot does not exist."""

    code = ErrorCode.NOT_FOUND


class FeatureNotSupportedError(SandboxError):
    """The requested feature is not supported by this backend."""

    code = ErrorCode.FEATURE_NOT_SUPPORTED


class WorkspaceError(SandboxError):
    """Materializing a workspace manifest or transferring files failed."""

    code = ErrorCode.WORKSPACE
