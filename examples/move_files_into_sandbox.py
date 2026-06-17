"""
Move local files — Claude Code skills, datasets, configs — into a sandbox.

Single files are easy: `sb.upload_file(local, remote)`, or seed them at
creation with `Sandbox(files={remote: bytes})`. Moving a whole *folder* (a
skill is SKILL.md plus helper scripts and reference data) is what
`sb.upload_dir()` is for — and `sb.download_dir()` brings a tree back out.
This example shows the three strategies the SDK gives you, simplest to most
efficient, and verifies each inside the sandbox.

  files   build_files_map(local, remote) -> Sandbox(files=...). Seeds the tree
          at creation, before preset setup commands run, so setup or an agent
          boots with the files already in place. (Goes through write_file, which
          drops the unix executable bit — see the verification output.)
  upload  sb.upload_dir(local, remote, method="per_file"). On a live sandbox,
          walks the tree and uploads one file at a time. One round-trip per file.
  tar     sb.upload_dir(local, remote, method="tar"). Packs the tree into one
          .tar.gz, uploads that single file, and extracts it in the sandbox. One
          transfer, and tar restores the directory structure and executable bits.

`method="auto"` (the default) is tar when the sandbox has it, else per_file.
upload_dir/download_dir/build_files_map live in the SDK, so this is real usage,
not copy-paste: point --src at any directory — a dataset or config tree, not
just skills. For a single loose file, call sb.upload_file() directly.

Paths are kept relative so they resolve the same everywhere: under the workdir
on the local backend (which is also $HOME, so `.claude/skills/<name>` is exactly
where a local Claude Code agent looks) and under the home dir on cloud backends.

Usage:
    # zero setup — local backend, all three methods, a generated demo skill
    python examples/move_files_into_sandbox.py

    # move one of your real skills with the tar method
    python examples/move_files_into_sandbox.py --src ~/.claude/skills/my-skill --method tar

    # target a cloud backend (install its extra + set its key first)
    python examples/move_files_into_sandbox.py --backend daytona

Prerequisites:
    The local backend needs nothing. For a cloud --backend, install the extra
    and set the provider key, e.g. pip install 'bespokelabs-sandbox[daytona]'
    and DAYTONA_API_KEY.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from bespokelabs.sandbox import Sandbox, build_files_map

# --------------------------------------------------------------------------
# Demo: generate a small but realistic skill, then move it in three ways.
# --------------------------------------------------------------------------


def make_demo_skill(root: Path) -> Path:
    """Create a tiny, realistic skill folder under root; return its path."""
    skill = root / "demo-skill"
    (skill / "scripts").mkdir(parents=True)
    (skill / "reference").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        "name: demo-skill\n"
        "description: A tiny demo skill used to show moving skill files into a sandbox.\n"
        "---\n\n"
        "# Demo Skill\n\n"
        "Run `scripts/greet.sh` and consult `reference/notes.md`.\n"
    )
    greet = skill / "scripts" / "greet.sh"
    greet.write_text('#!/usr/bin/env bash\necho "hello from the demo skill"\n')
    greet.chmod(0o755)  # executable; watch which methods preserve this bit
    (skill / "reference" / "notes.md").write_text(
        "# Notes\n\nSupporting reference data for the demo skill.\n"
    )
    return skill


def verify(sb: Sandbox, dest: str) -> None:
    """Prove the tree landed: list files, read one back, check the exec bit."""
    listing = sb.execute_command("bash", args=["-c", f"find '{dest}' -type f | sort"])
    for line in listing.stdout.splitlines():
        print("   ", line)
    try:
        head = sb.read_file(f"{dest}/SKILL.md").decode().splitlines()
        print("    SKILL.md[0]:", head[0] if head else "(empty)")
    except Exception:
        pass
    # Only report the exec bit when the file actually exists — a real --src
    # skill may not ship a scripts/greet.sh, and "no" would be misleading.
    xbit = sb.execute_command(
        "bash",
        args=[
            "-c",
            f'f="{dest}/scripts/greet.sh"; '
            'if [ -e "$f" ]; then test -x "$f" && echo yes || echo no; fi',
        ],
    )
    if xbit.stdout.strip():
        print("    scripts/greet.sh executable:", xbit.stdout.strip())


def run_method(method: str, backend: str, preset: str | None, src_dir: Path, dest: str) -> None:
    print(f"== method: {method} ==")
    opts = {"preset": preset} if preset else {}
    if method == "files":
        files = build_files_map(src_dir, dest)
        with Sandbox(backend, files=files, **opts) as sb:
            print(f"  seeded {len(files)} files at creation via files=")
            verify(sb, dest)
    elif method == "upload":
        with Sandbox(backend, **opts) as sb:
            n = sb.upload_dir(src_dir, dest, method="per_file")
            print(f"  uploaded {n} files via sb.upload_dir(method='per_file')")
            verify(sb, dest)
    elif method == "tar":
        with Sandbox(backend, **opts) as sb:
            n = sb.upload_dir(src_dir, dest, method="tar")
            print(f"  uploaded {n} files via sb.upload_dir(method='tar')")
            verify(sb, dest)
            # The same tar trick runs in reverse — pull the tree back out.
            with tempfile.TemporaryDirectory(prefix="sbx_dl_") as out:
                got = sb.download_dir(dest, out, method="tar")
                print(f"  round-tripped {got} files back via sb.download_dir()")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Move a directory (e.g. a Claude Code skill) into a sandbox"
    )
    parser.add_argument("--backend", default="local", help="Sandbox backend (default: local)")
    parser.add_argument(
        "--preset",
        default=None,
        help="Optional preset, e.g. claude-code (adds setup; needed only to actually run an agent)",
    )
    parser.add_argument(
        "--src",
        default=None,
        help="Local directory to move in (default: a generated demo skill)",
    )
    parser.add_argument(
        "--dest-root",
        default=".claude/skills",
        help="Remote parent dir for the directory (default: .claude/skills)",
    )
    parser.add_argument(
        "--method", choices=["files", "upload", "tar", "all"], default="all"
    )
    args = parser.parse_args()

    tmp: tempfile.TemporaryDirectory | None = None
    if args.src:
        src_dir = Path(args.src).expanduser().resolve()
        if not src_dir.is_dir():
            sys.exit(f"--src is not a directory: {src_dir}")
    else:
        tmp = tempfile.TemporaryDirectory(prefix="sbx_skill_src_")
        src_dir = make_demo_skill(Path(tmp.name))

    dest = f"{args.dest_root.rstrip('/')}/{src_dir.name}"
    methods = ["files", "upload", "tar"] if args.method == "all" else [args.method]

    print(f"Source:          {src_dir}")
    print(f"Sandbox dest:    {dest}")
    print(f"Backend:         {args.backend}{' + preset ' + args.preset if args.preset else ''}\n")

    try:
        for method in methods:
            run_method(method, args.backend, args.preset, src_dir, dest)
    finally:
        if tmp:
            tmp.cleanup()

    print("Done. The same helpers move any directory — datasets, configs, not just skills.")


if __name__ == "__main__":
    main()
