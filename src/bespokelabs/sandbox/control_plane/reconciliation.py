"""Provider reconciliation contracts independent of any provider SDK."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class ProviderObservation:
    """One provider resource visible to a tenant-scoped reconciliation run."""

    observation_id: str
    provider_resource_id: str
    status: str
    observed_at: str
    provider_cost_usd: Decimal | None = None
    currency: str = "USD"


class ProviderReconciler(Protocol):
    """List resources after applying the provider's tenant label/filter."""

    def list_resources(self, organization_id: str) -> list[ProviderObservation]:
        ...
