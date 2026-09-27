"""Launch a persistent local sandbox agent that wakes on Envy messages.

Set ENVY_USER_TOKEN once. Configure CODEX_API_KEY (Codex) or
ANTHROPIC_API_KEY (Claude) and install that CLI. This example does not
install software or modify permissions. Ctrl-C finishes the current delivery.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
from pathlib import Path

from bespokelabs.sandbox import Sandbox
from bespokelabs.sandbox.envy import EnvySupervisor, SandboxConversation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument(
        "--harness", choices=["codex", "claude-code"], default="codex"
    )
    parser.add_argument(
        "--url", default=os.environ.get("ENVY_URL", "http://127.0.0.1:8790")
    )
    parser.add_argument(
        "--workdir", required=True, help="Dedicated persistent agent workspace"
    )
    parser.add_argument(
        "--state",
        required=True,
        help="Private supervisor state file, outside the workspace",
    )
    parser.add_argument(
        "--channel",
        action="append",
        default=[],
        help="Subscribe to a channel; otherwise mentions and followed threads only",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    with EnvySupervisor(
        name=args.name, state_path=args.state, url=args.url
    ) as supervisor:
        for channel in args.channel:
            supervisor.subscribe_channel(channel)
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: supervisor.stop())
        with Sandbox(
            "local",
            workdir=str(Path(args.workdir).expanduser().resolve()),
            timeout_secs=1800,
        ) as sandbox:
            print(
                f"Listening for @{args.name}. Mention it in Envy to start a turn.",
                flush=True,
            )
            supervisor.run(SandboxConversation(sandbox, harness=args.harness))


if __name__ == "__main__":
    main()
