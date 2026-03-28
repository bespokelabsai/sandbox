"""Fetch GitHub repo stats using Codex inside a sandbox.

Demonstrates the return_type feature: Codex searches the web for repo
stats and returns structured JSON that is automatically parsed into a
dataclass.

Prerequisites:
    - OPENAI_API_KEY set in your environment

Usage:
    OPENAI_API_KEY=sk-... python examples/github_stats.py
    OPENAI_API_KEY=sk-... python examples/github_stats.py --repo pytorch/pytorch
"""

import argparse
import dataclasses
import os
import sys

from bespokelabs.sandbox import Sandbox, SandboxExecutionError, json_schema

WORKDIR = os.path.join(os.path.dirname(__file__), ".sandbox_workdir")


@dataclasses.dataclass
class RepoStats:
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
            stats = sb.execute_command(
                "codex", args=["-q", prompt],
                return_type=RepoStats,
            )
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
