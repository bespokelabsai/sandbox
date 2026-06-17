"""
Upload a Claude Code *skill* into a sandbox, then have Claude Code use it.

A skill is a folder under `.claude/skills/<name>/` holding a `SKILL.md` with
YAML frontmatter (a `name` and a `description`). Claude Code auto-discovers
skills there and invokes them when a request matches the description. This
example ships one skill — `talk-like-a-pirate.md` — uploads it into a sandbox
as `.claude/skills/talk-like-a-pirate/SKILL.md`, and asks Claude Code to use
it to write a short pirate monologue.

The upload is the point: skills live on disk, so getting one into a sandbox is
exactly the "move local files in" problem. Here the skill is a single file, so
`sb.upload_file()` (renaming it to SKILL.md on the way in) is enough. A skill
with helper scripts or reference data is a whole directory — use
`sb.upload_dir()` instead (see move_files_into_sandbox.py).

On the local backend the sandbox workdir is also $HOME, so the relative path
`.claude/skills/...` is exactly where a local Claude Code process looks.

Usage:
    # Local backend (default). The skill is always uploaded and verified;
    # Claude only runs if the `claude` CLI is installed and ANTHROPIC_API_KEY
    # is set — otherwise the example stops after verifying the upload.
    python examples/talk_like_a_pirate.py

    # Cloud backend — the preset installs Claude Code for you:
    python examples/talk_like_a_pirate.py --backend daytona

Prerequisites:
    Local:  npm install -g @anthropic-ai/claude-code   + ANTHROPIC_API_KEY
    Cloud:  pip install 'bespokelabs-sandbox[daytona]'  + DAYTONA_API_KEY
            + ANTHROPIC_API_KEY
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from bespokelabs.sandbox import Sandbox

SKILL_NAME = "talk-like-a-pirate"
SKILL_FILE = Path(__file__).parent / f"{SKILL_NAME}.md"

# Claude Code discovers skills at .claude/skills/<name>/SKILL.md. The path is
# kept relative so it resolves under the workdir (local backend, which is also
# $HOME) and under the home directory on cloud backends.
REMOTE_SKILL_PATH = f".claude/skills/{SKILL_NAME}/SKILL.md"

PROMPT = (
    "Use your talk-like-a-pirate skill to write a brief monologue — 3 to 4 "
    "sentences — in which a pirate captain introduces themselves and their "
    "ship. Output only the monologue, nothing else."
)


def upload_and_verify(sb: Sandbox) -> None:
    """Upload the skill into the sandbox and confirm it landed."""
    # A skill is just a directory under .claude/skills/. This one is a single
    # file, so upload_file (renaming it to SKILL.md) does the job; a skill with
    # helper scripts or data is a folder — use sb.upload_dir(local, remote).
    sb.upload_file(str(SKILL_FILE), REMOTE_SKILL_PATH)

    landed = sb.read_file(REMOTE_SKILL_PATH).decode()
    print(f"Uploaded {SKILL_FILE.name} -> {REMOTE_SKILL_PATH}")
    print(f"  SKILL.md starts with: {landed.splitlines()[0]!r}")
    listing = sb.execute_command("bash", args=["-c", "find .claude/skills -type f | sort"])
    for line in listing.stdout.splitlines():
        print("   ", line)


def run_claude(sb: Sandbox) -> int:
    """Prompt Claude Code to use the uploaded skill; print the monologue."""
    print("\nAsking Claude Code to use the skill...\n")
    # -p: non-interactive single prompt. --output-format text: plain text out.
    result = sb.execute_command("claude", args=["-p", PROMPT, "--output-format", "text"])
    print("--- pirate monologue ---")
    print(result.stdout.strip())
    if result.stderr:
        print("--- stderr ---", file=sys.stderr)
        print(result.stderr, file=sys.stderr)
    return result.exit_code


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upload a skill into a sandbox and have Claude Code use it"
    )
    parser.add_argument("--backend", default="local", help="Sandbox backend (default: local)")
    args = parser.parse_args()

    if not SKILL_FILE.is_file():
        sys.exit(f"Skill file not found: {SKILL_FILE}")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    env_vars = {"ANTHROPIC_API_KEY": api_key} if api_key else {}

    # Cloud backends install Claude Code via the preset; the local backend
    # expects the `claude` CLI to already be on PATH.
    opts: dict = {"env_vars": env_vars}
    if args.backend != "local":
        opts["preset"] = "claude-code"

    print(f"Backend: {args.backend}")
    with Sandbox(args.backend, **opts) as sb:
        upload_and_verify(sb)

        # Everything above needs no API key. Only run Claude if we actually can.
        missing = []
        if not api_key:
            missing.append("ANTHROPIC_API_KEY")
        if args.backend == "local" and not shutil.which("claude"):
            missing.append("the `claude` CLI (npm install -g @anthropic-ai/claude-code)")
        if missing:
            print(
                "\nSkill uploaded and verified. To watch Claude Code use it, provide: "
                + ", ".join(missing)
                + "."
            )
            return

        exit_code = run_claude(sb)
        if exit_code != 0:
            print(f"\n(claude exited with code {exit_code})", file=sys.stderr)
            sys.exit(exit_code)


if __name__ == "__main__":
    main()
