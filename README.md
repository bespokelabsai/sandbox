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
- **Cost tracking** — Monitor and compare spend across providers. *(coming soon)*
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

The Safehouse backend has no Python extra. Install the CLI separately on macOS:

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
for backend in ["local", "safehouse", "docker", "modal", "e2b", "daytona", "tensorlake", "ray"]:
    with Sandbox(backend) as sb:
        sb.execute_code('print("same code, any backend")')
```

## API Reference

### Creating a Sandbox

```python
from bespokelabs.sandbox import Sandbox

sb = Sandbox(
    backend,              # "local" | "safehouse" | "docker" | "ray" | "daytona" | "tensorlake" | "modal" | "e2b"
    *,
    preset=None,          # Preset name or SandboxPreset object
    cpu=1.0,              # vCPUs (Tensorlake, Modal, Docker, Daytona)
    memory_mb=1024,       # RAM in MB (Tensorlake, Modal, Docker, Daytona)
    disk_mb=None,         # Disk in MB (Daytona)
    timeout_secs=600,     # Max lifetime / subprocess timeout
    image=None,           # Container image (Docker, Modal, Daytona)
    template=None,        # Template ID (E2B)
    env_vars=None,        # dict of environment variables
    allow_internet=True,  # Network access (Docker, Tensorlake, Daytona)
    app_name=None,        # App name (Modal)
    snapshot_id=None,     # Restore from snapshot (Tensorlake, Modal)
    workdir=None,         # Host directory to use as sandbox root (Safehouse)
    backend_options=None, # dict merged into the backend's native create call
    files=None,           # {path: bytes|str} written into the sandbox on create
    git_repo=None,        # repo URL cloned into the sandbox on create
    git_ref=None,         # branch/tag for git_repo
)
```

Not every backend uses every parameter. Unsupported params are silently ignored.

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
```

### Agent-ready sandboxes

Sandboxes can also be bound to agents without replacing the low-level sandbox
API. Agent placement is explicit:

- `inside`: the agent process runs inside the sandbox, useful for CLI agents
  such as Codex CLI, Claude Code, or a custom inference runner.
- `external`: the agent process runs outside the sandbox and drives it through
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

External-driver agent:

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
    agent = sb.agent(AgentSpec.external(
        name="eval-runner",
        capabilities=["shell", "files"],
        runner=run_eval,
    ))

    print(agent.run("Evaluate this input"))
```

For external agent frameworks, use `agent_tools(...)` directly:

```python
with Sandbox("docker") as sb:
    tools = sb.agent_tools(capabilities=["shell", "files", "patch"])
    tools.write_file("/workspace/input.txt", "hello")
    print(tools.shell("cat", ["/workspace/input.txt"]).stdout)
```

The generic context currently exposes `shell`, `files`, and `patch` operations.
This keeps basic evaluation and inference usage stable while making the agent
runtime boundary visible.

### Presets

Presets are predefined sandbox configurations with setup commands that run after creation.
The built-in presets are intentionally focused on agent CLIs: `codex` and `claude-code`.
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

The Dockerfiles live under `images/<preset>/`. Local, Safehouse, Ray, and other backends that cannot use the prebuilt image still fall back to the preset setup commands. Tensorlake image names are project-scoped, so you can build/register equivalent images from the same Dockerfiles when you need Tensorlake-specific preset images.

```python
# Sandbox with Codex CLI installed
with Sandbox("docker", preset="codex") as sb:
    sb.execute_command("codex --version")

# Sandbox with Claude Code installed
with Sandbox("docker", preset="claude-code") as sb:
    sb.execute_command("claude --version")
```

Built-in presets:

| Preset | What it installs | Defaults |
|---|---|---|
| `claude-code` | `@anthropic-ai/claude-code` via npm | 2GB RAM, 30min timeout |
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
`create_and_connect`, and Daytona's create params; ignored by local, safehouse,
and ray.

```python
# e.g. set the container hostname (a Docker-only knob)
with Sandbox("docker", backend_options={"hostname": "build-box"}) as sb:
    sb.execute_command("hostname")
```

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
| Daytona | sandbox id (`client.get`) | `sandbox_id` |
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
| Daytona, E2B, Local, Ray, Safehouse | No |

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

| Feature | Local | Safehouse | Docker | Ray | Daytona | Tensorlake | Modal | E2B |
|---|---|---|---|---|---|---|---|---|
| `execute_code` | Any binary | Any binary | Any binary | Any binary | Python, TS, JS, Ruby, Go | Any binary | Any binary | Python |
| `execute_command` | Shell | Shell | Shell | Shell | Shell | Shell | Shell | Shell |
| `list_files` | Native | Native | `find` / `ls` | Native | Native SDK | via `ls` | Native SDK | Native SDK |
| `read_file` | Native | Native | `get_archive` | Native | Native SDK | via `cat` | Native SDK | Native SDK |
| `write_file` | Native | Native | `put_archive` | Native | Native SDK | via base64 | Native SDK | Native SDK |
| `upload_file` | `shutil.copy` | `shutil.copy` | `put_archive` | `ray.put` | Native SDK | via base64 | Native SDK | Native SDK |
| `download_file` | `shutil.copy` | `shutil.copy` | `get_archive` | `ray.get` | Native SDK | via base64 | Native SDK | Native SDK |
| `snapshot` | No | No | Yes | No | No | Yes | Yes | No |
| Resource limits | No | No | cpu, memory | cpu (Ray) | cpu, memory, disk | cpu, memory | cpu, memory, gpu | Tier-based |
| Network control | No | No | Yes | No | Firewall, VPN | Yes | Tunnels | No |
| Isolation | Process-level | macOS `sandbox-exec` | Container | Process | Full VM | Container | Container | Full VM |
| GPU | No | No | No | Via Ray | No | No | Yes | No |
| Needs install | Nothing | `safehouse` CLI | Docker daemon | `ray` | API key | `tl login` | API key | API key |

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

# E2B
export E2B_API_KEY=your_key
```
