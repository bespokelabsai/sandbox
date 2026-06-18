"""Declarative workspace manifests.

A :class:`Manifest` describes a sandbox's initial files as a mapping of
*destination path → entry*, unifying the three imperative ways to seed a
sandbox (``files=``, ``git_repo=``, and post-create ``upload_dir``) into one
typed, ordered, extensible spec::

    from bespokelabs.sandbox import Sandbox, Manifest, GitRepo, LocalDir, File

    with Sandbox("daytona", workspace=Manifest(entries={
        "repo": GitRepo("https://github.com/org/proj", ref="main"),
        ".claude/skills/pirate": LocalDir("~/.claude/skills/talk-like-a-pirate"),
        "config.json": File('{"env": "prod"}'),
    })) as sb:
        ...

Entries materialize in insertion order, so a later entry overlays an earlier
one. Each entry is built on the sandbox's existing primitives (``write_file``,
``upload_file``, ``upload_dir``, ``execute_command``), so manifests work on
every backend. Subclass :class:`WorkspaceEntry` to add custom sources.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from bespokelabs.sandbox.exceptions import SandboxConfigurationError, SandboxError, WorkspaceError

if TYPE_CHECKING:
    from bespokelabs.sandbox.sandbox import Sandbox


class WorkspaceEntry:
    """Base class for one declarative workspace entry.

    Subclass and implement :meth:`materialize` to add a custom source (an
    object-store download, a rendered template, …). It receives the live
    sandbox and *dest*, the entry's key in the :class:`Manifest`.
    """

    def materialize(self, sb: Sandbox, dest: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError


@dataclass
class File(WorkspaceEntry):
    """In-memory content written to *dest*.

    Goes through ``write_file``, which does **not** preserve the unix
    executable bit — use :class:`LocalFile` / :class:`LocalDir` for
    executables.
    """

    content: bytes | str

    def materialize(self, sb: Sandbox, dest: str) -> None:
        sb.write_file(dest, self.content)


@dataclass
class LocalFile(WorkspaceEntry):
    """A single local file uploaded to *dest* (preserves the executable bit)."""

    local_path: str | Path

    def materialize(self, sb: Sandbox, dest: str) -> None:
        src = Path(self.local_path).expanduser()
        if not src.is_file():
            raise SandboxConfigurationError(
                f"LocalFile source is not a file: {src}",
                op="materialize",
                context={"dest": dest, "source": str(src)},
            )
        sb.upload_file(str(src), dest)
        # upload_file is bytes-only on several backends (it does not chmod), so
        # re-apply the executable bit ourselves to honor the documented contract.
        if src.stat().st_mode & 0o111:
            result = sb.execute_command("sh", ["-c", f"chmod +x {shlex.quote(dest)}"])
            if result.exit_code != 0:
                raise WorkspaceError(
                    f"failed to set executable bit on {dest!r} (exit {result.exit_code})",
                    backend=getattr(sb, "backend_name", None),
                    op="materialize",
                    context={"dest": dest, "exit_code": result.exit_code},
                )


@dataclass
class LocalDir(WorkspaceEntry):
    """A local directory tree uploaded under *dest*.

    Preserves structure and executable bits and skips symlinks — see
    :meth:`Sandbox.upload_dir`. ``method`` is forwarded to it.
    """

    local_path: str | Path
    method: str = "auto"

    def materialize(self, sb: Sandbox, dest: str) -> None:
        src = Path(self.local_path).expanduser()
        if not src.is_dir():
            raise SandboxConfigurationError(
                f"LocalDir source is not a directory: {src}",
                op="materialize",
                context={"dest": dest, "source": str(src)},
            )
        sb.upload_dir(src, dest, method=self.method)


@dataclass
class GitRepo(WorkspaceEntry):
    """A git repository cloned into *dest* (requires ``git`` in the sandbox)."""

    repo: str
    ref: str | None = None
    depth: int | None = 1

    def materialize(self, sb: Sandbox, dest: str) -> None:
        cmd = ["git", "clone"]
        if self.depth:
            cmd += ["--depth", str(self.depth)]
        if self.ref:
            cmd += ["--branch", self.ref]
        cmd += [self.repo, dest]
        clone = " ".join(shlex.quote(part) for part in cmd)
        # git clone does not create the destination's parent directories, so
        # make them first — this keeps nested destinations (e.g. "work/repo")
        # working like File / LocalFile / LocalDir do.
        script = f'mkdir -p "$(dirname {shlex.quote(dest)})" && {clone}'
        result = sb.execute_command("sh", ["-c", script])
        if result.exit_code != 0:
            raise WorkspaceError(
                f"git clone of '{self.repo}' failed (exit {result.exit_code})",
                backend=getattr(sb, "backend_name", None),
                op="materialize",
                context={"dest": dest, "exit_code": result.exit_code},
            )


@dataclass
class Manifest:
    """A declarative description of a sandbox's initial workspace.

    ``entries`` maps a destination path to a :class:`WorkspaceEntry`.
    Entries materialize in insertion order (dicts preserve order), so a
    later entry overlays an earlier one. Pass via
    ``Sandbox(workspace=Manifest(...))`` to seed a sandbox at creation, or
    call :meth:`apply` on a live sandbox.
    """

    entries: dict[str, WorkspaceEntry] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for dest, entry in self.entries.items():
            if not isinstance(dest, str) or not dest.strip():
                raise SandboxConfigurationError(f"Manifest destination must be a non-empty string, got {dest!r}")
            if not isinstance(entry, WorkspaceEntry):
                raise SandboxConfigurationError(
                    f"Manifest entry for {dest!r} must be a WorkspaceEntry, got {type(entry).__name__}"
                )

    def apply(self, sb: Sandbox) -> int:
        """Materialize every entry into *sb*, in order. Returns the entry count."""
        for dest, entry in self.entries.items():
            try:
                entry.materialize(sb, dest)
            except SandboxError:
                raise
            except Exception as exc:  # wrap anything unexpected with context
                raise WorkspaceError(
                    f"Failed to materialize {type(entry).__name__} at {dest!r}: {exc}",
                    backend=getattr(sb, "backend_name", None),
                    op="materialize",
                    context={"dest": dest},
                ) from exc
        return len(self.entries)
