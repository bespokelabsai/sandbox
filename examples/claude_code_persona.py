"""Initialize Claude Code with a custom persona and interact with it.

This example shows how to give Claude Code a specific personality or role
using --append-system-prompt, then run it inside a persistent local sandbox
so the persona carries across follow-up prompts.

Prerequisites:
    - Claude Code CLI installed:  npm install -g @anthropic-ai/claude-code
    - ANTHROPIC_API_KEY set in your environment

Usage:
    # Run with the default "senior security auditor" persona:
    python examples/claude_code_persona.py

    # Use your own persona:
    python examples/claude_code_persona.py --persona "You are a Rust expert who favors zero-copy designs."

    # Send a follow-up (persona is baked into the continued session):
    python examples/claude_code_persona.py --resume --prompt "Now check for race conditions"
"""

import argparse
import os
import sys

from bespokelabs.sandbox import Sandbox

WORKDIR = os.path.join(os.path.dirname(__file__), ".persona_sandbox")

DEFAULT_PERSONA = """\
You are a senior security auditor.  When reviewing code:
- Flag every potential injection, overflow, or TOCTOU issue.
- Rate each finding as critical / high / medium / low.
- Suggest a concrete fix for each finding.
- At the end, give an overall security score from 0-10.
Be direct and technical.  Skip praise.
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Claude Code with a custom persona")
    parser.add_argument(
        "--persona",
        type=str,
        default=DEFAULT_PERSONA,
        help="System prompt that defines Claude's persona",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Continue the previous conversation (persona persists automatically)",
    )
    parser.add_argument(
        "--prompt",
        type=str,
        default=None,
        help="Custom user prompt (overrides the default)",
    )
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        sys.exit("Set ANTHROPIC_API_KEY before running this example.")

    with Sandbox(
        "local",
        timeout_secs=120,
        env_vars={"ANTHROPIC_API_KEY": api_key},
        workdir=WORKDIR,
    ) as sb:
        if not args.resume:
            # Seed sample code for the persona to work on.
            sb.write_file(
                "/app.py",
                """\
import sqlite3
import subprocess
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = parse_qs(self.path.split("?", 1)[-1])
        username = params.get("user", [""])[0]

        conn = sqlite3.connect("users.db")
        row = conn.execute(
            f"SELECT * FROM users WHERE name = '{username}'"
        ).fetchone()

        if row:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(f"Welcome back, {username}!".encode())
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"User not found")

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        body = self.rfile.read(length).decode()
        params = parse_qs(body)

        cmd = params.get("cmd", ["echo hello"])[0]
        output = subprocess.check_output(cmd, shell=True)

        self.send_response(200)
        self.end_headers()
        self.wfile.write(output)
""",
            )
            prompt = args.prompt or "Audit /app.py for security vulnerabilities."
        else:
            prompt = args.prompt or "Summarize your earlier findings."

        # Build the claude command.
        # --append-system-prompt preserves Claude Code's built-in capabilities
        # while layering the persona on top.
        claude_args = [
            "-p", prompt,
            "--output-format", "text",
            "--append-system-prompt", args.persona,
        ]
        if args.resume:
            claude_args.append("-c")

        result = sb.execute_command("claude", args=claude_args)

        print("--- Claude Code response ---")
        print(result.stdout)
        if result.stderr:
            print("--- stderr ---", file=sys.stderr)
            print(result.stderr, file=sys.stderr)
        if result.exit_code != 0:
            print(f"(exit code: {result.exit_code})")


if __name__ == "__main__":
    main()
