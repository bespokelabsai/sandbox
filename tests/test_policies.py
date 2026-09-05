"""Phase 4 policy, role, provider-health, and supervision tests."""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from bespokelabs.sandbox.control_plane.cli import (
    _provider_settings_from_environment,
)
from bespokelabs.sandbox.control_plane.errors import (
    ConflictError,
    PolicyDeniedError,
)
from bespokelabs.sandbox.control_plane.reconciliation import ProviderObservation
from bespokelabs.sandbox.control_plane.service import ControlPlane
from bespokelabs.sandbox.control_plane.store import SQLiteStore
from bespokelabs.sandbox.types import SandboxResult

try:
    from fastapi.testclient import TestClient

    from bespokelabs.sandbox.control_plane.api import create_app
except ImportError:  # pragma: no cover - server extra is optional
    TestClient = None
    create_app = None


class MutableClock:

    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class SimulatedProcessCrash(BaseException):
    """Abrupt process loss that normal provider-error handling cannot catch."""


class PolicyRuntime:

    def __init__(self, backend: str, resource_id: str) -> None:
        self.backend_name = backend
        self.provider_resource_id = resource_id
        self.destroyed = False
        self.destroy_calls = 0
        self.crash_on_destroy = False

    def execute_code(
        self, code: str, language: str = "python"
    ) -> SandboxResult:
        return SandboxResult(stdout=f"{language}:{code}")

    def execute_command(
        self, command: str, args: list[str] | None = None
    ) -> SandboxResult:
        return SandboxResult(stdout=" ".join([command, *(args or [])]))

    def estimate_compute_cost(self, elapsed_secs: float) -> float:
        return elapsed_secs / 3600

    def destroy(self) -> None:
        self.destroy_calls += 1
        if self.crash_on_destroy:
            raise SimulatedProcessCrash
        self.destroyed = True


class PolicyFactory:

    def __init__(self, *, block_first: bool = False) -> None:
        self.calls = 0
        self.created: list[PolicyRuntime] = []
        self.block_first = block_first
        self.entered = threading.Event()
        self.release = threading.Event()
        self.lock = threading.Lock()

    def __call__(self, backend: str, **config: object) -> PolicyRuntime:
        del config
        with self.lock:
            self.calls += 1
            call = self.calls
        if self.block_first and call == 1:
            self.entered.set()
            self.release.wait(timeout=3)
        runtime = PolicyRuntime(backend, f"fake-{backend}-{call}")
        self.created.append(runtime)
        return runtime


class FakeReconciler:

    def __init__(self) -> None:
        self.by_org: dict[str, list[ProviderObservation]] = {}

    def list_resources(self, organization_id: str) -> list[ProviderObservation]:
        return self.by_org.get(organization_id, [])


class PolicyStoreTest(unittest.TestCase):

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "phase4.db"
        self.store = SQLiteStore(self.path, key_pepper="phase4")
        self.clock = MutableClock(datetime(2026, 9, 4, 12, tzinfo=UTC))
        self.factory = PolicyFactory()
        self.service = ControlPlane(
            self.store,
            sandbox_factory=self.factory,
            allowed_backends={"daytona", "runpod"},
            now=self.clock,
            supervision_interval_secs=0,
        )
        self.org, self.key = self.service.bootstrap_organization("Policy org")
        self.principal = self.store.authenticate(self.key.secret)

    def tearDown(self) -> None:
        self.service.close()
        self.temp_dir.cleanup()

    def set_policy(self, **overrides: object) -> None:
        values = {
            "max_concurrent_sandboxes": None,
            "hourly_spend_limit_usd": None,
            "daily_spend_limit_usd": None,
            "allowed_backends": None,
            "allowed_gpu_types": None,
            "max_sandbox_lifetime_secs": None,
        }
        values.update(overrides)
        self.service.set_policy(self.principal, **values)

    def test_all_policy_denials_are_pre_provider_and_attributed(self) -> None:
        self.set_policy(
            max_concurrent_sandboxes=3,
            allowed_backends=["daytona"],
            allowed_gpu_types=["A10G"],
            max_sandbox_lifetime_secs=300,
        )
        requests = [
            ("runpod", {"gpu": "A10G", "timeout_secs": 300}),
            ("daytona", {"gpu": "A100", "timeout_secs": 300}),
            ("daytona", {"gpu": "A10G"}),
            ("daytona", {"gpu": "A10G", "timeout_secs": 301}),
        ]
        policies = []
        for backend, config in requests:
            with self.assertRaises(PolicyDeniedError) as raised:
                self.service.create_sandbox(self.principal, backend, config)
            policies.append(raised.exception.policy)

        self.assertEqual(
            policies,
            [
                "allowed_backends",
                "allowed_gpu_types",
                "max_sandbox_lifetime_secs",
                "max_sandbox_lifetime_secs",
            ],
        )
        self.assertEqual(self.factory.calls, 0)
        summary = self.service.policy_summary(self.principal)
        self.assertEqual(len(summary["denials"]), 4)
        self.assertTrue(
            all(
                item["api_key_id"] == self.key.id for item in summary["denials"]
            )
        )

    def test_hourly_and_daily_spend_limits_deny_before_provider(self) -> None:
        sandbox = self.service.create_sandbox(
            self.principal, "daytona", {"timeout_secs": 600}
        )
        self.clock.value += timedelta(minutes=2)
        self.service.cost_summary(self.principal)
        self.service.destroy_sandbox(self.principal, sandbox.id)
        calls = self.factory.calls

        self.set_policy(hourly_spend_limit_usd=Decimal("0.01"))
        with self.assertRaises(PolicyDeniedError) as hourly:
            self.service.create_sandbox(
                self.principal, "daytona", {"timeout_secs": 60}
            )
        self.assertEqual(hourly.exception.policy, "hourly_spend_limit_usd")

        self.set_policy(daily_spend_limit_usd=Decimal("0.01"))
        with self.assertRaises(PolicyDeniedError) as daily:
            self.service.create_sandbox(
                self.principal, "daytona", {"timeout_secs": 60}
            )
        self.assertEqual(daily.exception.policy, "daily_spend_limit_usd")
        self.assertEqual(self.factory.calls, calls)

    def test_policy_is_tenant_scoped(self) -> None:
        self.set_policy(max_concurrent_sandboxes=0)
        _, other_key = self.service.bootstrap_organization("Other")
        other = self.store.authenticate(other_key.secret)

        with self.assertRaises(PolicyDeniedError):
            self.service.create_sandbox(
                self.principal, "daytona", {"timeout_secs": 60}
            )
        created = self.service.create_sandbox(
            other, "daytona", {"timeout_secs": 60}
        )

        self.assertEqual(created.organization_id, other.organization_id)
        self.assertEqual(self.factory.calls, 1)
        self.assertEqual(
            len(self.service.policy_summary(self.principal)["denials"]), 1
        )
        self.assertEqual(len(self.service.policy_summary(other)["denials"]), 0)


class PolicyConcurrencyTest(unittest.TestCase):

    def test_concurrent_launches_cannot_exceed_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "race.db", key_pepper="race")
            factory = PolicyFactory(block_first=True)
            service = ControlPlane(
                store,
                sandbox_factory=factory,
                allowed_backends={"daytona"},
                supervision_interval_secs=0,
            )
            _, key = service.bootstrap_organization("Race org")
            principal = store.authenticate(key.secret)
            service.set_policy(
                principal,
                max_concurrent_sandboxes=1,
                hourly_spend_limit_usd=None,
                daily_spend_limit_usd=None,
                allowed_backends=["daytona"],
                allowed_gpu_types=None,
                max_sandbox_lifetime_secs=300,
            )

            def launch(key_suffix: str) -> str:
                try:
                    record = service.create_sandbox(
                        principal,
                        "daytona",
                        {"timeout_secs": 60},
                        idempotency_key=f"race-{key_suffix}",
                    )
                except PolicyDeniedError:
                    return "denied"
                return record.status

            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(launch, "one")
                self.assertTrue(factory.entered.wait(timeout=2))
                second = pool.submit(launch, "two")
                self.assertEqual(second.result(timeout=2), "denied")
                factory.release.set()
                self.assertEqual(first.result(timeout=2), "running")

            self.assertEqual(factory.calls, 1)
            self.assertEqual(
                len(
                    [
                        item
                        for item in store.list_sandboxes(
                            principal.organization_id
                        )
                        if item.status == "running"
                    ]
                ),
                1,
            )
            service.close()


class SupervisorTest(unittest.TestCase):

    def test_api_termination_and_close_destroy_a_runtime_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(
                Path(directory) / "termination-race.db",
                key_pepper="termination-race",
            )
            factory = PolicyFactory()
            service = ControlPlane(
                store,
                sandbox_factory=factory,
                allowed_backends={"daytona"},
                supervision_interval_secs=0,
            )
            _, key = service.bootstrap_organization("Termination race")
            principal = store.authenticate(key.secret)
            created = service.create_sandbox(principal, "daytona", {})
            transition_entered = threading.Event()
            release_transition = threading.Event()
            original_transition = store.begin_termination

            def blocking_transition(*args: object, **kwargs: object):
                transition_entered.set()
                release_transition.wait(timeout=2)
                return original_transition(*args, **kwargs)

            store.begin_termination = blocking_transition  # type: ignore[method-assign]
            close_waiting = threading.Event()
            original_lock = service._lock

            class ObservedLock:

                def __enter__(self):
                    if threading.current_thread().name == "close-thread":
                        close_waiting.set()
                    original_lock.acquire()
                    return self

                def __exit__(self, *exc: object) -> None:
                    original_lock.release()

            service._lock = ObservedLock()  # type: ignore[assignment]
            errors: list[BaseException] = []

            def terminate() -> None:
                try:
                    service.destroy_sandbox(principal, created.id)
                except BaseException as exc:
                    errors.append(exc)

            api_thread = threading.Thread(target=terminate)
            close_thread = threading.Thread(
                target=service.close, name="close-thread"
            )
            api_thread.start()
            self.assertTrue(transition_entered.wait(timeout=2))
            close_thread.start()
            self.assertTrue(close_waiting.wait(timeout=2))
            release_transition.set()
            api_thread.join(timeout=2)
            close_thread.join(timeout=2)

            self.assertFalse(api_thread.is_alive())
            self.assertFalse(close_thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(factory.created[0].destroyed, True)
            self.assertEqual(factory.created[0].destroy_calls, 1)
            self.assertEqual(
                store.get_sandbox(principal.organization_id, created.id).status,
                "destroyed",
            )

    def test_close_continues_after_a_lifecycle_marking_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(
                Path(directory) / "close-mark.db", key_pepper="close-mark"
            )
            factory = PolicyFactory()
            service = ControlPlane(
                store,
                sandbox_factory=factory,
                allowed_backends={"daytona"},
                supervision_interval_secs=0,
            )
            _, key = service.bootstrap_organization("Close marking")
            principal = store.authenticate(key.secret)
            service.create_sandbox(principal, "daytona", {})
            service.create_sandbox(principal, "daytona", {})
            original_mark = store.mark_lifecycle
            failed_once = False

            def fail_first_mark(*args: object, **kwargs: object):
                nonlocal failed_once
                if not failed_once:
                    failed_once = True
                    raise RuntimeError("database unavailable")
                return original_mark(*args, **kwargs)

            store.mark_lifecycle = fail_first_mark  # type: ignore[method-assign]

            service.close()

            self.assertEqual(
                [runtime.destroy_calls for runtime in factory.created], [1, 1]
            )

    def test_api_termination_does_not_claim_without_a_cleanup_adapter(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(
                Path(directory) / "terminate.db", key_pepper="terminate"
            )
            factory = PolicyFactory()
            service = ControlPlane(
                store,
                sandbox_factory=factory,
                allowed_backends={"daytona"},
                supervision_interval_secs=0,
            )
            _, key = service.bootstrap_organization("Terminate org")
            principal = store.authenticate(key.secret)
            created = service.create_sandbox(principal, "daytona", {})
            service._runtimes.clear()

            with self.assertRaises(ConflictError):
                service.destroy_sandbox(principal, created.id)

            self.assertEqual(
                store.get_sandbox(principal.organization_id, created.id).status,
                "running",
            )
            deleted: list[str] = []
            service._provider_terminators["daytona"] = deleted.append
            destroyed = service.destroy_sandbox(principal, created.id)
            self.assertEqual(deleted, [created.provider_resource_id])
            self.assertEqual(destroyed.status, "destroyed")
            service.close()

    def test_restart_supervisor_terminates_expired_resource_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "restart.db"
            store = SQLiteStore(path, key_pepper="restart")
            clock = MutableClock(datetime.now(UTC).replace(microsecond=0))
            factory = PolicyFactory()
            first = ControlPlane(
                store,
                sandbox_factory=factory,
                allowed_backends={"daytona"},
                now=clock,
                supervision_interval_secs=0,
            )
            _, key = first.bootstrap_organization("Restart org")
            principal = store.authenticate(key.secret)
            created = first.create_sandbox(
                principal, "daytona", {"timeout_secs": 10}
            )
            first._runtimes.clear()  # Simulate abrupt process loss.

            terminated: list[str] = []
            terminated_event = threading.Event()

            def terminate(resource_id: str) -> None:
                terminated.append(resource_id)
                terminated_event.set()

            clock.value += timedelta(seconds=20)
            restarted = ControlPlane(
                store,
                allowed_backends={"daytona"},
                provider_terminators={"daytona": terminate},
                now=clock,
                supervision_interval_secs=0.01,
            )
            restarted.start_supervision()
            self.assertTrue(terminated_event.wait(timeout=2))
            restarted.stop_supervision()
            repeated = restarted.run_watchdog_once()

            record = store.get_sandbox(principal.organization_id, created.id)
            self.assertEqual(terminated, [created.provider_resource_id])
            self.assertEqual(record.status, "destroyed")
            self.assertEqual(record.termination_reason, "ttl_expired")
            self.assertIsNone(record.terminated_by_api_key_id)
            self.assertEqual(repeated["expired_claimed"], 0)
            restarted.close()

    @unittest.skipIf(
        TestClient is None, "server dependencies are not installed"
    )
    def test_restart_reclaims_api_termination_interrupted_before_delete(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api-interrupted.db"
            store = SQLiteStore(path, key_pepper="api-interrupted")
            clock = MutableClock(datetime(2026, 9, 4, 12, tzinfo=UTC))
            factory = PolicyFactory()
            first = ControlPlane(
                store,
                sandbox_factory=factory,
                allowed_backends={"daytona"},
                now=clock,
                supervision_interval_secs=0,
            )
            _, key = first.bootstrap_organization("API interrupted org")
            principal = store.authenticate(key.secret)
            created = first.create_sandbox(
                principal, "daytona", {"timeout_secs": 10}
            )
            app = create_app(first)
            destroy_endpoint = next(
                route.endpoint
                for route in app.routes
                if getattr(route, "path", None) == "/v1/sandboxes/{sandbox_id}"
                and "DELETE" in getattr(route, "methods", set())
            )
            factory.created[0].crash_on_destroy = True
            clock.value += timedelta(seconds=20)
            with self.assertRaises(SimulatedProcessCrash):
                destroy_endpoint(
                    created.id,
                    authorization=f"Bearer {key.secret}",
                    if_match=None,
                )
            first.close()

            interrupted = store.get_sandbox(key.organization_id, created.id)
            self.assertEqual(interrupted.status, "stopping")
            self.assertIsNone(interrupted.termination_reason)

            deleted: list[str] = []
            clock.value += timedelta(seconds=61)
            restarted = ControlPlane(
                store,
                allowed_backends={"daytona"},
                provider_terminators={"daytona": deleted.append},
                now=clock,
                supervision_interval_secs=0,
            )
            first_pass = restarted.run_watchdog_once()
            second_pass = restarted.run_watchdog_once()

            recovered = store.get_sandbox(key.organization_id, created.id)
            self.assertEqual(deleted, [created.provider_resource_id])
            self.assertEqual(first_pass["expired_terminated"], 1)
            self.assertEqual(second_pass["expired_claimed"], 0)
            self.assertEqual(recovered.status, "destroyed")
            self.assertEqual(recovered.termination_reason, "ttl_expired")
            restarted.close()

    def test_restart_reclaims_an_expiry_interrupted_while_stopping(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "interrupted.db"
            store = SQLiteStore(path, key_pepper="interrupted")
            clock = MutableClock(datetime(2026, 9, 4, 12, tzinfo=UTC))
            factory = PolicyFactory()
            first = ControlPlane(
                store,
                sandbox_factory=factory,
                allowed_backends={"daytona"},
                now=clock,
                supervision_interval_secs=0,
            )
            _, key = first.bootstrap_organization("Interrupted org")
            principal = store.authenticate(key.secret)
            created = first.create_sandbox(
                principal, "daytona", {"timeout_secs": 10}
            )
            first._runtimes.clear()
            clock.value += timedelta(seconds=20)
            claimed = store.claim_expired_sandboxes(
                timestamp=clock.value.isoformat(),
                owner="dead-process",
                stale_before=(clock.value - timedelta(seconds=60)).isoformat(),
            )
            self.assertEqual([item.id for item in claimed], [created.id])

            deleted: list[str] = []
            clock.value += timedelta(seconds=61)
            restarted = ControlPlane(
                store,
                allowed_backends={"daytona"},
                provider_terminators={"daytona": deleted.append},
                now=clock,
                supervision_interval_secs=0,
            )
            result = restarted.run_watchdog_once()

            self.assertEqual(result["expired_terminated"], 1)
            self.assertEqual(deleted, [created.provider_resource_id])
            self.assertEqual(
                store.get_sandbox(principal.organization_id, created.id).status,
                "destroyed",
            )
            restarted.close()

    def test_orphan_watchdog_is_tenant_safe_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(
                Path(directory) / "orphan.db", key_pepper="orphan"
            )
            service = ControlPlane(
                store,
                allowed_backends={"daytona"},
                supervision_interval_secs=0,
            )
            org, _ = service.bootstrap_organization("Orphan org")
            other, _ = service.bootstrap_organization("Other org")
            reconciler = FakeReconciler()
            observed = datetime(2026, 9, 4, tzinfo=UTC).isoformat()
            reconciler.by_org[org["id"]] = [
                ProviderObservation(
                    "orphan-1", "resource-orphan", "running", observed
                )
            ]
            deleted: list[str] = []
            service._provider_reconcilers["daytona"] = reconciler
            service._provider_terminators["daytona"] = deleted.append

            first = service.run_watchdog_once()
            second = service.run_watchdog_once()

            self.assertEqual(deleted, ["resource-orphan"])
            self.assertEqual(first["orphans_deleted"], 1)
            self.assertEqual(second["orphans_deleted"], 0)
            with sqlite3.connect(store._path) as connection:
                row = connection.execute(
                    """SELECT organization_id, status FROM orphan_cleanups
                       WHERE provider_resource_id='resource-orphan'"""
                ).fetchone()
            self.assertEqual(row, (org["id"], "deleted"))
            self.assertNotEqual(row[0], other["id"])
            service.close()

    def test_stale_orphan_claim_is_recovered_after_process_loss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(
                Path(directory) / "orphan-crash.db", key_pepper="orphan-crash"
            )
            clock = MutableClock(datetime(2026, 9, 4, tzinfo=UTC))
            org, _ = ControlPlane(
                store, supervision_interval_secs=0
            ).bootstrap_organization("Orphan crash org")
            reconciler = FakeReconciler()
            reconciler.by_org[org["id"]] = [
                ProviderObservation(
                    "orphan-crash",
                    "resource-after-crash",
                    "running",
                    clock.value.isoformat(),
                )
            ]

            def crash(_: str) -> None:
                raise SimulatedProcessCrash

            interrupted = ControlPlane(
                store,
                allowed_backends={"daytona"},
                provider_reconcilers={"daytona": reconciler},
                provider_terminators={"daytona": crash},
                now=clock,
                supervision_interval_secs=0,
            )
            with self.assertRaises(SimulatedProcessCrash):
                interrupted.run_watchdog_once()

            deleted: list[str] = []
            restarted = ControlPlane(
                store,
                allowed_backends={"daytona"},
                provider_reconcilers={"daytona": reconciler},
                provider_terminators={"daytona": deleted.append},
                now=clock,
                supervision_interval_secs=0,
            )
            immediate = restarted.run_watchdog_once()
            clock.value += timedelta(seconds=61)
            recovered = restarted.run_watchdog_once()

            self.assertEqual(immediate["orphans_deleted"], 0)
            self.assertEqual(recovered["orphans_deleted"], 1)
            self.assertEqual(deleted, ["resource-after-crash"])
            restarted.close()

    def test_stale_orphan_completion_cannot_overwrite_current_claim(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(
                Path(directory) / "orphan-generation.db",
                key_pepper="orphan-generation",
            )
            org = store.create_organization("Orphan generation")
            first_at = datetime(2026, 9, 4, tzinfo=UTC)
            second_at = first_at + timedelta(seconds=61)
            first_claim = store.claim_orphan_cleanup(
                organization_id=org["id"],
                backend="daytona",
                provider_resource_id="resource-generation",
                observed_at=first_at.isoformat(),
                claimed_at=first_at.isoformat(),
                stale_before=(first_at - timedelta(seconds=60)).isoformat(),
            )
            second_claim = store.claim_orphan_cleanup(
                organization_id=org["id"],
                backend="daytona",
                provider_resource_id="resource-generation",
                observed_at=second_at.isoformat(),
                claimed_at=second_at.isoformat(),
                stale_before=(second_at - timedelta(seconds=60)).isoformat(),
            )

            self.assertIsNotNone(first_claim)
            self.assertIsNotNone(second_claim)
            self.assertNotEqual(first_claim, second_claim)
            self.assertFalse(
                store.renew_orphan_cleanup_claim(
                    "daytona",
                    "resource-generation",
                    claim_id=first_claim or "",
                    claimed_at=second_at.isoformat(),
                )
            )
            self.assertFalse(
                store.complete_orphan_cleanup(
                    "daytona",
                    "resource-generation",
                    claim_id=first_claim or "",
                    status="deleted",
                    completed_at=second_at.isoformat(),
                )
            )
            self.assertTrue(
                store.complete_orphan_cleanup(
                    "daytona",
                    "resource-generation",
                    claim_id=second_claim or "",
                    status="deleted",
                    completed_at=second_at.isoformat(),
                )
            )


@unittest.skipIf(TestClient is None, "server dependencies are not installed")
class RoleAndProviderAPITest(unittest.TestCase):

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "roles.db"
        self.store = SQLiteStore(self.path, key_pepper="roles")
        self.factory = PolicyFactory()
        self.secret = "provider-secret-must-never-persist"

        def unsafe_check(settings: dict[str, str]) -> bool:
            raise RuntimeError(f"provider rejected {settings['token']}")

        self.service = ControlPlane(
            self.store,
            sandbox_factory=self.factory,
            allowed_backends={"daytona"},
            provider_settings={"daytona": {"token": self.secret}},
            provider_health_checks={"daytona": unsafe_check},
            supervision_interval_secs=0,
        )
        _, self.admin_key = self.service.bootstrap_organization("Roles org")
        self.admin = self.store.authenticate(self.admin_key.secret)
        self.admin_headers = {
            "Authorization": f"Bearer {self.admin_key.secret}"
        }
        self.client = TestClient(create_app(self.service))

    def tearDown(self) -> None:
        self.client.close()
        self.service.close()
        self.temp_dir.cleanup()

    def issue(self, name: str, scopes: list[str]) -> dict[str, str]:
        key = self.service.issue_api_key(self.admin, name=name, scopes=scopes)
        return {"Authorization": f"Bearer {key.secret}"}

    def test_scoped_roles_and_action_attribution(self) -> None:
        launcher = self.issue("Launcher", ["sandboxes:create"])
        terminator = self.issue("Terminator", ["sandboxes:terminate"])
        observer = self.issue(
            "Observer",
            ["sandboxes:read", "usage:read", "policies:read", "providers:read"],
        )
        created = self.client.post(
            "/v1/sandboxes",
            json={"backend": "daytona", "timeout_secs": 60},
            headers=launcher,
        )
        denied_list = self.client.get("/v1/sandboxes", headers=launcher)
        denied_destroy = self.client.delete(
            f"/v1/sandboxes/{created.json()['id']}", headers=launcher
        )
        visible = self.client.get("/v1/sandboxes", headers=observer)
        terminated = self.client.delete(
            f"/v1/sandboxes/{created.json()['id']}", headers=terminator
        )

        self.assertEqual(created.status_code, 201)
        self.assertEqual(denied_list.status_code, 403)
        self.assertEqual(denied_destroy.status_code, 403)
        self.assertEqual(visible.status_code, 200)
        self.assertEqual(terminated.status_code, 200)
        self.assertEqual(
            terminated.json()["terminated_by_api_key_name"], "Terminator"
        )
        session = self.client.get("/v1/session", headers=observer).json()
        self.assertEqual(session["role"], "observer")
        self.assertFalse(session["can_terminate"])

    def test_policy_denial_api_is_stable_safe_and_tenant_scoped(self) -> None:
        policy_manager = self.issue(
            "Policy manager", ["policies:read", "policies:write"]
        )
        launcher = self.issue("Restricted launcher", ["sandboxes:create"])
        configured = self.client.put(
            "/v1/policies/current",
            json={
                "max_concurrent_sandboxes": 0,
                "allowed_backends": ["daytona"],
            },
            headers=policy_manager,
        )
        denied = self.client.post(
            "/v1/sandboxes",
            json={"backend": "daytona", "timeout_secs": 60},
            headers=launcher,
        )
        summary = self.client.get(
            "/v1/policy-summary", headers=self.admin_headers
        )

        self.assertEqual(configured.status_code, 200)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(
            denied.json()["detail"],
            {
                "message": "Concurrent sandbox limit has been reached.",
                "code": "policy_denied",
                "policy": "max_concurrent_sandboxes",
                "retryable": False,
                "current": 0,
                "limit": 0,
            },
        )
        self.assertEqual(summary.status_code, 200)
        self.assertEqual(len(summary.json()["denials"]), 1)
        self.assertEqual(
            summary.json()["denials"][0]["api_key_name"],
            "Restricted launcher",
        )
        self.assertNotIn(self.secret, denied.text + summary.text)
        self.assertEqual(self.factory.calls, 0)

    def test_provider_health_never_exposes_or_persists_credentials(
        self,
    ) -> None:
        observer = self.issue("Provider viewer", ["providers:read"])
        manager = self.issue("Provider manager", ["providers:write"])
        before = self.client.get("/v1/providers", headers=observer)
        denied = self.client.post(
            "/v1/providers/daytona/health-check", headers=observer
        )
        checked = self.client.post(
            "/v1/providers/daytona/health-check", headers=manager
        )
        after = self.client.get("/v1/providers", headers=observer)

        self.assertEqual(before.json()["items"][0]["status"], "unknown")
        self.assertTrue(before.json()["items"][0]["configured"])
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(checked.status_code, 200)
        self.assertEqual(checked.json()["status"], "degraded")
        self.assertEqual(
            checked.json()["message"], "Provider health check failed."
        )
        combined = before.text + checked.text + after.text
        self.assertNotIn(self.secret, combined)
        self.assertNotIn("token", combined)
        self.assertNotIn(self.secret.encode(), self.path.read_bytes())

    def test_default_cli_provider_without_checker_remains_unchecked(
        self,
    ) -> None:
        path = Path(self.temp_dir.name) / "unchecked.db"
        store = SQLiteStore(path, key_pepper="unchecked")
        secret = "configured-but-not-health-checked-secret"
        service = ControlPlane(
            store,
            allowed_backends={"daytona"},
            provider_settings=_provider_settings_from_environment(
                {"DAYTONA_API_KEY": secret}
            ),
            supervision_interval_secs=0,
        )
        _, key = service.bootstrap_organization("Unchecked provider org")
        principal = store.authenticate(key.secret)

        checked = service.check_provider_health(principal, "daytona")
        listed = service.provider_summary(principal)[0]

        self.assertTrue(checked["configured"])
        self.assertEqual(checked["status"], "unchecked")
        self.assertEqual(
            checked["message"], "Provider health check is unavailable."
        )
        self.assertEqual(listed, checked)
        self.assertNotIn(secret, str(checked) + str(listed))
        self.assertNotIn(secret.encode(), path.read_bytes())
        service.close()


if __name__ == "__main__":
    unittest.main()
