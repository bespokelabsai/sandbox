"""Run a GLM model with OpenCode: ZHIPU_API_KEY=... python examples/opencode_glm.py."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from bespokelabs.sandbox import Sandbox

DEFAULT_WORKDIR = (
    Path(__file__).resolve().parent / ".sandbox_workdir" / "opencode_glm"
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run GLM through OpenCode")
    parser.add_argument("--backend", default="local")
    parser.add_argument("--model", default="zai/glm-4.7")
    parser.add_argument(
        "--prompt",
        default="Create a hello-world Python script in this workspace.",
    )
    parser.add_argument(
        "--workdir",
        default=str(DEFAULT_WORKDIR),
        help="Persistent workspace (default: examples/.sandbox_workdir/opencode_glm)",
    )
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    api_key = os.environ.get("ZHIPU_API_KEY")
    if not api_key:
        parser.error("Set ZHIPU_API_KEY to your Z.AI API key")
    with Sandbox(
        args.backend,
        preset="opencode",
        env_vars={"ZHIPU_API_KEY": api_key},
        workdir=args.workdir,
    ) as sandbox:
        result = sandbox.run_agent(
            args.prompt,
            harness="opencode",
            model=args.model,
            resume=args.resume,
        )
        print(result.text)
        print(f"Tokens: {result.usage.total_tokens}")
        print(f"Total estimated cost: ${result.usage.total_cost_usd:.6f}")
        if result.stderr:
            print(result.stderr)
        raise SystemExit(result.exit_code)


if __name__ == "__main__":
    main()
