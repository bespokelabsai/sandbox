"""Daytona backend tests.

Every Daytona SDK call is faked -- these tests never touch the network and
never create, start, stop or delete a real sandbox.
"""

from __future__ import annotations

import unittest

from bespokelabs.sandbox.exceptions import SandboxConfigurationError, SandboxCreationError
from bespokelabs.sandbox.types import SandboxConfig

try:
    import daytona  # noqa: F401

    _HAS_DAYTONA = True
except ImportError:  # pragma: no cover - exercised only without the extra
    _HAS_DAYTONA = False

if _HAS_DAYTONA:
    from bespokelabs.sandbox.backends.daytona import _CREATE_TOKEN_LABEL, DaytonaClient


# The exact shape of the failure seen in production: the create succeeded
# server-side and only the HTTP response timed out, so the error text carries
# no sandbox id -- and neither does the exception, whose __cause__ the SDK's
# intercept_errors decorator drops by re-raising ``from None``.
_READ_TIMEOUT = "Failed to create sandbox: HTTPSConnectionPool(host='app.daytona.io', port=443): Read timed out."


class _FakeProcess:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def exec(self, command: str, cwd: str | None = None, env: dict | None = None, timeout: int | None = None):
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


class _FakeDaytona:
    """Stand-in for ``daytona.Daytona`` with the same surface we depend on."""

    def __init__(self, *, fail_with: BaseException | None = None, list_error: Exception | None = None) -> None:
        self._fail_with = fail_with
        self._list_error = list_error
        self.live: list[_FakeSandbox] = []
        self.created: list[tuple[object, dict]] = []
        self.deleted: list[_FakeSandbox] = []
        self.list_queries: list[object] = []

    def create(self, params=None, **kwargs):
        self.created.append((params, kwargs))
        # Daytona builds the sandbox before the client ever sees a response,
        # so it exists even when the call goes on to fail.
        sandbox = _FakeSandbox(f"sbx-{len(self.created)}", dict(getattr(params, "labels", None) or {}))
        self.live.append(sandbox)
        if self._fail_with is not None:
            raise self._fail_with
        return sandbox

    def list(self, query=None, request_timeout: float | None = None):
        self.list_queries.append(query)
        if self._list_error is not None:
            raise self._list_error
        labels = (getattr(query, "labels", None) or {}) if query is not None else {}
        # The real SDK returns an Iterator[Sandbox], not a list.
        return iter([s for s in self.live if all(s.labels.get(k) == v for k, v in labels.items())])

    def delete(self, sandbox, timeout: float = 60, wait: bool = False) -> None:
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
            _client(fake).create(SandboxConfig(backend="daytona", snapshot_id="snap"))

        self.assertIn("Read timed out", str(ctx.exception))
        self.assertEqual([s.id for s in fake.deleted], ["sbx-1"])
        self.assertEqual(fake.live, [], "a started, billing sandbox was left behind")

    def test_orphan_is_found_by_a_label_unique_to_the_create_call(self) -> None:
        fake = _FakeDaytona(fail_with=RuntimeError(_READ_TIMEOUT))
        config = SandboxConfig(backend="daytona", snapshot_id="snap")

        for _ in range(2):
            with self.assertRaises(SandboxCreationError):
                _client(fake).create(config)

        tokens = [s.labels[_CREATE_TOKEN_LABEL] for s in fake.deleted]
        self.assertEqual(len(tokens), 2)
        self.assertNotEqual(tokens[0], tokens[1], "the reap label must be unique per create call")
        # Each reap deleted only its own sandbox, not everything with the label.
        for query, token in zip(fake.list_queries, tokens, strict=True):
            self.assertEqual(query.labels, {_CREATE_TOKEN_LABEL: token})

    def test_caller_labels_cannot_displace_the_reap_label(self) -> None:
        fake = _FakeDaytona(fail_with=RuntimeError(_READ_TIMEOUT))
        config = SandboxConfig(
            backend="daytona", snapshot_id="snap", backend_options={"labels": {"team": "platform"}}
        )

        with self.assertRaises(SandboxCreationError):
            _client(fake).create(config)

        params = fake.created[0][0]
        self.assertEqual(params.labels["team"], "platform")
        self.assertIn(_CREATE_TOKEN_LABEL, params.labels)
        self.assertEqual(fake.live, [])

    def test_reap_failure_does_not_mask_the_creation_error(self) -> None:
        fake = _FakeDaytona(
            fail_with=RuntimeError(_READ_TIMEOUT), list_error=RuntimeError("list is down too")
        )

        with self.assertRaises(SandboxCreationError) as ctx:
            _client(fake).create(SandboxConfig(backend="daytona", snapshot_id="snap"))

        self.assertIn("Read timed out", str(ctx.exception))
        self.assertNotIn("list is down too", str(ctx.exception))

    def test_keyboard_interrupt_during_create_reaps_and_propagates_unchanged(self) -> None:
        fake = _FakeDaytona(fail_with=KeyboardInterrupt())

        with self.assertRaises(KeyboardInterrupt):
            _client(fake).create(SandboxConfig(backend="daytona", snapshot_id="snap"))

        self.assertEqual([s.id for s in fake.deleted], ["sbx-1"])
        self.assertEqual(fake.live, [])

    def test_successful_create_reaps_nothing(self) -> None:
        fake = _FakeDaytona()

        _client(fake).create(SandboxConfig(backend="daytona", snapshot_id="snap"))

        self.assertEqual(fake.deleted, [])
        self.assertEqual(len(fake.live), 1)


@unittest.skipUnless(_HAS_DAYTONA, "Daytona SDK not installed")
class DaytonaCreateTimeoutOptionTests(unittest.TestCase):
    def test_create_timeout_is_forwarded_to_the_sdk(self) -> None:
        fake = _FakeDaytona()
        config = SandboxConfig(backend="daytona", snapshot_id="snap", backend_options={"create_timeout": 300})

        _client(fake).create(config)

        self.assertEqual(fake.created[0][1], {"timeout": 300.0})
        # It targets Daytona.create(), so it must not leak into the params
        # object, where pydantic would silently swallow it.
        self.assertFalse(hasattr(fake.created[0][0], "create_timeout"))

    def test_create_timeout_defaults_to_the_sdk_default(self) -> None:
        fake = _FakeDaytona()

        _client(fake).create(SandboxConfig(backend="daytona", snapshot_id="snap"))

        self.assertEqual(fake.created[0][1], {})

    def test_zero_create_timeout_is_rejected_because_it_means_no_timeout(self) -> None:
        fake = _FakeDaytona()
        config = SandboxConfig(backend="daytona", snapshot_id="snap", backend_options={"create_timeout": 0})

        with self.assertRaises(SandboxConfigurationError) as ctx:
            _client(fake).create(config)

        self.assertIn("no timeout at all", str(ctx.exception))
        self.assertEqual(fake.created, [])


if __name__ == "__main__":
    unittest.main()
