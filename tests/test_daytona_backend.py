"""Daytona backend tests.

Every Daytona SDK call is faked -- these tests never touch the network and
never create, start, stop or delete a real sandbox.
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import tempfile
import threading
import time
import unittest
import warnings
from unittest import mock

from bespokelabs.sandbox import Sandbox, SandboxPreset, _metrics
from bespokelabs.sandbox.exceptions import (
    ErrorCode,
    ErrorOutcome,
    SandboxConfigurationError,
    SandboxCreationError,
    SandboxError,
    SandboxTimeoutError,
)
from bespokelabs.sandbox.types import SandboxConfig

try:
    import daytona  # noqa: F401

    _HAS_DAYTONA = True
except ImportError:  # pragma: no cover - exercised only without the extra
    _HAS_DAYTONA = False

if _HAS_DAYTONA:
    from bespokelabs.sandbox.backends import daytona as daytona_backend
    from bespokelabs.sandbox.backends.daytona import (
        _CREATE_TOKEN_LABEL,
        IDLE_GAP_METRIC,
        MAX_IDLE_GAP_METRIC,
        DaytonaClient,
        DaytonaSession,
        _build_params,
        _classify_daytona_error,
        _CleanupReport,
    )


# The exact shape of the failure seen in production: the create succeeded
# server-side and only the HTTP response timed out, so the error text carries
# no sandbox id -- and neither does the exception, whose __cause__ the SDK's
# intercept_errors decorator drops by re-raising ``from None``.
_READ_TIMEOUT = "Failed to create sandbox: HTTPSConnectionPool(host='app.daytona.io', port=443): Read timed out."


class _FakeProcess:

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict | None = None,
        timeout: int | None = None,
    ):
        self.calls.append((command, cwd))
        return _FakeResponse()


class _FakeResponse:
    result = ""
    exit_code = 0


class _FakeSandbox:

    def __init__(self, sandbox_id: str, labels: dict[str, str]) -> None:
        self.id = sandbox_id
        self.labels = labels
        self.process = _FakeProcess()


@contextlib.contextmanager
def _captured_param_kwargs():
    """Record the kwargs every Daytona params constructor receives.

    Params models ignore unknown fields, so inspecting the built object cannot
    show that a backend-only option leaked into the constructor call.
    """
    import daytona  # type: ignore[import-untyped]

    sent: list[dict] = []

    def spy(cls):
        def build(*args, **kwargs):
            sent.append(dict(kwargs))
            return cls(*args, **kwargs)

        return build

    with mock.patch.object(
        daytona,
        "CreateSandboxFromSnapshotParams",
        spy(daytona.CreateSandboxFromSnapshotParams),
    ), mock.patch.object(
        daytona,
        "CreateSandboxFromImageParams",
        spy(daytona.CreateSandboxFromImageParams),
    ):
        yield sent


class _FakeDaytona:
    """Stand-in for ``daytona.Daytona`` with the same surface we depend on."""

    def __init__(
        self,
        *,
        fail_with: BaseException | None = None,
        list_error: Exception | None = None,
        delete_error: Exception | None = None,
        create_resource_on_failure: bool = True,
    ) -> None:
        self._fail_with = fail_with
        self._list_error = list_error
        self._delete_error = delete_error
        self._create_resource_on_failure = create_resource_on_failure
        self.live: list[_FakeSandbox] = []
        self.created: list[tuple[object, dict]] = []
        self.deleted: list[_FakeSandbox] = []
        self.list_queries: list[object] = []

    def create(self, params=None, **kwargs):
        self.created.append((params, kwargs))
        # Daytona builds the sandbox before the client ever sees a response,
        # so it exists even when the call goes on to fail.
        sandbox = _FakeSandbox(
            f"sbx-{len(self.created)}",
            dict(getattr(params, "labels", None) or {}),
        )
        if self._fail_with is None or self._create_resource_on_failure:
            self.live.append(sandbox)
        if self._fail_with is not None:
            raise self._fail_with
        return sandbox

    def list(self, query=None, request_timeout: float | None = None):
        self.list_queries.append(query)
        if self._list_error is not None:
            raise self._list_error
        labels = (
            (getattr(query, "labels", None) or {}) if query is not None else {}
        )
        # The real SDK returns an Iterator[Sandbox], not a list.
        return iter(
            [
                s
                for s in self.live
                if all(s.labels.get(k) == v for k, v in labels.items())
            ]
        )

    def delete(self, sandbox, timeout: float = 60, wait: bool = False) -> None:
        if self._delete_error is not None:
            raise self._delete_error
        self.deleted.append(sandbox)
        self.live.remove(sandbox)


def _client(fake: _FakeDaytona) -> DaytonaClient:
    client = DaytonaClient()
    # Bypass _ensure_client so no DAYTONA_API_KEY or real SDK client is needed.
    client._client = fake
    return client


@unittest.skipUnless(_HAS_DAYTONA, "Daytona SDK not installed")
class DaytonaCreateLeakTests(unittest.TestCase):
    """A create that fails after the server built the sandbox must not leak it."""

    def test_create_timeout_reaps_the_orphaned_sandbox(self) -> None:
        fake = _FakeDaytona(fail_with=RuntimeError(_READ_TIMEOUT))

        with self.assertRaises(SandboxCreationError) as ctx:
            _client(fake).create(
                SandboxConfig(backend="daytona", snapshot_id="snap")
            )

        self.assertIn("Read timed out", str(ctx.exception))
        self.assertEqual([s.id for s in fake.deleted], ["sbx-1"])
        self.assertEqual(
            fake.live, [], "a started, billing sandbox was left behind"
        )

    def test_orphan_is_found_by_a_label_unique_to_the_create_call(self) -> None:
        fake = _FakeDaytona(fail_with=RuntimeError(_READ_TIMEOUT))
        config = SandboxConfig(backend="daytona", snapshot_id="snap")

        for _ in range(2):
            with self.assertRaises(SandboxCreationError):
                _client(fake).create(config)

        tokens = [s.labels[_CREATE_TOKEN_LABEL] for s in fake.deleted]
        self.assertEqual(len(tokens), 2)
        self.assertNotEqual(
            tokens[0],
            tokens[1],
            "the reap label must be unique per create call",
        )
        # Each reap deleted only its own sandbox, not everything with the label.
        for query, token in zip(fake.list_queries, tokens, strict=True):
            self.assertEqual(query.labels, {_CREATE_TOKEN_LABEL: token})

    def test_caller_labels_cannot_displace_the_reap_label(self) -> None:
        fake = _FakeDaytona(fail_with=RuntimeError(_READ_TIMEOUT))
        config = SandboxConfig(
            backend="daytona",
            snapshot_id="snap",
            backend_options={"labels": {"team": "platform"}},
        )

        with self.assertRaises(SandboxCreationError):
            _client(fake).create(config)

        params = fake.created[0][0]
        self.assertEqual(params.labels["team"], "platform")
        self.assertIn(_CREATE_TOKEN_LABEL, params.labels)
        self.assertEqual(fake.live, [])

    def test_reap_failure_does_not_mask_the_creation_error(self) -> None:
        fake = _FakeDaytona(
            fail_with=RuntimeError(_READ_TIMEOUT),
            list_error=RuntimeError("list is down too"),
        )

        with self.assertRaises(SandboxCreationError) as ctx:
            _client(fake).create(
                SandboxConfig(backend="daytona", snapshot_id="snap")
            )

        self.assertIn("Read timed out", str(ctx.exception))
        self.assertNotIn("list is down too", str(ctx.exception))
        self.assertEqual(ctx.exception.context["cleanup_status"], "unknown")
        self.assertEqual(ctx.exception.outcome, ErrorOutcome.UNKNOWN)
        self.assertFalse(ctx.exception.retryable)

    def test_successful_empty_search_reports_not_found(self) -> None:
        fake = _FakeDaytona(
            fail_with=RuntimeError("create rejected"),
            create_resource_on_failure=False,
        )

        with self.assertRaises(SandboxCreationError) as ctx:
            _client(fake).create(
                SandboxConfig(backend="daytona", snapshot_id="snap")
            )

        self.assertEqual(ctx.exception.context["cleanup_status"], "not_found")
        self.assertEqual(ctx.exception.outcome, ErrorOutcome.FAILED)

    def test_failed_delete_reports_unknown_and_blocks_timeout_retry(
        self,
    ) -> None:
        from daytona.common.errors import DaytonaConnectionTimeoutError

        fake = _FakeDaytona(
            fail_with=DaytonaConnectionTimeoutError("response timeout"),
            delete_error=RuntimeError("delete unavailable"),
        )

        with self.assertRaises(SandboxTimeoutError) as ctx:
            _client(fake).create(
                SandboxConfig(backend="daytona", snapshot_id="snap")
            )

        error = ctx.exception
        self.assertEqual(error.code, ErrorCode.TIMEOUT)
        self.assertEqual(error.context["cleanup_status"], "unknown")
        self.assertEqual(error.outcome, ErrorOutcome.UNKNOWN)
        self.assertFalse(error.retryable)
        self.assertEqual([sandbox.id for sandbox in fake.live], ["sbx-1"])

    def test_typed_timeout_is_classified_without_message_parsing(self) -> None:
        from daytona.common.errors import DaytonaConnectionTimeoutError

        fake = _FakeDaytona(
            fail_with=DaytonaConnectionTimeoutError("totally opaque")
        )

        with self.assertRaises(SandboxTimeoutError) as ctx:
            _client(fake).create(
                SandboxConfig(backend="daytona", snapshot_id="snap")
            )

        error = ctx.exception
        self.assertEqual(error.code, ErrorCode.TIMEOUT)
        self.assertEqual(error.backend, "daytona")
        self.assertEqual(error.op, "create")
        self.assertTrue(error.retryable)
        self.assertEqual(error.context["cleanup_status"], "deleted")
        self.assertEqual(fake.live, [])

    def test_keyboard_interrupt_during_create_reaps_and_propagates_unchanged(
        self,
    ) -> None:
        fake = _FakeDaytona(fail_with=KeyboardInterrupt())

        with self.assertRaises(KeyboardInterrupt):
            _client(fake).create(
                SandboxConfig(backend="daytona", snapshot_id="snap")
            )

        self.assertEqual([s.id for s in fake.deleted], ["sbx-1"])
        self.assertEqual(fake.live, [])

    def test_successful_create_reaps_nothing(self) -> None:
        fake = _FakeDaytona()

        _client(fake).create(
            SandboxConfig(backend="daytona", snapshot_id="snap")
        )

        self.assertEqual(fake.deleted, [])
        self.assertEqual(len(fake.live), 1)


@unittest.skipUnless(_HAS_DAYTONA, "Daytona SDK not installed")
class DaytonaErrorClassificationTests(unittest.TestCase):

    def test_sdk_types_not_messages_determine_the_error_contract(self) -> None:
        from daytona.common.errors import (
            DaytonaAuthenticationError,
            DaytonaConnectionError,
            DaytonaConnectionTimeoutError,
            DaytonaError,
        )

        cases = [
            (
                DaytonaAuthenticationError("looks like a timeout"),
                "create",
                ErrorCode.AUTHENTICATION,
                False,
            ),
            (
                DaytonaConnectionError("looks like bad credentials"),
                "create",
                ErrorCode.CONNECTION,
                True,
            ),
            (
                DaytonaConnectionTimeoutError("unclassified words"),
                "create",
                ErrorCode.TIMEOUT,
                True,
            ),
            (
                DaytonaError("looks like a connection timeout"),
                "execute",
                ErrorCode.EXECUTION_FAILED,
                False,
            ),
        ]
        for sdk_error, op, code, retryable in cases:
            with self.subTest(code=code):
                error = _classify_daytona_error(
                    sdk_error,
                    op=op,
                    cleanup=_CleanupReport("not_found")
                    if op == "create"
                    else None,
                )
                self.assertEqual(error.code, code)
                self.assertEqual(error.retryable, retryable)
                self.assertEqual(error.backend, "daytona")
                self.assertEqual(error.op, op)


@unittest.skipUnless(_HAS_DAYTONA, "Daytona SDK not installed")
class DaytonaCreateTimeoutOptionTests(unittest.TestCase):

    def test_create_timeout_is_forwarded_to_the_sdk(self) -> None:
        fake = _FakeDaytona()
        config = SandboxConfig(
            backend="daytona",
            snapshot_id="snap",
            backend_options={"create_timeout": 300},
        )

        _client(fake).create(config)

        self.assertEqual(fake.created[0][1], {"timeout": 300.0})
        # It targets Daytona.create(), so it must not leak into the params
        # object, where pydantic would silently swallow it.
        self.assertFalse(hasattr(fake.created[0][0], "create_timeout"))

    def test_create_timeout_defaults_to_the_sdk_default(self) -> None:
        fake = _FakeDaytona()

        _client(fake).create(
            SandboxConfig(backend="daytona", snapshot_id="snap")
        )

        self.assertEqual(fake.created[0][1], {})

    def test_zero_create_timeout_is_rejected_because_it_means_no_timeout(
        self,
    ) -> None:
        fake = _FakeDaytona()
        config = SandboxConfig(
            backend="daytona",
            snapshot_id="snap",
            backend_options={"create_timeout": 0},
        )

        with self.assertRaises(SandboxConfigurationError) as ctx:
            _client(fake).create(config)

        self.assertIn("no timeout at all", str(ctx.exception))
        self.assertEqual(fake.created, [])


@unittest.skipUnless(_HAS_DAYTONA, "Daytona SDK not installed")
class DaytonaDroppedConfigTests(unittest.TestCase):
    """timeout_secs and workdir must reach Daytona, not be silently discarded."""

    def _params(self, **config_kwargs):
        config = SandboxConfig(
            backend="daytona", snapshot_id="snap", **config_kwargs
        )
        return _build_params(config, create_token="tok")

    def test_explicit_timeout_secs_is_not_silently_dropped(self) -> None:
        # The 24h lifetime a caller asks for has to arrive as a real bound.
        params = self._params(
            timeout_secs=24 * 60 * 60, timeout_secs_explicit=True
        )
        self.assertEqual(params.ttl_minutes, 1440)

    def test_explicit_timeout_secs_rounds_up_and_never_floors_to_zero(
        self,
    ) -> None:
        # ttl_minutes=0 means "no TTL" to Daytona, the opposite of a 30s bound.
        self.assertEqual(
            self._params(
                timeout_secs=30, timeout_secs_explicit=True
            ).ttl_minutes,
            1,
        )
        self.assertEqual(
            self._params(
                timeout_secs=90, timeout_secs_explicit=True
            ).ttl_minutes,
            2,
        )

    def test_unrequested_timeout_secs_does_not_become_the_ttl(self) -> None:
        # ttl_minutes destroys the sandbox in any state and no activity resets
        # it, so a value nobody asked for must not become a lifetime cap: not
        # the 600s dataclass default, and not the 1800s the agent presets
        # recommend.  The sandbox gets the one-day backstop instead.
        for timeout_secs in (600, 1800):
            with self.subTest(timeout_secs=timeout_secs):
                params = self._params(timeout_secs=timeout_secs)
                self.assertEqual(params.ttl_minutes, 24 * 60)

    def test_backend_options_can_still_set_the_ttl(self) -> None:
        # It overrides a derived TTL...
        params = self._params(
            timeout_secs=600,
            timeout_secs_explicit=True,
            backend_options={"ttl_minutes": 5},
        )
        self.assertEqual(params.ttl_minutes, 5)
        # ...and remains the way to ask for one without a timeout_secs.
        self.assertEqual(
            self._params(backend_options={"ttl_minutes": 5}).ttl_minutes, 5
        )

    def test_workdir_is_not_silently_dropped(self) -> None:
        fake = _FakeDaytona()
        config = SandboxConfig(
            backend="daytona", snapshot_id="snap", workdir="/work/project"
        )

        session = _client(fake).create(config)
        session.execute_command("pytest", ["-q"])

        sandbox = fake.live[0]
        self.assertEqual(
            sandbox.process.calls,
            [("mkdir -p /work/project", None), ("pytest -q", "/work/project")],
        )

    def test_workdir_survives_session_state_and_resume(self) -> None:
        fake = _FakeDaytona()
        config = SandboxConfig(
            backend="daytona", snapshot_id="snap", workdir="/work/project"
        )
        client = _client(fake)

        state = client.create(config).session_state()

        self.assertEqual(
            state, {"sandbox_id": "sbx-1", "workdir": "/work/project"}
        )
        fake.get = lambda sandbox_id: fake.live[0]  # noqa: E731 - stand in for Daytona.get
        client.resume(state).execute_command("pwd")
        self.assertEqual(
            fake.live[0].process.calls[-1], ("pwd", "/work/project")
        )

    def test_session_state_omits_workdir_when_unset(self) -> None:
        fake = _FakeDaytona()

        state = (
            _client(fake)
            .create(SandboxConfig(backend="daytona", snapshot_id="snap"))
            .session_state()
        )

        self.assertEqual(state, {"sandbox_id": "sbx-1"})

    def test_backend_options_env_vars_do_not_clobber_the_env_dict(self) -> None:
        params = self._params(
            env_vars={"ANTHROPIC_API_KEY": "secret", "SHARED": "from-env-vars"},
            backend_options={
                "env_vars": {"EXTRA": "1", "SHARED": "from-options"}
            },
        )

        self.assertEqual(
            params.env_vars,
            {
                "ANTHROPIC_API_KEY": "secret",
                "SHARED": "from-options",
                "EXTRA": "1",
            },
        )


class TimeoutSecsExplicitnessTests(unittest.TestCase):
    """``Sandbox()`` must record whether ``timeout_secs`` came from the caller.

    Daytona is the only backend that reads the flag -- it is what keeps a
    default from becoming a hard wall-clock destroy -- but the wiring lives in
    ``Sandbox.__init__``, so it is pinned here on the local backend, off the
    network and without the Daytona SDK.
    """

    def _config(self, **kwargs) -> SandboxConfig:
        sb = Sandbox("local", **kwargs)
        self.addCleanup(sb.destroy)
        return sb._config

    def test_caller_supplied_timeout_secs_is_explicit(self) -> None:
        config = self._config(timeout_secs=300)
        self.assertEqual(config.timeout_secs, 300)
        self.assertTrue(config.timeout_secs_explicit)

    def test_omitted_timeout_secs_is_not_explicit(self) -> None:
        config = self._config()
        self.assertEqual(config.timeout_secs, 600)
        self.assertFalse(config.timeout_secs_explicit)

    def test_preset_timeout_secs_is_a_recommendation_not_a_request(
        self,
    ) -> None:
        preset = SandboxPreset(
            name="_ttl_test", description="", timeout_secs=1800
        )

        config = self._config(preset=preset)

        # The preset's value still reaches the backends that read it...
        self.assertEqual(config.timeout_secs, 1800)
        # ...but it must not turn into a 30-minute destroy on Daytona.
        self.assertFalse(config.timeout_secs_explicit)


@unittest.skipUnless(_HAS_DAYTONA, "Daytona SDK not installed")
class DaytonaLifecycleDefaultsTests(unittest.TestCase):
    """A sandbox whose caller set no lifecycle must still end on its own.

    The organization default for auto-stop can be off, and with no stop the
    auto-delete timer never starts either: a driver killed before destroy()
    leaves the sandbox running forever.
    """

    def _params(self, **config_kwargs):
        config = SandboxConfig(
            backend="daytona", snapshot_id="snap", **config_kwargs
        )
        return _build_params(config, create_token="tok")

    def test_nothing_set_gets_idle_stop_delete_after_stop_and_ttl(
        self,
    ) -> None:
        params = self._params()
        self.assertEqual(params.auto_stop_interval, 240)
        self.assertEqual(params.auto_delete_interval, 60)
        self.assertEqual(params.ttl_minutes, 24 * 60)

    def test_defaults_reach_image_params_too(self) -> None:
        config = SandboxConfig(backend="daytona", image="python:3.12-slim")
        params = _build_params(config, create_token="tok")
        self.assertEqual(
            (
                params.auto_stop_interval,
                params.auto_delete_interval,
                params.ttl_minutes,
            ),
            (240, 60, 24 * 60),
        )

    def test_idle_stop_outlasts_a_long_blocking_agent_call(self) -> None:
        # A long single blocking agent call can make no Toolbox call for well
        # over an hour; a 60-minute idle stop would stop it mid-run.
        self.assertGreaterEqual(self._params().auto_stop_interval, 4 * 60)

    def test_explicit_values_are_kept_including_zero(self) -> None:
        # 0 means "disabled" for auto-stop and TTL and "delete on stop" for
        # auto-delete; each is a decision, not an absence.
        params = self._params(
            backend_options={
                "auto_stop_interval": 0,
                "auto_delete_interval": 0,
                "ttl_minutes": 0,
            }
        )
        self.assertEqual(params.auto_stop_interval, 0)
        self.assertEqual(params.auto_delete_interval, 0)
        self.assertEqual(params.ttl_minutes, 0)

        params = self._params(
            backend_options={
                "auto_stop_interval": 15,
                "auto_delete_interval": 600,
            }
        )
        self.assertEqual(params.auto_stop_interval, 15)
        self.assertEqual(params.auto_delete_interval, 600)

    def test_each_field_defaults_independently(self) -> None:
        # A caller who bounded only idle time still gets the other two.
        params = self._params(backend_options={"auto_stop_interval": 0})
        self.assertEqual(params.auto_stop_interval, 0)
        self.assertEqual(params.auto_delete_interval, 60)
        self.assertEqual(params.ttl_minutes, 24 * 60)

    def test_explicit_timeout_secs_replaces_the_ttl_backstop(self) -> None:
        # A run longer than a day asks for it, and gets it.
        params = self._params(
            timeout_secs=3 * 24 * 60 * 60, timeout_secs_explicit=True
        )
        self.assertEqual(params.ttl_minutes, 3 * 24 * 60)
        self.assertEqual(params.auto_stop_interval, 240)

    def test_ephemeral_caller_gets_no_conflicting_auto_delete(self) -> None:
        # ephemeral forces auto_delete_interval=0 and the SDK warns when both
        # are set, so ephemeral alone counts as the caller's decision.
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            params = self._params(backend_options={"ephemeral": True})
        self.assertEqual(params.auto_delete_interval, 0)
        self.assertEqual(params.auto_stop_interval, 240)

        params = self._params(backend_options={"ephemeral": False})
        self.assertIsNone(params.auto_delete_interval)

    def test_auto_pause_caller_gets_no_auto_stop(self) -> None:
        # Daytona allows at most one of auto-stop and auto-pause non-zero.
        params = self._params(backend_options={"auto_pause_interval": 30})
        self.assertIsNone(params.auto_stop_interval)
        self.assertNotIn("auto_stop_interval", params.model_fields_set)
        self.assertEqual(params.ttl_minutes, 24 * 60)

    def test_defaults_reach_the_create_call(self) -> None:
        fake = _FakeDaytona()
        _client(fake).create(SandboxConfig(backend="daytona"))
        params, _ = fake.created[0]
        self.assertEqual(params.auto_stop_interval, 240)
        self.assertEqual(params.auto_delete_interval, 60)
        self.assertEqual(params.ttl_minutes, 24 * 60)


class _SlowSandbox:
    """A sandbox whose exec blocks until released, counting keepalives."""

    def __init__(self, *, refresh_error: Exception | None = None) -> None:
        self.id = "sbx-slow"
        self.cpu = 2
        self.memory = 4
        self.gpu = 0
        self.labels = {"example-pipeline-label": "example-pipeline"}
        self.refresh_calls: list[float | None] = []
        self.refresh_error = refresh_error
        self.release = threading.Event()
        self.exec_error: Exception | None = None
        sandbox = self

        class _Process:

            def exec(self, command, cwd=None):
                assert sandbox.release.wait(5), "exec never released"
                if sandbox.exec_error is not None:
                    raise sandbox.exec_error
                return _FakeResponse()

            def code_run(self, code):
                return self.exec(code)

        self.process = _Process()

    def refresh_activity(self, request_timeout: float | None = None) -> None:
        self.refresh_calls.append(request_timeout)
        if self.refresh_error is not None:
            raise self.refresh_error


class _RecordingSink:

    def __init__(self) -> None:
        self.points: list[tuple[str, float, dict]] = []

    def __call__(self, name, value, tags) -> None:
        self.points.append((name, value, dict(tags)))

    def named(self, name: str) -> list[tuple[str, float, dict]]:
        return [p for p in self.points if p[0] == name]


def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition never held")
        time.sleep(0.01)


@unittest.skipUnless(_HAS_DAYTONA, "Daytona SDK not installed")
class DaytonaKeepaliveTests(unittest.TestCase):

    def setUp(self) -> None:
        self.sink = _RecordingSink()
        _metrics.set_metrics_sink(self.sink)
        self.addCleanup(_metrics.set_metrics_sink, None)

    def _run_exec(self, session, sandbox) -> threading.Thread:
        result: dict = {}

        def run() -> None:
            try:
                result["value"] = session.execute_command("sleep 600")
            except Exception as exc:  # noqa: BLE001 - asserted by the test
                result["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.result = result
        thread.start()
        return thread

    def test_pinned_sdk_has_refresh_activity(self) -> None:
        from daytona import Sandbox as DaytonaSandbox

        self.assertTrue(callable(DaytonaSandbox.refresh_activity))

    def test_heartbeat_runs_during_a_long_call_and_stops_after(self) -> None:
        sandbox = _SlowSandbox()
        session = DaytonaSession(
            client=object(), sandbox=sandbox, keepalive_secs=0.02
        )
        thread = self._run_exec(session, sandbox)
        _wait_for(lambda: len(sandbox.refresh_calls) >= 3)
        sandbox.release.set()
        thread.join(5)
        self.assertEqual(thread.result["value"].exit_code, 0)
        self.assertEqual(
            sandbox.refresh_calls[0],
            daytona_backend._KEEPALIVE_REQUEST_TIMEOUT_SECS,
        )
        stopped_at = len(sandbox.refresh_calls)
        time.sleep(0.1)
        self.assertEqual(len(sandbox.refresh_calls), stopped_at)
        self.assertFalse(
            any(t.name == "daytona-keepalive" for t in threading.enumerate())
        )

    def test_heartbeat_stops_when_the_call_raises(self) -> None:
        sandbox = _SlowSandbox()
        sandbox.exec_error = RuntimeError("boom")
        session = DaytonaSession(
            client=object(), sandbox=sandbox, keepalive_secs=0.02
        )
        thread = self._run_exec(session, sandbox)
        _wait_for(lambda: len(sandbox.refresh_calls) >= 1)
        sandbox.release.set()
        thread.join(5)
        self.assertIn("error", thread.result)
        stopped_at = len(sandbox.refresh_calls)
        time.sleep(0.1)
        self.assertEqual(len(sandbox.refresh_calls), stopped_at)

    def test_heartbeat_failure_does_not_break_the_exec(self) -> None:
        sandbox = _SlowSandbox(refresh_error=ConnectionError("down"))
        session = DaytonaSession(
            client=object(), sandbox=sandbox, keepalive_secs=0.01
        )
        with self.assertLogs(daytona_backend.logger, "WARNING") as logs:
            thread = self._run_exec(session, sandbox)
            _wait_for(
                lambda: len(sandbox.refresh_calls)
                >= daytona_backend._KEEPALIVE_MAX_CONSECUTIVE_FAILURES
            )
            time.sleep(0.1)
            sandbox.release.set()
            thread.join(5)
        self.assertEqual(thread.result["value"].exit_code, 0)
        # Bounded: it gives up after a few failures in a row.
        self.assertEqual(
            len(sandbox.refresh_calls),
            daytona_backend._KEEPALIVE_MAX_CONSECUTIVE_FAILURES,
        )
        self.assertTrue(any("giving up" in line for line in logs.output))

    def test_zero_interval_disables_the_heartbeat(self) -> None:
        sandbox = _SlowSandbox()
        sandbox.release.set()
        session = DaytonaSession(
            client=object(), sandbox=sandbox, keepalive_secs=0
        )
        session.execute_command("true")
        self.assertEqual(sandbox.refresh_calls, [])

    def test_interval_is_configurable(self) -> None:
        self.assertEqual(
            DaytonaSession(
                client=object(), sandbox=_SlowSandbox()
            )._keepalive_secs,
            300,
        )
        with mock.patch.dict(
            os.environ, {daytona_backend._KEEPALIVE_ENV: "60"}
        ):
            self.assertEqual(
                DaytonaSession(
                    client=object(), sandbox=_SlowSandbox()
                )._keepalive_secs,
                60,
            )
        fake = _FakeDaytona()
        with _captured_param_kwargs() as sent:
            session = _client(fake).create(
                SandboxConfig(
                    backend="daytona",
                    backend_options={"keepalive_interval_secs": 120},
                )
            )
        self.assertEqual(session._keepalive_secs, 120)
        # Consumed here, never sent to Daytona, and carried across resume.
        self.assertTrue(sent)
        for kwargs in sent:
            self.assertNotIn("keepalive_interval_secs", kwargs)
        self.assertEqual(
            session.session_state()["keepalive_interval_secs"], 120
        )
        with self.assertRaises(SandboxConfigurationError):
            _client(_FakeDaytona()).create(
                SandboxConfig(
                    backend="daytona",
                    backend_options={"keepalive_interval_secs": -1},
                )
            )

    def test_env_interval_is_carried_across_resume(self) -> None:
        fake = _FakeDaytona()
        with mock.patch.dict(
            os.environ, {daytona_backend._KEEPALIVE_ENV: "60"}
        ):
            state = (
                _client(fake)
                .create(SandboxConfig(backend="daytona", snapshot_id="snap"))
                .session_state()
            )
        self.assertEqual(state["keepalive_interval_secs"], 60)
        fake.get = lambda sandbox_id: fake.live[0]  # noqa: E731 - stand in for Daytona.get
        with mock.patch.dict(
            os.environ, {daytona_backend._KEEPALIVE_ENV: "900"}
        ):
            resumed = _client(fake).resume(state)
        self.assertEqual(resumed._keepalive_secs, 60)


@unittest.skipUnless(_HAS_DAYTONA, "Daytona SDK not installed")
class DaytonaIdleGapMetricTests(unittest.TestCase):

    def setUp(self) -> None:
        self.sink = _RecordingSink()
        _metrics.set_metrics_sink(self.sink)
        self.addCleanup(_metrics.set_metrics_sink, None)
        self.clock = [1000.0]
        patcher = mock.patch.object(
            daytona_backend.time, "monotonic", lambda: self.clock[0]
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # The tag env vars must come only from the test that sets them.
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            not in (
                daytona_backend._CALLER_ENV,
                daytona_backend._METRIC_TAG_LABELS_ENV,
            )
        }
        env_patcher = mock.patch.dict(os.environ, env, clear=True)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def _session(self, **kwargs) -> DaytonaSession:
        sandbox = _SlowSandbox()
        sandbox.release.set()
        return DaytonaSession(
            client=_FakeDaytona(), sandbox=sandbox, keepalive_secs=0, **kwargs
        )

    def test_gap_math_and_per_sandbox_max(self) -> None:
        session = self._session()
        self.clock[0] += 30  # 30s from session start to the first call
        session.execute_command("a")
        self.clock[0] += 400  # the longest gap
        session.execute_command("b")
        self.clock[0] += 10
        session.execute_command("c")
        self.clock[0] += 50  # trailing idle before destroy counts too
        session._client.live.append(session._sandbox)
        session.destroy()

        gaps = [v for _, v, _ in self.sink.named(IDLE_GAP_METRIC)]
        self.assertEqual(gaps, [30, 400, 10])
        (max_point,) = self.sink.named(MAX_IDLE_GAP_METRIC)
        self.assertEqual(max_point[1], 400)
        # No label is read as a tag unless a mapping is configured.
        self.assertEqual(
            max_point[2],
            {"backend": "daytona", "sandbox_class": "cpu2-mem4g"},
        )
        self.assertEqual(
            self.sink.named(IDLE_GAP_METRIC)[0][2]["op"], "execute_command"
        )

    def test_gap_is_measured_from_the_end_of_the_previous_call(self) -> None:
        session = self._session()
        sandbox = session._sandbox
        clock = self.clock

        class _TimedProcess:

            def exec(self, command, cwd=None):
                clock[0] += 3600  # a one-hour blocking call
                return _FakeResponse()

        sandbox.process = _TimedProcess()
        session.execute_command("long")
        clock[0] += 5
        session.execute_command("next")
        gaps = [v for _, v, _ in self.sink.named(IDLE_GAP_METRIC)]
        self.assertEqual(gaps, [0, 5])

    def test_max_is_emitted_once_and_tags_are_overridable(self) -> None:
        with mock.patch.dict(
            os.environ, {daytona_backend._CALLER_ENV: "example-caller"}
        ):
            session = self._session(
                metric_tags={"pipeline": "override"},
                metric_tag_labels={"pipeline": "example-pipeline-label"},
            )
        session._client.live.append(session._sandbox)
        session.destroy()
        session.destroy()
        (point,) = self.sink.named(MAX_IDLE_GAP_METRIC)
        self.assertEqual(point[2]["caller"], "example-caller")
        self.assertEqual(point[2]["pipeline"], "override")

    def test_label_mapping_from_backend_options(self) -> None:
        fake = _FakeDaytona()
        with _captured_param_kwargs() as sent:
            session = _client(fake).create(
                SandboxConfig(
                    backend="daytona",
                    snapshot_id="snap",
                    backend_options={
                        "labels": {
                            "example-pipeline-label": "example-pipeline",
                            "example-caller-label": "example-caller",
                        },
                        "metric_tag_labels": {
                            "pipeline": "example-pipeline-label",
                            "caller": "example-caller-label",
                            "team": "absent-label",
                        },
                    },
                )
            )
        # Consumed here, never sent to Daytona.
        self.assertTrue(sent)
        for kwargs in sent:
            self.assertNotIn("metric_tag_labels", kwargs)
        self.assertEqual(session._tags["pipeline"], "example-pipeline")
        self.assertEqual(session._tags["caller"], "example-caller")
        # A mapped label the sandbox does not carry adds no tag.
        self.assertNotIn("team", session._tags)
        self.assertEqual(
            session.session_state()["metric_tag_labels"],
            {
                "pipeline": "example-pipeline-label",
                "caller": "example-caller-label",
                "team": "absent-label",
            },
        )

    def test_label_mapping_from_env_and_carried_across_resume(self) -> None:
        fake = _FakeDaytona()
        config = SandboxConfig(
            backend="daytona",
            snapshot_id="snap",
            backend_options={
                "labels": {
                    "label-a": "example-pipeline",
                    "label-b": "example-caller",
                }
            },
        )
        with mock.patch.dict(
            os.environ,
            {
                daytona_backend._METRIC_TAG_LABELS_ENV: (
                    " pipeline=label-a, caller=label-b,bogus,=x,"
                )
            },
        ):
            with self.assertLogs(daytona_backend.logger, "WARNING"):
                session = _client(fake).create(config)
        self.assertEqual(session._tags["pipeline"], "example-pipeline")
        self.assertEqual(session._tags["caller"], "example-caller")
        state = session.session_state()
        self.assertEqual(
            state["metric_tag_labels"],
            {"pipeline": "label-a", "caller": "label-b"},
        )
        # Resumed with no env var set, the session keeps the mapping.
        fake.get = lambda sandbox_id: fake.live[0]  # noqa: E731
        resumed = _client(fake).resume(state)
        self.assertEqual(resumed._tags["pipeline"], "example-pipeline")

    def test_explicit_empty_label_mapping_survives_resume(self) -> None:
        fake = _FakeDaytona()
        config = SandboxConfig(
            backend="daytona",
            snapshot_id="snap",
            backend_options={
                "labels": {"label-a": "example-pipeline"},
                "metric_tag_labels": {},
            },
        )
        env = {daytona_backend._METRIC_TAG_LABELS_ENV: "pipeline=label-a"}
        with mock.patch.dict(os.environ, env):
            session = _client(fake).create(config)
            self.assertNotIn("pipeline", session._tags)
            state = session.session_state()
            self.assertEqual(state["metric_tag_labels"], {})
            # Resumed where the env var maps the label, the explicit empty
            # mapping still wins.
            fake.get = lambda sandbox_id: fake.live[0]  # noqa: E731
            resumed = _client(fake).resume(state)
        self.assertNotIn("pipeline", resumed._tags)

    def test_caller_label_wins_over_caller_env(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                daytona_backend._CALLER_ENV: "from-env",
                daytona_backend._METRIC_TAG_LABELS_ENV: (
                    "caller=example-pipeline-label"
                ),
            },
        ):
            session = self._session()
        self.assertEqual(session._tags["caller"], "example-pipeline")

    def test_bad_label_mapping_is_a_config_error(self) -> None:
        for bad in ("pipeline=label-a", {"pipeline": 3}, {"": "label"}):
            with self.subTest(bad=bad):
                with self.assertRaises(SandboxConfigurationError):
                    _client(_FakeDaytona()).create(
                        SandboxConfig(
                            backend="daytona",
                            backend_options={"metric_tag_labels": bad},
                        )
                    )

    def test_points_are_emitted_after_the_call_and_the_delete(self) -> None:
        order: list[str] = []
        session = self._session()
        sandbox = session._sandbox

        class _Process:

            def exec(self, command, cwd=None):
                order.append("exec")
                return _FakeResponse()

        sandbox.process = _Process()
        real_delete = session._client.delete

        def delete(sb) -> None:
            order.append("delete")
            real_delete(sb)

        session._client.delete = delete
        session._client.live.append(sandbox)
        _metrics.set_metrics_sink(lambda name, *_: order.append(name))
        session.execute_command("a")
        session.destroy()
        self.assertEqual(
            order, ["exec", IDLE_GAP_METRIC, "delete", MAX_IDLE_GAP_METRIC]
        )

    def test_max_is_emitted_even_when_delete_fails(self) -> None:
        session = self._session()

        def delete(sb) -> None:
            raise RuntimeError("delete unavailable")

        session._client.delete = delete
        with self.assertRaises(SandboxError):
            session.destroy()
        self.assertEqual(len(self.sink.named(MAX_IDLE_GAP_METRIC)), 1)

    def test_structured_log_line_is_written(self) -> None:
        session = self._session()
        session._client.live.append(session._sandbox)
        with self.assertLogs(_metrics.metrics_logger, "INFO") as logs:
            session.destroy()
        self.assertIn(MAX_IDLE_GAP_METRIC, logs.output[0])
        self.assertIn('"sandbox_class": "cpu2-mem4g"', logs.output[0])


class MetricsSinkTests(unittest.TestCase):

    def setUp(self) -> None:
        _metrics.set_metrics_sink(None)
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("DD_AGENT", "DD_DOGSTATSD", "DD_ENV"))
        }
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_tag_line_breaks_cannot_split_a_datagram(self) -> None:
        datagram = _metrics._datagram(
            "x.y", 1.0, {"k\nj": "a\r\nb,c|d#e"}
        ).decode()
        self.assertNotIn("\n", datagram)
        self.assertNotIn("\r", datagram)
        self.assertIn("k_j:a__b_c_d_e", datagram)

    def test_no_op_without_a_sink(self) -> None:
        with mock.patch.object(_metrics.socket, "socket") as make_socket:
            with self.assertLogs(_metrics.metrics_logger, "INFO"):
                _metrics.emit("x.y", 1.0, {"a": "b"})
        make_socket.assert_not_called()

    def test_dogstatsd_over_unix_socket(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "dsd.socket")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            server.bind(path)
            self.addCleanup(server.close)
            with mock.patch.dict(
                os.environ,
                {"DD_DOGSTATSD_SOCKET": path, "DD_ENV": "production"},
            ):
                _metrics.emit(
                    "bespokelabs.sandbox.idle_gap_seconds",
                    12.5,
                    {"backend": "daytona"},
                    log_level=logging.DEBUG,
                )
            server.settimeout(2)
            self.assertEqual(
                server.recv(1024),
                b"bespokelabs.sandbox.idle_gap_seconds:12.5|d"
                b"|#backend:daytona,env:production",
            )

    def test_dogstatsd_setup_is_retried_after_a_failure(self) -> None:
        clock = [1000.0]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "dsd.socket")
            with (
                mock.patch.dict(os.environ, {"DD_DOGSTATSD_SOCKET": path}),
                mock.patch.object(_metrics.time, "monotonic", lambda: clock[0]),
            ):
                self.addCleanup(setattr, _metrics, "_statsd", None)
                # No agent yet: setup fails and is not retried right away.
                _metrics.emit("x.y", 1.0, {}, log_level=logging.DEBUG)
                server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                server.bind(path)
                self.addCleanup(server.close)
                server.settimeout(0.2)
                _metrics.emit("x.y", 2.0, {}, log_level=logging.DEBUG)
                with self.assertRaises(socket.timeout):
                    server.recv(1024)
                # After the retry window it reconnects and sends.
                clock[0] += _metrics._RETRY_SECS + 1
                _metrics.emit("x.y", 3.0, {}, log_level=logging.DEBUG)
                server.settimeout(2)
                self.assertEqual(server.recv(1024), b"x.y:3|d")

    def test_a_failing_sink_never_raises(self) -> None:
        def broken(name, value, tags):
            raise RuntimeError("sink down")

        _metrics.set_metrics_sink(broken)
        self.addCleanup(_metrics.set_metrics_sink, None)
        _metrics.emit("x.y", 1.0, {})


if __name__ == "__main__":
    unittest.main()
