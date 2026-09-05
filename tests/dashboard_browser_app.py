"""Deterministic fake-provider app used for manual browser acceptance."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from bespokelabs.sandbox.control_plane.api import create_app
from bespokelabs.sandbox.control_plane.errors import PolicyDeniedError
from bespokelabs.sandbox.control_plane.reconciliation import ProviderObservation
from bespokelabs.sandbox.control_plane.service import ControlPlane
from bespokelabs.sandbox.control_plane.store import SQLiteStore
from bespokelabs.sandbox.exceptions import SandboxTimeoutError
from bespokelabs.sandbox.types import SandboxResult

DB_PATH = "/tmp/bespoke-phase5-browser.db"
OPERATOR_TOKEN = "bsk_live_phase5_operator_fixture_only"
READ_ONLY_TOKEN = "bsk_live_phase5_read_only_fixture_only"


class BrowserRuntime:

    def __init__(self, backend: str, index: int, **config: object) -> None:
        if config.get("app_name") == "failed-launch":
            raise SandboxTimeoutError(
                "Deterministic fake launch timed out",
                backend=backend,
                op="create",
                context={"cleanup_status": "deleted"},
            )
        self.backend_name = backend
        self.provider_resource_id = f"fake-{backend}-{index:03d}"
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
        return elapsed_secs / 1800

    def destroy(self) -> None:
        self.destroyed = True


class BrowserFactory:

    def __init__(self) -> None:
        self.created: list[BrowserRuntime] = []

    def __call__(self, backend: str, **config: object) -> BrowserRuntime:
        runtime = BrowserRuntime(backend, len(self.created) + 1, **config)
        self.created.append(runtime)
        return runtime


class BrowserReconciler:

    def __init__(self) -> None:
        self.by_org: dict[str, list[ProviderObservation]] = {}

    def list_resources(self, organization_id: str) -> list[ProviderObservation]:
        return self.by_org.get(organization_id, [])


now = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=516)


def current_time() -> datetime:
    return now


store = SQLiteStore(DB_PATH, key_pepper="phase5-browser-fixture")
factory = BrowserFactory()
reconciler = BrowserReconciler()
service = ControlPlane(
    store,
    sandbox_factory=factory,
    allowed_backends={"daytona", "e2b", "runpod"},
    provider_settings={
        "daytona": {"DAYTONA_API_KEY": "fixture-not-returned"},
        "e2b": {"E2B_API_KEY": "fixture-not-returned"},
        "runpod": {"RUNPOD_API_KEY": "fixture-not-returned"},
    },
    provider_health_checks={
        "daytona": lambda _settings: True,
        "runpod": lambda _settings: True,
    },
    provider_reconcilers={"daytona": reconciler},
    now=current_time,
)
_, operator_key = service.bootstrap_organization("Phase 5 browser fixture")
operator = store.authenticate(operator_key.secret)
read_key = service.issue_api_key(
    operator,
    name="Read-only observer",
    scopes=["sandboxes:read", "usage:read"],
)
with store._connect() as connection:
    connection.execute(
        "UPDATE api_keys SET key_digest=? WHERE id=?",
        (store._digest(OPERATOR_TOKEN), operator_key.id),
    )
    connection.execute(
        "UPDATE api_keys SET key_digest=? WHERE id=?",
        (store._digest(READ_ONLY_TOKEN), read_key.id),
    )

for index in range(8):
    archived = service.create_sandbox(
        operator,
        "runpod" if index % 2 else "daytona",
        {"preset": "archived-cpu", "timeout_secs": 3600},
    )
    now += timedelta(seconds=1)
    service.destroy_sandbox(operator, archived.id)
    now += timedelta(seconds=1)

live = service.create_sandbox(
    operator,
    "daytona",
    {
        "gpu": "A10G",
        "cpu": 4,
        "memory_mb": 16384,
        "timeout_secs": 600,
        "app_name": "operator-demo",
    },
)
service.execute(
    operator,
    live.id,
    code="print('fake provider ready')",
    request_id="browser-execution-1",
)
now += timedelta(seconds=500)
reconciler.by_org[operator.organization_id] = [
    ProviderObservation(
        "browser-observation-1",
        live.provider_resource_id or "",
        "running",
        now.isoformat(),
        Decimal("0.24"),
    )
]
service.reconcile(operator, "daytona")

try:
    service.create_sandbox(
        operator,
        "daytona",
        {"app_name": "failed-launch", "timeout_secs": 900},
    )
except SandboxTimeoutError:
    pass

service.set_policy(
    operator,
    max_concurrent_sandboxes=3,
    hourly_spend_limit_usd=Decimal("1"),
    daily_spend_limit_usd=Decimal("10"),
    allowed_backends=["daytona", "runpod"],
    allowed_gpu_types=["A10G"],
    max_sandbox_lifetime_secs=3600,
)
service.set_alert_configuration(
    operator,
    budget_threshold_percent=25,
    repeated_failures_count=1,
    repeated_failures_window_minutes=60,
    provider_degradation_enabled=True,
    long_running_secs=300,
    failed_cleanup_enabled=True,
)
service.check_provider_health(operator, "daytona")
service.check_provider_health(operator, "e2b")
service.check_provider_health(operator, "runpod")
try:
    service.create_sandbox(
        operator,
        "runpod",
        {"gpu": "A100", "timeout_secs": 600},
    )
except PolicyDeniedError:
    pass

service.alert_history(operator, offset=0, limit=20)

app = create_app(
    service,
    session_cookie_secure=False,
    enable_local_dashboard_login=True,
)
