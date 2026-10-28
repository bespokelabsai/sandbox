"""Daytona sandbox backend."""

from __future__ import annotations

import ipaddress
import logging
import math
import os
import pathlib
import shlex
import threading
import time
import uuid
from dataclasses import dataclass

from bespokelabs.sandbox import _metrics
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

# Label stamped on every sandbox this backend creates, carrying a token unique
# to the create call.  Daytona filters on labels server-side, so this token is
# the only handle we have on a sandbox whose create call failed *after* the
# server had already built it.
_CREATE_TOKEN_LABEL = "bespokelabs.sandbox/create-token"

# backend_options key that overrides the deadline passed to Daytona.create().
_CREATE_TIMEOUT_OPTION = "create_timeout"
# backend_options key setting the in-flight exec keepalive interval in seconds
# (0 disables it); see _Keepalive.  The env var is the fallback, which also
# covers resume().
_KEEPALIVE_OPTION = "keepalive_interval_secs"
_KEEPALIVE_ENV = "BESPOKELABS_SANDBOX_DAYTONA_KEEPALIVE_SECS"
_DEFAULT_KEEPALIVE_SECS = 300.0
# Bounds on one refresh_activity call, and on how many failures in a row the
# keepalive tolerates before it gives up for the rest of that exec.
_KEEPALIVE_REQUEST_TIMEOUT_SECS = 30.0
_KEEPALIVE_MAX_CONSECUTIVE_FAILURES = 3
# backend_options key with extra metric tags (e.g. {"caller": "my-job"}).
_METRIC_TAGS_OPTION = "metric_tags"
# backend_options key mapping a metric tag name to the sandbox label whose
# value becomes that tag (e.g. {"pipeline": "my-pipeline-label"}).  The env
# var is the fallback, as "tag=label,tag=label".  There is no default mapping.
_METRIC_TAG_LABELS_OPTION = "metric_tag_labels"
_METRIC_TAG_LABELS_ENV = "BESPOKELABS_SANDBOX_METRIC_TAG_LABELS"
# Options this backend consumes itself; none of them reach the params object.
_LOCAL_OPTIONS = frozenset(
    {
        _CREATE_TIMEOUT_OPTION,
        _KEEPALIVE_OPTION,
        _METRIC_TAGS_OPTION,
        _METRIC_TAG_LABELS_OPTION,
    }
)

IDLE_GAP_METRIC = "bespokelabs.sandbox.idle_gap_seconds"
MAX_IDLE_GAP_METRIC = "bespokelabs.sandbox.max_idle_gap_seconds"
_CALLER_ENV = "BESPOKELABS_SANDBOX_CALLER"

# Lifecycle defaults for a sandbox whose caller set none (see
# _apply_lifecycle_defaults).
#
# Idle stop: Daytona resets this clock on every Toolbox API call, and every
# exec, file read and file write this backend makes is one.  Processes inside
# the sandbox do not reset it, and Daytona does not document whether a call
# still in flight counts as activity.  A long single blocking agent call can
# run past an hour, and so make no Toolbox call for well over an hour while
# the work is very much alive, so
# the window is 4 hours, not 1.  A driver silent for that long is gone; the
# idle stop is a backstop for a killed driver, not a tight cost control.  The
# in-flight keepalive (_Keepalive) now refreshes activity under a blocking
# exec; with it turned off, a call that may run longer still should pass an
# explicit backend_options auto_stop_interval.  The 4 hours is a placeholder
# until the max_idle_gap_seconds metric gives an evidence-based value.
_DEFAULT_AUTO_STOP_MINUTES = 4 * 60
# Delete after stop: this backend never stops a sandbox itself and resume()
# does not restart one, so a stopped sandbox is already unusable from here.
# The hour is only a grace period for someone to inspect or restart it by
# hand; after that it is disk and quota held for nothing.
_DEFAULT_AUTO_DELETE_MINUTES = 60
# Wall-clock backstop when no timeout_secs was given: destroys the sandbox in
# any state, active or not, so it also bounds a sandbox kept awake by a
# runaway poller.  A day is far above any run the agent presets plan for
# (their recommended timeouts are 30 minutes), so it cannot cut off normal
# work; anything longer passes timeout_secs, or ttl_minutes=0 to disable.
_DEFAULT_TTL_MINUTES = 24 * 60


@dataclass(frozen=True)
class _CleanupReport:
    status: str
    provider_resource_id: str | None = None


def _classify_daytona_error(
    exc: Exception,
    *,
    op: str,
    cleanup: _CleanupReport | None = None,
) -> SandboxError:
    """Translate Daytona SDK exception types into the public error taxonomy."""
    try:
        from daytona.common.errors import (  # type: ignore[import-untyped]
            DaytonaAuthenticationError,
            DaytonaConnectionError,
            DaytonaForbiddenError,
            DaytonaNotFoundError,
            DaytonaTimeoutError,
        )
    except ImportError:  # pragma: no cover - DaytonaClient guards installation
        DaytonaAuthenticationError = ()
        DaytonaConnectionError = ()
        DaytonaForbiddenError = ()
        DaytonaNotFoundError = ()
        DaytonaTimeoutError = ()

    context: dict[str, str | int] = {}
    provider_status = getattr(exc, "status_code", None)
    provider_code = getattr(exc, "code", None)
    if isinstance(provider_status, int):
        context["provider_status"] = provider_status
    if isinstance(provider_code, str):
        context["provider_code"] = provider_code
    if cleanup is not None:
        context["cleanup_status"] = cleanup.status
        if cleanup.provider_resource_id:
            context["provider_resource_id"] = cleanup.provider_resource_id

    outcome = (
        ErrorOutcome.UNKNOWN
        if cleanup is not None and cleanup.status == "unknown"
        else ErrorOutcome.FAILED
    )
    common = {
        "backend": "daytona",
        "op": op,
        "context": context,
        "outcome": outcome,
    }
    if isinstance(exc, (DaytonaAuthenticationError, DaytonaForbiddenError)):
        return SandboxAuthenticationError(
            "Daytona authentication failed.", **common
        )
    if isinstance(exc, DaytonaTimeoutError) or isinstance(exc, TimeoutError):
        return SandboxTimeoutError(
            f"Daytona {op} timed out.",
            retryable=outcome is ErrorOutcome.FAILED,
            **common,
        )
    if isinstance(exc, DaytonaConnectionError) or isinstance(
        exc, ConnectionError
    ):
        return SandboxConnectionError(
            f"Daytona {op} connection failed.",
            retryable=outcome is ErrorOutcome.FAILED,
            **common,
        )
    if isinstance(exc, DaytonaNotFoundError):
        return SandboxNotFoundError(
            f"Daytona resource was not found during {op}.", **common
        )
    if op == "create":
        # Preserve the historical local-library message for opaque/older SDK
        # failures. The hosted API never serializes this provider text.
        return SandboxCreationError(
            f"Failed to create Daytona sandbox: {exc}", **common
        )
    return SandboxExecutionError(f"Daytona {op} failed.", **common)


class DaytonaClient:
    """Factory for Daytona sandboxes.

    The authenticated SDK client is built on first create() (reading
    DAYTONA_* env vars at that point) and reused for every session
    created through this client.
    """

    supports_dockerfile = True
    network_modes = frozenset({"public", "none", "allowlist"})
    network_option_keys = frozenset(
        {"network_block_all", "network_allow_list", "domain_allow_list"}
    )

    def __init__(self) -> None:
        try:
            from daytona import (  # type: ignore[import-untyped]
                Daytona,
                DaytonaConfig,
            )
        except ImportError as exc:
            raise BackendNotInstalledError(
                "Daytona SDK not installed. Run: pip install bespokelabs-sandbox[daytona]"
            ) from exc
        self._daytona_cls = Daytona
        self._daytona_config_cls = DaytonaConfig
        self._client: object = None
        # create() may run concurrently (e.g. via AsyncSandboxClient);
        # guard the one-time authenticated client construction.
        self._connect_lock = threading.Lock()

    def _ensure_client(self) -> object:
        if self._client is None:
            with self._connect_lock:
                if self._client is None:
                    api_key = os.environ.get("DAYTONA_API_KEY")
                    if not api_key:
                        raise SandboxAuthenticationError(
                            "DAYTONA_API_KEY environment variable is not set",
                            backend="daytona",
                            op="create",
                        )
                    self._client = self._daytona_cls(
                        self._daytona_config_cls(
                            api_key=api_key,
                            api_url=os.environ.get(
                                "DAYTONA_API_URL", "https://app.daytona.io/api"
                            ),
                            target=os.environ.get("DAYTONA_TARGET", "us"),
                        )
                    )
        return self._client

    def resume(self, data: dict) -> DaytonaSession:
        client = self._ensure_client()
        getter = getattr(client, "get", None) or getattr(
            client, "find_one", None
        )
        if getter is None:
            raise FeatureNotSupportedError(
                "This Daytona SDK version has no get()/find_one(); cannot resume by id"
            )
        try:
            sandbox = getter(data["sandbox_id"])
        except Exception as exc:
            raise _classify_daytona_error(exc, op="connect") from exc
        return DaytonaSession(
            client=client,
            sandbox=sandbox,
            workdir=data.get("workdir"),
            keepalive_secs=data.get(_KEEPALIVE_OPTION),
            metric_tags=data.get(_METRIC_TAGS_OPTION),
            metric_tag_labels=data.get(_METRIC_TAG_LABELS_OPTION),
        )

    def create(self, config: SandboxConfig) -> DaytonaSession:
        self._ensure_client()
        keepalive_secs = _keepalive_option(config)
        metric_tags = _metric_tags_option(config)
        metric_tag_labels = _metric_tag_labels_option(config)

        # Unique to this create call.  If the call fails after Daytona has
        # already built the sandbox, this token is how we find the orphan.
        create_token = uuid.uuid4().hex
        create_kwargs = _create_kwargs(config)
        _check_dockerfile_context(config)
        # Validate here so a bad policy is a config error, not a create error.
        _network_params(config.network)

        try:
            params = _build_params(config, create_token=create_token)
            sandbox = self._client.create(params, **create_kwargs)
            if config.workdir:
                # process.exec(cwd=...) does not create the directory, so make
                # it once here.  Inside the try, so a failure is still reaped.
                sandbox.process.exec(f"mkdir -p {shlex.quote(config.workdir)}")
        except Exception as exc:
            cleanup = self._reap_orphan(create_token)
            raise _classify_daytona_error(
                exc, op="create", cleanup=cleanup
            ) from exc
        except BaseException:
            # KeyboardInterrupt / SystemExit: still reap, then propagate as-is.
            self._reap_orphan(create_token)
            raise

        return DaytonaSession(
            client=self._client,
            sandbox=sandbox,
            workdir=config.workdir,
            keepalive_secs=keepalive_secs,
            metric_tags=metric_tags,
            metric_tag_labels=metric_tag_labels,
        )

    def _reap_orphan(self, create_token: str) -> _CleanupReport:
        """Delete the sandbox a failed create may have left running.

        When the create request's HTTP response times out, the sandbox exists
        server-side -- started, and billing -- and only the reply was lost.  The
        Daytona SDK's exception carries no sandbox id: not as an attribute, not
        in the message, and not in ``__cause__`` (its ``intercept_errors``
        decorator re-raises ``from None``).  No caller above this layer can
        clean that up, so it has to happen here, using the create-token label
        ``_build_params`` stamped on the sandbox as the only remaining handle.

        Best effort by design: any failure here is logged and swallowed so it
        can never mask the creation error the caller actually needs to see.
        """
        try:
            from daytona import (
                ListSandboxesQuery,  # type: ignore[import-untyped]
            )

            query = ListSandboxesQuery(
                labels={_CREATE_TOKEN_LABEL: create_token}
            )
            # list() is a generator, so materialize it inside the guard.
            orphans = list(self._client.list(query))
        except Exception:
            logger.warning(
                "Could not search for a Daytona sandbox orphaned by a failed create "
                "(label %s=%s); it may still be running and billing.",
                _CREATE_TOKEN_LABEL,
                create_token,
                exc_info=True,
            )
            return _CleanupReport("unknown")

        if not orphans:
            return _CleanupReport("not_found")

        deleted_ids: list[str] = []
        unknown_id: str | None = None
        for orphan in orphans:
            orphan_id = getattr(orphan, "id", "<unknown>")
            try:
                self._client.delete(orphan)
            except Exception:
                logger.warning(
                    "Failed to delete Daytona sandbox %s orphaned by a failed create; delete it manually.",
                    orphan_id,
                    exc_info=True,
                )
                if unknown_id is None and orphan_id != "<unknown>":
                    unknown_id = str(orphan_id)
            else:
                deleted_ids.append(str(orphan_id))
                logger.warning(
                    "Deleted Daytona sandbox %s orphaned by a failed create.",
                    orphan_id,
                )
        if unknown_id is not None or len(deleted_ids) != len(orphans):
            return _CleanupReport("unknown", unknown_id)
        return _CleanupReport(
            "deleted", deleted_ids[0] if len(deleted_ids) == 1 else None
        )


def _create_kwargs(config: SandboxConfig) -> dict:
    """Keyword arguments for ``Daytona.create()`` itself (not the params object)."""
    raw = config.backend_options.get(_CREATE_TIMEOUT_OPTION)
    if raw is None:
        return {}
    try:
        timeout = float(raw)
    except (TypeError, ValueError) as exc:
        raise SandboxConfigurationError(
            f"backend_options['{_CREATE_TIMEOUT_OPTION}'] must be a number of seconds, got {raw!r}",
            backend="daytona",
            op="create",
        ) from exc
    if timeout <= 0:
        raise SandboxConfigurationError(
            f"backend_options['{_CREATE_TIMEOUT_OPTION}'] must be positive; the Daytona SDK treats 0 as "
            "'no timeout at all', so pass a large finite value instead.",
            backend="daytona",
            op="create",
        )
    return {"timeout": timeout}


def _keepalive_option(config: SandboxConfig) -> float | None:
    """Return the keepalive interval from backend_options, or ``None``."""
    raw = config.backend_options.get(_KEEPALIVE_OPTION)
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise SandboxConfigurationError(
            f"backend_options['{_KEEPALIVE_OPTION}'] must be a number of "
            f"seconds (0 disables), got {raw!r}",
            backend="daytona",
            op="create",
        ) from exc
    if value < 0 or not math.isfinite(value):
        raise SandboxConfigurationError(
            f"backend_options['{_KEEPALIVE_OPTION}'] must be >= 0, got {raw!r}",
            backend="daytona",
            op="create",
        )
    return value


def _metric_tags_option(config: SandboxConfig) -> dict[str, str] | None:
    raw = config.backend_options.get(_METRIC_TAGS_OPTION)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SandboxConfigurationError(
            f"backend_options['{_METRIC_TAGS_OPTION}'] must be a dict of "
            f"tag name to value, got {raw!r}",
            backend="daytona",
            op="create",
        )
    return {str(k): str(v) for k, v in raw.items()}


def _metric_tag_labels_option(
    config: SandboxConfig,
) -> dict[str, str] | None:
    """Return the tag-to-label mapping from backend_options, or ``None``."""
    raw = config.backend_options.get(_METRIC_TAG_LABELS_OPTION)
    if raw is None:
        return None
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and k and isinstance(v, str) and v
        for k, v in raw.items()
    ):
        raise SandboxConfigurationError(
            f"backend_options['{_METRIC_TAG_LABELS_OPTION}'] must be a dict "
            f"of metric tag name to sandbox label key, got {raw!r}",
            backend="daytona",
            op="create",
        )
    return dict(raw)


def _env_metric_tag_labels() -> dict[str, str] | None:
    """Parse ``tag=label,tag=label`` from the env var, or return ``None``.

    A malformed entry is logged and skipped; it never fails the caller.
    """
    raw = os.environ.get(_METRIC_TAG_LABELS_ENV)
    if not raw:
        return None
    mapping: dict[str, str] = {}
    for raw_entry in raw.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        tag, sep, label = (part.strip() for part in entry.partition("="))
        if not sep or not tag or not label:
            logger.warning(
                "Ignoring %r in %s: expected tag=label",
                entry,
                _METRIC_TAG_LABELS_ENV,
            )
            continue
        mapping[tag] = label
    return mapping or None


def _env_keepalive_secs() -> float | None:
    """Return a valid interval from the env var, or ``None``."""
    raw = os.environ.get(_KEEPALIVE_ENV)
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "Ignoring %s=%r: not a number of seconds", _KEEPALIVE_ENV, raw
        )
        return None
    if value >= 0 and math.isfinite(value):
        return value
    logger.warning("Ignoring %s=%r: must be >= 0", _KEEPALIVE_ENV, raw)
    return None


def _resolve_keepalive_secs(explicit: float | None) -> float:
    """Return the explicit interval, else the env var, else the default."""
    if explicit is not None:
        return float(explicit)
    from_env = _env_keepalive_secs()
    return _DEFAULT_KEEPALIVE_SECS if from_env is None else from_env


def _sandbox_class(sandbox: object) -> str:
    """Return a coarse size class such as ``cpu2-mem4g``, or ``unknown``."""

    def num(name: str) -> float | None:
        value = getattr(sandbox, name, None)
        return float(value) if isinstance(value, (int, float)) else None

    cpu, memory, gpu = num("cpu"), num("memory"), num("gpu")
    if cpu is None and memory is None:
        return "unknown"
    parts = []
    if cpu is not None:
        parts.append(f"cpu{cpu:g}")
    if memory is not None:
        parts.append(f"mem{memory:g}g")
    if gpu:
        parts.append(f"gpu{gpu:g}")
    return "-".join(parts)


class _Keepalive:
    """Call ``refresh_activity()`` on a timer while one blocking call runs.

    Daytona resets its idle auto-stop clock on Toolbox calls but does not
    document whether a call still in flight counts, so a long blocking exec
    could be idle-stopped mid-run.  ``POST /sandbox/{id}/last-activity``
    (``Sandbox.refresh_activity``) resets the clock explicitly.

    Best effort and bounded: each refresh has a request timeout, failures are
    logged and never reach the exec, the thread gives up after a few failures
    in a row, and it is a daemon thread that stop() joins only briefly.
    """

    def __init__(
        self, sandbox: object, interval: float, on_refresh=None
    ) -> None:
        self._sandbox = sandbox
        self._interval = interval
        self._on_refresh = on_refresh
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.refreshes = 0
        self.failures = 0

    def __enter__(self) -> _Keepalive:
        refresh = getattr(self._sandbox, "refresh_activity", None)
        if self._interval > 0 and callable(refresh):
            self._thread = threading.Thread(
                target=self._run,
                args=(refresh,),
                name="daytona-keepalive",
                daemon=True,
            )
            self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._stop.set()
        if self._thread is not None:
            # A refresh in flight finishes on its own (it is bounded by
            # _KEEPALIVE_REQUEST_TIMEOUT_SECS); never hold up the exec result.
            self._thread.join(timeout=1.0)

    def _run(self, refresh) -> None:
        consecutive = 0
        while not self._stop.wait(self._interval):
            try:
                refresh(request_timeout=_KEEPALIVE_REQUEST_TIMEOUT_SECS)
            except Exception:  # noqa: BLE001 - never fail the exec
                self.failures += 1
                consecutive += 1
                logger.warning(
                    "Daytona keepalive refresh_activity failed for sandbox %s "
                    "(%d in a row)",
                    getattr(self._sandbox, "id", "<unknown>"),
                    consecutive,
                    exc_info=True,
                )
                if consecutive >= _KEEPALIVE_MAX_CONSECUTIVE_FAILURES:
                    logger.warning(
                        "Daytona keepalive giving up for this call after %d "
                        "failures in a row",
                        consecutive,
                    )
                    return
            else:
                consecutive = 0
                self.refreshes += 1
                if self._on_refresh is not None:
                    self._on_refresh()


def _check_dockerfile_context(config: SandboxConfig) -> None:
    """Reject a build context Daytona's declarative builder cannot use.

    ``Image.from_dockerfile`` resolves COPY sources against the Dockerfile's
    own directory and takes no separate context argument.
    """
    if not config.dockerfile or not config.context_dir:
        return
    dockerfile_dir = os.path.dirname(os.path.realpath(config.dockerfile))
    if os.path.realpath(config.context_dir) != dockerfile_dir:
        raise SandboxConfigurationError(
            "Daytona builds use the Dockerfile's directory as the build "
            "context; move the Dockerfile to the context root or omit "
            "context_dir.",
            backend="daytona",
            op="create",
        )


def _image_source(config: SandboxConfig) -> object | None:
    """Return a registry image name, a declarative Image, or ``None``."""
    if config.dockerfile:
        from daytona import Image  # type: ignore[import-untyped]

        return Image.from_dockerfile(config.dockerfile)
    return config.image


def _network_params(
    policy: NetworkPolicy | None, *, update: bool = False
) -> dict:
    """Map a policy onto Daytona's network fields.

    Daytona allow-lists take either domains or IPv4 CIDRs, not both.  A CIDR
    to a preview host opens every preview host, since they share proxy IPs;
    a domain allow-list filters on SNI and isolates correctly.
    """
    if policy is None or policy.mode == "public":
        # On update, False also clears a stored allow-list.
        return {"network_block_all": False} if update else {}
    if policy.mode == "none":
        return {"network_block_all": True}
    if policy.domains and policy.cidrs:
        raise SandboxConfigurationError(
            "Daytona cannot combine domain and CIDR allow-lists; use one",
            backend="daytona",
            op="set_network" if update else "create",
        )
    if policy.cidrs:
        for cidr in policy.cidrs:
            if ipaddress.ip_network(cidr, strict=False).version != 4:
                raise SandboxConfigurationError(
                    f"Daytona CIDR allow-lists take IPv4 only, got {cidr!r}",
                    backend="daytona",
                    op="set_network" if update else "create",
                )
        return {"network_allow_list": ",".join(policy.cidrs)}
    return {"domain_allow_list": ",".join(policy.domains)}


def _apply_lifecycle_defaults(common: dict) -> None:
    """Bound a sandbox whose caller set no lifecycle of its own.

    Daytona's documented 15-minute idle auto-stop is only the *SDK-side*
    description; the effective default is the organization's, which can be
    off.  Where it is off, a sandbox created with nothing set never stops,
    so the auto-delete and auto-archive timers (which only count while
    stopped) never start either, and a driver that dies without reaching
    destroy() (SIGKILL, OOM, a preempted VM) leaves it billing indefinitely.
    Each default below fills a field only when the caller passed nothing for
    it, through timeout_secs or backend_options; an explicit value, including
    0 ("disabled"), is kept as given.
    """
    # At most one of auto_stop_interval and auto_pause_interval may be
    # non-zero, so a caller who chose auto-pause gets no auto-stop default.
    if "auto_stop_interval" not in common and not common.get(
        "auto_pause_interval"
    ):
        common["auto_stop_interval"] = _DEFAULT_AUTO_STOP_MINUTES
    # ephemeral is itself an auto-delete setting (it forces 0), so either key
    # means the caller decided.
    if "auto_delete_interval" not in common and "ephemeral" not in common:
        common["auto_delete_interval"] = _DEFAULT_AUTO_DELETE_MINUTES
    if "ttl_minutes" not in common:
        common["ttl_minutes"] = _DEFAULT_TTL_MINUTES


def _build_params(config: SandboxConfig, *, create_token: str) -> object:
    """Build the appropriate Daytona params object for the config.

    Always returns a params object -- never ``None`` -- so the create-token
    label lands on every sandbox.  ``Daytona.create(params)`` with neither a
    snapshot nor an image behaves exactly like ``Daytona.create()``.
    """
    from daytona import (  # type: ignore[import-untyped]
        CreateSandboxFromImageParams,
        CreateSandboxFromSnapshotParams,
    )
    from daytona.common.sandbox import Resources  # type: ignore[import-untyped]

    # _LOCAL_OPTIONS are consumed by this backend (create_timeout targets
    # Daytona.create() itself), not the params object; leaving them in would
    # be silently swallowed (params ignore extra fields).
    options = {
        k: v
        for k, v in config.backend_options.items()
        if k not in _LOCAL_OPTIONS
    }

    common: dict = {}
    if config.env_vars:
        common["env_vars"] = config.env_vars
    common.update(_network_params(config.network))

    # timeout_secs is documented as the sandbox's max lifetime, and ttl_minutes
    # is the only absolute bound Daytona offers, so that is what it maps to.
    # The auto_* intervals cannot stand in.  Per
    # https://www.daytona.io/docs/en/sandboxes/#automated-lifecycle-management
    # auto_stop_interval is an *idle* timer that "triggers
    # even if there are internal processes running", but its clock is reset by
    # every Toolbox API call -- which is what every exec, file read and file
    # write in this backend is.  A sandbox a driver polls every 10s therefore
    # never trips it.  auto_archive_interval, auto_delete_interval and
    # ephemeral only start counting once a sandbox is *stopped*, so they never
    # fire on one that never stops.  Nothing but ttl_minutes bounds it.
    #
    # But ttl_minutes (same page, #wall-clock-ttl) destroys the sandbox "in any
    # state: started, stopped, paused, or archived", counts wall-clock from
    # creation (snapshot pull and boot included), and is reset by no activity
    # at all.  That is a lifetime cap, not a recommendation, so it is derived
    # only from a timeout_secs the caller actually asked for -- never from the
    # preset or dataclass default, which would silently kill running work at
    # 30 or 10 minutes.  Without one, _apply_lifecycle_defaults below sets the
    # generous _DEFAULT_TTL_MINUTES backstop instead.
    #
    # Rounded up, and floored at Daytona's 1-minute granularity, because
    # ttl_minutes=0 means "no TTL" -- the opposite of a sub-minute bound.
    if config.timeout_secs_explicit:
        common["ttl_minutes"] = max(1, math.ceil(config.timeout_secs / 60))

    if options:
        # backend_options wins, as the documented escape hatch -- except that
        # env_vars is merged rather than replaced, so a caller adding one
        # variable here cannot silently drop everything passed via env_vars=.
        option_env = options.get("env_vars")
        common.update(options)
        if option_env and config.env_vars:
            common["env_vars"] = {**config.env_vars, **option_env}

    # After backend_options, so a value the caller passed there always wins.
    _apply_lifecycle_defaults(common)

    # Stamped after backend_options so a caller's labels can add to the reap
    # handle but never replace it.
    common["labels"] = {
        **(common.get("labels") or {}),
        _CREATE_TOKEN_LABEL: create_token,
    }

    image = _image_source(config)
    if image is not None:
        # Build resources if non-default cpu or memory is specified
        resources_kwargs: dict = {}
        if config.cpu != 1.0:
            resources_kwargs["cpu"] = max(1, int(config.cpu))
        if config.memory_mb != 1024:
            # Daytona SDK expects memory in GiB
            resources_kwargs["memory"] = math.ceil(config.memory_mb / 1024)
        if config.disk_mb is not None:
            # Daytona SDK expects disk in GiB
            resources_kwargs["disk"] = math.ceil(config.disk_mb / 1024)

        resources = Resources(**resources_kwargs) if resources_kwargs else None
        return CreateSandboxFromImageParams(
            image=image, resources=resources, **common
        )

    if config.snapshot_id:
        return CreateSandboxFromSnapshotParams(
            snapshot=config.snapshot_id, **common
        )

    return CreateSandboxFromSnapshotParams(**common)


class DaytonaSession:
    """One live Daytona sandbox.

    ``workdir`` is the working directory for shell commands, matching the
    Tensorlake backend's meaning of the field.  It does not apply to
    ``execute_code``, whose Daytona endpoint takes no working directory, nor
    to file paths, which Daytona resolves against the sandbox root.
    """

    def __init__(
        self,
        *,
        client: object,
        sandbox: object,
        workdir: str | None = None,
        keepalive_secs: float | None = None,
        metric_tags: dict[str, str] | None = None,
        metric_tag_labels: dict[str, str] | None = None,
    ) -> None:
        self._client = client
        self._sandbox: object = sandbox
        self._workdir = workdir
        # What session_state() persists: the explicit option, else an interval
        # taken from the env var, so a resume in a process with a different
        # (or no) env value keeps this session's interval.  Only the built-in
        # default is left out.
        self._keepalive_option = (
            float(keepalive_secs)
            if keepalive_secs is not None
            else _env_keepalive_secs()
        )
        self._keepalive_secs = _resolve_keepalive_secs(self._keepalive_option)
        self._metric_tags_option = metric_tags
        # Like the keepalive: the explicit mapping, else the env var's, is
        # what session_state() persists, so a resume reads the same labels.
        self._metric_tag_labels = (
            dict(metric_tag_labels)
            if metric_tag_labels is not None
            else _env_metric_tag_labels()
        )
        self._tags = self._build_tags(
            sandbox, metric_tags, self._metric_tag_labels
        )
        # Idle-gap tracking.  The clock starts when this session object is
        # made (just after create, or at resume) and moves on every activity
        # mark: a toolbox call starting or ending, and a successful keepalive.
        self._activity_lock = threading.Lock()
        self._last_activity = time.monotonic()
        self._max_gap = 0.0
        self._max_gap_emitted = False

    @staticmethod
    def _build_tags(
        sandbox: object,
        metric_tags: dict[str, str] | None,
        metric_tag_labels: dict[str, str] | None,
    ) -> dict[str, str]:
        tags = {"backend": "daytona", "sandbox_class": _sandbox_class(sandbox)}
        labels = getattr(sandbox, "labels", None)
        if isinstance(labels, dict):
            for tag, label in (metric_tag_labels or {}).items():
                if labels.get(label):
                    tags[tag] = str(labels[label])
        if "caller" not in tags and os.environ.get(_CALLER_ENV):
            tags["caller"] = os.environ[_CALLER_ENV]
        tags.update(metric_tags or {})
        return tags

    def _mark_activity(self) -> None:
        with self._activity_lock:
            self._last_activity = time.monotonic()

    def _record_gap(self, *, now: float | None = None) -> float:
        """Fold the gap since the last activity mark into the max; reset."""
        now = time.monotonic() if now is None else now
        with self._activity_lock:
            gap = max(0.0, now - self._last_activity)
            self._last_activity = now
            self._max_gap = max(self._max_gap, gap)
        return gap

    def _toolbox(self, op: str, fn, *, keepalive: bool = False):
        """Run one Toolbox API call with idle-gap tracking (and keepalive)."""
        # The gap is measured before the call but emitted after it, so a slow
        # metrics sink can never delay the call (and let the sandbox idle-stop
        # before it reaches Daytona).
        gap = self._record_gap()
        try:
            if keepalive:
                with _Keepalive(
                    self._sandbox,
                    self._keepalive_secs,
                    on_refresh=self._mark_activity,
                ):
                    return fn()
            return fn()
        finally:
            self._mark_activity()
            _metrics.emit(
                IDLE_GAP_METRIC,
                gap,
                {**self._tags, "op": op},
                log_level=logging.DEBUG,
            )

    @property
    def provider_resource_id(self) -> str | None:
        """Return Daytona's durable sandbox identifier when available."""
        value = getattr(self._sandbox, "id", None)
        return str(value) if value is not None else None

    def execute_code(
        self, code: str, language: str = "python"
    ) -> SandboxResult:
        del language  # Unused; the Daytona code endpoint executes Python.
        try:
            response = self._toolbox(
                "execute_code",
                lambda: self._sandbox.process.code_run(code),
                keepalive=True,
            )
            return SandboxResult(
                stdout=getattr(response, "result", "") or "",
                stderr="",
                exit_code=getattr(response, "exit_code", 0) or 0,
            )
        except Exception as exc:
            raise _classify_daytona_error(exc, op="execute") from exc

    def execute_command(
        self, command: str, args: list[str] | None = None
    ) -> SandboxResult:
        try:
            full_cmd = (
                command
                if not args
                else f"{command} {' '.join(shlex.quote(a) for a in args)}"
            )
            response = self._toolbox(
                "execute_command",
                lambda: self._sandbox.process.exec(full_cmd, cwd=self._workdir),
                keepalive=True,
            )
            return SandboxResult(
                stdout=getattr(response, "result", "") or "",
                stderr="",
                exit_code=getattr(response, "exit_code", 0) or 0,
            )
        except Exception as exc:
            raise _classify_daytona_error(exc, op="execute") from exc

    def list_files(self, path: str = "/") -> list[FileInfo]:
        try:
            entries = self._toolbox(
                "list_files", lambda: self._sandbox.fs.list_files(path)
            )
            return [
                FileInfo(
                    path=getattr(e, "name", str(e)),
                    is_dir=getattr(e, "is_dir", False),
                    size=getattr(e, "size", None),
                )
                for e in entries
            ]
        except Exception as exc:
            raise SandboxExecutionError(
                f"Daytona list_files failed: {exc}"
            ) from exc

    def read_file(self, path: str) -> bytes:
        try:
            content = self._toolbox(
                "read_file", lambda: self._sandbox.fs.download_file(path)
            )
            return content if isinstance(content, bytes) else content.encode()
        except Exception as exc:
            raise SandboxExecutionError(
                f"Daytona read_file failed: {exc}"
            ) from exc

    def write_file(self, path: str, content: bytes | str) -> None:
        try:
            data = content if isinstance(content, bytes) else content.encode()
            self._toolbox(
                "write_file", lambda: self._sandbox.fs.upload_file(data, path)
            )
        except Exception as exc:
            raise SandboxExecutionError(
                f"Daytona write_file failed: {exc}"
            ) from exc

    def upload_file(self, local_path: str, remote_path: str) -> None:
        try:
            data = pathlib.Path(local_path).read_bytes()
            self._toolbox(
                "upload_file",
                lambda: self._sandbox.fs.upload_file(data, remote_path),
            )
        except Exception as exc:
            raise SandboxExecutionError(
                f"Daytona upload_file failed: {exc}"
            ) from exc

    def download_file(self, remote_path: str, local_path: str) -> None:
        try:
            data = self._toolbox(
                "download_file",
                lambda: self._sandbox.fs.download_file(remote_path),
            )
            content = data if isinstance(data, bytes) else data.encode()
            pathlib.Path(local_path).write_bytes(content)
        except Exception as exc:
            raise SandboxExecutionError(
                f"Daytona download_file failed: {exc}"
            ) from exc

    def expose_port(self, port: int) -> ExposedPort:
        """Return the port's preview link; the sandbox stays private.

        A private sandbox answers 401 unless the request carries the preview
        token, so the token goes in the returned headers.
        """
        try:
            link = self._sandbox.get_preview_link(port)
        except Exception as exc:
            raise _classify_daytona_error(exc, op="expose_port") from exc
        token = getattr(link, "token", None)
        headers = {"X-Daytona-Preview-Token": token} if token else {}
        return ExposedPort(port=port, url=link.url, headers=headers)

    def set_network(self, policy: NetworkPolicy) -> None:
        params = _network_params(policy, update=True)
        try:
            self._sandbox.update_network_settings(**params)
        except Exception as exc:
            raise _classify_daytona_error(exc, op="set_network") from exc

    def snapshot(self) -> SnapshotInfo:
        raise FeatureNotSupportedError(
            "Snapshots are not supported by the Daytona backend via this SDK"
        )

    def session_state(self) -> dict:
        sandbox_id = getattr(self._sandbox, "id", None)
        if sandbox_id is None:
            raise FeatureNotSupportedError(
                "Daytona sandbox object exposes no id; cannot serialize"
            )
        state = {"sandbox_id": str(sandbox_id)}
        if self._workdir:
            # Carried across resume so the working directory isn't dropped there
            # either.
            state["workdir"] = self._workdir
        if self._keepalive_option is not None:
            state[_KEEPALIVE_OPTION] = self._keepalive_option
        if self._metric_tags_option:
            state[_METRIC_TAGS_OPTION] = dict(self._metric_tags_option)
        # `is not None`, so an explicit empty mapping is carried too and a
        # resume does not fall back to the environment's mapping.
        if self._metric_tag_labels is not None:
            state[_METRIC_TAG_LABELS_OPTION] = dict(self._metric_tag_labels)
        return state

    def destroy(self) -> None:
        emit_max = self._sandbox is not None and not self._max_gap_emitted
        if emit_max:
            # The time from the last activity to destroy() is idle time the
            # sandbox really spent, so it counts toward the max.  Emitted once,
            # even if a failed destroy() is retried.
            self._max_gap_emitted = True
            self._record_gap()
        try:
            if self._sandbox and self._client:
                self._client.delete(self._sandbox)
        except Exception as exc:
            raise _classify_daytona_error(exc, op="destroy") from exc
        finally:
            # After the delete, so a slow metrics sink cannot hold up cleanup.
            if emit_max:
                _metrics.emit(MAX_IDLE_GAP_METRIC, self._max_gap, self._tags)
        self._sandbox = None
