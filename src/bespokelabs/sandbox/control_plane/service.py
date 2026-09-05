"""Tenant-aware sandbox lifecycle and metering service."""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from bespokelabs.sandbox import pricing
from bespokelabs.sandbox.backends import BACKENDS
from bespokelabs.sandbox.control_plane.errors import (
    AuthorizationError,
    ConflictError,
    normalize_provider_error,
    provider_error_payload,
)
from bespokelabs.sandbox.control_plane.models import (
    AlertConfiguration,
    IssuedAPIKey,
    OrganizationPolicy,
    Principal,
    RetentionPolicy,
    SandboxRecord,
)
from bespokelabs.sandbox.control_plane.reconciliation import ProviderReconciler
from bespokelabs.sandbox.control_plane.store import SQLiteStore
from bespokelabs.sandbox.exceptions import ErrorOutcome
from bespokelabs.sandbox.sandbox import Sandbox
from bespokelabs.sandbox.types import SandboxResult

_SANDBOX_CONFIG_FIELDS = {
    "preset",
    "cpu",
    "memory_mb",
    "disk_mb",
    "gpu",
    "timeout_secs",
    "image",
    "env_vars",
    "allow_internet",
    "app_name",
    "template",
    "snapshot_id",
    "workdir",
    "git_repo",
    "git_ref",
}


class SandboxRuntime(Protocol):
    """Operations the control plane needs from a live sandbox."""

    backend_name: str

    @property
    def provider_resource_id(self) -> str | None:
        ...

    def execute_code(
        self, code: str, language: str = "python"
    ) -> SandboxResult:
        ...

    def execute_command(
        self, command: str, args: list[str] | None = None
    ) -> SandboxResult:
        ...

    def estimate_compute_cost(self, elapsed_secs: float) -> float:
        ...

    def destroy(self) -> None:
        ...


class ControlPlane:
    """Coordinates authentication, sandbox routing, and usage accounting."""

    def __init__(
        self,
        store: SQLiteStore,
        *,
        sandbox_factory: Callable[..., SandboxRuntime] = Sandbox,
        customer_markup: Decimal | str = Decimal("1"),
        allowed_backends: Iterable[str] | None = None,
        provider_reconcilers: dict[str, ProviderReconciler] | None = None,
        provider_settings: dict[str, dict[str, str]] | None = None,
        provider_health_checks: (
            dict[str, Callable[[dict[str, str]], bool]] | None
        ) = None,
        provider_terminators: dict[str, Callable[[str], None]] | None = None,
        supervision_interval_secs: float = 30,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self._sandbox_factory = sandbox_factory
        self._customer_markup = Decimal(customer_markup)
        if self._customer_markup < 0:
            raise ValueError("customer_markup must be non-negative")
        self._allowed_backends = set(
            BACKENDS if allowed_backends is None else allowed_backends
        )
        self._runtimes: dict[str, SandboxRuntime] = {}
        self._provider_reconcilers = provider_reconcilers or {}
        self._provider_settings = {
            backend: dict(values)
            for backend, values in (provider_settings or {}).items()
        }
        self._provider_health_checks = provider_health_checks or {}
        self._provider_terminators = provider_terminators or {}
        self._supervision_interval_secs = supervision_interval_secs
        self._supervisor_stop = threading.Event()
        self._supervisor_thread: threading.Thread | None = None
        self._supervisor_owner = f"supervisor-{uuid.uuid4().hex}"
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()

    def bootstrap_organization(
        self, name: str
    ) -> tuple[dict[str, str], IssuedAPIKey]:
        organization = self.store.create_organization(name)
        key = self.store.issue_api_key(
            organization["id"], name="Initial key", scopes=("*",)
        )
        return organization, key

    def authenticate(self, secret: str, scope: str) -> Principal:
        principal = self.store.authenticate(secret)
        self.require(principal, scope)
        return principal

    @staticmethod
    def require(principal: Principal, scope: str) -> None:
        if not principal.allows(scope):
            raise AuthorizationError(f"API key requires scope: {scope}")

    @staticmethod
    def require_any(principal: Principal, *scopes: str) -> None:
        if not any(principal.allows(scope) for scope in scopes):
            raise AuthorizationError(
                f"API key requires one of: {', '.join(scopes)}"
            )

    def issue_api_key(
        self,
        principal: Principal,
        *,
        name: str,
        scopes: Iterable[str],
        expires_at: str | None = None,
    ) -> IssuedAPIKey:
        self.require(principal, "keys:write")
        return self.store.issue_api_key(
            principal.organization_id,
            name=name,
            scopes=scopes,
            expires_at=expires_at,
        )

    def create_sandbox(
        self,
        principal: Principal,
        backend: str,
        config: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> SandboxRecord:
        self.require(principal, "sandboxes:create")
        backend = backend.lower().strip()
        if backend not in self._allowed_backends:
            raise ValueError(f"backend is not enabled: {backend}")
        unknown_fields = set(config) - _SANDBOX_CONFIG_FIELDS
        if unknown_fields:
            names = ", ".join(sorted(unknown_fields))
            raise ValueError(f"unsupported sandbox configuration: {names}")
        if idempotency_key is not None and not (
            1 <= len(idempotency_key) <= 255
        ):
            raise ValueError("idempotency key must contain 1 to 255 characters")

        persisted_config = dict(config)
        env_vars = persisted_config.pop("env_vars", None)
        if env_vars:
            persisted_config["env_var_names"] = sorted(env_vars)
        timestamp = self._now().isoformat()
        self._accrue_active(principal, timestamp=timestamp)
        price = pricing.get_backend_pricing(backend) or {}
        hourly_rate = Decimal(str(price.get("vcpu_per_hour_usd", 0))) * Decimal(
            str(config.get("cpu", 1))
        ) + Decimal(str(price.get("ram_gib_per_hour_usd", 0))) * (
            Decimal(str(config.get("memory_mb", 1024))) / Decimal(1024)
        )
        record, claimed = self.store.claim_sandbox_creation(
            principal.organization_id,
            backend,
            persisted_config,
            idempotency_key=idempotency_key,
            request_payload={"backend": backend, **config},
            timestamp=timestamp,
            hourly_rate_usd=hourly_rate,
            pricing_source=pricing.get_pricing()["last_updated"],
            creator_api_key_id=principal.api_key_id,
            enforce_policy=True,
        )
        if not claimed:
            return record
        try:
            runtime = self._sandbox_factory(backend, **config)
        except Exception as exc:
            completed_at = self._now().isoformat()
            provider_error = normalize_provider_error(
                exc, backend=backend, op="create"
            )
            payload = provider_error_payload(provider_error)
            cleanup_status = payload["context"].get("cleanup_status")
            provider_resource_id = payload["context"].get(
                "provider_resource_id"
            )
            retry_status = self._retry_status(provider_error, cleanup_status)
            self.store.complete_sandbox_creation(
                record.id,
                status="failed",
                error=payload,
                retry_status=retry_status,
                cleanup_status=cleanup_status,
                provider_resource_id=provider_resource_id,
                timestamp=completed_at,
            )
            failed = self.store.get_sandbox(
                principal.organization_id, record.id
            )
            self._record_lifecycle_estimate(
                failed, completed_at, observation_id=f"create:{record.id}"
            )
            if provider_error is exc:
                raise
            raise provider_error from exc
        with self._lock:
            self._runtimes[record.id] = runtime
        dynamic_rate = None
        if backend not in {"docker", "local", "ray", "safehouse"}:
            candidate_rate = Decimal(str(runtime.estimate_compute_cost(3600)))
            if candidate_rate > 0:
                dynamic_rate = candidate_rate
        self.store.complete_sandbox_creation(
            record.id,
            status="running",
            provider_resource_id=self._provider_resource_id(runtime),
            timestamp=self._now().isoformat(),
            hourly_rate_usd=dynamic_rate,
        )
        return self.store.get_sandbox(principal.organization_id, record.id)

    def list_sandboxes(self, principal: Principal) -> list[SandboxRecord]:
        self.require(principal, "sandboxes:read")
        return self.store.list_sandboxes(principal.organization_id)

    def get_sandbox(
        self, principal: Principal, sandbox_id: str
    ) -> SandboxRecord:
        self.require(principal, "sandboxes:read")
        return self.store.get_sandbox(principal.organization_id, sandbox_id)

    def execute(
        self,
        principal: Principal,
        sandbox_id: str,
        *,
        code: str | None = None,
        language: str = "python",
        command: str | None = None,
        args: list[str] | None = None,
        request_id: str | None = None,
    ) -> dict:
        self.require(principal, "sandboxes:execute")
        if (code is None) == (command is None):
            raise ValueError("provide exactly one of code or command")
        record = self.store.get_sandbox(principal.organization_id, sandbox_id)
        if record.status != "running":
            raise ConflictError(f"sandbox is {record.status}")
        with self._lock:
            runtime = self._runtimes.get(sandbox_id)
        if runtime is None:
            raise ConflictError(
                "sandbox runtime is unavailable after a control-plane restart"
            )

        request_id = request_id or f"req_{uuid.uuid4().hex}"
        execution_id, cached = self.store.begin_execution(
            principal.organization_id, sandbox_id, request_id
        )
        if cached is not None:
            return cached

        started = time.monotonic()
        try:
            if code is not None:
                result = runtime.execute_code(code, language)
            else:
                result = runtime.execute_command(command or "", args)
            elapsed = time.monotonic() - started
            usage = self._record_usage(
                principal,
                record,
                runtime,
                execution_id,
                request_id,
                elapsed,
            )
            response = {
                "request_id": request_id,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.exit_code,
                "usage": usage,
            }
            self.store.finish_execution(execution_id, response)
            return response
        except Exception as exc:
            elapsed = time.monotonic() - started
            self._record_usage(
                principal,
                record,
                runtime,
                execution_id,
                request_id,
                elapsed,
            )
            provider_error = normalize_provider_error(
                exc, backend=record.backend, op="execute"
            )
            self.store.fail_execution(
                execution_id,
                json.dumps(
                    provider_error_payload(provider_error), sort_keys=True
                ),
            )
            if provider_error is exc:
                raise
            raise provider_error from exc

    def destroy_sandbox(
        self,
        principal: Principal,
        sandbox_id: str,
        *,
        expected_version: int | None = None,
    ) -> SandboxRecord:
        self.require(principal, "sandboxes:terminate")
        record = self.store.get_sandbox(principal.organization_id, sandbox_id)
        if record.status == "destroyed":
            return record
        stopping_at = self._now().isoformat()
        self.store.begin_termination(
            principal.organization_id,
            sandbox_id,
            timestamp=stopping_at,
            expected_version=expected_version,
        )
        with self._lock:
            runtime = self._runtimes.pop(sandbox_id, None)
        if runtime is None:
            raise ConflictError(
                "sandbox runtime is not attached to this process"
            )
        try:
            runtime.destroy()
        except Exception as exc:
            failed_at = self._now().isoformat()
            provider_error = normalize_provider_error(
                exc, backend=record.backend, op="destroy"
            )
            self.store.fail_termination(
                sandbox_id,
                timestamp=failed_at,
                error=provider_error_payload(provider_error),
            )
            self._record_lifecycle_estimate(
                self.store.get_sandbox_internal(sandbox_id),
                failed_at,
                observation_id=f"terminate-failed:{sandbox_id}",
            )
            if provider_error is exc:
                raise
            raise provider_error from exc
        terminated_at = self._now().isoformat()
        self.store.mark_lifecycle(
            sandbox_id,
            "destroyed",
            terminated_at,
            actor_api_key_id=principal.api_key_id,
            reason="operator",
        )
        record = self.store.get_sandbox(principal.organization_id, sandbox_id)
        self._record_lifecycle_estimate(
            record, terminated_at, observation_id=f"terminate:{sandbox_id}"
        )
        return self.store.get_sandbox(principal.organization_id, sandbox_id)

    def sandbox_detail(self, principal: Principal, sandbox_id: str) -> dict:
        self.require(principal, "sandboxes:read")
        self.require(principal, "usage:read")
        self._accrue_active(principal)
        return self.store.sandbox_detail(principal.organization_id, sandbox_id)

    def get_policy(self, principal: Principal) -> OrganizationPolicy:
        self.require(principal, "policies:read")
        return self.store.get_policy(principal.organization_id)

    def set_policy(
        self,
        principal: Principal,
        *,
        max_concurrent_sandboxes: int | None,
        hourly_spend_limit_usd: Decimal | None,
        daily_spend_limit_usd: Decimal | None,
        allowed_backends: Iterable[str] | None,
        allowed_gpu_types: Iterable[str] | None,
        max_sandbox_lifetime_secs: int | None,
    ) -> OrganizationPolicy:
        self.require(principal, "policies:write")
        if (
            max_concurrent_sandboxes is not None
            and max_concurrent_sandboxes < 0
        ):
            raise ValueError("max_concurrent_sandboxes must be non-negative")
        if (
            max_sandbox_lifetime_secs is not None
            and max_sandbox_lifetime_secs < 1
        ):
            raise ValueError("max_sandbox_lifetime_secs must be positive")
        for name, value in (
            ("hourly_spend_limit_usd", hourly_spend_limit_usd),
            ("daily_spend_limit_usd", daily_spend_limit_usd),
        ):
            if value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative")
        normalized_backends = (
            tuple(sorted({item.lower().strip() for item in allowed_backends}))
            if allowed_backends is not None
            else None
        )
        if normalized_backends is not None:
            unknown = set(normalized_backends) - self._allowed_backends
            if unknown:
                raise ValueError(
                    f"backend is not enabled: {sorted(unknown)[0]}"
                )
        normalized_gpus = (
            tuple(sorted({item.strip() for item in allowed_gpu_types}))
            if allowed_gpu_types is not None
            else None
        )
        if normalized_gpus is not None and any(
            not item for item in normalized_gpus
        ):
            raise ValueError("allowed GPU types must not be empty strings")
        return self.store.set_policy(
            principal.organization_id,
            actor_api_key_id=principal.api_key_id,
            max_concurrent_sandboxes=max_concurrent_sandboxes,
            hourly_spend_limit_usd=hourly_spend_limit_usd,
            daily_spend_limit_usd=daily_spend_limit_usd,
            allowed_backends=normalized_backends,
            allowed_gpu_types=normalized_gpus,
            max_sandbox_lifetime_secs=max_sandbox_lifetime_secs,
            timestamp=self._now().isoformat(),
        )

    def policy_summary(self, principal: Principal) -> dict:
        self.require(principal, "policies:read")
        self.require(principal, "usage:read")
        timestamp = self._now().isoformat()
        self._accrue_active(principal, timestamp=timestamp)
        return self.store.policy_summary(
            principal.organization_id, timestamp=timestamp
        )

    def provider_summary(self, principal: Principal) -> list[dict]:
        self.require(principal, "providers:read")
        stored = self.store.list_provider_health()
        backends = sorted(
            self._allowed_backends
            | set(self._provider_settings)
            | set(self._provider_health_checks)
        )
        return [
            self._provider_public_status(backend, stored.get(backend))
            for backend in backends
        ]

    def check_provider_health(self, principal: Principal, backend: str) -> dict:
        self.require(principal, "providers:write")
        backend = backend.lower().strip()
        if backend not in self._allowed_backends:
            raise ValueError(f"backend is not enabled: {backend}")
        previous = self.store.list_provider_health().get(backend)
        settings = self._provider_settings.get(backend, {})
        configured = self._provider_is_configured(backend, settings)
        if not configured:
            status = "unconfigured"
            message = "Provider configuration is incomplete."
        elif backend not in self._provider_health_checks:
            status = "unchecked"
            message = "Provider health check is unavailable."
        else:
            try:
                healthy = bool(
                    self._provider_health_checks[backend](dict(settings))
                )
            except Exception:
                healthy = False
            if healthy:
                status = "healthy"
                message = "Provider health check succeeded."
            else:
                status = "degraded"
                message = "Provider health check failed."
        checked_at = self._now().isoformat()
        self.store.record_provider_health(
            backend,
            status=status,
            checked_at=checked_at,
            message=message,
            actor_api_key_id=principal.api_key_id,
        )
        if status == "degraded" and (
            previous is None or previous["status"] != "degraded"
        ):
            for organization_id in self.store.list_organization_ids():
                configuration = self.store.get_alert_configuration(
                    organization_id
                )
                if configuration.provider_degradation_enabled:
                    self.store.record_alert(
                        organization_id,
                        alert_type="provider_degradation",
                        severity="critical",
                        message=f"Provider {backend} is degraded.",
                        resource_type="provider",
                        resource_id=backend,
                        dedupe_key=(
                            f"provider_degradation:{backend}:{checked_at}"
                        ),
                        created_at=checked_at,
                    )
        return self._provider_public_status(
            backend, self.store.list_provider_health().get(backend)
        )

    def get_alert_configuration(
        self, principal: Principal
    ) -> AlertConfiguration:
        self.require(principal, "alerts:read")
        return self.store.get_alert_configuration(principal.organization_id)

    def set_alert_configuration(
        self,
        principal: Principal,
        *,
        budget_threshold_percent: int | None,
        repeated_failures_count: int | None,
        repeated_failures_window_minutes: int,
        provider_degradation_enabled: bool,
        long_running_secs: int | None,
        failed_cleanup_enabled: bool,
    ) -> AlertConfiguration:
        self.require(principal, "alerts:write")
        if budget_threshold_percent is not None and not (
            1 <= budget_threshold_percent <= 100
        ):
            raise ValueError("budget threshold must be between 1 and 100")
        if repeated_failures_count is not None and repeated_failures_count < 1:
            raise ValueError("repeated failure count must be positive")
        if repeated_failures_window_minutes < 1:
            raise ValueError("repeated failure window must be positive")
        if long_running_secs is not None and long_running_secs < 1:
            raise ValueError("long-running threshold must be positive")
        return self.store.set_alert_configuration(
            principal.organization_id,
            actor_api_key_id=principal.api_key_id,
            budget_threshold_percent=budget_threshold_percent,
            repeated_failures_count=repeated_failures_count,
            repeated_failures_window_minutes=repeated_failures_window_minutes,
            provider_degradation_enabled=provider_degradation_enabled,
            long_running_secs=long_running_secs,
            failed_cleanup_enabled=failed_cleanup_enabled,
            timestamp=self._now().isoformat(),
        )

    def alert_history(
        self, principal: Principal, *, offset: int, limit: int
    ) -> tuple[list[dict], int | None]:
        self.require(principal, "alerts:read")
        self._evaluate_alerts(principal.organization_id)
        return self.store.list_alerts(
            principal.organization_id, offset=offset, limit=limit
        )

    def audit_history(
        self, principal: Principal, *, offset: int, limit: int
    ) -> tuple[list[dict], int | None]:
        self.require(principal, "audit:read")
        return self.store.list_audit(
            principal.organization_id, offset=offset, limit=limit
        )

    def record_audit(
        self,
        principal: Principal,
        *,
        action: str,
        outcome: str = "success",
        session_id: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        details: dict | None = None,
    ) -> str:
        return self.store.record_audit(
            principal.organization_id,
            api_key_id=principal.api_key_id,
            session_id=session_id,
            action=action,
            outcome=outcome,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details,
            created_at=self._now().isoformat(),
        )

    def get_retention_policy(self, principal: Principal) -> RetentionPolicy:
        self.require(principal, "retention:read")
        return self.store.get_retention_policy(principal.organization_id)

    def set_retention_policy(
        self,
        principal: Principal,
        *,
        operational_days: int,
        audit_days: int,
    ) -> RetentionPolicy:
        self.require(principal, "retention:write")
        if not 1 <= operational_days <= 3650:
            raise ValueError("operational retention must be 1 to 3650 days")
        if not 1 <= audit_days <= 3650:
            raise ValueError("audit retention must be 1 to 3650 days")
        return self.store.set_retention_policy(
            principal.organization_id,
            actor_api_key_id=principal.api_key_id,
            operational_days=operational_days,
            audit_days=audit_days,
            timestamp=self._now().isoformat(),
        )

    def apply_retention(self, principal: Principal) -> dict[str, int]:
        self.require(principal, "retention:write")
        return self.store.apply_retention(
            principal.organization_id, timestamp=self._now().isoformat()
        )

    def export_rows(
        self,
        principal: Principal,
        *,
        kind: str,
        offset: int,
        limit: int,
    ) -> tuple[list[str], list[dict], int | None]:
        self.require(principal, "exports:read")
        self._accrue_active(principal)
        return self.store.export_rows(
            principal.organization_id, kind=kind, offset=offset, limit=limit
        )

    def cost_summary(
        self,
        principal: Principal,
        *,
        group_by: str = "sandbox",
        start: str | None = None,
        end: str | None = None,
    ) -> list[dict[str, str]]:
        self.require(principal, "usage:read")
        self._accrue_active(principal)
        summaries = self.store.summarize_costs(
            principal.organization_id,
            group_by=group_by,
            start=start,
            end=end,
        )
        return [
            {
                "key": item.key,
                "runtime_seconds": str(item.runtime_seconds),
                "provider_cost_usd": str(item.provider_cost_usd),
                "customer_cost_usd": str(item.customer_cost_usd),
            }
            for item in summaries
        ]

    def reconcile(self, principal: Principal, backend: str) -> dict:
        """Reconcile one tenant against a provider-filtered resource list."""
        self.require(principal, "providers:reconcile")
        reconciler = self._provider_reconcilers.get(backend)
        if reconciler is None:
            raise ValueError(f"reconciliation is not configured: {backend}")
        try:
            observations = reconciler.list_resources(principal.organization_id)
        except Exception:
            return {
                "backend": backend,
                "status": "partial_outage",
                "updated": 0,
                "missing": 0,
                "orphans": 0,
            }
        allowed_statuses = {
            "provisioning",
            "running",
            "stopping",
            "terminated",
            "failed",
        }
        invalid = [
            item.status
            for item in observations
            if item.status.lower().strip() not in allowed_statuses
        ]
        if invalid:
            raise ValueError(
                f"unsupported provider lifecycle status: {invalid[0]}"
            )
        known_records = [
            r
            for r in self.store.list_sandboxes(principal.organization_id)
            if r.backend == backend and r.provider_resource_id
        ]
        records = [
            r
            for r in known_records
            if r.status in {"creating", "running", "stopping"}
        ]
        known_resource_ids = {
            record.provider_resource_id for record in known_records
        }
        by_resource = {o.provider_resource_id: o for o in observations}
        updated = missing = 0
        for record in records:
            observation = by_resource.get(record.provider_resource_id or "")
            if observation is None:
                when = self._now().isoformat()
                observation_id = f"missing:{record.id}:{when}"
                self.store.record_provider_observation(
                    principal.organization_id,
                    record.id,
                    observation_id=observation_id,
                    provider_resource_id=record.provider_resource_id,
                    status="missing",
                    observed_at=when,
                    missing=True,
                )
                self.store.apply_provider_observation(
                    principal.organization_id,
                    record.id,
                    status="failed",
                    observed_at=when,
                    missing=True,
                )
                self._record_lifecycle_estimate(
                    record, when, observation_id=observation_id
                )
                missing += 1
                continue
            self.store.record_provider_observation(
                principal.organization_id,
                record.id,
                observation_id=observation.observation_id,
                provider_resource_id=observation.provider_resource_id,
                status=observation.status,
                observed_at=observation.observed_at,
                provider_cost_usd=observation.provider_cost_usd,
                currency=observation.currency,
            )
            self.store.apply_provider_observation(
                principal.organization_id,
                record.id,
                status=observation.status,
                observed_at=observation.observed_at,
            )
            self._record_lifecycle_estimate(
                record,
                observation.observed_at,
                observation_id=observation.observation_id,
                provider_cost=observation.provider_cost_usd,
                currency=observation.currency,
            )
            updated += 1
        return {
            "backend": backend,
            "status": "ok",
            "updated": updated,
            "missing": missing,
            "orphans": len(
                [
                    o
                    for o in observations
                    if o.provider_resource_id not in known_resource_ids
                ]
            ),
        }

    def reconciliation_summary(self, principal: Principal) -> dict:
        """Return tenant-scoped reconciliation health."""
        self.require(principal, "sandboxes:read")
        records = self.store.list_sandboxes(principal.organization_id)
        return {
            "total": len(records),
            "unreconciled": sum(r.cost_state == "estimated" for r in records),
            "provider_reported": sum(
                r.cost_state == "provider_reported" for r in records
            ),
            "reconciled": sum(r.cost_state == "reconciled" for r in records),
            "missing": sum(r.provider_missing for r in records),
            "last_provider_observed_at": max(
                (
                    r.last_provider_observed_at
                    for r in records
                    if r.last_provider_observed_at
                ),
                default=None,
            ),
        }

    def close(self) -> None:
        """Best-effort cleanup of runtimes owned by this process."""
        self.stop_supervision()
        with self._lock:
            runtimes = list(self._runtimes.items())
            self._runtimes.clear()
        for sandbox_id, runtime in runtimes:
            stopping_at = self._now().isoformat()
            self.store.mark_lifecycle(sandbox_id, "stopping", stopping_at)
            try:
                runtime.destroy()
            except Exception:
                failed_at = self._now().isoformat()
                self.store.mark_lifecycle(sandbox_id, "failed", failed_at)
                self._record_lifecycle_estimate(
                    self.store.get_sandbox_internal(sandbox_id),
                    failed_at,
                    observation_id=f"close-failed:{sandbox_id}",
                )
            else:
                terminated_at = self._now().isoformat()
                self.store.mark_lifecycle(
                    sandbox_id,
                    "destroyed",
                    terminated_at,
                    reason="shutdown",
                )
                self._record_lifecycle_estimate(
                    self.store.get_sandbox_internal(sandbox_id),
                    terminated_at,
                    observation_id=f"close:{sandbox_id}",
                )

    def _record_usage(
        self,
        principal: Principal,
        record: SandboxRecord,
        runtime: SandboxRuntime,
        execution_id: str,
        request_id: str,
        elapsed: float,
    ) -> dict[str, str]:
        provider_cost = Decimal(str(runtime.estimate_compute_cost(elapsed)))
        customer_cost = provider_cost * self._customer_markup
        runtime_seconds = Decimal(str(elapsed))
        self.store.record_runtime_usage(
            organization_id=principal.organization_id,
            sandbox_id=record.id,
            execution_id=execution_id,
            request_id=request_id,
            runtime_seconds=runtime_seconds,
            provider_cost_usd=provider_cost,
            customer_cost_usd=customer_cost,
            price_version=pricing.get_pricing()["last_updated"],
        )
        return {
            "runtime_seconds": str(runtime_seconds),
            "provider_cost_usd": str(provider_cost),
            "customer_cost_usd": str(customer_cost),
        }

    @staticmethod
    def _provider_resource_id(runtime: SandboxRuntime) -> str | None:
        value = getattr(runtime, "provider_resource_id", None)
        return str(value) if value is not None else None

    def _accrue_active(
        self, principal: Principal, *, timestamp: str | None = None
    ) -> None:
        timestamp = timestamp or self._now().isoformat()
        for record in self.store.list_sandboxes(principal.organization_id):
            if (
                record.status in {"creating", "running", "stopping"}
                and record.cost_state == "estimated"
            ):
                self._record_lifecycle_estimate(
                    record,
                    timestamp,
                    observation_id=f"estimate:{record.id}:{timestamp}",
                )

    def start_supervision(self) -> None:
        """Start the daemon supervisor once for this service process."""
        if self._supervision_interval_secs <= 0:
            return
        with self._lock:
            if self._supervisor_thread and self._supervisor_thread.is_alive():
                return
            self._supervisor_stop.clear()
            self._supervisor_thread = threading.Thread(
                target=self._supervision_loop,
                name="sandbox-lifecycle-supervisor",
                daemon=True,
            )
            self._supervisor_thread.start()

    def stop_supervision(self) -> None:
        self._supervisor_stop.set()
        thread = self._supervisor_thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(1, self._supervision_interval_secs + 1))
        self._supervisor_thread = None

    def run_watchdog_once(self) -> dict[str, int]:
        """Clean expired and orphaned provider resources idempotently."""
        when = self._now()
        timestamp = when.isoformat()
        stale_before = (
            when
            - timedelta(seconds=max(60, self._supervision_interval_secs * 3))
        ).isoformat()
        expired = self.store.claim_expired_sandboxes(
            timestamp=timestamp,
            owner=self._supervisor_owner,
            stale_before=stale_before,
        )
        result = {
            "expired_claimed": len(expired),
            "expired_terminated": 0,
            "expired_failed": 0,
            "orphans_deleted": 0,
            "orphan_failures": 0,
        }
        for record in expired:
            with self._lock:
                runtime = self._runtimes.pop(record.id, None)
            try:
                if runtime is not None:
                    runtime.destroy()
                elif (
                    record.provider_resource_id
                    and record.backend in self._provider_terminators
                ):
                    self._provider_terminators[record.backend](
                        record.provider_resource_id
                    )
                else:
                    raise RuntimeError("provider terminator is unavailable")
            except Exception:
                error = normalize_provider_error(
                    RuntimeError("supervised cleanup failed"),
                    backend=record.backend,
                    op="destroy",
                )
                self.store.fail_termination(
                    record.id,
                    timestamp=timestamp,
                    error=provider_error_payload(error),
                )
                configuration = self.store.get_alert_configuration(
                    record.organization_id
                )
                if configuration.failed_cleanup_enabled:
                    self.store.record_alert(
                        record.organization_id,
                        alert_type="failed_cleanup",
                        severity="critical",
                        message="Expired sandbox cleanup failed.",
                        resource_type="sandbox",
                        resource_id=record.id,
                        dedupe_key=f"failed_cleanup:sandbox:{record.id}:{timestamp}",
                        created_at=timestamp,
                    )
                result["expired_failed"] += 1
                continue
            self.store.mark_lifecycle(
                record.id,
                "destroyed",
                timestamp,
                reason="ttl_expired",
            )
            final = self.store.get_sandbox_internal(record.id)
            self._record_lifecycle_estimate(
                final,
                timestamp,
                observation_id=f"watchdog-expiry:{record.id}:{record.expires_at}",
            )
            result["expired_terminated"] += 1

        for backend, reconciler in self._provider_reconcilers.items():
            terminator = self._provider_terminators.get(backend)
            if terminator is None:
                continue
            for organization_id in self.store.list_organization_ids():
                try:
                    observations = reconciler.list_resources(organization_id)
                except Exception:
                    continue
                known = self.store.known_provider_resource_ids(
                    organization_id, backend
                )
                for observation in observations:
                    if observation.provider_resource_id in known:
                        continue
                    if not self.store.claim_orphan_cleanup(
                        organization_id=organization_id,
                        backend=backend,
                        provider_resource_id=observation.provider_resource_id,
                        observed_at=observation.observed_at,
                    ):
                        continue
                    try:
                        terminator(observation.provider_resource_id)
                    except Exception:
                        cleanup_status = "failed"
                        result["orphan_failures"] += 1
                        configuration = self.store.get_alert_configuration(
                            organization_id
                        )
                        if configuration.failed_cleanup_enabled:
                            self.store.record_alert(
                                organization_id,
                                alert_type="failed_cleanup",
                                severity="critical",
                                message="Orphan provider cleanup failed.",
                                resource_type="provider_resource",
                                resource_id=observation.provider_resource_id,
                                dedupe_key=(
                                    "failed_cleanup:orphan:"
                                    f"{backend}:{observation.provider_resource_id}:"
                                    f"{timestamp}"
                                ),
                                created_at=timestamp,
                            )
                    else:
                        cleanup_status = "deleted"
                        result["orphans_deleted"] += 1
                    self.store.complete_orphan_cleanup(
                        backend,
                        observation.provider_resource_id,
                        status=cleanup_status,
                        completed_at=timestamp,
                    )
        for organization_id in self.store.list_organization_ids():
            self._evaluate_alerts(organization_id)
        return result

    def _evaluate_alerts(self, organization_id: str) -> None:
        configuration = self.store.get_alert_configuration(organization_id)
        when = self._now()
        timestamp = when.isoformat()
        if configuration.budget_threshold_percent is not None:
            summary = self.store.policy_summary(
                organization_id, timestamp=timestamp
            )
            policy = summary["policy"]
            usage = summary["usage"]
            budget_windows = (
                (
                    "hourly",
                    policy.hourly_spend_limit_usd,
                    usage["hourly_spend_usd"],
                    when.strftime("%Y-%m-%dT%H"),
                ),
                (
                    "daily",
                    policy.daily_spend_limit_usd,
                    usage["daily_spend_usd"],
                    when.strftime("%Y-%m-%d"),
                ),
            )
            for label, limit, current, bucket in budget_windows:
                if limit is None or Decimal(limit) <= 0:
                    continue
                percentage = Decimal(current) / Decimal(limit) * 100
                if percentage >= configuration.budget_threshold_percent:
                    self.store.record_alert(
                        organization_id,
                        alert_type="budget_threshold",
                        severity="warning",
                        message=(
                            f"{label.capitalize()} spend reached "
                            f"{int(percentage)}% of its budget."
                        ),
                        resource_type="organization",
                        resource_id=organization_id,
                        dedupe_key=f"budget:{label}:{bucket}",
                        created_at=timestamp,
                    )
        if configuration.repeated_failures_count is not None:
            since = (
                when
                - timedelta(
                    minutes=configuration.repeated_failures_window_minutes
                )
            ).isoformat()
            failures = self.store.failed_sandbox_count(
                organization_id, since=since
            )
            if failures >= configuration.repeated_failures_count:
                self.store.record_alert(
                    organization_id,
                    alert_type="repeated_failures",
                    severity="critical",
                    message=(
                        f"{failures} sandbox failures occurred in the configured "
                        "window."
                    ),
                    resource_type="organization",
                    resource_id=organization_id,
                    dedupe_key=(
                        "failures:"
                        f"{when.strftime('%Y-%m-%dT%H')}:"
                        f"{configuration.repeated_failures_count}"
                    ),
                    created_at=timestamp,
                )
        if configuration.long_running_secs is not None:
            started_before = (
                when - timedelta(seconds=configuration.long_running_secs)
            ).isoformat()
            for sandbox_id in self.store.long_running_sandboxes(
                organization_id, started_before=started_before
            ):
                self.store.record_alert(
                    organization_id,
                    alert_type="long_running",
                    severity="warning",
                    message="Sandbox exceeded the configured runtime threshold.",
                    resource_type="sandbox",
                    resource_id=sandbox_id,
                    dedupe_key=f"long_running:{sandbox_id}",
                    created_at=timestamp,
                )

    def _supervision_loop(self) -> None:
        while not self._supervisor_stop.wait(self._supervision_interval_secs):
            try:
                self.run_watchdog_once()
            except Exception:
                continue

    def _provider_public_status(
        self, backend: str, stored: dict | None
    ) -> dict:
        settings = self._provider_settings.get(backend, {})
        configured = self._provider_is_configured(backend, settings)
        return {
            "backend": backend,
            "configured": configured,
            "status": stored["status"] if stored else "unknown",
            "checked_at": stored["checked_at"] if stored else None,
            "message": stored["message"] if stored else "Not checked yet.",
        }

    @staticmethod
    def _provider_is_configured(backend: str, settings: dict[str, str]) -> bool:
        if backend in {"local", "docker", "ray", "safehouse"}:
            return True
        return bool(settings) and all(
            bool(value) for value in settings.values()
        )

    def _record_lifecycle_estimate(
        self,
        record: SandboxRecord,
        end_at: str,
        *,
        observation_id: str,
        provider_cost: Decimal | None = None,
        currency: str | None = None,
    ) -> None:
        started = record.provisioning_at or record.created_at
        seconds = max(
            Decimal(0),
            Decimal(
                str(
                    (
                        datetime.fromisoformat(end_at)
                        - datetime.fromisoformat(started)
                    ).total_seconds()
                )
            ),
        )
        rate = Decimal(record.hourly_rate_usd)
        if rate == 0 and provider_cost is None:
            return
        estimate = seconds * rate / Decimal(3600)
        self.store.record_lifecycle_cost(
            organization_id=record.organization_id,
            sandbox_id=record.id,
            observation_id=observation_id,
            billable_seconds=seconds,
            estimated_cost_usd=estimate,
            provider_reported_cost_usd=provider_cost,
            customer_markup=self._customer_markup,
            currency=currency or record.currency,
            pricing_source=(
                "provider"
                if provider_cost is not None
                else record.pricing_source
            ),
            timestamp=end_at,
        )

    @staticmethod
    def _retry_status(error: Exception, cleanup_status: object) -> str:
        outcome = getattr(error, "outcome", ErrorOutcome.UNKNOWN)
        if outcome is ErrorOutcome.UNKNOWN or cleanup_status == "unknown":
            return "blocked_cleanup_unknown"
        return (
            "retryable"
            if getattr(error, "retryable", False)
            else "not_retryable"
        )
