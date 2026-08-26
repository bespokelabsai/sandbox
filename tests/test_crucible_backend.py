"""Crucible backend tests.

The failures that matter here are not "a call returned the wrong dict". They are
(1) a 404 that stops looking like a 404, which makes a lost sandbox
indistinguishable from a slow one, and (2) an oversized inline transfer, which
the platform rejects with bare HTML before crucible ever sees it.
"""

from __future__ import annotations

import base64
import json
import shlex
import urllib.error
import urllib.request

import pytest

from bespokelabs.sandbox.backends import BACKENDS
from bespokelabs.sandbox.backends import crucible as cru
from bespokelabs.sandbox.exceptions import (
    FeatureNotSupportedError,
    SandboxConfigurationError,
    SandboxConnectionError,
    SandboxCreationError,
    SandboxExecutionError,
    SandboxNotFoundError,
)
from bespokelabs.sandbox.types import SandboxConfig


@pytest.fixture(autouse=True)
def _creds(monkeypatch):
    monkeypatch.setenv("CRUCIBLE_API_KEY", "crc_live_test")
    monkeypatch.setenv("CRUCIBLE_BASE_URL", "https://crucible.example")


class _Resp:
    def __init__(self, payload, status=200):
        self._body = (
            payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        )
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _capture(monkeypatch, payloads):
    """Record every request and answer from `payloads` in order."""
    calls = []

    def _urlopen(request, timeout=None):
        body = None
        if request.data:
            body = json.loads(request.data)
        calls.append(
            {
                "method": request.method,
                "url": request.full_url,
                "headers": dict(request.headers),
                "body": body,
                "timeout": timeout,
            }
        )
        nxt = payloads.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return _Resp(nxt)

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    return calls


def _http_error(status, payload=None):
    import io

    body = json.dumps(payload or {"detail": "boom"}).encode()
    return urllib.error.HTTPError(
        "https://crucible.example/x", status, "err", {}, io.BytesIO(body)
    )


# -- registration ----------------------------------------------------------


def test_it_is_registered_and_needs_no_sdk():
    """The one backend whose constructor cannot raise BackendNotInstalledError:
    crucible speaks plain JSON and there is no optional extra to forget."""
    assert BACKENDS["crucible"] is cru.CrucibleClient
    BACKENDS["crucible"]()          # no credentials, no SDK, must not raise


def test_missing_credentials_fail_at_create_not_construction(monkeypatch):
    """Constructing must stay cheap so listing backends never needs a key."""
    monkeypatch.delenv("CRUCIBLE_API_KEY", raising=False)
    client = cru.CrucibleClient()
    with pytest.raises(SandboxCreationError, match="CRUCIBLE_API_KEY"):
        client.create(SandboxConfig(backend="crucible", image="img"))


# -- THE classifier requirement -------------------------------------------


def test_a_404_stays_recognisable_as_not_found(monkeypatch):
    """Load-bearing, and the reason is outside this package.

    Callers distinguish "this sandbox is gone" from "the control plane hiccuped"
    by walking the exception chain for a 404 status or a NotFound-named class --
    PDK's `_provider_reports_not_found` does exactly that. Collapse a 404 into a
    generic execution error and a lost sandbox becomes indistinguishable from a
    slow one, so a driver polls it until its own deadline (24h by default).
    """
    _capture(monkeypatch, [_http_error(404, {"detail": "not found"})])
    session = cru.CrucibleSession(
        base_url="https://crucible.example", api_key="k", sandbox_id="sbx-1"
    )
    with pytest.raises(SandboxNotFoundError) as excinfo:
        session.list_files("/tmp")

    err = excinfo.value
    # Both discriminators a caller might use.
    assert "NotFound" in type(err).__name__
    assert err.context.get("status_code") == 404
    # And the chain is intact, since callers walk __cause__/__context__.
    assert isinstance(err.__cause__, urllib.error.HTTPError)


def test_the_pdk_classifier_actually_accepts_it(monkeypatch):
    """Runs PDK's own predicate verbatim, rather than asserting a shape I think
    it wants. If this fails, PDK's loss detection silently never fires."""

    def _provider_reports_not_found(exc: BaseException) -> bool:
        seen: set[int] = set()
        current: BaseException | None = exc
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if (
                getattr(current, "status_code", None) == 404
                or getattr(current, "status", None) == 404
                or "NotFound" in type(current).__name__
            ):
                return True
            current = current.__cause__ or current.__context__
        return False

    _capture(monkeypatch, [_http_error(404)])
    session = cru.CrucibleSession(
        base_url="https://crucible.example", api_key="k", sandbox_id="sbx-1"
    )
    try:
        session.list_files("/tmp")
    except BaseException as exc:
        assert _provider_reports_not_found(exc) is True
    else:
        pytest.fail("expected a not-found error")


@pytest.mark.parametrize(
    "status,expected",
    [
        (503, SandboxConnectionError),
        (429, SandboxConnectionError),
        (401, SandboxCreationError),
        (403, SandboxCreationError),
        (500, SandboxExecutionError),
        (502, SandboxExecutionError),
    ],
)
def test_other_statuses_map_to_distinguishable_types(monkeypatch, status, expected):
    _capture(monkeypatch, [_http_error(status)])
    session = cru.CrucibleSession(
        base_url="https://crucible.example", api_key="k", sandbox_id="s"
    )
    with pytest.raises(expected) as excinfo:
        session.list_files("/")
    assert excinfo.value.context.get("status_code") == status
    # A pool exhaustion must NOT look like a missing sandbox.
    if status != 404:
        assert not isinstance(excinfo.value, SandboxNotFoundError)


def test_a_503_is_a_fallback_signal_not_a_missing_sandbox(monkeypatch):
    """Crucible answers 503 when the SHARED Tensorlake pool is full -- not the
    caller's quota, and retrying will not clear it. A caller with a second
    provider needs to tell that apart from every other failure."""
    _capture(monkeypatch, [_http_error(503, {"detail": "pool at capacity"})])
    session = cru.CrucibleSession(
        base_url="https://crucible.example", api_key="k", sandbox_id="s"
    )
    with pytest.raises(SandboxConnectionError) as excinfo:
        session.execute_command("true")
    assert excinfo.value.context.get("status_code") == 503
    assert "capacity" in str(excinfo.value)


def test_a_non_json_error_body_still_yields_a_message(monkeypatch):
    """The platform's own rejections are bare HTML with no JSON -- a 413 from the
    Google frontend, for one. A parse error there would hide the status."""
    import io

    err = urllib.error.HTTPError(
        "https://crucible.example/x",
        413,
        "Request Entity Too Large",
        {},
        io.BytesIO(b"<html><head><title>413</title></head>"),
    )
    _capture(monkeypatch, [err])
    session = cru.CrucibleSession(
        base_url="https://crucible.example", api_key="k", sandbox_id="s"
    )
    with pytest.raises(SandboxExecutionError) as excinfo:
        session.list_files("/")
    assert "413" in str(excinfo.value)


# -- create ----------------------------------------------------------------


def test_create_sends_resources_and_returns_a_session(monkeypatch):
    calls = _capture(monkeypatch, [{"sandboxId": "sbx-9"}])
    client = cru.CrucibleClient()
    session = client.create(
        SandboxConfig(
            backend="crucible",
            image="pdk-worker-v1.2.3",
            cpu=4,
            memory_mb=8192,
            disk_mb=51200,
            env_vars={"A": "1"},
        )
    )
    body = calls[0]["body"]
    assert calls[0]["method"] == "POST"
    assert body["image"] == "pdk-worker-v1.2.3"
    assert body["cpus"] == 4.0 and body["memoryMb"] == 8192
    # disk at CREATE is the point: one registered image serves every size,
    # unlike a Daytona snapshot with its sizes baked in.
    assert body["diskMb"] == 51200
    assert body["env"] == {"A": "1"}
    assert calls[0]["headers"]["Authorization"] == "Bearer crc_live_test"
    assert session.session_state()["sandbox_id"] == "sbx-9"


def test_an_unrequested_timeout_is_not_sent(monkeypatch):
    """Sending the dataclass default would silently cap a long sandbox at 10
    minutes without anyone choosing that -- the same trap the Daytona backend
    documents for ttl_minutes."""
    calls = _capture(monkeypatch, [{"sandboxId": "s"}])
    cru.CrucibleClient().create(
        SandboxConfig(backend="crucible", image="img")
    )
    assert "timeoutSecs" not in calls[0]["body"]


def test_an_explicit_timeout_is_sent(monkeypatch):
    calls = _capture(monkeypatch, [{"sandboxId": "s"}])
    cru.CrucibleClient().create(
        SandboxConfig(
            backend="crucible",
            image="img",
            timeout_secs=86400,
            timeout_secs_explicit=True,
        )
    )
    assert calls[0]["body"]["timeoutSecs"] == 86400


def test_a_gpu_request_is_refused_without_a_round_trip(monkeypatch):
    calls = _capture(monkeypatch, [])
    with pytest.raises(FeatureNotSupportedError, match="no GPU"):
        cru.CrucibleClient().create(
            SandboxConfig(backend="crucible", image="img", gpu="H100")
        )
    assert calls == [], "must not contact crucible to learn this"


def test_a_missing_image_is_refused_with_the_actionable_reason(monkeypatch):
    """Crucible needs a REGISTERED name. Its vendor-level 400 for this says
    "Image 'x' is not registered in the server", which reads like a crucible bug
    rather than a missing argument."""
    calls = _capture(monkeypatch, [])
    with pytest.raises(SandboxConfigurationError, match="already registered"):
        cru.CrucibleClient().create(SandboxConfig(backend="crucible"))
    assert calls == []


def test_backend_options_override_but_env_merges(monkeypatch):
    calls = _capture(monkeypatch, [{"sandboxId": "s"}])
    cru.CrucibleClient().create(
        SandboxConfig(
            backend="crucible",
            image="img",
            env_vars={"KEEP": "1"},
            backend_options={"env": {"EXTRA": "2"}, "labels": {"x": "y"}},
        )
    )
    body = calls[0]["body"]
    # Adding one variable via backend_options must not drop the others.
    assert body["env"] == {"KEEP": "1", "EXTRA": "2"}
    assert body["labels"] == {"x": "y"}


def test_workload_id_is_sent_as_the_name_handle(monkeypatch):
    calls = _capture(monkeypatch, [{"sandboxId": "s"}])
    cru.CrucibleClient().create(
        SandboxConfig(
            backend="crucible",
            image="img",
            backend_options={"workload_id": "pdk-run-7"},
        )
    )
    assert calls[0]["body"]["workloadId"] == "pdk-run-7"
    # And it must not leak through as an unknown top-level field too.
    assert "workload_id" not in calls[0]["body"]


def test_a_failed_workdir_setup_destroys_the_sandbox(monkeypatch):
    """Otherwise the create raises with a live sandbox nobody holds an id for --
    the orphan the Daytona backend has to hunt by label. Here the id is already
    in hand, so there is no excuse for leaking it."""
    calls = _capture(
        monkeypatch,
        [
            {"sandboxId": "sbx-leak"},          # create
            _http_error(500),                    # mkdir exec fails
            {},                                  # DELETE
        ],
    )
    with pytest.raises(SandboxExecutionError):
        cru.CrucibleClient().create(
            SandboxConfig(backend="crucible", image="img", workdir="/tmp/pdk")
        )
    assert calls[-1]["method"] == "DELETE"
    assert "sbx-leak" in calls[-1]["url"]


# -- files -----------------------------------------------------------------


def test_read_and_write_round_trip_base64(monkeypatch):
    payload = b"\x00\x01binary\xff"
    calls = _capture(
        monkeypatch,
        [{}, {"contentBase64": base64.b64encode(payload).decode()}],
    )
    session = cru.CrucibleSession(
        base_url="https://crucible.example", api_key="k", sandbox_id="s"
    )
    session.write_file("/tmp/x", payload)
    assert base64.b64decode(calls[0]["body"]["contentBase64"]) == payload
    assert session.read_file("/tmp/x") == payload


def test_an_oversized_inline_write_is_refused_locally(monkeypatch):
    """The platform rejects a body over 32 MiB at the frontend, before crucible
    sees it, and answers bare HTML. Refusing here names the alternative."""
    calls = _capture(monkeypatch, [])
    session = cru.CrucibleSession(
        base_url="https://crucible.example", api_key="k", sandbox_id="s"
    )
    with pytest.raises(SandboxConfigurationError) as excinfo:
        session.write_file("/tmp/big", b"x" * (cru._MAX_INLINE_BYTES + 1))
    assert "signed-URL" in str(excinfo.value)
    assert calls == [], "must not attempt a request that cannot succeed"


def test_a_transfer_at_the_limit_is_allowed(monkeypatch):
    calls = _capture(monkeypatch, [{}])
    session = cru.CrucibleSession(
        base_url="https://crucible.example", api_key="k", sandbox_id="s"
    )
    session.write_file("/tmp/ok", b"x" * cru._MAX_INLINE_BYTES)
    assert len(calls) == 1


def test_the_inline_limit_sits_under_the_platform_ceiling():
    """32 MiB on the wire, and base64 costs 4/3. A limit above that would make
    the refusal above useless -- the frontend would answer first."""
    assert cru._MAX_INLINE_BYTES * 4 / 3 < 32 * 1024 * 1024


# -- exec / lifecycle ------------------------------------------------------


def test_exec_sends_argv_separately_and_reports_all_three_fields(monkeypatch):
    calls = _capture(
        monkeypatch,
        [{"stdout": "out", "stderr": "err", "exitCode": 3}],
    )
    session = cru.CrucibleSession(
        base_url="https://crucible.example",
        api_key="k",
        sandbox_id="s",
        workdir="/tmp/pdk",
    )
    result = session.execute_command("ls", ["-la", "/a b"])
    assert calls[0]["body"] == {
        "command": "ls",
        "args": ["-la", "/a b"],
        "workingDir": "/tmp/pdk",
    }
    assert (result.stdout, result.stderr, result.exit_code) == ("out", "err", 3)


def test_execute_code_passes_source_on_stdin_not_argv(monkeypatch):
    """Code containing quotes is ordinary. Interpolating it into argv would
    break on the first one."""
    calls = _capture(monkeypatch, [{"stdout": "", "stderr": "", "exitCode": 0}])
    session = cru.CrucibleSession(
        base_url="https://crucible.example", api_key="k", sandbox_id="s"
    )
    code = "print('it\\'s fine')"
    session.execute_code(code)
    sent = calls[0]["body"]
    assert sent["command"] == "sh"
    shell = sent["args"][1]

    # `"python3 -" in shell` is NOT enough: `python3 -c print(...)` contains that
    # substring too, so the obvious assertion passes on the very mutation it is
    # meant to catch. Found by mutating this file. Assert the mechanism instead:
    # the source is piped in, and it appears SHELL-QUOTED rather than raw.
    assert "printf" in shell, "source must arrive on stdin, not in argv"
    assert shlex.quote(code) in shell, "source must be shell-quoted"
    assert " -c " not in shell, "must not pass source as a -c argument"


def test_an_unknown_language_is_refused(monkeypatch):
    session = cru.CrucibleSession(
        base_url="https://crucible.example", api_key="k", sandbox_id="s"
    )
    with pytest.raises(FeatureNotSupportedError, match="cannot execute"):
        session.execute_code("puts 1", language="ruby")


def test_resume_checks_existence_so_a_dead_sandbox_fails_now(monkeypatch):
    """Cheap here because crucible is stateless HTTP -- the id is the whole
    handle. Without the check, a resumed-but-gone sandbox surfaces as a puzzling
    failure on the first read instead of at resume."""
    _capture(monkeypatch, [_http_error(404)])
    with pytest.raises(SandboxNotFoundError):
        cru.CrucibleClient().resume({"sandbox_id": "sbx-gone"})


def test_resume_round_trips_the_workdir(monkeypatch):
    _capture(monkeypatch, [{"sandboxId": "s"}])
    session = cru.CrucibleClient().resume(
        {"sandbox_id": "s", "workdir": "/tmp/pdk"}
    )
    assert session.session_state() == {"sandbox_id": "s", "workdir": "/tmp/pdk"}


def test_destroy_never_raises(monkeypatch):
    """Matches every other backend: teardown must not mask the error that led
    the caller to tear down."""
    _capture(monkeypatch, [_http_error(500)])
    session = cru.CrucibleSession(
        base_url="https://crucible.example", api_key="k", sandbox_id="s"
    )
    session.destroy()          # must not raise


def test_list_files_maps_the_response_shape(monkeypatch):
    _capture(
        monkeypatch,
        [{"entries": [{"path": "/a", "isDir": True, "size": None},
                      {"path": "/b", "isDir": False, "size": 12}]}],
    )
    session = cru.CrucibleSession(
        base_url="https://crucible.example", api_key="k", sandbox_id="s"
    )
    entries = session.list_files("/")
    assert [(e.path, e.is_dir, e.size) for e in entries] == [
        ("/a", True, None),
        ("/b", False, 12),
    ]


@pytest.mark.parametrize("raw", ["abc", 0, -1])
def test_a_bad_request_timeout_is_refused(monkeypatch, raw):
    with pytest.raises(SandboxConfigurationError):
        cru.CrucibleClient().create(
            SandboxConfig(
                backend="crucible",
                image="img",
                backend_options={"request_timeout": raw},
            )
        )
