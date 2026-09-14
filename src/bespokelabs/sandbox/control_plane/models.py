"""Value objects used by the sandbox control plane."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Principal:
    """Authenticated organization and API-key identity."""

    organization_id: str
    api_key_id: str
    scopes: frozenset[str]

    def allows(self, scope: str) -> bool:
        if "*" in self.scopes or scope in self.scopes:
            return True
        legacy_write_scopes = {
            "sandboxes:create",
            "sandboxes:execute",
            "sandboxes:terminate",
            "providers:reconcile",
        }
        return scope in legacy_write_scopes and "sandboxes:write" in self.scopes


@dataclass(frozen=True)
class IssuedAPIKey:
    """Newly issued API key. ``secret`` is only available at creation."""

    id: str
    organization_id: str
    name: str
    prefix: str
    scopes: tuple[str, ...]
    secret: str
    created_at: str


@dataclass(frozen=True)
class SandboxRecord:
    """Tenant-scoped metadata for a sandbox runtime."""

    id: str
    organization_id: str
    backend: str
    status: str
    config: dict
    created_at: str
    destroyed_at: str | None = None
    error: str | None = None
    attempt_count: int = 0
    latest_error: dict | None = None
    retry_status: str = "not_applicable"
    cleanup_status: str | None = None
    provider_resource_id: str | None = None
    requested_at: str | None = None
    provisioning_at: str | None = None
    running_at: str | None = None
    stopping_at: str | None = None
    terminated_at: str | None = None
    failed_at: str | None = None
    last_provider_observed_at: str | None = None
    hourly_rate_usd: str = "0"
    currency: str = "USD"
    pricing_source: str = "bundled"
    cost_state: str = "estimated"
    provider_missing: bool = False
    creator_api_key_id: str | None = None
    creator_api_key_name: str | None = None
    version: int = 1
    expires_at: str | None = None
    terminated_by_api_key_id: str | None = None
    terminated_by_api_key_name: str | None = None
    termination_reason: str | None = None


@dataclass(frozen=True)
class OrganizationPolicy:
    """Tenant-owned sandbox and spend guardrails."""

    organization_id: str
    max_concurrent_sandboxes: int | None = None
    hourly_spend_limit_usd: str | None = None
    daily_spend_limit_usd: str | None = None
    allowed_backends: tuple[str, ...] | None = None
    allowed_gpu_types: tuple[str, ...] | None = None
    max_sandbox_lifetime_secs: int | None = None
    version: int = 1
    updated_at: str | None = None
    updated_by_api_key_id: str | None = None


@dataclass(frozen=True)
class AlertConfiguration:
    """Tenant thresholds for durable operational alert events."""

    organization_id: str
    budget_threshold_percent: int | None = None
    repeated_failures_count: int | None = None
    repeated_failures_window_minutes: int = 60
    provider_degradation_enabled: bool = True
    long_running_secs: int | None = None
    failed_cleanup_enabled: bool = True
    version: int = 1
    updated_at: str | None = None
    updated_by_api_key_id: str | None = None


@dataclass(frozen=True)
class RetentionPolicy:
    """Tenant retention periods for operational and audit history."""

    organization_id: str
    operational_days: int = 90
    audit_days: int = 365
    version: int = 1
    updated_at: str | None = None
    updated_by_api_key_id: str | None = None


@dataclass(frozen=True)
class CostSummary:
    """Aggregated provider and customer cost for one grouping key."""

    key: str
    runtime_seconds: Decimal
    provider_cost_usd: Decimal
    customer_cost_usd: Decimal
