"""Pricing data for sandbox backends.

Load pricing from the bundled pricing.json and compute estimated costs.

Usage:
    from bespokelabs.sandbox.pricing import get_pricing, cost_per_second

    pricing = get_pricing()           # full dict
    cps = cost_per_second("modal")    # $/sec for 1 vCPU, 1 GiB RAM
"""

from __future__ import annotations

import json
from pathlib import Path

_PRICING_PATH = Path(__file__).parent / "pricing.json"
_cache: dict | None = None


def get_pricing() -> dict:
    """Return the full pricing dict from pricing.json."""
    global _cache
    if _cache is None:
        with open(_PRICING_PATH) as f:
            _cache = json.load(f)
    return _cache


def get_backend_pricing(backend: str) -> dict | None:
    """Return pricing info for a single backend, or None if not found."""
    return get_pricing()["backends"].get(backend)


def cost_per_second(backend: str, vcpu: float = 1.0, ram_gib: float = 1.0) -> float:
    """Estimated cost per second for a backend at the given resource level.

    Returns 0.0 for free/local backends or unknown backends.
    """
    info = get_backend_pricing(backend)
    if info is None:
        return 0.0
    vcpu_per_sec = info.get("vcpu_per_hour_usd", 0.0) / 3600
    ram_per_sec = info.get("ram_gib_per_hour_usd", 0.0) / 3600
    return vcpu_per_sec * vcpu + ram_per_sec * ram_gib


def list_backends() -> list[str]:
    """Return the list of backends with pricing data."""
    return list(get_pricing()["backends"].keys())
