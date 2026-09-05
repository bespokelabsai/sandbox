const state = {
  localMode: window.location.pathname === "/dashboard/local",
  apiKey: window.location.pathname === "/dashboard/local" ? sessionStorage.getItem("bespoke_dashboard_api_key") || "" : "",
  csrfToken: "", authenticated: false,
  days: "30", page: 1, pageSize: 8, session: null,
  sandboxes: [], costs: [], selectedId: null, terminatingId: null,
};

const $ = (selector) => document.querySelector(selector);
const elements = {
  accessPanel: $("#accessPanel"), apiKey: $("#apiKey"), connectionDot: $("#connectionDot"), connectionStatus: $("#connectionStatus"), accessSummary: $("#accessSummary"),
  loginHeading: $("#loginHeading"), loginCopy: $("#loginCopy"),
  dashboard: $("#dashboard"), disconnectButton: $("#disconnectButton"), errorNotice: $("#errorNotice"), permissionNotice: $("#permissionNotice"), keyForm: $("#keyForm"), refreshButton: $("#refreshButton"), updatedAt: $("#updatedAt"),
  activeResources: $("#activeResources"), failedLaunches: $("#failedLaunches"), unreconciledSpend: $("#unreconciledSpend"), ttlNearing: $("#ttlNearing"),
  guardrails: $("#guardrails"), guardrailsNav: $("#guardrailsNav"), quotaList: $("#quotaList"), allowlistSummary: $("#allowlistSummary"), policyVersion: $("#policyVersion"), healthList: $("#healthList"), denialList: $("#denialList"), denialCount: $("#denialCount"),
  launchPanel: $("#launchPanel"), launchForm: $("#launchForm"), launchBackend: $("#launchBackend"), launchPreset: $("#launchPreset"), launchGpu: $("#launchGpu"), launchTimeout: $("#launchTimeout"), launchButton: $("#launchButton"),
  activity: $("#activity"), activityNav: $("#activityNav"), alertList: $("#alertList"), alertCount: $("#alertCount"), auditList: $("#auditList"), auditCount: $("#auditCount"), exportPanel: $("#exportPanel"),
  sandboxRows: $("#sandboxRows"), tableCount: $("#tableCount"), loadingState: $("#loadingState"), emptyState: $("#emptyState"), tableScroll: $("#tableScroll"), pagination: $("#pagination"), previousPage: $("#previousPage"), nextPage: $("#nextPage"), pageLabel: $("#pageLabel"),
  filterForm: $("#filterForm"), statusFilter: $("#statusFilter"), providerFilter: $("#providerFilter"), dateFilter: $("#dateFilter"), clearFilters: $("#clearFilters"),
  periodTabs: $("#periodTabs"), spendChart: $("#spendChart"), providerList: $("#providerList"), detailDialog: $("#detailDialog"), detailTitle: $("#detailTitle"), detailLoading: $("#detailLoading"), detailContent: $("#detailContent"), detailFooter: $("#detailFooter"), closeDetail: $("#closeDetail"), confirmDialog: $("#confirmDialog"), confirmCopy: $("#confirmCopy"), confirmTerminate: $("#confirmTerminate"), toast: $("#toast"),
};

function startDate() { if (state.days === "all") return null; const date = new Date(); date.setUTCDate(date.getUTCDate() - Number(state.days)); return date.toISOString(); }
function costUrl(groupBy) { const parameters = new URLSearchParams({ group_by: groupBy }); const start = startDate(); if (start) parameters.set("start", start); return `/v1/costs?${parameters}`; }

async function api(path, options = {}) {
  const method = options.method || "GET"; const headers = { ...(options.headers || {}) };
  if (state.localMode && state.apiKey) headers.Authorization = `Bearer ${state.apiKey}`;
  if (!state.localMode && !["GET", "HEAD", "OPTIONS"].includes(method)) headers["X-CSRF-Token"] = state.csrfToken;
  if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { ...options, method, headers, credentials: "same-origin" });
  let payload = null;
  try { payload = await response.json(); } catch { payload = null; }
  if (!response.ok) {
    const detail = payload?.detail;
    const error = new Error(typeof detail === "string" ? detail : detail?.message || `Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

function setConnected(connected) {
  elements.connectionDot.classList.toggle("online", connected);
  elements.connectionStatus.textContent = connected ? "Connected" : "Not connected";
  elements.accessPanel.hidden = connected; elements.dashboard.hidden = !connected; elements.disconnectButton.hidden = !connected; elements.refreshButton.hidden = !connected;
}
function showError(message) { elements.errorNotice.textContent = message; elements.errorNotice.hidden = false; }
function clearError() { elements.errorNotice.hidden = true; elements.errorNotice.textContent = ""; }
function showToast(message) { elements.toast.textContent = message; elements.toast.hidden = false; window.setTimeout(() => { elements.toast.hidden = true; }, 4200); }
function escapeHtml(value) { return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;"); }
function formatCurrency(value) { const number = Number(value || 0); if (number === 0) return "$0.00"; if (number < 0.0001) return "≤ $0.0001"; if (number < 1) return `$${number.toFixed(4)}`; return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 }).format(number); }
function formatDuration(seconds) { if (seconds == null || !Number.isFinite(Number(seconds))) return "—"; const value = Math.max(0, Number(seconds)); if (value < 60) return `${Math.round(value)}s`; if (value < 3600) return `${Math.round(value / 60)}m`; if (value < 86400) return `${(value / 3600).toFixed(1)}h`; return `${Math.floor(value / 86400)}d ${Math.round((value % 86400) / 3600)}h`; }
function ageSeconds(sandbox) { return (Date.now() - new Date(sandbox.created_at).getTime()) / 1000; }
function ttlRemaining(sandbox) { const ttl = Number(sandbox.config?.timeout_secs); return ttl > 0 ? ttl - ageSeconds(sandbox) : null; }
function active(sandbox) { return ["creating", "running", "stopping"].includes(sandbox.status); }
function computeLabel(sandbox) { if (sandbox.config?.gpu) return sandbox.config.gpu; if (sandbox.config?.preset) return sandbox.config.preset; if (sandbox.config?.cpu) return `${sandbox.config.cpu} vCPU`; return "Default"; }
function shortId(value, size = 11) { return value && value.length > size ? `${value.slice(0, size)}…` : value || "—"; }
function formatDateTime(value) { if (!value) return "—"; return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }).format(new Date(value)); }

function renderOverview() {
  const costById = new Map(state.costs.map((cost) => [cost.key, cost]));
  const activeItems = state.sandboxes.filter(active);
  const failed = state.sandboxes.filter((item) => item.status === "failed" && !item.running_at).length;
  const unreconciled = state.sandboxes.filter((item) => item.cost_state === "estimated").reduce((sum, item) => sum + Number(costById.get(item.id)?.provider_cost_usd || 0), 0);
  const nearing = activeItems.filter((item) => { const remaining = ttlRemaining(item); const ttl = Number(item.config?.timeout_secs); return remaining != null && remaining > 0 && remaining <= Math.min(900, ttl * 0.25); }).length;
  elements.activeResources.textContent = String(activeItems.length); elements.failedLaunches.textContent = String(failed); elements.unreconciledSpend.textContent = state.session?.can_view_usage ? formatCurrency(unreconciled) : "Unavailable"; elements.ttlNearing.textContent = String(nearing);
}

function quotaRow(label, current, limit, formatter = String) {
  const bounded = limit != null; const ratio = bounded && Number(limit) > 0 ? Math.min(Number(current) / Number(limit) * 100, 100) : bounded && Number(current) > 0 ? 100 : 0;
  return `<div class="quota-row"><div class="quota-meta"><span>${escapeHtml(label)}</span><strong>${escapeHtml(formatter(current))} <small>/ ${bounded ? escapeHtml(formatter(limit)) : "No limit"}</small></strong></div><progress class="quota-track" aria-label="${escapeHtml(label)}" max="100" value="${ratio.toFixed(2)}">${Math.round(ratio)}%</progress></div>`;
}

function renderGovernance(summary, providers) {
  const visible = Boolean(summary || providers); elements.guardrails.hidden = !visible; elements.guardrailsNav.hidden = !visible; if (!visible) return;
  if (summary) {
    const policy = summary.policy; const usage = summary.usage;
    elements.policyVersion.textContent = policy.updated_at ? `Revision ${policy.version}` : "Default policy";
    elements.quotaList.innerHTML = quotaRow("Concurrent sandboxes", usage.active_sandboxes, policy.max_concurrent_sandboxes) + quotaRow("Rolling-hour spend", usage.hourly_spend_usd, policy.hourly_spend_limit_usd, formatCurrency) + quotaRow("UTC-day spend", usage.daily_spend_usd, policy.daily_spend_limit_usd, formatCurrency);
    const backends = policy.allowed_backends == null ? "All enabled providers" : policy.allowed_backends.length ? policy.allowed_backends.join(", ") : "None"; const gpus = policy.allowed_gpu_types == null ? "All GPU types" : policy.allowed_gpu_types.length ? policy.allowed_gpu_types.join(", ") : "CPU only"; const lifetime = policy.max_sandbox_lifetime_secs == null ? "No lifetime ceiling" : `${formatDuration(policy.max_sandbox_lifetime_secs)} maximum TTL`;
    elements.allowlistSummary.innerHTML = `<span><strong>Providers</strong>${escapeHtml(backends)}</span><span><strong>GPU policy</strong>${escapeHtml(gpus)}</span><span><strong>Lifetime</strong>${escapeHtml(lifetime)}</span>`;
    elements.denialCount.textContent = `${summary.denials.length} recent`;
    elements.denialList.innerHTML = summary.denials.length ? summary.denials.map((item) => `<article><span class="denial-mark" aria-hidden="true">!</span><div><strong>${escapeHtml(item.message)}</strong><span>${escapeHtml(item.backend || "Request")} · ${formatDateTime(item.created_at)} · ${escapeHtml(item.api_key_name || shortId(item.api_key_id, 16))}</span></div></article>`).join("") : '<p class="empty-copy">No recent policy denials.</p>';
  } else {
    elements.quotaList.innerHTML = '<p class="empty-copy">Policy visibility is not granted to this key.</p>'; elements.allowlistSummary.innerHTML = ""; elements.denialList.innerHTML = '<p class="empty-copy">Policy visibility is not granted to this key.</p>'; elements.denialCount.textContent = "";
  }
  if (providers) {
    elements.healthList.innerHTML = providers.items.length ? providers.items.map((item) => `<article><span class="health-dot ${escapeHtml(item.status)}"></span><div><strong>${escapeHtml(item.backend)}</strong><span>${escapeHtml(item.message)}${item.checked_at ? ` · ${formatDateTime(item.checked_at)}` : ""}</span></div><span class="health-state">${escapeHtml(item.configured ? item.status : "unconfigured")}</span>${state.session?.can_manage_providers ? `<button class="button secondary check-health" type="button" data-backend="${escapeHtml(item.backend)}">Check</button>` : ""}</article>`).join("") : '<p class="empty-copy">No enabled providers.</p>';
  } else {
    elements.healthList.innerHTML = '<p class="empty-copy">Provider visibility is not granted to this key.</p>';
  }
}

function renderActivity(alerts, audit) {
  const visible = Boolean(alerts || audit || state.session?.can_export); elements.activity.hidden = !visible; elements.activityNav.hidden = !visible;
  elements.exportPanel.hidden = !state.session?.can_export;
  if (alerts) {
    elements.alertCount.textContent = `${alerts.items.length} shown`;
    elements.alertList.innerHTML = alerts.items.length ? alerts.items.map((item) => `<article><span class="activity-mark ${escapeHtml(item.severity)}" aria-hidden="true">${item.severity === "critical" ? "!" : "•"}</span><div><strong>${escapeHtml(item.message)}</strong><span>${escapeHtml(item.alert_type.replaceAll("_", " "))} · ${formatDateTime(item.created_at)}</span></div></article>`).join("") : '<p class="empty-copy">No active alert events.</p>';
  } else { elements.alertCount.textContent = ""; elements.alertList.innerHTML = '<p class="empty-copy">Alert visibility is not granted to this key.</p>'; }
  if (audit) {
    elements.auditCount.textContent = `${audit.items.length} shown`;
    elements.auditList.innerHTML = audit.items.length ? audit.items.map((item) => `<article><span class="activity-mark audit" aria-hidden="true">↳</span><div><strong>${escapeHtml(item.action)}</strong><span>${escapeHtml(item.outcome)} · ${escapeHtml(item.api_key_name || shortId(item.api_key_id, 16))} · ${formatDateTime(item.created_at)}</span></div></article>`).join("") : '<p class="empty-copy">No audit activity yet.</p>';
  } else { elements.auditCount.textContent = ""; elements.auditList.innerHTML = '<p class="empty-copy">Audit visibility is not granted to this key.</p>'; }
}

function filteredSandboxes() {
  const status = elements.statusFilter.value; const provider = elements.providerFilter.value; const days = elements.dateFilter.value; const cutoff = days === "all" ? null : Date.now() - Number(days) * 86400000;
  return state.sandboxes.filter((item) => (status === "all" || item.status === status) && (provider === "all" || item.backend === provider) && (cutoff == null || new Date(item.created_at).getTime() >= cutoff));
}

function renderProviderFilter() {
  const selected = elements.providerFilter.value; const providers = [...new Set(state.sandboxes.map((item) => item.backend))].sort();
  elements.providerFilter.innerHTML = '<option value="all">All providers</option>' + providers.map((provider) => `<option value="${escapeHtml(provider)}">${escapeHtml(provider)}</option>`).join("");
  elements.providerFilter.value = providers.includes(selected) ? selected : "all";
}

function renderSandboxes() {
  const filtered = filteredSandboxes(); const pages = Math.max(1, Math.ceil(filtered.length / state.pageSize)); state.page = Math.min(state.page, pages); const start = (state.page - 1) * state.pageSize; const items = filtered.slice(start, start + state.pageSize);
  elements.loadingState.hidden = true; elements.tableCount.textContent = `${filtered.length} of ${state.sandboxes.length}`; elements.emptyState.hidden = items.length !== 0; elements.tableScroll.hidden = items.length === 0;
  elements.pagination.hidden = filtered.length <= state.pageSize; elements.pageLabel.textContent = `Page ${state.page} of ${pages}`; elements.previousPage.disabled = state.page === 1; elements.nextPage.disabled = state.page === pages;
  elements.sandboxRows.innerHTML = items.map((sandbox) => {
    const remaining = ttlRemaining(sandbox); const creator = sandbox.creator_api_key_name || shortId(sandbox.creator_api_key_id); const cleanup = sandbox.cleanup_status || (sandbox.status === "destroyed" ? "deleted" : "—"); const canTerminate = state.session?.can_terminate && ["creating", "running", "failed"].includes(sandbox.status);
    return `<tr data-sandbox-id="${escapeHtml(sandbox.id)}" class="${state.terminatingId === sandbox.id ? "terminating" : ""}">
      <td><button class="link-button view-detail" type="button" data-id="${escapeHtml(sandbox.id)}"><span class="sandbox-id">${escapeHtml(shortId(sandbox.id))}</span></button><span class="cell-sub"><span class="status-pill ${escapeHtml(sandbox.status)}">${escapeHtml(state.terminatingId === sandbox.id ? "stopping" : sandbox.status)}</span> · ${escapeHtml(creator)}</span></td>
      <td><span title="${escapeHtml(sandbox.provider_resource_id || "Not assigned")}">${escapeHtml(shortId(sandbox.provider_resource_id, 14))}</span><span class="cell-sub provider-name">${escapeHtml(sandbox.backend)}</span></td>
      <td>${escapeHtml(computeLabel(sandbox))}<span class="cell-sub">${sandbox.config?.memory_mb ? `${escapeHtml(sandbox.config.memory_mb)} MB RAM` : "Instance default"}</span></td>
      <td>${formatCurrency(sandbox.hourly_rate_usd)}<span class="cell-sub">per hour</span></td>
      <td>${formatDuration(ageSeconds(sandbox))}<span class="cell-sub">TTL ${remaining == null ? "not set" : remaining <= 0 ? "expired" : formatDuration(remaining)}</span></td>
      <td><span class="state-label ${cleanup === "unknown" ? "bad" : ""}">${escapeHtml(cleanup)}</span></td>
      <td><span class="state-label ${sandbox.cost_state === "estimated" ? "pending" : "good"}">${escapeHtml(sandbox.provider_missing ? "provider missing" : sandbox.cost_state)}</span><span class="cell-sub">${formatDateTime(sandbox.last_provider_observed_at)}</span></td>
      <td><div class="row-actions"><button class="button secondary view-detail" type="button" data-id="${escapeHtml(sandbox.id)}">Details</button>${canTerminate ? `<button class="button danger terminate" type="button" data-id="${escapeHtml(sandbox.id)}" data-version="${sandbox.version}">Terminate</button>` : ""}</div></td>
    </tr>`;
  }).join("");
}

function renderChart(items) {
  if (!items.length) { elements.spendChart.innerHTML = '<p class="chart-empty">No metered usage in this period.</p>'; return; }
  const width = 760, height = 210, pad = { top: 12, right: 12, bottom: 25, left: 48 }; const values = items.map((item) => Number(item.customer_cost_usd)); const maximum = Math.max(...values, 0.000001); const x = (index) => pad.left + (index / Math.max(items.length - 1, 1)) * (width - pad.left - pad.right); const y = (value) => height - pad.bottom - (value / maximum) * (height - pad.top - pad.bottom); const points = values.map((value, index) => `${x(index)},${y(value)}`).join(" "); const area = `${pad.left},${height - pad.bottom} ${points} ${x(values.length - 1)},${height - pad.bottom}`;
  elements.spendChart.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Daily customer spend chart"><defs><linearGradient id="spendFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#ed4b2f" stop-opacity=".24"/><stop offset="100%" stop-color="#ed4b2f" stop-opacity=".02"/></linearGradient></defs><line x1="${pad.left}" y1="${height - pad.bottom}" x2="${width - pad.right}" y2="${height - pad.bottom}" stroke="#ded9ce"/><polygon points="${area}" fill="url(#spendFill)"/><polyline points="${points}" fill="none" stroke="#ed4b2f" stroke-width="2.5" stroke-linejoin="round"/>${values.map((value, index) => `<circle cx="${x(index)}" cy="${y(value)}" r="3" fill="#ed4b2f"><title>${escapeHtml(items[index].key)}: ${formatCurrency(value)}</title></circle>`).join("")}</svg>`;
}

function renderProviders(items) {
  if (!items.length) { elements.providerList.innerHTML = '<p class="provider-empty">No provider usage yet.</p>'; return; }
  const maximum = Math.max(...items.map((item) => Number(item.customer_cost_usd)), .000001);
  elements.providerList.innerHTML = [...items].sort((a, b) => Number(b.customer_cost_usd) - Number(a.customer_cost_usd)).map((item) => { const value = Number(item.customer_cost_usd); return `<div class="provider-row"><div class="provider-meta"><span class="provider-name"><span class="provider-swatch"></span>${escapeHtml(item.key)}</span><span class="provider-value">${formatCurrency(value)} · ${formatDuration(item.runtime_seconds)}</span></div><progress class="provider-track" aria-label="${escapeHtml(item.key)} share of spend" max="100" value="${Math.max(value / maximum * 100, 1).toFixed(2)}"></progress></div>`; }).join("");
}

function timelineItem(label, value) { return `<li class="${value ? "complete" : ""}"><span class="timeline-dot"></span><div><strong>${escapeHtml(label)}</strong><span>${escapeHtml(formatDateTime(value))}</span></div></li>`; }
function errorBlock(error) { if (!error) return ""; return `<div class="error-block"><strong>${escapeHtml(error.code || "Operation failed")}</strong><p>${escapeHtml(error.message || "Sandbox operation failed.")}</p></div>`; }

function renderDetail(detail) {
  const sandbox = detail.sandbox; state.selectedId = sandbox.id; elements.detailTitle.textContent = sandbox.id; elements.detailLoading.hidden = true;
  const cost = detail.cost || {}; const canTerminate = state.session?.can_terminate && ["creating", "running", "failed"].includes(sandbox.status);
  elements.detailContent.innerHTML = `<div class="detail-summary"><div><span>Status</span><strong><span class="status-pill ${escapeHtml(sandbox.status)}">${escapeHtml(sandbox.status)}</span></strong></div><div><span>Provider</span><strong>${escapeHtml(sandbox.backend)}</strong></div><div><span>Resource</span><strong title="${escapeHtml(sandbox.provider_resource_id || "")}">${escapeHtml(shortId(sandbox.provider_resource_id, 18))}</strong></div><div><span>Creator key</span><strong>${escapeHtml(sandbox.creator_api_key_name || shortId(sandbox.creator_api_key_id))}</strong></div></div>
    ${errorBlock(sandbox.latest_error)}
    <div class="detail-grid"><section><p class="section-label">Lifecycle timeline</p><ol class="timeline">${timelineItem("Requested", sandbox.requested_at)}${timelineItem("Provisioning", sandbox.provisioning_at)}${timelineItem("Running", sandbox.running_at)}${timelineItem("Stopping", sandbox.stopping_at)}${timelineItem("Terminated", sandbox.terminated_at)}${timelineItem("Failed", sandbox.failed_at)}</ol></section>
    <section><p class="section-label">Cost breakdown</p><dl class="cost-list"><div><dt>Billable runtime</dt><dd>${formatDuration(cost.billable_seconds || 0)}</dd></div><div><dt>Estimated</dt><dd>${formatCurrency(cost.estimated_cost_usd || 0)}</dd></div><div><dt>Provider reported</dt><dd>${cost.provider_reported_cost_usd == null ? "—" : formatCurrency(cost.provider_reported_cost_usd)}</dd></div><div><dt>Effective cost</dt><dd>${formatCurrency(cost.effective_cost_usd || 0)}</dd></div><div><dt>Customer cost</dt><dd>${formatCurrency(cost.customer_cost_usd || 0)}</dd></div><div><dt>State</dt><dd>${escapeHtml(cost.cost_state || sandbox.cost_state)}</dd></div></dl></section></div>
    <section class="history-section"><p class="section-label">Creation attempts</p>${detail.attempts.length ? `<div class="history-list">${detail.attempts.map((item) => `<article><div><strong>Attempt ${item.attempt_number}</strong><span>${escapeHtml(item.status)} · ${formatDateTime(item.started_at)}</span></div>${errorBlock(item.error)}</article>`).join("")}</div>` : '<p class="empty-copy">No creation attempts recorded.</p>'}</section>
    <section class="history-section"><p class="section-label">Executions</p>${detail.executions.length ? `<div class="history-list">${detail.executions.map((item) => `<article><div><strong>${escapeHtml(shortId(item.request_id, 18))}</strong><span>${escapeHtml(item.status)} · ${formatDateTime(item.created_at)}</span></div>${item.response ? `<code>exit ${escapeHtml(item.response.exit_code ?? 0)} · ${escapeHtml(shortId(item.response.stdout || "No output", 80))}</code>` : ""}${errorBlock(item.error)}</article>`).join("")}</div>` : '<p class="empty-copy">No executions recorded.</p>'}</section>
    <section class="history-section"><p class="section-label">Provider observations</p>${detail.provider_observations.length ? `<div class="history-list">${detail.provider_observations.map((item) => `<article><div><strong>${escapeHtml(item.missing ? "Provider resource missing" : item.status)}</strong><span>${formatDateTime(item.observed_at)}${item.provider_cost_usd == null ? "" : ` · ${formatCurrency(item.provider_cost_usd)}`}</span></div></article>`).join("")}</div>` : '<p class="empty-copy">No provider observations recorded.</p>'}</section>`;
  elements.detailFooter.innerHTML = `<span>Revision ${sandbox.version}</span>${canTerminate ? `<button class="button danger terminate" type="button" data-id="${escapeHtml(sandbox.id)}" data-version="${sandbox.version}">Terminate sandbox</button>` : '<span class="read-only-label">Read-only access</span>'}`;
}

async function openDetail(id) {
  state.selectedId = id; elements.detailContent.innerHTML = ""; elements.detailFooter.innerHTML = ""; elements.detailLoading.hidden = false; if (!elements.detailDialog.open) elements.detailDialog.showModal();
  try { renderDetail(await api(`/v1/sandboxes/${encodeURIComponent(id)}/detail`)); } catch (error) { elements.detailLoading.hidden = true; elements.detailContent.innerHTML = `<div class="detail-error" role="alert">${escapeHtml(error.message)}</div>`; }
}

function requestTermination(id, version) {
  const sandbox = state.sandboxes.find((item) => item.id === id); if (!state.session?.can_terminate || !sandbox) return;
  elements.confirmTerminate.dataset.id = id; elements.confirmTerminate.dataset.version = String(version); elements.confirmCopy.textContent = `Terminate ${id}? This stops ${sandbox.provider_resource_id || "its provider resource"} and finalizes lifecycle cost.`; elements.confirmDialog.showModal();
}

async function terminateSandbox(id, version) {
  elements.confirmTerminate.disabled = true; elements.confirmTerminate.textContent = "Terminating…"; state.terminatingId = id; renderSandboxes();
  try {
    await api(`/v1/sandboxes/${encodeURIComponent(id)}`, { method: "DELETE", headers: { "If-Match": `\"${version}\"` } });
    elements.confirmDialog.close(); await loadDashboard(); await openDetail(id); showToast("Sandbox terminated. Final lifecycle and cost are now visible.");
  } catch (error) {
    elements.confirmDialog.close(); showError(error.status === 409 ? "The sandbox changed before termination. Its latest state has been loaded." : error.message); await loadDashboard(); if (state.selectedId === id) await openDetail(id);
  } finally { state.terminatingId = null; elements.confirmTerminate.disabled = false; elements.confirmTerminate.textContent = "Terminate sandbox"; renderSandboxes(); }
}

async function loadDashboard() {
  if (state.localMode && !state.apiKey) { setConnected(false); return; }
  clearError(); elements.refreshButton.disabled = true; elements.updatedAt.textContent = "Refreshing…"; if (!state.sandboxes.length) { elements.loadingState.hidden = false; elements.tableScroll.hidden = true; }
  try {
    const session = await api("/v1/session"); state.session = session; state.authenticated = true; state.csrfToken = session.csrf_token || "";
    const unavailableCosts = { items: [] };
    const costRequests = session.can_view_usage ? [api(costUrl("day")), api(costUrl("backend")), api(costUrl("sandbox"))] : [Promise.resolve(unavailableCosts), Promise.resolve(unavailableCosts), Promise.resolve(unavailableCosts)];
    const requests = [...costRequests, api("/v1/sandboxes")];
    const governanceRequests = [session.can_view_policy ? api("/v1/policy-summary") : Promise.resolve(null), session.can_view_providers ? api("/v1/providers") : Promise.resolve(null)];
    const activityRequests = [session.can_view_alerts ? api("/v1/alerts?limit=8") : Promise.resolve(null), session.can_view_audit ? api("/v1/audit?limit=8") : Promise.resolve(null)];
    const [daily, providers, sandboxCosts, sandboxes, policySummary, providerHealth, alerts, audit] = await Promise.all([...requests, ...governanceRequests, ...activityRequests]);
    state.sandboxes = sandboxes; state.costs = sandboxCosts.items || []; elements.permissionNotice.hidden = session.can_terminate; elements.accessSummary.textContent = `${session.role[0].toUpperCase()}${session.role.slice(1)} access · ${session.can_terminate ? "termination enabled" : "destructive controls hidden"}`;
    elements.launchPanel.hidden = !session.can_create; renderProviderFilter(); renderOverview(); renderGovernance(policySummary, providerHealth); renderActivity(alerts, audit); renderSandboxes(); if (session.can_view_usage) { renderChart(daily.items || []); renderProviders(providers.items || []); } else { elements.spendChart.innerHTML = '<p class="chart-empty">Spend data requires usage access.</p>'; elements.providerList.innerHTML = '<p class="provider-empty">Provider spend requires usage access.</p>'; } setConnected(true); elements.updatedAt.textContent = `Updated ${new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit" }).format(new Date())}`;
  } catch (error) {
    if (error.status === 401 || error.status === 403) { if (state.localMode) { sessionStorage.removeItem("bespoke_dashboard_api_key"); state.apiKey = ""; } state.authenticated = false; state.csrfToken = ""; setConnected(false); if (state.localMode || error.status === 403) showError("Authentication failed or this identity lacks dashboard access."); }
    else { showError(error.message || "Could not load dashboard data."); elements.updatedAt.textContent = "Refresh failed"; }
  } finally { elements.refreshButton.disabled = false; }
}

elements.keyForm.addEventListener("submit", async (event) => { event.preventDefault(); const value = elements.apiKey.value.trim(); if (!value) return; clearError(); try { if (state.localMode) { state.apiKey = value; sessionStorage.setItem("bespoke_dashboard_api_key", value); } else { const response = await fetch("/v1/dashboard/session", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ api_key: value }) }); const payload = await response.json(); if (!response.ok) { const detail = payload?.detail; throw Object.assign(new Error(typeof detail === "string" ? detail : "Dashboard sign-in failed."), { status: response.status }); } state.csrfToken = payload.csrf_token; state.authenticated = true; } elements.apiKey.value = ""; await loadDashboard(); } catch (error) { showError(error.message || "Dashboard sign-in failed."); setConnected(false); } });
elements.refreshButton.addEventListener("click", loadDashboard);
elements.disconnectButton.addEventListener("click", async () => { if (!state.localMode && state.authenticated) { try { await api("/v1/dashboard/session", { method: "DELETE" }); } catch {} } sessionStorage.removeItem("bespoke_dashboard_api_key"); state.apiKey = ""; state.csrfToken = ""; state.authenticated = false; state.session = null; state.sandboxes = []; setConnected(false); clearError(); elements.updatedAt.textContent = "Waiting to connect"; });
elements.filterForm.addEventListener("change", () => { state.page = 1; renderSandboxes(); });
elements.clearFilters.addEventListener("click", () => { elements.statusFilter.value = "all"; elements.providerFilter.value = "all"; elements.dateFilter.value = "all"; state.page = 1; renderSandboxes(); });
elements.previousPage.addEventListener("click", () => { state.page -= 1; renderSandboxes(); }); elements.nextPage.addEventListener("click", () => { state.page += 1; renderSandboxes(); });
elements.sandboxRows.addEventListener("click", (event) => { const detail = event.target.closest(".view-detail"); const terminate = event.target.closest(".terminate"); if (detail) openDetail(detail.dataset.id); if (terminate) requestTermination(terminate.dataset.id, Number(terminate.dataset.version)); });
elements.detailFooter.addEventListener("click", (event) => { const terminate = event.target.closest(".terminate"); if (terminate) requestTermination(terminate.dataset.id, Number(terminate.dataset.version)); });
elements.healthList.addEventListener("click", async (event) => { const button = event.target.closest(".check-health"); if (!button) return; button.disabled = true; button.textContent = "Checking…"; try { await api(`/v1/providers/${encodeURIComponent(button.dataset.backend)}/health-check`, { method: "POST" }); await loadDashboard(); showToast(`${button.dataset.backend} health check completed.`); } catch (error) { showError(error.message); button.disabled = false; button.textContent = "Check"; } });
elements.launchForm.addEventListener("submit", async (event) => { event.preventDefault(); elements.launchButton.disabled = true; elements.launchButton.textContent = "Launching…"; const body = { backend: elements.launchBackend.value.trim(), timeout_secs: Number(elements.launchTimeout.value) }; if (elements.launchPreset.value.trim()) body.preset = elements.launchPreset.value.trim(); if (elements.launchGpu.value.trim()) body.gpu = elements.launchGpu.value.trim(); try { const created = await api("/v1/sandboxes", { method: "POST", body: JSON.stringify(body) }); await loadDashboard(); await openDetail(created.id); showToast("Sandbox launched and visible in live inventory."); } catch (error) { showError(error.message); } finally { elements.launchButton.disabled = false; elements.launchButton.textContent = "Launch sandbox"; } });
elements.exportPanel.addEventListener("click", async (event) => { const button = event.target.closest(".export-button"); if (!button) return; button.disabled = true; try { const headers = {}; if (state.localMode && state.apiKey) headers.Authorization = `Bearer ${state.apiKey}`; const response = await fetch(`/v1/exports/${encodeURIComponent(button.dataset.kind)}.csv?limit=500`, { credentials: "same-origin", headers }); if (!response.ok) throw new Error("CSV export failed."); const blob = await response.blob(); const link = document.createElement("a"); link.href = URL.createObjectURL(blob); link.download = `bespoke-${button.dataset.kind}.csv`; link.click(); URL.revokeObjectURL(link.href); await loadDashboard(); showToast(`${button.dataset.kind} CSV downloaded.`); } catch (error) { showError(error.message); } finally { button.disabled = false; } });
elements.closeDetail.addEventListener("click", () => elements.detailDialog.close());
elements.confirmTerminate.addEventListener("click", (event) => { event.preventDefault(); terminateSandbox(event.currentTarget.dataset.id, Number(event.currentTarget.dataset.version)); });
elements.periodTabs.addEventListener("click", (event) => { const button = event.target.closest("button[data-days]"); if (!button) return; state.days = button.dataset.days; elements.periodTabs.querySelectorAll("button").forEach((item) => item.classList.toggle("active", item === button)); loadDashboard(); });
document.addEventListener("visibilitychange", () => { if (!document.hidden && (state.authenticated || state.apiKey)) loadDashboard(); });
window.setInterval(() => { if (!document.hidden && (state.authenticated || state.apiKey)) loadDashboard(); }, 15000);

if (state.localMode) { elements.loginHeading.textContent = "Use local API-key mode"; elements.loginCopy.textContent = "Development-only mode stores the API key in this browser tab. Production deployments should use the HTTP-only session dashboard."; }
setConnected(false); if (state.localMode ? state.apiKey : true) loadDashboard();
