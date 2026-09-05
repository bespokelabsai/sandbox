"""Behavioral tests for tenant isolation, API keys, and cost metering."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from bespokelabs.sandbox import Sandbox
from bespokelabs.sandbox.backends.docker import DockerSession
from bespokelabs.sandbox.backends.runpod import RunpodSession
from bespokelabs.sandbox.control_plane.errors import (
    AuthorizationError,
    NotFoundError,
)
from bespokelabs.sandbox.control_plane.service import ControlPlane
from bespokelabs.sandbox.control_plane.store import _SCHEMA, SQLiteStore
from bespokelabs.sandbox.exceptions import (
    ErrorOutcome,
    SandboxExecutionError,
    SandboxTimeoutError,
)
from bespokelabs.sandbox.types import SandboxConfig, SandboxResult

try:
    from fastapi.testclient import TestClient

    from bespokelabs.sandbox.control_plane.api import create_app
except ImportError:  # pragma: no cover - server extra is optional
    TestClient = None
    create_app = None


class FakeSandbox:

    def __init__(self, backend: str, **config: object) -> None:
        self.backend_name = backend
        self.config = config
        self.calls = 0
        self.destroyed = False
        self.provider_resource_id = f"provider-{len(config)}"

    def execute_code(
        self, code: str, language: str = "python"
    ) -> SandboxResult:
        self.calls += 1
        return SandboxResult(stdout=f"{language}:{code}")

    def execute_command(
        self, command: str, args: list[str] | None = None
    ) -> SandboxResult:
        self.calls += 1
        return SandboxResult(stdout=" ".join([command, *(args or [])]))

    def estimate_compute_cost(self, elapsed_secs: float) -> float:
        del elapsed_secs
        return 0.25

    def destroy(self) -> None:
        self.destroyed = True


class FakeFactory:

    def __init__(self) -> None:
        self.created: list[FakeSandbox] = []

    def __call__(self, backend: str, **config: object) -> FakeSandbox:
        sandbox = FakeSandbox(backend, **config)
        self.created.append(sandbox)
        return sandbox


class ControlPlaneTest(unittest.TestCase):

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "control-plane.db"
        self.store = SQLiteStore(self.db_path, key_pepper="test-pepper")
        self.factory = FakeFactory()
        self.service = ControlPlane(
            self.store,
            sandbox_factory=self.factory,
            customer_markup=Decimal("1.5"),
            allowed_backends={"local"},
        )
        self.org, self.key = self.service.bootstrap_organization("Acme")
        self.principal = self.store.authenticate(self.key.secret)

    def tearDown(self) -> None:
        self.service.close()
        self.temp_dir.cleanup()

    def test_key_is_hashed_and_secret_authenticates(self) -> None:
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT key_digest FROM api_keys WHERE id = ?", (self.key.id,)
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertNotEqual(row[0], self.key.secret)
        self.assertEqual(self.principal.organization_id, self.org["id"])

    def test_tenant_cannot_read_another_tenants_sandbox(self) -> None:
        sandbox = self.service.create_sandbox(
            self.principal, "local", {"cpu": 2}
        )
        _, other_key = self.service.bootstrap_organization("Other")
        other = self.store.authenticate(other_key.secret)
        with self.assertRaises(NotFoundError):
            self.service.get_sandbox(other, sandbox.id)

    def test_scoped_key_cannot_create_sandbox(self) -> None:
        issued = self.service.issue_api_key(
            self.principal, name="Read only", scopes=["sandboxes:read"]
        )
        read_only = self.store.authenticate(issued.secret)
        with self.assertRaises(AuthorizationError):
            self.service.create_sandbox(read_only, "local", {})

    def test_execution_is_idempotent_and_metered_once(self) -> None:
        sandbox = self.service.create_sandbox(
            self.principal,
            "local",
            {"env_vars": {"SECRET": "do-not-store"}},
        )
        first = self.service.execute(
            self.principal,
            sandbox.id,
            code="print(1)",
            request_id="same-request",
        )
        second = self.service.execute(
            self.principal,
            sandbox.id,
            code="print(1)",
            request_id="same-request",
        )

        self.assertEqual(first, second)
        self.assertEqual(self.factory.created[0].calls, 1)
        refreshed = self.service.get_sandbox(self.principal, sandbox.id)
        self.assertNotIn("env_vars", refreshed.config)
        self.assertEqual(refreshed.config["env_var_names"], ["SECRET"])

        summary = self.service.cost_summary(self.principal, group_by="backend")
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["key"], "local")
        self.assertEqual(
            Decimal(summary[0]["provider_cost_usd"]), Decimal("0.25")
        )
        self.assertEqual(
            Decimal(summary[0]["customer_cost_usd"]), Decimal("0.375")
        )

    def test_destroy_terminates_runtime(self) -> None:
        sandbox = self.service.create_sandbox(self.principal, "local", {})
        destroyed = self.service.destroy_sandbox(self.principal, sandbox.id)
        self.assertEqual(destroyed.status, "destroyed")
        self.assertTrue(self.factory.created[0].destroyed)


class _TimeoutAfterCreateFactory:

    def __init__(self) -> None:
        self.calls = 0
        self.live: list[str] = []

    def __call__(self, backend: str, **config: object) -> FakeSandbox:
        del config
        self.calls += 1
        resource_id = f"daytona-{self.calls}"
        self.live.append(resource_id)
        # Simulate the Daytona create response timing out after the resource
        # exists, followed by successful create-token cleanup.
        self.live.remove(resource_id)
        raise SandboxTimeoutError(
            "provider said secret=do-not-leak",
            backend=backend,
            op="create",
            context={
                "cleanup_status": "deleted",
                "provider_resource_id": resource_id,
                "credential": "do-not-leak",
            },
            outcome=ErrorOutcome.FAILED,
        )


class _AmbiguousTimeoutFactory:

    def __init__(self) -> None:
        self.live = ["possibly-live-daytona-resource"]
        self.calls = 0

    def __call__(self, backend: str, **config: object) -> FakeSandbox:
        del config
        self.calls += 1
        raise SandboxTimeoutError(
            "provider response contained SUPER_SECRET",
            backend=backend,
            op="create",
            context={"cleanup_status": "unknown", "token": "SUPER_SECRET"},
            outcome=ErrorOutcome.UNKNOWN,
        )


class _FailingExecutionSandbox(FakeSandbox):

    def execute_code(
        self, code: str, language: str = "python"
    ) -> SandboxResult:
        del code, language
        raise SandboxExecutionError(
            "provider leaked password=SUPER_SECRET",
            context={"password": "SUPER_SECRET", "exit_code": 17},
        )


@unittest.skipIf(TestClient is None, "server dependencies are not installed")
class ControlPlaneAPILifecycleTest(unittest.TestCase):

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = SQLiteStore(
            Path(self.temp_dir.name) / "api.db", key_pepper="api-pepper"
        )
        self.factory = FakeFactory()
        self.service = ControlPlane(
            self.store,
            sandbox_factory=self.factory,
            allowed_backends={"daytona", "docker", "local", "runpod"},
        )
        _, key = self.service.bootstrap_organization("Acme")
        self.headers = {"Authorization": f"Bearer {key.secret}"}
        self.client = TestClient(create_app(self.service))

    def tearDown(self) -> None:
        self.client.close()
        self.service.close()
        self.temp_dir.cleanup()

    def test_create_idempotency_returns_one_logical_and_provider_sandbox(
        self,
    ) -> None:
        headers = {**self.headers, "Idempotency-Key": "launch-42"}
        first = self.client.post(
            "/v1/sandboxes",
            json={"backend": "local", "cpu": 2},
            headers=headers,
        )
        second = self.client.post(
            "/v1/sandboxes",
            json={"backend": "local", "cpu": 2},
            headers=headers,
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(len(self.factory.created), 1)
        self.assertEqual(first.json()["attempt_count"], 1)
        self.assertEqual(first.json()["retry_status"], "not_applicable")
        self.assertEqual(first.json()["provider_resource_id"], "provider-1")
        self.assertIn("latest_error", first.json())
        self.assertIn("cleanup_status", first.json())
        with sqlite3.connect(self.store._path) as connection:
            attempts = connection.execute(
                "SELECT status FROM sandbox_creation_attempts"
            ).fetchall()
        self.assertEqual(attempts, [("succeeded",)])

    def test_same_idempotency_key_rejects_a_different_request(self) -> None:
        headers = {**self.headers, "Idempotency-Key": "launch-42"}
        first = self.client.post(
            "/v1/sandboxes",
            json={"backend": "local", "cpu": 1},
            headers=headers,
        )
        second = self.client.post(
            "/v1/sandboxes",
            json={"backend": "local", "cpu": 2},
            headers=headers,
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(len(self.factory.created), 1)

    def test_lifecycle_and_reconciliation_summaries_are_authenticated(
        self,
    ) -> None:
        created = self.client.post(
            "/v1/sandboxes",
            json={"backend": "local"},
            headers=self.headers,
        ).json()

        self.assertIsNotNone(created["requested_at"])
        self.assertIsNotNone(created["provisioning_at"])
        self.assertIsNotNone(created["running_at"])
        self.assertEqual(created["currency"], "USD")
        self.assertEqual(created["cost_state"], "estimated")
        summary = self.client.get("/v1/reconciliation", headers=self.headers)
        denied = self.client.get("/v1/reconciliation")

        self.assertEqual(summary.status_code, 200)
        self.assertEqual(summary.json()["total"], 1)
        self.assertIn("unreconciled", summary.json())
        self.assertEqual(denied.status_code, 401)

    def test_docker_and_runpod_resource_ids_persist_in_hosted_records(
        self,
    ) -> None:
        deleted: list[str] = []

        def factory(backend: str, **config: object) -> Sandbox:
            del config
            if backend == "docker":
                container = SimpleNamespace(
                    id="container-hosted",
                    remove=lambda **_: deleted.append("container-hosted"),
                )
                session = DockerSession(container=container, timeout=60)
            else:
                session = object.__new__(RunpodSession)
                session._pod_id = "pod-hosted"
                session._api = SimpleNamespace(
                    delete_pod=lambda pod_id: deleted.append(pod_id)
                )
            return Sandbox._from_session(
                backend, session, SandboxConfig(backend=backend)
            )

        self.service._sandbox_factory = factory
        docker = self.client.post(
            "/v1/sandboxes",
            json={"backend": "docker"},
            headers={**self.headers, "Idempotency-Key": "docker-id"},
        )
        runpod = self.client.post(
            "/v1/sandboxes",
            json={"backend": "runpod"},
            headers={**self.headers, "Idempotency-Key": "runpod-id"},
        )

        self.assertEqual(docker.status_code, 201)
        self.assertEqual(runpod.status_code, 201)
        self.assertEqual(
            docker.json()["provider_resource_id"], "container-hosted"
        )
        self.assertEqual(runpod.json()["provider_resource_id"], "pod-hosted")

    def test_timeout_is_structured_persisted_redacted_and_cleaned(self) -> None:
        factory = _TimeoutAfterCreateFactory()
        self.service._sandbox_factory = factory
        response = self.client.post(
            "/v1/sandboxes",
            json={"backend": "daytona"},
            headers={**self.headers, "Idempotency-Key": "timeout-1"},
        )

        self.assertEqual(response.status_code, 504)
        detail = response.json()["detail"]
        self.assertEqual(
            set(detail),
            {
                "message",
                "code",
                "backend",
                "op",
                "retryable",
                "outcome",
                "context",
            },
        )
        self.assertEqual(detail["code"], "timeout")
        self.assertEqual(detail["backend"], "daytona")
        self.assertEqual(detail["op"], "create")
        self.assertTrue(detail["retryable"])
        self.assertEqual(detail["outcome"], "failed")
        self.assertEqual(detail["context"]["cleanup_status"], "deleted")
        self.assertNotIn("credential", detail["context"])
        self.assertNotIn("do-not-leak", response.text)
        self.assertEqual(factory.live, [])

        sandboxes = self.client.get(
            "/v1/sandboxes", headers=self.headers
        ).json()
        self.assertEqual(len(sandboxes), 1)
        record = sandboxes[0]
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["attempt_count"], 1)
        self.assertEqual(record["latest_error"], detail)
        self.assertEqual(record["retry_status"], "retryable")
        self.assertEqual(record["cleanup_status"], "deleted")
        self.assertEqual(record["provider_resource_id"], "daytona-1")

        with sqlite3.connect(self.store._path) as connection:
            stored = " ".join(
                value or ""
                for value in connection.execute(
                    "SELECT latest_error_json, error FROM sandboxes"
                ).fetchone()
            )
            attempt = connection.execute(
                """
                SELECT status, error_json, retry_status, cleanup_status,
                       provider_resource_id, completed_at
                FROM sandbox_creation_attempts
                """
            ).fetchone()
        self.assertNotIn("do-not-leak", stored)
        self.assertEqual(attempt[0], "failed")
        self.assertEqual(json.loads(attempt[1]), detail)
        self.assertEqual(attempt[2], "retryable")
        self.assertEqual(attempt[3], "deleted")
        self.assertEqual(attempt[4], "daytona-1")
        self.assertIsNotNone(attempt[5])

    def test_ambiguous_create_is_never_reported_safe_to_retry(self) -> None:
        factory = _AmbiguousTimeoutFactory()
        self.service._sandbox_factory = factory

        response = self.client.post(
            "/v1/sandboxes",
            json={"backend": "daytona"},
            headers={**self.headers, "Idempotency-Key": "ambiguous-1"},
        )

        self.assertEqual(response.status_code, 504)
        detail = response.json()["detail"]
        self.assertEqual(detail["outcome"], "unknown")
        self.assertFalse(detail["retryable"])
        self.assertEqual(detail["context"], {"cleanup_status": "unknown"})
        self.assertNotIn("SUPER_SECRET", response.text)
        record = self.client.get("/v1/sandboxes", headers=self.headers).json()[
            0
        ]
        self.assertEqual(record["retry_status"], "blocked_cleanup_unknown")
        self.assertEqual(record["cleanup_status"], "unknown")
        self.assertEqual(record["attempt_count"], 1)
        self.assertEqual(factory.live, ["possibly-live-daytona-resource"])
        replay = self.client.post(
            "/v1/sandboxes",
            json={"backend": "daytona"},
            headers={**self.headers, "Idempotency-Key": "ambiguous-1"},
        )
        self.assertEqual(replay.status_code, 201)
        self.assertEqual(replay.json()["id"], record["id"])
        self.assertEqual(factory.calls, 1)

    def test_execution_error_is_structured_and_redacted(self) -> None:
        created: list[_FailingExecutionSandbox] = []

        def factory(backend: str, **config: object) -> _FailingExecutionSandbox:
            sandbox = _FailingExecutionSandbox(backend, **config)
            created.append(sandbox)
            return sandbox

        self.service._sandbox_factory = factory
        sandbox = self.client.post(
            "/v1/sandboxes",
            json={"backend": "local"},
            headers=self.headers,
        ).json()

        response = self.client.post(
            f"/v1/sandboxes/{sandbox['id']}/execute",
            json={"code": "raise SystemExit"},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 502)
        detail = response.json()["detail"]
        self.assertEqual(detail["code"], "execution_failed")
        self.assertEqual(detail["backend"], "local")
        self.assertEqual(detail["op"], "execute")
        self.assertEqual(detail["context"], {"exit_code": 17})
        self.assertNotIn("SUPER_SECRET", response.text)
        self.assertEqual(len(created), 1)
        with sqlite3.connect(self.store._path) as connection:
            stored_error = connection.execute(
                "SELECT error FROM executions"
            ).fetchone()[0]
        self.assertNotIn("SUPER_SECRET", stored_error)


class SQLiteMigrationTest(unittest.TestCase):

    def test_fresh_database_has_phase5_schema_and_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fresh.db"
            store = SQLiteStore(path, key_pepper="fresh-migration")
            organization = store.create_organization("Fresh")

            alerts = store.get_alert_configuration(organization["id"])
            retention = store.get_retention_policy(organization["id"])
            with sqlite3.connect(path) as connection:
                versions = connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()

            self.assertEqual(
                versions,
                [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,)],
            )
            self.assertIsNone(alerts.budget_threshold_percent)
            self.assertTrue(alerts.provider_degradation_enabled)
            self.assertEqual(retention.operational_days, 90)
            self.assertEqual(retention.audit_days, 365)

    def test_legacy_database_is_migrated_in_place_and_reopen_is_safe(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            with sqlite3.connect(path) as connection:
                connection.executescript(_SCHEMA)
                connection.execute(
                    "INSERT INTO organizations VALUES (?, ?, ?)",
                    ("org_old", "Legacy", "2026-01-01T00:00:00+00:00"),
                )
                connection.execute(
                    """
                    INSERT INTO sandboxes(
                        id, organization_id, backend, status, config_json,
                        created_at, destroyed_at, error
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)
                    """,
                    (
                        "sbx_old",
                        "org_old",
                        "local",
                        "running",
                        json.dumps({}),
                        "2026-01-01T00:00:00+00:00",
                    ),
                )

            store = SQLiteStore(path, key_pepper="migration-pepper")
            record = store.get_sandbox("org_old", "sbx_old")
            self.assertEqual(record.id, "sbx_old")
            self.assertEqual(record.attempt_count, 0)
            self.assertEqual(record.retry_status, "not_applicable")
            self.assertIsNone(record.creator_api_key_id)
            self.assertIsNone(record.expires_at)
            self.assertIsNone(record.termination_reason)
            self.assertEqual(record.version, 1)
            SQLiteStore(path, key_pepper="migration-pepper")
            with sqlite3.connect(path) as connection:
                versions = connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
                observation_table = connection.execute(
                    "SELECT name FROM sqlite_schema WHERE name='provider_observations'"
                ).fetchone()
                policy_table = connection.execute(
                    "SELECT name FROM sqlite_schema WHERE name='organization_policies'"
                ).fetchone()
                denial_table = connection.execute(
                    "SELECT name FROM sqlite_schema WHERE name='policy_denials'"
                ).fetchone()
                health_table = connection.execute(
                    "SELECT name FROM sqlite_schema WHERE name='provider_health'"
                ).fetchone()
                orphan_table = connection.execute(
                    "SELECT name FROM sqlite_schema WHERE name='orphan_cleanups'"
                ).fetchone()
                phase5_tables = connection.execute(
                    """SELECT name FROM sqlite_schema WHERE name IN (
                       'web_sessions', 'alert_configurations', 'alert_events',
                       'audit_log', 'retention_policies') ORDER BY name"""
                ).fetchall()
            self.assertEqual(
                versions,
                [(1,), (2,), (3,), (4,), (5,), (6,), (7,), (8,)],
            )
            self.assertEqual(observation_table, ("provider_observations",))
            self.assertEqual(policy_table, ("organization_policies",))
            self.assertEqual(denial_table, ("policy_denials",))
            self.assertEqual(health_table, ("provider_health",))
            self.assertEqual(orphan_table, ("orphan_cleanups",))
            self.assertEqual(
                phase5_tables,
                [
                    ("alert_configurations",),
                    ("alert_events",),
                    ("audit_log",),
                    ("retention_policies",),
                    ("web_sessions",),
                ],
            )


if __name__ == "__main__":
    unittest.main()
