"""Spin up a sandbox with Claude Code and ask it to find cloud sandbox pricing."""

import argparse
import os
import sys

from bespokelabs.sandbox import Sandbox

WORKDIR = os.path.join(os.path.dirname(__file__), ".sandbox_workdir")

PROVIDERS = {
    "daytona": "Daytona (daytona.io) cloud sandboxes",
    "e2b": "E2B (e2b.dev) code interpreter sandboxes",
    "tensorlake": "Tensorlake (tensorlake.ai) sandboxes",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch cloud sandbox pricing using Claude Code")
    parser.add_argument(
        "--providers",
        default=",".join(PROVIDERS),
        help=f"Comma-separated providers to look up (default: all). Choices: {', '.join(PROVIDERS)}",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Claude model to use (e.g. sonnet, opus, haiku). Defaults to Claude Code's default.",
    )
    args = parser.parse_args()

    names = [n.strip() for n in args.providers.split(",") if n.strip()]
    unknown = [n for n in names if n not in PROVIDERS]
    if unknown:
        sys.exit(f"Unknown provider(s): {', '.join(unknown)}. Choose from: {', '.join(PROVIDERS)}")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        sys.exit("Set ANTHROPIC_API_KEY before running this example.")

    descriptions = [PROVIDERS[n] for n in names]
    provider_list = ", ".join(descriptions)

    if len(names) > 1:
        prompt = (
            f"Find the exact per-unit pricing for each of these cloud sandbox providers: {provider_list}. "
            "For each provider I need the actual dollar amounts: $/vCPU/hour, $/GiB RAM/hour, "
            "$/GiB storage/month, per-execution fees, or however they structure their pricing. "
            "Check their pricing pages, docs, billing docs, and any blog posts or community comparisons. "
            "If a pricing page requires login, try to find the rates from third-party sources, "
            "blog posts, or documentation. I want exact numbers, not just tier limits.\n\n"
            "Present the results as a single comparison table with providers as rows and pricing "
            "dimensions as columns. After the table, add a brief commentary comparing the providers: "
            "which is cheapest for different use cases, any notable differences in pricing models, "
            "and anything else worth calling out."
        )
    else:
        prompt = (
            f"Find the exact per-unit pricing for {provider_list}. "
            "I need the actual dollar amounts: $/vCPU/hour, $/GiB RAM/hour, $/GiB storage/month, "
            "per-execution fees, or however they structure their pricing. "
            "Check their pricing page, docs, billing docs, and any blog posts or community comparisons. "
            "If the pricing page requires login, try to find the rates from third-party sources, "
            "blog posts, or documentation. I want exact numbers, not just tier limits. "
            "Present the results in a table if possible."
        )

    print(f"Looking up pricing for: {provider_list}\n")

    with Sandbox(
        "local",
        preset="claude-code",
        env_vars={"ANTHROPIC_API_KEY": api_key},
        workdir=WORKDIR,
    ) as sb:
        claude_args = [
            "-p", prompt,
            "--output-format", "text",
            "--allowedTools", "WebSearch,WebFetch",
        ]
        if args.model:
            claude_args += ["--model", args.model]

        result = sb.execute_command("claude", args=claude_args)

        print(result.stdout)
        if result.stderr:
            print("--- stderr ---", file=sys.stderr)
            print(result.stderr, file=sys.stderr)
        if result.exit_code != 0:
            print(f"(exit code: {result.exit_code})")


if __name__ == "__main__":
    main()
