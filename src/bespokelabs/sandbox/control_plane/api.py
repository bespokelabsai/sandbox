"""FastAPI transport for the sandbox control plane."""

from __future__ import annotations

import csv
import dataclasses
import io
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from bespokelabs.sandbox.control_plane.errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    NotFoundError,
    PolicyDeniedError,
    provider_error_payload,
)
from bespokelabs.sandbox.control_plane.models import Principal, SandboxRecord
from bespokelabs.sandbox.control_plane.security import (
    decode_cursor,
    encode_cursor,
)
from bespokelabs.sandbox.control_plane.service import ControlPlane
from bespokelabs.sandbox.exceptions import ErrorCode, SandboxError

_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")
_CSV_NUMERIC_COLUMNS = frozenset(
    {
        "quantity",
        "provider_cost_usd",
        "customer_cost_usd",
        "billable_seconds",
        "estimated_cost_usd",
        "provider_reported_cost_usd",
        "effective_cost_usd",
        "runtime_seconds",
        "provider_delta_usd",
        "customer_delta_usd",
    }
)


class OrganizationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class APIKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    scopes: list[str] = Field(default_factory=lambda: ["*"])
    expires_at: datetime | None = None


class SandboxCreate(BaseModel):
    backend: str
    preset: str | None = None
    cpu: float | None = Field(default=None, gt=0)
    memory_mb: int | None = Field(default=None, gt=0)
    disk_mb: int | None = Field(default=None, gt=0)
    gpu: str | None = None
    timeout_secs: int | None = Field(default=None, gt=0)
    image: str | None = None
    env_vars: dict[str, str] | None = None
    allow_internet: bool | None = None
    app_name: str | None = None
    template: str | None = None
    snapshot_id: str | None = None
    workdir: str | None = None
    git_repo: str | None = None
    git_ref: str | None = None


class OrganizationPolicyUpdate(BaseModel):
    max_concurrent_sandboxes: int | None = Field(default=None, ge=0)
    hourly_spend_limit_usd: Decimal | None = Field(default=None, ge=0)
    daily_spend_limit_usd: Decimal | None = Field(default=None, ge=0)
    allowed_backends: list[str] | None = None
    allowed_gpu_types: list[str] | None = None
    max_sandbox_lifetime_secs: int | None = Field(default=None, gt=0)


class DashboardLogin(BaseModel):
    api_key: str = Field(min_length=1, max_length=512)


class AlertConfigurationUpdate(BaseModel):
    budget_threshold_percent: int | None = Field(default=None, ge=1, le=100)
    repeated_failures_count: int | None = Field(default=None, ge=1)
    repeated_failures_window_minutes: int = Field(default=60, ge=1, le=10080)
    provider_degradation_enabled: bool = True
    long_running_secs: int | None = Field(default=None, ge=1)
    failed_cleanup_enabled: bool = True


class RetentionPolicyUpdate(BaseModel):
    operational_days: int = Field(default=90, ge=1, le=3650)
    audit_days: int = Field(default=365, ge=1, le=3650)


class ExecuteRequest(BaseModel):
    code: str | None = None
    language: str = "python"
    command: str | None = None
    args: list[str] | None = None

    @model_validator(mode="after")
    def validate_operation(self) -> ExecuteRequest:
        if (self.code is None) == (self.command is None):
            raise ValueError("provide exactly one of code or command")
        return self


def _sandbox_response(record: SandboxRecord) -> dict[str, Any]:
    result = dataclasses.asdict(record)
    result.pop("organization_id")
    if result.get("error"):
        result["error"] = "Sandbox operation failed."
    return result


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise AuthenticationError("Bearer API key required")
    return authorization.removeprefix("Bearer ").strip()


def _if_match_version(value: str | None) -> int | None:
    if value is None:
        return None
    normalized = value.strip().removeprefix("W/").strip('"')
    try:
        version = int(normalized)
    except ValueError as exc:
        raise ValueError("If-Match must contain a sandbox version") from exc
    if version < 1:
        raise ValueError("If-Match must contain a positive sandbox version")
    return version


def _spreadsheet_safe_csv_rows(
    columns: list[str], rows: list[dict]
) -> list[dict]:
    """Neutralize formula-like text while preserving numeric CSV fields."""
    safe_rows = []
    for row in rows:
        safe_row = dict(row)
        for column in columns:
            value = safe_row.get(column)
            if (
                column not in _CSV_NUMERIC_COLUMNS
                and isinstance(value, str)
                and value.startswith(_CSV_FORMULA_PREFIXES)
            ):
                safe_row[column] = f"'{value}"
        safe_rows.append(safe_row)
    return safe_rows


def create_app(
    control_plane: ControlPlane,
    *,
    admin_token: str | None = None,
    session_cookie_secure: bool = True,
    enable_local_dashboard_login: bool = False,
    session_ttl_seconds: int = 8 * 60 * 60,
) -> FastAPI:
    """Create an HTTP API around an initialized control-plane service."""
    dashboard_dir = Path(__file__).with_name("static")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        control_plane.start_supervision()
        yield
        control_plane.close()

    app = FastAPI(
        title="Bespoke Sandbox Control Plane",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.mount(
        "/dashboard/assets",
        StaticFiles(directory=dashboard_dir),
        name="dashboard-assets",
    )
    request_auth: ContextVar[dict] = ContextVar("request_auth")

    def add_security_headers(response: Response) -> Response:
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'; "
            "object-src 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Strict-Transport-Security"] = (
            "max-age=63072000; includeSubDomains"
        )
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def audit_action(request: Request) -> str:
        route = request.scope.get("route")
        template = getattr(route, "path", request.url.path)
        names = {
            ("POST", "/v1/sandboxes"): "sandbox.create",
            ("DELETE", "/v1/sandboxes/{sandbox_id}"): "sandbox.terminate",
            ("POST", "/v1/sandboxes/{sandbox_id}/execute"): "sandbox.execute",
            ("PUT", "/v1/policies/current"): "policy.update",
            (
                "POST",
                "/v1/providers/{backend}/health-check",
            ): "provider.health_check",
            ("POST", "/v1/reconciliation/{backend}"): "provider.reconcile",
            ("POST", "/v1/api-keys"): "api_key.create",
            ("DELETE", "/v1/api-keys/{key_id}"): "api_key.revoke",
            ("PUT", "/v1/alerts/configuration"): "alerts.configure",
            ("PUT", "/v1/retention/current"): "retention.configure",
            ("POST", "/v1/retention/apply"): "retention.apply",
        }
        return names.get(
            (request.method, template), f"{request.method.lower()} {template}"
        )

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        state: dict = {}
        context_token = request_auth.set(state)
        cookie = request.cookies.get("bespoke_dashboard_session")
        if cookie:
            try:
                principal, session_id = (
                    control_plane.store.authenticate_web_session(
                        cookie, timestamp=control_plane._now().isoformat()
                    )
                )
            except AuthenticationError:
                pass
            else:
                state["principal"] = principal
                state["session_id"] = session_id
        is_login = (
            request.method == "POST"
            and request.url.path == "/v1/dashboard/session"
        )
        if (
            state.get("session_id")
            and not request.headers.get("Authorization")
            and not request.headers.get("X-Control-Plane-Admin")
            and request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and not is_login
            and not control_plane.store.validate_session_csrf(
                state["session_id"], request.headers.get("X-CSRF-Token")
            )
        ):
            request_auth.reset(context_token)
            return add_security_headers(
                JSONResponse(
                    status_code=403,
                    content={"detail": "CSRF token is missing or invalid"},
                )
            )
        try:
            response = await call_next(request)
            principal = state.get("principal")
            should_audit = request.url.path.startswith("/v1/") and (
                request.method in {"POST", "PUT", "PATCH", "DELETE"}
                or request.url.path.startswith("/v1/exports/")
            )
            if principal is not None and should_audit and not is_login:
                control_plane.record_audit(
                    principal,
                    action=audit_action(request),
                    outcome=(
                        "success"
                        if response.status_code < 400
                        else "denied"
                        if response.status_code < 500
                        else "failure"
                    ),
                    session_id=state.get("session_id"),
                    resource_type="http_route",
                    resource_id=request.url.path[:255],
                    details={"status_code": response.status_code},
                )
            return add_security_headers(response)
        finally:
            request_auth.reset(context_token)

    @app.exception_handler(AuthenticationError)
    async def authentication_error(
        _: Request, exc: AuthenticationError
    ) -> JSONResponse:
        return JSONResponse(status_code=401, content={"detail": str(exc)})

    @app.exception_handler(AuthorizationError)
    async def authorization_error(
        _: Request, exc: AuthorizationError
    ) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(NotFoundError)
    async def not_found_error(_: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ConflictError)
    async def conflict_error(_: Request, exc: ConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(PolicyDeniedError)
    async def policy_denied_error(
        _: Request, exc: PolicyDeniedError
    ) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": exc.payload()})

    @app.exception_handler(ValueError)
    async def invalid_request(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(SandboxError)
    async def provider_error(_: Request, exc: SandboxError) -> JSONResponse:
        status_code = 504 if exc.code is ErrorCode.TIMEOUT else 502
        if exc.code in {
            ErrorCode.BACKEND_NOT_INSTALLED,
            ErrorCode.CONFIGURATION,
        }:
            status_code = 422
        return JSONResponse(
            status_code=status_code,
            content={"detail": provider_error_payload(exc)},
        )

    @app.exception_handler(Exception)
    async def unexpected_error(_: Request, __: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal control-plane error."},
        )

    def current_principal(authorization: str | None) -> Principal:
        state = request_auth.get({})
        if authorization:
            principal = control_plane.store.authenticate(_bearer(authorization))
            state["principal"] = principal
            return principal
        principal = state.get("principal")
        if principal is None:
            raise AuthenticationError(
                "Bearer API key or dashboard session required"
            )
        return principal

    def authenticate(authorization: str | None, scope: str) -> Principal:
        principal = current_principal(authorization)
        control_plane.require(principal, scope)
        return principal

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/dashboard")

    @app.get("/dashboard", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(
            dashboard_dir / "dashboard.html", media_type="text/html"
        )

    @app.get("/dashboard/local", include_in_schema=False)
    def local_dashboard() -> FileResponse:
        if not enable_local_dashboard_login:
            raise HTTPException(404, "local dashboard login is disabled")
        return FileResponse(
            dashboard_dir / "dashboard.html", media_type="text/html"
        )

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/dashboard/session")
    def create_dashboard_session(body: DashboardLogin) -> JSONResponse:
        token, csrf_token, session_id, principal = (
            control_plane.store.create_web_session(
                body.api_key,
                ttl_seconds=session_ttl_seconds,
                timestamp=control_plane._now().isoformat(),
            )
        )
        control_plane.record_audit(
            principal,
            action="dashboard.login",
            session_id=session_id,
            resource_type="dashboard_session",
            resource_id=session_id,
        )
        response = JSONResponse(
            {
                "authenticated": True,
                "csrf_token": csrf_token,
                "expires_in": session_ttl_seconds,
            }
        )
        response.set_cookie(
            "bespoke_dashboard_session",
            token,
            max_age=session_ttl_seconds,
            httponly=True,
            secure=session_cookie_secure,
            samesite="strict",
            path="/",
        )
        return response

    @app.delete("/v1/dashboard/session", status_code=204)
    def delete_dashboard_session(
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        principal = current_principal(authorization)
        state = request_auth.get({})
        session_id = state.get("session_id")
        if session_id is None:
            raise ValueError("dashboard session is not active")
        control_plane.store.revoke_web_session(
            session_id, timestamp=control_plane._now().isoformat()
        )
        control_plane.record_audit(
            principal,
            action="dashboard.logout",
            session_id=session_id,
            resource_type="dashboard_session",
            resource_id=session_id,
        )
        response = Response(status_code=204)
        response.delete_cookie(
            "bespoke_dashboard_session",
            path="/",
            secure=session_cookie_secure,
            httponly=True,
            samesite="strict",
        )
        return response

    @app.get("/v1/session")
    def session(
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        principal = current_principal(authorization)
        state = request_auth.get({})
        session_id = state.get("session_id")
        result = {
            "api_key_id": principal.api_key_id,
            "scopes": sorted(principal.scopes),
            "role": (
                "administrator"
                if principal.allows("policies:write")
                and principal.allows("providers:write")
                else "operator"
                if principal.allows("sandboxes:terminate")
                else "observer"
            ),
            "can_terminate": principal.allows("sandboxes:terminate"),
            "can_view_policy": principal.allows("policies:read")
            and principal.allows("usage:read"),
            "can_manage_policy": principal.allows("policies:write"),
            "can_view_providers": principal.allows("providers:read"),
            "can_manage_providers": principal.allows("providers:write"),
            "can_create": principal.allows("sandboxes:create"),
            "can_view_alerts": principal.allows("alerts:read"),
            "can_view_audit": principal.allows("audit:read"),
            "can_export": principal.allows("exports:read"),
            "auth_mode": "session" if session_id else "api_key",
        }
        if session_id:
            result["csrf_token"] = control_plane.store.rotate_session_csrf(
                session_id
            )
        return result

    @app.get("/v1/policies/current")
    def get_policy(
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        principal = authenticate(authorization, "policies:read")
        policy = dataclasses.asdict(control_plane.get_policy(principal))
        policy.pop("organization_id")
        return policy

    @app.put("/v1/policies/current")
    def set_policy(
        body: OrganizationPolicyUpdate,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        principal = authenticate(authorization, "policies:write")
        policy = dataclasses.asdict(
            control_plane.set_policy(principal, **body.model_dump())
        )
        policy.pop("organization_id")
        return policy

    @app.get("/v1/policy-summary")
    def policy_summary(
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        principal = authenticate(authorization, "policies:read")
        summary = control_plane.policy_summary(principal)
        summary["policy"] = dataclasses.asdict(summary["policy"])
        summary["policy"].pop("organization_id")
        return summary

    @app.get("/v1/providers")
    def provider_summary(
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        principal = authenticate(authorization, "providers:read")
        return {"items": control_plane.provider_summary(principal)}

    @app.post("/v1/providers/{backend}/health-check")
    def check_provider_health(
        backend: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        principal = authenticate(authorization, "providers:write")
        return control_plane.check_provider_health(principal, backend)

    @app.get("/v1/alerts/configuration")
    def get_alert_configuration(
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        principal = authenticate(authorization, "alerts:read")
        result = dataclasses.asdict(
            control_plane.get_alert_configuration(principal)
        )
        result.pop("organization_id")
        return result

    @app.put("/v1/alerts/configuration")
    def set_alert_configuration(
        body: AlertConfigurationUpdate,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        principal = authenticate(authorization, "alerts:write")
        result = dataclasses.asdict(
            control_plane.set_alert_configuration(
                principal, **body.model_dump()
            )
        )
        result.pop("organization_id")
        return result

    @app.get("/v1/alerts")
    def list_alerts(
        authorization: Annotated[str | None, Header()] = None,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict:
        principal = authenticate(authorization, "alerts:read")
        items, next_offset = control_plane.alert_history(
            principal, offset=decode_cursor(cursor), limit=limit
        )
        return {"items": items, "next_cursor": encode_cursor(next_offset or 0)}

    @app.get("/v1/audit")
    def list_audit(
        authorization: Annotated[str | None, Header()] = None,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict:
        principal = authenticate(authorization, "audit:read")
        items, next_offset = control_plane.audit_history(
            principal, offset=decode_cursor(cursor), limit=limit
        )
        return {"items": items, "next_cursor": encode_cursor(next_offset or 0)}

    @app.get("/v1/retention/current")
    def get_retention_policy(
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        principal = authenticate(authorization, "retention:read")
        result = dataclasses.asdict(
            control_plane.get_retention_policy(principal)
        )
        result.pop("organization_id")
        return result

    @app.put("/v1/retention/current")
    def set_retention_policy(
        body: RetentionPolicyUpdate,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        principal = authenticate(authorization, "retention:write")
        result = dataclasses.asdict(
            control_plane.set_retention_policy(principal, **body.model_dump())
        )
        result.pop("organization_id")
        return result

    @app.post("/v1/retention/apply")
    def apply_retention(
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, int]:
        principal = authenticate(authorization, "retention:write")
        return control_plane.apply_retention(principal)

    @app.get("/v1/exports/{kind}.csv")
    def export_csv(
        kind: str,
        authorization: Annotated[str | None, Header()] = None,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 500,
    ) -> Response:
        principal = authenticate(authorization, "exports:read")
        columns, rows, next_offset = control_plane.export_rows(
            principal,
            kind=kind,
            offset=decode_cursor(cursor),
            limit=limit,
        )
        output = io.StringIO(newline="")
        writer = csv.DictWriter(
            output, fieldnames=columns, extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(_spreadsheet_safe_csv_rows(columns, rows))
        headers = {
            "Content-Disposition": f'attachment; filename="bespoke-{kind}.csv"',
            "X-Next-Cursor": encode_cursor(next_offset or 0) or "",
        }
        return Response(
            output.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers=headers,
        )

    @app.post("/v1/organizations", status_code=status.HTTP_201_CREATED)
    def create_organization(
        body: OrganizationCreate,
        x_control_plane_admin: Annotated[str | None, Header()] = None,
    ) -> dict:
        if admin_token is None:
            raise HTTPException(503, "organization bootstrap is disabled")
        if x_control_plane_admin is None or not secrets.compare_digest(
            x_control_plane_admin, admin_token
        ):
            raise HTTPException(401, "invalid control-plane admin token")
        organization, api_key = control_plane.bootstrap_organization(body.name)
        return {
            "organization": organization,
            "api_key": {
                "id": api_key.id,
                "name": api_key.name,
                "prefix": api_key.prefix,
                "scopes": api_key.scopes,
                "secret": api_key.secret,
                "created_at": api_key.created_at,
            },
        }

    @app.get("/v1/api-keys")
    def list_api_keys(
        authorization: Annotated[str | None, Header()] = None,
    ) -> list[dict]:
        principal = authenticate(authorization, "keys:write")
        return control_plane.store.list_api_keys(principal.organization_id)

    @app.post("/v1/api-keys", status_code=status.HTTP_201_CREATED)
    def issue_api_key(
        body: APIKeyCreate,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        principal = authenticate(authorization, "keys:write")
        issued = control_plane.issue_api_key(
            principal,
            name=body.name,
            scopes=body.scopes,
            expires_at=body.expires_at.isoformat() if body.expires_at else None,
        )
        return {
            "id": issued.id,
            "name": issued.name,
            "prefix": issued.prefix,
            "scopes": issued.scopes,
            "secret": issued.secret,
            "created_at": issued.created_at,
        }

    @app.delete("/v1/api-keys/{key_id}", status_code=204)
    def revoke_api_key(
        key_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        principal = authenticate(authorization, "keys:write")
        control_plane.store.revoke_api_key(principal.organization_id, key_id)

    @app.post("/v1/sandboxes", status_code=status.HTTP_201_CREATED)
    def create_sandbox(
        body: SandboxCreate,
        authorization: Annotated[str | None, Header()] = None,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> dict:
        principal = authenticate(authorization, "sandboxes:create")
        values = body.model_dump(exclude={"backend"}, exclude_none=True)
        return _sandbox_response(
            control_plane.create_sandbox(
                principal,
                body.backend,
                values,
                idempotency_key=idempotency_key,
            )
        )

    @app.get("/v1/sandboxes")
    def list_sandboxes(
        authorization: Annotated[str | None, Header()] = None,
    ) -> list[dict]:
        principal = authenticate(authorization, "sandboxes:read")
        return [
            _sandbox_response(item)
            for item in control_plane.list_sandboxes(principal)
        ]

    @app.get("/v1/sandboxes/{sandbox_id}")
    def get_sandbox(
        sandbox_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        principal = authenticate(authorization, "sandboxes:read")
        return _sandbox_response(
            control_plane.get_sandbox(principal, sandbox_id)
        )

    @app.get("/v1/sandboxes/{sandbox_id}/detail")
    def sandbox_detail(
        sandbox_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        principal = authenticate(authorization, "sandboxes:read")
        detail = control_plane.sandbox_detail(principal, sandbox_id)
        detail["sandbox"] = _sandbox_response(detail["sandbox"])
        for attempt in detail["attempts"]:
            attempt.pop("error_json", None)
        for execution in detail["executions"]:
            execution.pop("response_json", None)
        return detail

    @app.post("/v1/sandboxes/{sandbox_id}/execute")
    def execute(
        sandbox_id: str,
        body: ExecuteRequest,
        authorization: Annotated[str | None, Header()] = None,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> dict:
        principal = authenticate(authorization, "sandboxes:execute")
        return control_plane.execute(
            principal,
            sandbox_id,
            code=body.code,
            language=body.language,
            command=body.command,
            args=body.args,
            request_id=idempotency_key,
        )

    @app.delete("/v1/sandboxes/{sandbox_id}")
    def destroy_sandbox(
        sandbox_id: str,
        authorization: Annotated[str | None, Header()] = None,
        if_match: Annotated[str | None, Header()] = None,
    ) -> dict:
        principal = authenticate(authorization, "sandboxes:terminate")
        return _sandbox_response(
            control_plane.destroy_sandbox(
                principal,
                sandbox_id,
                expected_version=_if_match_version(if_match),
            )
        )

    @app.get("/v1/costs")
    def costs(
        authorization: Annotated[str | None, Header()] = None,
        group_by: Annotated[
            str, Query(pattern="^(sandbox|backend|day)$")
        ] = "sandbox",
        start: str | None = None,
        end: str | None = None,
    ) -> dict:
        principal = authenticate(authorization, "usage:read")
        items = control_plane.cost_summary(
            principal, group_by=group_by, start=start, end=end
        )
        return {"group_by": group_by, "items": items}

    @app.get("/v1/reconciliation")
    def reconciliation_summary(
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        principal = authenticate(authorization, "sandboxes:read")
        return control_plane.reconciliation_summary(principal)

    @app.post("/v1/reconciliation/{backend}")
    def reconcile(
        backend: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        principal = authenticate(authorization, "providers:reconcile")
        return control_plane.reconcile(principal, backend)

    return app
