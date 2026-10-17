"""Detached jobs: the generic sandbox implementation and the shared wait loop.

A job runs one command to completion without the submitting process staying
attached, like an AWS Batch job.  Backends that have a native job service
(``gcp_batch``) implement :class:`SandboxJobBackend` themselves.  Every other
backend with reattach support gets :class:`SessionJobRunner`:

* submit creates a sandbox, seeds its files, writes a small launcher script
  into a per-job directory and starts it detached (``setsid``/``nohup ... &``).
  The launcher records its pid, sends the command's stdout and stderr to one
  log file, and atomically writes the exit code (plus start/finish times) when
  the command ends.
* the job id encodes the backend, the sandbox's ``session_state()`` payload
  and the job directory, so a brand new process can find the job again.
* status runs one probe command that reports the launcher's liveness and the
  recorded files; cancel marks the job cancelled, kills it and destroys the
  sandbox.

The wait loop rides out transient provider read failures the way PDK's
sandbox runner learned to (pdk/sandbox.py): a few consecutive failures are
tolerated before the job has been seen alive; once seen alive, failures are
tolerated until the caller's deadline.  A provider's own not-found verdict is
not a transient failure -- it ends the job as ``unknown`` immediately.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import inspect
import json
import logging
import re
import shlex
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from bespokelabs.sandbox.exceptions import (
    BackendNotInstalledError,
    FeatureNotSupportedError,
    SandboxAuthenticationError,
    SandboxConfigurationError,
    SandboxConnectionError,
    SandboxError,
    SandboxExecutionError,
    SandboxNotFoundError,
    SandboxTimeoutError,
)
from bespokelabs.sandbox.types import (
    JobSpec,
    JobState,
    JobStatus,
    SandboxSessionState,
)

if TYPE_CHECKING:
    from bespokelabs.sandbox.protocols import SandboxBackendClient
    from bespokelabs.sandbox.sandbox import Sandbox

logger = logging.getLogger(__name__)

# Version of the generic job id payload.  Bump it only with a decoder that
# still reads every older version: ids outlive the process that made them.
_JOB_ID_VERSION = 1

# Per-job control directory, as seen by write_file().  /tmp is writable on
# every image; a uuid keeps two jobs in one host workdir apart.
_JOB_DIR_BASE = "/tmp/bespokelabs-jobs"

# How long a job's sandbox outlives the command's own timeout, so the caller
# can still read logs and outputs after it ends.  cancel_job releases it
# earlier.  Daytona maps the resulting lifetime to its wall-clock TTL.
_RETAIN_SECS = 60 * 60

# Providers whose sandboxes have a hard lifetime ceiling.
_MAX_LIFETIME_SECS = {"modal": 24 * 60 * 60}

# Backends whose write_file() paths resolve under the host workdir while
# their shell sees real host paths.
_HOST_ROOTED_BACKENDS = frozenset({"local", "safehouse"})

_UNSUPPORTED_BACKENDS = {
    "ray": "the ray backend cannot reattach to a sandbox, so a job could "
    "never be found again",
    "runpod": "the runpod backend is deprecated and does not run jobs",
}

# backend_options key that carries JobSpec.labels on each backend.
_LABEL_OPTIONS = {
    "daytona": "labels",
    "docker": "labels",
    "modal": "tags",
    "e2b": "metadata",
}

# backend_options a job sandbox needs unless the caller overrides them.
# Daytona's auto-stop is an idle timer that stops a sandbox "even if there
# are internal processes running"; a job polled rarely would be killed by it.
# The lifetime cap (ttl_minutes, from timeout_secs) still bounds the sandbox.
_DEFAULT_OPTIONS: dict[str, dict[str, Any]] = {
    "daytona": {"auto_stop_interval": 0},
}

# Wait-loop tolerance, ported from PDK (pdk/sandbox.py).  Consecutive failed
# provider reads tolerated before the job has been seen alive, and the
# ceiling on the backoff between them.
_PROVIDER_READ_ATTEMPTS = 5
_PROVIDER_READ_MAX_BACKOFF = 60.0

# Errors no amount of retrying fixes; the wait loop raises them at once.
_FATAL_ERRORS = (
    SandboxConfigurationError,
    SandboxAuthenticationError,
    BackendNotInstalledError,
    FeatureNotSupportedError,
)

_BACKEND_RE = re.compile(r"[a-z][a-z0-9_]*")
_GCP_JOB_NAME_RE = re.compile(r"projects/[^/]+/locations/[^/]+/jobs/[^/]+")


# -- Job ids -------------------------------------------------------------------


def encode_job_id(backend: str, payload: dict) -> str:
    """Encode a generic job id: ``<backend>:<base64url(JSON payload)>``."""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    token = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    return f"{backend}:{token}"


def decode_job_id(job_id: str) -> tuple[str, dict]:
    """Split a generic job id into its backend name and payload."""
    backend, sep, token = str(job_id).partition(":")
    if not sep or not _BACKEND_RE.fullmatch(backend) or not token:
        raise SandboxConfigurationError(
            f"Malformed job id {job_id!r}", op="decode_job_id"
        )
    try:
        padded = token + "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (binascii.Error, ValueError) as exc:
        raise SandboxConfigurationError(
            f"Malformed job id {job_id!r}", op="decode_job_id"
        ) from exc
    if not isinstance(payload, dict) or payload.get("v") != _JOB_ID_VERSION:
        raise SandboxConfigurationError(
            f"Unsupported job id version in {job_id!r}", op="decode_job_id"
        )
    if not isinstance(payload.get("state"), dict) or not isinstance(
        payload.get("dir"), str
    ):
        raise SandboxConfigurationError(
            f"Malformed job id {job_id!r}", op="decode_job_id"
        )
    return backend, payload


def job_backend(job_id: str) -> str:
    """Return the backend that owns *job_id*.

    Lets a process that holds only a job id (a restarted worker) rebuild the
    right client: ``SandboxClient(job_backend(job_id)).wait_job(job_id)``.
    Cloud Batch job ids are the job's resource name
    (``projects/<p>/locations/<region>/jobs/<id>``); every other backend's id
    starts with ``<backend>:``.
    """
    job_id = str(job_id)
    if _GCP_JOB_NAME_RE.fullmatch(job_id):
        return "gcp_batch"
    return decode_job_id(job_id)[0]


# -- Provider verdicts ---------------------------------------------------------


def is_gone(exc: BaseException) -> bool:
    """Whether an exception chain carries the provider's own not-found verdict.

    Backends re-raise provider errors as their own types ``from`` the
    original, so the verdict can sit anywhere down the ``__cause__`` /
    ``__context__`` chain.  Duck-type it (a 404 status, a ``NotFound`` class,
    Modal's ``SandboxTerminatedError``) instead of importing optional
    provider SDKs.  Anything else -- 5xx, timeouts, resets -- is transport
    noise that says nothing about the sandbox.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, SandboxNotFoundError):
            return True
        if (
            getattr(current, "status_code", None) == 404
            or getattr(current, "status", None) == 404
        ):
            return True
        name = type(current).__name__
        if "NotFound" in name or name == "SandboxTerminatedError":
            return True
        current = current.__cause__ or current.__context__
    return False


def _wrap(exc: Exception, backend: str, op: str) -> SandboxError:
    if isinstance(exc, SandboxError):
        return exc
    return SandboxExecutionError(
        f"{backend} {op} failed: {exc}", backend=backend, op=op
    )


# -- Shell scripts -------------------------------------------------------------


def _launcher_script(spec: JobSpec, job_dir: str, workdir: str | None) -> str:
    """Build the script that runs the command and records how it ended."""
    q = shlex.quote
    if isinstance(spec.command, list):
        command = shlex.join(spec.command)
    else:
        command = f"sh -c {q(spec.command)}"
    timeout = int(spec.timeout_secs)
    exports = "".join(
        f"export {name}={q(value)}\n" for name, value in spec.env_vars.items()
    )
    enter_workdir = ""
    if workdir:
        enter_workdir = (
            f"mkdir -p {q(workdir)} && cd {q(workdir)} || {{\n"
            f"  echo 'bespokelabs-sandbox: cannot enter workdir' {q(workdir)}"
            ' >>"$d/log"\n'
            "  finish 127\n"
            "}\n"
        )
    return (
        "#!/bin/sh\n"
        "# Written by bespokelabs-sandbox: runs one job, records its exit code.\n"
        f"d={q(job_dir)}\n"
        "finish() {\n"
        '  date +%s >"$d/finished-at.tmp" && mv -f "$d/finished-at.tmp"'
        ' "$d/finished-at"\n'
        '  printf \'%s\\n\' "$1" >"$d/exit-code.tmp" &&'
        ' mv -f "$d/exit-code.tmp" "$d/exit-code"\n'
        '  exit "$1"\n'
        "}\n"
        f"{exports}"
        "start=$(date +%s)\n"
        'printf \'%s\\n\' "$start" >"$d/started-at"\n'
        f"{enter_workdir}"
        "watchdog=\n"
        "# The command gets its own process group (setsid, else job control)\n"
        "# so a timeout or cancel can signal everything it started.\n"
        "if command -v setsid >/dev/null 2>&1; then grp=setsid\n"
        "else grp=; set -m 2>/dev/null || :\n"
        "fi\n"
        "if command -v timeout >/dev/null 2>&1; then\n"
        f'  $grp timeout {timeout} {command} >>"$d/log" 2>&1 </dev/null &\n'
        "  child=$!\n"
        "else\n"
        f'  $grp {command} >>"$d/log" 2>&1 </dev/null &\n'
        "  child=$!\n"
        '  $grp sh -c \'sleep "$1"; kill -TERM "-$2" 2>/dev/null ||'
        f' kill -TERM "$2"\' watchdog {timeout} "$child"'
        " >/dev/null 2>&1 </dev/null &\n"
        "  watchdog=$!\n"
        '  printf \'%s\\n\' "$watchdog" >"$d/watchdog.pid"\n'
        "fi\n"
        'printf \'%s\\n\' "$child" >"$d/child.pid"\n'
        'wait "$child"\n'
        "code=$?\n"
        'if [ -n "$watchdog" ]; then\n'
        '  kill -TERM "-$watchdog" 2>/dev/null || kill -TERM "$watchdog"'
        " 2>/dev/null\n"
        "fi\n"
        f'if [ "$code" -ne 0 ] && [ $(( $(date +%s) - start )) -ge {timeout} ];'
        " then\n"
        '  : >"$d/timed-out"\n'
        "fi\n"
        'finish "$code"\n'
    )


def _start_script(job_dir: str) -> str:
    """Start the launcher detached and publish its pid before returning."""
    return (
        f"d={shlex.quote(job_dir)}\n"
        "if command -v setsid >/dev/null 2>&1; then run='setsid nohup'\n"
        "elif command -v nohup >/dev/null 2>&1; then run=nohup\n"
        "else run=\n"
        "fi\n"
        '$run sh "$d/launch.sh" >"$d/launcher.log" 2>&1 </dev/null &\n'
        "pid=$!\n"
        'printf \'%s\\n\' "$pid" >"$d/pid.tmp" &&'
        ' mv -f "$d/pid.tmp" "$d/pid" && echo "submitted=$pid"\n'
        "exit 0\n"
    )


def _probe_script(job_dir: str) -> str:
    """Report liveness first, then the recorded files.

    Liveness is read before the exit code on purpose: the launcher writes
    the exit code before it exits, so a launcher seen dead with no exit code
    really died without recording one.  A zombie counts as dead.
    """
    return (
        f"d={shlex.quote(job_dir)}\n"
        'if [ ! -d "$d" ]; then echo missing=1; echo probe=ok; exit 0; fi\n'
        "alive=0\n"
        'p=$(cat "$d/pid" 2>/dev/null)\n'
        'case "$p" in\n'
        "  ''|*[!0-9]*) ;;\n"
        '  *) if kill -0 "$p" 2>/dev/null; then alive=1; fi\n'
        '     case "$(cat "/proc/$p/stat" 2>/dev/null)" in'
        ' *") Z "*) alive=0;; esac;;\n'
        "esac\n"
        'echo "alive=$alive"\n'
        "for f in pid exit-code started-at finished-at timed-out cancelled;"
        " do\n"
        '  if [ -f "$d/$f" ]; then'
        ' printf \'%s=%s\\n\' "$f" "$(cat "$d/$f" 2>/dev/null)"; fi\n'
        "done\n"
        "echo probe=ok\n"
        "exit 0\n"
    )


def _cancel_script(job_dir: str) -> str:
    """Mark the job cancelled, then stop the launcher before its command."""
    return (
        f"d={shlex.quote(job_dir)}\n"
        ': >"$d/cancelled" 2>/dev/null\n'
        "for f in pid child.pid watchdog.pid; do\n"
        '  p=$(cat "$d/$f" 2>/dev/null)\n'
        '  case "$p" in\n'
        "    ''|*[!0-9]*) ;;\n"
        '    *) kill -TERM "-$p" 2>/dev/null || kill -TERM "$p" 2>/dev/null;;\n'
        "  esac\n"
        "done\n"
        "echo cancelled=1\n"
        "exit 0\n"
    )


def _logs_script(job_dir: str, tail: int | None) -> str:
    reader = "cat" if tail is None else f"tail -n {int(tail)}"
    return (
        f"d={shlex.quote(job_dir)}\n"
        f'if [ -f "$d/log" ]; then {reader} "$d/log"; fi\n'
        "exit 0\n"
    )


def _run(executor: Any, script: str) -> str:
    """Run a control script through the backend's shell and return stdout.

    Every control script exits 0 and answers on stdout, so a non-zero exit
    means the provider, not the job, failed.  The script is passed as one
    quoted ``sh -c`` argument, which keeps the local backend's path rewriting
    away from it.
    """
    result = executor.execute_command(f"sh -c {shlex.quote(script)}", None)
    if result.exit_code != 0:
        raise SandboxExecutionError(
            f"Job control command exited {result.exit_code}: "
            f"{(result.stderr or result.stdout or '')[:300]}",
            op="job_control",
        )
    return result.stdout or ""


def _facts(output: str) -> dict[str, str]:
    facts: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep:
            facts[key] = value.strip()
    return facts


def _int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _epoch(value: str | None) -> datetime | None:
    seconds = _int(value)
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def _status_from_facts(
    job_id: str, facts: dict[str, str], details: dict
) -> JobStatus:
    common = {
        "job_id": job_id,
        "started_at": _epoch(facts.get("started-at")),
        "finished_at": _epoch(facts.get("finished-at")),
        "details": details,
    }
    if facts.get("missing") == "1":
        return JobStatus(
            state=JobState.UNKNOWN, reason="job_files_missing", **common
        )
    cancelled = "cancelled" in facts
    # A torn or unparsable exit code reads as "not yet": it is settled on the
    # next poll instead of being mistaken for an exit.
    exit_code = _int(facts.get("exit-code"))
    if exit_code is not None:
        if exit_code == 0:
            return JobStatus(state=JobState.SUCCEEDED, exit_code=0, **common)
        if cancelled:
            return JobStatus(
                state=JobState.CANCELLED,
                exit_code=exit_code,
                reason="cancelled",
                **common,
            )
        return JobStatus(
            state=JobState.FAILED,
            exit_code=exit_code,
            reason="timeout" if "timed-out" in facts else "exit_code",
            **common,
        )
    if cancelled:
        return JobStatus(state=JobState.CANCELLED, reason="cancelled", **common)
    if facts.get("alive") == "1":
        return JobStatus(state=JobState.RUNNING, **common)
    if "pid" not in facts:
        # Submit publishes the pid before it returns, so this is a job
        # mid-launch.  Not proof of anything; let the next poll settle it.
        return JobStatus(state=JobState.PENDING, **common)
    return JobStatus(state=JobState.UNKNOWN, reason="process_lost", **common)


def _sandbox_ref(state: dict) -> str | None:
    for key in ("sandbox_id", "container_id", "pod_id", "workdir"):
        if state.get(key):
            return str(state[key])
    return None


# -- Generic runner ------------------------------------------------------------


def job_runner(backend_name: str, backend_client: Any) -> Any:
    """Return the job implementation for one backend client.

    A client that implements the job methods itself (checked on its type, so
    a mock instance does not qualify by accident) is used directly.
    """
    if callable(getattr(type(backend_client), "submit_job", None)):
        return backend_client
    reason = _UNSUPPORTED_BACKENDS.get(backend_name)
    if reason is not None:
        raise FeatureNotSupportedError(
            f"Jobs are not supported on '{backend_name}': {reason}",
            backend=backend_name,
            op="submit_job",
        )
    return SessionJobRunner(backend_name, backend_client)


class SessionJobRunner:
    """Jobs on any backend that can create and resume interactive sandboxes."""

    def __init__(
        self, backend_name: str, backend_client: SandboxBackendClient
    ) -> None:
        self._backend = backend_name
        self._client = backend_client

    # -- Submit ----------------------------------------------------------

    def submit_job(self, spec: JobSpec) -> str:
        from bespokelabs.sandbox.sandbox import Sandbox

        backend = self._backend
        options = dict(spec.backend_options)
        for key, value in _DEFAULT_OPTIONS.get(backend, {}).items():
            options.setdefault(key, value)
        label_key = _LABEL_OPTIONS.get(backend)
        if label_key and spec.labels:
            options[label_key] = {
                **spec.labels,
                **(options.get(label_key) or {}),
            }
        lifetime = int(spec.timeout_secs) + _RETAIN_SECS
        ceiling = _MAX_LIFETIME_SECS.get(backend)
        if ceiling is not None:
            if spec.timeout_secs > ceiling:
                raise SandboxConfigurationError(
                    f"{backend} sandboxes live at most {ceiling}s; "
                    f"timeout_secs={spec.timeout_secs} cannot fit",
                    backend=backend,
                    op="submit_job",
                )
            lifetime = min(lifetime, ceiling)

        sb = Sandbox(
            backend,
            cpu=spec.cpu,
            memory_mb=spec.memory_mb,
            disk_mb=spec.disk_mb,
            gpu=spec.gpu,
            timeout_secs=lifetime,
            image=spec.image,
            allow_internet=spec.allow_internet,
            app_name=spec.app_name,
            template=spec.template,
            snapshot_id=spec.snapshot_id,
            workdir=spec.workdir,
            backend_options=options,
            files=spec.files,
            workspace=spec.workspace,
            _backend_client=self._client,
        )
        try:
            state = sb.session_state().data
            relative_dir = f"{_JOB_DIR_BASE}/{uuid.uuid4().hex}"
            host_rooted = backend in _HOST_ROOTED_BACKENDS
            root = str(state.get("workdir") or "") if host_rooted else ""
            job_dir = root.rstrip("/") + relative_dir
            # On host-rooted backends the workdir *is* the root and already
            # the command's cwd, so the launcher does not change directory.
            workdir = None if host_rooted else spec.workdir
            _run(sb, f"mkdir -p {shlex.quote(job_dir)}")
            sb.write_file(
                f"{relative_dir}/launch.sh",
                _launcher_script(spec, job_dir, workdir),
            )
            started = _facts(_run(sb, _start_script(job_dir)))
            if _int(started.get("submitted")) is None:
                raise SandboxExecutionError(
                    f"{backend} did not confirm the job launch",
                    backend=backend,
                    op="submit_job",
                )
        except BaseException:
            try:
                sb.destroy()
            except Exception:
                logger.warning(
                    "Could not destroy the %s sandbox of a failed job submit; "
                    "it may still be running.",
                    backend,
                    exc_info=True,
                )
            raise
        return encode_job_id(
            backend, {"v": _JOB_ID_VERSION, "state": state, "dir": job_dir}
        )

    # -- Reattach --------------------------------------------------------

    def _decode(self, job_id: str) -> dict:
        backend, payload = decode_job_id(job_id)
        if backend != self._backend:
            raise SandboxConfigurationError(
                f"Job id belongs to backend '{backend}', not '{self._backend}'",
                backend=self._backend,
                op="decode_job_id",
            )
        return payload

    def _details(self, payload: dict) -> dict:
        return {
            "backend": self._backend,
            "sandbox_id": _sandbox_ref(payload["state"]),
        }

    def _gone(self, job_id: str, payload: dict) -> JobStatus:
        return JobStatus(
            job_id=job_id,
            state=JobState.UNKNOWN,
            reason="sandbox_gone",
            details=self._details(payload),
        )

    def _attach(self, payload: dict, op: str) -> Any:
        """Resume the job's sandbox; ``None`` when the provider says it is gone."""
        try:
            return self._client.resume(payload["state"])
        except Exception as exc:
            if is_gone(exc):
                return None
            raise _wrap(exc, self._backend, op) from exc

    def _probe(self, job_id: str, payload: dict, session: Any) -> JobStatus:
        try:
            output = _run(session, _probe_script(payload["dir"]))
        except Exception as exc:
            if is_gone(exc):
                return self._gone(job_id, payload)
            raise _wrap(exc, self._backend, "job_status") from exc
        facts = _facts(output)
        if facts.get("probe") != "ok":
            raise SandboxConnectionError(
                "The job status probe returned no answer",
                backend=self._backend,
                op="job_status",
            )
        return _status_from_facts(job_id, facts, self._details(payload))

    def job_status(self, job_id: str) -> JobStatus:
        payload = self._decode(job_id)
        session = self._attach(payload, "job_status")
        if session is None:
            return self._gone(job_id, payload)
        return self._probe(job_id, payload, session)

    def job_logs(self, job_id: str, tail: int | None = None) -> str:
        payload = self._decode(job_id)
        session = self._attach(payload, "job_logs")
        if session is None:
            raise SandboxNotFoundError(
                "The job's sandbox no longer exists, and its logs with it",
                backend=self._backend,
                op="job_logs",
            )
        try:
            return _run(session, _logs_script(payload["dir"], tail))
        except Exception as exc:
            if is_gone(exc):
                raise SandboxNotFoundError(
                    "The job's sandbox no longer exists, and its logs with it",
                    backend=self._backend,
                    op="job_logs",
                ) from exc
            raise _wrap(exc, self._backend, "job_logs") from exc

    def cancel_job(self, job_id: str) -> JobStatus:
        payload = self._decode(job_id)
        session = self._attach(payload, "cancel_job")
        if session is None:
            return self._gone(job_id, payload)
        try:
            status = self._probe(job_id, payload, session)
        except Exception:
            # An unreadable job is still cancelled: signal it and destroy.
            logger.warning(
                "Could not read the job before cancelling it", exc_info=True
            )
            status = JobStatus(
                job_id=job_id,
                state=JobState.RUNNING,
                details=self._details(payload),
            )
        if status.state is JobState.UNKNOWN and status.reason == "sandbox_gone":
            return status
        if not status.done:
            try:
                _run(session, _cancel_script(payload["dir"]))
            except Exception:
                # Destroying the sandbox below stops the job regardless.
                logger.warning(
                    "Could not signal job before destroying its sandbox",
                    exc_info=True,
                )
            status = JobStatus(
                job_id=job_id,
                state=JobState.CANCELLED,
                started_at=status.started_at,
                reason="cancelled",
                details=status.details,
            )
        try:
            session.destroy()
        except Exception as exc:
            if not is_gone(exc):
                raise _wrap(exc, self._backend, "cancel_job") from exc
        return status

    def job_sandbox(self, job_id: str) -> Sandbox:
        from bespokelabs.sandbox.sandbox import Sandbox

        payload = self._decode(job_id)
        return Sandbox._resume_with(
            self._backend,
            self._client,
            SandboxSessionState(backend=self._backend, data=payload["state"]),
        )


# -- Wait loop -----------------------------------------------------------------


class _Waiter:
    """Polling policy shared by the sync and async ``wait_job``."""

    def __init__(
        self, job_id: str, timeout_secs: float | None, poll_secs: float
    ) -> None:
        if timeout_secs is not None and timeout_secs < 0:
            raise SandboxConfigurationError("timeout_secs must be >= 0")
        self._job_id = job_id
        self._timeout = timeout_secs
        self._deadline = (
            None if timeout_secs is None else time.monotonic() + timeout_secs
        )
        self._poll = max(0.1, float(poll_secs))
        self._failures = 0
        self._observed = False
        self._last: JobStatus | None = None

    def _remaining(self) -> float | None:
        if self._deadline is None:
            return None
        return self._deadline - time.monotonic()

    def _timeout_error(self) -> SandboxTimeoutError:
        state = self._last.state.value if self._last else "unobserved"
        return SandboxTimeoutError(
            f"Timed out after {self._timeout}s waiting for the job; "
            f"last state: {state}",
            op="wait_job",
            context={"job_id": self._job_id, "state": state},
        )

    def observed(self, status: JobStatus) -> float | None:
        """Record a status; the delay before the next poll, or ``None`` if done."""
        self._failures = 0
        self._last = status
        if status.done:
            return None
        self._observed = True
        remaining = self._remaining()
        if remaining is not None and remaining <= 0:
            raise self._timeout_error()
        return self._poll if remaining is None else min(self._poll, remaining)

    def failed(self, exc: Exception) -> float:
        """Record a failed read; the delay before retrying, or raise."""
        if isinstance(exc, _FATAL_ERRORS):
            raise exc
        self._failures += 1
        remaining = self._remaining()
        if remaining is not None and remaining <= 0:
            if self._observed:
                raise self._timeout_error() from exc
            raise exc
        if self._failures >= _PROVIDER_READ_ATTEMPTS and not self._observed:
            raise exc
        logger.info(
            "Job status read failed (%d in a row); retrying: %s",
            self._failures,
            exc,
        )
        index = min(self._failures, _PROVIDER_READ_ATTEMPTS) - 1
        delay = min(_PROVIDER_READ_MAX_BACKOFF, self._poll * 2**index)
        return delay if remaining is None else max(0.0, min(delay, remaining))


def wait_sync(
    status_fn: Callable[[str], JobStatus],
    job_id: str,
    *,
    timeout_secs: float | None,
    poll_secs: float,
    on_poll: Callable[[JobStatus], Any] | None,
) -> JobStatus:
    waiter = _Waiter(job_id, timeout_secs, poll_secs)
    while True:
        try:
            status = status_fn(job_id)
        except Exception as exc:
            time.sleep(waiter.failed(exc))
            continue
        if on_poll is not None:
            on_poll(status)
        delay = waiter.observed(status)
        if delay is None:
            return status
        time.sleep(delay)


async def wait_async(
    status_fn: Callable[[str], JobStatus],
    job_id: str,
    *,
    timeout_secs: float | None,
    poll_secs: float,
    on_poll: Callable[[JobStatus], Any] | None,
) -> JobStatus:
    waiter = _Waiter(job_id, timeout_secs, poll_secs)
    while True:
        try:
            status = await asyncio.to_thread(status_fn, job_id)
        except Exception as exc:
            await asyncio.sleep(waiter.failed(exc))
            continue
        if on_poll is not None:
            outcome = on_poll(status)
            if inspect.isawaitable(outcome):
                await outcome
        delay = waiter.observed(status)
        if delay is None:
            return status
        await asyncio.sleep(delay)
