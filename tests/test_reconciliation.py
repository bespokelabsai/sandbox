"""Deterministic lifecycle-metering and reconciliation tests."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from bespokelabs.sandbox.control_plane.reconciliation import ProviderObservation
from bespokelabs.sandbox.control_plane.service import ControlPlane
from bespokelabs.sandbox.control_plane.store import SQLiteStore
from bespokelabs.sandbox.exceptions import ErrorOutcome, SandboxTimeoutError
from bespokelabs.sandbox.types import SandboxResult


class _Clock:

    def __init__(self, *values: datetime) -> None:
        self.values = list(values)
        self.last = values[-1]

    def __call__(self) -> datetime:
        if self.values:
            self.last = self.values.pop(0)
        return self.last


class _Runtime:
    backend_name = "daytona"

    def __init__(self, resource_id: str) -> None:
        self.provider_resource_id = resource_id

    def execute_code(
        self, code: str, language: str = "python"
    ) -> SandboxResult:
        return SandboxResult(stdout=code)

    def execute_command(self, command: str, args=None) -> SandboxResult:
        return SandboxResult(stdout=command)

    def estimate_compute_cost(self, elapsed_secs: float) -> float:
        return 0

    def destroy(self) -> None:
        pass


class _Factory:

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def __call__(self, backend: str, **config: object) -> _Runtime:
        del config
        self.calls += 1
        if self.fail:
            raise SandboxTimeoutError(
                "opaque timeout",
                backend=backend,
                op="create",
                context={"cleanup_status": "deleted"},
                outcome=ErrorOutcome.FAILED,
            )
        return _Runtime(f"resource-{self.calls}")


class _Reconciler:

    def __init__(self) -> None:
        self.by_org: dict[str, list[ProviderObservation]] = {}
        self.failures = 0
        self.calls: list[str] = []

    def list_resources(self, organization_id: str) -> list[ProviderObservation]:
        self.calls.append(organization_id)
        if self.failures:
            self.failures -= 1
            raise ConnectionError("provider unavailable")
        return self.by_org.get(organization_id, [])


class LifecycleReconciliationTest(unittest.TestCase):

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = SQLiteStore(
            Path(self.temp_dir.name) / "phase2.db", key_pepper="phase2"
        )
        self.base = datetime(2026, 1, 1, tzinfo=UTC)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _service(self, clock: _Clock, factory=None, reconciler=None):
        service = ControlPlane(
            self.store,
            sandbox_factory=factory or _Factory(),
            allowed_backends={"daytona"},
            provider_reconcilers={"daytona": reconciler or _Reconciler()},
            now=clock,
            customer_markup=Decimal("1.25"),
        )
        _, key = service.bootstrap_organization("Acme")
        return service, self.store.authenticate(key.secret)

    def test_failed_provisioning_accrues_nonzero_lifecycle_cost(self) -> None:
        clock = _Clock(self.base, self.base + timedelta(minutes=10))
        service, principal = self._service(clock, factory=_Factory(fail=True))

        with self.assertRaises(SandboxTimeoutError):
            service.create_sandbox(principal, "daytona", {})

        record = service.list_sandboxes(principal)[0]
        summary = service.cost_summary(principal)[0]
        self.assertEqual(record.requested_at, self.base.isoformat())
        self.assertEqual(record.provisioning_at, self.base.isoformat())
        self.assertEqual(
            record.failed_at, (self.base + timedelta(minutes=10)).isoformat()
        )
        self.assertEqual(record.cost_state, "estimated")
        self.assertEqual(Decimal(record.hourly_rate_usd), Decimal("0.054"))
        self.assertEqual(record.currency, "USD")
        self.assertEqual(record.pricing_source, "2026-03-28")
        self.assertEqual(Decimal(summary["runtime_seconds"]), Decimal("600.0"))
        self.assertGreater(Decimal(summary["provider_cost_usd"]), 0)
        with sqlite3.connect(self.store._path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM lifecycle_ledger_entries"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_provider_cost_replaces_estimate_without_double_counting(
        self,
    ) -> None:
        observed = self.base + timedelta(hours=1)
        clock = _Clock(self.base, self.base, observed)
        reconciler = _Reconciler()
        service, principal = self._service(clock, reconciler=reconciler)
        sandbox = service.create_sandbox(principal, "daytona", {})
        service.cost_summary(principal)
        reconciler.by_org[principal.organization_id] = [
            ProviderObservation(
                "obs-1",
                sandbox.provider_resource_id or "",
                "running",
                observed.isoformat(),
                Decimal("0.123456789012345678"),
            )
        ]

        first = service.reconcile(principal, "daytona")
        second = service.reconcile(principal, "daytona")
        summary = service.cost_summary(principal)[0]

        self.assertEqual(first["updated"], 1)
        self.assertEqual(second["updated"], 1)
        self.assertEqual(
            Decimal(summary["provider_cost_usd"]),
            Decimal("0.123456789012345678"),
        )
        refreshed = service.get_sandbox(principal, sandbox.id)
        self.assertEqual(refreshed.cost_state, "reconciled")
        self.assertEqual(refreshed.pricing_source, "provider")
        self.assertEqual(
            refreshed.last_provider_observed_at, observed.isoformat()
        )
        with sqlite3.connect(self.store._path) as connection:
            deltas = connection.execute(
                "SELECT provider_delta_usd FROM lifecycle_ledger_entries"
            ).fetchall()
        self.assertEqual(
            sum((Decimal(row[0]) for row in deltas), Decimal(0)),
            Decimal("0.123456789012345678"),
        )
        self.assertEqual(len(deltas), 2)

        later = observed + timedelta(hours=1)
        reconciler.by_org[principal.organization_id] = [
            ProviderObservation(
                "obs-status-only",
                sandbox.provider_resource_id or "",
                "running",
                later.isoformat(),
            )
        ]
        service.reconcile(principal, "daytona")
        service.reconcile(principal, "daytona")
        preserved = service.cost_summary(principal)[0]
        refreshed = service.get_sandbox(principal, sandbox.id)
        self.assertEqual(
            Decimal(preserved["provider_cost_usd"]),
            Decimal("0.123456789012345678"),
        )
        self.assertEqual(refreshed.cost_state, "reconciled")
        self.assertEqual(refreshed.pricing_source, "provider")
        with sqlite3.connect(self.store._path) as connection:
            after = connection.execute(
                "SELECT provider_delta_usd FROM lifecycle_ledger_entries"
            ).fetchall()
        self.assertEqual(
            sum((Decimal(row[0]) for row in after), Decimal(0)),
            Decimal("0.123456789012345678"),
        )
        self.assertEqual(len(after), 3)

    def test_partial_outage_retry_disappearance_orphan_and_tenant_safety(
        self,
    ) -> None:
        clock = _Clock(self.base, self.base, self.base + timedelta(minutes=5))
        reconciler = _Reconciler()
        service, principal = self._service(clock, reconciler=reconciler)
        sandbox = service.create_sandbox(principal, "daytona", {})
        _, other_key = service.bootstrap_organization("Other")
        other = self.store.authenticate(other_key.secret)
        other_sandbox = service.create_sandbox(other, "daytona", {})

        reconciler.failures = 1
        self.assertEqual(
            service.reconcile(principal, "daytona")["status"], "partial_outage"
        )
        self.assertIsNone(
            service.get_sandbox(principal, sandbox.id).last_provider_observed_at
        )
        reconciler.by_org[principal.organization_id] = [
            ProviderObservation(
                "orphan-1",
                "untracked-resource",
                "running",
                self.base.isoformat(),
            )
        ]
        result = service.reconcile(principal, "daytona")

        self.assertEqual(result["missing"], 1)
        self.assertEqual(result["orphans"], 1)
        self.assertTrue(
            service.get_sandbox(principal, sandbox.id).provider_missing
        )
        self.assertEqual(
            service.get_sandbox(principal, sandbox.id).status, "failed"
        )
        self.assertGreater(
            Decimal(service.cost_summary(principal)[0]["provider_cost_usd"]),
            0,
        )
        untouched = service.get_sandbox(other, other_sandbox.id)
        self.assertFalse(untouched.provider_missing)
        self.assertEqual(untouched.status, "running")
        self.assertEqual(reconciler.calls, [principal.organization_id] * 2)

    def test_clock_reversal_clamps_billable_duration_to_zero(self) -> None:
        clock = _Clock(self.base, self.base, self.base - timedelta(seconds=1))
        service, principal = self._service(clock)
        service.create_sandbox(principal, "daytona", {})

        summary = service.cost_summary(principal)[0]

        self.assertEqual(Decimal(summary["runtime_seconds"]), Decimal(0))
        self.assertEqual(Decimal(summary["provider_cost_usd"]), Decimal(0))

    def test_destroy_records_stopping_terminated_and_full_billable_window(
        self,
    ) -> None:
        running = self.base + timedelta(seconds=2)
        stopping = self.base + timedelta(minutes=30)
        terminated = stopping + timedelta(seconds=3)
        clock = _Clock(self.base, running, stopping, terminated)
        service, principal = self._service(clock)
        sandbox = service.create_sandbox(principal, "daytona", {})

        record = service.destroy_sandbox(principal, sandbox.id)
        summary = service.cost_summary(principal)[0]

        self.assertEqual(record.running_at, running.isoformat())
        self.assertEqual(record.stopping_at, stopping.isoformat())
        self.assertEqual(record.terminated_at, terminated.isoformat())
        self.assertEqual(record.status, "destroyed")
        self.assertEqual(Decimal(summary["runtime_seconds"]), Decimal("1803.0"))

    def test_reconciliation_preserves_terminal_state_and_billable_window(
        self,
    ) -> None:
        terminated = self.base + timedelta(minutes=5)
        much_later = self.base + timedelta(days=2)
        clock = _Clock(self.base, self.base, terminated, terminated, much_later)
        reconciler = _Reconciler()
        service, principal = self._service(clock, reconciler=reconciler)
        sandbox = service.create_sandbox(principal, "daytona", {})
        terminal = service.destroy_sandbox(principal, sandbox.id)
        before = service.cost_summary(principal)[0]

        result = service.reconcile(principal, "daytona")
        after = service.cost_summary(principal)[0]
        preserved = service.get_sandbox(principal, sandbox.id)

        self.assertEqual(result["missing"], 0)
        self.assertEqual(preserved.status, "destroyed")
        self.assertFalse(preserved.provider_missing)
        self.assertEqual(preserved.terminated_at, terminal.terminated_at)
        self.assertEqual(after, before)

    def test_daily_and_bounded_summaries_are_stable_across_utc_midnight(
        self,
    ) -> None:
        started = datetime(2026, 1, 1, 23, 30, tzinfo=UTC)
        first_refresh = started + timedelta(hours=1)
        second_refresh = started + timedelta(hours=1, minutes=30)
        clock = _Clock(started, started, first_refresh, second_refresh)
        service, principal = self._service(clock)
        service.create_sandbox(principal, "daytona", {})

        service.cost_summary(principal, group_by="day")
        daily = service.cost_summary(principal, group_by="day")
        bounded = service.cost_summary(
            principal,
            start="2026-01-01T00:00:00+00:00",
            end="2026-01-02T00:00:00+00:00",
        )

        self.assertEqual(
            [item["key"] for item in daily],
            ["2026-01-01", "2026-01-02"],
        )
        self.assertEqual(
            Decimal(daily[0]["runtime_seconds"]), Decimal("1800.0")
        )
        self.assertEqual(
            Decimal(daily[1]["runtime_seconds"]), Decimal("3600.0")
        )
        self.assertEqual(
            Decimal(bounded[0]["runtime_seconds"]), Decimal("1800.0")
        )
        self.assertEqual(
            Decimal(bounded[0]["provider_cost_usd"]), Decimal("0.027000")
        )

    def test_transitional_statuses_and_invalid_status_are_strict(self) -> None:
        clock = _Clock(self.base, self.base)
        reconciler = _Reconciler()
        service, principal = self._service(clock, reconciler=reconciler)
        sandbox = service.create_sandbox(principal, "daytona", {})
        resource_id = sandbox.provider_resource_id or ""
        self.store.update_sandbox_status(sandbox.id, "creating")
        provisioning = self.base + timedelta(seconds=30)
        reconciler.by_org[principal.organization_id] = [
            ProviderObservation(
                "provisioning",
                resource_id,
                "provisioning",
                provisioning.isoformat(),
            )
        ]
        service.reconcile(principal, "daytona")
        self.assertEqual(
            service.get_sandbox(principal, sandbox.id).status, "creating"
        )
        stopping = self.base + timedelta(minutes=1)
        reconciler.by_org[principal.organization_id] = [
            ProviderObservation(
                "stop", resource_id, "stopping", stopping.isoformat()
            )
        ]
        service.reconcile(principal, "daytona")
        stopped = service.get_sandbox(principal, sandbox.id)
        self.assertEqual(stopped.status, "stopping")
        self.assertEqual(stopped.stopping_at, stopping.isoformat())

        terminated = self.base + timedelta(minutes=2)
        reconciler.by_org[principal.organization_id] = [
            ProviderObservation(
                "terminated",
                resource_id,
                "terminated",
                terminated.isoformat(),
            )
        ]
        service.reconcile(principal, "daytona")
        terminal = service.get_sandbox(principal, sandbox.id)
        self.assertEqual(terminal.status, "destroyed")
        self.assertEqual(terminal.terminated_at, terminated.isoformat())

        reconciler.by_org[principal.organization_id] = [
            ProviderObservation(
                "bad", resource_id, "mystery", stopping.isoformat()
            )
        ]
        with self.assertRaisesRegex(
            ValueError, "unsupported provider lifecycle status"
        ):
            service.reconcile(principal, "daytona")
        self.assertEqual(
            service.get_sandbox(principal, sandbox.id).status, "destroyed"
        )


if __name__ == "__main__":
    unittest.main()
