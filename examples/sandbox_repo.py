"""
Summarize a GitHub repository using an agent inside a Daytona sandbox.

This example keeps the four agent placement patterns separate so each one is
easy to read:

- Claude Code with Sandbox(git_repo=...)
- Claude Code without git_repo, using Claude web tools
- Codex with Sandbox(git_repo=...)
- Codex without git_repo, using Codex web search

Usage:
    python examples/sandbox_repo.py --agent claude-code --mode git_repo
    python examples/sandbox_repo.py --agent claude-code --mode web
    python examples/sandbox_repo.py --agent codex --mode git_repo
    python examples/sandbox_repo.py --agent codex --mode web
    python examples/sandbox_repo.py --repo https://github.com/bespokelabsai/sandbox

Prerequisites:
    pip install 'bespokelabs-sandbox[daytona]'
    DAYTONA_API_KEY must be set
    ANTHROPIC_API_KEY must be set for --agent claude-code
    CODEX_API_KEY or OPENAI_API_KEY must be set for --agent codex
"""

import argparse
import os
import sys

from bespokelabs.sandbox import AgentSpec, Sandbox
from bespokelabs.sandbox.types import SandboxResult

DEFAULT_REPO = "https://github.com/bespokelabsai/curator"

_CODEX_FINAL_OUTPUT_SCRIPT = r"""
set -e

if [ "$#" -lt 2 ]; then
    echo "Usage: codex wrapper requires a command and prompt" >&2
    exit 2
fi

last_arg_index="$#"
prompt="${!last_arg_index}"
command_arg_count=$((last_arg_index - 1))
codex_args=("${@:1:command_arg_count}")

last_message="$(mktemp)"
transcript="$(mktemp)"
cleanup() {
    rm -f "$last_message" "$transcript"
}
trap cleanup EXIT

if "${codex_args[@]}" --output-last-message "$last_message" "$prompt" >"$transcript" 2>&1; then
    cat "$last_message"
else
    status=$?
    echo "Codex failed. Transcript tail:" >&2
    tail -n 200 "$transcript" >&2 || true
    exit "$status"
fi
""".strip()


def claude_code_with_git_repo(repo: str) -> SandboxResult:
    """Clone the repo with Sandbox(git_repo=...) and run Claude Code inside it."""
    env_vars = _require_env("ANTHROPIC_API_KEY")
    repo_name = _repo_name(repo)

    print(
        f"Creating Daytona sandbox with Claude Code and cloning {repo}...", flush=True)
    with Sandbox(
        "daytona",
        preset="claude-code",
        git_repo=repo,
        env_vars=env_vars,
    ) as sb:
        agent = sb.agent(AgentSpec.inside(
            name="claude",
            command=[
                "claude",
                "-p",
                "--permission-mode",
                "dontAsk",
                "--",
            ],
            cwd=repo_name,
            env=env_vars,
            input_mode="argv",
        ))

        print(
            f"Running Claude Code inside Daytona on cloned {repo_name} repo...", flush=True)
        return agent.run(_repo_prompt())


def claude_code_without_git_repo(repo: str) -> SandboxResult:
    """Ask Claude Code to summarize a GitHub URL with web tools."""
    env_vars = _require_env("ANTHROPIC_API_KEY")

    print("Creating Daytona sandbox with Claude Code...", flush=True)
    with Sandbox(
        "daytona",
        preset="claude-code",
        env_vars=env_vars,
    ) as sb:
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
                "--",
            ],
            env=env_vars,
            input_mode="argv",
        ))

        print(
            f"Running Claude Code inside Daytona to summarize {repo}...", flush=True)
        return agent.run(_web_prompt(repo))


def codex_with_git_repo(repo: str) -> SandboxResult:
    """Clone the repo with Sandbox(git_repo=...) and run Codex inside it."""
    env_vars = {"CODEX_API_KEY": _codex_api_key()}
    repo_name = _repo_name(repo)

    print(
        f"Creating Daytona sandbox with Codex and cloning {repo}...", flush=True)
    with Sandbox(
        "daytona",
        preset="codex",
        git_repo=repo,
        env_vars=env_vars,
    ) as sb:
        agent = sb.agent(AgentSpec.inside(
            name="codex",
            command=_codex_command(search=False),
            cwd=repo_name,
            env=env_vars,
            input_mode="argv",
        ))

        print(
            f"Running Codex inside Daytona on cloned {repo_name} repo...", flush=True)
        return agent.run(_repo_prompt())


def codex_without_git_repo(repo: str) -> SandboxResult:
    """Ask Codex to summarize a GitHub URL with live search enabled."""
    env_vars = {"CODEX_API_KEY": _codex_api_key()}

    print("Creating Daytona sandbox with Codex...", flush=True)
    with Sandbox(
        "daytona",
        preset="codex",
        env_vars=env_vars,
    ) as sb:
        agent = sb.agent(AgentSpec.inside(
            name="codex",
            command=_codex_command(search=True),
            env=env_vars,
            input_mode="argv",
        ))

        print(
            f"Running Codex inside Daytona to summarize {repo}...", flush=True)
        return agent.run(_web_prompt(repo))


def _repo_prompt() -> str:
    return (
        "Read the README and key source files in this repository, then summarize what it does. "
        "Cover: (1) main purpose, (2) key features, (3) primary use cases. "
        "Be concise but comprehensive."
    )


def _web_prompt(repo: str) -> str:
    return (
        f"Please summarize what {repo} does. "
        "Cover: (1) main purpose, (2) key features, (3) primary use cases. "
        "Be concise but comprehensive. Do not write code; just a clear prose summary."
    )


def _codex_command(*, search: bool) -> list[str]:
    command = ["codex"]
    if search:
        command.append("--search")
    command.extend([
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--ignore-user-config",
        "--ignore-rules",
    ])
    return ["bash", "-lc", _CODEX_FINAL_OUTPUT_SCRIPT, "codex-final-output", *command]


def _repo_name(repo: str) -> str:
    return repo.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1]


def _require_env(name: str) -> dict[str, str]:
    value = os.environ.get(name, "")
    if not value:
        sys.exit(f"Error: {name} is not set.")
    return {name: value}


def _codex_api_key() -> str:
    api_key = os.environ.get(
        "CODEX_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        sys.exit("Error: CODEX_API_KEY or OPENAI_API_KEY is not set.")
    return api_key


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Summarize a repository with an agent in Daytona")
    parser.add_argument(
        "--agent", choices=["claude-code", "codex"], default="claude-code")
    parser.add_argument(
        "--mode", choices=["git_repo", "web"], default="git_repo")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    args = parser.parse_args()

    if not os.environ.get("DAYTONA_API_KEY"):
        sys.exit("Error: DAYTONA_API_KEY is not set.")

    agent = args.agent

    if agent == "claude-code" and args.mode == "git_repo":
        result = claude_code_with_git_repo(args.repo)
    elif agent == "claude-code":
        result = claude_code_without_git_repo(args.repo)
    elif args.mode == "git_repo":
        result = codex_with_git_repo(args.repo)
    else:
        result = codex_without_git_repo(args.repo)

    print(f"=== Summary of {args.repo} ===\n")
    print(result.stdout)

    if result.stderr:
        print("\n--- stderr ---", file=sys.stderr)
        print(result.stderr, file=sys.stderr)

    if result.exit_code != 0:
        print(f"\n(exit code: {result.exit_code})", file=sys.stderr)
        sys.exit(result.exit_code)


if __name__ == "__main__":
    main()
