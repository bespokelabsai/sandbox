"""Command-line entry point for the sandbox control plane."""

from __future__ import annotations

import os
from collections.abc import Mapping
from decimal import Decimal

from bespokelabs.sandbox.control_plane.service import ControlPlane
from bespokelabs.sandbox.control_plane.store import SQLiteStore

_PROVIDER_ENVIRONMENT = {
    "daytona": ("DAYTONA_API_KEY",),
    "e2b": ("E2B_API_KEY",),
    "modal": ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET"),
    "runpod": ("RUNPOD_API_KEY",),
    "tensorlake": ("TENSORLAKE_API_KEY",),
}


def _provider_settings_from_environment(
    environ: Mapping[str, str],
) -> dict[str, dict[str, str]]:
    """Copy expected provider settings without persisting their values."""
    return {
        backend: {name: environ.get(name, "") for name in names}
        for backend, names in _PROVIDER_ENVIRONMENT.items()
    }


def main() -> None:
    """Run the HTTP server using environment-based configuration."""
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit(
            "Install server dependencies with: "
            "pip install 'bespokelabs-sandbox[server]'"
        ) from exc

    from bespokelabs.sandbox.control_plane.api import create_app

    pepper = os.environ.get("BESPOKE_API_KEY_PEPPER")
    admin_token = os.environ.get("BESPOKE_CONTROL_PLANE_ADMIN_TOKEN")
    if not pepper:
        raise SystemExit("BESPOKE_API_KEY_PEPPER is required")
    if not admin_token:
        raise SystemExit("BESPOKE_CONTROL_PLANE_ADMIN_TOKEN is required")

    database_path = os.environ.get(
        "BESPOKE_CONTROL_PLANE_DB", "./bespoke-control-plane.db"
    )
    allowed_backends_env = os.environ.get("BESPOKE_ALLOWED_BACKENDS")
    allowed_backends = (
        [item.strip() for item in allowed_backends_env.split(",")]
        if allowed_backends_env
        else None
    )
    store = SQLiteStore(database_path, key_pepper=pepper)
    provider_settings = _provider_settings_from_environment(os.environ)
    control_plane = ControlPlane(
        store,
        customer_markup=Decimal(os.environ.get("BESPOKE_CUSTOMER_MARKUP", "1")),
        allowed_backends=allowed_backends,
        provider_settings=provider_settings,
        supervision_interval_secs=float(
            os.environ.get("BESPOKE_SUPERVISION_INTERVAL_SECS", "30")
        ),
    )
    app = create_app(
        control_plane,
        admin_token=admin_token,
        session_cookie_secure=os.environ.get(
            "BESPOKE_SESSION_COOKIE_SECURE", "1"
        )
        != "0",
        enable_local_dashboard_login=os.environ.get(
            "BESPOKE_ENABLE_LOCAL_DASHBOARD_LOGIN", "0"
        )
        == "1",
        session_ttl_seconds=int(
            os.environ.get("BESPOKE_DASHBOARD_SESSION_TTL_SECS", "28800")
        ),
    )
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":
    main()
