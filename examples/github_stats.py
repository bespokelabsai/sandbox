"""Fetch GitHub repo stats using Codex inside a sandbox.

Demonstrates the return_type feature: Codex searches the web for repo
stats and returns structured JSON that is automatically parsed into a
Pydantic model.

Prerequisites:
    - OPENAI_API_KEY set in your environment

Usage:
    OPENAI_API_KEY=sk-... python examples/github_stats.py
    OPENAI_API_KEY=sk-... python examples/github_stats.py --repo pytorch/pytorch
"""

import argparse
import os
import sys

from pydantic import BaseModel

from bespokelabs.sandbox import Sandbox, SandboxExecutionError, json_schema

WORKDIR = os.path.join(os.path.dirname(__file__), ".sandbox_workdir")
OUTPUT_FILE = "/tmp/codex_output.txt"


class RepoStats(BaseModel):
    stars: int = 0
    forks: int = 0
    open_issues: int = 0
    language: str = ""
    description: str = ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch GitHub repo stats via Codex in a sandbox")
    parser.add_argument("--repo", default="bespokelabs/curator", help="GitHub owner/repo (default: bespokelabs/curator)")
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        sys.exit("Set OPENAI_API_KEY before running this example.")

    prompt = f"Look up the GitHub repository {args.repo}. {json_schema(RepoStats)}"

    print(f"Fetching stats for {args.repo} using Codex...\n")

    with Sandbox("local", preset="codex", env_vars={"OPENAI_API_KEY": api_key}, workdir=WORKDIR) as sb:
        try:
            result = sb.execute_command("codex", args=[
                "exec", "--full-auto", "--skip-git-repo-check", "--search",
                "-o", OUTPUT_FILE,
                prompt,
            ])
            if result.exit_code != 0:
                sys.exit(f"Codex failed (exit {result.exit_code}):\n{result.stderr[:500]}")

            raw = sb.read_file(OUTPUT_FILE).decode()
            stats = Sandbox.parse_as(raw, RepoStats)
        except SandboxExecutionError as e:
            sys.exit(f"Failed: {e}")

    print(f"  Repository:   {args.repo}")
    print(f"  Description:  {stats.description}")
    print(f"  Language:     {stats.language}")
    print(f"  Stars:        {stats.stars:,}")
    print(f"  Forks:        {stats.forks:,}")
    print(f"  Open issues:  {stats.open_issues:,}")


if __name__ == "__main__":
    main()
