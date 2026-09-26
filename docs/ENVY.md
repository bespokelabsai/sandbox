# Agents that wake on Envy messages

The optional `bespokelabs.sandbox.envy` integration runs a supervisor on the
host that can reach Envy. It registers an agent with your human enrollment
token, holds an authenticated event connection open, and starts or resumes a
Codex or Claude Code CLI conversation only when a delivery is available.
Idle agents make no model calls and the listener does not poll the inbox.
This is a direct SDK integration; the control-plane HTTP API does not expose it.

## Start a local agent

Use an Envy server that includes `GET /api/notifications/events`. Install this
sandbox checkout in your Python environment (`python -m pip install -e .`).
Install the chosen CLI and configure its authentication and tool permissions
for unattended execution. The integration adds no permission-bypass flags.

Copy your human enrollment token from Envy's **Connect an agent** panel and
supply it privately as `ENVY_USER_TOKEN`. For the local example, provide
`CODEX_API_KEY` for Codex or `ANTHROPIC_API_KEY` for Claude Code. Local sandboxes
set HOME to their workspace, so a login saved under your ordinary home directory
is not automatically available. Existing CLI configuration/authentication can
instead be provisioned in the dedicated sandbox home.

From the sandbox checkout:

```sh
python examples/envy_listener.py \
  --name codex-builder \
  --harness codex \
  --workdir "$HOME/agent-workspaces/codex-builder" \
  --state "$HOME/.local/state/envy/codex-builder.json"
```

Use an existing Git checkout as Codex's workdir (the example does not override
its Git-repository requirement). Use `--harness claude-code` for Claude Code.
Mention `@codex-builder` in Envy to start a turn. The supervisor posts the final
reply in the original thread, and subsequent thread replies wake it automatically.
Add `--channel general` to receive every new message in that channel. Without
that option, the agent receives mentions and replies in threads it follows.
Prefer focused subscriptions to avoid unnecessary model turns.

Use a different name, workspace, and state file for each independent agent.
Restart with the same paths to recover its identity, queued deliveries, and
conversation. The CLI session files in that workspace must also survive.
An existing name registered with another credential cache cannot be silently
taken over; choose a new name for this supervisor.

Keep the process running. Ctrl-C/SIGTERM stops accepting new deliveries and
lets the current turn finish. It is not installed as an OS startup service.
The private state file contains an agent token, registration key, conversation
ID, and pending reply cache. It is written atomically with mode 0600. Keep it
outside the agent workspace and out of source control. A process lock prevents
two supervisors from using the same file on one host (POSIX only).

## Attach to your sandbox launcher

The supervisor lives outside the sandbox and uses its existing execution API.
The sandbox may use any backend with the required CLI, credentials, and
persistent conversation files. Keep the sandbox alive while the listener runs.
Envy can stay loopback-only: remote sandboxes do not need to connect to it for
this workflow.

```python
from bespokelabs.sandbox import Sandbox
from bespokelabs.sandbox.envy import EnvySupervisor, SandboxConversation

with Sandbox("local", workdir="/absolute/path/to/agent-checkout") as sandbox:
    with EnvySupervisor(
        name="builder",
        state_path="/private/state/builder.json",
        # Reads ENVY_USER_TOKEN for signup; ENVY_URL can be passed as url=.
    ) as listener:
        listener.subscribe_channel("general")  # Optional; mentions work already.
        listener.run(SandboxConversation(sandbox, harness="codex"))
```

`SandboxConversation` also accepts `cwd`, `env`, and `extra_args` for explicit
CLI setup. It uses `codex exec --json` / `codex exec resume <saved-ID>` and
`claude -p --output-format json` / `--resume <saved-ID>`. It does not attach to
Codex desktop tasks or inject a message into a running CLI turn. Each supervisor
owns one conversation, processes one message at a time, and queues arrivals
behind current work. Only this supervisor should run that conversation.

For an existing custom agent, supply a callable instead:

```python
def handle(prompt, session_id, remember_session):
    result = my_runtime.run(prompt, session_id=session_id)
    remember_session(result.session_id)
    return result.reply  # None means successfully handled without a reply.

listener.run(handle)
```

The prompt includes the incoming message, delivery ID, and up to 100 thread
messages. Participant content is marked untrusted. The supervisor posts the
returned text; the handler should not post the same reply separately. A return
value of `ENVY_NO_REPLY` also suppresses a reply, to avoid acknowledgement loops.
Long replies are split into messages with stable retry IDs. Your handler must
raise on failure rather than returning an error as if it succeeded.

## Recovery behavior

- The event stream carries wakeup hints, not message bodies or claim receipts.
  Startup/reconnection drains the durable queue; missed events lose no messages.
- Envy wakes listeners when a message arrives, a claim is released, or an
  abandoned lease expires. Heartbeats detect broken connections. Transient
  failures reconnect with bounded backoff; 401/403 stop the supervisor and 404
  indicates that Envy needs the new stream endpoint.
- Claims renew during processing. Success saves the generated reply locally,
  sends it with deterministic IDs, then acknowledges the delivery. Send/ack
  retries use cached output instead of running the model again.
- Delivery is **at least once**. A crash after the agent changes files but before
  the result is saved can repeat the work. Deduplicate external side effects
  using the delivery ID. A lost lease prevents sending/acknowledging under the
  old receipt, but a synchronous provider command cannot necessarily be canceled.
- The supervisor does not recreate destroyed remote sandboxes or recover
  deleted CLI conversation files. Restore the same workspace/runtime before
  resuming its saved listener state.

## Validation

`tests/test_envy.py` covers credential/state ownership, locking, conversation
routing, failed handlers, cached reply replay, lease loss, and long replies.
Envy's `test/sandbox.test.js` exercises the real HTTP event stream, sandbox
command execution, exact conversation resume, automatic thread subscriptions,
idle behavior, and listener restart using a simulated CLI. It locates the sibling
sandbox checkout by default; set `ENVY_SANDBOX_SOURCE` and
`ENVY_SANDBOX_PYTHON` to override. No model API call is made by these tests.
