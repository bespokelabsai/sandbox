"""Crucible sandbox backend.

Crucible is Bespoke's own sandbox control plane. It fronts Tensorlake microVMs
behind `/v1/sandboxes`, adding per-organization tenancy, image policy, quota
accounting and admission control that the vendor API has no concept of.

WHY GO THROUGH IT RATHER THAN STRAIGHT TO TENSORLAKE. The `tensorlake` backend
in this package talks to the vendor directly, which means the caller holds a
Tensorlake API key. That key is PROJECT-scoped and per-tenant projects are not
available, so anyone holding it can see and terminate every sandbox in the
shared project -- including other workloads'. A crucible organization key is
scoped to one organization's sandboxes and images and is revocable on its own.
For code that runs on developer or contractor machines that difference is the
whole point.

NO SDK, DELIBERATELY. Crucible speaks plain JSON over HTTPS and there is no
client library to import, so this backend uses the standard library. That is
why it is the one backend whose constructor cannot raise
BackendNotInstalledError: there is no optional extra to forget to install.

BULK TRANSFERS DO NOT GO THROUGH HERE. `read_file`/`write_file` move content as
base64 inside a JSON body, and Cloud Run rejects a request body over 32 MiB at
the frontend -- about 24 MiB of actual content after base64 expansion, and not a
limit crucible can raise in code. Anything larger must use crucible's
signed-URL route (`POST /v1/sandboxes/{id}/files/signed-url`, HRZN-946), which
hands back a URL straight to object storage. The methods here are correct for
the small control files a driver polls; they are the wrong tool for an artifact
archive, and `_MAX_INLINE_BYTES` refuses rather than letting the frontend
return an opaque 413.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import pathlib
import shlex
import urllib.error
import urllib.parse
import urllib.request

from bespokelabs.sandbox.exceptions import (
    FeatureNotSupportedError,
    SandboxConfigurationError,
    SandboxConnectionError,
    SandboxCreationError,
    SandboxExecutionError,
    SandboxNotFoundError,
)
from bespokelabs.sandbox.types import (
    FileInfo,
    SandboxConfig,
    SandboxResult,
    SnapshotInfo,
)

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://crucible.bespokelabs.ai"

#: Refuse an inline transfer this large rather than letting Cloud Run answer a
#: bare 413 from the Google frontend, which arrives as HTML with no JSON body
#: and no hint about what to do instead. 20 MiB leaves room under the ~24 MiB
#: effective ceiling for the base64 expansion plus the surrounding JSON.
_MAX_INLINE_BYTES = 20 * 1024 * 1024

#: Crucible derives a sandbox's display name from this, so it is also the only
#: handle on a sandbox whose create call failed after the server built it.
_WORKLOAD_ID_OPTION = "workload_id"

_TIMEOUT_OPTION = "request_timeout"
_DEFAULT_REQUEST_TIMEOUT = 60.0


class CrucibleClient:
    """Factory for crucible-hosted sandboxes.

    Reads CRUCIBLE_API_KEY (and optionally CRUCIBLE_BASE_URL) on first create,
    matching the other remote backends: constructing the client must not require
    credentials, so that listing available backends never fails.
    """

    def __init__(self) -> None:
        # No SDK import, so nothing to fail. Kept explicit because every other
        # backend's constructor is where BackendNotInstalledError comes from,
        # and a reader will look here for it.
        self._base_url: str | None = None
        self._api_key: str | None = None

    def _ensure_credentials(self) -> tuple[str, str]:
        if self._api_key is None:
            api_key = (os.environ.get("CRUCIBLE_API_KEY") or "").strip()
            if not api_key:
                raise SandboxCreationError(
                    "CRUCIBLE_API_KEY environment variable is not set",
                    backend="crucible",
                    op="create",
                )
            self._api_key = api_key
            self._base_url = (
                os.environ.get("CRUCIBLE_BASE_URL") or _DEFAULT_BASE_URL
            ).rstrip("/")
        return self._api_key, self._base_url  # type: ignore[return-value]

    def create(self, config: SandboxConfig) -> CrucibleSession:
        api_key, base_url = self._ensure_credentials()

        if config.gpu:
            # Crucible answers 501 for this; saying so here costs no round trip
            # and names the reason, which the 501 body does not.
            raise FeatureNotSupportedError(
                "crucible has no GPU sandboxes: the microVM executor has no "
                "accelerator. Use a GPU-capable backend such as modal.",
                backend="crucible",
                op="create",
            )
        if not config.image:
            # Crucible requires a REGISTERED image name -- it cannot boot an
            # arbitrary registry reference, and the vendor 400 for that says
            # "Image 'x' is not registered in the server", which reads like a
            # crucible bug rather than a missing argument.
            raise SandboxConfigurationError(
                "crucible requires `image` to name an image already registered "
                "with the organization; it cannot pull an arbitrary registry "
                "reference. Register it via POST /v1/images first.",
                backend="crucible",
                op="create",
            )

        options = dict(config.backend_options)
        timeout = _request_timeout(options.pop(_TIMEOUT_OPTION, None))
        workload_id = options.pop(_WORKLOAD_ID_OPTION, None)

        body: dict = {
            "image": config.image,
            "cpus": float(config.cpu),
            "memoryMb": int(config.memory_mb),
            # Crucible honours resources at CREATE, unlike a Daytona snapshot
            # whose sizes are baked in -- so one registered image serves every
            # size and disk is just another field here.
            "egress": {"allowInternet": bool(config.allow_internet)},
        }
        if config.disk_mb is not None:
            body["diskMb"] = int(config.disk_mb)
        if config.env_vars:
            body["env"] = dict(config.env_vars)
        if config.timeout_secs_explicit:
            # Only when the caller actually asked. Crucible clamps rather than
            # rejecting, but sending the dataclass default would still shorten a
            # long-running sandbox to 10 minutes without anyone choosing that.
            body["timeoutSecs"] = int(config.timeout_secs)
        if workload_id:
            body["workloadId"] = str(workload_id)
        # backend_options last, as the documented escape hatch -- but env is
        # merged rather than replaced, so adding one variable here cannot
        # silently drop everything passed via env_vars=.
        if options:
            option_env = options.get("env")
            body.update(options)
            if option_env and config.env_vars:
                body["env"] = {**config.env_vars, **option_env}

        created = _request(
            base_url,
            api_key,
            "POST",
            "/v1/sandboxes",
            body=body,
            timeout=timeout,
            op="create",
        )
        sandbox_id = created.get("sandboxId") or created.get("sandbox_id")
        if not sandbox_id:
            raise SandboxCreationError(
                f"crucible create returned no sandboxId: {created!r}",
                backend="crucible",
                op="create",
            )

        session = CrucibleSession(
            base_url=base_url,
            api_key=api_key,
            sandbox_id=str(sandbox_id),
            workdir=config.workdir,
            timeout=timeout,
        )
        if config.workdir:
            # exec does not create the working directory, so make it once. A
            # failure here leaves a live sandbox nothing else will clean up, so
            # it is destroyed before the error propagates -- the same reasoning
            # as the Daytona backend's orphan reap, minus the guesswork, because
            # here the id is already in hand.
            try:
                session.execute_command(
                    "mkdir", ["-p", config.workdir]
                )
            except BaseException:
                session.destroy()
                raise
        return session

    def resume(self, data: dict) -> CrucibleSession:
        """Reattach by id from a different process.

        Cheap and reliable here: crucible is stateless HTTP, so there is no
        connection to re-establish -- the id is the whole handle. The existence
        check is deliberate, so that resuming a sandbox that is gone raises
        SandboxNotFoundError now rather than surfacing as a confusing failure on
        the first read.
        """
        api_key, base_url = self._ensure_credentials()
        sandbox_id = data.get("sandbox_id")
        if not sandbox_id:
            raise SandboxCreationError(
                "cannot resume a crucible sandbox without a sandbox_id",
                backend="crucible",
                op="resume",
            )
        timeout = _request_timeout(data.get("request_timeout"))
        _request(
            base_url,
            api_key,
            "GET",
            f"/v1/sandboxes/{urllib.parse.quote(str(sandbox_id))}",
            timeout=timeout,
            op="resume",
        )
        return CrucibleSession(
            base_url=base_url,
            api_key=api_key,
            sandbox_id=str(sandbox_id),
            workdir=data.get("workdir"),
            timeout=timeout,
        )


class CrucibleSession:
    """One live crucible-hosted sandbox."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        sandbox_id: str,
        workdir: str | None = None,
        timeout: float = _DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._sandbox_id = sandbox_id
        self._workdir = workdir
        self._timeout = timeout

    # -- helpers ---------------------------------------------------------

    def _path(self, suffix: str = "") -> str:
        return f"/v1/sandboxes/{urllib.parse.quote(self._sandbox_id)}{suffix}"

    def _call(self, method: str, suffix: str = "", **kw) -> dict:
        return _request(
            self._base_url, self._api_key, method, self._path(suffix), **kw
        )

    # -- execution -------------------------------------------------------

    def execute_code(
        self, code: str, language: str = "python"
    ) -> SandboxResult:
        interpreter = {"python": "python3", "bash": "bash", "sh": "sh"}.get(
            language
        )
        if interpreter is None:
            raise FeatureNotSupportedError(
                f"crucible cannot execute {language!r}; it has no code endpoint, "
                "so only languages with a known interpreter are supported "
                "(python, bash, sh)",
                backend="crucible",
                op="execute_code",
            )
        # No dedicated code endpoint, so this is exec with the source on stdin
        # rather than in argv -- argv would break on any code containing a quote.
        return self.execute_command(
            "sh",
            [
                "-c",
                f"printf %s {shlex.quote(code)} | {interpreter} -",
            ],
        )

    def execute_command(
        self, command: str, args: list[str] | None = None
    ) -> SandboxResult:
        body: dict = {"command": command, "args": list(args or [])}
        if self._workdir:
            body["workingDir"] = self._workdir
        result = self._call("POST", "/exec", body=body, op="exec")
        return SandboxResult(
            stdout=result.get("stdout") or "",
            stderr=result.get("stderr") or "",
            exit_code=int(result.get("exitCode") or 0),
        )

    # -- files -----------------------------------------------------------

    def list_files(self, path: str = "/") -> list[FileInfo]:
        result = self._call(
            "GET",
            "/files/list",
            query={"path": path},
            op="list_files",
        )
        return [
            FileInfo(
                path=e.get("path", ""),
                is_dir=bool(e.get("isDir")),
                size=e.get("size"),
            )
            for e in (result.get("entries") or [])
        ]

    def read_file(self, path: str) -> bytes:
        result = self._call(
            "GET", "/files", query={"path": path}, op="read_file"
        )
        encoded = result.get("contentBase64") or ""
        return base64.b64decode(encoded)

    def write_file(self, path: str, content: bytes | str) -> None:
        data = content if isinstance(content, bytes) else content.encode()
        _refuse_oversize(data, path, op="write_file")
        self._call(
            "PUT",
            "/files",
            body={
                "path": path,
                "contentBase64": base64.b64encode(data).decode(),
            },
            op="write_file",
        )

    def upload_file(self, local_path: str, remote_path: str) -> None:
        data = pathlib.Path(local_path).read_bytes()
        _refuse_oversize(data, local_path, op="upload_file")
        self.write_file(remote_path, data)

    def download_file(self, remote_path: str, local_path: str) -> None:
        pathlib.Path(local_path).write_bytes(self.read_file(remote_path))

    # -- lifecycle -------------------------------------------------------

    def snapshot(self) -> SnapshotInfo:
        result = self._call("POST", "/snapshot", body={}, op="snapshot")
        snapshot_id = result.get("snapshotId") or result.get("snapshot_id")
        return SnapshotInfo(
            snapshot_id=str(snapshot_id) if snapshot_id else "",
            backend="crucible",
        )

    def session_state(self) -> dict:
        state = {"sandbox_id": self._sandbox_id}
        if self._workdir:
            state["workdir"] = self._workdir
        return state

    def destroy(self) -> None:
        try:
            self._call("DELETE", op="destroy")
        except Exception:
            # Matches every other backend: destroy is best effort and must never
            # mask the error that led the caller to tear down.
            logger.warning(
                "failed to terminate crucible sandbox %s",
                self._sandbox_id,
                exc_info=True,
            )


# -- transport -----------------------------------------------------------


def _request_timeout(raw) -> float:
    if raw is None:
        return _DEFAULT_REQUEST_TIMEOUT
    try:
        timeout = float(raw)
    except (TypeError, ValueError) as exc:
        raise SandboxConfigurationError(
            f"backend_options['{_TIMEOUT_OPTION}'] must be a number of seconds, "
            f"got {raw!r}",
            backend="crucible",
            op="create",
        ) from exc
    if timeout <= 0:
        raise SandboxConfigurationError(
            f"backend_options['{_TIMEOUT_OPTION}'] must be positive",
            backend="crucible",
            op="create",
        )
    return timeout


def _refuse_oversize(data: bytes, what: str, *, op: str) -> None:
    if len(data) <= _MAX_INLINE_BYTES:
        return
    raise SandboxConfigurationError(
        f"{what!r} is {len(data)} bytes, over the {_MAX_INLINE_BYTES}-byte limit "
        "for inline transfer. Crucible moves file content as base64 in a JSON "
        "body and the platform rejects a request over 32 MiB before it reaches "
        "the service, so a larger payload cannot succeed here. Use crucible's "
        "signed-URL route (POST /v1/sandboxes/{id}/files/signed-url) to move it "
        "through object storage instead.",
        backend="crucible",
        op=op,
    )


def _request(
    base_url: str,
    api_key: str,
    method: str,
    path: str,
    *,
    body: dict | None = None,
    query: dict | None = None,
    timeout: float = _DEFAULT_REQUEST_TIMEOUT,
    op: str = "request",
) -> dict:
    """One crucible call, with its errors mapped to this package's exceptions.

    THE 404 MAPPING IS LOAD-BEARING and not merely tidy. Callers distinguish
    "this sandbox is gone" from "the control plane hiccuped" by walking the
    exception chain for a 404 or a NotFound-named class -- PDK's
    `_provider_reports_not_found` is one such caller. Collapsing a 404 into a
    generic execution error makes a lost sandbox indistinguishable from a
    transient read, and a driver then polls a sandbox that no longer exists
    until its own deadline expires.

    503 is likewise deliberate. Crucible answers 503 when the SHARED Tensorlake
    pool is full -- not the caller's quota, and retrying will not clear it. It is
    raised as a retryable connection error carrying the status so a caller with a
    second provider can fall back instead of waiting.
    """
    url = f"{base_url}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"

    data = None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise _map_http_error(exc, method=method, path=path, op=op) from exc
    except urllib.error.URLError as exc:
        raise SandboxConnectionError(
            f"crucible {method} {path} could not be reached: {exc.reason}",
            backend="crucible",
            op=op,
        ) from exc

    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise SandboxExecutionError(
            f"crucible {method} {path} returned a non-JSON body",
            backend="crucible",
            op=op,
        ) from exc
    return parsed if isinstance(parsed, dict) else {"result": parsed}


def _map_http_error(
    exc: urllib.error.HTTPError, *, method: str, path: str, op: str
) -> Exception:
    status = exc.code
    detail = _detail_of(exc)
    where = f"crucible {method} {path} -> {status}"

    if status == 404:
        # Crucible answers 404 for BOTH "no such sandbox" and "not yours", on
        # purpose: a 403 would turn the by-id surface into an enumeration
        # oracle. So this cannot distinguish them, and must not pretend to.
        return SandboxNotFoundError(
            f"{where}: {detail}",
            backend="crucible",
            op=op,
            context={"status_code": status},
        )
    if status == 503:
        return SandboxConnectionError(
            f"{where}: {detail}",
            backend="crucible",
            op=op,
            context={"status_code": status},
        )
    if status in (401, 403):
        return SandboxCreationError(
            f"{where}: {detail}",
            backend="crucible",
            op=op,
            context={"status_code": status},
        )
    if status == 429:
        return SandboxConnectionError(
            f"{where}: {detail}",
            backend="crucible",
            op=op,
            retryable=True,
            context={"status_code": status},
        )
    return SandboxExecutionError(
        f"{where}: {detail}",
        backend="crucible",
        op=op,
        context={"status_code": status},
    )


def _detail_of(exc: urllib.error.HTTPError) -> str:
    """The `detail` crucible puts in an error body, or the raw text.

    Worth the effort: the platform's own rejections (a 413 from the Google
    frontend, say) are bare HTML with no JSON at all, and a caller staring at
    `<html><head>` deserves at least the status line rather than a parse error.
    """
    try:
        payload = json.loads(exc.read() or b"")
    except Exception:
        return exc.reason or "no detail"
    if isinstance(payload, dict):
        return str(
            payload.get("detail") or payload.get("error") or payload
        )
    return str(payload)
