"""Sandbox backend for boxd machines.

Every sandbox is a boxd machine: a KVM microVM with its own kernel, booted
from boxd's Ubuntu 24.04 image (Python, Node, Docker, git, and the Claude
Code, Codex and OpenCode CLIs preinstalled).  Machines persist until they are
deleted, so cleanup matters more here than on providers whose sandboxes
expire on their own.
"""

from __future__ import annotations

import inspect
import ipaddress
import logging
import math
import os
import pathlib
import posixpath
import re
import shlex
import socket
import ssl
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from bespokelabs.sandbox.exceptions import (
    BackendNotInstalledError,
    ErrorOutcome,
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
    SandboxResult,
    SnapshotInfo,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Label stamped on every machine this backend creates, carrying a token unique
# to the create call.  If the call fails after boxd has already built the
# machine, the label is how we find the orphan and delete it.
_CREATE_TOKEN_LABEL = "bespokelabs.sandbox/create-token"

# The machine sizes boxd offers, as (vCPUs, MiB of RAM).  There is nothing in
# between, so a request is rounded up to the smallest size that holds it.
_SIZES = ((1, 4096), (2, 8192), (4, 16384), (8, 32768))

# Every machine gets a 100 GiB copy-on-write disk; the size is not settable.
_DISK_MB = 100 * 1024

# A boxd egress allow-list admits only what it names, and there is no
# separate "block everything" switch.  A name under the reserved .invalid TLD
# (RFC 2606) never resolves, so a list holding only this entry reaches
# nothing.
_DENY_ALL_ENTRY = "egress-none.invalid"

# A changed allow-list "takes effect within a second on a running machine"
# (https://docs.boxd.sh/guides/egress-allowlist); it took 0.6-0.8 s when
# measured.  create() and set_network() wait it out, so the sandbox they
# hand back cannot reach what the policy forbids.
_EGRESS_SETTLE_SECS = 1.0

# A new proxy route serves TLS once boxd has issued its certificate, which
# took about 18 s when measured; until then the handshake is refused.
_PORT_READY_TIMEOUT_SECS = 90.0

# boxd closes an exec that has written nothing for 30 minutes ("no output
# from the command for 30m"), which a quiet build, test run or agent turn can
# reach.  Every command runs beside a loop that writes a NUL to stderr at the
# start and every five minutes; execute_command strips them from the result.
_KEEPALIVE_SECS = 300
_KEEPALIVE = (
    "( printf '\\0' >&2; while :; do "
    f"sleep {_KEEPALIVE_SECS} >/dev/null 2>&1 </dev/null; "
    "printf '\\0' >&2; done ) & __bsb_keepalive=$!; "
)

# Curator and the README examples write programs into /workspace.  Commands
# run as the unprivileged ``boxd`` user, so the directory is created and
# handed to that user up front.
_WORKSPACE_DIR = "/workspace"

# backend_options consumed here rather than passed to machines.create().
_ADAPTER_OPTIONS = frozenset(
    {
        "name",
        "name_prefix",
        "ready_timeout_secs",
        "snapshot_timeout_secs",
        "auto_hibernate_timeout",
    }
)
_DEFAULT_NAME_PREFIX = "sandbox"
_DEFAULT_READY_TIMEOUT_SECS = 120.0
_DEFAULT_SNAPSHOT_TIMEOUT_SECS = 300.0

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class _CleanupReport:
    status: str
    provider_resource_id: str | None = None


def _classify_boxd_error(
    exc: BaseException,
    *,
    op: str,
    cleanup: _CleanupReport | None = None,
    missing: type[SandboxError] = SandboxNotFoundError,
) -> SandboxError:
    """Translate boxd SDK exception types into the public error taxonomy.

    ``missing`` is the type a boxd ``NotFoundError`` for the machine becomes.
    A failure inside the machine's filesystem (a missing file, a directory, a
    path the ``boxd`` user may not touch) is an execution error, as on the
    other backends, never a credentials or machine problem.
    """
    from boxd import (
        APIConnectionError,
        APIStatusError,
        AuthenticationError,
        MachineFilesystemError,
        NotFoundError,
        PermissionDeniedError,
        RateLimitError,
    )

    context: dict[str, str] = {}
    provider_code = getattr(exc, "code", None)
    if isinstance(provider_code, str):
        context["provider_code"] = provider_code
    provider_reason = getattr(exc, "reason", None)
    if isinstance(provider_reason, str):
        context["provider_reason"] = provider_reason
    if cleanup is not None:
        context["cleanup_status"] = cleanup.status
        if cleanup.provider_resource_id:
            context["provider_resource_id"] = cleanup.provider_resource_id

    outcome = (
        ErrorOutcome.UNKNOWN
        if cleanup is not None and cleanup.status == "unknown"
        else ErrorOutcome.FAILED
    )
    common: dict[str, Any] = {
        "backend": "boxd",
        "op": op,
        "context": context,
        "outcome": outcome,
    }
    # MachinePermissionError is also a PermissionDeniedError, so this goes
    # before the credentials check.
    if isinstance(exc, MachineFilesystemError):
        return SandboxExecutionError(f"boxd {op} failed: {exc}", **common)
    # Only missing or refused credentials: since boxd 0.2.20, a token exchange
    # that could not reach boxd is an APIConnectionError, and one that got a
    # server error an APIStatusError.
    if isinstance(exc, (AuthenticationError, PermissionDeniedError)):
        return SandboxAuthenticationError(
            f"boxd rejected the credentials: {exc}", **common
        )
    if isinstance(exc, TimeoutError) or provider_code == "deadline_exceeded":
        return SandboxTimeoutError(
            f"boxd {op} timed out: {exc}",
            retryable=outcome is ErrorOutcome.FAILED,
            **common,
        )
    if isinstance(exc, (APIConnectionError, ConnectionError)):
        return SandboxConnectionError(
            f"boxd {op} connection failed: {exc}",
            retryable=outcome is ErrorOutcome.FAILED,
            **common,
        )
    if isinstance(exc, NotFoundError):
        return missing(f"boxd {op} failed: {exc}", **common)
    # A rate limit, a full machine quota, or a server error answering the
    # token exchange can clear by itself.
    transient = isinstance(exc, RateLimitError) or (
        isinstance(exc, APIStatusError) and (exc.http_status or 0) >= 500
    )
    retryable = transient and outcome is ErrorOutcome.FAILED
    if op == "create":
        return SandboxCreationError(
            f"Failed to create boxd machine: {exc}",
            retryable=retryable,
            **common,
        )
    return SandboxExecutionError(
        f"boxd {op} failed: {exc}", retryable=retryable, **common
    )


def _denied(exc: SandboxError) -> bool:
    """Return whether *exc* is the ``boxd`` user being refused a path."""
    from boxd import MachinePermissionError

    return isinstance(exc.__cause__, MachinePermissionError)


class BoxdClient:
    """Factory for boxd machines.

    The SDK client is built on first create() or resume() and reused for
    every session created through this client.  It reads ``BOXD_API_KEY``
    (or ``BOXD_TOKEN``) and ``BOXD_BASE_URL`` itself; ``BOXD_ORG`` picks the
    organization when the credential can reach more than one.
    """

    supports_dockerfile = False
    network_modes = frozenset({"public", "none", "allowlist"})

    def __init__(self) -> None:
        try:
            from boxd import Boxd
        except ImportError as exc:
            raise BackendNotInstalledError(
                "boxd SDK not installed. Run: pip install bespokelabs-sandbox[boxd]"
            ) from exc
        self._boxd_cls = Boxd
        self._client: Any = None
        # create() may run concurrently (e.g. via AsyncSandboxClient);
        # guard the one-time client construction.
        self._connect_lock = threading.Lock()

    def _ensure_client(self) -> Any:
        if self._client is None:
            with self._connect_lock:
                if self._client is None:
                    self._client = self._boxd_cls()
        return self._client

    def create(self, config: SandboxConfig) -> BoxdSession:
        _check_config(config)
        env_vars = _check_env(config.env_vars)
        egress = _egress_entries(_effective_policy(config), op="create")
        options, adapter = _split_options(config.backend_options)
        try:
            client = self._ensure_client()
        except Exception as exc:
            raise _classify_boxd_error(exc, op="create") from exc
        _check_create_options(client, options)

        org = options.pop("org", None) or os.environ.get("BOXD_ORG") or None
        # Unique to this create call.  If the call fails after boxd has built
        # the machine, this token is how we find the orphan.
        create_token = uuid.uuid4().hex
        name = adapter.get("name") or (
            f"{adapter.get('name_prefix', _DEFAULT_NAME_PREFIX)}-"
            f"{create_token[:12]}"
        )
        kwargs = _create_kwargs(
            config, options, org=org, create_token=create_token
        )
        ready_timeout = float(
            adapter.get("ready_timeout_secs", _DEFAULT_READY_TIMEOUT_SECS)
        )

        machine_id: str | None = None
        egress_set_at: float | None = None
        try:
            machine = client.machines.create(name, **kwargs)
            machine_id = machine.id
            # create() returns once the machine is scheduled, before exec
            # works; wait_until_ready() also round-trips one exec.
            client.machines.wait_until_ready(machine_id, timeout=ready_timeout)
            if egress:
                client.machines.set_egress_allow(machine_id, egress)
                egress_set_at = time.monotonic()
            if "auto_hibernate_timeout" in adapter:
                client.machines.set_auto_hibernate_timeout(
                    machine_id, int(adapter["auto_hibernate_timeout"])
                )
            session = BoxdSession(
                client=client,
                machine_id=machine_id,
                name=machine.name,
                domain=_machine_domain(machine),
                workdir=config.workdir,
                env_vars=env_vars,
                org=org,
                snapshot_timeout_secs=float(
                    adapter.get(
                        "snapshot_timeout_secs", _DEFAULT_SNAPSHOT_TIMEOUT_SECS
                    )
                ),
            )
            session._prepare_dirs()
            if egress_set_at is not None:
                _settle_egress(egress_set_at)
        except Exception as exc:
            cleanup = self._reap(client, machine_id, create_token, org)
            if isinstance(exc, SandboxError):
                exc.context.setdefault("cleanup_status", cleanup.status)
                if cleanup.status == "unknown":
                    exc.outcome = ErrorOutcome.UNKNOWN
                    exc.retryable = False
                raise
            raise _classify_boxd_error(
                exc, op="create", cleanup=cleanup
            ) from exc
        except BaseException:
            # KeyboardInterrupt / SystemExit: still reap, then propagate as-is.
            self._reap(client, machine_id, create_token, org)
            raise
        return session

    def resume(self, data: dict) -> BoxdSession:
        machine_id = str(data.get("machine_id") or "")
        if not machine_id:
            raise SandboxConfigurationError(
                "boxd session state is missing machine_id",
                backend="boxd",
                op="resume",
            )
        try:
            client = self._ensure_client()
            machine = client.machines.get(machine_id)
            _wake(client, machine)
        except Exception as exc:
            raise _classify_boxd_error(exc, op="resume") from exc
        return BoxdSession(
            client=client,
            machine_id=machine_id,
            name=machine.name,
            domain=_machine_domain(machine),
            workdir=data.get("workdir"),
            env_vars=dict(data.get("env_vars") or {}),
            org=data.get("org"),
            snapshot_timeout_secs=float(
                data.get(
                    "snapshot_timeout_secs", _DEFAULT_SNAPSHOT_TIMEOUT_SECS
                )
            ),
        )

    @staticmethod
    def _reap(
        client: Any,
        machine_id: str | None,
        create_token: str,
        org: str | None,
    ) -> _CleanupReport:
        """Delete the machine a failed create may have left running.

        Best effort by design: any failure here is logged and swallowed so it
        can never mask the creation error the caller actually needs to see.
        """
        if machine_id is not None:
            ids = [machine_id]
        else:
            try:
                ids = [
                    m.id
                    for m in client.machines.list(
                        org=org, labels={_CREATE_TOKEN_LABEL: create_token}
                    )
                ]
            except Exception:
                logger.warning(
                    "Could not search for a boxd machine orphaned by a failed "
                    "create (label %s=%s); it may still be running and billing.",
                    _CREATE_TOKEN_LABEL,
                    create_token,
                    exc_info=True,
                )
                return _CleanupReport("unknown")
        if not ids:
            return _CleanupReport("not_found")

        unknown_id: str | None = None
        for orphan_id in ids:
            try:
                client.machines.delete(orphan_id)
            except Exception:
                logger.warning(
                    "Failed to delete boxd machine %s left by a failed create; "
                    "delete it manually.",
                    orphan_id,
                    exc_info=True,
                )
                unknown_id = unknown_id or orphan_id
            else:
                logger.warning(
                    "Deleted boxd machine %s left by a failed create.",
                    orphan_id,
                )
        if unknown_id is not None:
            return _CleanupReport("unknown", unknown_id)
        return _CleanupReport("deleted", ids[0] if len(ids) == 1 else None)


def _check_config(config: SandboxConfig) -> None:
    """Refuse what a boxd machine cannot honor, before anything is created."""
    if config.gpu is not None:
        raise FeatureNotSupportedError(
            "boxd machines have no GPUs",
            backend="boxd",
            op="create",
        )
    if config.image:
        raise FeatureNotSupportedError(
            "boxd machines boot boxd's Ubuntu 24.04 image (Python, Node, "
            "Docker and git preinstalled) and cannot boot an OCI image; run "
            "the image inside the machine with docker, or restore a boxd "
            "snapshot with snapshot_id",
            backend="boxd",
            op="create",
        )
    if config.disk_mb is not None and config.disk_mb > _DISK_MB:
        raise SandboxConfigurationError(
            f"boxd machines have a fixed {_DISK_MB // 1024} GiB disk; "
            f"disk_mb={config.disk_mb} does not fit",
            backend="boxd",
            op="create",
        )


def _check_env(env_vars: dict[str, str]) -> dict[str, str]:
    for key in env_vars:
        if not _ENV_NAME_RE.match(key):
            raise SandboxConfigurationError(
                f"Invalid environment variable name: {key!r}",
                backend="boxd",
                op="create",
            )
    return dict(env_vars)


def _split_options(options: dict) -> tuple[dict, dict]:
    """Split backend_options into machines.create() kwargs and adapter keys."""
    native = {k: v for k, v in options.items() if k not in _ADAPTER_OPTIONS}
    adapter = {k: v for k, v in options.items() if k in _ADAPTER_OPTIONS}
    if "isolated" in native and not native["isolated"]:
        raise SandboxConfigurationError(
            "boxd sandboxes are always created isolated; drop "
            "backend_options['isolated']",
            backend="boxd",
            op="create",
        )
    return native, adapter


def _check_create_options(client: Any, options: dict) -> None:
    """Reject backend_options machines.create() would not accept."""
    try:
        params = inspect.signature(client.machines.create).parameters
    except (TypeError, ValueError):
        return
    if any(p.kind is p.VAR_KEYWORD for p in params.values()):
        return
    unknown = sorted(k for k in options if k not in params or k == "name")
    if unknown:
        accepted = sorted(
            set(params) - {"name", "self"} | _ADAPTER_OPTIONS | {"org"}
        )
        raise SandboxConfigurationError(
            f"Unknown boxd backend_options {unknown}; accepted keys are "
            f"{', '.join(accepted)}",
            backend="boxd",
            op="create",
        )


def _machine_size(config: SandboxConfig) -> tuple[int, int]:
    """Return the smallest boxd size holding ``cpu`` and ``memory_mb``."""
    for vcpu, memory_mb in _SIZES:
        if config.cpu <= vcpu and config.memory_mb <= memory_mb:
            return vcpu, memory_mb
    raise SandboxConfigurationError(
        "boxd machines come in 1 vCPU/4 GiB, 2/8, 4/16 and 8/32; "
        f"cpu={config.cpu}, memory_mb={config.memory_mb} fits none of them",
        backend="boxd",
        op="create",
    )


def _create_kwargs(
    config: SandboxConfig, options: dict, *, org: str | None, create_token: str
) -> dict:
    """Keyword arguments for ``client.machines.create()``."""
    # Sandboxes run untrusted code.  An isolated machine has no in-VM boxd
    # CLI, no account integrations and no agent logins, so code inside it
    # cannot act as the account that created it.
    kwargs: dict = {"isolated": True}
    if org:
        kwargs["org"] = org
    if config.snapshot_id:
        # A restore keeps the size the snapshot was taken at.
        kwargs["from_snapshot"] = config.snapshot_id
    else:
        vcpu, memory_mb = _machine_size(config)
        kwargs["vcpu"] = vcpu
        kwargs["memory"] = f"{memory_mb // 1024}G"
    # Standby pauses the machine after a quiet spell; a sandbox running a
    # long job between commands must not be paused under it.
    kwargs["auto_suspend_timeout"] = 0
    # Machines never expire on their own.  timeout_secs is documented as the
    # sandbox's lifetime, so an explicit value becomes delete_after, which the
    # platform enforces in any state, running work included.  Like the
    # Daytona TTL it is set only when the caller asked for it: a preset or
    # dataclass default must not silently delete a machine mid-run.
    if config.timeout_secs_explicit:
        kwargs["delete_after"] = max(1, math.ceil(config.timeout_secs))
    # backend_options wins, as the documented escape hatch, except that the
    # create token is stamped last so a caller's labels add to it.
    kwargs.update(options)
    kwargs["labels"] = {
        **(kwargs.get("labels") or {}),
        _CREATE_TOKEN_LABEL: create_token,
    }
    return kwargs


def _effective_policy(config: SandboxConfig) -> NetworkPolicy | None:
    """Return ``network`` when given, else the legacy ``allow_internet`` flag."""
    if config.network is not None:
        return config.network
    if not config.allow_internet:
        return NetworkPolicy(mode="none")
    return None


def _egress_entries(policy: NetworkPolicy | None, *, op: str) -> list[str]:
    """Map a policy onto a boxd egress allow-list; ``[]`` is unrestricted.

    boxd admits HTTP and HTTPS by hostname (SNI or Host) and everything else
    by address, so a CIDR does not open HTTPS to a bare IP in that range.
    """
    if policy is None or policy.mode == "public":
        return []
    if policy.mode == "none":
        return [_DENY_ALL_ENTRY]
    for cidr in policy.cidrs:
        network = ipaddress.ip_network(cidr, strict=False)
        if network.version != 4 or not network.is_global:
            raise SandboxConfigurationError(
                f"boxd allow-lists take public IPv4 addresses, got {cidr!r}",
                backend="boxd",
                op=op,
            )
    return [*policy.domains, *policy.cidrs]


def _settle_egress(since: float) -> None:
    """Wait until an allow-list set at ``since`` is enforced."""
    remaining = _EGRESS_SETTLE_SECS - (time.monotonic() - since)
    if remaining > 0:
        time.sleep(remaining)


def _wait_for_tls(host: str, timeout: float) -> bool:
    """Return True once *host* completes a TLS handshake on port 443."""
    deadline = time.monotonic() + timeout
    context = ssl.create_default_context()
    while True:
        try:
            with socket.create_connection((host, 443), timeout=10) as sock:
                with context.wrap_socket(sock, server_hostname=host):
                    return True
        except OSError:  # ssl.SSLError included: the certificate is not out yet
            if time.monotonic() >= deadline:
                return False
            time.sleep(1.0)


def _machine_domain(machine: object) -> str:
    access = getattr(machine, "access", None)
    return getattr(access, "domain", None) or "boxd.sh"


def _wake(client: Any, machine: Any) -> None:
    """Bring a machine back to running so exec works, then wait for it."""
    status = machine.status
    machine_id = machine.id
    if status == "suspended":
        client.machines.resume(machine_id)
    elif status == "hibernated":
        client.machines.wake(machine_id)
    elif status == "stopped":
        client.machines.start(machine_id)
    if status != "running":
        client.machines.wait_until_ready(machine_id)


class BoxdSession:
    """One live boxd machine.

    Commands run through ``bash -c`` as the machine's ``boxd`` user, who has
    passwordless sudo, in ``workdir`` when it is set and in ``/home/boxd``
    otherwise.  ``env_vars`` are exported into every command instead of being
    stored on the machine, so they never reach its disk or its snapshots.

    Files go through boxd's file API (``machines.fs``) as the ``boxd`` user:
    relative paths resolve under ``/home/boxd``, a write creates missing
    parent directories, keeps an existing file's mode and writes through a
    symlink, and paths the ``boxd`` user cannot write or read are handled
    through sudo.  A machine started before boxd had that API, such as one
    restored from an older snapshot, refuses file operations with a
    ``SandboxExecutionError`` until it is rebooted.
    """

    def __init__(
        self,
        *,
        client: Any,
        machine_id: str,
        name: str,
        domain: str = "boxd.sh",
        workdir: str | None = None,
        env_vars: dict[str, str] | None = None,
        org: str | None = None,
        snapshot_timeout_secs: float = _DEFAULT_SNAPSHOT_TIMEOUT_SECS,
    ) -> None:
        self._client = client
        self._machine_id: str | None = machine_id
        self._name = name
        self._domain = domain
        self._workdir = workdir
        self._env_vars = dict(env_vars or {})
        self._org = org
        self._snapshot_timeout_secs = snapshot_timeout_secs

    @property
    def provider_resource_id(self) -> str | None:
        """Return the boxd machine ID until the machine is destroyed."""
        return self._machine_id

    # -- Internal ----------------------------------------------------------

    def _call(
        self,
        op: str,
        fn: Callable[[], T],
        *,
        missing: type[SandboxError] = SandboxNotFoundError,
    ) -> T:
        """Run one SDK call, starting a stopped machine once if needed."""
        if self._machine_id is None:
            raise SandboxNotFoundError(
                "The boxd machine has already been destroyed",
                backend="boxd",
                op=op,
            )
        from boxd import ConflictError, MachineFilesystemError

        try:
            try:
                return fn()
            except ConflictError as exc:
                # Standby and hibernated machines wake on a call by themselves;
                # a stopped one has to be started first.  A file that is a
                # directory, say, is a ConflictError too, but about the path.
                if isinstance(exc, MachineFilesystemError) or (
                    self._client.machines.get(self._machine_id).status
                    != "stopped"
                ):
                    raise
                self._client.machines.start(self._machine_id)
                self._client.machines.wait_until_ready(self._machine_id)
                return fn()
        except SandboxError:
            raise
        except Exception as exc:
            raise _classify_boxd_error(exc, op=op, missing=missing) from exc

    def _script(self, command_line: str) -> str:
        """Wrap a command line with the session env, workdir and keepalive.

        The command runs in its own ``bash -c``, so an ``exit`` or ``exec``
        in it still lets this shell stop the keepalive and pass the exit code
        on.
        """
        prefix = "".join(
            f"export {key}={shlex.quote(value)}; "
            for key, value in self._env_vars.items()
        )
        if self._workdir:
            prefix += f"cd -- {shlex.quote(self._workdir)} || exit 1; "
        return (
            f"{prefix}{_KEEPALIVE}bash -c {shlex.quote(command_line)}; "
            "__bsb_rc=$?; kill $__bsb_keepalive 2>/dev/null; exit $__bsb_rc"
        )

    def _exec(
        self, command_line: str, *, op: str = "execute", buffer: bool = False
    ) -> Any:
        script = self._script(command_line)
        return self._call(
            op,
            lambda: self._client.machines.exec(
                self._machine_id,
                ["bash", "-c", script],
                encoding="buffer" if buffer else "utf-8",
            ),
        )

    def _run_checked(
        self, command_line: str, *, op: str, buffer: bool = False
    ) -> Any:
        """Run a helper command outside the session env; raise on failure."""
        result = self._call(
            op,
            lambda: self._client.machines.exec(
                self._machine_id,
                ["bash", "-c", command_line],
                encoding="buffer" if buffer else "utf-8",
            ),
        )
        if result.exit_code != 0:
            stderr = result.stderr
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
            raise SandboxExecutionError(
                f"boxd {op} failed: {stderr.strip()}",
                backend="boxd",
                op=op,
                context={"exit_code": result.exit_code},
            )
        return result

    def _prepare_dirs(self) -> None:
        """Create /workspace and ``workdir`` for the machine user if missing."""
        dirs = [_WORKSPACE_DIR]
        if self._workdir and self._workdir != _WORKSPACE_DIR:
            dirs.append(self._workdir)
        self._run_checked(
            " && ".join(
                f"{{ [ -d {shlex.quote(d)} ] || sudo -n install -d "
                f'-o "$(id -un)" -g "$(id -gn)" -- {shlex.quote(d)}; }}'
                for d in dirs
            ),
            op="create",
        )

    # -- Execution ---------------------------------------------------------

    def execute_code(
        self, code: str, language: str = "python"
    ) -> SandboxResult:
        return self.execute_command(language, ["-c", code])

    def execute_command(
        self, command: str, args: list[str] | None = None
    ) -> SandboxResult:
        command_line = command if not args else shlex.join([command, *args])
        result = self._exec(command_line)
        return SandboxResult(
            stdout=result.stdout,
            # The keepalive NULs; a NUL the command itself wrote goes too.
            stderr=result.stderr.replace("\x00", ""),
            exit_code=result.exit_code,
        )

    # -- Files -------------------------------------------------------------

    def list_files(self, path: str = "/") -> list[FileInfo]:
        try:
            entries = self._call(
                "list_files",
                lambda: self._client.machines.fs.list(self._machine_id, path),
            )
        except SandboxError as exc:
            if not _denied(exc):
                raise
            return self._find(path)
        base = path.rstrip("/")
        # An entry is described by what it resolves to: a symlink to a
        # directory lists as a directory, a dangling one as neither.
        return sorted(
            (
                FileInfo(
                    path=f"{base}/{entry.name}",
                    is_dir=entry.target_type == "directory",
                    size=entry.size if entry.target_type == "file" else None,
                )
                for entry in entries
            ),
            key=lambda info: info.path,
        )

    def _find(self, path: str) -> list[FileInfo]:
        """List a directory with sudo find: complete, and root-readable."""
        result = self._run_checked(
            f"sudo -n find {shlex.quote(path)} -mindepth 1 -maxdepth 1 "
            r"-printf '%y\t%s\t%p\0'",
            op="list_files",
            buffer=True,
        )
        files: list[FileInfo] = []
        for record in result.stdout.split(b"\0"):
            parts = record.split(b"\t", 2)
            if len(parts) != 3:
                continue
            file_type, size, file_path = parts
            files.append(
                FileInfo(
                    path=file_path.decode(errors="replace"),
                    is_dir=file_type == b"d",
                    size=None
                    if file_type == b"d" or not size.isdigit()
                    else int(size),
                )
            )
        return sorted(files, key=lambda info: info.path)

    def read_file(self, path: str) -> bytes:
        try:
            return self._call(
                "read_file",
                lambda: self._client.machines.fs.read(self._machine_id, path),
            )
        except SandboxError as exc:
            if not _denied(exc):
                raise
        # The boxd user cannot read the path; root can.
        return self._run_checked(
            f"sudo -n cat -- {shlex.quote(path)}", op="read_file", buffer=True
        ).stdout

    def write_file(self, path: str, content: bytes | str) -> None:
        data = content if isinstance(content, bytes) else content.encode()
        try:
            self._call(
                "write_file",
                lambda: self._client.machines.fs.write(
                    self._machine_id, path, data
                ),
            )
        except SandboxError as exc:
            if not _denied(exc):
                raise
            self._sudo_write(path, data)

    def _sudo_write(self, path: str, data: bytes) -> None:
        """Write a path the boxd user cannot: stage it in /tmp, then sudo cp.

        ``cp -T`` writes into an existing file, so it keeps the file's mode
        and owner and writes through a symlink, as the file API does for the
        ``boxd`` user's own paths; a new file is root's.
        """
        staging = f"/tmp/.bespokelabs-sandbox-{uuid.uuid4().hex}"
        self._call(
            "write_file",
            lambda: self._client.machines.fs.write(
                self._machine_id, staging, data
            ),
        )
        parent = posixpath.dirname(path) or "."
        quoted_staging = shlex.quote(staging)
        self._run_checked(
            f"sudo -n mkdir -p -- {shlex.quote(parent)} && "
            f"sudo -n cp -T -- {quoted_staging} {shlex.quote(path)}; "
            f"status=$?; rm -f -- {quoted_staging}; exit $status",
            op="write_file",
        )

    def upload_file(self, local_path: str, remote_path: str) -> None:
        try:
            data = pathlib.Path(local_path).read_bytes()
        except OSError as exc:
            raise SandboxExecutionError(
                f"boxd upload_file failed: {exc}",
                backend="boxd",
                op="upload_file",
            ) from exc
        self.write_file(remote_path, data)

    def download_file(self, remote_path: str, local_path: str) -> None:
        data = self.read_file(remote_path)
        try:
            pathlib.Path(local_path).write_bytes(data)
        except OSError as exc:
            raise SandboxExecutionError(
                f"boxd download_file failed: {exc}",
                backend="boxd",
                op="download_file",
            ) from exc

    # -- Ports and network -------------------------------------------------

    def expose_port(self, port: int) -> ExposedPort:
        """Return a public HTTPS URL for *port*.

        Adds a named boxd proxy, ``port-<n>.<machine>.boxd.sh``, and waits
        until it serves TLS.  The URL is not authenticated: anyone who has it
        reaches the port, so no headers are returned.  A proxy that already
        exists for the port is reused.
        """
        label = f"port-{port}"

        def _expose() -> str:
            proxies = self._client.machines.proxies
            routes = {p.name: p for p in proxies.list(self._machine_id)}
            route = routes.get(label)
            if route is None or route.port != port:
                if route is not None:
                    proxies.delete(self._machine_id, label)
                proxies.create(self._machine_id, label, port)
                routes = {p.name: p for p in proxies.list(self._machine_id)}
                route = routes.get(label)
            domain = getattr(route, "domain", None)
            return domain or f"{label}.{self._name}.{self._domain}"

        domain = self._call("expose_port", _expose)
        if not _wait_for_tls(domain, _PORT_READY_TIMEOUT_SECS):
            logger.warning(
                "boxd route %s did not serve TLS within %s s; it may need a "
                "little longer.",
                domain,
                _PORT_READY_TIMEOUT_SECS,
            )
        return ExposedPort(port=port, url=f"https://{domain}")

    def set_network(self, policy: NetworkPolicy) -> None:
        entries = _egress_entries(policy, op="set_network")
        self._call(
            "set_network",
            lambda: self._client.machines.set_egress_allow(
                self._machine_id, entries
            ),
        )
        _settle_egress(time.monotonic())

    # -- Lifecycle ---------------------------------------------------------

    def snapshot(self) -> SnapshotInfo:
        """Capture memory and disk; returns once the snapshot is ready.

        Pass the returned ``snapshot_id`` as ``snapshot_id=`` to create a new
        machine from it.  Snapshots outlive the sandbox; delete them with
        ``boxd snapshot remove`` when they are no longer needed.
        """
        name = f"bespokelabs-{uuid.uuid4().hex[:12]}"
        self._call(
            "snapshot",
            lambda: self._client.snapshots.create(self._machine_id, name),
        )
        deadline = time.monotonic() + self._snapshot_timeout_secs
        while True:
            snap = self._call(
                "snapshot",
                lambda: self._client.snapshots.get(name, org=self._org),
            )
            if snap.status == "ready":
                created = getattr(snap, "created_at", None)
                return SnapshotInfo(
                    snapshot_id=name,
                    backend="boxd",
                    created_at=created.isoformat() if created else None,
                )
            if snap.status == "failed":
                raise SandboxExecutionError(
                    f"boxd snapshot {name!r} failed",
                    backend="boxd",
                    op="snapshot",
                )
            if time.monotonic() >= deadline:
                raise SandboxTimeoutError(
                    f"boxd snapshot {name!r} was not ready within "
                    f"{self._snapshot_timeout_secs} seconds",
                    backend="boxd",
                    op="snapshot",
                )
            time.sleep(1.0)

    def session_state(self) -> dict:
        if self._machine_id is None:
            raise SandboxNotFoundError(
                "The boxd machine has already been destroyed",
                backend="boxd",
                op="session_state",
            )
        state: dict = {"machine_id": self._machine_id}
        if self._workdir:
            state["workdir"] = self._workdir
        if self._env_vars:
            # Applied per command rather than stored on the machine, so they
            # travel with the handle to stay in effect after resume.
            state["env_vars"] = dict(self._env_vars)
        if self._org:
            state["org"] = self._org
        if self._snapshot_timeout_secs != _DEFAULT_SNAPSHOT_TIMEOUT_SECS:
            state["snapshot_timeout_secs"] = self._snapshot_timeout_secs
        return state

    def destroy(self) -> None:
        if self._machine_id is None:
            return
        from boxd import NotFoundError

        try:
            self._client.machines.delete(self._machine_id)
        except NotFoundError:
            pass  # Already gone, which is what destroy() asks for.
        except Exception as exc:
            raise _classify_boxd_error(exc, op="destroy") from exc
        self._machine_id = None
