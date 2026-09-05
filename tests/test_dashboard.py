"""Operational dashboard asset and API acceptance tests."""

from __future__ import annotations

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

from bespokelabs.sandbox.control_plane.service import ControlPlane
from bespokelabs.sandbox.control_plane.store import SQLiteStore
from bespokelabs.sandbox.types import SandboxResult


class DashboardFakeSandbox:
    """Deterministic, non-billable-provider runtime used by dashboard tests."""

    def __init__(self, backend: str, index: int, **config: object) -> None:
        self.backend_name = backend
        self.config = config
        self.provider_resource_id = f"fake-resource-{index}"
        self.destroyed = False
        self.fail_destroy = False

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
        if self.fail_destroy:
            raise RuntimeError("deterministic provider termination failure")
        self.destroyed = True


class DashboardFakeFactory:

    def __init__(self) -> None:
        self.created: list[DashboardFakeSandbox] = []

    def __call__(self, backend: str, **config: object) -> DashboardFakeSandbox:
        runtime = DashboardFakeSandbox(backend, len(self.created) + 1, **config)
        self.created.append(runtime)
        return runtime


@unittest.skipIf(TestClient is None, "server dependencies are not installed")
class DashboardTest(unittest.TestCase):

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = SQLiteStore(
            Path(self.temp_dir.name) / "dashboard.db",
            key_pepper="dashboard-test",
        )
        self.now = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
        self.factory = DashboardFakeFactory()
        self.service = ControlPlane(
            self.store,
            sandbox_factory=self.factory,
            allowed_backends={"daytona", "local"},
            now=lambda: self.now,
        )
        _, self.key = self.service.bootstrap_organization("Dashboard org")
        self.headers = {"Authorization": f"Bearer {self.key.secret}"}
        read_key = self.service.issue_api_key(
            self.store.authenticate(self.key.secret),
            name="Observers",
            scopes=["sandboxes:read", "usage:read"],
        )
        self.read_headers = {"Authorization": f"Bearer {read_key.secret}"}
        self.client = TestClient(create_app(self.service, admin_token="admin"))

    def tearDown(self) -> None:
        self.client.close()
        self.service.close()
        self.temp_dir.cleanup()

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)

    def create_sandbox(self) -> dict:
        response = self.client.post(
            "/v1/sandboxes",
            json={
                "backend": "daytona",
                "cpu": 4,
                "memory_mb": 8192,
                "gpu": "A10G",
                "timeout_secs": 900,
            },
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 201)
        return response.json()

    def test_dashboard_routes_and_operational_assets(self) -> None:
        paths = {route.path for route in self.client.app.routes}
        self.assertIn("/", paths)
        self.assertIn("/dashboard", paths)
        self.assertIn("/dashboard/assets", paths)

        dashboard = self.client.get("/dashboard")
        script = self.client.get("/dashboard/assets/dashboard.js")
        stylesheet = self.client.get("/dashboard/assets/dashboard.css")
        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(script.status_code, 200)
        self.assertIn("Active resources", dashboard.text)
        self.assertIn("Budget &amp; quota", dashboard.text)
        self.assertIn("Provider health", dashboard.text)
        self.assertIn("Recent denials", dashboard.text)
        self.assertIn('id="guardrails"', dashboard.text)
        self.assertIn('id="statusFilter"', dashboard.text)
        self.assertIn('id="detailDialog"', dashboard.text)
        self.assertIn('id="confirmDialog"', dashboard.text)
        self.assertIn('id="launchForm"', dashboard.text)
        self.assertIn('id="activity"', dashboard.text)
        self.assertIn('id="alertList"', dashboard.text)
        self.assertIn('id="auditList"', dashboard.text)
        self.assertIn("export-button", dashboard.text)
        self.assertIn("default-src 'none'", dashboard.text)
        self.assertIn('api("/v1/session")', script.text)
        self.assertIn('fetch("/v1/dashboard/session"', script.text)
        self.assertIn('method: "DELETE"', script.text)
        self.assertIn('headers["X-CSRF-Token"]', script.text)
        self.assertIn(
            'window.location.pathname === "/dashboard/local"', script.text
        )
        self.assertIn('api("/v1/alerts?limit=8")', script.text)
        self.assertIn('api("/v1/audit?limit=8")', script.text)
        self.assertIn("/v1/exports/${encodeURIComponent", script.text)
        self.assertIn('"If-Match"', script.text)
        self.assertIn("document.hidden", script.text)
        self.assertIn("renderSandboxes", script.text)
        self.assertIn("renderGovernance", script.text)
        self.assertIn('api("/v1/policy-summary")', script.text)
        self.assertIn("/health-check", script.text)
        self.assertIn(".governance-grid", stylesheet.text)
        self.assertIn(".activity-grid", stylesheet.text)
        self.assertIn(".launch-form", stylesheet.text)
        self.assertIn(".quota-track", stylesheet.text)
        self.assertIn("@media (max-width: 780px)", stylesheet.text)
        self.assertIn(":focus-visible", stylesheet.text)

    def test_session_creator_detail_and_history_contract(self) -> None:
        created = self.create_sandbox()
        self.advance(60)
        execution = self.client.post(
            f"/v1/sandboxes/{created['id']}/execute",
            json={"code": "print('dashboard')"},
            headers=self.headers,
        )
        self.assertEqual(execution.status_code, 200)
        self.store.record_provider_observation(
            self.key.organization_id,
            created["id"],
            observation_id="browser-observation-1",
            provider_resource_id=created["provider_resource_id"],
            status="running",
            observed_at=self.now.isoformat(),
            provider_cost_usd=Decimal("0.02"),
            currency="USD",
        )

        session = self.client.get("/v1/session", headers=self.headers)
        detail = self.client.get(
            f"/v1/sandboxes/{created['id']}/detail",
            headers=self.headers,
        )

        self.assertTrue(session.json()["can_terminate"])
        payload = detail.json()
        self.assertEqual(payload["sandbox"]["creator_api_key_id"], self.key.id)
        self.assertEqual(
            payload["sandbox"]["creator_api_key_name"], "Initial key"
        )
        self.assertEqual(payload["sandbox"]["config"]["gpu"], "A10G")
        self.assertEqual(len(payload["attempts"]), 1)
        self.assertEqual(len(payload["executions"]), 1)
        self.assertEqual(payload["executions"][0]["status"], "completed")
        self.assertEqual(len(payload["provider_observations"]), 1)
        self.assertGreater(Decimal(payload["cost"]["effective_cost_usd"]), 0)
        self.assertNotIn("organization_id", detail.text)
        self.assertNotIn("error_json", detail.text)
        self.assertNotIn("response_json", detail.text)

    def test_termination_is_optimistic_idempotent_and_finalizes_cost(
        self,
    ) -> None:
        created = self.create_sandbox()
        self.advance(120)
        stale = self.client.delete(
            f"/v1/sandboxes/{created['id']}",
            headers={**self.headers, "If-Match": str(created["version"] - 1)},
        )
        self.assertEqual(stale.status_code, 409)
        self.assertFalse(self.factory.created[0].destroyed)

        terminated = self.client.delete(
            f"/v1/sandboxes/{created['id']}",
            headers={**self.headers, "If-Match": f'"{created["version"]}"'},
        )
        repeated = self.client.delete(
            f"/v1/sandboxes/{created['id']}",
            headers={**self.headers, "If-Match": f'"{created["version"]}"'},
        )
        detail = self.client.get(
            f"/v1/sandboxes/{created['id']}/detail",
            headers=self.headers,
        ).json()

        self.assertEqual(terminated.status_code, 200)
        self.assertEqual(terminated.json()["status"], "destroyed")
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(repeated.json(), terminated.json())
        self.assertTrue(self.factory.created[0].destroyed)
        self.assertIsNotNone(detail["sandbox"]["stopping_at"])
        self.assertIsNotNone(detail["sandbox"]["terminated_at"])
        self.assertGreater(Decimal(detail["cost"]["effective_cost_usd"]), 0)

    def test_termination_failure_is_visible_and_structured(self) -> None:
        created = self.create_sandbox()
        self.factory.created[0].fail_destroy = True
        self.advance(30)

        response = self.client.delete(
            f"/v1/sandboxes/{created['id']}",
            headers={**self.headers, "If-Match": str(created["version"])},
        )
        detail = self.client.get(
            f"/v1/sandboxes/{created['id']}/detail",
            headers=self.headers,
        ).json()

        self.assertEqual(response.status_code, 502)
        self.assertEqual(detail["sandbox"]["status"], "failed")
        self.assertEqual(detail["sandbox"]["latest_error"]["op"], "destroy")
        self.assertIsNotNone(detail["sandbox"]["failed_at"])

    def test_read_only_key_can_inspect_but_cannot_terminate(self) -> None:
        created = self.create_sandbox()
        session = self.client.get("/v1/session", headers=self.read_headers)
        listing = self.client.get("/v1/sandboxes", headers=self.read_headers)
        detail = self.client.get(
            f"/v1/sandboxes/{created['id']}/detail",
            headers=self.read_headers,
        )
        denied = self.client.delete(
            f"/v1/sandboxes/{created['id']}",
            headers={**self.read_headers, "If-Match": str(created["version"])},
        )

        self.assertEqual(session.status_code, 200)
        self.assertFalse(session.json()["can_terminate"])
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(denied.status_code, 403)
        self.assertFalse(self.factory.created[0].destroyed)


if __name__ == "__main__":
    unittest.main()
