"""boxd backend tests.

Every boxd SDK call in the unit tests is faked -- they never touch the
network and never create, start, stop or delete a real machine.  The
behaviour they pin was observed against production boxd.  ``BoxdLiveTests``
at the bottom repeats the boxd-specific checks on real machines when
``BOXD_API_KEY`` is set; ``test_regression.py`` runs the shared suite.
"""

from __future__ import annotations

import hashlib
import inspect
import os
import shlex
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bespokelabs.sandbox import NetworkPolicy, Sandbox, SandboxClient
from bespokelabs.sandbox.exceptions import (
    ErrorOutcome,
    FeatureNotSupportedError,
    SandboxAuthenticationError,
    SandboxConfigurationError,
    SandboxConnectionError,
    SandboxCreationError,
    SandboxExecutionError,
    SandboxNotFoundError,
    SandboxTimeoutError,
)
from bespokelabs.sandbox.types import SandboxConfig, SandboxSessionState

try:
    import boxd  # noqa: F401

    _HAS_BOXD = True
except ImportError:  # pragma: no cover - exercised only without the extra
    _HAS_BOXD = False

if _HAS_BOXD:
    from boxd import (
        APIConnectionError,
        AuthenticationError,
        Boxd,
        ConflictError,
        FsEntry,
        MachineAgentOutdatedError,
        MachineFileNotFoundError,
        MachineIsADirectoryError,
        MachinePermissionError,
        NotFoundError,
        PermissionDeniedError,
        RateLimitError,
    )
    from boxd.errors import APIStatusError
    from boxd.resources.machines import Machines

    from bespokelabs.sandbox.backends.boxd import (
        _CREATE_TOKEN_LABEL,
        _DENY_ALL_ENTRY,
        BoxdClient,
        _classify_boxd_error,
    )


def _inner(script: str) -> str:
    """Return the caller's command from inside the keepalive wrapper."""
    quoted = script.split("bash -c ", 1)[1].rsplit("; __bsb_rc=$?", 1)[0]
    return shlex.split(quoted)[0]


def _result(stdout="", stderr="", exit_code=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, exit_code=exit_code)


class _FakeFs:
    """``boxd.machines.fs``, failing with the SDK's own exception classes."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.writes: list[str] = []
        # Path prefixes the boxd user may not touch, and paths that are
        # directories.
        self.denied: set[str] = set()
        self.dirs: set[str] = set()
        self.listing: list = []
        # A machine started before the file API existed refuses every call.
        self.outdated = False

    def _check(self, machine_id: str, path: str) -> None:
        if self.outdated:
            raise MachineAgentOutdatedError(
                f"{machine_id} was started before the filesystem API "
                "existed; reboot it to update it",
                code="failed_precondition",
                reason="agent_outdated",
            )
        if any(path.startswith(d) for d in self.denied):
            raise MachinePermissionError(
                f"{path}: Permission denied",
                code="permission_denied",
                reason="permission_denied",
                filename=path,
            )

    def write(self, machine_id: str, path: str, data: bytes):
        self._check(machine_id, path)
        self.writes.append(path)
        self.store[path] = bytes(data)
        return SimpleNamespace(path=path, size=len(data))

    def read(self, machine_id: str, path: str) -> bytes:
        self._check(machine_id, path)
        if path in self.dirs:
            raise MachineIsADirectoryError(
                f"{path}: Is a directory",
                code="failed_precondition",
                reason="is_a_directory",
                filename=path,
            )
        if path not in self.store:
            raise MachineFileNotFoundError(
                f"{path}: No such file or directory",
                code="not_found",
                reason="not_found",
                filename=path,
            )
        return self.store[path]

    def list(self, machine_id: str, path: str) -> list:
        self._check(machine_id, path)
        return self.listing


def _entry(name: str, kind: str, target: str | None, size: int):
    """Return a listed entry; ``target`` is what it resolves to."""
    return FsEntry(
        name=name,
        path=f"/workspace/{name}",
        type=kind,
        target_type=target,
        size=size,
        mode=0o644,
        mtime_ns=0,
        uid=1000,
        gid=1000,
    )


class _FakeProxies:

    def __init__(self) -> None:
        self.routes: dict[str, int] = {}
        self.created: list[tuple[str, int]] = []

    def create(self, machine: str, name: str, port: int):
        self.created.append((name, port))
        self.routes[name] = port

    def delete(self, machine: str, name: str) -> None:
        self.routes.pop(name, None)

    def list(self, machine: str):
        return [
            SimpleNamespace(
                name=name, port=port, domain=f"{name}.sb-test.boxd.sh"
            )
            for name, port in self.routes.items()
        ]


class _FakeMachines:
    """Stand-in for ``boxd.machines`` with the surface the backend uses."""

    def __init__(self) -> None:
        self.fs = _FakeFs()
        self.proxies = _FakeProxies()
        self.created: list[dict] = []
        self.deleted: list[str] = []
        self.started: list[str] = []
        self.resumed: list[str] = []
        self.woken: list[str] = []
        self.ready: list[str] = []
        self.egress: list[tuple[str, list[str]]] = []
        self.hibernate: list[tuple[str, int]] = []
        self.execs: list[str] = []
        self.calls: list[str] = []
        self.exec_handler = lambda script: _result()
        self.create_error: BaseException | None = None
        self.ready_error: BaseException | None = None
        self.delete_error: BaseException | None = None
        self.status = "running"
        self.stopped = False

    # Same keyword surface as boxd.resources.machines.Machines.create, so
    # the backend's option check sees the real parameter names.
    def create(
        self,
        name=None,
        *,
        image=None,
        from_snapshot=None,
        org=None,
        shared=False,
        env=None,
        cmd=None,
        restart_policy=None,
        networks=None,
        isolated=False,
        labels=None,
        vcpu=None,
        memory=None,
        disk=None,
        auto_suspend_timeout=None,
        auto_destroy_timeout=None,
        idle_ignore_outbound=None,
        delete_after=None,
        delete_after_cron=None,
        ssh=None,
        proxies=None,
        volumes=None,
    ):
        kwargs = {
            k: v
            for k, v in locals().items()
            if k not in ("self", "name") and v is not None and v is not False
        }
        self.created.append({"name": name, **kwargs})
        self.calls.append("create")
        if self.create_error is not None:
            raise self.create_error
        return self._machine(f"vm-{len(self.created)}", name)

    def _machine(self, machine_id: str, name: str | None):
        return SimpleNamespace(
            id=machine_id,
            name=name or "sb-test",
            status="stopped" if self.stopped else self.status,
            access=SimpleNamespace(domain="boxd.sh"),
        )

    def get(self, machine_id: str):
        if machine_id in self.deleted:
            raise NotFoundError("VM not found", code="not_found")
        return self._machine(machine_id, "sb-test")

    def list(self, *, org=None, labels=None):
        token = (labels or {}).get(_CREATE_TOKEN_LABEL)
        return [
            SimpleNamespace(id=f"vm-{i + 1}")
            for i, c in enumerate(self.created)
            if c["labels"].get(_CREATE_TOKEN_LABEL) == token
        ]

    def wait_until_ready(self, machine_id: str, *, timeout: float = 90.0):
        self.calls.append("ready")
        self.ready.append(machine_id)
        if self.ready_error is not None:
            raise self.ready_error

    def delete(self, machine_id: str) -> None:
        self.calls.append("delete")
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append(machine_id)

    def start(self, machine_id: str) -> None:
        self.started.append(machine_id)
        self.stopped = False

    def resume(self, machine_id: str) -> None:
        self.resumed.append(machine_id)

    def wake(self, machine_id: str) -> None:
        self.woken.append(machine_id)

    def set_egress_allow(self, machine_id: str, entries):
        self.calls.append("egress")
        self.egress.append((machine_id, list(entries)))
        return list(entries)

    def set_auto_hibernate_timeout(self, machine_id: str, seconds: int):
        self.hibernate.append((machine_id, seconds))

    def exec(self, machine_id: str, command, *, encoding="utf-8", **kwargs):
        self.calls.append("exec")
        if self.stopped:
            raise ConflictError(
                f"VM {machine_id} is stopped, so it won't be resumed "
                "automatically.",
                code="failed_precondition",
            )
        self.assert_bash(command)
        script = command[2]
        self.execs.append(script)
        result = self.exec_handler(script)
        if encoding == "buffer" and isinstance(result.stdout, str):
            result = _result(
                result.stdout.encode(), result.stderr.encode(), result.exit_code
            )
        return result

    @staticmethod
    def assert_bash(command) -> None:
        assert list(command[:2]) == ["bash", "-c"], command


class _FakeSnapshots:

    def __init__(self) -> None:
        self.statuses: list[str] = ["pending", "ready"]
        self.created: list[tuple[str, str]] = []

    def create(self, machine_id: str, name: str):
        self.created.append((machine_id, name))
        return SimpleNamespace(name=name, status="pending")

    def get(self, name: str, *, org=None):
        status = (
            self.statuses.pop(0)
            if len(self.statuses) > 1
            else (self.statuses[0])
        )
        return SimpleNamespace(name=name, status=status, created_at=None)


class _FakeBoxd:

    def __init__(self) -> None:
        self.machines = _FakeMachines()
        self.snapshots = _FakeSnapshots()


def _client(fake: _FakeBoxd | None = None):
    client = BoxdClient()
    client._client = fake or _FakeBoxd()
    return client, client._client


@unittest.skipUnless(_HAS_BOXD, "boxd SDK not installed")
class BoxdCreateTests(unittest.TestCase):

    def _create(self, **config) -> tuple[object, _FakeBoxd]:
        client, fake = _client()
        session = client.create(SandboxConfig(backend="boxd", **config))
        return session, fake

    def test_every_machine_is_isolated(self) -> None:
        _, fake = self._create()
        self.assertTrue(fake.machines.created[0]["isolated"])

    def test_isolation_cannot_be_turned_off(self) -> None:
        client, fake = _client()
        with self.assertRaises(SandboxConfigurationError):
            client.create(
                SandboxConfig(
                    backend="boxd", backend_options={"isolated": False}
                )
            )
        self.assertEqual(fake.machines.created, [])

    def test_resources_round_up_to_a_boxd_size(self) -> None:
        cases = [
            ({}, (1, "4G")),
            ({"cpu": 2, "memory_mb": 4096}, (2, "8G")),
            ({"cpu": 1, "memory_mb": 9000}, (4, "16G")),
            ({"cpu": 8, "memory_mb": 32768}, (8, "32G")),
        ]
        for config, (vcpu, memory) in cases:
            with self.subTest(config=config):
                _, fake = self._create(**config)
                created = fake.machines.created[0]
                self.assertEqual(
                    (created["vcpu"], created["memory"]), (vcpu, memory)
                )

    def test_a_size_boxd_does_not_offer_is_refused_before_create(self) -> None:
        client, fake = _client()
        with self.assertRaises(SandboxConfigurationError):
            client.create(SandboxConfig(backend="boxd", cpu=16))
        self.assertEqual(fake.machines.created, [])

    def test_unsupported_requests_create_nothing(self) -> None:
        cases = [
            ({"gpu": "L4"}, FeatureNotSupportedError),
            ({"image": "python:3.12-slim"}, FeatureNotSupportedError),
            ({"disk_mb": 200 * 1024}, SandboxConfigurationError),
            ({"env_vars": {"BAD-NAME": "x"}}, SandboxConfigurationError),
            (
                {"backend_options": {"no_such_option": 1}},
                SandboxConfigurationError,
            ),
        ]
        for config, error in cases:
            with self.subTest(config=config):
                client, fake = _client()
                with self.assertRaises(error):
                    client.create(SandboxConfig(backend="boxd", **config))
                self.assertEqual(fake.machines.created, [])

    def test_snapshot_restore_keeps_the_snapshot_size(self) -> None:
        _, fake = self._create(snapshot_id="golden", cpu=4)
        created = fake.machines.created[0]
        self.assertEqual(created["from_snapshot"], "golden")
        self.assertNotIn("vcpu", created)
        self.assertNotIn("memory", created)

    def test_only_an_explicit_timeout_becomes_delete_after(self) -> None:
        _, fake = self._create(timeout_secs=600)
        self.assertNotIn("delete_after", fake.machines.created[0])

        _, fake = self._create(timeout_secs=40, timeout_secs_explicit=True)
        self.assertEqual(fake.machines.created[0]["delete_after"], 40)

    def test_standby_is_off_so_jobs_between_commands_keep_running(self) -> None:
        _, fake = self._create()
        self.assertEqual(fake.machines.created[0]["auto_suspend_timeout"], 0)

    def test_create_token_label_survives_caller_labels(self) -> None:
        _, fake = self._create(
            backend_options={
                "labels": {"team": "rl", _CREATE_TOKEN_LABEL: "mine"}
            }
        )
        labels = fake.machines.created[0]["labels"]
        self.assertEqual(labels["team"], "rl")
        self.assertNotEqual(labels[_CREATE_TOKEN_LABEL], "mine")
        self.assertEqual(len(labels[_CREATE_TOKEN_LABEL]), 32)

    def test_name_org_and_native_options(self) -> None:
        _, fake = self._create(
            backend_options={
                "name": "rl-rollout-7",
                "org": "acme",
                "auto_destroy_timeout": 3600,
            }
        )
        created = fake.machines.created[0]
        self.assertEqual(created["name"], "rl-rollout-7")
        self.assertEqual(created["org"], "acme")
        self.assertEqual(created["auto_destroy_timeout"], 3600)

    def test_default_name_is_prefixed_and_unique(self) -> None:
        _, fake = self._create(backend_options={"name_prefix": "curator"})
        _, fake2 = self._create(backend_options={"name_prefix": "curator"})
        a = fake.machines.created[0]["name"]
        b = fake2.machines.created[0]["name"]
        self.assertTrue(a.startswith("curator-"))
        self.assertNotEqual(a, b)

    @mock.patch("bespokelabs.sandbox.backends.boxd.time.sleep")
    def test_waits_for_ready_before_the_first_exec(self, _: mock.Mock) -> None:
        _, fake = self._create(network=NetworkPolicy(mode="none"))
        calls = fake.machines.calls
        self.assertLess(calls.index("ready"), calls.index("egress"))
        self.assertLess(calls.index("egress"), calls.index("exec"))

    def test_workspace_and_workdir_are_created_for_the_machine_user(
        self,
    ) -> None:
        _, fake = self._create(workdir="/srv/task")
        script = fake.machines.execs[0]
        self.assertIn("[ -d /workspace ] || sudo -n install -d", script)
        self.assertIn("[ -d /srv/task ] || sudo -n install -d", script)

    def test_auto_hibernate_option_is_applied_after_create(self) -> None:
        _, fake = self._create(backend_options={"auto_hibernate_timeout": 0})
        self.assertEqual(fake.machines.hibernate, [("vm-1", 0)])
        self.assertNotIn("auto_hibernate_timeout", fake.machines.created[0])

    def test_adapter_kwargs_are_accepted_by_the_real_sdk(self) -> None:
        _, fake = self._create(
            snapshot_id=None, timeout_secs=60, timeout_secs_explicit=True
        )
        kwargs = dict(fake.machines.created[0])
        name = kwargs.pop("name")
        inspect.signature(Machines.create).bind(None, name, **kwargs)


@unittest.skipUnless(_HAS_BOXD, "boxd SDK not installed")
class BoxdCreateCleanupTests(unittest.TestCase):

    def test_failure_after_create_deletes_the_machine(self) -> None:
        client, fake = _client()
        fake.machines.ready_error = TimeoutError("not running within 120s")
        with self.assertRaises(SandboxTimeoutError) as ctx:
            client.create(SandboxConfig(backend="boxd"))
        self.assertEqual(fake.machines.deleted, ["vm-1"])
        self.assertEqual(ctx.exception.context["cleanup_status"], "deleted")
        self.assertEqual(ctx.exception.outcome, ErrorOutcome.FAILED)

    def test_failed_create_call_reaps_by_label(self) -> None:
        client, fake = _client()
        fake.machines.create_error = APIConnectionError("reset by peer")
        with self.assertRaises(SandboxConnectionError) as ctx:
            client.create(SandboxConfig(backend="boxd"))
        self.assertEqual(fake.machines.deleted, ["vm-1"])
        self.assertEqual(ctx.exception.context["cleanup_status"], "deleted")

    def test_unknown_cleanup_marks_the_outcome_unknown(self) -> None:
        client, fake = _client()
        fake.machines.ready_error = TimeoutError("slow")
        fake.machines.delete_error = APIConnectionError("down")
        with self.assertRaises(SandboxTimeoutError) as ctx:
            client.create(SandboxConfig(backend="boxd"))
        self.assertEqual(ctx.exception.outcome, ErrorOutcome.UNKNOWN)
        self.assertFalse(ctx.exception.retryable)

    def test_keyboard_interrupt_still_reaps(self) -> None:
        client, fake = _client()
        fake.machines.ready_error = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            client.create(SandboxConfig(backend="boxd"))
        self.assertEqual(fake.machines.deleted, ["vm-1"])

    def test_error_classes(self) -> None:
        cases = [
            (AuthenticationError("bad key"), SandboxAuthenticationError),
            (RateLimitError("quota"), SandboxCreationError),
            (APIStatusError("boom"), SandboxCreationError),
        ]
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                client, fake = _client()
                fake.machines.create_error = error
                with self.assertRaises(expected) as ctx:
                    client.create(SandboxConfig(backend="boxd"))
                self.assertEqual(ctx.exception.backend, "boxd")
                self.assertEqual(ctx.exception.op, "create")
        client, fake = _client()
        fake.machines.create_error = RateLimitError("quota")
        with self.assertRaises(SandboxCreationError) as ctx:
            client.create(SandboxConfig(backend="boxd"))
        self.assertTrue(ctx.exception.retryable)

    def test_transient_credential_failures_are_not_auth_errors(self) -> None:
        # Since boxd 0.2.20, AuthenticationError means only missing or
        # refused credentials.
        unreachable = _classify_boxd_error(
            APIConnectionError("could not reach the token exchange"),
            op="create",
        )
        self.assertIsInstance(unreachable, SandboxConnectionError)
        self.assertTrue(unreachable.retryable)
        server_error = _classify_boxd_error(
            APIStatusError("exchange failed", http_status=503), op="create"
        )
        self.assertIsInstance(server_error, SandboxCreationError)
        self.assertTrue(server_error.retryable)
        refused = _classify_boxd_error(
            AuthenticationError("invalid key", reason="invalid_api_key"),
            op="create",
        )
        self.assertIsInstance(refused, SandboxAuthenticationError)
        self.assertEqual(refused.context["provider_reason"], "invalid_api_key")

    def test_a_file_permission_error_is_not_an_auth_error(self) -> None:
        denied = MachinePermissionError(
            "/etc/x: Permission denied",
            code="permission_denied",
            reason="permission_denied",
            filename="/etc/x",
        )
        self.assertIsInstance(
            _classify_boxd_error(denied, op="read_file"), SandboxExecutionError
        )
        self.assertIsInstance(
            _classify_boxd_error(PermissionDeniedError("no"), op="read_file"),
            SandboxAuthenticationError,
        )


@unittest.skipUnless(_HAS_BOXD, "boxd SDK not installed")
class BoxdNetworkTests(unittest.TestCase):

    def setUp(self) -> None:
        patcher = mock.patch("bespokelabs.sandbox.backends.boxd.time.sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def _egress(self, **config) -> list[str]:
        client, fake = _client()
        client.create(SandboxConfig(backend="boxd", **config))
        return fake.machines.egress[0][1] if fake.machines.egress else []

    def test_public_sets_no_allowlist(self) -> None:
        self.assertEqual(self._egress(), [])
        self.assertEqual(self._egress(network=NetworkPolicy()), [])

    def test_none_and_legacy_flag_allow_nothing(self) -> None:
        self.assertEqual(
            self._egress(network=NetworkPolicy(mode="none")),
            [_DENY_ALL_ENTRY],
        )
        self.assertEqual(self._egress(allow_internet=False), [_DENY_ALL_ENTRY])
        self.assertTrue(_DENY_ALL_ENTRY.endswith(".invalid"))

    def test_allowlist_takes_domains_and_public_cidrs(self) -> None:
        policy = NetworkPolicy(
            mode="allowlist", domains=["pypi.org"], cidrs=["1.1.1.0/24"]
        )
        self.assertEqual(
            self._egress(network=policy), ["pypi.org", "1.1.1.0/24"]
        )

    def test_private_and_ipv6_cidrs_are_refused(self) -> None:
        for cidr in ("10.0.0.0/8", "2001:db8::/32"):
            with self.subTest(cidr=cidr):
                client, fake = _client()
                with self.assertRaises(SandboxConfigurationError):
                    client.create(
                        SandboxConfig(
                            backend="boxd",
                            network=NetworkPolicy(
                                mode="allowlist", cidrs=[cidr]
                            ),
                        )
                    )
                self.assertEqual(fake.machines.created, [])

    def test_policies_are_given_time_to_apply_before_returning(self) -> None:
        # boxd enforces a new allow-list within a second of the call.
        client, _ = _client()
        client.create(SandboxConfig(backend="boxd"))
        self.sleep.assert_not_called()

        session = client.create(
            SandboxConfig(backend="boxd", network=NetworkPolicy(mode="none"))
        )
        self.sleep.assert_called_once()
        self.assertLessEqual(self.sleep.call_args.args[0], 1.0)

        self.sleep.reset_mock()
        session.set_network(NetworkPolicy())
        self.sleep.assert_called_once()

    def test_set_network_replaces_the_allowlist(self) -> None:
        client, fake = _client()
        session = client.create(SandboxConfig(backend="boxd"))
        session.set_network(NetworkPolicy(mode="none"))
        session.set_network(NetworkPolicy())
        self.assertEqual(
            [entries for _, entries in fake.machines.egress],
            [[_DENY_ALL_ENTRY], []],
        )

    def test_sandbox_accepts_every_mode(self) -> None:
        client, _ = _client()
        for mode in ("none", "allowlist"):
            policy = (
                NetworkPolicy(mode=mode, domains=["pypi.org"])
                if mode == "allowlist"
                else NetworkPolicy(mode=mode)
            )
            with self.subTest(mode=mode):
                Sandbox("boxd", network=policy, _backend_client=client)


@unittest.skipUnless(_HAS_BOXD, "boxd SDK not installed")
class BoxdSessionTests(unittest.TestCase):

    def setUp(self) -> None:
        self.client, self.fake = _client()
        self.session = self.client.create(
            SandboxConfig(
                backend="boxd",
                env_vars={"API_TOKEN": "s3cr$t value"},
                workdir="/workspace",
            )
        )
        self.machines = self.fake.machines
        self.machines.execs.clear()

    def test_env_vars_are_exported_into_every_command_not_stored(self) -> None:
        self.session.execute_command("echo $API_TOKEN")
        script = self.machines.execs[-1]
        self.assertIn(
            f"export API_TOKEN={shlex.quote('s3cr$t value')};", script
        )
        self.assertNotIn(
            "s3cr$t",
            "".join(str(v) for v in self.machines.fs.store.values()),
        )
        self.assertEqual(self.machines.fs.writes, [])

    def test_commands_run_in_workdir_and_stop_if_it_is_missing(self) -> None:
        self.session.execute_command("pwd")
        script = self.machines.execs[-1]
        self.assertIn("cd -- /workspace || exit 1; ", script)
        self.assertEqual(_inner(script), "pwd")

    def test_args_are_quoted(self) -> None:
        self.session.execute_command("printf", ["%s\n", "a b", "$HOME"])
        self.assertEqual(
            _inner(self.machines.execs[-1]), "printf '%s\n' 'a b' '$HOME'"
        )

    def test_execute_code_uses_the_named_interpreter(self) -> None:
        self.session.execute_code("print(1)", "python3")
        self.assertEqual(
            _inner(self.machines.execs[-1]), "python3 -c 'print(1)'"
        )

    def test_quiet_commands_get_a_keepalive_that_is_stripped(self) -> None:
        # boxd closes an exec after 30 minutes without output.
        self.machines.exec_handler = lambda s: _result("out", "\x00e\x00rr", 3)
        result = self.session.execute_command("exit 3")
        script = self.machines.execs[-1]
        self.assertIn("printf '\\0' >&2", script)
        self.assertIn("sleep 300 >/dev/null 2>&1 </dev/null", script)
        self.assertTrue(script.endswith("exit $__bsb_rc"))
        self.assertEqual(
            (result.stdout, result.stderr, result.exit_code), ("out", "err", 3)
        )

    def test_exit_code_and_streams_are_forwarded(self) -> None:
        self.machines.exec_handler = lambda s: _result("out", "err", 42)
        result = self.session.execute_command("exit 42")
        self.assertEqual(
            (result.stdout, result.stderr, result.exit_code),
            ("out", "err", 42),
        )

    def test_a_stopped_machine_is_started_and_the_call_retried(self) -> None:
        self.machines.stopped = True
        result = self.session.execute_command("true")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(self.machines.started, ["vm-1"])

    def test_files_go_through_the_file_api(self) -> None:
        self.session.write_file("/tmp/x.bin", b"\x00\x01")
        self.assertEqual(self.session.read_file("/tmp/x.bin"), b"\x00\x01")
        self.assertEqual(self.machines.fs.writes, ["/tmp/x.bin"])
        self.assertEqual(self.machines.execs, [])

    def test_empty_files_are_written_like_any_other(self) -> None:
        self.session.write_file("/workspace/input.txt", "")
        self.assertEqual(self.session.read_file("/workspace/input.txt"), b"")
        self.assertEqual(self.machines.execs, [])

    def test_root_owned_paths_are_written_through_sudo(self) -> None:
        self.machines.fs.denied.add("/opt")
        self.session.write_file("/opt/app/config.json", "{}")
        staged = self.machines.fs.writes[-1]
        self.assertTrue(staged.startswith("/tmp/.bespokelabs-sandbox-"))
        self.assertEqual(self.machines.fs.store[staged], b"{}")
        # cp writes into an existing file, keeping its mode and owner.
        command = self.machines.execs[-1]
        self.assertIn("sudo -n mkdir -p -- /opt/app && ", command)
        self.assertIn(
            f"sudo -n cp -T -- {staged} /opt/app/config.json;", command
        )
        self.assertIn(f"rm -f -- {staged}", command)

    def test_unreadable_files_are_read_through_sudo(self) -> None:
        self.machines.fs.denied.add("/etc/shadow")
        self.machines.exec_handler = lambda s: _result("root-only")
        self.assertEqual(self.session.read_file("/etc/shadow"), b"root-only")
        self.assertIn("sudo -n cat -- /etc/shadow", self.machines.execs[-1])

    def test_missing_file_is_an_execution_error_without_sudo(self) -> None:
        with self.assertRaises(SandboxExecutionError) as ctx:
            self.session.read_file("/nope")
        self.assertIsInstance(ctx.exception.__cause__, FileNotFoundError)
        self.assertEqual(ctx.exception.context["provider_reason"], "not_found")
        self.assertEqual(self.machines.execs, [])

    def test_a_path_error_naming_stopped_does_not_start_the_machine(
        self,
    ) -> None:
        # Reading a directory is a ConflictError, as a stopped machine is.
        self.machines.fs.dirs.add("/srv/stopped")
        with self.assertRaises(SandboxExecutionError):
            self.session.read_file("/srv/stopped")
        self.assertEqual(self.machines.started, [])

    def test_a_machine_older_than_the_file_api_is_told_to_reboot(
        self,
    ) -> None:
        self.machines.fs.outdated = True
        with self.assertRaises(SandboxExecutionError) as ctx:
            self.session.write_file("/workspace/x.txt", "x")
        self.assertIn("reboot it", str(ctx.exception))
        self.assertEqual(
            ctx.exception.context["provider_reason"], "agent_outdated"
        )
        self.assertEqual(self.machines.started, [])

    def test_list_files_returns_full_sorted_paths(self) -> None:
        self.machines.fs.listing = [
            _entry("program.py", "file", "file", 12),
            _entry("out", "directory", "directory", 4096),
            _entry("data", "symlink", "directory", 4096),
            _entry("gone", "symlink", None, 4),
        ]
        files = self.session.list_files("/workspace/")
        # Entries are described by what they resolve to.
        self.assertEqual(
            [(f.path, f.is_dir, f.size) for f in files],
            [
                ("/workspace/data", True, None),
                ("/workspace/gone", False, None),
                ("/workspace/out", True, None),
                ("/workspace/program.py", False, 12),
            ],
        )

    def test_an_unreadable_directory_is_listed_with_sudo_find(self) -> None:
        self.machines.fs.denied.add("/data")
        self.machines.exec_handler = lambda s: _result(
            "f\t3\t/data/a\0d\t4096\t/data/b\0"
        )
        files = self.session.list_files("/data")
        self.assertIn("sudo -n find /data", self.machines.execs[-1])
        self.assertEqual(
            [(f.path, f.is_dir, f.size) for f in files],
            [("/data/a", False, 3), ("/data/b", True, None)],
        )

    def test_upload_and_download_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp, "in.bin")
            src.write_bytes(b"payload")
            self.session.upload_file(str(src), "/tmp/in.bin")
            dst = Path(tmp, "out.bin")
            self.session.download_file("/tmp/in.bin", str(dst))
            self.assertEqual(dst.read_bytes(), b"payload")

    @mock.patch(
        "bespokelabs.sandbox.backends.boxd._wait_for_tls", return_value=True
    )
    def test_expose_port_returns_a_public_url_and_reuses_the_route(
        self, wait: mock.Mock
    ) -> None:
        exposed = self.session.expose_port(8000)
        # A new route refuses TLS until its certificate is issued.
        wait.assert_called_with("port-8000.sb-test.boxd.sh", mock.ANY)
        self.assertEqual(exposed.url, "https://port-8000.sb-test.boxd.sh")
        self.assertEqual(exposed.headers, {})
        self.session.expose_port(8000)
        self.assertEqual(self.machines.proxies.created, [("port-8000", 8000)])

    @mock.patch("bespokelabs.sandbox.backends.boxd.time.sleep")
    def test_snapshot_waits_until_ready(self, sleep: mock.Mock) -> None:
        info = self.session.snapshot()
        sleep.assert_called_once()
        self.assertEqual(info.backend, "boxd")
        self.assertTrue(info.snapshot_id.startswith("bespokelabs-"))
        self.assertEqual(self.fake.snapshots.statuses, ["ready"])

    def test_failed_snapshot_raises(self) -> None:
        self.fake.snapshots.statuses = ["failed"]
        with self.assertRaises(SandboxExecutionError):
            self.session.snapshot()

    def test_destroy_is_idempotent_and_tolerates_a_gone_machine(self) -> None:
        self.machines.delete_error = NotFoundError("VM not found")
        self.session.destroy()
        self.session.destroy()
        self.assertIsNone(self.session.provider_resource_id)
        with self.assertRaises(SandboxNotFoundError):
            self.session.execute_command("true")

    def test_destroy_surfaces_real_failures(self) -> None:
        self.machines.delete_error = APIConnectionError("down")
        with self.assertRaises(SandboxConnectionError):
            self.session.destroy()


@unittest.skipUnless(_HAS_BOXD, "boxd SDK not installed")
class BoxdResumeTests(unittest.TestCase):

    def test_session_state_carries_what_resume_needs(self) -> None:
        client, _ = _client()
        session = client.create(
            SandboxConfig(
                backend="boxd",
                env_vars={"A": "1"},
                workdir="/srv",
                backend_options={"org": "acme"},
            )
        )
        self.assertEqual(
            session.session_state(),
            {
                "machine_id": "vm-1",
                "workdir": "/srv",
                "env_vars": {"A": "1"},
                "org": "acme",
            },
        )

    def test_resume_wakes_a_sleeping_machine(self) -> None:
        cases = {
            "suspended": "resumed",
            "hibernated": "woken",
            "stopped": "started",
        }
        for status, record in cases.items():
            with self.subTest(status=status):
                client, fake = _client()
                fake.machines.status = status
                session = client.resume({"machine_id": "vm-9"})
                self.assertEqual(getattr(fake.machines, record), ["vm-9"])
                self.assertEqual(fake.machines.ready, ["vm-9"])
                self.assertEqual(session.provider_resource_id, "vm-9")

    def test_resume_of_a_running_machine_does_not_touch_it(self) -> None:
        client, fake = _client()
        session = client.resume(
            {"machine_id": "vm-9", "env_vars": {"A": "1"}, "workdir": "/srv"}
        )
        session.execute_command("env")
        self.assertEqual(fake.machines.ready, [])
        self.assertIn("export A=1; cd -- /srv", fake.machines.execs[-1])

    def test_resume_errors(self) -> None:
        client, fake = _client()
        with self.assertRaises(SandboxConfigurationError):
            client.resume({})
        fake.machines.deleted.append("vm-gone")
        with self.assertRaises(SandboxNotFoundError):
            client.resume({"machine_id": "vm-gone"})

    def test_sandbox_resume_roundtrip(self) -> None:
        client, _ = _client()
        sb = Sandbox("boxd", _backend_client=client)
        state = SandboxSessionState.from_json(sb.session_state().to_json())
        sb2 = Sandbox._resume_with("boxd", client, state)
        self.assertEqual(sb2.provider_resource_id, sb.provider_resource_id)


@unittest.skipUnless(_HAS_BOXD, "boxd SDK not installed")
class BoxdSandboxWiringTests(unittest.TestCase):

    def test_registered_and_buildable(self) -> None:
        self.assertEqual(SandboxClient("boxd").backend_name, "boxd")

    def test_dockerfile_is_refused_before_create(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dockerfile = Path(tmp, "Dockerfile")
            dockerfile.write_text("FROM python:3.12\n")
            with self.assertRaises(FeatureNotSupportedError):
                Sandbox("boxd", dockerfile=str(dockerfile))

    def test_presets_run_their_boxd_setup_instead_of_an_image(self) -> None:
        client, fake = _client()
        sb = Sandbox("boxd", preset="claude-code", _backend_client=client)
        self.assertIsNone(sb._config.image)
        self.assertEqual(_inner(fake.machines.execs[-1]), "claude --version")


def _live() -> bool:
    return _HAS_BOXD and bool(
        os.environ.get("BOXD_API_KEY") or os.environ.get("BOXD_TOKEN")
    )


@unittest.skipUnless(_live(), "boxd SDK or BOXD_API_KEY not available")
class BoxdLiveTests(unittest.TestCase):
    """boxd-specific behaviour on real machines; each test deletes its own."""

    def _sandbox(self, **kwargs) -> Sandbox:
        sb = Sandbox("boxd", **kwargs)
        self.addCleanup(sb.destroy)
        return sb

    def _status(self, sb: Sandbox, url: str) -> str:
        return sb.execute_command(
            f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 8 {url}"
        ).stdout

    def test_curator_style_workspace_run(self) -> None:
        sb = self._sandbox()
        sb.write_file("/workspace/program.py", "print(input()[::-1])")
        sb.write_file("/workspace/input.txt", "")
        sb.write_file("/workspace/input.txt", "olleh")
        result = sb.execute_command(
            "bash", ["-c", "cd /workspace && python3 program.py < input.txt"]
        )
        self.assertEqual((result.exit_code, result.stdout), (0, "hello\n"))
        names = [f.path for f in sb.list_files("/workspace")]
        self.assertEqual(
            names, ["/workspace/input.txt", "/workspace/program.py"]
        )

    def test_env_vars_reach_commands_and_never_the_disk(self) -> None:
        sb = self._sandbox(env_vars={"TOKEN": "a b$c"})
        self.assertEqual(sb.execute_command('echo "$TOKEN"').stdout, "a b$c\n")
        # -l lists matching files; special files make grep exit 2 anyway.
        grep = sb.execute_command(
            "sudo grep -rlsF -- 'a b$c' /home /workspace /etc /tmp; true"
        )
        self.assertEqual(grep.stdout, "")

    def test_exit_and_exec_inside_the_keepalive_wrapper(self) -> None:
        sb = self._sandbox()
        result = sb.execute_command("echo e >&2; exec sh -c 'exit 7'")
        self.assertEqual((result.exit_code, result.stderr), (7, "e\n"))
        result = sb.execute_command("echo o; exit 5")
        self.assertEqual((result.exit_code, result.stdout), (5, "o\n"))

    def test_large_file_roundtrip(self) -> None:
        sb = self._sandbox()
        data = os.urandom(5 * 1024 * 1024)
        sb.write_file("/tmp/big.bin", data)
        self.assertEqual(
            hashlib.sha256(sb.read_file("/tmp/big.bin")).hexdigest(),
            hashlib.sha256(data).hexdigest(),
        )

    def test_root_owned_paths(self) -> None:
        sb = self._sandbox()
        sb.write_file("/opt/app/config.json", "{}")
        self.assertEqual(sb.read_file("/opt/app/config.json"), b"{}")
        self.assertTrue(sb.read_file("/etc/shadow"))
        sb.execute_command("sudo install -m 600 /dev/null /etc/bsp-test")
        sb.write_file("/etc/bsp-test", "b")
        self.assertEqual(
            sb.execute_command("sudo stat -c '%a %U' /etc/bsp-test").stdout,
            "600 root\n",
        )

    def test_overwrites_keep_the_mode_and_write_through_symlinks(self) -> None:
        sb = self._sandbox()
        sb.write_file("/workspace/run.sh", "#!/bin/sh\necho one\n")
        sb.execute_command(
            "chmod 755 /workspace/run.sh && ln -s run.sh /workspace/link"
        )
        sb.write_file("/workspace/link", "#!/bin/sh\necho two\n")
        result = sb.execute_command(
            "stat -c '%a %F' /workspace/run.sh /workspace/link "
            "&& /workspace/run.sh"
        )
        self.assertEqual(
            result.stdout, "755 regular file\n777 symbolic link\ntwo\n"
        )

    def test_network_policies_are_enforced(self) -> None:
        sb = self._sandbox(network=NetworkPolicy(mode="none"))
        self.assertEqual(self._status(sb, "https://pypi.org/simple/"), "000")
        sb.set_network(NetworkPolicy(mode="allowlist", domains=["pypi.org"]))
        self.assertEqual(self._status(sb, "https://pypi.org/simple/"), "200")
        self.assertEqual(self._status(sb, "https://example.com"), "000")
        sb.set_network(NetworkPolicy())
        self.assertEqual(self._status(sb, "https://example.com"), "200")

    def test_expose_port(self) -> None:
        sb = self._sandbox()
        sb.execute_command(
            "nohup python3 -m http.server 8000 >/dev/null 2>&1 & sleep 1"
        )
        url = sb.expose_port(8000).url
        deadline = time.monotonic() + 60
        while True:
            try:
                with urllib.request.urlopen(url, timeout=10) as response:
                    self.assertEqual(response.status, 200)
                    return
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(2)

    def test_snapshot_restores_files_and_running_processes(self) -> None:
        sb = self._sandbox()
        sb.write_file("/workspace/state.txt", "warm")
        sb.execute_command("nohup sleep 3600 >/dev/null 2>&1 &")
        snap = sb.snapshot()
        self.addCleanup(
            Boxd().snapshots.delete,
            snap.snapshot_id,
            org=os.environ.get("BOXD_ORG"),
        )
        restored = self._sandbox(snapshot_id=snap.snapshot_id)
        self.assertEqual(restored.read_file("/workspace/state.txt"), b"warm")
        self.assertEqual(
            restored.execute_command("pgrep -x sleep").exit_code, 0
        )

    def test_resume_from_serialized_state(self) -> None:
        sb = self._sandbox(env_vars={"MARK": "kept"})
        sb.execute_command("echo hi > /tmp/work.txt")
        state = SandboxSessionState.from_json(sb.session_state().to_json())
        sb2 = SandboxClient("boxd").resume(state)
        self.assertEqual(sb2.read_file("/tmp/work.txt"), b"hi\n")
        self.assertEqual(sb2.execute_command("echo $MARK").stdout, "kept\n")


if __name__ == "__main__":
    unittest.main()
