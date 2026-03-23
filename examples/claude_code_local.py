"""Run Claude Code inside a local sandbox to process a command and return an answer.

The sandbox uses a persistent working directory so you can resume the same
Claude Code session across multiple runs.  All files and Claude's conversation
history (stored in .claude/ inside the workdir) survive between invocations.

Prerequisites:
    - Claude Code CLI installed:  npm install -g @anthropic-ai/claude-code
    - ANTHROPIC_API_KEY set in your environment

Usage:
    # First run — seeds a file and asks Claude to review it:
    python examples/claude_code_local.py

    # Second run — same sandbox, Claude remembers the previous conversation:
    python examples/claude_code_local.py --resume
"""

import argparse
import os
import sys

from bespokelabs.sandbox import Sandbox

# A fixed directory so the sandbox (and Claude's session state) persists.
WORKDIR = os.path.join(os.path.dirname(__file__), ".sandbox_workdir")


def main() -> None:
    parser = argparse.ArgumentParser(description="Claude Code in a local sandbox")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip seeding files — just send a follow-up prompt to the existing session",
    )
    parser.add_argument(
        "--prompt",
        type=str,
        default=None,
        help="Custom prompt to send to Claude Code",
    )
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        sys.exit("Set ANTHROPIC_API_KEY before running this example.")

    # workdir= pins the sandbox to a stable directory.  On destroy() the
    # directory is preserved, so Claude Code's .claude/ conversation history
    # and any files it created survive for the next run.
    with Sandbox(
        "local",
        timeout_secs=120,
        env_vars={"ANTHROPIC_API_KEY": api_key},
        workdir=WORKDIR,
    ) as sb:
        if not args.resume:
            # First run: seed the sandbox with a file for Claude to work on.
            sb.write_file(
                "/code.py",
                """\
def fibonacci(n):
    if n <= 1:
        return n
    return fibonacci(n - 1) + fibonacci(n - 2)

def flatten(lst):
    result = []
    for item in lst:
        if type(item) == list:
            result = result + flatten(item)
        else:
            result.append(item)
    return result
""",
            )
            prompt = (
                "Review /code.py. Point out bugs or performance issues "
                "and suggest fixes."
            )
        else:
            # Resume: Claude still sees all previous files + conversation.
            prompt = "What files have you seen so far? Summarize your earlier review."

        if args.prompt:
            prompt = args.prompt

        # -p  : non-interactive single-prompt mode
        # --output-format text : plain-text output (no markdown)
        # -c  : continue the most recent conversation (enables resume)
        claude_args = ["-p", prompt, "--output-format", "text"]
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
