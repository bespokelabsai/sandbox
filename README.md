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

## Why?

- **No lock-in** — Your code works across all backends. Switch providers without rewriting a single line.
- **Easily move between providers** — If one provider has an outage or capacity issue, change one string and keep running.
- **Cost tracking** — Track Claude Code token usage, estimated sandbox compute cost, and compare backend pricing.
- **Automatic scheduling to lowest cost provider** — Let the library route your workloads to the cheapest available backend. *(coming soon)*

## Install

```bash
pip install bespokelabs-sandbox
```

With a specific backend:

```bash
pip install bespokelabs-sandbox[docker]
pip install bespokelabs-sandbox[daytona]
pip install bespokelabs-sandbox[tensorlake]
pip install bespokelabs-sandbox[modal]
pip install bespokelabs-sandbox[e2b]
pip install bespokelabs-sandbox[ray]
pip install bespokelabs-sandbox[all]
```

The RunPod backend has no Python extra; it uses the system OpenSSH client.
The Safehouse backend also has no Python extra. Install its CLI separately on
macOS:

```bash
brew install eugene1g/safehouse/agent-safehouse
```

## Supported Backends

### Local

No API keys, no cloud accounts. Just works.

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
| [Tensorlake](https://tensorlake.ai) | `[tensorlake]` | `tl login` |
| [Modal](https://modal.com) | `[modal]` | `MODAL_TOKEN_ID` + `MODAL_TOKEN_SECRET` |
| [RunPod](https://www.runpod.io) | _(none)_ | `RUNPOD_API_KEY` + registered SSH key |
| [E2B](https://e2b.dev) | `[e2b]` | `E2B_API_KEY` |

You only need to install the backend you use. The others are lazily imported.

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
changing `BESPOKE_ALLOWED_BACKENDS`.

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

The context manager destroys each sandbox automatically. Reusing an
`idempotency_key` on either creation or execution returns the original logical
operation without creating, running, or billing it twice. Reusing a creation
key with a different request is rejected.

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

The control-plane supervisor terminates expired sandboxes even when the client
disconnects. Its durable claim state is safe across process restarts, and
configured provider terminators can clean up resources whose in-memory runtime
was lost. Reconciliation watchdogs also remove provider resources that do not
belong to any recorded sandbox, with durable idempotency so a confirmed cleanup
is not repeated. Set `BESPOKE_SUPERVISION_INTERVAL_SECS` to tune the scan
interval. These restart-recovery and reconciliation behaviors require injected
terminator/reconciler adapters; the stock CLI does not supply them.

### 5. Inspect usage in the dashboard

Open [`http://127.0.0.1:8000/dashboard`](http://127.0.0.1:8000/dashboard) and
enter the `bsk_live_...` product key. The initial organization key has every
scope. A read-only dashboard key needs both `usage:read` and
`sandboxes:read`.

The dashboard provides:

- operational cards for active resources, failed launches, unreconciled spend,
  and resources nearing their TTL;
- budget/quota progress, backend/GPU/lifetime policy, provider health, and
  recent policy denials when the key has the corresponding read scopes;
- status, provider, and creation-date filtering with pagination;
- creator key, provider resource, compute/GPU, rate, age/TTL, cleanup, and
  reconciliation state for every sandbox;
- a detail view for lifecycle, attempts, executions, errors, cost, and provider
  observations;
- confirmed, revision-safe termination for keys with `sandboxes:terminate`;
  legacy `sandboxes:write` keys remain compatible; and
- automatic refresh while the tab is visible, plus manual refresh.

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
and ledger entries. Alert thresholds and retention periods are tenant-owned and
available through the API.

### Security and deployment notes

API keys, browser session cookies, and CSRF tokens are HMAC-hashed at rest.
Runtime scopes are `sandboxes:read`,
`sandboxes:create`, `sandboxes:execute`, and `sandboxes:terminate`; governance
uses `usage:read`, `policies:read`, `policies:write`, `providers:read`, and
`providers:write`; reconciliation uses `providers:reconcile`; and key issuance
uses `keys:write`. Operational access uses `alerts:read`, `alerts:write`,
`audit:read`, `exports:read`, `retention:read`, and `retention:write`. The legacy
`sandboxes:write` scope grants the four sandbox write operations and provider
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
    backend,              # "local" | "safehouse" | "docker" | "ray" | "daytona" | "tensorlake" | "modal" | "runpod" | "e2b"
    *,
    preset=None,          # Preset name or SandboxPreset object
    cpu=1.0,              # vCPUs (Tensorlake, Modal, Docker, Daytona, RunPod)
    memory_mb=1024,       # RAM in MB (Tensorlake, Modal, Docker, Daytona, RunPod)
    disk_mb=None,         # Disk in MB (Daytona, RunPod)
    gpu=None,             # GPU type/count (Modal, RunPod)
    timeout_secs=600,     # Max lifetime / subprocess timeout
    image=None,           # Container image (Docker, Modal, Daytona, RunPod)
    template=None,        # Template ID (E2B, RunPod)
    env_vars=None,        # dict of environment variables
    allow_internet=True,  # Network access (Docker, Tensorlake, Daytona)
    app_name=None,        # App name (Modal)
    snapshot_id=None,     # Restore from snapshot (Tensorlake, Modal)
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

`timeout_secs` is a command timeout on Local, Ray, and RunPod; a sandbox
timeout on E2B and Modal; and on Daytona a wall-clock `ttl_minutes` deadline.
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

`language` defaults to `"python"`. Daytona also supports `"typescript"`, `"javascript"`, `"ruby"`, and `"go"`. Safehouse, Docker, Tensorlake, Modal, Local, and Ray accept any installed binary name.

### Running Shell Commands

```python
result = sb.execute_command("ls -la /tmp")
result = sb.execute_command("grep", args=["-r", "TODO", "/app"])
```

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
> on every backend. To seed a tree *before* the sandbox boots (e.g. so preset
> setup sees it), use `build_files_map(local, remote)` with `Sandbox(files=...)`.
> See [`examples/move_files_into_sandbox.py`](examples/move_files_into_sandbox.py).

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

Entries materialize in insertion order, so a later one overlays an earlier one.
`GitRepo` clones, `LocalDir` / `LocalFile` upload (preserving the executable
bit), and `File` writes in-memory content. Subclass `WorkspaceEntry` for custom
sources, or call `manifest.apply(sb)` on a live sandbox.

### Errors

Every failure is a `SandboxError` subclass carrying a machine-readable `code`,
the `backend` and `op` in flight, a `retryable` flag, and a `context` dict — so
you can branch on it instead of parsing message strings:

```python
from bespokelabs.sandbox import Sandbox, SandboxError

try:
    with Sandbox("daytona") as sb:
        sb.execute_command("…")
except SandboxError as e:
    if e.retryable:        # transient (timeout / connection) — back off and retry
        ...
    print(e.code, e.backend, e.op, e.context)
```

Subtypes include `SandboxConfigurationError`, `SandboxCreationError`,
`CommandFailedError` (with `exit_code` / `stdout` / `stderr`),
`SandboxTimeoutError` and `SandboxConnectionError` (both `retryable`),
`WorkspaceError`, `BackendNotInstalledError`, and `FeatureNotSupportedError`.

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
        cwd="/sandbox",
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

The generic context currently exposes `shell`, `files`, and `patch` operations.
This keeps basic evaluation and inference usage stable while making the agent
runtime boundary visible.

### Token usage & cost

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
between calls). Pass extra CLI flags with `extra_args=[...]`. Only Claude Code
is supported today; token counts and `llm_cost_usd` reflect exactly what its
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

Pricing data is best-effort and local to the installed package. To compare
available backends for a real workload, see
[`examples/find_cheapest.py`](examples/find_cheapest.py), which benchmarks
cold-start and execution time, then estimates cost with the same pricing data.

### Presets

Presets are predefined sandbox configurations with setup commands that run after creation.
The built-in presets are intentionally focused on agent CLIs: `codex`, `claude-code`, and `claude-code-codex`.
Both assume the sandbox image already includes Node.js and `npm` when setup commands are used as a fallback.

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

### Declarative workspace

Populate the sandbox at creation instead of scripting uploads afterwards.
`files` are written after any `git_repo` clone, and both land before preset
setup commands run.

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
    entries = sb.list_files("/requests")  # repo is cloned to /<repo-name>
    print(f"cloned {len(entries)} entries")
```

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

`SandboxClient("e2b").resume(state)` is equivalent and reuses a pooled client.
Resume returns the sandbox as-is — preset setup and `files`/`git_repo`
materialization are skipped.

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
```

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
    async with await client.create(image="python:3.12-slim") as sb:
        result = await sb.execute_code(code)
        return result.stdout

async def main():
    client = AsyncSandboxClient("daytona")
    outputs = await asyncio.gather(*(run_snippet(client, c) for c in snippets))

asyncio.run(main())
```

One-step creation works too: `sb = await AsyncSandbox.create("local")`.

Backend SDKs are synchronous, so async calls are offloaded to worker
threads — the event loop is never blocked. Note that the missing-SDK check
(`BackendNotInstalledError`) surfaces at the first `await client.create()`
rather than at `AsyncSandboxClient(...)` construction, which does no I/O.

## Feature Support Matrix

| Feature | Local | Safehouse | Docker | Ray | Daytona | Tensorlake | Modal | RunPod | E2B |
|---|---|---|---|---|---|---|---|---|---|
| `execute_code` | Any binary | Any binary | Any binary | Any binary | Python, TS, JS, Ruby, Go | Any binary | Any binary | Any binary | Python |
| `execute_command` | Shell | Shell | Shell | Shell | Shell | Shell | Shell | SSH | Shell |
| `list_files` | Native | Native | `find` / `ls` | Native | Native SDK | via `ls` | Native SDK | via SSH | Native SDK |
| `read_file` | Native | Native | `get_archive` | Native | Native SDK | via `cat` | Native SDK | via SSH | Native SDK |
| `write_file` | Native | Native | `put_archive` | Native | Native SDK | via base64 | Native SDK | via SSH | Native SDK |
| `upload_file` | `shutil.copy` | `shutil.copy` | `put_archive` | `ray.put` | Native SDK | via base64 | Native SDK | via SSH | Native SDK |
| `download_file` | `shutil.copy` | `shutil.copy` | `get_archive` | `ray.get` | Native SDK | via base64 | Native SDK | via SSH | Native SDK |
| `snapshot` | No | No | Yes | No | No | Yes | Yes | No | No |
| Resource limits | No | No | cpu, memory | cpu (Ray) | cpu, memory, disk | cpu, memory | cpu, memory, gpu | cpu, memory, disk, gpu | Tier-based |
| Network control | No | No | Yes | No | Firewall, VPN | Yes | Tunnels | No | No |
| Isolation | Process-level | macOS `sandbox-exec` | Container | Process | Full VM | Container | Container | Container | Full VM |
| GPU | No | No | No | Via Ray | No | No | Yes | Yes | No |
| Needs install | Nothing | `safehouse` CLI | Docker daemon | `ray` | API key | `tl login` | API key | API key + OpenSSH | API key |

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

# Tensorlake (authenticate via CLI)
tl login

# Modal
export MODAL_TOKEN_ID=your_id
export MODAL_TOKEN_SECRET=your_secret

# RunPod — add the matching public key to your RunPod account
export RUNPOD_API_KEY=your_key

# E2B
export E2B_API_KEY=your_key
```
