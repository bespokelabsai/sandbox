"""Fetch current sandbox pricing from the web and update pricing.json.

Spins up a local sandbox with Claude Code, asks it to web-search each
cloud provider's pricing page, and writes structured results back to
src/bespokelabs/sandbox/pricing.json.

Usage:
    ANTHROPIC_API_KEY=sk-... python examples/update_pricing.py
    ANTHROPIC_API_KEY=sk-... python examples/update_pricing.py --providers e2b,modal
    ANTHROPIC_API_KEY=sk-... python examples/update_pricing.py --dry-run
"""

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

from bespokelabs.sandbox import Sandbox

PRICING_JSON = Path(__file__).resolve().parent.parent / "src" / "bespokelabs" / "sandbox" / "pricing.json"
WORKDIR = os.path.join(os.path.dirname(__file__), ".sandbox_workdir")

# Cloud providers whose pricing we fetch. Local backends (local, docker,
# safehouse, ray) are always free and don't need web lookups.
CLOUD_PROVIDERS = {
    "e2b": {
        "name": "E2B",
        "url": "https://e2b.dev/pricing",
        "description": "E2B (e2b.dev) code interpreter sandboxes",
    },
    "daytona": {
        "name": "Daytona",
        "url": "https://www.daytona.io/pricing",
        "description": "Daytona (daytona.io) cloud sandboxes",
    },
    "modal": {
        "name": "Modal",
        "url": "https://modal.com/pricing",
        "description": "Modal (modal.com) serverless compute sandboxes",
    },
    "tensorlake": {
        "name": "Tensorlake",
        "url": "https://tensorlake.ai",
        "description": "Tensorlake (tensorlake.ai) sandboxes",
    },
}

PROMPT_TEMPLATE = """Find the exact, current pricing for {name} ({description}).

Check their pricing page at {url} and any other relevant docs or blog posts.
I need the actual dollar amounts — not just tier names.

Return ONLY a JSON object (no markdown fences, no explanation) with exactly these fields:
{{
  "vcpu_per_hour_usd": <number>,
  "ram_gib_per_hour_usd": <number>,
  "storage_gib_per_month_usd": <number>,
  "per_execution_usd": <number>,
  "free_tier": <string or null>,
  "pricing_url": "<url you found pricing on>",
  "notes": "<one-line summary of pricing model>"
}}

Rules:
- Use 0.0 if a dimension is not charged separately (e.g. RAM included in vCPU price).
- Use the base/default tier price, not enterprise pricing.
- If you cannot find an exact number, use your best estimate and note it in "notes".
- Return ONLY the JSON object, nothing else.
"""


def fetch_provider_pricing(sb, provider_key: str) -> dict | None:
    """Ask Claude Code inside the sandbox to look up pricing for one provider."""
    provider = CLOUD_PROVIDERS[provider_key]
    prompt = PROMPT_TEMPLATE.format(**provider)

    result = sb.execute_command(
        "claude",
        args=[
            "-p", prompt,
            "--output-format", "text",
            "--allowedTools", "WebSearch,WebFetch",
        ],
    )

    if result.exit_code != 0:
        print(f"  WARNING: Claude Code exited {result.exit_code} for {provider_key}", file=sys.stderr)
        if result.stderr:
            print(f"  stderr: {result.stderr[:200]}", file=sys.stderr)
        return None

    # Parse JSON from the response — strip any accidental markdown fences
    text = result.stdout.strip()
    if text.startswith("```"):
        text = "\n".join(text.split("\n")[1:])
    if text.endswith("```"):
        text = "\n".join(text.split("\n")[:-1])
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print(f"  WARNING: Could not parse JSON for {provider_key}", file=sys.stderr)
        print(f"  Raw output: {text[:300]}", file=sys.stderr)
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch sandbox pricing and update pricing.json")
    parser.add_argument(
        "--providers",
        default=",".join(CLOUD_PROVIDERS),
        help=f"Comma-separated providers to update (choices: {', '.join(CLOUD_PROVIDERS)})",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print fetched pricing without writing to file")
    parser.add_argument("--model", default=None, help="Claude model to use (e.g. sonnet, haiku)")
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        sys.exit("Set ANTHROPIC_API_KEY before running this script.")

    providers = [p.strip() for p in args.providers.split(",") if p.strip()]
    unknown = [p for p in providers if p not in CLOUD_PROVIDERS]
    if unknown:
        sys.exit(f"Unknown provider(s): {', '.join(unknown)}. Choose from: {', '.join(CLOUD_PROVIDERS)}")

    # Load existing pricing
    with open(PRICING_JSON) as f:
        pricing = json.load(f)

    print(f"Fetching pricing for: {', '.join(providers)}")
    print(f"Using sandbox with Claude Code preset...\n")

    with Sandbox("local", preset="claude-code", env_vars={"ANTHROPIC_API_KEY": api_key}, workdir=WORKDIR) as sb:
        if args.model:
            # Set model via env var for subsequent claude calls
            sb.execute_command("export", args=[f"CLAUDE_MODEL={args.model}"])

        for provider_key in providers:
            print(f"  Fetching {provider_key}...")
            result = fetch_provider_pricing(sb, provider_key)
            if result is None:
                print(f"  SKIPPED {provider_key} (fetch failed)\n")
                continue

            # Validate required fields
            expected_fields = {"vcpu_per_hour_usd", "ram_gib_per_hour_usd", "storage_gib_per_month_usd",
                               "per_execution_usd", "free_tier", "pricing_url", "notes"}
            missing = expected_fields - set(result.keys())
            if missing:
                print(f"  WARNING: Missing fields for {provider_key}: {missing}", file=sys.stderr)

            pricing["backends"][provider_key] = result
            print(f"  OK: {provider_key} -> ${result.get('vcpu_per_hour_usd', '?')}/vCPU-hr\n")

    pricing["last_updated"] = date.today().isoformat()

    if args.dry_run:
        print("\n--- Dry run: would write ---")
        print(json.dumps(pricing, indent=2))
    else:
        with open(PRICING_JSON, "w") as f:
            json.dump(pricing, f, indent=2)
            f.write("\n")
        print(f"\nUpdated {PRICING_JSON}")


if __name__ == "__main__":
    sys.exit(main() or 0)
