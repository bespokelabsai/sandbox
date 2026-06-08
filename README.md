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
)
```

Not every backend uses every parameter. Unsupported params are silently ignored.

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

### Presets

Presets are predefined sandbox configurations with setup commands that run after creation.
Presets that install tools with `npm`, such as `codex`, `claude-code`, and `web-dev`, assume the sandbox image already includes Node.js and `npm`.

```python
# Sandbox with Codex CLI installed
with Sandbox("docker", preset="codex") as sb:
    sb.execute_command("codex --version")

# Sandbox with Claude Code installed
with Sandbox("docker", preset="claude-code") as sb:
    sb.execute_command("claude --version")

# Python data science stack
with Sandbox("e2b", preset="python-data-science") as sb:
    sb.execute_code("import pandas as pd; print(pd.__version__)")
```

Built-in presets:

**Agents**

| Preset | What it installs | Defaults |
|---|---|---|
| `claude-code` | `@anthropic-ai/claude-code` via npm | 2GB RAM, 30min timeout |
| `claude-sdk` | Claude Agent SDK + bundled Claude Code CLI | 2GB RAM, 30min timeout |
| `codex` | `@openai/codex` via npm | 2GB RAM, 30min timeout |

**Language runtimes**

| Preset | What it installs | Defaults |
|---|---|---|
| `node` | Node.js 20 LTS + typescript, ts-node, pnpm, yarn | 2GB RAM |
| `web-dev` | Node.js + typescript, ts-node, prettier, eslint | 2GB RAM |
| `go` | Go 1.22 toolchain | 2GB RAM |
| `rust` | Rust + clippy, rustfmt | 2GB RAM |
| `java` | Java 21 (Temurin) + Maven, Gradle | 2GB RAM |
| `ruby` | Ruby 3.3 + bundler, rake | defaults |
| `php` | PHP 8.3 + Composer | defaults |
| `dotnet` | .NET 8.0 SDK | 2GB RAM |
| `cpp` | gcc 13 + cmake, ninja, clang, gdb | 2GB RAM |
| `r` | R 4.4.1 + data.table, ggplot2, dplyr, jsonlite | 2GB RAM |

**Machine learning / AI**

| Preset | What it installs | Defaults |
|---|---|---|
| `python-data-science` | numpy, pandas, matplotlib, scikit-learn | 2GB RAM |
| `python-ml` | torch, transformers, datasets, accelerate | 2 vCPU, 4GB RAM, 30min timeout |
| `pytorch` | torch, torchvision, torchaudio (CPU) | 2 vCPU, 4GB RAM, 30min timeout |
| `tensorflow` | tensorflow-cpu | 2 vCPU, 4GB RAM, 30min timeout |
| `huggingface` | transformers, datasets, accelerate, hub, safetensors | 2 vCPU, 4GB RAM, 30min timeout |
| `nlp` | spaCy + NLTK (en_core_web_sm, common corpora) | 2GB RAM, 30min timeout |
| `llm` | openai, anthropic, langchain, tiktoken, tenacity | 2GB RAM |
| `scientific` | numpy, scipy, sympy, networkx, statsmodels | 2GB RAM |

**Web / scraping / browser**

| Preset | What it installs | Defaults |
|---|---|---|
| `scraping` | requests, httpx, beautifulsoup4, lxml, parsel, scrapy | 2GB RAM |
| `playwright` | Playwright + browsers | 2 vCPU, 4GB RAM, 30min timeout |
| `selenium` | selenium, webdriver-manager + headless Chromium | 2 vCPU, 2GB RAM, 30min timeout |
| `fastapi` | fastapi, uvicorn, pydantic, httpx | 2GB RAM |

**Data / documents / media**

| Preset | What it installs | Defaults |
|---|---|---|
| `dataeng` | polars, duckdb, pyarrow, sqlalchemy | 4GB RAM |
| `pdf` | pypdf, pdfplumber, reportlab, pdf2image + poppler | 2GB RAM |
| `image` | pillow, opencv-headless, scikit-image, numpy | 2GB RAM |

**Other**

| Preset | What it installs | Defaults |
|---|---|---|
| `empty` | Nothing | defaults |

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
