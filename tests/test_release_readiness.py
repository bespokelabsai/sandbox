"""Phase 5 security, alerts, exports, retention, and end-to-end tests."""

from __future__ import annotations

import csv
import io
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

try:
    from fastapi.testclient import TestClient

    from bespokelabs.sandbox.control_plane.api import create_app
except ImportError:  # pragma: no cover - server extra is optional
    TestClient = None
    create_app = None

from bespokelabs.sandbox.control_plane.reconciliation import ProviderObservation
from bespokelabs.sandbox.control_plane.service import ControlPlane
from bespokelabs.sandbox.control_plane.store import SQLiteStore
from bespokelabs.sandbox.types import SandboxResult


class Clock:

    def __init__(self) -> None:
        self.value = datetime(2026, 9, 4, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


class Runtime:

    def __init__(self, backend: str, resource_id: str) -> None:
        self.backend_name = backend
        self.provider_resource_id = resource_id
        self.destroyed = False

    def execute_code(
        self, code: str, language: str = "python"
    ) -> SandboxResult:
        return SandboxResult(stdout=f"{language}:{code}", exit_code=0)

    def execute_command(
        self, command: str, args: list[str] | None = None
    ) -> SandboxResult:
        return SandboxResult(stdout=" ".join([command, *(args or [])]))

    def estimate_compute_cost(self, elapsed_secs: float) -> float:
        return elapsed_secs / 3600

    def destroy(self) -> None:
        self.destroyed = True


class Factory:

    def __init__(self) -> None:
        self.created: list[Runtime] = []

    def __call__(self, backend: str, **_: object) -> Runtime:
        runtime = Runtime(backend, f"fixture-{backend}-{len(self.created) + 1}")
        self.created.append(runtime)
        return runtime


class Reconciler:

    def __init__(self) -> None:
        self.by_org: dict[str, list[ProviderObservation]] = {}

    def list_resources(self, organization_id: str) -> list[ProviderObservation]:
        return self.by_org.get(organization_id, [])


@unittest.skipIf(TestClient is None, "server dependencies are not installed")
class ProductionSessionTest(unittest.TestCase):

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "sessions.db"
        self.store = SQLiteStore(self.path, key_pepper="sessions")
        self.clock = Clock()
        self.factory = Factory()
        self.service = ControlPlane(
            self.store,
            sandbox_factory=self.factory,
            allowed_backends={"daytona"},
            now=self.clock,
            supervision_interval_secs=0,
        )
        _, self.key = self.service.bootstrap_organization("Session org")
        self.client = TestClient(
            create_app(
                self.service,
                session_cookie_secure=True,
                enable_local_dashboard_login=True,
            ),
            base_url="https://testserver",
        )

    def tearDown(self) -> None:
        self.client.close()
        self.service.close()
        self.temp_dir.cleanup()

    def login(self) -> str:
        response = self.client.post(
            "/v1/dashboard/session", json={"api_key": self.key.secret}
        )
        self.assertEqual(response.status_code, 200)
        return self.client.get("/v1/session").json()["csrf_token"]

    def test_http_only_session_csrf_security_headers_and_local_path(
        self,
    ) -> None:
        dashboard = self.client.get("/dashboard")
        local = self.client.get("/dashboard/local")
        login = self.client.post(
            "/v1/dashboard/session", json={"api_key": self.key.secret}
        )

        cookie = login.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("secure", cookie)
        self.assertIn("samesite=strict", cookie)
        self.assertNotIn(self.key.secret, login.text)
        self.assertEqual(local.status_code, 200)
        self.assertIn(
            "default-src 'none'", dashboard.headers["content-security-policy"]
        )
        self.assertEqual(dashboard.headers["x-frame-options"], "DENY")
        self.assertEqual(dashboard.headers["x-content-type-options"], "nosniff")
        self.assertEqual(dashboard.headers["referrer-policy"], "no-referrer")
        self.assertIn("camera=()", dashboard.headers["permissions-policy"])

        session = self.client.get("/v1/session")
        csrf = session.json()["csrf_token"]
        denied = self.client.post(
            "/v1/sandboxes",
            json={"backend": "daytona", "timeout_secs": 60},
        )
        created = self.client.post(
            "/v1/sandboxes",
            json={"backend": "daytona", "timeout_secs": 60},
            headers={"X-CSRF-Token": csrf},
        )
        bearer_created = self.client.post(
            "/v1/sandboxes",
            json={"backend": "daytona", "timeout_secs": 60},
            headers={"Authorization": f"Bearer {self.key.secret}"},
        )

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(
            denied.json()["detail"], "CSRF token is missing or invalid"
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(bearer_created.status_code, 201)
        self.assertEqual(len(self.factory.created), 2)
        self.assertNotIn(self.key.secret.encode(), self.path.read_bytes())

        stale_logout = self.client.delete(
            "/v1/dashboard/session", headers={"X-CSRF-Token": "wrong"}
        )
        logout = self.client.delete(
            "/v1/dashboard/session", headers={"X-CSRF-Token": csrf}
        )
        after = self.client.get("/v1/session")
        self.assertEqual(stale_logout.status_code, 403)
        self.assertEqual(logout.status_code, 204)
        self.assertEqual(after.status_code, 401)

    def test_default_app_disables_explicit_local_login_path(self) -> None:
        other = TestClient(
            create_app(self.service), base_url="https://testserver"
        )
        try:
            self.assertEqual(other.get("/dashboard/local").status_code, 404)
        finally:
            other.close()

    def test_paginated_exports_alerts_and_audit_are_tenant_scoped(self) -> None:
        headers = {"Authorization": f"Bearer {self.key.secret}"}
        created = self.client.post(
            "/v1/sandboxes",
            json={"backend": "daytona", "timeout_secs": 600},
            headers=headers,
        ).json()
        for index in range(2):
            self.client.post(
                f"/v1/sandboxes/{created['id']}/execute",
                json={"code": f"print({index})"},
                headers={**headers, "Idempotency-Key": f"csv-{index}"},
            )
            self.store.record_alert(
                self.key.organization_id,
                alert_type="test",
                severity="warning",
                message=f"Alert {index}",
                dedupe_key=f"test-{index}",
                created_at=(
                    self.clock.value + timedelta(seconds=index)
                ).isoformat(),
            )

        _, other_key = self.service.bootstrap_organization("Export other")
        other = self.store.authenticate(other_key.secret)
        other_sandbox = self.service.create_sandbox(
            other, "daytona", {"timeout_secs": 600}
        )
        self.service.execute(
            other,
            other_sandbox.id,
            code="print('other')",
            request_id="other-usage",
        )

        first = self.client.get(
            "/v1/exports/usage.csv?limit=1", headers=headers
        )
        next_cursor = first.headers["x-next-cursor"]
        second = self.client.get(
            f"/v1/exports/usage.csv?limit=1&cursor={next_cursor}",
            headers=headers,
        )
        first_rows = list(csv.DictReader(io.StringIO(first.text)))
        second_rows = list(csv.DictReader(io.StringIO(second.text)))
        self.assertEqual(first.status_code, 200)
        self.assertEqual(len(first_rows), 1)
        self.assertEqual(len(second_rows), 1)
        self.assertNotEqual(first_rows[0]["id"], second_rows[0]["id"])
        self.assertNotIn(other_sandbox.id, first.text + second.text)
        for kind in ("costs", "ledger"):
            exported = self.client.get(
                f"/v1/exports/{kind}.csv?limit=1", headers=headers
            )
            self.assertEqual(exported.status_code, 200)
            self.assertIn("attachment", exported.headers["content-disposition"])

        alerts = self.client.get("/v1/alerts?limit=1", headers=headers).json()
        next_alerts = self.client.get(
            f"/v1/alerts?limit=1&cursor={alerts['next_cursor']}",
            headers=headers,
        ).json()
        self.assertEqual(len(alerts["items"]), 1)
        self.assertEqual(len(next_alerts["items"]), 1)
        self.assertNotEqual(
            alerts["items"][0]["id"], next_alerts["items"][0]["id"]
        )

        audit = self.client.get("/v1/audit?limit=1", headers=headers).json()
        self.assertIsNotNone(audit["next_cursor"])
        next_audit = self.client.get(
            f"/v1/audit?limit=1&cursor={audit['next_cursor']}",
            headers=headers,
        ).json()
        self.assertEqual(len(next_audit["items"]), 1)

    def test_csv_neutralizes_formula_text_without_corrupting_numbers(
        self,
    ) -> None:
        principal = self.store.authenticate(self.key.secret)
        sandbox = self.service.create_sandbox(
            principal, "daytona", {"timeout_secs": 600}
        )
        dangerous = [
            "=1+1",
            "+1+1",
            "-1+1",
            "@SUM(A1:A2)",
            "\t=1+1",
            "\r=1+1",
        ]
        for value in dangerous:
            self.service.execute(
                principal,
                sandbox.id,
                code="print('safe')",
                request_id=value,
            )

        reconciler = Reconciler()
        self.service._provider_reconcilers["daytona"] = reconciler
        for index, value in enumerate(dangerous, start=1):
            self.clock.value += timedelta(seconds=1)
            reconciler.by_org[principal.organization_id] = [
                ProviderObservation(
                    value,
                    sandbox.provider_resource_id or "",
                    "running",
                    self.clock.value.isoformat(),
                    Decimal(index),
                )
            ]
            self.service.reconcile(principal, "daytona")
        self.clock.value += timedelta(seconds=1)
        reconciler.by_org[principal.organization_id] = [
            ProviderObservation(
                "numeric-negative-delta",
                sandbox.provider_resource_id or "",
                "running",
                self.clock.value.isoformat(),
                Decimal("0"),
            )
        ]
        self.service.reconcile(principal, "daytona")

        currency_observations = []
        for index, value in enumerate(dangerous, start=1):
            currency_sandbox = self.service.create_sandbox(
                principal, "daytona", {"timeout_secs": 600}
            )
            self.clock.value += timedelta(seconds=1)
            currency_observations.append(
                ProviderObservation(
                    f"currency-{index}",
                    currency_sandbox.provider_resource_id or "",
                    "running",
                    self.clock.value.isoformat(),
                    Decimal(index),
                    value,
                )
            )
        reconciler.by_org[principal.organization_id] = currency_observations
        self.service.reconcile(principal, "daytona")

        headers = {"Authorization": f"Bearer {self.key.secret}"}
        usage_rows = list(
            csv.DictReader(
                io.StringIO(
                    self.client.get(
                        "/v1/exports/usage.csv?limit=1000", headers=headers
                    ).text
                )
            )
        )
        ledger_rows = list(
            csv.DictReader(
                io.StringIO(
                    self.client.get(
                        "/v1/exports/ledger.csv?limit=1000", headers=headers
                    ).text
                )
            )
        )
        cost_rows = list(
            csv.DictReader(
                io.StringIO(
                    self.client.get(
                        "/v1/exports/costs.csv?limit=1000", headers=headers
                    ).text
                )
            )
        )

        escaped = {f"'{value}" for value in dangerous}
        self.assertTrue(
            escaped.issubset({row["request_id"] for row in usage_rows})
        )
        ledger_references = {row["reference_id"] for row in ledger_rows}
        self.assertTrue(
            all(
                any(
                    reference.startswith(f"'{value}")
                    for reference in ledger_references
                )
                for value in dangerous
            )
        )
        negative = next(
            row
            for row in ledger_rows
            if row["reference_id"].startswith("numeric-negative-delta")
            and row["provider_delta_usd"].startswith("-")
        )
        self.assertTrue(negative["provider_delta_usd"].startswith("-"))
        self.assertFalse(negative["provider_delta_usd"].startswith("'-"))
        self.assertTrue(
            escaped.issubset({row["currency"] for row in cost_rows})
        )

    def test_operational_scopes_and_cursor_validation(self) -> None:
        principal = self.store.authenticate(self.key.secret)
        observer = self.service.issue_api_key(
            principal,
            name="Operational observer",
            scopes=[
                "alerts:read",
                "audit:read",
                "exports:read",
                "retention:read",
            ],
        )
        headers = {"Authorization": f"Bearer {observer.secret}"}

        self.assertEqual(
            self.client.get(
                "/v1/alerts/configuration", headers=headers
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                "/v1/retention/current", headers=headers
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get("/v1/audit", headers=headers).status_code, 200
        )
        self.assertEqual(
            self.client.get(
                "/v1/exports/ledger.csv", headers=headers
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.put(
                "/v1/alerts/configuration", json={}, headers=headers
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/v1/retention/apply", headers=headers
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                "/v1/audit?cursor=not-a-cursor", headers=headers
            ).status_code,
            422,
        )


@unittest.skipIf(TestClient is None, "server dependencies are not installed")
class ProductionEndToEndTest(unittest.TestCase):

    def test_session_launch_reconcile_terminate_deny_export_and_audit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "e2e.db"
            store = SQLiteStore(path, key_pepper="e2e")
            clock = Clock()
            factory = Factory()
            reconciler = Reconciler()
            service = ControlPlane(
                store,
                sandbox_factory=factory,
                allowed_backends={"daytona"},
                provider_reconcilers={"daytona": reconciler},
                now=clock,
                supervision_interval_secs=0,
            )
            _, key = service.bootstrap_organization("Production fixture")
            client = TestClient(
                create_app(service, session_cookie_secure=True),
                base_url="https://testserver",
            )
            try:
                login = client.post(
                    "/v1/dashboard/session", json={"api_key": key.secret}
                )
                self.assertEqual(login.status_code, 200)
                session = client.get("/v1/session").json()
                csrf = session["csrf_token"]

                launched = client.post(
                    "/v1/sandboxes",
                    json={"backend": "daytona", "timeout_secs": 600},
                    headers={"X-CSRF-Token": csrf},
                )
                self.assertEqual(launched.status_code, 201)
                sandbox = launched.json()
                live = client.get("/v1/sandboxes").json()
                self.assertEqual(live[0]["status"], "running")

                clock.value += timedelta(minutes=5)
                reconciler.by_org[key.organization_id] = [
                    ProviderObservation(
                        "e2e-cost",
                        sandbox["provider_resource_id"],
                        "running",
                        clock.value.isoformat(),
                        Decimal("0.125"),
                    )
                ]
                reconciled = client.post(
                    "/v1/reconciliation/daytona",
                    headers={"X-CSRF-Token": csrf},
                )
                self.assertEqual(reconciled.status_code, 200)
                detail = client.get(
                    f"/v1/sandboxes/{sandbox['id']}/detail"
                ).json()
                self.assertEqual(detail["sandbox"]["cost_state"], "reconciled")
                self.assertEqual(
                    detail["cost"]["provider_reported_cost_usd"], "0.125"
                )

                current = client.get(f"/v1/sandboxes/{sandbox['id']}").json()
                terminated = client.delete(
                    f"/v1/sandboxes/{sandbox['id']}",
                    headers={
                        "X-CSRF-Token": csrf,
                        "If-Match": str(current["version"]),
                    },
                )
                self.assertEqual(terminated.status_code, 200)
                self.assertEqual(terminated.json()["status"], "destroyed")

                policy = client.put(
                    "/v1/policies/current",
                    json={"max_concurrent_sandboxes": 0},
                    headers={"X-CSRF-Token": csrf},
                )
                denied = client.post(
                    "/v1/sandboxes",
                    json={"backend": "daytona", "timeout_secs": 60},
                    headers={"X-CSRF-Token": csrf},
                )
                self.assertEqual(policy.status_code, 200)
                self.assertEqual(denied.status_code, 403)
                self.assertEqual(
                    denied.json()["detail"]["code"], "policy_denied"
                )

                exported = client.get("/v1/exports/costs.csv?limit=1")
                self.assertEqual(exported.status_code, 200)
                self.assertIn("sandbox_id,backend", exported.text)
                audit = client.get("/v1/audit?limit=50").json()["items"]
                actions = {item["action"] for item in audit}
                self.assertTrue(
                    {
                        "dashboard.login",
                        "sandbox.create",
                        "provider.reconcile",
                        "sandbox.terminate",
                        "policy.update",
                        "get /v1/exports/{kind}.csv",
                    }.issubset(actions)
                )
                self.assertNotIn(key.secret, exported.text + str(audit))
                self.assertNotIn(key.secret.encode(), path.read_bytes())
            finally:
                client.close()
                service.close()


class AlertRetentionAndExportTest(unittest.TestCase):

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "operations.db"
        self.store = SQLiteStore(self.path, key_pepper="operations")
        self.clock = Clock()
        self.factory = Factory()
        self.service = ControlPlane(
            self.store,
            sandbox_factory=self.factory,
            allowed_backends={"daytona"},
            provider_settings={"daytona": {"token": "never-persist-this"}},
            provider_health_checks={"daytona": lambda _: False},
            provider_terminators={
                "daytona": lambda _: (_ for _ in ()).throw(
                    RuntimeError("credential never-persist-this")
                )
            },
            now=self.clock,
            supervision_interval_secs=0,
        )
        self.org, self.key = self.service.bootstrap_organization("Alerts org")
        self.principal = self.store.authenticate(self.key.secret)

    def tearDown(self) -> None:
        self.service.close()
        self.temp_dir.cleanup()

    def test_all_alert_types_are_tenant_scoped_and_idempotent(self) -> None:
        self.service.set_alert_configuration(
            self.principal,
            budget_threshold_percent=10,
            repeated_failures_count=1,
            repeated_failures_window_minutes=60,
            provider_degradation_enabled=True,
            long_running_secs=60,
            failed_cleanup_enabled=True,
        )
        long_running = self.service.create_sandbox(
            self.principal, "daytona", {"timeout_secs": 3600}
        )
        expired = self.service.create_sandbox(
            self.principal, "daytona", {"timeout_secs": 10}
        )
        failed = self.service.create_sandbox(
            self.principal, "daytona", {"timeout_secs": 3600}
        )
        self.service._runtimes.pop(expired.id)
        self.service._runtimes.pop(failed.id)
        self.clock.value += timedelta(seconds=120)
        self.store.mark_lifecycle(
            failed.id, "failed", self.clock.value.isoformat()
        )
        self.service.set_policy(
            self.principal,
            max_concurrent_sandboxes=None,
            hourly_spend_limit_usd=Decimal("0.01"),
            daily_spend_limit_usd=Decimal("1"),
            allowed_backends=None,
            allowed_gpu_types=None,
            max_sandbox_lifetime_secs=None,
        )
        self.service.cost_summary(self.principal)
        self.service.check_provider_health(self.principal, "daytona")
        self.service.run_watchdog_once()
        items, _ = self.service.alert_history(
            self.principal, offset=0, limit=50
        )
        repeated, _ = self.service.alert_history(
            self.principal, offset=0, limit=50
        )

        alert_types = {item["alert_type"] for item in items}
        self.assertEqual(
            alert_types,
            {
                "budget_threshold",
                "repeated_failures",
                "provider_degradation",
                "long_running",
                "failed_cleanup",
            },
        )
        self.assertEqual(len(repeated), len(items))
        self.assertIn(long_running.id, {item["resource_id"] for item in items})
        _, other_key = self.service.bootstrap_organization("Other tenant")
        other = self.store.authenticate(other_key.secret)
        other_items, _ = self.service.alert_history(other, offset=0, limit=50)
        self.assertEqual(other_items, [])
        self.assertNotIn(b"never-persist-this", self.path.read_bytes())

    def test_provider_degradation_alerts_enabled_tenants_on_transition(
        self,
    ) -> None:
        _, enabled_key = self.service.bootstrap_organization("Enabled tenant")
        enabled = self.store.authenticate(enabled_key.secret)
        _, disabled_key = self.service.bootstrap_organization("Disabled tenant")
        disabled = self.store.authenticate(disabled_key.secret)
        self.service.set_alert_configuration(
            disabled,
            budget_threshold_percent=None,
            repeated_failures_count=None,
            repeated_failures_window_minutes=60,
            provider_degradation_enabled=False,
            long_running_secs=None,
            failed_cleanup_enabled=True,
        )

        self.service.check_provider_health(self.principal, "daytona")
        self.clock.value += timedelta(minutes=5)
        self.service.check_provider_health(self.principal, "daytona")

        enabled_alerts, _ = self.service.alert_history(
            enabled, offset=0, limit=10
        )
        disabled_alerts, _ = self.service.alert_history(
            disabled, offset=0, limit=10
        )
        self.assertEqual(
            [item["alert_type"] for item in enabled_alerts],
            ["provider_degradation"],
        )
        self.assertEqual(disabled_alerts, [])

        self.service._provider_health_checks["daytona"] = lambda _: True
        self.service.check_provider_health(self.principal, "daytona")
        self.clock.value += timedelta(minutes=5)
        self.service._provider_health_checks["daytona"] = lambda _: False
        self.service.check_provider_health(self.principal, "daytona")
        enabled_alerts, _ = self.service.alert_history(
            enabled, offset=0, limit=10
        )
        self.assertEqual(len(enabled_alerts), 2)

    def test_csv_pages_audit_redaction_and_retention(self) -> None:
        sandbox = self.service.create_sandbox(
            self.principal, "daytona", {"timeout_secs": 3600}
        )
        self.service.execute(
            self.principal,
            sandbox.id,
            code="print(1)",
            request_id="export-1",
        )
        self.service.execute(
            self.principal,
            sandbox.id,
            code="print(2)",
            request_id="export-2",
        )
        columns, first, next_offset = self.service.export_rows(
            self.principal, kind="usage", offset=0, limit=1
        )
        _, second, final_offset = self.service.export_rows(
            self.principal,
            kind="usage",
            offset=next_offset or 0,
            limit=1,
        )
        self.assertEqual(columns[0], "id")
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertNotEqual(first[0]["id"], second[0]["id"])
        self.assertIsNone(final_offset)

        old = (self.clock.value - timedelta(days=500)).isoformat()
        self.store.record_audit(
            self.org["id"],
            api_key_id=self.key.id,
            action="secret.redaction",
            outcome="success",
            details={
                "api_key": self.key.secret,
                "nested": {"password": "unsafe", "message": self.key.secret},
            },
            created_at=old,
        )
        audit, _ = self.service.audit_history(
            self.principal, offset=0, limit=10
        )
        serialized = str(audit)
        self.assertNotIn(self.key.secret, serialized)
        self.assertIn("[REDACTED]", serialized)

        self.store.record_alert(
            self.org["id"],
            alert_type="old",
            severity="warning",
            message="Old operational event.",
            dedupe_key="old-alert",
            created_at=old,
        )
        self.service.set_retention_policy(
            self.principal, operational_days=30, audit_days=365
        )
        deleted = self.service.apply_retention(self.principal)
        self.assertEqual(deleted["alerts_deleted"], 1)
        self.assertEqual(deleted["audit_deleted"], 1)
        self.assertNotIn(self.key.secret.encode(), self.path.read_bytes())

        costs_columns, costs, _ = self.service.export_rows(
            self.principal, kind="costs", offset=0, limit=10
        )
        ledger_columns, ledger, _ = self.service.export_rows(
            self.principal, kind="ledger", offset=0, limit=10
        )
        self.assertIn("customer_cost_usd", costs_columns)
        self.assertTrue(costs)
        self.assertIn("customer_delta_usd", ledger_columns)
        self.assertTrue(ledger)


if __name__ == "__main__":
    unittest.main()
