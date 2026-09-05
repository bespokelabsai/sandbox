# Dashboard roadmap final verification

Verified on 2026-09-04 with deterministic local/provider fakes only. No live
credentials or billable resources were used.

## Release result

| Check | Result |
|---|---|
| Pinned formatter | Pyink 24.10.1: 61 files unchanged |
| Static analysis | Ruff with cache disabled: clean |
| Byte compilation | `src` and `tests`: clean |
| Complete offline suite | 275 passed, 81 skipped, 2 subtests passed |
| Database compatibility | Fresh schema v7 and legacy in-place migration/reopen passed |
| Distribution | sdist and wheel built without isolation |
| Clean wheel install | Version 0.1.3, API entry point, and dashboard assets verified |
| Production-style browser | Login → live inventory → launch → terminate → reconciled cost → denial → CSV → audit passed |
| Browser diagnostics | Expected HTTP statuses; zero console warnings/errors |
| Responsive check | 390×844 layout inspected; table/nav overflow is locally contained |

## Phase 1 — Lifecycle and error contract

| Requirement | Authoritative evidence |
|---|---|
| Stable provider errors | `test_errors.py` contract tests; `test_remote.py::test_structured_provider_error_fields_are_exposed`; API provider error handler |
| Daytona typed failure classification | `test_daytona_backend.py::test_typed_timeout_is_classified_without_message_parsing` and `test_sdk_types_not_messages_determine_the_error_contract` |
| Orphan cleanup result and retry safety | Daytona not-found/unknown/delete tests; `test_control_plane.py::test_ambiguous_create_is_never_reported_safe_to_retry` |
| Idempotent sandbox creation | `test_control_plane.py::test_create_idempotency_returns_one_logical_and_provider_sandbox` and differing-request conflict test |
| Persisted attempts and retry/cleanup metadata | Migration 2 `sandbox_creation_attempts`; timeout and detail API tests |
| Local-library and SQLite compatibility | Complete regression suite; `test_fresh_database_has_phase5_schema_and_defaults`; legacy migration/reopen test |
| Unit/API/client coverage and redaction | `test_daytona_backend.py`, `test_control_plane.py`, `test_remote.py`, and `test_errors.py` |
| Timeout acceptance gate | `test_control_plane.py::test_timeout_is_structured_persisted_redacted_and_cleaned` plus Daytona orphan reap test |
| Duplicate-create acceptance gate | `test_create_idempotency_returns_one_logical_and_provider_sandbox` asserts one provider call/resource |

## Phase 2 — Metering and reconciliation

| Requirement | Authoritative evidence |
|---|---|
| Full lifecycle timestamps | Migration 3 fields; lifecycle transition and dashboard detail tests |
| Entire billable lifecycle cost | `test_destroy_records_stopping_terminated_and_full_billable_window` and failed provisioning cost test |
| Rate/currency/source/cost-state snapshots | `lifecycle_costs` schema and cost-detail API assertions |
| Reconciler, missing, and orphan abstraction | `reconciliation.py`; partial-outage/disappearance/orphan test |
| Tenant-safe idempotent reconciliation | `test_partial_outage_retry_disappearance_orphan_and_tenant_safety` |
| Failed provisioning/cleanup ledger cost | `test_failed_provisioning_accrues_nonzero_lifecycle_cost` |
| Authenticated lifecycle/reconciliation APIs | `test_lifecycle_and_reconciliation_summaries_are_authenticated` and remote endpoint test |
| Retry/outage/duplicate/time/decimal coverage | All `test_reconciliation.py` tests, including UTC midnight and clock reversal |
| Failed-cost acceptance gate | Failed provisioning test asserts non-zero lifecycle cost |
| Reconciled-cost acceptance gate | `test_provider_cost_replaces_estimate_without_double_counting`; browser rendered `$0.2400` reconciled cost |

## Phase 3 — Operational dashboard

| Requirement | Authoritative evidence |
|---|---|
| Overview cards | Dashboard asset assertions and rendered browser values |
| Rich sandbox inventory | Dashboard row rendering plus creator/detail/history API test |
| Filters, pagination, states, visibility-aware refresh | Static dashboard test and rendered two-page browser inventory |
| Lifecycle detail view | `test_session_creator_detail_and_history_contract`; rendered timeline, execution, observation, and cost |
| Authorized optimistic/idempotent termination | Success/stale/idempotency and failure tests; browser confirmation flow |
| Keyboard/responsive controls | Semantic button/form/dialog markup, focus styles, breakpoints, and 390×844 rendered inspection |
| Browser/API permissions coverage | Dashboard test suite and Phase 5 production browser run |
| Operator acceptance gate | Browser-created fake resource terminated and immediately rendered Destroyed with final timeline |
| Read-only acceptance gate | `test_read_only_key_can_inspect_but_cannot_terminate` |

## Phase 4 — Guardrails and provider health

| Requirement | Authoritative evidence |
|---|---|
| Complete organization policy | `test_all_policy_denials_are_pre_provider_and_attributed` and spend-limit test |
| Atomic pre-provider enforcement | `test_concurrent_launches_cannot_exceed_limit` and denial provider-call assertions |
| Server-owned safe provider health | `test_provider_health_never_exposes_or_persists_credentials` |
| Attribution and scoped roles | `test_scoped_roles_and_action_attribution` and dashboard permission test |
| TTL/orphan restart watchdog | Restart recovery, stale-stopping recovery, and orphan idempotency tests |
| Guardrails/health/denials UI | Dashboard static checks and rendered quota, health, and denial cards |
| Isolation/race/restart/redaction coverage | Complete `test_policies.py` suite |
| Concurrency acceptance gate | Concurrent test proves the tenant limit is never exceeded |
| Restart acceptance gate | Restart supervisor tests prove one cleanup without persisted provider credentials |

## Phase 5 — Alerts, exports, security, and readiness

| Requirement | Authoritative evidence |
|---|---|
| Five configurable alert types | `test_all_alert_types_are_tenant_scoped_and_idempotent`; alert configuration API/models/tables; rendered alert feed |
| Tenant CSV usage/cost/ledger | Paginated/all-kind export tests, formula-prefix neutralization with signed numeric preservation, and successful rendered Costs CSV action/API request |
| HTTP-only production session and explicit local path | Secure cookie/session test; `/dashboard/local` default-off test; production browser login |
| CSRF, CSP/headers, audit, redaction | Session security test, E2E audit assertions, secret persistence checks, and zero-error browser run |
| Retention and large-data pagination | Retention deletion/preservation test; cursor pages for alerts, audit, and exports; malformed cursor test |
| Complete operational documentation | `docs/CONTROL_PLANE_OPERATIONS.md`, `docs/CONTROL_PLANE_API.md`, and updated README |
| Full release pipeline | Formatter, Ruff, compile, 275-test suite, schema v7 migration, sdist/wheel build/install, and browser smoke above |
| Production-style acceptance gate | `ProductionEndToEndTest` plus rendered login/launch/live/terminate/reconciled/denial/export/audit flow |
| Final evidence acceptance gate | This requirement-by-requirement matrix and the checked authoritative roadmap |

## Security and operational boundaries

The shipped SQLite store intentionally supports one control-plane process.
Production requires HTTPS even though the loopback browser fixture disables the
Secure attribute solely so an HTTP-only local test can receive the cookie.
Operational retention does not delete sandbox, usage, lifecycle-cost, or
financial-ledger records. Provider health remains `unchecked` when no actual
checker is configured.

The stock `bespokelabs-sandbox-api` CLI supplies provider settings only. It does
not register real provider health checkers, reconcilers, or out-of-process
terminators. Therefore a stock-CLI process cannot reconcile provider-reported
state/cost, verify health, discover orphans, or reclaim a real cloud resource
after restart once the in-memory runtime is gone. Phase 4 proves those service
boundaries with injected deterministic adapters; production deployments must
implement and inject equivalent provider-specific adapters before claiming
those capabilities. These are documented constraints, not unverified gaps.
