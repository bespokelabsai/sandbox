"""Run Claude Code outside a sandbox while it inspects a repo inside the sandbox.

Claude Code runs on the host machine. The repository is cloned into the sandbox
with ``Sandbox(git_repo=...)``. Claude can only inspect that repo by calling a
short-lived localhost bridge, which forwards shell commands to the sandbox.

The task asks Claude to count regular files in the repository root directory by
running sandbox commands.

Prerequisites:
    - Claude Code CLI installed: npm install -g @anthropic-ai/claude-code
    - ANTHROPIC_API_KEY set in your host environment
    - git available in the selected sandbox backend

Usage:
    python examples/claude_code_outside.py
    python examples/claude_code_outside.py --backend docker
    python examples/claude_code_outside.py --repo https://github.com/bespokelabsai/sandbox
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from bespokelabs.sandbox import AgentContext, AgentSpec, Sandbox
from bespokelabs.sandbox.types import SandboxResult

DEFAULT_REPO = "https://github.com/bespokelabsai/curator"


class SandboxShellBridge:
    """Local command bridge that forwards shell commands into one sandbox."""

    def __init__(self, ctx: AgentContext) -> None:
        self._ctx = ctx
        self._token = secrets.token_urlsafe(24)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._script_path: Path | None = None

    @property
    def command(self) -> str:
        if self._script_path is None:
            raise RuntimeError("Bridge has not started")
        return f"{shlex.quote(sys.executable)} {shlex.quote(str(self._script_path))}"

    def __enter__(self) -> SandboxShellBridge:
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                try:
                    payload = self._read_payload()
                    if payload.get("token") != bridge._token:
                        self._send_json({"stderr": "invalid bridge token\n", "exit_code": 403}, status=403)
                        return
                    command = str(payload.get("command", "")).strip()
                    if not command:
                        self._send_json({"stderr": "missing command\n", "exit_code": 2}, status=400)
                        return

                    result = bridge._ctx.shell("bash", ["-lc", command])
                    self._send_json({
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                        "exit_code": result.exit_code,
                    })
                except Exception as exc:
                    self._send_json({"stderr": f"{type(exc).__name__}: {exc}\n", "exit_code": 1}, status=500)

            def log_message(self, format: str, *args: Any) -> None:
                del format, args

            def _read_payload(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0"))
                return json.loads(self.rfile.read(length).decode())

            def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

        host, port = self._server.server_address
        self._script_path = self._write_client_script(f"http://{host}:{port}")
        return self

    def __exit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._script_path is not None:
            self._script_path.unlink(missing_ok=True)

    def _write_client_script(self, url: str) -> Path:
        script = textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json
            import sys
            import urllib.request

            command = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else sys.stdin.read()
            if not command.strip():
                sys.exit("Usage: sandbox-shell '<command>'")

            request = urllib.request.Request(
                {url!r},
                data=json.dumps({{"token": {self._token!r}, "command": command}}).encode(),
                headers={{"Content-Type": "application/json"}},
            )
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = json.loads(response.read().decode())

            sys.stdout.write(payload.get("stdout", ""))
            sys.stderr.write(payload.get("stderr", ""))
            raise SystemExit(int(payload.get("exit_code", 1)))
            """
        )
        handle = tempfile.NamedTemporaryFile("w", prefix="sandbox-shell-", suffix=".py", delete=False)
        with handle:
            handle.write(script)
        path = Path(handle.name)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path


def run_claude_code_outside(ctx: AgentContext, prompt: str) -> SandboxResult:
    """Launch Claude Code on the host and let it use the sandbox bridge."""
    with SandboxShellBridge(ctx) as bridge:
        claude_prompt = textwrap.dedent(
            f"""\
            You are Claude Code running outside the sandbox. The repository is
            installed inside the sandbox, so do not inspect local host files for
            this task.

            Use this bridge to run shell commands inside the sandbox:

                {bridge.command} '<shell command>'

            {prompt}

            Return the final answer as FILE_COUNT=<number>, followed by the
            sandbox commands you used.
            """
        )
        completed = subprocess.run(
            [
                "claude",
                "-p",
                "--permission-mode",
                "dontAsk",
                "--allowedTools",
                "Bash",
                "--output-format",
                "text",
                "--",
                claude_prompt,
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    return SandboxResult(
        stdout=completed.stdout,
        stderr=completed.stderr,
        exit_code=completed.returncode,
    )


def count_repo_root_files_with_claude(backend: str, repo: str, git_ref: str | None) -> SandboxResult:
    repo_name = _repo_name(repo)
    print(f"Creating {backend} sandbox and cloning {repo}...", flush=True)
    with Sandbox(backend, git_repo=repo, git_ref=git_ref, timeout_secs=300) as sb:
        agent = sb.agent(AgentSpec.outside(
            name="claude-code",
            capabilities=["shell"],
            runner=run_claude_code_outside,
        ))

        prompt = (
            f"The repo is at /{repo_name}. Find the number of regular files "
            "directly in that repository root directory. Do not count files in "
            "subdirectories. Run sandbox commands to verify the path and count."
        )
        return agent.run(prompt)


def _repo_name(repo: str) -> str:
    return repo.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Use host-side Claude Code to inspect a repo cloned inside a sandbox"
    )
    parser.add_argument("--backend", default="local")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--git-ref", default=None)
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Error: ANTHROPIC_API_KEY is not set.")

    result = count_repo_root_files_with_claude(args.backend, args.repo, args.git_ref)
    print(result.stdout)

    if result.stderr:
        print("--- Claude Code stderr ---", file=sys.stderr)
        print(result.stderr, file=sys.stderr)

    if result.exit_code != 0:
        sys.exit(result.exit_code)


if __name__ == "__main__":
    main()
