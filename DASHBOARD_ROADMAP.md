# Dashboard production roadmap

This checklist turns the current read-only usage prototype into a trustworthy,
multi-tenant sandbox operations dashboard. Each phase is complete only when its
acceptance checks pass and its tests cover both success and failure paths.

## Manager/implementer protocol

The manager owns sequencing and acceptance. One implementer task works through
the phases in order; it must not start the next phase until the manager reviews
the current phase's code, tests, and checklist evidence.

For every phase:

1. The manager sends the phase prompt below as a new `/goal`.
2. The implementer treats this roadmap and the current worktree as authoritative,
   completes every item and acceptance gate, and checks only items supported by
   direct evidence.
3. The implementer reports files changed, migrations, tests/commands and their
   results, remaining risks, and any manual verification performed.
4. The manager independently inspects the diff, runs the relevant acceptance
   checks, and sends corrections to the same implementer if evidence is missing.
5. Only after manager acceptance does the manager issue the next phase `/goal`.

Phase prompts:

- Phase 1: `/goal Complete Phase 1 of DASHBOARD_ROADMAP.md completely,
  extremely well. Do not start Phase 2. Prove every checked item and both
  acceptance gates with authoritative tests and report the evidence.`
- Phase 2: `/goal Complete Phase 2 of DASHBOARD_ROADMAP.md completely,
  extremely well, building on accepted Phase 1. Do not start Phase 3. Prove
  every checked item and both acceptance gates with authoritative evidence.`
- Phase 3: `/goal Complete Phase 3 of DASHBOARD_ROADMAP.md completely,
  extremely well, building on accepted Phases 1–2. Do not start Phase 4. Test
  the rendered dashboard and prove both acceptance gates.`
- Phase 4: `/goal Complete Phase 4 of DASHBOARD_ROADMAP.md completely,
  extremely well, building on accepted Phases 1–3. Do not start Phase 5. Prove
  policy race safety, restart recovery, tenancy, and secret redaction.`
- Phase 5: `/goal Complete Phase 5 of DASHBOARD_ROADMAP.md completely,
  extremely well, building on accepted Phases 1–4. Perform the final
  requirement-by-requirement audit and do not finish until every roadmap item
  and acceptance gate has authoritative evidence.`

If a phase changes a public API or database schema, compatibility and migration
coverage are part of that phase's acceptance even when not repeated in every
individual checklist item. Provider credentials and live billable resources
must never be used in automated tests; use deterministic provider fakes.

## Phase 1 — Lifecycle and error contract

- [x] Add stable, structured provider errors to the hosted API and remote client:
  `code`, `backend`, `op`, `retryable`, `outcome`, and safe context.
- [x] Classify Daytona creation, connection, timeout, authentication, and
  execution failures without parsing errors in application code.
- [x] Make Daytona orphan cleanup report `not_found`, `deleted`, or `unknown`;
  never claim a failed create is safe to retry when cleanup is unknown.
- [x] Add idempotency to `POST /v1/sandboxes` so repeating the same key cannot
  create a second provider resource.
- [x] Persist creation attempts and expose attempt count, latest error, retry
  status, cleanup status, and provider resource ID in sandbox responses.
- [x] Preserve compatibility for existing local-library users and existing
  SQLite databases through explicit migrations.
- [x] Add focused unit/API/remote-client tests for every new contract field,
  duplicate creation requests, ambiguous creates, and redaction of secrets.

Acceptance gate:

- [x] A simulated Daytona response timeout produces a structured API response,
  records the failed attempt, and never leaks a sandbox in the fake provider.
- [x] Repeating a create request with the same idempotency key returns the same
  logical sandbox and performs exactly one provider create.

## Phase 2 — Complete lifecycle metering and provider reconciliation

- [x] Track lifecycle timestamps: requested, provisioning, running, stopping,
  terminated, failed, and last provider observation.
- [x] Accrue estimated provider cost for the entire billable lifecycle, not only
  while an execute request is running.
- [x] Store rate snapshots, currency, pricing source, and cost state
  (`estimated`, `provider_reported`, or `reconciled`).
- [x] Add a provider reconciliation abstraction plus implementations/fakes that
  list resources, update state/cost, and detect missing or orphaned resources.
- [x] Ensure reconciliation is tenant-safe and idempotent.
- [x] Record failed provisioning and cleanup costs in the ledger.
- [x] Expose lifecycle and reconciliation summaries through authenticated APIs.
- [x] Test retries, partial provider outages, duplicate reconciliation runs,
  clock boundaries, decimal precision, and provider-resource disappearance.

Acceptance gate:

- [x] A failed provisioning attempt with simulated billable time appears in the
  dashboard cost summary with a non-zero estimated/provider cost.
- [x] Reconciliation can replace an estimate with provider-reported cost without
  double-counting the ledger.

## Phase 3 — Operational dashboard

- [x] Add overview cards for active resources, failed launches, unreconciled
  spend, and resources nearing TTL.
- [x] Upgrade the sandbox table with creator/API-key attribution, provider
  resource ID, instance/GPU type, hourly rate, age, TTL, cleanup status, and
  reconciliation state.
- [x] Add status/provider/date filters, pagination, empty/loading/error states,
  and periodic refresh that pauses when the tab is hidden.
- [x] Add a sandbox detail view with lifecycle timeline, attempts, executions,
  errors, cost breakdown, and provider observations.
- [x] Add an authorized terminate action with confirmation, optimistic locking,
  visible progress, and idempotent behavior.
- [x] Make all new controls keyboard accessible and responsive.
- [x] Add browser/API tests covering rendering, filters, detail view,
  termination success/failure, and permission denial.

Acceptance gate:

- [x] An operator can identify and terminate a live fake-provider sandbox from
  the dashboard and see its final lifecycle and cost without refreshing.
- [x] A read-only key cannot see or invoke destructive controls.

## Phase 4 — Multi-tenant guardrails and provider health

- [x] Add organization policies for maximum concurrent sandboxes, hourly/daily
  spend, allowed backends, allowed GPU types, and maximum sandbox lifetime.
- [x] Enforce policies atomically before provider creation and surface a clear,
  non-retryable denial without calling the provider.
- [x] Add server-owned provider configuration/health checks without exposing
  provider credentials or secret values to clients or the database.
- [x] Add API-key/user attribution and scoped operator roles for viewing usage,
  viewing sandboxes, terminating sandboxes, managing policies, and managing
  provider configuration.
- [x] Add automatic TTL cleanup and an orphan watchdog that continues even when
  clients disconnect or the API process restarts.
- [x] Display budgets, quota utilization, provider health, and policy denials in
  the dashboard.
- [x] Test cross-tenant isolation, concurrent limit races, restart recovery,
  watchdog idempotency, and credential redaction.

Acceptance gate:

- [x] Concurrent requests cannot exceed an organization limit even under race.
- [x] A restarted control plane resumes lifecycle supervision and terminates an
  expired fake-provider resource without exposing provider credentials.

## Phase 5 — Alerts, exports, security, and release readiness

- [x] Add configurable alerts for budget thresholds, repeated failures,
  provider degradation, long-running resources, and failed cleanup.
- [x] Add tenant-scoped CSV export for usage, costs, and ledger entries.
- [x] Replace browser API-key storage in the production dashboard path with an
  HTTP-only, secure, same-site authenticated session; retain an explicit local
  development login path where appropriate.
- [x] Add CSRF protection for mutations, strict content security policy,
  security headers, audit logging, and log/error secret redaction.
- [x] Add retention controls and pagination for large datasets.
- [x] Document deployment, migrations, worker/reconciler operation, provider
  setup, backup/restore, incident cleanup, and the user-facing API contract.
- [x] Run formatting, static checks, the complete offline test suite, production
  package build, migration tests, and an end-to-end local dashboard smoke test.

Acceptance gate:

- [x] A production-style local deployment supports login, launch, live status,
  termination, reconciled cost, policy enforcement, export, and audit history.
- [x] The final verification report maps every checklist item to authoritative
  test, API, database, or rendered-UI evidence.
