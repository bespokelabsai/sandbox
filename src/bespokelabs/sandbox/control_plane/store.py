"""SQLite persistence for API keys, sandbox metadata, and usage ledgers."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from bespokelabs.sandbox.control_plane.errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PolicyDeniedError,
)
from bespokelabs.sandbox.control_plane.models import (
    AlertConfiguration,
    CostSummary,
    IssuedAPIKey,
    OrganizationPolicy,
    Principal,
    RetentionPolicy,
    SandboxRecord,
)
from bespokelabs.sandbox.control_plane.security import redact_payload

_SCHEMA = """
CREATE TABLE IF NOT EXISTS organizations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    name TEXT NOT NULL,
    key_prefix TEXT NOT NULL,
    key_digest TEXT NOT NULL UNIQUE,
    scopes_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    expires_at TEXT,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS api_keys_org_idx
    ON api_keys(organization_id);

CREATE TABLE IF NOT EXISTS sandboxes (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    backend TEXT NOT NULL,
    status TEXT NOT NULL,
    config_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    destroyed_at TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS sandboxes_org_idx
    ON sandboxes(organization_id, created_at);

CREATE TABLE IF NOT EXISTS executions (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    sandbox_id TEXT NOT NULL REFERENCES sandboxes(id),
    request_id TEXT NOT NULL,
    status TEXT NOT NULL,
    response_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(organization_id, sandbox_id, request_id)
);

CREATE TABLE IF NOT EXISTS usage_events (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    sandbox_id TEXT NOT NULL REFERENCES sandboxes(id),
    execution_id TEXT REFERENCES executions(id),
    request_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    quantity TEXT NOT NULL,
    unit TEXT NOT NULL,
    provider_cost_usd TEXT NOT NULL,
    customer_cost_usd TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(organization_id, sandbox_id, request_id, metric)
);
CREATE INDEX IF NOT EXISTS usage_events_org_idx
    ON usage_events(organization_id, created_at);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    sandbox_id TEXT NOT NULL REFERENCES sandboxes(id),
    usage_event_id TEXT NOT NULL REFERENCES usage_events(id),
    kind TEXT NOT NULL,
    amount_usd TEXT NOT NULL,
    price_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(usage_event_id, kind)
);
"""

_MIGRATIONS = (
    (
        1,
        (
            "ALTER TABLE sandboxes ADD COLUMN idempotency_key TEXT",
            "ALTER TABLE sandboxes ADD COLUMN request_fingerprint TEXT",
            "ALTER TABLE sandboxes ADD COLUMN provider_resource_id TEXT",
            "ALTER TABLE sandboxes ADD COLUMN latest_error_json TEXT",
            "ALTER TABLE sandboxes ADD COLUMN retry_status TEXT NOT NULL "
            "DEFAULT 'not_applicable'",
            "ALTER TABLE sandboxes ADD COLUMN cleanup_status TEXT",
            "CREATE UNIQUE INDEX sandboxes_org_idempotency_idx "
            "ON sandboxes(organization_id, idempotency_key) "
            "WHERE idempotency_key IS NOT NULL",
            """
            CREATE TABLE sandbox_creation_attempts (
                id TEXT PRIMARY KEY,
                sandbox_id TEXT NOT NULL REFERENCES sandboxes(id),
                attempt_number INTEGER NOT NULL,
                status TEXT NOT NULL,
                error_json TEXT,
                retry_status TEXT NOT NULL,
                cleanup_status TEXT,
                provider_resource_id TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                UNIQUE(sandbox_id, attempt_number)
            )
            """,
            "CREATE INDEX sandbox_creation_attempts_sandbox_idx "
            "ON sandbox_creation_attempts(sandbox_id, attempt_number)",
        ),
    ),
    (
        2,
        (
            "ALTER TABLE sandboxes ADD COLUMN requested_at TEXT",
            "ALTER TABLE sandboxes ADD COLUMN provisioning_at TEXT",
            "ALTER TABLE sandboxes ADD COLUMN running_at TEXT",
            "ALTER TABLE sandboxes ADD COLUMN stopping_at TEXT",
            "ALTER TABLE sandboxes ADD COLUMN terminated_at TEXT",
            "ALTER TABLE sandboxes ADD COLUMN failed_at TEXT",
            "ALTER TABLE sandboxes ADD COLUMN last_provider_observed_at TEXT",
            "ALTER TABLE sandboxes ADD COLUMN hourly_rate_usd TEXT NOT NULL DEFAULT '0'",
            "ALTER TABLE sandboxes ADD COLUMN currency TEXT NOT NULL DEFAULT 'USD'",
            "ALTER TABLE sandboxes ADD COLUMN pricing_source TEXT NOT NULL DEFAULT 'bundled'",
            "ALTER TABLE sandboxes ADD COLUMN cost_state TEXT NOT NULL DEFAULT 'estimated'",
            "ALTER TABLE sandboxes ADD COLUMN provider_missing INTEGER NOT NULL DEFAULT 0",
            """
            CREATE TABLE lifecycle_costs (
                sandbox_id TEXT PRIMARY KEY REFERENCES sandboxes(id),
                organization_id TEXT NOT NULL,
                billable_seconds TEXT NOT NULL,
                estimated_cost_usd TEXT NOT NULL,
                provider_reported_cost_usd TEXT,
                effective_cost_usd TEXT NOT NULL,
                customer_cost_usd TEXT NOT NULL,
                currency TEXT NOT NULL,
                pricing_source TEXT NOT NULL,
                cost_state TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE lifecycle_ledger_entries (
                id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL,
                sandbox_id TEXT NOT NULL REFERENCES sandboxes(id),
                observation_id TEXT NOT NULL,
                provider_delta_usd TEXT NOT NULL,
                customer_delta_usd TEXT NOT NULL,
                cost_state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(sandbox_id, observation_id)
            )
            """,
            "CREATE INDEX lifecycle_ledger_org_idx ON lifecycle_ledger_entries(organization_id, created_at)",
            """
            UPDATE sandboxes SET requested_at = created_at,
                provisioning_at = created_at
            WHERE requested_at IS NULL
            """,
        ),
    ),
    (
        3,
        (
            "ALTER TABLE lifecycle_ledger_entries ADD COLUMN billable_seconds_delta TEXT NOT NULL DEFAULT '0'",
            """
            CREATE TABLE lifecycle_observations (
                sandbox_id TEXT NOT NULL REFERENCES sandboxes(id),
                observation_id TEXT NOT NULL,
                PRIMARY KEY(sandbox_id, observation_id)
            )
            """,
            """
            INSERT INTO lifecycle_observations(sandbox_id, observation_id)
            SELECT sandbox_id, observation_id FROM lifecycle_ledger_entries
            """,
            """
            UPDATE lifecycle_ledger_entries
            SET billable_seconds_delta = COALESCE(
                (SELECT lc.billable_seconds FROM lifecycle_costs lc
                 WHERE lc.sandbox_id = lifecycle_ledger_entries.sandbox_id),
                '0'
            )
            WHERE rowid = (
                SELECT MAX(previous.rowid)
                FROM lifecycle_ledger_entries previous
                WHERE previous.sandbox_id = lifecycle_ledger_entries.sandbox_id
            )
            """,
        ),
    ),
    (
        4,
        (
            "ALTER TABLE sandboxes ADD COLUMN creator_api_key_id TEXT REFERENCES api_keys(id)",
            "ALTER TABLE sandboxes ADD COLUMN version INTEGER NOT NULL DEFAULT 1",
            "CREATE INDEX sandboxes_org_status_idx ON sandboxes(organization_id, status, created_at)",
            """
            CREATE TABLE provider_observations (
                id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL,
                sandbox_id TEXT NOT NULL REFERENCES sandboxes(id),
                observation_id TEXT NOT NULL,
                provider_resource_id TEXT,
                status TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                provider_cost_usd TEXT,
                currency TEXT,
                missing INTEGER NOT NULL DEFAULT 0,
                UNIQUE(sandbox_id, observation_id)
            )
            """,
            "CREATE INDEX provider_observations_sandbox_idx ON provider_observations(sandbox_id, observed_at)",
        ),
    ),
    (
        5,
        (
            "ALTER TABLE sandboxes ADD COLUMN expires_at TEXT",
            "ALTER TABLE sandboxes ADD COLUMN terminated_by_api_key_id TEXT REFERENCES api_keys(id)",
            "ALTER TABLE sandboxes ADD COLUMN termination_reason TEXT",
            "ALTER TABLE sandboxes ADD COLUMN supervision_owner TEXT",
            "ALTER TABLE sandboxes ADD COLUMN supervision_claimed_at TEXT",
            """
            CREATE TABLE organization_policies (
                organization_id TEXT PRIMARY KEY REFERENCES organizations(id),
                max_concurrent_sandboxes INTEGER,
                hourly_spend_limit_usd TEXT,
                daily_spend_limit_usd TEXT,
                allowed_backends_json TEXT,
                allowed_gpu_types_json TEXT,
                max_sandbox_lifetime_secs INTEGER,
                version INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                updated_by_api_key_id TEXT REFERENCES api_keys(id)
            )
            """,
            """
            CREATE TABLE policy_denials (
                id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL REFERENCES organizations(id),
                api_key_id TEXT REFERENCES api_keys(id),
                policy TEXT NOT NULL,
                message TEXT NOT NULL,
                backend TEXT,
                gpu TEXT,
                requested_timeout_secs INTEGER,
                current_value TEXT,
                limit_value TEXT,
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX policy_denials_org_created_idx ON policy_denials(organization_id, created_at)",
            """
            CREATE TABLE provider_health (
                backend TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                checked_at TEXT NOT NULL,
                message TEXT NOT NULL,
                checked_by_api_key_id TEXT REFERENCES api_keys(id)
            )
            """,
            """
            CREATE TABLE orphan_cleanups (
                backend TEXT NOT NULL,
                provider_resource_id TEXT NOT NULL,
                organization_id TEXT NOT NULL REFERENCES organizations(id),
                status TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                completed_at TEXT,
                PRIMARY KEY(backend, provider_resource_id)
            )
            """,
            "CREATE INDEX sandboxes_expiry_idx ON sandboxes(expires_at, status)",
        ),
    ),
    (
        6,
        (
            """
            CREATE TABLE web_sessions (
                id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL REFERENCES organizations(id),
                api_key_id TEXT NOT NULL REFERENCES api_keys(id),
                token_digest TEXT NOT NULL UNIQUE,
                csrf_digest TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_used_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked_at TEXT
            )
            """,
            "CREATE INDEX web_sessions_org_expiry_idx ON web_sessions(organization_id, expires_at)",
            """
            CREATE TABLE alert_configurations (
                organization_id TEXT PRIMARY KEY REFERENCES organizations(id),
                budget_threshold_percent INTEGER,
                repeated_failures_count INTEGER,
                repeated_failures_window_minutes INTEGER NOT NULL DEFAULT 60,
                provider_degradation_enabled INTEGER NOT NULL DEFAULT 1,
                long_running_secs INTEGER,
                failed_cleanup_enabled INTEGER NOT NULL DEFAULT 1,
                version INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                updated_by_api_key_id TEXT REFERENCES api_keys(id)
            )
            """,
            """
            CREATE TABLE alert_events (
                id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL REFERENCES organizations(id),
                alert_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                message TEXT NOT NULL,
                resource_type TEXT,
                resource_id TEXT,
                dedupe_key TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(organization_id, dedupe_key)
            )
            """,
            "CREATE INDEX alert_events_org_created_idx ON alert_events(organization_id, created_at DESC)",
            """
            CREATE TABLE audit_log (
                id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL REFERENCES organizations(id),
                api_key_id TEXT REFERENCES api_keys(id),
                session_id TEXT REFERENCES web_sessions(id),
                action TEXT NOT NULL,
                resource_type TEXT,
                resource_id TEXT,
                outcome TEXT NOT NULL,
                details_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX audit_log_org_created_idx ON audit_log(organization_id, created_at DESC)",
            """
            CREATE TABLE retention_policies (
                organization_id TEXT PRIMARY KEY REFERENCES organizations(id),
                operational_days INTEGER NOT NULL DEFAULT 90,
                audit_days INTEGER NOT NULL DEFAULT 365,
                version INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                updated_by_api_key_id TEXT REFERENCES api_keys(id)
            )
            """,
            "CREATE INDEX ledger_entries_org_created_idx ON ledger_entries(organization_id, created_at)",
        ),
    ),
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _normalize_expiration(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("expires_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("expires_at must include a timezone")
    return parsed.astimezone(UTC).isoformat()


class SQLiteStore:
    """Small durable store suitable for a single control-plane deployment.

    A separate connection is opened per operation, which keeps this safe for
    FastAPI's worker threads. Production deployments can retain the service
    interface and replace this class with a Postgres implementation.
    """

    def __init__(self, path: str | Path, *, key_pepper: str) -> None:
        if not key_pepper:
            raise ValueError("key_pepper must not be empty")
        self._path = str(path)
        self._pepper = key_pepper.encode()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(_SCHEMA)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            applied = {
                row[0]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations"
                )
            }
            for version, statements in _MIGRATIONS:
                if version in applied:
                    continue
                for statement in statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) "
                    "VALUES (?, ?)",
                    (version, _now()),
                )

    def create_organization(self, name: str) -> dict[str, str]:
        name = name.strip()
        if not name:
            raise ValueError("organization name must not be empty")
        organization_id = _id("org")
        created_at = _now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO organizations(id, name, created_at) VALUES (?, ?, ?)",
                (organization_id, name, created_at),
            )
        return {
            "id": organization_id,
            "name": name,
            "created_at": created_at,
        }

    def issue_api_key(
        self,
        organization_id: str,
        *,
        name: str,
        scopes: Iterable[str] = ("*",),
        expires_at: str | None = None,
    ) -> IssuedAPIKey:
        name = name.strip()
        if not name:
            raise ValueError("API-key name must not be empty")
        normalized_scopes = tuple(sorted(set(scopes)))
        if not normalized_scopes:
            raise ValueError("at least one API-key scope is required")
        secret = "bsk_live_" + secrets.token_urlsafe(32)
        prefix = secret[:17]
        key_id = _id("key")
        created_at = _now()
        expires_at = _normalize_expiration(expires_at)
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO api_keys(
                        id, organization_id, name, key_prefix, key_digest,
                        scopes_json, created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key_id,
                        organization_id,
                        name,
                        prefix,
                        self._digest(secret),
                        json.dumps(normalized_scopes),
                        created_at,
                        expires_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise NotFoundError("organization not found") from exc
        return IssuedAPIKey(
            id=key_id,
            organization_id=organization_id,
            name=name,
            prefix=prefix,
            scopes=normalized_scopes,
            secret=secret,
            created_at=created_at,
        )

    def authenticate(self, secret: str) -> Principal:
        if not secret.startswith("bsk_live_"):
            raise AuthenticationError("invalid API key")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, organization_id, scopes_json, expires_at, revoked_at
                FROM api_keys WHERE key_digest = ?
                """,
                (self._digest(secret),),
            ).fetchone()
            if row is None or row["revoked_at"] is not None:
                raise AuthenticationError("invalid API key")
            if row["expires_at"] and row["expires_at"] <= _now():
                raise AuthenticationError("API key has expired")
            connection.execute(
                "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
                (_now(), row["id"]),
            )
        return Principal(
            organization_id=row["organization_id"],
            api_key_id=row["id"],
            scopes=frozenset(json.loads(row["scopes_json"])),
        )

    def create_web_session(
        self,
        secret: str,
        *,
        ttl_seconds: int,
        timestamp: str | None = None,
    ) -> tuple[str, str, str, Principal]:
        """Exchange an API key for an opaque, hashed browser session."""
        if ttl_seconds < 1:
            raise ValueError("session lifetime must be positive")
        principal = self.authenticate(secret)
        created_at = timestamp or _now()
        expires_at = (
            datetime.fromisoformat(created_at) + timedelta(seconds=ttl_seconds)
        ).isoformat()
        session_id = _id("ses")
        token = "bss_" + secrets.token_urlsafe(32)
        csrf_token = "csrf_" + secrets.token_urlsafe(24)
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO web_sessions(
                   id, organization_id, api_key_id, token_digest, csrf_digest,
                   created_at, last_used_at, expires_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    principal.organization_id,
                    principal.api_key_id,
                    self._digest(token),
                    self._digest(csrf_token),
                    created_at,
                    created_at,
                    expires_at,
                ),
            )
        return token, csrf_token, session_id, principal

    def authenticate_web_session(
        self, token: str, *, timestamp: str | None = None
    ) -> tuple[Principal, str]:
        """Authenticate a non-revoked browser session and its source key."""
        when = timestamp or _now()
        with self._connect() as connection:
            row = connection.execute(
                """SELECT s.id AS session_id, s.organization_id,
                   s.api_key_id, s.expires_at AS session_expires_at,
                   s.revoked_at AS session_revoked_at, k.scopes_json,
                   k.expires_at AS key_expires_at, k.revoked_at AS key_revoked_at
                   FROM web_sessions s JOIN api_keys k ON k.id=s.api_key_id
                   WHERE s.token_digest=?""",
                (self._digest(token),),
            ).fetchone()
            if (
                row is None
                or row["session_revoked_at"] is not None
                or row["key_revoked_at"] is not None
                or row["session_expires_at"] <= when
                or (row["key_expires_at"] and row["key_expires_at"] <= when)
            ):
                raise AuthenticationError("invalid dashboard session")
            connection.execute(
                "UPDATE web_sessions SET last_used_at=? WHERE id=?",
                (when, row["session_id"]),
            )
        return (
            Principal(
                organization_id=row["organization_id"],
                api_key_id=row["api_key_id"],
                scopes=frozenset(json.loads(row["scopes_json"])),
            ),
            row["session_id"],
        )

    def validate_session_csrf(self, session_id: str, token: str | None) -> bool:
        if token is None:
            return False
        with self._connect() as connection:
            row = connection.execute(
                "SELECT csrf_digest FROM web_sessions WHERE id=?",
                (session_id,),
            ).fetchone()
        return row is not None and hmac.compare_digest(
            row["csrf_digest"], self._digest(token)
        )

    def rotate_session_csrf(self, session_id: str) -> str:
        token = "csrf_" + secrets.token_urlsafe(24)
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE web_sessions SET csrf_digest=?
                   WHERE id=? AND revoked_at IS NULL""",
                (self._digest(token), session_id),
            )
        if cursor.rowcount == 0:
            raise AuthenticationError("invalid dashboard session")
        return token

    def revoke_web_session(
        self, session_id: str, *, timestamp: str | None = None
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE web_sessions SET revoked_at=COALESCE(revoked_at, ?)
                   WHERE id=?""",
                (timestamp or _now(), session_id),
            )

    def revoke_api_key(self, organization_id: str, key_id: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE api_keys SET revoked_at = ?
                WHERE id = ? AND organization_id = ? AND revoked_at IS NULL
                """,
                (_now(), key_id, organization_id),
            )
            if cursor.rowcount == 0:
                raise NotFoundError("API key not found")

    def list_api_keys(self, organization_id: str) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, name, key_prefix, scopes_json, created_at,
                       last_used_at, expires_at, revoked_at
                FROM api_keys WHERE organization_id = ? ORDER BY created_at
                """,
                (organization_id,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "prefix": row["key_prefix"],
                "scopes": json.loads(row["scopes_json"]),
                "created_at": row["created_at"],
                "last_used_at": row["last_used_at"],
                "expires_at": row["expires_at"],
                "revoked_at": row["revoked_at"],
            }
            for row in rows
        ]

    def get_policy(self, organization_id: str) -> OrganizationPolicy:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM organization_policies WHERE organization_id=?",
                (organization_id,),
            ).fetchone()
        if row is None:
            return OrganizationPolicy(organization_id=organization_id)
        return self._policy_from_row(row)

    def set_policy(
        self,
        organization_id: str,
        *,
        actor_api_key_id: str,
        max_concurrent_sandboxes: int | None,
        hourly_spend_limit_usd: Decimal | None,
        daily_spend_limit_usd: Decimal | None,
        allowed_backends: Iterable[str] | None,
        allowed_gpu_types: Iterable[str] | None,
        max_sandbox_lifetime_secs: int | None,
        timestamp: str | None = None,
    ) -> OrganizationPolicy:
        updated_at = timestamp or _now()
        backend_values = (
            tuple(sorted(set(allowed_backends)))
            if allowed_backends is not None
            else None
        )
        gpu_values = (
            tuple(sorted(set(allowed_gpu_types)))
            if allowed_gpu_types is not None
            else None
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO organization_policies(
                   organization_id, max_concurrent_sandboxes,
                   hourly_spend_limit_usd, daily_spend_limit_usd,
                   allowed_backends_json, allowed_gpu_types_json,
                   max_sandbox_lifetime_secs, version, updated_at,
                   updated_by_api_key_id
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                   ON CONFLICT(organization_id) DO UPDATE SET
                   max_concurrent_sandboxes=excluded.max_concurrent_sandboxes,
                   hourly_spend_limit_usd=excluded.hourly_spend_limit_usd,
                   daily_spend_limit_usd=excluded.daily_spend_limit_usd,
                   allowed_backends_json=excluded.allowed_backends_json,
                   allowed_gpu_types_json=excluded.allowed_gpu_types_json,
                   max_sandbox_lifetime_secs=excluded.max_sandbox_lifetime_secs,
                   version=organization_policies.version+1,
                   updated_at=excluded.updated_at,
                   updated_by_api_key_id=excluded.updated_by_api_key_id""",
                (
                    organization_id,
                    max_concurrent_sandboxes,
                    str(hourly_spend_limit_usd)
                    if hourly_spend_limit_usd is not None
                    else None,
                    str(daily_spend_limit_usd)
                    if daily_spend_limit_usd is not None
                    else None,
                    json.dumps(backend_values)
                    if backend_values is not None
                    else None,
                    json.dumps(gpu_values) if gpu_values is not None else None,
                    max_sandbox_lifetime_secs,
                    updated_at,
                    actor_api_key_id,
                ),
            )
        return self.get_policy(organization_id)

    def policy_summary(
        self, organization_id: str, *, timestamp: str | None = None
    ) -> dict:
        when = datetime.fromisoformat(timestamp or _now()).astimezone(UTC)
        policy = self.get_policy(organization_id)
        hour_start = when - timedelta(hours=1)
        day_start = when.replace(hour=0, minute=0, second=0, microsecond=0)
        with self._connect() as connection:
            active = connection.execute(
                """SELECT COUNT(*) FROM sandboxes WHERE organization_id=?
                   AND status IN ('creating', 'running', 'stopping')""",
                (organization_id,),
            ).fetchone()[0]
            hourly = self._spend_between(
                connection,
                organization_id,
                hour_start.isoformat(),
                when.isoformat(),
            )
            daily = self._spend_between(
                connection,
                organization_id,
                day_start.isoformat(),
                when.isoformat(),
            )
            denials = connection.execute(
                """SELECT d.policy, d.message, d.backend, d.gpu,
                   d.requested_timeout_secs, d.current_value, d.limit_value,
                   d.created_at, d.api_key_id,
                   (SELECT k.name FROM api_keys k
                    WHERE k.id=d.api_key_id) AS api_key_name
                   FROM policy_denials d WHERE d.organization_id=?
                   ORDER BY d.created_at DESC LIMIT 20""",
                (organization_id,),
            ).fetchall()
        return {
            "policy": policy,
            "usage": {
                "active_sandboxes": active,
                "hourly_spend_usd": str(hourly),
                "daily_spend_usd": str(daily),
            },
            "denials": [dict(row) for row in denials],
        }

    def get_alert_configuration(
        self, organization_id: str
    ) -> AlertConfiguration:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM alert_configurations
                   WHERE organization_id=?""",
                (organization_id,),
            ).fetchone()
        if row is None:
            return AlertConfiguration(organization_id=organization_id)
        return AlertConfiguration(
            organization_id=row["organization_id"],
            budget_threshold_percent=row["budget_threshold_percent"],
            repeated_failures_count=row["repeated_failures_count"],
            repeated_failures_window_minutes=row[
                "repeated_failures_window_minutes"
            ],
            provider_degradation_enabled=bool(
                row["provider_degradation_enabled"]
            ),
            long_running_secs=row["long_running_secs"],
            failed_cleanup_enabled=bool(row["failed_cleanup_enabled"]),
            version=row["version"],
            updated_at=row["updated_at"],
            updated_by_api_key_id=row["updated_by_api_key_id"],
        )

    def set_alert_configuration(
        self,
        organization_id: str,
        *,
        actor_api_key_id: str,
        budget_threshold_percent: int | None,
        repeated_failures_count: int | None,
        repeated_failures_window_minutes: int,
        provider_degradation_enabled: bool,
        long_running_secs: int | None,
        failed_cleanup_enabled: bool,
        timestamp: str | None = None,
    ) -> AlertConfiguration:
        updated_at = timestamp or _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO alert_configurations(
                   organization_id, budget_threshold_percent,
                   repeated_failures_count, repeated_failures_window_minutes,
                   provider_degradation_enabled, long_running_secs,
                   failed_cleanup_enabled, version, updated_at,
                   updated_by_api_key_id
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                   ON CONFLICT(organization_id) DO UPDATE SET
                   budget_threshold_percent=excluded.budget_threshold_percent,
                   repeated_failures_count=excluded.repeated_failures_count,
                   repeated_failures_window_minutes=excluded.repeated_failures_window_minutes,
                   provider_degradation_enabled=excluded.provider_degradation_enabled,
                   long_running_secs=excluded.long_running_secs,
                   failed_cleanup_enabled=excluded.failed_cleanup_enabled,
                   version=alert_configurations.version+1,
                   updated_at=excluded.updated_at,
                   updated_by_api_key_id=excluded.updated_by_api_key_id""",
                (
                    organization_id,
                    budget_threshold_percent,
                    repeated_failures_count,
                    repeated_failures_window_minutes,
                    int(provider_degradation_enabled),
                    long_running_secs,
                    int(failed_cleanup_enabled),
                    updated_at,
                    actor_api_key_id,
                ),
            )
        return self.get_alert_configuration(organization_id)

    def record_alert(
        self,
        organization_id: str,
        *,
        alert_type: str,
        severity: str,
        message: str,
        dedupe_key: str,
        created_at: str,
        resource_type: str | None = None,
        resource_id: str | None = None,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO alert_events(
                   id, organization_id, alert_type, severity, message,
                   resource_type, resource_id, dedupe_key, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _id("alt"),
                    organization_id,
                    alert_type,
                    severity,
                    message,
                    resource_type,
                    resource_id,
                    dedupe_key,
                    created_at,
                ),
            )
        return cursor.rowcount == 1

    def list_alerts(
        self, organization_id: str, *, offset: int, limit: int
    ) -> tuple[list[dict], int | None]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id, alert_type, severity, message, resource_type,
                   resource_id, created_at FROM alert_events
                   WHERE organization_id=? ORDER BY created_at DESC, id DESC
                   LIMIT ? OFFSET ?""",
                (organization_id, limit + 1, offset),
            ).fetchall()
        has_more = len(rows) > limit
        return [dict(row) for row in rows[:limit]], (
            offset + limit if has_more else None
        )

    def failed_sandbox_count(self, organization_id: str, *, since: str) -> int:
        with self._connect() as connection:
            return connection.execute(
                """SELECT COUNT(*) FROM sandboxes WHERE organization_id=?
                   AND failed_at IS NOT NULL AND failed_at>=?""",
                (organization_id, since),
            ).fetchone()[0]

    def long_running_sandboxes(
        self, organization_id: str, *, started_before: str
    ) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT s.id FROM sandboxes s WHERE s.organization_id=?
                   AND s.status IN ('creating', 'running', 'stopping')
                   AND s.created_at<=? ORDER BY s.created_at""",
                (organization_id, started_before),
            ).fetchall()
        return [row["id"] for row in rows]

    def record_audit(
        self,
        organization_id: str,
        *,
        action: str,
        outcome: str,
        created_at: str,
        api_key_id: str | None = None,
        session_id: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        details: dict | None = None,
    ) -> str:
        """Append a redacted audit record; no update API is exposed."""
        audit_id = _id("aud")
        safe_details = redact_payload(details or {})
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO audit_log(
                   id, organization_id, api_key_id, session_id, action,
                   resource_type, resource_id, outcome, details_json,
                   created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    audit_id,
                    organization_id,
                    api_key_id,
                    session_id,
                    action,
                    resource_type,
                    resource_id,
                    outcome,
                    json.dumps(safe_details, sort_keys=True),
                    created_at,
                ),
            )
        return audit_id

    def list_audit(
        self, organization_id: str, *, offset: int, limit: int
    ) -> tuple[list[dict], int | None]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT a.id, a.action, a.resource_type, a.resource_id,
                   a.outcome, a.details_json, a.created_at, a.api_key_id,
                   (SELECT k.name FROM api_keys k
                    WHERE k.id=a.api_key_id) AS api_key_name
                   FROM audit_log a WHERE a.organization_id=?
                   ORDER BY a.created_at DESC, a.id DESC LIMIT ? OFFSET ?""",
                (organization_id, limit + 1, offset),
            ).fetchall()
        has_more = len(rows) > limit
        items = []
        for row in rows[:limit]:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            items.append(item)
        return items, offset + limit if has_more else None

    def get_retention_policy(self, organization_id: str) -> RetentionPolicy:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM retention_policies WHERE organization_id=?",
                (organization_id,),
            ).fetchone()
        if row is None:
            return RetentionPolicy(organization_id=organization_id)
        return RetentionPolicy(
            organization_id=row["organization_id"],
            operational_days=row["operational_days"],
            audit_days=row["audit_days"],
            version=row["version"],
            updated_at=row["updated_at"],
            updated_by_api_key_id=row["updated_by_api_key_id"],
        )

    def set_retention_policy(
        self,
        organization_id: str,
        *,
        actor_api_key_id: str,
        operational_days: int,
        audit_days: int,
        timestamp: str | None = None,
    ) -> RetentionPolicy:
        updated_at = timestamp or _now()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO retention_policies(
                   organization_id, operational_days, audit_days, version,
                   updated_at, updated_by_api_key_id
                   ) VALUES (?, ?, ?, 1, ?, ?)
                   ON CONFLICT(organization_id) DO UPDATE SET
                   operational_days=excluded.operational_days,
                   audit_days=excluded.audit_days,
                   version=retention_policies.version+1,
                   updated_at=excluded.updated_at,
                   updated_by_api_key_id=excluded.updated_by_api_key_id""",
                (
                    organization_id,
                    operational_days,
                    audit_days,
                    updated_at,
                    actor_api_key_id,
                ),
            )
        return self.get_retention_policy(organization_id)

    def apply_retention(
        self, organization_id: str, *, timestamp: str
    ) -> dict[str, int]:
        policy = self.get_retention_policy(organization_id)
        when = datetime.fromisoformat(timestamp)
        operational_before = (
            when - timedelta(days=policy.operational_days)
        ).isoformat()
        audit_before = (when - timedelta(days=policy.audit_days)).isoformat()
        statements = {
            "alerts_deleted": (
                "DELETE FROM alert_events WHERE organization_id=? AND created_at<?",
                operational_before,
            ),
            "denials_deleted": (
                "DELETE FROM policy_denials WHERE organization_id=? AND created_at<?",
                operational_before,
            ),
            "observations_deleted": (
                "DELETE FROM provider_observations WHERE organization_id=? AND observed_at<?",
                operational_before,
            ),
            "audit_deleted": (
                "DELETE FROM audit_log WHERE organization_id=? AND created_at<?",
                audit_before,
            ),
        }
        deleted: dict[str, int] = {}
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for name, (statement, cutoff) in statements.items():
                deleted[name] = connection.execute(
                    statement, (organization_id, cutoff)
                ).rowcount
        return deleted

    def create_sandbox(
        self, organization_id: str, backend: str, config: dict
    ) -> SandboxRecord:
        record, _ = self.claim_sandbox_creation(
            organization_id, backend, config
        )
        return record

    def claim_sandbox_creation(
        self,
        organization_id: str,
        backend: str,
        config: dict,
        *,
        idempotency_key: str | None = None,
        request_payload: dict | None = None,
        timestamp: str | None = None,
        hourly_rate_usd: Decimal = Decimal("0"),
        currency: str = "USD",
        pricing_source: str = "bundled",
        creator_api_key_id: str | None = None,
        enforce_policy: bool = False,
    ) -> tuple[SandboxRecord, bool]:
        """Atomically claim a logical create and its first provider attempt.

        Returns ``(record, claimed)``. A repeated key returns the existing
        record with ``claimed=False`` and never starts another attempt.
        """
        timestamp = timestamp or _now()
        record = SandboxRecord(
            id=_id("sbx"),
            organization_id=organization_id,
            backend=backend,
            status="creating",
            config=config,
            created_at=timestamp,
        )
        fingerprint = self._request_fingerprint(
            request_payload
            if request_payload is not None
            else {"backend": backend, **config}
        )
        requested_timeout = config.get("timeout_secs")
        expires_at = None
        if requested_timeout is not None:
            expires_at = (
                datetime.fromisoformat(timestamp)
                + timedelta(seconds=int(requested_timeout))
            ).isoformat()
        existing_id: str | None = None
        denied: PolicyDeniedError | None = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if idempotency_key is not None:
                existing = connection.execute(
                    """SELECT id, request_fingerprint FROM sandboxes
                       WHERE organization_id=? AND idempotency_key=?""",
                    (organization_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    if not hmac.compare_digest(
                        existing["request_fingerprint"], fingerprint
                    ):
                        raise ConflictError(
                            "idempotency key was already used with a different request"
                        )
                    existing_id = existing["id"]
            if existing_id is None and enforce_policy:
                denied = self._policy_violation(
                    connection,
                    organization_id,
                    backend=backend,
                    gpu=config.get("gpu"),
                    requested_timeout_secs=requested_timeout,
                    timestamp=timestamp,
                )
                if denied is not None:
                    connection.execute(
                        """INSERT INTO policy_denials(
                           id, organization_id, api_key_id, policy, message,
                           backend, gpu, requested_timeout_secs, current_value,
                           limit_value, created_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            _id("deny"),
                            organization_id,
                            creator_api_key_id,
                            denied.policy,
                            str(denied),
                            backend,
                            config.get("gpu"),
                            requested_timeout,
                            str(denied.current)
                            if denied.current is not None
                            else None,
                            str(denied.limit)
                            if denied.limit is not None
                            else None,
                            timestamp,
                        ),
                    )
            if existing_id is None and denied is None:
                connection.execute(
                    """
                    INSERT INTO sandboxes(
                        id, organization_id, backend, status, config_json,
                        created_at, idempotency_key, request_fingerprint,
                        requested_at, provisioning_at, hourly_rate_usd,
                        currency, pricing_source, creator_api_key_id, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.id,
                        record.organization_id,
                        record.backend,
                        record.status,
                        json.dumps(record.config, sort_keys=True),
                        record.created_at,
                        idempotency_key,
                        fingerprint,
                        timestamp,
                        timestamp,
                        str(hourly_rate_usd),
                        currency,
                        pricing_source,
                        creator_api_key_id,
                        expires_at,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO sandbox_creation_attempts(
                        id, sandbox_id, attempt_number, status,
                        retry_status, started_at
                    ) VALUES (?, ?, 1, 'creating', 'not_applicable', ?)
                    """,
                    (_id("att"), record.id, record.created_at),
                )
        if existing_id is not None:
            return self.get_sandbox(organization_id, existing_id), False
        if denied is not None:
            raise denied
        return self.get_sandbox(organization_id, record.id), True

    def complete_sandbox_creation(
        self,
        sandbox_id: str,
        *,
        status: str,
        error: dict | None = None,
        retry_status: str = "not_applicable",
        cleanup_status: str | None = None,
        provider_resource_id: str | None = None,
        timestamp: str | None = None,
        hourly_rate_usd: Decimal | None = None,
    ) -> None:
        """Finish the active creation attempt and update its summary."""
        completed_at = timestamp or _now()
        error_json = json.dumps(error, sort_keys=True) if error else None
        attempt_status = "succeeded" if status == "running" else status
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE sandbox_creation_attempts
                SET status = ?, error_json = ?, retry_status = ?,
                    cleanup_status = ?, provider_resource_id = ?,
                    completed_at = ?
                WHERE sandbox_id = ? AND attempt_number = 1
                """,
                (
                    attempt_status,
                    error_json,
                    retry_status,
                    cleanup_status,
                    provider_resource_id,
                    completed_at,
                    sandbox_id,
                ),
            )
            connection.execute(
                """
                UPDATE sandboxes
                SET status = ?, latest_error_json = ?, retry_status = ?,
                    cleanup_status = ?, provider_resource_id = ?,
                    error = NULL,
                    hourly_rate_usd = COALESCE(?, hourly_rate_usd),
                    running_at = CASE WHEN ? = 'running' THEN ? ELSE running_at END,
                    failed_at = CASE WHEN ? = 'failed' THEN ? ELSE failed_at END,
                    version = version + 1
                WHERE id = ?
                """,
                (
                    status,
                    error_json,
                    retry_status,
                    cleanup_status,
                    provider_resource_id,
                    str(hourly_rate_usd)
                    if hourly_rate_usd is not None
                    else None,
                    status,
                    completed_at,
                    status,
                    completed_at,
                    sandbox_id,
                ),
            )

    def mark_lifecycle(
        self,
        sandbox_id: str,
        status: str,
        timestamp: str,
        *,
        actor_api_key_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        column = {
            "stopping": "stopping_at",
            "destroyed": "terminated_at",
            "failed": "failed_at",
        }.get(status)
        if column is None:
            raise ValueError("unsupported lifecycle status")
        with self._connect() as connection:
            connection.execute(
                f"""UPDATE sandboxes SET status = ?, {column} = ?,
                   terminated_by_api_key_id=CASE WHEN ? = 'destroyed'
                       THEN ? ELSE terminated_by_api_key_id END,
                   termination_reason=CASE WHEN ? = 'destroyed'
                       THEN ? ELSE termination_reason END,
                   supervision_owner=NULL, supervision_claimed_at=NULL,
                   version = version + 1 WHERE id = ?""",
                (
                    status,
                    timestamp,
                    status,
                    actor_api_key_id,
                    status,
                    reason,
                    sandbox_id,
                ),
            )

    def begin_termination(
        self,
        organization_id: str,
        sandbox_id: str,
        *,
        timestamp: str,
        expected_version: int | None = None,
    ) -> SandboxRecord:
        """Atomically move a sandbox to stopping with optional revision check."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, version FROM sandboxes WHERE id=? AND organization_id=?",
                (sandbox_id, organization_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("sandbox not found")
            if row["status"] == "destroyed":
                return self.get_sandbox(organization_id, sandbox_id)
            if (
                expected_version is not None
                and row["version"] != expected_version
            ):
                raise ConflictError(
                    "sandbox changed; refresh before terminating"
                )
            if row["status"] == "stopping":
                raise ConflictError(
                    "sandbox termination is already in progress"
                )
            connection.execute(
                """UPDATE sandboxes SET status='stopping', stopping_at=?,
                   version=version+1 WHERE id=? AND organization_id=?""",
                (timestamp, sandbox_id, organization_id),
            )
        return self.get_sandbox(organization_id, sandbox_id)

    def claim_expired_sandboxes(
        self,
        *,
        timestamp: str,
        owner: str,
        stale_before: str,
    ) -> list[SandboxRecord]:
        """Claim expired resources once, including after supervisor restart."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT id FROM sandboxes
                   WHERE expires_at IS NOT NULL AND expires_at <= ?
                   AND ((status IN ('creating', 'running')
                         AND (supervision_claimed_at IS NULL
                              OR supervision_claimed_at < ?))
                        OR (status='stopping'
                            AND COALESCE(supervision_claimed_at,
                                         stopping_at, expires_at) < ?))
                   ORDER BY expires_at""",
                (timestamp, stale_before, stale_before),
            ).fetchall()
            ids = [row["id"] for row in rows]
            for sandbox_id in ids:
                connection.execute(
                    """UPDATE sandboxes SET status='stopping',
                       stopping_at=COALESCE(stopping_at, ?),
                       supervision_owner=?, supervision_claimed_at=?,
                       version=version+1 WHERE id=?""",
                    (timestamp, owner, timestamp, sandbox_id),
                )
        return [self.get_sandbox_internal(sandbox_id) for sandbox_id in ids]

    def list_organization_ids(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM organizations ORDER BY id"
            ).fetchall()
        return [row["id"] for row in rows]

    def known_provider_resource_ids(
        self, organization_id: str, backend: str
    ) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT provider_resource_id FROM sandboxes
                   WHERE organization_id=? AND backend=?
                   AND provider_resource_id IS NOT NULL""",
                (organization_id, backend),
            ).fetchall()
        return {row["provider_resource_id"] for row in rows}

    def claim_orphan_cleanup(
        self,
        *,
        organization_id: str,
        backend: str,
        provider_resource_id: str,
        observed_at: str,
    ) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """SELECT status FROM orphan_cleanups
                   WHERE backend=? AND provider_resource_id=?""",
                (backend, provider_resource_id),
            ).fetchone()
            if existing is not None:
                if existing["status"] != "failed":
                    return False
                connection.execute(
                    """UPDATE orphan_cleanups SET status='claimed',
                       organization_id=?, observed_at=?, completed_at=NULL
                       WHERE backend=? AND provider_resource_id=?""",
                    (
                        organization_id,
                        observed_at,
                        backend,
                        provider_resource_id,
                    ),
                )
                return True
            connection.execute(
                """INSERT INTO orphan_cleanups(
                   backend, provider_resource_id, organization_id,
                   status, observed_at) VALUES (?, ?, ?, 'claimed', ?)""",
                (
                    backend,
                    provider_resource_id,
                    organization_id,
                    observed_at,
                ),
            )
        return True

    def complete_orphan_cleanup(
        self,
        backend: str,
        provider_resource_id: str,
        *,
        status: str,
        completed_at: str,
    ) -> None:
        if status not in {"deleted", "failed"}:
            raise ValueError("invalid orphan cleanup status")
        with self._connect() as connection:
            connection.execute(
                """UPDATE orphan_cleanups SET status=?, completed_at=?
                   WHERE backend=? AND provider_resource_id=?""",
                (status, completed_at, backend, provider_resource_id),
            )

    def record_provider_health(
        self,
        backend: str,
        *,
        status: str,
        checked_at: str,
        message: str,
        actor_api_key_id: str | None,
    ) -> None:
        if status not in {
            "healthy",
            "degraded",
            "unchecked",
            "unconfigured",
        }:
            raise ValueError("invalid provider health status")
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO provider_health(
                   backend, status, checked_at, message,
                   checked_by_api_key_id) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(backend) DO UPDATE SET
                   status=excluded.status, checked_at=excluded.checked_at,
                   message=excluded.message,
                   checked_by_api_key_id=excluded.checked_by_api_key_id""",
                (
                    backend,
                    status,
                    checked_at,
                    message,
                    actor_api_key_id,
                ),
            )

    def list_provider_health(self) -> dict[str, dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT backend, status, checked_at, message
                   FROM provider_health ORDER BY backend"""
            ).fetchall()
        return {row["backend"]: dict(row) for row in rows}

    def fail_termination(
        self, sandbox_id: str, *, timestamp: str, error: dict
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE sandboxes SET status='failed', failed_at=?,
                   latest_error_json=?, retry_status='not_retryable',
                   version=version+1 WHERE id=?""",
                (timestamp, json.dumps(error, sort_keys=True), sandbox_id),
            )

    def record_lifecycle_cost(
        self,
        *,
        organization_id: str,
        sandbox_id: str,
        observation_id: str,
        billable_seconds: Decimal,
        estimated_cost_usd: Decimal,
        provider_reported_cost_usd: Decimal | None,
        customer_markup: Decimal,
        currency: str,
        pricing_source: str,
        timestamp: str,
    ) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT billable_seconds, estimated_cost_usd,
                       provider_reported_cost_usd, effective_cost_usd,
                       customer_cost_usd, currency, pricing_source, cost_state
                FROM lifecycle_costs WHERE sandbox_id = ?
                """,
                (sandbox_id,),
            ).fetchone()
            old_seconds = Decimal(existing[0]) if existing else Decimal(0)
            old_effective = Decimal(existing[3]) if existing else Decimal(0)
            old_customer = Decimal(existing[4]) if existing else Decimal(0)
            billable_seconds = max(billable_seconds, old_seconds)
            if (
                provider_reported_cost_usd is None
                and existing
                and existing[7] in {"provider_reported", "reconciled"}
            ):
                provider_reported_cost_usd = Decimal(existing[2])
                effective = old_effective
                customer = old_customer
                currency = existing[5]
                pricing_source = existing[6]
                state = existing[7]
            else:
                state = (
                    "reconciled"
                    if provider_reported_cost_usd is not None
                    else "estimated"
                )
                effective = (
                    provider_reported_cost_usd
                    if provider_reported_cost_usd is not None
                    else estimated_cost_usd
                )
                customer = effective * customer_markup
            try:
                connection.execute(
                    "INSERT INTO lifecycle_observations VALUES (?, ?)",
                    (sandbox_id, observation_id),
                )
            except sqlite3.IntegrityError:
                return False

            seconds_delta = billable_seconds - old_seconds
            provider_delta = effective - old_effective
            customer_delta = customer - old_customer
            entries: list[tuple] = []
            allocated_provider = Decimal(0)
            allocated_customer = Decimal(0)
            if seconds_delta > 0:
                interval_end = datetime.fromisoformat(timestamp)
                interval_start = interval_end - timedelta(
                    microseconds=int(seconds_delta * Decimal(1_000_000))
                )
                cursor = interval_start
                index = 0
                while cursor < interval_end:
                    midnight = cursor.replace(
                        hour=0, minute=0, second=0, microsecond=0
                    ) + timedelta(days=1)
                    segment_end = min(midnight, interval_end)
                    segment_seconds = Decimal(
                        str((segment_end - cursor).total_seconds())
                    )
                    if state == "estimated":
                        segment_provider = (
                            provider_delta * segment_seconds / seconds_delta
                        )
                        segment_customer = (
                            customer_delta * segment_seconds / seconds_delta
                        )
                    else:
                        segment_provider = Decimal(0)
                        segment_customer = Decimal(0)
                    allocated_provider += segment_provider
                    allocated_customer += segment_customer
                    entries.append(
                        (
                            _id("lcl"),
                            organization_id,
                            sandbox_id,
                            f"{observation_id}:time:{index}",
                            str(segment_provider),
                            str(segment_customer),
                            state,
                            cursor.isoformat(),
                            str(segment_seconds),
                        )
                    )
                    cursor = segment_end
                    index += 1
            remaining_provider = provider_delta - allocated_provider
            remaining_customer = customer_delta - allocated_customer
            if (
                remaining_provider != 0
                or remaining_customer != 0
                or not entries
            ):
                entries.append(
                    (
                        _id("lcl"),
                        organization_id,
                        sandbox_id,
                        f"{observation_id}:adjustment",
                        str(remaining_provider),
                        str(remaining_customer),
                        state,
                        timestamp,
                        "0",
                    )
                )
            connection.executemany(
                """
                    INSERT INTO lifecycle_ledger_entries(
                        id, organization_id, sandbox_id, observation_id,
                        provider_delta_usd, customer_delta_usd, cost_state,
                        created_at, billable_seconds_delta
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                entries,
            )
            connection.execute(
                """
                INSERT INTO lifecycle_costs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sandbox_id) DO UPDATE SET
                    billable_seconds=excluded.billable_seconds,
                    estimated_cost_usd=excluded.estimated_cost_usd,
                    provider_reported_cost_usd=excluded.provider_reported_cost_usd,
                    effective_cost_usd=excluded.effective_cost_usd,
                    customer_cost_usd=excluded.customer_cost_usd,
                    currency=excluded.currency, pricing_source=excluded.pricing_source,
                    cost_state=excluded.cost_state, updated_at=excluded.updated_at
                """,
                (
                    sandbox_id,
                    organization_id,
                    str(billable_seconds),
                    str(estimated_cost_usd),
                    str(provider_reported_cost_usd)
                    if provider_reported_cost_usd is not None
                    else None,
                    str(effective),
                    str(customer),
                    currency,
                    pricing_source,
                    state,
                    timestamp,
                ),
            )
            connection.execute(
                """UPDATE sandboxes SET cost_state = ?, currency = ?,
                   pricing_source = ? WHERE id = ?""",
                (state, currency, pricing_source, sandbox_id),
            )

    def record_provider_observation(
        self,
        organization_id: str,
        sandbox_id: str,
        *,
        observation_id: str,
        provider_resource_id: str | None,
        status: str,
        observed_at: str,
        provider_cost_usd: Decimal | None = None,
        currency: str | None = None,
        missing: bool = False,
    ) -> bool:
        try:
            with self._connect() as connection:
                connection.execute(
                    """INSERT INTO provider_observations(
                       id, organization_id, sandbox_id, observation_id,
                       provider_resource_id, status, observed_at,
                       provider_cost_usd, currency, missing
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        _id("obs"),
                        organization_id,
                        sandbox_id,
                        observation_id,
                        provider_resource_id,
                        status,
                        observed_at,
                        str(provider_cost_usd)
                        if provider_cost_usd is not None
                        else None,
                        currency,
                        int(missing),
                    ),
                )
            return True
        except sqlite3.IntegrityError:
            return False
        return True

    def apply_provider_observation(
        self,
        organization_id: str,
        sandbox_id: str,
        *,
        status: str,
        observed_at: str,
        missing: bool = False,
    ) -> None:
        lifecycle_status = status.lower().strip()
        normalized = {
            "running": "running",
            "provisioning": "creating",
            "stopping": "stopping",
            "terminated": "destroyed",
            "failed": "failed",
        }.get(lifecycle_status)
        if normalized is None:
            raise ValueError(f"unsupported provider lifecycle status: {status}")
        if missing:
            normalized = "failed"
        with self._connect() as connection:
            current = connection.execute(
                "SELECT status FROM sandboxes WHERE id=? AND organization_id=?",
                (sandbox_id, organization_id),
            ).fetchone()
            if current is None:
                raise NotFoundError("sandbox not found")
            if current[0] in {"destroyed", "failed"}:
                connection.execute(
                    """UPDATE sandboxes SET last_provider_observed_at=?,
                       provider_missing=0, version=version+1
                       WHERE id=? AND organization_id=?""",
                    (observed_at, sandbox_id, organization_id),
                )
                return
            cursor = connection.execute(
                """UPDATE sandboxes SET last_provider_observed_at=?,
                   provider_missing=?, status=?,
                   provisioning_at=CASE WHEN ? = 'creating' THEN COALESCE(provisioning_at, ?) ELSE provisioning_at END,
                   running_at=CASE WHEN ? = 'running' THEN COALESCE(running_at, ?) ELSE running_at END,
                   stopping_at=CASE WHEN ? = 'stopping' THEN COALESCE(stopping_at, ?) ELSE stopping_at END,
                   failed_at=CASE WHEN ? = 'failed' THEN ? ELSE failed_at END,
                   terminated_at=CASE WHEN ? = 'destroyed' THEN ? ELSE terminated_at END,
                   version=version+1
                   WHERE id=? AND organization_id=?""",
                (
                    observed_at,
                    int(missing),
                    normalized,
                    normalized,
                    observed_at,
                    normalized,
                    observed_at,
                    normalized,
                    observed_at,
                    normalized,
                    observed_at,
                    normalized,
                    observed_at,
                    sandbox_id,
                    organization_id,
                ),
            )
            if cursor.rowcount != 1:
                raise NotFoundError("sandbox not found")

    def update_sandbox_status(
        self, sandbox_id: str, status: str, *, error: str | None = None
    ) -> None:
        destroyed_at = _now() if status == "destroyed" else None
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE sandboxes
                SET status = ?, error = ?, destroyed_at = COALESCE(?, destroyed_at),
                    version = version + 1
                WHERE id = ?
                """,
                (status, error, destroyed_at, sandbox_id),
            )

    def get_sandbox(
        self, organization_id: str, sandbox_id: str
    ) -> SandboxRecord:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT s.*,
                       (SELECT COUNT(*) FROM sandbox_creation_attempts a
                        WHERE a.sandbox_id = s.id) AS attempt_count,
                       (SELECT name FROM api_keys k
                        WHERE k.id = s.creator_api_key_id) AS creator_api_key_name,
                       (SELECT name FROM api_keys k
                        WHERE k.id = s.terminated_by_api_key_id) AS terminated_by_api_key_name
                FROM sandboxes s
                WHERE s.id = ? AND s.organization_id = ?
                """,
                (sandbox_id, organization_id),
            ).fetchone()
        if row is None:
            raise NotFoundError("sandbox not found")
        return self._sandbox_from_row(row)

    def list_sandboxes(self, organization_id: str) -> list[SandboxRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT s.*,
                       (SELECT COUNT(*) FROM sandbox_creation_attempts a
                        WHERE a.sandbox_id = s.id) AS attempt_count,
                       (SELECT name FROM api_keys k
                        WHERE k.id = s.creator_api_key_id) AS creator_api_key_name,
                       (SELECT name FROM api_keys k
                        WHERE k.id = s.terminated_by_api_key_id) AS terminated_by_api_key_name
                FROM sandboxes s WHERE s.organization_id = ?
                ORDER BY s.created_at DESC
                """,
                (organization_id,),
            ).fetchall()
        return [self._sandbox_from_row(row) for row in rows]

    def get_sandbox_internal(self, sandbox_id: str) -> SandboxRecord:
        """Load a sandbox for trusted lifecycle workers without tenant input."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT s.*,
                       (SELECT COUNT(*) FROM sandbox_creation_attempts a
                        WHERE a.sandbox_id = s.id) AS attempt_count,
                       (SELECT name FROM api_keys k
                        WHERE k.id = s.creator_api_key_id) AS creator_api_key_name,
                       (SELECT name FROM api_keys k
                        WHERE k.id = s.terminated_by_api_key_id) AS terminated_by_api_key_name
                FROM sandboxes s WHERE s.id = ?
                """,
                (sandbox_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("sandbox not found")
        return self._sandbox_from_row(row)

    def sandbox_detail(self, organization_id: str, sandbox_id: str) -> dict:
        """Return tenant-scoped operational history for a sandbox."""
        sandbox = self.get_sandbox(organization_id, sandbox_id)
        with self._connect() as connection:
            attempts = connection.execute(
                """SELECT attempt_number, status, error_json, retry_status,
                   cleanup_status, provider_resource_id, started_at, completed_at
                   FROM sandbox_creation_attempts WHERE sandbox_id=?
                   ORDER BY attempt_number""",
                (sandbox_id,),
            ).fetchall()
            executions = connection.execute(
                """SELECT id, request_id, status, response_json, error,
                   created_at, completed_at FROM executions
                   WHERE sandbox_id=? AND organization_id=? ORDER BY created_at DESC""",
                (sandbox_id, organization_id),
            ).fetchall()
            cost = connection.execute(
                """SELECT billable_seconds, estimated_cost_usd,
                   provider_reported_cost_usd, effective_cost_usd,
                   customer_cost_usd, currency, pricing_source,
                   cost_state, updated_at FROM lifecycle_costs
                   WHERE sandbox_id=? AND organization_id=?""",
                (sandbox_id, organization_id),
            ).fetchone()
            observations = connection.execute(
                """SELECT observation_id, provider_resource_id, status,
                   observed_at, provider_cost_usd, currency, missing
                   FROM provider_observations
                   WHERE sandbox_id=? AND organization_id=? ORDER BY observed_at DESC""",
                (sandbox_id, organization_id),
            ).fetchall()

        def decoded(value: str | None) -> dict | None:
            if not value:
                return None
            try:
                result = json.loads(value)
            except json.JSONDecodeError:
                return {"message": "Sandbox operation failed."}
            return result if isinstance(result, dict) else None

        return {
            "sandbox": sandbox,
            "attempts": [
                {
                    **{
                        key: row[key]
                        for key in row.keys()
                        if key != "error_json"
                    },
                    "error": decoded(row["error_json"]),
                }
                for row in attempts
            ],
            "executions": [
                {
                    **{
                        key: row[key]
                        for key in row.keys()
                        if key not in {"response_json", "error"}
                    },
                    "response": decoded(row["response_json"]),
                    "error": decoded(row["error"]),
                }
                for row in executions
            ],
            "cost": dict(cost) if cost else None,
            "provider_observations": [
                {**dict(row), "missing": bool(row["missing"])}
                for row in observations
            ],
        }

    def begin_execution(
        self, organization_id: str, sandbox_id: str, request_id: str
    ) -> tuple[str, dict | None]:
        execution_id = _id("exe")
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO executions(
                        id, organization_id, sandbox_id, request_id, status,
                        created_at
                    ) VALUES (?, ?, ?, ?, 'running', ?)
                    """,
                    (
                        execution_id,
                        organization_id,
                        sandbox_id,
                        request_id,
                        _now(),
                    ),
                )
            return execution_id, None
        except sqlite3.IntegrityError:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT id, status, response_json, error FROM executions
                    WHERE organization_id = ? AND sandbox_id = ?
                          AND request_id = ?
                    """,
                    (organization_id, sandbox_id, request_id),
                ).fetchone()
            if row is None:
                raise
            if row["status"] == "completed":
                return row["id"], json.loads(row["response_json"])
            detail = row["error"] or "execution is already in progress"
            raise ConflictError(detail) from None

    def finish_execution(self, execution_id: str, response: dict) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE executions
                SET status = 'completed', response_json = ?, completed_at = ?
                WHERE id = ?
                """,
                (json.dumps(response), _now(), execution_id),
            )

    def fail_execution(self, execution_id: str, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE executions
                SET status = 'failed', error = ?, completed_at = ? WHERE id = ?
                """,
                (error, _now(), execution_id),
            )

    def record_runtime_usage(
        self,
        *,
        organization_id: str,
        sandbox_id: str,
        execution_id: str,
        request_id: str,
        runtime_seconds: Decimal,
        provider_cost_usd: Decimal,
        customer_cost_usd: Decimal,
        price_version: str,
    ) -> bool:
        event_id = _id("use")
        created_at = _now()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO usage_events(
                        id, organization_id, sandbox_id, execution_id,
                        request_id, metric, quantity, unit,
                        provider_cost_usd, customer_cost_usd, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'runtime_seconds', ?, 'second',
                              ?, ?, ?)
                    """,
                    (
                        event_id,
                        organization_id,
                        sandbox_id,
                        execution_id,
                        request_id,
                        str(runtime_seconds),
                        str(provider_cost_usd),
                        str(customer_cost_usd),
                        created_at,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO ledger_entries(
                        id, organization_id, sandbox_id, usage_event_id,
                        kind, amount_usd, price_version, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            _id("led"),
                            organization_id,
                            sandbox_id,
                            event_id,
                            "provider_cost",
                            str(provider_cost_usd),
                            price_version,
                            created_at,
                        ),
                        (
                            _id("led"),
                            organization_id,
                            sandbox_id,
                            event_id,
                            "customer_cost",
                            str(customer_cost_usd),
                            price_version,
                            created_at,
                        ),
                    ],
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def summarize_costs(
        self,
        organization_id: str,
        *,
        group_by: str = "sandbox",
        start: str | None = None,
        end: str | None = None,
    ) -> list[CostSummary]:
        if group_by not in {"sandbox", "backend", "day"}:
            raise ValueError("group_by must be sandbox, backend, or day")
        clauses = ["u.organization_id = ?"]
        params: list[str] = [organization_id]
        if start:
            clauses.append("u.created_at >= ?")
            params.append(start)
        if end:
            clauses.append("u.created_at < ?")
            params.append(end)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT u.sandbox_id, s.backend, u.created_at, u.quantity,
                       u.provider_cost_usd, u.customer_cost_usd
                FROM usage_events u
                JOIN sandboxes s ON s.id = u.sandbox_id
                WHERE {' AND '.join(clauses)}
                  AND NOT EXISTS (SELECT 1 FROM lifecycle_costs lc
                                  WHERE lc.sandbox_id = u.sandbox_id)
                ORDER BY u.created_at
                """,
                params,
            ).fetchall()
            lifecycle_rows = connection.execute(
                """
                SELECT l.sandbox_id, s.backend, l.created_at,
                       l.billable_seconds_delta AS quantity,
                       l.provider_delta_usd AS provider_cost_usd,
                       l.customer_delta_usd AS customer_cost_usd
                FROM lifecycle_ledger_entries l
                JOIN sandboxes s ON s.id = l.sandbox_id
                WHERE l.organization_id = ?
                  AND (? IS NULL OR l.created_at >= ?)
                  AND (? IS NULL OR l.created_at < ?)
                """,
                (organization_id, start, start, end, end),
            ).fetchall()

        totals: dict[str, list[Decimal]] = {}
        for row in [*rows, *lifecycle_rows]:
            if group_by == "sandbox":
                key = row["sandbox_id"]
            elif group_by == "backend":
                key = row["backend"]
            else:
                key = row["created_at"][:10]
            values = totals.setdefault(
                key, [Decimal(0), Decimal(0), Decimal(0)]
            )
            values[0] += Decimal(row["quantity"])
            values[1] += Decimal(row["provider_cost_usd"])
            values[2] += Decimal(row["customer_cost_usd"])
        return [
            CostSummary(
                key=key,
                runtime_seconds=values[0],
                provider_cost_usd=values[1],
                customer_cost_usd=values[2],
            )
            for key, values in sorted(totals.items())
        ]

    def export_rows(
        self,
        organization_id: str,
        *,
        kind: str,
        offset: int,
        limit: int,
    ) -> tuple[list[str], list[dict], int | None]:
        """Read one tenant-scoped page for a stable CSV export contract."""
        queries = {
            "usage": (
                [
                    "id",
                    "sandbox_id",
                    "request_id",
                    "metric",
                    "quantity",
                    "unit",
                    "provider_cost_usd",
                    "customer_cost_usd",
                    "created_at",
                ],
                """SELECT id, sandbox_id, request_id, metric, quantity,
                   unit, provider_cost_usd, customer_cost_usd, created_at
                   FROM usage_events WHERE organization_id=?
                   ORDER BY created_at, id LIMIT ? OFFSET ?""",
            ),
            "costs": (
                [
                    "sandbox_id",
                    "backend",
                    "billable_seconds",
                    "estimated_cost_usd",
                    "provider_reported_cost_usd",
                    "effective_cost_usd",
                    "customer_cost_usd",
                    "currency",
                    "pricing_source",
                    "cost_state",
                    "updated_at",
                ],
                """SELECT c.sandbox_id, s.backend, c.billable_seconds,
                   c.estimated_cost_usd, c.provider_reported_cost_usd,
                   c.effective_cost_usd, c.customer_cost_usd, c.currency,
                   c.pricing_source, c.cost_state, c.updated_at
                   FROM lifecycle_costs c JOIN sandboxes s ON s.id=c.sandbox_id
                   WHERE c.organization_id=? ORDER BY c.updated_at, c.sandbox_id
                   LIMIT ? OFFSET ?""",
            ),
            "ledger": (
                [
                    "id",
                    "sandbox_id",
                    "kind",
                    "reference_id",
                    "runtime_seconds",
                    "provider_delta_usd",
                    "customer_delta_usd",
                    "cost_state",
                    "created_at",
                ],
                """SELECT id, sandbox_id, kind, reference_id,
                   runtime_seconds, provider_delta_usd, customer_delta_usd,
                   cost_state, created_at FROM (
                     SELECT id, organization_id, sandbox_id, kind,
                       usage_event_id AS reference_id, '0' AS runtime_seconds,
                       amount_usd AS provider_delta_usd,
                       amount_usd AS customer_delta_usd,
                       'execution' AS cost_state, created_at
                     FROM ledger_entries
                     UNION ALL
                     SELECT id, organization_id, sandbox_id, 'lifecycle' AS kind,
                       observation_id AS reference_id,
                       billable_seconds_delta AS runtime_seconds,
                       provider_delta_usd, customer_delta_usd, cost_state,
                       created_at FROM lifecycle_ledger_entries
                   ) WHERE organization_id=? ORDER BY created_at, id
                   LIMIT ? OFFSET ?""",
            ),
        }
        if kind not in queries:
            raise ValueError("unsupported export kind")
        columns, query = queries[kind]
        with self._connect() as connection:
            rows = connection.execute(
                query, (organization_id, limit + 1, offset)
            ).fetchall()
        has_more = len(rows) > limit
        return (
            columns,
            [dict(row) for row in rows[:limit]],
            (offset + limit if has_more else None),
        )

    def _digest(self, secret: str) -> str:
        return hmac.new(
            self._pepper, secret.encode(), hashlib.sha256
        ).hexdigest()

    def _request_fingerprint(self, payload: dict) -> str:
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        ).encode()
        return hmac.new(self._pepper, canonical, hashlib.sha256).hexdigest()

    @staticmethod
    def _sandbox_from_row(row: sqlite3.Row) -> SandboxRecord:
        return SandboxRecord(
            id=row["id"],
            organization_id=row["organization_id"],
            backend=row["backend"],
            status=row["status"],
            config=json.loads(row["config_json"]),
            created_at=row["created_at"],
            destroyed_at=row["destroyed_at"],
            error=row["error"],
            attempt_count=row["attempt_count"],
            latest_error=json.loads(row["latest_error_json"])
            if row["latest_error_json"]
            else None,
            retry_status=row["retry_status"],
            cleanup_status=row["cleanup_status"],
            provider_resource_id=row["provider_resource_id"],
            requested_at=row["requested_at"],
            provisioning_at=row["provisioning_at"],
            running_at=row["running_at"],
            stopping_at=row["stopping_at"],
            terminated_at=row["terminated_at"],
            failed_at=row["failed_at"],
            last_provider_observed_at=row["last_provider_observed_at"],
            hourly_rate_usd=row["hourly_rate_usd"],
            currency=row["currency"],
            pricing_source=row["pricing_source"],
            cost_state=row["cost_state"],
            provider_missing=bool(row["provider_missing"]),
            creator_api_key_id=row["creator_api_key_id"],
            creator_api_key_name=row["creator_api_key_name"],
            version=row["version"],
            expires_at=row["expires_at"],
            terminated_by_api_key_id=row["terminated_by_api_key_id"],
            terminated_by_api_key_name=row["terminated_by_api_key_name"],
            termination_reason=row["termination_reason"],
        )

    @staticmethod
    def _policy_from_row(row: sqlite3.Row) -> OrganizationPolicy:
        return OrganizationPolicy(
            organization_id=row["organization_id"],
            max_concurrent_sandboxes=row["max_concurrent_sandboxes"],
            hourly_spend_limit_usd=row["hourly_spend_limit_usd"],
            daily_spend_limit_usd=row["daily_spend_limit_usd"],
            allowed_backends=(
                tuple(json.loads(row["allowed_backends_json"]))
                if row["allowed_backends_json"] is not None
                else None
            ),
            allowed_gpu_types=(
                tuple(json.loads(row["allowed_gpu_types_json"]))
                if row["allowed_gpu_types_json"] is not None
                else None
            ),
            max_sandbox_lifetime_secs=row["max_sandbox_lifetime_secs"],
            version=row["version"],
            updated_at=row["updated_at"],
            updated_by_api_key_id=row["updated_by_api_key_id"],
        )

    def _policy_violation(
        self,
        connection: sqlite3.Connection,
        organization_id: str,
        *,
        backend: str,
        gpu: object,
        requested_timeout_secs: object,
        timestamp: str,
    ) -> PolicyDeniedError | None:
        row = connection.execute(
            "SELECT * FROM organization_policies WHERE organization_id=?",
            (organization_id,),
        ).fetchone()
        if row is None:
            return None
        policy = self._policy_from_row(row)
        if policy.allowed_backends is not None and backend not in set(
            policy.allowed_backends
        ):
            return PolicyDeniedError(
                f"Backend {backend} is not allowed by organization policy.",
                policy="allowed_backends",
                current=backend,
                limit=",".join(policy.allowed_backends),
            )
        if (
            gpu is not None
            and policy.allowed_gpu_types is not None
            and str(gpu) not in set(policy.allowed_gpu_types)
        ):
            return PolicyDeniedError(
                f"GPU type {gpu} is not allowed by organization policy.",
                policy="allowed_gpu_types",
                current=str(gpu),
                limit=",".join(policy.allowed_gpu_types),
            )
        if policy.max_sandbox_lifetime_secs is not None:
            if requested_timeout_secs is None:
                return PolicyDeniedError(
                    "A sandbox lifetime is required by organization policy.",
                    policy="max_sandbox_lifetime_secs",
                    limit=policy.max_sandbox_lifetime_secs,
                )
            if int(requested_timeout_secs) > policy.max_sandbox_lifetime_secs:
                return PolicyDeniedError(
                    "Requested sandbox lifetime exceeds organization policy.",
                    policy="max_sandbox_lifetime_secs",
                    current=int(requested_timeout_secs),
                    limit=policy.max_sandbox_lifetime_secs,
                )
        if policy.max_concurrent_sandboxes is not None:
            active = connection.execute(
                """SELECT COUNT(*) FROM sandboxes WHERE organization_id=?
                   AND status IN ('creating', 'running', 'stopping')""",
                (organization_id,),
            ).fetchone()[0]
            if active >= policy.max_concurrent_sandboxes:
                return PolicyDeniedError(
                    "Concurrent sandbox limit has been reached.",
                    policy="max_concurrent_sandboxes",
                    current=active,
                    limit=policy.max_concurrent_sandboxes,
                )
        when = datetime.fromisoformat(timestamp).astimezone(UTC)
        spend_windows = (
            (
                "hourly_spend_limit_usd",
                policy.hourly_spend_limit_usd,
                when - timedelta(hours=1),
            ),
            (
                "daily_spend_limit_usd",
                policy.daily_spend_limit_usd,
                when.replace(hour=0, minute=0, second=0, microsecond=0),
            ),
        )
        for name, limit_value, start in spend_windows:
            if limit_value is None:
                continue
            current = self._spend_between(
                connection,
                organization_id,
                start.isoformat(),
                when.isoformat(),
            )
            if current >= Decimal(limit_value):
                return PolicyDeniedError(
                    f"{name.replace('_', ' ').capitalize()} has been reached.",
                    policy=name,
                    current=str(current),
                    limit=limit_value,
                )
        return None

    @staticmethod
    def _spend_between(
        connection: sqlite3.Connection,
        organization_id: str,
        start: str,
        end: str,
    ) -> Decimal:
        lifecycle = connection.execute(
            """SELECT customer_delta_usd FROM lifecycle_ledger_entries
               WHERE organization_id=? AND created_at>=? AND created_at<=?""",
            (organization_id, start, end),
        ).fetchall()
        legacy = connection.execute(
            """SELECT u.customer_cost_usd FROM usage_events u
               WHERE u.organization_id=? AND u.created_at>=? AND u.created_at<=?
               AND NOT EXISTS (SELECT 1 FROM lifecycle_costs lc
                               WHERE lc.sandbox_id=u.sandbox_id)""",
            (organization_id, start, end),
        ).fetchall()
        return sum(
            (Decimal(row[0]) for row in [*lifecycle, *legacy]), Decimal(0)
        )
