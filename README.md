<p align="center">
  <a href="https://bespokelabs.ai/" target="_blank">
    <picture>
      <source media="(prefers-color-scheme: light)" width="100px" srcset="https://github.com/bespokelabsai/curator/blob/main/docs/Bespoke-Labs-Logomark-Red-crop.png">
      <img alt="Bespoke Labs Logo" width="100px" src="https://github.com/bespokelabsai/curator/blob/main/docs/Bespoke-Labs-Logomark-Red-crop.png">
    </picture>
  </a>
</p>

<h1 align="center">OpenRouter for Sandboxes</h1>
<h3 align="center" style="font-size: 20px; margin-bottom: 4px">One API. Many sandbox providers.</h3>
<br/>

Just like [OpenRouter](https://openrouter.ai) gives you a single API across LLM providers, `bespokelabs-sandbox` gives you a unified interface across sandbox providers. Write your code once, swap backends with a single parameter.

Use `Sandbox` / `SandboxClient` when your application owns provider credentials,
or `RemoteSandboxClient` when it connects to a control plane that you operate.
The direct SDK provides execution, files, workspaces, agent helpers, and session
resume. The optional server adds organization keys, policies, metering, and an
operations dashboard.

**Contents:** [Install](#install) · [Quickstart](#quickstart) ·
[Hosted control plane](#hosted-control-plane) · [API reference](#api-reference) ·
[Feature support](#feature-support-matrix) · [Examples](#examples) ·
[Development](#development)

## Why?

- **Common interface** — Use the same execution and file APIs across nine backends, with provider-specific configuration where needed.
- **Easily move between providers** — If one provider has an outage or capacity issue, change one string and keep running.
- **Cost tracking** — Track Claude Code token usage, estimated sandbox compute cost, and compare backend pricing.
- **Optional hosted access** — Operate multiple providers behind organization API keys, with lifecycle costs, policies, audit history, and a dashboard.

Backend selection is explicit. Automatic cheapest-provider routing and automatic
failover are not implemented; the benchmark example helps compare providers.

## Install

The core SDK declares Python **3.10+**. Use **Python 3.11+** for the control
plane and its tests: the current server implementation imports `datetime.UTC`.
The only required core dependency is Pydantic; provider SDKs are optional.

```bash
pip install bespokelabs-sandbox
```

With a specific backend:

```bash
pip install 'bespokelabs-sandbox[docker]'
pip install 'bespokelabs-sandbox[daytona]'
pip install 'bespokelabs-sandbox[tensorlake]'
pip install 'bespokelabs-sandbox[modal]'
pip install 'bespokelabs-sandbox[e2b]'
pip install 'bespokelabs-sandbox[ray]'
pip install 'bespokelabs-sandbox[all]'
```

`[all]` installs all optional provider SDKs, but does not include `[server]`
or `[dev]`. Install `'bespokelabs-sandbox[server,e2b]'`, for example, to run
the API with E2B support. The remote HTTP client needs only the core package.

The RunPod backend has no Python extra; it uses the system OpenSSH client.
The Safehouse backend also has no Python extra. Install its CLI separately on
macOS:

```bash
brew install eugene1g/safehouse/agent-safehouse
```

## Supported Backends

### Local

Local execution needs no provider account. Docker, Ray, and Safehouse have
the runtime requirements listed below.

| Backend | Extra | Requires |
|---|---|---|
| Local subprocess | _(none)_ | Python installed |
| [Agent Safehouse](https://github.com/eugene1g/agent-safehouse) | _(none)_ | macOS + `safehouse` CLI |
| [Docker](https://www.docker.com) | `[docker]` | Docker daemon running |
| [Ray](https://www.ray.io) | `[ray]` | Ray installed (local or remote cluster) |

### Cloud

| Backend | Extra | Auth |
|---|---|---|
| [Daytona](https://www.daytona.io) | `[daytona]` | `DAYTONA_API_KEY` |
| [Tensorlake](https://tensorlake.ai) | `[tensorlake]` | `TENSORLAKE_API_KEY` or `tl login` |
| [Modal](https://modal.com) | `[modal]` | `MODAL_TOKEN_ID` + `MODAL_TOKEN_SECRET` |
| [RunPod](https://www.runpod.io) | _(none)_ | `RUNPOD_API_KEY` + registered SSH key |
| [E2B](https://e2b.dev) | `[e2b]` | `E2B_API_KEY` |

You only need to install the backend you use. The others are lazily imported.

Local and Ray execute subprocesses on the host or Ray worker and inherit its
environment. Their workspace path rewriting is a convenience, not an OS
security boundary. Safehouse wraps subprocesses with its macOS policy, while
its file helpers still access the host directly. Use the isolation properties
of your selected backend when deciding which workloads to run.

## Quickstart

```python
from bespokelabs.sandbox import Sandbox

# Zero setup — runs locally
with Sandbox("local") as sb:
    result = sb.execute_code('print("hello")')
    print(result.stdout)

# Or use Safehouse on macOS
with Sandbox("safehouse") as sb:
    result = sb.execute_code('print("hello from safehouse")')
    print(result.stdout)

# Or use Docker
with Sandbox("docker") as sb:
    result = sb.execute_code('print("hello from a container")')
    print(result.stdout)

# Or any cloud provider — same interface
with Sandbox("e2b") as sb:
    result = sb.execute_code('print("hello from the cloud")')
    print(result.stdout)
```

Switch backends by changing one string:

```python
for backend in [
    "local", "safehouse", "docker", "modal", "runpod",
    "e2b", "daytona", "tensorlake", "ray",
]:
    options = {"gpu": "NVIDIA L4"} if backend == "runpod" else {}
    with Sandbox(backend, **options) as sb:
        sb.execute_code('print("same code, any backend")')
```

## Hosted control plane

The optional control plane lets a customer use one Bespoke API key across all
of the providers you operate. Provider credentials remain on the server, and
every execution is automatically attributed to the customer's organization
and sandbox.

This is a self-hosted service shipped in the repository. Run **one API process
per SQLite database**: live sandbox handles are held in that process, and
execution cannot resume automatically after a restart. Graceful shutdown
attempts to destroy the process's attached sandboxes.

### 1. Install the server and provider

Install only the provider adapters enabled by this deployment. For a local and
E2B development server:

```bash
pip install 'bespokelabs-sandbox[server,e2b]'
```

From a source checkout, use:

```bash
pip install -e '.[server,e2b]'
```

### 2. Configure and start the server

Provider credentials belong on the server. Customers never receive them.

```bash
export BESPOKE_API_KEY_PEPPER='replace-with-a-long-random-secret'
export BESPOKE_CONTROL_PLANE_ADMIN_TOKEN='replace-with-another-random-secret'
export BESPOKE_CONTROL_PLANE_DB='/absolute/path/to/bespoke-control-plane.db'
export BESPOKE_ALLOWED_BACKENDS='local,e2b'
export BESPOKE_CUSTOMER_MARKUP='1.20'
export BESPOKE_SUPERVISION_INTERVAL_SECS='30'
export BESPOKE_SESSION_COOKIE_SECURE='1'
export BESPOKE_DASHBOARD_SESSION_TTL_SECS='28800'

# Provider credential — server-side only.
export E2B_API_KEY='your-e2b-api-key'

bespokelabs-sandbox-api
```

Verify that it is running:

```bash
curl http://127.0.0.1:8000/healthz
# {"status":"ok"}
```

`BESPOKE_API_KEY_PEPPER` and `BESPOKE_CONTROL_PLANE_DB` are persistent server
identity. Keep both values unchanged across restarts. Changing the pepper or
pointing at another database makes previously issued product keys invalid.
The backend allowlist is also read at startup, so restart the server after
changing `BESPOKE_ALLOWED_BACKENDS`. If omitted, all registered backends are
allowed; set an explicit list for your deployment. `HOST` and `PORT` default to
`127.0.0.1` and `8000`. The CLI serves HTTP; terminate HTTPS at a reverse proxy
for the secure dashboard cookie.

Provider configuration is server-owned. The control plane reads the standard
Daytona, E2B, Modal, Runpod, and Tensorlake credential variables into memory;
it never returns their names or values through the product API. Scoped
provider-health responses expose only the backend, configured state, health
state, check time, and a fixed safe message. A configured provider remains
`unchecked` unless a deployment-supplied health checker actually runs; only a
successful check is reported as `healthy`.

The stock `bespokelabs-sandbox-api` CLI wires provider settings for safe
configuration visibility only. It does **not** construct provider health
checkers, reconcilers, or out-of-process terminators. Consequently, stock-CLI
provider health remains `unchecked`, reconciliation is unavailable, and a
restarted process cannot reclaim a real cloud resource whose in-memory runtime
was lost. A production deployment requiring those capabilities must instantiate
`ControlPlane` in deployment code and inject provider-specific health-check,
reconciler, and terminator adapters after testing them against that provider.

### 3. Issue the initial product API key

Issue an organization's initial product key through the protected bootstrap
endpoint. The returned `secret` is shown only in this response:

```bash
curl -X POST http://127.0.0.1:8000/v1/organizations \
  -H "X-Control-Plane-Admin: $BESPOKE_CONTROL_PLANE_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Acme"}'
```

Copy the returned value beginning with `bsk_live_`. That is the customer-facing
product key. It is different from `E2B_API_KEY` and works across every backend
enabled by the server.

### 4. Run a client job

Set the product key in the client environment:

```bash
export BESPOKE_SANDBOX_KEY='bsk_live_...'
```

Then create and use a sandbox through the remote client:

```python
import os

from bespokelabs.sandbox import RemoteSandboxClient

client = RemoteSandboxClient(
    "http://127.0.0.1:8000",
    os.environ["BESPOKE_SANDBOX_KEY"],
)

with client.create(
    "e2b",
    timeout_secs=60,
    idempotency_key="example-launch-001",
) as sandbox:
    result = sandbox.execute_code(
        'print("hello from E2B")',
        idempotency_key="example-job-001",
    )
    print(result.stdout)
    print(result.usage)

# The same key can create a different provider's sandbox.
with client.create("local") as sandbox:
    result = sandbox.execute_command("python", ["--version"])
    print(result.stdout)

costs = client.costs(group_by="backend")
print(costs)
```

The context manager requests sandbox destruction on exit. Creation keys are
scoped to an organization: the same key and payload return the existing record,
including its current status, while a different payload is rejected. Execution
keys are scoped to an organization and sandbox. A completed execution can
return its cached response while the runtime remains attached and running;
an in-progress or failed execution returns a conflict. Execution payloads are
not compared, so use a new key for each distinct command and reuse it only to
retry that command. The client does not automatically retry HTTP requests.

`RemoteSandboxClient(..., timeout_secs=60)` sets the HTTP request timeout;
`client.create(..., timeout_secs=60)` sets the sandbox timeout. Allow enough
HTTP time for provider creation. A client timeout does not cancel server work.

The remote surface currently supports creation, inventory (`list` / `get`),
code/command execution, destruction, costs, and reconciliation. It does not
expose the direct SDK's file transfers, `Manifest`, `backend_options`, agent
helpers, structured `return_type`, snapshot creation, or session resume. See the
[API contract](docs/CONTROL_PLANE_API.md) for the remaining HTTP endpoints.
Cost amounts and durations arrive as decimal strings; `group_by` accepts
`"sandbox"`, `"backend"`, or `"day"`.

Provider failures expose a stable error contract in both HTTP responses and
`RemoteSandboxError`: `code`, `backend`, `op`, `retryable`, `outcome`, and a
small, redacted `context`. Sandbox responses include `attempt_count`,
`latest_error`, `retry_status`, `cleanup_status`, and `provider_resource_id`.
An ambiguous create has `outcome="unknown"` and
`retry_status="blocked_cleanup_unknown"`; do not issue a new provider create
until the possible orphan has been reconciled manually.

Sandbox records also expose requested, provisioning, running, stopping,
terminated, failed, and last-provider-observation timestamps. Lifecycle cost
uses the rate, currency, and pricing-source snapshot captured at creation and
accrues across the whole provider-billable window, including failed setup and
cleanup. `GET /v1/reconciliation` returns tenant-scoped health; an operator can
run a configured provider reconciler with
`POST /v1/reconciliation/{backend}` when the deployment injected that backend's
reconciler. Provider-reported cost replaces the
estimate through an idempotent delta ledger, so repeated observations do not
double-count spend. Billable-time deltas are split at UTC day boundaries, so
refreshes and later provider-cost adjustments do not move historical usage
between reporting days.

Sandbox responses also include the creating API-key ID/name and a numeric
`version`. `GET /v1/sandboxes/{id}/detail` returns the tenant-scoped creation
attempts, executions, lifecycle cost, and provider-observation history. A
termination request may send that version in `If-Match`; stale versions are
rejected with `409`, while repeating a successful termination remains
idempotent. `GET /v1/session` reports the current key's scopes and whether the
dashboard may show termination controls.

Organization guardrails are managed with `GET` and `PUT` requests to
`/v1/policies/current`. They can cap concurrent sandboxes, rolling-hour and
UTC-day customer spend, allowed backends, allowed GPU types, and requested
sandbox lifetime. Each create request reserves its concurrency slot and checks
all limits in one database transaction before any provider call. A denial is a
stable, non-retryable `policy_denied` response and is attributed to the API key
that made the request. `GET /v1/policy-summary` returns current quota/spend and
recent denials. Redacted provider health is available from `GET /v1/providers`
and `POST /v1/providers/{backend}/health-check`.

An explicit creation `timeout_secs` also sets the control plane's `expires_at`
on every backend, independently of the provider's own timeout semantics. Omitted
values do not create a supervisor deadline, even when a preset supplies a
recommended timeout. The control-plane supervisor terminates expired sandboxes
even when the client disconnects. Its durable claim state is safe across process
restarts, and configured provider terminators can clean up resources whose
in-memory runtime was lost. Reconciliation watchdogs also remove provider resources that do not
belong to any recorded sandbox, with durable idempotency so a confirmed cleanup
is not repeated. Set `BESPOKE_SUPERVISION_INTERVAL_SECS` to tune the scan
interval. These restart-recovery and reconciliation behaviors require injected
terminator/reconciler adapters; the stock CLI does not supply them.

### 5. Inspect usage in the dashboard

Open [`http://127.0.0.1:8000/dashboard`](http://127.0.0.1:8000/dashboard) and
enter the `bsk_live_...` product key. For this plain-HTTP loopback example,
set `BESPOKE_SESSION_COOKIE_SECURE=0` before starting the server so the browser
can send its session cookie. Keep the default `1` when serving over HTTPS.
The initial organization key has every scope. A read-only dashboard key needs
both `usage:read` and `sandboxes:read`.

The dashboard provides:

- operational cards for active resources, failed launches, unreconciled spend,
  and resources nearing their TTL;
- budget/quota progress, backend/GPU/lifetime policy, provider health, and
  recent policy denials when the key has the corresponding read scopes;
- launches for keys with `sandboxes:create` (choose an enabled backend and
  clear the form's `dashboard-cpu` preset default unless your deployment has
  registered that custom preset);
- status, provider, and creation-date filtering with client-side pagination;
- creator key, provider resource, compute/GPU, rate, age/TTL, cleanup, and
  reconciliation state for every sandbox;
- a detail view for lifecycle, attempts, executions, errors, cost, and provider
  observations;
- confirmed, revision-safe termination for keys with `sandboxes:terminate`;
  legacy `sandboxes:write` keys remain compatible; and
- automatic refresh every 15 seconds while the tab is visible, plus manual refresh.

The production dashboard exchanges the product key for an expiring, HTTP-only,
Secure, SameSite=Strict cookie and never stores that key in browser storage.
State-changing requests carry a session-bound CSRF token. For an explicitly
enabled local-only workflow, set `BESPOKE_ENABLE_LOCAL_DASHBOARD_LOGIN=1`, set
`BESPOKE_SESSION_COOKIE_SECURE=0` only when serving plain HTTP on loopback, and
open `/dashboard/local`; that development route keeps the key in the current
tab's `sessionStorage`. Never expose that route on a shared network. Local
sandboxes record runtime but have a provider cost of `$0`; use a priced fake or
cloud backend to exercise spend counters.

The Activity view shows durable alerts and append-only audit history. Operators
with `exports:read` can download paginated CSV pages for usage, lifecycle costs,
and ledger entries. Dashboard export buttons download the first page of up to
500 rows; full exports require following the HTTP `X-Next-Cursor` header as
described in the [API contract](docs/CONTROL_PLANE_API.md#pagination-and-csv).
Alert thresholds and retention periods are tenant-owned and available through
the API. Retention defaults to 90 days for operational history and 365 days for
audit; an explicit retention request preserves usage and financial ledgers.

### Security and deployment notes

API keys, browser session cookies, and CSRF tokens are HMAC-hashed at rest.
Runtime scopes are `sandboxes:read`,
`sandboxes:create`, `sandboxes:execute`, and `sandboxes:terminate`; governance
uses `usage:read`, `policies:read`, `policies:write`, `providers:read`, and
`providers:write`; reconciliation uses `providers:reconcile`; and key issuance
uses `keys:write`. Operational access uses `alerts:read`, `alerts:write`,
`audit:read`, `exports:read`, `retention:read`, and `retention:write`. The legacy
`sandboxes:write` scope grants create, execute, terminate, and provider
reconciliation for compatibility. Runtime events and provider/customer costs
are written to an idempotent ledger. All responses carry a restrictive content
security policy and browser security headers. See the
[operations runbook](docs/CONTROL_PLANE_OPERATIONS.md) and
[hosted API contract](docs/CONTROL_PLANE_API.md) before deployment.

Never commit the API-key pepper, admin token, provider credentials, or product
keys. Revoke and replace a product key if it appears in source code, logs, or
chat. Environment-variable ownership is:

| Value | Where it belongs |
|---|---|
| `E2B_API_KEY` and other provider credentials | Control-plane server only |
| `BESPOKE_API_KEY_PEPPER` | Control-plane server only; stable across restarts |
| `BESPOKE_CONTROL_PLANE_ADMIN_TOKEN` | Administrative bootstrap tooling only |
| `bsk_live_...` / `BESPOKE_SANDBOX_KEY` | Customer client or dashboard |

Common errors:

| Error | Resolution |
|---|---|
| `backend is not enabled: e2b` | Add `e2b` to `BESPOKE_ALLOWED_BACKENDS` and restart the server. |
| `invalid API key` | Confirm the full `bsk_live_...` value, database path, and original pepper. |
| Provider authentication failure | Install that provider's extra and configure its credential on the server. |

## API Reference

### Creating a Sandbox

```python
from bespokelabs.sandbox import Sandbox

sb = Sandbox(
    "local",              # "local" | "safehouse" | "docker" | "ray" | "daytona" | "tensorlake" | "modal" | "runpod" | "e2b"
    preset=None,          # Preset name or SandboxPreset object
    cpu=1.0,              # vCPUs (Tensorlake, Modal, Docker, Daytona, RunPod)
    memory_mb=1024,       # RAM in MB (Tensorlake, Modal, Docker, Daytona, RunPod)
    disk_mb=None,         # Disk in MB (Daytona, RunPod)
    gpu=None,             # GPU type/count (Modal, RunPod)
    timeout_secs=None,    # Omitted: preset default, otherwise 600 seconds
    image=None,           # OCI image, or Tensorlake project image name
    template=None,        # Template ID (E2B, RunPod)
    env_vars=None,        # dict of environment variables
    allow_internet=True,  # Mapped by Docker and Tensorlake; rejected if false on RunPod
    app_name=None,        # App name (Modal)
    snapshot_id=None,     # Restore from snapshot (Tensorlake, Modal, Daytona)
    workdir=None,         # Sandbox root or command working directory
    backend_options=None, # dict merged into the backend's native create call
    files=None,           # {path: bytes|str} written into the sandbox on create
    git_repo=None,        # repo URL cloned into the sandbox on create
    git_ref=None,         # branch/tag for git_repo
    workspace=None,       # Manifest of files/dirs/repos to materialize on create
)
```

Not every backend uses every parameter. Most unsupported parameters are
ignored; a backend rejects values it cannot safely honor, such as network
isolation on RunPod.

Modal GPU sandboxes accept Modal's GPU reservation strings, including a GPU
type such as `gpu="L4"` or a type and count such as `gpu="H100:2"`:

```python
with Sandbox("modal", gpu="A100") as sb:
    result = sb.execute_command("nvidia-smi")
    print(result.stdout)
```

GPU sandboxes can be preempted, so GPU workloads should tolerate interruption.

RunPod uses exact GPU type IDs and requires a GPU selection. Its official
PyTorch image is used by default because it includes SSH support:

```python
with Sandbox(
    "runpod",
    gpu="NVIDIA H100 80GB HBM3:2",
    backend_options={"ssh_private_key_path": "~/.ssh/id_ed25519"},
) as sb:
    result = sb.execute_command("nvidia-smi")
    print(result.stdout)
```

For capacity fallback, pass RunPod's native creation fields through
`backend_options`, such as a `gpuTypeIds` list containing `"NVIDIA L40S"` and
`"NVIDIA RTX A6000"`, with `gpuTypePriority="availability"`. Custom images and
templates must run an SSH daemon on port 22. Add the matching public key to the
RunPod account before creating a sandbox. `ssh_private_key_path` is optional
when the key is already discoverable by OpenSSH.

`timeout_secs` is a command timeout on Local, Safehouse, Docker, Ray, and
RunPod. E2B and Modal pass it as the provider sandbox timeout; Tensorlake passes
it to `create_and_connect`. Daytona maps an explicit value to a wall-clock
`ttl_minutes` deadline, rounded up to at least one minute.
RunPod creation has a separate 10-minute default readiness timeout, adjustable
with `backend_options={"create_timeout_secs": ...}`. Daytona
destroys the sandbox when that deadline elapses in whatever state it is in,
running work included; no activity resets the clock, and the clock starts at
creation, so image pull and boot count against it. The mapping happens only
when you pass `timeout_secs` yourself. Omit it — including when a preset
supplies its own recommended value — and the Daytona sandbox gets no TTL at
all, leaving it bounded only by Daytona's default 15-minute idle auto-stop.

Constructing a `Sandbox` creates the underlying sandbox immediately. To launch
many sandboxes on one backend, or to use `async`/`await`, see
[Reusing a client across many sandboxes](#reusing-a-client-across-many-sandboxes)
and [Async](#async).

### Executing Code

```python
result = sb.execute_code('print(1 + 1)', language="python")

print(result.stdout)     # "2"
print(result.stderr)     # ""
print(result.exit_code)  # 0
```

`language` defaults to `"python"`. The Daytona and E2B adapters ignore this
argument and call their Python code endpoints. Other adapters invoke an
installed interpreter as `<language> -c <code>`; for runtimes with different
flags, use `execute_command` explicitly.

By default, inspect `exit_code` on the returned `SandboxResult`; a nonzero
exit is not uniformly converted into an exception. Local, Safehouse, Docker,
and Ray return exit code `124` on execution timeout. Provider/transport
failures can instead raise `SandboxError` subclasses.

### Running Shell Commands

```python
result = sb.execute_command("ls -la /tmp")
result = sb.execute_command("grep", args=["-r", "TODO", "/app"])
```

### Structured output

Pass `return_type` to `execute_code` or `execute_command` to parse stdout into
a Pydantic model, dataclass, or class accepting keyword arguments:

```python
from pydantic import BaseModel
from bespokelabs.sandbox import Sandbox

class Stats(BaseModel):
    count: int
    mean: float

with Sandbox("local") as sb:
    stats = sb.execute_code(
        'import json; print(json.dumps(dict(count=3, mean=2.0)))',
        return_type=Stats,
    )
    print(stats.count, stats.mean)

# Parse existing text, including a downloaded file's decoded contents.
stats = Sandbox.parse_as('{"count": 3, "mean": 2.0}', Stats)
```

Parsing accepts JSON objects, including objects in Markdown fences or
surrounding text. It raises `CommandFailedError` if execution exited nonzero,
and `SandboxExecutionError` if no object can be parsed or constructed.
`json_schema(Stats)` generates a prompt instruction; for command calls,
`inject_schema=True` appends that instruction to the last item in `args` when
`return_type` is supplied. These helpers do not force the command to emit JSON.

### File Operations

```python
# List files
files = sb.list_files("/home")
for f in files:
    print(f.path, f.is_dir, f.size)

# Read / write in-memory content
sb.write_file("/tmp/config.json", '{"key": "value"}')
data = sb.read_file("/tmp/config.json")  # returns bytes

# Upload a local file into the sandbox
sb.upload_file("./local_data.csv", "/home/user/data.csv")

# Download a file from the sandbox to local disk
sb.download_file("/home/user/results.json", "./results.json")

# Move a whole directory tree in or out (preserves structure + executable bits).
# Defaults to a single tar.gz transfer, falling back to a per-file loop.
sb.upload_dir("~/.claude/skills/my-skill", ".claude/skills/my-skill")
sb.download_dir("/workspace/output", "./results")
```

> Directory transfer is built on the single-file primitives above, so it works
> on every backend with the required shell tools. To seed a tree during
> creation, use `build_files_map(local, remote)` with
> `Sandbox(files=...)`, or a `Manifest` containing `LocalDir`.
> See [`examples/move_files_into_sandbox.py`](examples/move_files_into_sandbox.py).

Directory methods return the number of transferred files. Uploads skip symlinks
and empty directories. `method="auto"` uses tar when available, otherwise
`"per_file"`; the per-file download fallback does not preserve executable bits.
`build_files_map` and `files=` carry contents only. Some provider file APIs
require parent directories to exist, so create them before writing nested paths.
On macOS, `COPYFILE_DISABLE=1` avoids AppleDouble metadata files in local tar
downloads.

### Declarative workspaces

`files=`, `git_repo=`, and `upload_dir()` cover the imperative cases. For
anything richer, pass a `Manifest` — a typed, ordered map of *destination →
source* materialized at creation (built on the primitives above, so it works on
every backend):

```python
from bespokelabs.sandbox import Sandbox, Manifest, GitRepo, LocalDir, LocalFile, File

with Sandbox("daytona", workspace=Manifest(entries={
    "repo": GitRepo("https://github.com/org/proj", ref="main"),
    ".claude/skills/pirate": LocalDir("~/.claude/skills/talk-like-a-pirate"),
    "data/seed.csv": LocalFile("./seed.csv"),
    "config.json": File('{"env": "prod"}'),
})) as sb:
    ...
```

Default creation order is `git_repo` clone → `files` → `workspace` entries →
preset setup (when needed). OpenCode opts into setup before workspace
materialization so npm and Git are ready before cloning. All of this happens after the provider resource exists;
setup/materialization failure triggers a cleanup attempt. Entries materialize
in insertion order, so a later one overlays an earlier one.
`GitRepo` clones, `LocalDir` / `LocalFile` upload (preserving the executable
bit), and `File` writes in-memory content. Subclass `WorkspaceEntry` for custom
sources, or call `manifest.apply(sb)` on a live sandbox.

### Errors

SDK operation errors use `SandboxError` subclasses carrying a machine-readable
`code`, the `backend` and `op` when supplied, a `retryable` flag, `outcome`, and
a `context` dict. Branch on these fields instead of parsing message strings:

```python
from bespokelabs.sandbox import Sandbox, SandboxError

try:
    with Sandbox("daytona") as sb:
        sb.execute_command("…")
except SandboxError as e:
    print(e.code, e.backend, e.op, e.retryable, e.outcome, e.context)
    # Decide whether retrying this operation is safe before repeating it.
```

Subtypes include `SandboxConfigurationError`, `SandboxCreationError`,
`CommandFailedError` (with `exit_code` / `stdout` / `stderr`),
`SandboxTimeoutError` and `SandboxConnectionError` (both `retryable`),
`SandboxAuthenticationError`, `SandboxNotFoundError`, `WorkspaceError`,
`BackendNotInstalledError`, and `FeatureNotSupportedError`. Invalid helper
arguments can still raise standard Python exceptions, such as `KeyError` for
an unknown preset. `RemoteSandboxError` is a separate HTTP-client exception
and does not inherit from `SandboxError`.

A retryable transport failure alone does not prove that repeating a create or
command is safe. Check `outcome` and cleanup state: `"unknown"` can mean the
provider performed the operation but its response was lost.

### Agent-ready sandboxes

Sandboxes can also be bound to agents without replacing the low-level sandbox
API. Agent placement is explicit:

- `inside`: the agent process runs inside the sandbox, useful for CLI agents
  such as Codex CLI, Claude Code, or a custom inference runner.
- `outside`: the agent process runs outside the sandbox and drives it through
  capability-checked sandbox tools.

Inside-sandbox agent:

```python
from bespokelabs.sandbox import AgentSpec, Sandbox

with Sandbox(
    "docker",
    preset="codex",
    git_repo="https://github.com/bespokelabsai/sandbox",
) as sb:
    agent = sb.agent(AgentSpec.inside(
        name="codex",
        command=["codex", "exec"],
        cwd="sandbox",
    ))

    result = agent.run("Run the eval suite and summarize failures")
    print(result.stdout)
```

Outside-driver agent:

```python
from bespokelabs.sandbox import AgentSpec, Sandbox

def run_eval(ctx, prompt: str) -> str:
    ctx.write_file("/workspace/task.txt", prompt)
    result = ctx.shell("python3", ["/workspace/eval.py"])
    return result.stdout

with Sandbox(
    "docker",
    files={"/workspace/eval.py": "print('ok')"},
) as sb:
    agent = sb.agent(AgentSpec.outside(
        name="eval-runner",
        capabilities=["shell", "files"],
        runner=run_eval,
    ))

    print(agent.run("Evaluate this input"))
```

For a complete Claude Code outside-sandbox example, see
[`examples/claude_code_outside.py`](examples/claude_code_outside.py). It starts
Claude Code on the host, clones a repository into a sandbox with `git_repo=...`,
and gives Claude a localhost bridge for running commands inside the sandbox. The
example asks Claude to count regular files directly in the cloned repository
root.

For outside agent frameworks, use `agent_tools(...)` directly:

```python
with Sandbox("docker") as sb:
    tools = sb.agent_tools(capabilities=["shell", "files", "patch"])
    tools.write_file("/workspace/input.txt", "hello")
    print(tools.shell("cat", ["/workspace/input.txt"]).stdout)
```

The generic context exposes `shell`, `files`, and `patch` operations. `ports`
and `artifacts` are accepted capability names but have no operations yet.
Capabilities gate context methods, not the sandbox OS: `shell` can access files,
and the runner can access the underlying sandbox through `ctx.sandbox`.

Inside agents accept `input_mode="stdin"` (default), `"argv"` (append the
prompt), `"file"` (write the prompt and append its path), or `"none"`. They
return `SandboxResult`; outside agents return their runner's value. Install and
authenticate the agent CLI separately from provider authentication. Presets
install tools but do not supply agent credentials.

### Token usage & cost

To run GLM through the [OpenCode harness](https://opencode.ai/docs/cli/#run):

```python
import os
from bespokelabs.sandbox import Sandbox

with Sandbox(
    "local", preset="opencode",
    env_vars={"ZHIPU_API_KEY": os.environ["ZHIPU_API_KEY"]},
) as sb:
    result = sb.run_agent(
        "Review this project.", harness="opencode", model="zai/glm-4.7",
    )
    print(result.text)
    print(result.usage.total_tokens, result.usage.total_cost_usd)
```

For a [Z.AI Coding Plan](https://opencode.ai/docs/providers/#zai), use
`model="zai-coding-plan/glm-4.7"` with the same environment variable. Other
GLM models can be selected using their OpenCode `provider/model` identifier.
`resume=True` continues the latest conversation in the same workspace;
`extra_args=["--session", session_id]` selects a specific session.
The preset installs OpenCode at startup, requiring npm and install permissions
on the sandbox; its Docker setup installs npm and Git on the default Debian image
before cloning repositories. OpenCode opts into `setup_before_workspace=True`;
other presets retain setup after workspace materialization by default.
No prebuilt OpenCode image is assumed. The preset installs the CLI; select the
harness separately with `harness="opencode"` when calling `run_agent`.
OpenCode JSON step costs and token counts are summed across the run, including
cache usage and reasoning tokens (included in `output_tokens`). Original events
are available in `result.raw["events"]`; stdout, stderr and exit status are
preserved. See [the runnable GLM example](examples/opencode_glm.py). Its local
workspace defaults to the Git-ignored `examples/.sandbox_workdir/opencode_glm`
directory and survives cleanup, so `--resume` works across invocations. Use
`--workdir` to override it. Recreating a cloud sandbox does not retain its files.

`run_agent(...)` runs Claude Code on a prompt and reports what the run cost.
It drives the CLI with JSON output under the hood, so it can return both the
assistant's text answer and a `Usage` breakdown — LLM token counts and dollar
cost (from Claude Code's own `total_cost_usd`), plus an estimate of the sandbox
compute the call consumed (elapsed wall-clock × the backend's per-second price).

```python
with Sandbox("local", preset="claude-code") as sb:
    result = sb.run_agent("Review /code.py and suggest fixes.")
    print(result.text)                       # the assistant's answer

    u = result.usage                         # usage for this call
    print(u.input_tokens, u.output_tokens)   # token counts
    print(u.llm_cost_usd)                    # tokens, in dollars
    print(u.compute_cost_usd)                # sandbox compute, in dollars
    print(u.total_cost_usd)                  # llm + compute

    # Continue the conversation; usage accumulates on the sandbox.
    sb.run_agent("Now apply those fixes.", resume=True)
    print(sb.usage.total_cost_usd)           # running total across both calls
    print(sb.usage.total_tokens)
```

`sb.usage` is the running total across every `run_agent` call in the sandbox's
lifetime (its compute component sums the agent-call durations, not idle time
between calls). Pass extra CLI flags with `extra_args=[...]`. Claude Code and
OpenCode are supported; token counts and `llm_cost_usd` reflect what the CLI's
JSON output reports, so `llm_cost_usd` can be `0` under subscription auth that
omits `total_cost_usd` (the token counts are still captured). The async
`AsyncSandbox` exposes the same `run_agent(...)` / `usage`. This is distinct
from `bespokelabs.sandbox.pricing`, which prices sandbox compute on its own.

For standalone sandbox compute estimates, use the bundled pricing helpers:

```python
from bespokelabs.sandbox.pricing import cost_per_second, get_backend_pricing

print(get_backend_pricing("modal"))          # raw bundled pricing metadata
print(cost_per_second("modal", vcpu=2.0))    # estimated $/sec for a sandbox
```

`sb.estimate_compute_cost(elapsed_secs)` estimates a measured interval using
the live session's hourly rate when available (RunPod), otherwise bundled
CPU/RAM rates. Bundled estimates omit GPU, disk, network, and other charges;
an unknown backend also returns zero, which does not establish that it is free.
Direct SDK `sb.usage` tracks only `run_agent` calls. Remote execution usage
tracks each command's runtime, while hosted cost summaries use lifecycle costs
when available, including idle time, without adding execution costs twice.

Pricing data is best-effort and local to the installed package. To compare
available backends for a real workload, see
[`examples/find_cheapest.py`](examples/find_cheapest.py), which benchmarks
cold-start and execution time, then estimates cost with the same pricing data.

### Presets

Presets are predefined sandbox configurations with setup commands that run after creation.
The built-in presets are focused on agent CLIs: `codex`, `claude-code`, `claude-code-codex`, and `opencode`.
The npm-based setup commands require Node.js and `npm` in the execution environment when setup commands are used as a fallback.

#### Prebuilt Preset Images

Built-in presets have prebuilt OCI images published to GitHub Container Registry:

```text
ghcr.io/bespokelabsai/sandbox/<preset>:v2
```

Docker, Daytona, and Modal use these images automatically when you pass a preset, then skip the preset's setup commands because the tools are already baked into the image.

The main advantage is that prebuilt images move setup work from sandbox startup time to image build time:

- Sandboxes start faster because they do not reinstall the same tools for every run.
- Startup is more reliable because it depends less on package registry availability during sandbox creation.
- Preset environments are more reproducible because images use pinned tags instead of a moving `latest` tag.

For example, this Docker sandbox starts from `ghcr.io/bespokelabsai/sandbox/codex:v2` and does not run `npm install -g @openai/codex` at startup:

```python
with Sandbox("docker", preset="codex") as sb:
    sb.execute_command("codex --version")
```

You can still override the image explicitly when you need a custom base image:

```python
with Sandbox("docker", preset="codex", image="my-registry/codex-tools:v3") as sb:
    sb.execute_command("codex --version")
```

An explicit image different from the preset image makes setup commands run
again, so that custom image must support the preset's installation commands.
On Local and Ray, those commands run on the host/worker and can install global
npm packages. Use `Sandbox.list_presets()` to inspect registered defaults.

The Dockerfiles live under `images/<preset>/`. Local, Safehouse, Ray, and other
backends that cannot use the prebuilt image still fall back to the preset setup
commands. RunPod deliberately does this on its SSH-ready default image.
Tensorlake image names are project-scoped, so you can build/register equivalent
images from the same Dockerfiles when you need Tensorlake-specific preset
images.

```python
# Sandbox with Codex CLI installed
with Sandbox("docker", preset="codex") as sb:
    sb.execute_command("codex --version")

# Sandbox with Claude Code installed
with Sandbox("docker", preset="claude-code") as sb:
    sb.execute_command("claude --version")

# Sandbox with both Claude Code and Codex CLI installed
with Sandbox("docker", preset="claude-code-codex") as sb:
    sb.execute_command("claude --version")
    sb.execute_command("codex --version")
```

Built-in presets:

| Preset | What it installs | Defaults |
|---|---|---|
| `claude-code` | `@anthropic-ai/claude-code` via npm | 2GB RAM, 30min timeout |
| `claude-code-codex` | `@anthropic-ai/claude-code` and `@openai/codex` via npm | 2GB RAM, 30min timeout |
| `codex` | `@openai/codex` via npm | 2GB RAM, 30min timeout |
| `opencode` | `opencode-ai` via npm | 2GB RAM, 30min timeout |

#### Non-interactive web access

The preset controls which CLI is installed in the sandbox. It does not grant
the CLI permission to use its own web tools. When you run an agent inside a
remote sandbox such as Daytona, preconfigure the CLI for non-interactive runs
instead of waiting for an in-terminal approval prompt.

For Claude Code, `WebFetch` and `WebSearch` are permission-gated tools. Use
`--permission-mode dontAsk` with the narrowest `--allowedTools` entries that
fit the task:

```python
agent = sb.agent(AgentSpec.inside(
    name="claude",
    command=[
        "claude",
        "-p",
        "--permission-mode",
        "dontAsk",
        "--allowedTools",
        "WebFetch(domain:github.com)",
        "WebSearch",
    ],
    input_mode="argv",
))

result = agent.run("Summarize https://github.com/bespokelabsai/sandbox")
```

For Codex CLI, use `codex exec` with explicit approval, sandbox, and search
settings. For read-only website summaries, keep the Codex sandbox read-only,
disable approval prompts, and enable live search:

```python
agent = sb.agent(AgentSpec.inside(
    name="codex",
    command=[
        "codex",
        "exec",
        "--ask-for-approval",
        "never",
        "--sandbox",
        "read-only",
        "--search",
    ],
    input_mode="argv",
))

result = agent.run("Summarize https://github.com/bespokelabsai/sandbox")
```

Use broader modes only when the outer sandbox is the trust boundary. For
example, `codex exec --sandbox workspace-write --ask-for-approval never` lets
Codex edit files without pausing, and Claude Code's `--permission-mode
bypassPermissions` skips most permission prompts. Those modes are best kept to
isolated sandboxes with scoped credentials.

Create your own:

```python
from bespokelabs.sandbox import Sandbox, SandboxPreset

Sandbox.register_preset(SandboxPreset(
    name="my-stack",
    description="My custom environment",
    setup_commands=["pip install my-library", "npm install -g my-tool"],
    cpu=2.0,
    memory_mb=4096,
))

with Sandbox("docker", preset="my-stack") as sb:
    ...
```

Explicit kwargs always override preset defaults.

### Workspace convenience arguments

Populate the sandbox at creation instead of scripting uploads afterwards.
`files` are written after any `git_repo` clone. Preset setup normally follows
workspace materialization; OpenCode runs setup first to install its prerequisites.

`files` needs nothing special and works on every backend and image:

```python
with Sandbox(
    "docker",
    image="python:3.12-slim",
    files={"/work/run.py": "print('ready')"},
) as sb:
    print(sb.execute_command("python", ["/work/run.py"]).stdout)
```

`files` values may be `str` or `bytes`.

`git_repo` runs `git clone` **inside** the sandbox, so `git` must be present in
the image. All prebuilt preset images include `git`, so `git_repo` works with
any preset on Docker/Daytona/Modal. For a custom `image`, make sure `git` is
installed; the host-based backends (`local`, `safehouse`) use the host's `git`.

```python
with Sandbox(
    "local",
    git_repo="https://github.com/psf/requests",
    git_ref="main",                      # optional branch/tag
) as sb:
    entries = sb.list_files("/requests")  # local workspace root / repo name
    print(f"cloned {len(entries)} entries")
```

`git_repo=` clones into a relative `<repo-name>` under the command working
directory; the location is not always `/<repo-name>` on cloud backends. Use
`Manifest(entries={"/explicit/path": GitRepo(...)})` to choose a destination.
For Local and Safehouse, `workdir` is a host directory: an explicit directory
survives `destroy()`, while an automatically created temporary directory is
removed. Daytona and Tensorlake use `workdir` for shell commands only; their
code and file APIs keep provider path semantics. RunPod defaults command
execution to `/workspace`, while its file helpers use SSH path semantics.
Docker, Modal, E2B, and Ray do not map the top-level `workdir` option.

### Backend-specific options

`backend_options` is an escape hatch: the dict is merged into the backend's
native creation call, so you can reach provider features the unified API
doesn't model — without waiting for a new keyword. It is forwarded to Docker
`containers.run`, Modal `Sandbox.create`, E2B `Sandbox.create`, Tensorlake
`create_and_connect`, Daytona's create params, and the RunPod REST creation
payload; ignored by local, safehouse, and ray.

```python
# e.g. set the container hostname (a Docker-only knob)
with Sandbox("docker", backend_options={"hostname": "build-box"}) as sb:
    sb.execute_command("hostname")
```

On Daytona, `env_vars` given here is merged over the `env_vars=` parameter
rather than replacing it, and `create_timeout` (seconds) is consumed by the SDK
adapter to bound the create call itself instead of being passed as a sandbox
parameter.

On RunPod, transport-only options are consumed by the adapter rather than sent
to the REST API: `create_timeout_secs`, `api_timeout_secs`,
`poll_interval_secs`, `ssh_private_key_path`, `ssh_user`,
`ssh_connect_timeout_secs`, `ssh_strict_host_key_checking`, and
`ssh_known_hosts_file`. Other keys use RunPod's native camelCase field names.

### Session state (resume)

A **snapshot** saves state to restore later; **session state** is a
lightweight, serializable handle that reattaches to a sandbox that is *still
running* — including from another process or machine:

```python
sb = Sandbox("e2b", timeout_secs=600)
sb.execute_command("echo hi > /tmp/work.txt")

state = sb.session_state()
blob = state.to_json()          # JSON-safe; stash in a queue/DB/file
# ... do NOT destroy sb — the sandbox must stay alive to reattach ...

# Elsewhere — another worker, another process:
from bespokelabs.sandbox import Sandbox, SandboxSessionState

sb2 = Sandbox.resume(SandboxSessionState.from_json(blob))
print(sb2.read_file("/tmp/work.txt"))   # b"hi\n"
```

`SandboxClient("e2b").resume(state)` is equivalent; an existing client can
reuse its provider connection.
Resume skips preset setup and workspace materialization. Provider credentials
must be available in the resuming process; host-directory sessions require the
same accessible directory. Session state is not a complete configuration or
usage checkpoint: wrapper configuration and cumulative agent usage reset on
resume. Local/Safehouse state includes the supplied environment overlay, which
may contain secrets. Both attached handles refer to the same resource, so
destroying either affects the other.

| Backend | Resume by | session_state payload |
|---|---|---|
| Docker | container id (`containers.get`) | `container_id` |
| E2B | sandbox id (`Sandbox.connect`) | `sandbox_id` |
| Modal | sandbox id (`Sandbox.from_id`) | `sandbox_id` |
| Tensorlake | sandbox id (`client.connect`) | `sandbox_id` |
| Daytona | sandbox id (`client.get`) | `sandbox_id`, `workdir` (when set) |
| RunPod | Pod id (`GET /pods/{id}`) | `pod_id`, workdir and SSH transport settings |
| Local, Safehouse | host workdir | `workdir`, env overlay |
| Ray | — (not supported) | raises `FeatureNotSupportedError` |

### Snapshots

```python
snap = sb.snapshot()
print(snap.snapshot_id)

# Restore later
sb2 = Sandbox("tensorlake", snapshot_id=snap.snapshot_id)
```

| Backend | Snapshot support |
|---|---|
| Docker | Yes (`container.commit()`) |
| Tensorlake | Yes (filesystem + memory) |
| Modal | Yes (filesystem) |
| Daytona, E2B, Local, Ray, RunPod, Safehouse | No |

Restore a Docker snapshot using `image=snap.snapshot_id`; Docker does not map
`snapshot_id`. Modal and Tensorlake accept `snapshot_id`. Daytona can create
from an existing provider snapshot via `snapshot_id`, although this adapter
cannot create snapshots with `snapshot()`.

### Lifecycle

```python
# Context manager (recommended) — auto-destroys on exit
with Sandbox("local") as sb:
    sb.execute_code("print('hi')")

# Manual cleanup
sb = Sandbox("docker")
sb.execute_code("print('hi')")
sb.destroy()

# Check state
sb.is_alive       # True/False
sb.backend_name   # "docker"
sb.provider_resource_id  # provider ID, or None for local-style backends
```

`is_alive` tracks whether this wrapper has been destroyed; it does not poll the
provider or detect external termination. Cleanup behavior varies by adapter:
some suppress provider deletion errors. Use provider inventory or configured
control-plane reconciliation when you need confirmation of resource removal.

### Reusing a client across many sandboxes

`Sandbox(backend, ...)` builds a fresh provider connection per sandbox. When
launching many sandboxes on one backend, create a `SandboxClient` once and
reuse it — provider-level state (the Docker daemon connection, Daytona auth,
the Ray runtime) is shared across `create()` calls:

```python
from bespokelabs.sandbox import SandboxClient

client = SandboxClient("docker")

for task in tasks:
    with client.create(image="python:3.12-slim") as sb:
        sb.execute_code(task)
```

`client.create(...)` accepts the same keyword arguments as `Sandbox(...)`
and returns a regular `Sandbox` session. `SandboxClient(backend)` validates
the backend name and SDK availability up front — it raises
`BackendNotInstalledError` immediately if the backend's extra isn't
installed — but performs no network I/O until `create()`.

### Async

`AsyncSandboxClient` / `AsyncSandbox` mirror the sync API with coroutine
methods, so you can create and drive many sandboxes concurrently from one
event loop:

```python
import asyncio
from bespokelabs.sandbox import AsyncSandbox, AsyncSandboxClient

async def run_snippet(client: AsyncSandboxClient, code: str) -> str:
    async with await client.create() as sb:
        result = await sb.execute_code(code)
        return result.stdout

async def main():
    client = AsyncSandboxClient("local")
    snippets = ["print(1 + 1)", "print(2 + 2)"]
    outputs = await asyncio.gather(*(run_snippet(client, c) for c in snippets))
    print(outputs)

asyncio.run(main())
```

One-step creation works too: `sb = await AsyncSandbox.create("local")`.
Use one `AsyncSandboxClient` per event loop. Execution, files, `run_agent`,
snapshots, resume, and destruction are awaitable; `session_state()` and
properties remain synchronous. The wrapper does not expose `agent()` or
`agent_tools()`.

Backend SDKs are synchronous, so async calls are offloaded to worker
threads — the event loop is never blocked. Note that the missing-SDK check
(`BackendNotInstalledError`) surfaces at the first `await client.create()`
rather than at `AsyncSandboxClient(...)` construction, which does no I/O.

## Feature Support Matrix

This table describes the adapters in this repository, not every feature offered
by each provider's native SDK. “Interpreter” means an installed binary that
accepts `-c`; use shell commands for other language runtimes.

| Feature | Local | Safehouse | Docker | Ray | Daytona | Tensorlake | Modal | RunPod | E2B |
|---|---|---|---|---|---|---|---|---|---|
| `execute_code` | Interpreter | Interpreter | Interpreter | Interpreter | Python | Interpreter | Interpreter | Interpreter | Python |
| `execute_command` | Shell | Shell | Shell | Shell | Shell | Shell | Shell | SSH | Shell |
| `list_files` | Native | Native | `find` / `ls` | Native | Native SDK | via `ls` | Native SDK | via SSH | Native SDK |
| `read_file` | Native | Native | `get_archive` | Native | Native SDK | via `cat` | Native SDK | via SSH | Native SDK |
| `write_file` | Native | Native | `put_archive` | Native | Native SDK | via base64 | Native SDK | via SSH | Native SDK |
| `upload_file` | `shutil.copy` | `shutil.copy` | `put_archive` | Actor RPC | Native SDK | via base64 | Native SDK | via SSH | Native SDK |
| `download_file` | `shutil.copy` | `shutil.copy` | `get_archive` | Actor RPC | Native SDK | via base64 | Native SDK | via SSH | Native SDK |
| `snapshot` | No | No | Yes | No | No | Yes | Yes | No | No |
| Resource limits | No | No | cpu, memory | cpu (Ray) | cpu, memory, disk | cpu, memory | cpu, memory, gpu | cpu, memory, disk, gpu | Tier-based |
| `allow_internet=False` | Ignored | Ignored | Yes | Ignored | Ignored | Yes | Ignored | Rejected | Ignored |
| Isolation | Process-level | macOS `sandbox-exec` | Container | Process | Full VM | Container | Container | Container | Full VM |
| `gpu=` reservation | No | No | No | No | No | No | Yes | Yes | No |
| Needs install | Nothing | `safehouse` CLI | Docker daemon | `ray` | API key | `tl login` | API key | API key + OpenSSH | API key |

Daytona applies CPU/RAM/disk overrides when creating from `image`; the snapshot
creation path uses the snapshot's resources. RunPod CPU/RAM values are minimums
per GPU, not exact allocations. Ray reserves CPU scheduling resources without
requesting GPU or memory limits. Tensorlake currently does not forward the
top-level `env_vars` option; inside-agent `AgentSpec.env` can supply per-command
variables. Its `read_file` converts text stdout to bytes; use `download_file`
for binary data.

## Exceptions

```python
from bespokelabs.sandbox import (
    SandboxError,              # Base class for all errors
    SandboxCreationError,      # Sandbox failed to start
    SandboxExecutionError,     # Code or command execution failed
    BackendNotInstalledError,  # pip package missing for chosen backend
    FeatureNotSupportedError,  # Backend doesn't support this operation
)
```

All exceptions inherit from `SandboxError`, so you can catch broadly or narrowly:

```python
try:
    sb.snapshot()
except FeatureNotSupportedError:
    print("This backend doesn't support snapshots")
except SandboxError as e:
    print(f"Something else went wrong: {e}")
```

## Environment Variables

```bash
# Docker — no auth needed, just a running Docker daemon

# Local — no auth needed

# Ray — optional remote cluster
export RAY_ADDRESS=ray://head-node:10001  # omit for local cluster

# Daytona
export DAYTONA_API_KEY=your_key
export DAYTONA_API_URL=https://app.daytona.io/api   # optional
export DAYTONA_TARGET=us                              # optional

# Tensorlake
export TENSORLAKE_API_KEY=your_key
# Or authenticate via CLI:
tl login

# Modal
export MODAL_TOKEN_ID=your_id
export MODAL_TOKEN_SECRET=your_secret

# RunPod — add the matching public key to your RunPod account
export RUNPOD_API_KEY=your_key

# E2B
export E2B_API_KEY=your_key
```

## Examples

Run examples from a source checkout after installing the package. Each script
lists its own provider and agent prerequisites.

| Example | Demonstrates |
|---|---|
| [move_files_into_sandbox.py](examples/move_files_into_sandbox.py) | Generated local directory, file-map seeding, and live transfers |
| [find_cheapest.py](examples/find_cheapest.py) | Cold-start/execution benchmarks and bundled compute estimates |
| [sandbox_repo.py](examples/sandbox_repo.py) | Claude Code or Codex inside Daytona/Tensorlake, with a clone or web input |
| [claude_code_outside.py](examples/claude_code_outside.py) | Host-side agent driving sandbox commands through a local bridge |
| [claude_code_persona.py](examples/claude_code_persona.py) | Declarative workspace and agent persona files |
| [github_stats.py](examples/github_stats.py) | Parsing agent output into a Pydantic model |

A local transfer demo needs no cloud credentials or agent CLI:

```bash
python examples/move_files_into_sandbox.py --backend local
```

## Development

Use Python 3.11+ to include the server modules and tests:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,server]'

# Unit/API tests using local execution and mocked providers.
python -m pytest -q --ignore=tests/test_regression.py

# Repository formatting and lint configuration.
python -m pyink --check src tests examples
python -m ruff check src tests examples
```

On macOS, prefix the test command with `COPYFILE_DISABLE=1` to prevent the
system tar from adding AppleDouble `._*` files to directory-transfer fixtures:

```bash
COPYFILE_DISABLE=1 python -m pytest -q --ignore=tests/test_regression.py
```

`tests/test_regression.py` probes installed providers at collection time and
can create real resources when SDKs/authentication are available. Run
`python -m pytest -q` when you intend to exercise those integrations. Mocked
Daytona adapter tests also need the `[daytona]` extra and skip without it.
The test suite exercises Python/API behavior and dashboard asset contracts;
`tests/dashboard_browser_app.py` is a separate deterministic browser fixture.

### Codebase map

| Path | Responsibility |
|---|---|
| `src/bespokelabs/sandbox/sandbox.py` | Direct SDK, lifecycle, workspace setup, structured output, agent cost tracking |
| `src/bespokelabs/sandbox/aio.py` | Thread-backed async client and session wrapper |
| `src/bespokelabs/sandbox/backends/` | Nine lazy-loaded provider clients and sessions |
| `src/bespokelabs/sandbox/protocols.py`, `types.py`, `exceptions.py` | Backend contracts and shared public types/errors |
| `src/bespokelabs/sandbox/workspace.py`, `_transfer.py` | Manifest entries and directory transfer strategies |
| `src/bespokelabs/sandbox/agents.py`, `_agent_runtime.py`, `_usage.py` | Agent placement, command preparation, Claude/OpenCode usage parsing |
| `src/bespokelabs/sandbox/presets.py`, `pricing.py`, `pricing.json` | Preset configuration and bundled compute rates |
| `src/bespokelabs/sandbox/remote.py` | Standard-library HTTP client for the control plane |
| `src/bespokelabs/sandbox/control_plane/` | FastAPI routes, tenant service, SQLite migrations/ledgers, supervision, dashboard assets |
| `images/`, `.github/workflows/build-images.yml` | Preset Dockerfiles and image publishing workflow |
| `tests/`, `examples/`, `docs/` | Regression coverage, usage examples, API contract and operations runbook |

See [DEVELOPMENT.md](DEVELOPMENT.md) for design direction and compatibility
rules. It includes proposed integrations as well as implemented concepts;
the current public API is exported by `src/bespokelabs/sandbox/__init__.py`.
For server deployments, read the [operations runbook](docs/CONTROL_PLANE_OPERATIONS.md).

## License

[Apache License 2.0](LICENSE).
