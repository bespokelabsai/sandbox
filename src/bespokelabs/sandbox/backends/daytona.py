from __future__ import annotations

import logging
import math
import os
import pathlib
import shlex
import threading
import uuid

from bespokelabs.sandbox.exceptions import (
    BackendNotInstalledError,
    FeatureNotSupportedError,
    SandboxConfigurationError,
    SandboxCreationError,
    SandboxExecutionError,
)
from bespokelabs.sandbox.types import FileInfo, SandboxConfig, SandboxResult, SnapshotInfo

logger = logging.getLogger(__name__)

# Label stamped on every sandbox this backend creates, carrying a token unique
# to the create call.  Daytona filters on labels server-side, so this token is
# the only handle we have on a sandbox whose create call failed *after* the
# server had already built it.
_CREATE_TOKEN_LABEL = "bespokelabs.sandbox/create-token"

# backend_options key that overrides the deadline passed to Daytona.create().
_CREATE_TIMEOUT_OPTION = "create_timeout"


class DaytonaClient:
    """Factory for Daytona sandboxes.

    The authenticated SDK client is built on first create() (reading
    DAYTONA_* env vars at that point) and reused for every session
    created through this client.
    """

    def __init__(self) -> None:
        try:
            from daytona import Daytona, DaytonaConfig  # type: ignore[import-untyped]
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
                        raise SandboxCreationError("DAYTONA_API_KEY environment variable is not set")
                    self._client = self._daytona_cls(self._daytona_config_cls(
                        api_key=api_key,
                        api_url=os.environ.get("DAYTONA_API_URL", "https://app.daytona.io/api"),
                        target=os.environ.get("DAYTONA_TARGET", "us"),
                    ))
        return self._client

    def resume(self, data: dict) -> DaytonaSession:
        client = self._ensure_client()
        getter = getattr(client, "get", None) or getattr(client, "find_one", None)
        if getter is None:
            raise FeatureNotSupportedError(
                "This Daytona SDK version has no get()/find_one(); cannot resume by id"
            )
        try:
            sandbox = getter(data["sandbox_id"])
        except Exception as exc:
            raise SandboxCreationError(
                f"Cannot resume Daytona sandbox '{data.get('sandbox_id')}': {exc}"
            ) from exc
        return DaytonaSession(client=client, sandbox=sandbox, workdir=data.get("workdir"))

    def create(self, config: SandboxConfig) -> DaytonaSession:
        self._ensure_client()

        # Unique to this create call.  If the call fails after Daytona has
        # already built the sandbox, this token is how we find the orphan.
        create_token = uuid.uuid4().hex
        create_kwargs = _create_kwargs(config)

        try:
            params = _build_params(config, create_token=create_token)
            sandbox = self._client.create(params, **create_kwargs)
            if config.workdir:
                # process.exec(cwd=...) does not create the directory, so make
                # it once here.  Inside the try, so a failure is still reaped.
                sandbox.process.exec(f"mkdir -p {shlex.quote(config.workdir)}")
        except Exception as exc:
            self._reap_orphan(create_token)
            raise SandboxCreationError(f"Failed to create Daytona sandbox: {exc}") from exc
        except BaseException:
            # KeyboardInterrupt / SystemExit: still reap, then propagate as-is.
            self._reap_orphan(create_token)
            raise

        return DaytonaSession(client=self._client, sandbox=sandbox, workdir=config.workdir)

    def _reap_orphan(self, create_token: str) -> None:
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
            from daytona import ListSandboxesQuery  # type: ignore[import-untyped]

            query = ListSandboxesQuery(labels={_CREATE_TOKEN_LABEL: create_token})
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
            return

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
            else:
                logger.warning(
                    "Deleted Daytona sandbox %s orphaned by a failed create.", orphan_id
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


def _build_params(config: SandboxConfig, *, create_token: str) -> object:
    """Build the appropriate Daytona params object for the config.

    Always returns a params object -- never ``None`` -- so the create-token
    label lands on every sandbox.  ``Daytona.create(params)`` with neither a
    snapshot nor an image behaves exactly like ``Daytona.create()``.
    """
    from daytona import CreateSandboxFromImageParams, CreateSandboxFromSnapshotParams  # type: ignore[import-untyped]
    from daytona.common.sandbox import Resources  # type: ignore[import-untyped]

    # _CREATE_TIMEOUT_OPTION targets Daytona.create(), not the params object;
    # leaving it in would be silently swallowed (params ignore extra fields).
    options = {k: v for k, v in config.backend_options.items() if k != _CREATE_TIMEOUT_OPTION}

    common: dict = {}
    if config.env_vars:
        common["env_vars"] = config.env_vars

    # timeout_secs is documented as the sandbox's max lifetime, and ttl_minutes
    # is the only absolute bound Daytona offers, so that is what it maps to.
    # The auto_* intervals cannot stand in.  Per
    # https://www.daytona.io/docs/en/sandboxes/#automated-lifecycle-management
    # auto_stop_interval is an *idle* timer (default 15 minutes) that "triggers
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
    # at all.  That is a lifetime cap, not a recommendation, so it is set only
    # from a timeout_secs the caller actually asked for -- never from the
    # preset or dataclass default, which would silently kill running work at
    # 30 or 10 minutes.
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

    # Stamped after backend_options so a caller's labels can add to the reap
    # handle but never replace it.
    common["labels"] = {**(common.get("labels") or {}), _CREATE_TOKEN_LABEL: create_token}

    if config.image:
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
        return CreateSandboxFromImageParams(image=config.image, resources=resources, **common)

    if config.snapshot_id:
        return CreateSandboxFromSnapshotParams(snapshot=config.snapshot_id, **common)

    return CreateSandboxFromSnapshotParams(**common)


class DaytonaSession:
    """One live Daytona sandbox.

    ``workdir`` is the working directory for shell commands, matching the
    Tensorlake backend's meaning of the field.  It does not apply to
    ``execute_code``, whose Daytona endpoint takes no working directory, nor
    to file paths, which Daytona resolves against the sandbox root.
    """

    def __init__(self, *, client: object, sandbox: object, workdir: str | None = None) -> None:
        self._client = client
        self._sandbox: object = sandbox
        self._workdir = workdir

    def execute_code(self, code: str, language: str = "python") -> SandboxResult:
        try:
            response = self._sandbox.process.code_run(code)
            return SandboxResult(
                stdout=getattr(response, "result", "") or "",
                stderr="",
                exit_code=getattr(response, "exit_code", 0) or 0,
            )
        except Exception as exc:
            raise SandboxExecutionError(f"Daytona code execution failed: {exc}") from exc

    def execute_command(self, command: str, args: list[str] | None = None) -> SandboxResult:
        try:
            full_cmd = command if not args else f"{command} {' '.join(shlex.quote(a) for a in args)}"
            response = self._sandbox.process.exec(full_cmd, cwd=self._workdir)
            return SandboxResult(
                stdout=getattr(response, "result", "") or "",
                stderr="",
                exit_code=getattr(response, "exit_code", 0) or 0,
            )
        except Exception as exc:
            raise SandboxExecutionError(f"Daytona command execution failed: {exc}") from exc

    def list_files(self, path: str = "/") -> list[FileInfo]:
        try:
            entries = self._sandbox.fs.list_files(path)
            return [
                FileInfo(
                    path=getattr(e, "name", str(e)),
                    is_dir=getattr(e, "is_dir", False),
                    size=getattr(e, "size", None),
                )
                for e in entries
            ]
        except Exception as exc:
            raise SandboxExecutionError(f"Daytona list_files failed: {exc}") from exc

    def read_file(self, path: str) -> bytes:
        try:
            content = self._sandbox.fs.download_file(path)
            return content if isinstance(content, bytes) else content.encode()
        except Exception as exc:
            raise SandboxExecutionError(f"Daytona read_file failed: {exc}") from exc

    def write_file(self, path: str, content: bytes | str) -> None:
        try:
            data = content if isinstance(content, bytes) else content.encode()
            self._sandbox.fs.upload_file(data, path)
        except Exception as exc:
            raise SandboxExecutionError(f"Daytona write_file failed: {exc}") from exc

    def upload_file(self, local_path: str, remote_path: str) -> None:
        try:
            data = pathlib.Path(local_path).read_bytes()
            self._sandbox.fs.upload_file(data, remote_path)
        except Exception as exc:
            raise SandboxExecutionError(f"Daytona upload_file failed: {exc}") from exc

    def download_file(self, remote_path: str, local_path: str) -> None:
        try:
            data = self._sandbox.fs.download_file(remote_path)
            content = data if isinstance(data, bytes) else data.encode()
            pathlib.Path(local_path).write_bytes(content)
        except Exception as exc:
            raise SandboxExecutionError(f"Daytona download_file failed: {exc}") from exc

    def snapshot(self) -> SnapshotInfo:
        raise FeatureNotSupportedError(
            "Snapshots are not supported by the Daytona backend via this SDK"
        )

    def session_state(self) -> dict:
        sandbox_id = getattr(self._sandbox, "id", None)
        if sandbox_id is None:
            raise FeatureNotSupportedError("Daytona sandbox object exposes no id; cannot serialize")
        state = {"sandbox_id": str(sandbox_id)}
        if self._workdir:
            # Carried across resume so the working directory isn't dropped there
            # either.
            state["workdir"] = self._workdir
        return state

    def destroy(self) -> None:
        try:
            if self._sandbox and self._client:
                self._client.delete(self._sandbox)
        except Exception:
            pass
        self._sandbox = None
