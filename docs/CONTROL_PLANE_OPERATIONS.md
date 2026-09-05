# Control-plane operations runbook

This runbook covers the SQLite-backed hosted control plane. It is designed for
one API process with its built-in supervisor. Provider calls, reconciliations,
and automated cleanup must be tested with non-billable fakes before production.

## Deployment baseline

Terminate TLS at a trusted reverse proxy and forward only to a loopback or
private listener. Preserve the original HTTPS scheme, restrict request-body
size, and do not cache `/dashboard` or `/v1/*`. Run one control-plane process
for each SQLite database; multiple API replicas need a shared runtime registry
and a transactional shared store rather than this SQLite implementation.

Required server-owned values are:

| Variable | Purpose |
|---|---|
| `BESPOKE_API_KEY_PEPPER` | Stable HMAC pepper for API keys and sessions |
| `BESPOKE_CONTROL_PLANE_ADMIN_TOKEN` | Organization bootstrap only |
| `BESPOKE_CONTROL_PLANE_DB` | Absolute persistent database path |
| `BESPOKE_ALLOWED_BACKENDS` | Comma-separated provider allowlist |

Optional operational values are:

| Variable | Default | Purpose |
|---|---:|---|
| `BESPOKE_CUSTOMER_MARKUP` | `1` | Customer/provider cost multiplier |
| `BESPOKE_SUPERVISION_INTERVAL_SECS` | `30` | TTL, cleanup, orphan, and alert scan interval; `0` disables the loop |
| `BESPOKE_SESSION_COOKIE_SECURE` | `1` | Keep `1` behind HTTPS |
| `BESPOKE_DASHBOARD_SESSION_TTL_SECS` | `28800` | Browser session lifetime |
| `BESPOKE_ENABLE_LOCAL_DASHBOARD_LOGIN` | `0` | Enables `/dashboard/local`; development only |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Private application listener |

Keep the pepper, admin token, product keys, session values, and provider
credentials out of source control, process arguments, logs, and support
transcripts. Rotate a disclosed product key immediately. Changing the pepper
invalidates all existing keys and sessions.

## Provider setup

Install only the extras for enabled providers. Configure provider credentials
on the server (`DAYTONA_API_KEY`, `E2B_API_KEY`, `MODAL_TOKEN_ID` plus
`MODAL_TOKEN_SECRET`, `RUNPOD_API_KEY`, or `TENSORLAKE_API_KEY`). Credential
values remain in process memory and are neither persisted nor returned by the
API. A configured provider is `unchecked` until a deployment-supplied checker
runs; it is `healthy` only after a successful check.

Before enabling a provider, verify create, execute, terminate, not-found
cleanup, ambiguous-create cleanup, list/reconcile, reported cost, and credential
failure behavior in a staging tenant. Limit the allowlist until those checks
pass.

### Stock CLI adapter boundary

`bespokelabs-sandbox-api` reads provider settings but does not register real
provider health checkers, reconcilers, or out-of-process terminators. With the
stock CLI, configured providers therefore remain `unchecked`; reconciliation
cannot run; and after an API restart the supervisor cannot terminate a real
cloud resource whose Python runtime object was lost. The supervisor will record
failed cleanup when no terminator is available, but that is detection—not
provider cleanup.

Production deployments that require verified health, provider-reported cost,
or restart/orphan cleanup must supply deployment code that constructs
`ControlPlane` with provider-specific `provider_health_checks`,
`provider_reconcilers`, and `provider_terminators`. The repository currently
ships tested interfaces and deterministic fakes, not stock real-provider
implementations for those three adapter roles. Do not claim restart recovery or
reconciliation readiness for a provider until its injected adapters pass the
staging checklist above.

## Migrations and releases

Schema migrations are ordered, transactional, and applied automatically when
`SQLiteStore` opens. The current schema version is 6. Before upgrading:

1. Stop the API cleanly and confirm no supervisor process still owns the DB.
2. Checkpoint the WAL and take a consistent backup of the database.
3. Retain the old package and backup until the new process has opened the DB and
   `/healthz`, login, listing, and a fake-provider smoke test pass.
4. Never downgrade an upgraded database in place. Restore the pre-upgrade
   backup if rollback is required.

Release validation must include formatting, lint, the complete offline suite,
fresh and legacy migration tests, wheel build/install, and a production-session
dashboard smoke test. See `PHASE5_VERIFICATION.md` for the recorded evidence.

## Supervisor and reconciler operation

The in-process supervisor scans durable sandbox state at the configured
interval. It reclaims expired resources, retries recoverable stopped/failed
cleanup, removes confirmed provider orphans, and evaluates configured alerts.
Its claims are durable and idempotent across restart. Actual cleanup after a
restart still requires an injected provider terminator, and orphan detection
requires an injected reconciler. Keep exactly one active supervisor per SQLite
database.

Reconciliation is provider-specific and is unavailable in the stock CLI.
`GET /v1/reconciliation` reports tenant
state; an authorized `POST /v1/reconciliation/{backend}` lists provider
resources only when the deployment injected a reconciler, applies status/cost
observations, detects missing resources, and queues orphan cleanup. Repeating
an observation does not duplicate ledger cost.
Schedule reconciliation at a frequency compatible with provider rate limits
and billing freshness. Treat a missing checker or reconciler as `unchecked`,
not healthy.

## Alerts, audit, and retention

Each tenant can configure budget percentage, repeated-failure count/window,
provider-degradation, long-running, and failed-cleanup alerts. Events use stable
deduplication keys and are evaluated by supervision and alert reads. Alert
history and audit history are cursor-paginated and tenant-scoped.

Audit rows record authenticated mutations, exports, login/logout identity,
outcome, and a redacted details object. Rows have no update endpoint and are
append-only until an explicit retention purge. Secret-looking keys and product
key/Bearer values are redacted before persistence.

Retention defaults to 90 days for operational history and 365 days for audit.
Applying retention deletes old alerts, policy denials, provider observations,
and audit rows for that tenant. It deliberately preserves sandbox records,
usage, lifecycle cost, and financial ledgers. Export or archive evidence before
reducing retention, according to legal and billing requirements.

## Backup and restore

Use a volume with encryption, backups, and free-space monitoring. For an offline
backup, stop the API, checkpoint the WAL with `sqlite3 DB 'PRAGMA wal_checkpoint(TRUNCATE);'`,
then copy the database file and record its package version, schema version, and
pepper identifier. Do not copy only the main file while writes are active.

To restore, stop the API, preserve the failed database for investigation,
restore the backup to a new explicit path with restrictive permissions, supply
the same pepper, start one process, and validate schema version, organization
login, tenant isolation, sandbox inventory, and ledger totals before reopening
traffic. Reconcile every enabled provider after restore; resources created
after the backup may appear as orphans and require deliberate cleanup.

## Incident cleanup

For `cleanup_status=unknown` or ambiguous creation, do not retry creation until
provider inventory resolves whether a resource exists. Reconcile first; if a
resource exists, terminate it with the provider identifier and confirm deletion
before marking the incident resolved. For failed TTL cleanup, preserve the
alert/audit trail, verify provider credentials and reachability, then allow the
supervisor to retry or perform a scoped provider-side deletion.

For an orphan alert, verify tenant/account ownership and the absence of a local
sandbox record before deletion. Never bulk-delete using an unbounded provider
query. For suspected credential disclosure, disable ingress, rotate the affected
credential/key, revoke product keys, terminate unrecognized resources, export
audit/ledger evidence, and reconcile all providers.

Health checks: `/healthz` proves process availability only. Operational health
also requires database writes, supervisor progress, current provider checks,
reconciliation freshness, absence of failed-cleanup alerts, and expected audit
events. Escalate growing WAL size, repeated 5xx responses, stale observations,
or unexplained ledger deltas.
