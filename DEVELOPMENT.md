# Development Direction

This package should focus on sandboxes for agents: creating, preparing,
resuming, and exposing isolated workspaces that agents can use safely. It
should not become a full agent framework.

The core ownership boundary is:

- This package owns sandbox lifecycle, filesystem access, command execution,
  snapshots, session resume, workspace preparation, and provider adapters.
- Agent frameworks own planning, model routing, handoffs, long-term memory,
  and agent orchestration.
- Integrations should let OpenAI Agents SDK, Claude SDK, LangGraph, custom
  runners, and CLI agents use the same sandbox primitive.

## Stable Core API

Keep the current `Sandbox` primitive stable. Existing users such as Curator
depend on direct code execution and file access:

```python
with Sandbox("docker", image="python:3.11") as sb:
    sb.write_file("/workspace/program.py", code)
    result = sb.execute_command("python3", ["program.py"])
```

Do not make users opt into an agent abstraction just to run code. The following
APIs should remain stable:

- `Sandbox(...)`
- `SandboxClient(...)`
- `execute_command(...)`
- `execute_code(...)`
- `write_file(...)`, `read_file(...)`, `list_files(...)`
- `snapshot()`
- `session_state()` and `resume(...)`
- context manager cleanup behavior

Agent support should be additive.

## Agent Placement

Agent placement should be explicit because it affects security, credentials,
logging, dependency installation, and failure modes.

There are two first-class modes:

- `inside`: the agent process runs inside the sandbox. This fits CLI agents
  such as Codex CLI, Claude Code, Gemini CLI, or a custom inference runner.
- `external`: the agent process runs outside the sandbox and drives it through
  sandbox-backed tools such as shell, files, patches, ports, and artifacts.

Example shape:

```python
from bespokelabs.sandbox import AgentSpec, Sandbox

with Sandbox("docker", git_repo="https://github.com/acme/project") as sb:
    agent = sb.agent(AgentSpec.inside(
        name="codex",
        command=["codex", "exec"],
        cwd="/workspace/project",
    ))
    result = agent.run("Run the eval and summarize failures")
```

External-driver shape:

```python
with Sandbox("docker", preset="python-data-science") as sb:
    tools = sb.agent_tools(capabilities=["shell", "files", "patch"])
    result = my_agent.run("Inspect the dataset and run inference", tools=tools)
```

The low-level sandbox object remains the source of truth. Agent APIs should sit
beside it, not replace it:

```python
agent = sb.agent(AgentSpec.inside(...))
tools = sb.agent_tools(...)
```

Avoid changing `with Sandbox(...) as sb` to return an agent session.

## Suggested Agent Types

The initial agent-facing API can be small:

- `AgentSpec`: declares name, placement, command, environment, working
  directory, input mode, and capabilities.
- `AgentSession`: wraps an inside-sandbox agent process and exposes `run(...)`.
- `AgentContext`: wraps a sandbox for external agents and exposes capability
  methods such as `shell(...)`, `read_file(...)`, `write_file(...)`, and
  `apply_patch(...)`.
- `agent_tools(...)`: returns framework-specific or generic tools backed by
  sandbox operations.

Capabilities should be explicit. Start with:

- `shell`
- `files`
- `patch`
- `ports`
- `artifacts`

## Workspace Declaration

The current `files=` and `git_repo=` kwargs are useful conveniences, but the
agent-focused API should grow toward a manifest-style workspace declaration.

Potential shape:

```python
workspace = Workspace(
    git_repo="https://github.com/acme/project",
    git_ref="main",
    files={"task.md": "..."},
    workdir="/workspace/project",
    env={"PYTHONUNBUFFERED": "1"},
)

with Sandbox("docker", workspace=workspace) as sb:
    ...
```

Keep `files=` and `git_repo=` as compatibility conveniences and translate them
into the workspace model internally.

## OpenAI Agents SDK

The OpenAI Agents SDK has useful patterns to borrow:

- sandbox agents distinguish the outer agent runtime from the sandbox session;
- manifests declare the initial workspace;
- run config chooses sandbox client, session state, snapshot, and options;
- capabilities expose shell/files behavior instead of assuming every agent gets
  unrestricted access;
- sandbox session state and tracing make agent runs easier to resume and audit.

Do not make `openai-agents` a core dependency. It brings in the full agent
runtime stack, while this package should stay lightweight and provider-neutral.
Instead, add OpenAI support behind an optional extra or separate adapter package.

Possible integration points:

- `bespokelabs-sandbox[openai-agents]`
- `Sandbox.to_openai_tools()`
- `Sandbox.to_openai_shell_tool()`
- `BespokeSandboxClient` implementing the OpenAI Agents SDK sandbox client
  interface
- conversion helpers between this package's workspace model and the OpenAI
  Agents SDK manifest model

## Compatibility Rules

When adding agent features:

- Make new APIs additive.
- Do not change the return type or context manager behavior of `Sandbox`.
- Do not require agent dependencies for basic sandbox execution.
- Keep provider SDKs behind optional extras.
- Preserve direct eval and inference use cases.
- Keep Curator-style usage working without changes.
- Prefer small adapter layers over importing large agent frameworks in core.

