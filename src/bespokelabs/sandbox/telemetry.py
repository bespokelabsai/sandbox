"""Lifecycle telemetry for sandboxes.

Every sandbox create, setup, exec, and destroy emits one
:class:`SandboxEvent` carrying the backend, duration, and outcome.  Events go
to the ``bespokelabs.sandbox.telemetry`` logger (INFO on success, WARNING on
failure) and to any listener added with :func:`add_listener`, so an
application can forward them to Datadog, Prometheus, or OpenTelemetry without
the SDK depending on any of them.

Events never carry commands, code, file contents, paths, environment
variables, or error messages, because those can contain secrets.  Only the
stable :class:`~bespokelabs.sandbox.ErrorCode` and the exception class name
are recorded.

Example::

    from bespokelabs.sandbox import telemetry

    telemetry.add_listener(
        lambda e: statsd.timing(
            f"sandbox.{e.op}", e.duration_secs * 1000,
            tags=[f"backend:{e.backend}", f"outcome:{e.outcome}"],
        )
    )
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("bespokelabs.sandbox.telemetry")

# Operations that emit events.
OP_CREATE = "create"
OP_SETUP = "setup"
OP_EXEC = "exec"
OP_DESTROY = "destroy"


@dataclass(frozen=True)
class SandboxEvent:
    """One finished sandbox operation.

    Attributes:
        op: ``create``, ``setup``, ``exec``, or ``destroy``.
        backend: Backend name, e.g. ``"daytona"``.
        outcome: ``ok`` or ``error``.  An ``exec`` whose command exits
            non-zero is still ``ok``: the sandbox worked; see ``exit_code``.
        duration_secs: Wall-clock time of the operation.
        error_code: Stable ``ErrorCode`` value when ``outcome`` is ``error``.
        error_type: Exception class name when ``outcome`` is ``error``.
        retryable: ``SandboxError.retryable`` when available.
        exit_code: Command exit code for ``exec``.
        preset: Preset name for ``create``/``setup`` when one was used.
        lifetime_secs: For ``destroy``, seconds since the sandbox was created
            or resumed in this process.

    """

    op: str
    backend: str
    outcome: str
    duration_secs: float
    error_code: str | None = None
    error_type: str | None = None
    retryable: bool | None = None
    exit_code: int | None = None
    preset: str | None = None
    lifetime_secs: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


Listener = Callable[[SandboxEvent], None]

_listeners: list[Listener] = []
_lock = threading.Lock()


def add_listener(listener: Listener) -> None:
    """Register *listener*; it is called synchronously for every event."""
    with _lock:
        if listener not in _listeners:
            _listeners.append(listener)


def remove_listener(listener: Listener) -> None:
    with _lock:
        if listener in _listeners:
            _listeners.remove(listener)


def emit(event: SandboxEvent) -> None:
    """Log *event* and call listeners.  Never raises."""
    try:
        try:
            failed = event.outcome != "ok"
            logger.log(
                logging.WARNING if failed else logging.INFO,
                "sandbox %s backend=%s outcome=%s duration_secs=%.3f%s",
                event.op,
                event.backend,
                event.outcome,
                event.duration_secs,
                f" error_code={event.error_code} error_type={event.error_type}"
                if failed
                else "",
                extra={"sandbox_event": event},
            )
        except Exception:  # A broken log handler must not skip listeners.
            pass
        with _lock:
            listeners = list(_listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                logger.exception("sandbox telemetry listener failed")
    except Exception:  # Telemetry must never break a sandbox operation.
        pass


class _Span:
    """Mutable attributes recorded onto the event when the span closes."""

    def __init__(self) -> None:
        self.attrs: dict[str, Any] = {}

    def set(self, **attrs: Any) -> None:
        self.attrs.update(attrs)


@contextmanager
def span(op: str, backend: str, **attrs: Any) -> Iterator[_Span]:
    """Time the block and emit one event; re-raises any exception."""
    sp = _Span()
    sp.attrs.update(attrs)
    start = time.monotonic()
    try:
        yield sp
    except BaseException as exc:
        code = getattr(exc, "code", None)
        emit(
            _build(
                op,
                backend,
                "error",
                time.monotonic() - start,
                sp.attrs,
                error_code=getattr(code, "value", code) or "unknown",
                error_type=type(exc).__name__,
                retryable=getattr(exc, "retryable", None),
            )
        )
        raise
    else:
        emit(_build(op, backend, "ok", time.monotonic() - start, sp.attrs))


_FIELDS = {
    "exit_code",
    "preset",
    "lifetime_secs",
}


def _build(
    op: str,
    backend: str,
    outcome: str,
    duration: float,
    attrs: dict[str, Any],
    **kw: Any,
) -> SandboxEvent:
    known = {k: v for k, v in attrs.items() if k in _FIELDS}
    extra = {k: v for k, v in attrs.items() if k not in _FIELDS}
    return SandboxEvent(
        op=op,
        backend=backend,
        outcome=outcome,
        duration_secs=duration,
        extra=extra,
        **known,
        **kw,
    )
