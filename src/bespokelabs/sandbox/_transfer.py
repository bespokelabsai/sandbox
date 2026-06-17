"""Directory-transfer helpers shared by the sync and async Sandbox.

Backends expose only single-file primitives (``write_file`` /
``upload_file`` / ``read_file`` / ``download_file``) plus
``execute_command``.  These functions build *directory*-level transfer
on top of those primitives, so one implementation works across every
backend without per-backend code.

``upload_dir`` / ``download_dir`` move a whole tree to or from a *live*
sandbox.  By default they pack the tree into a single ``.tar.gz`` and
unpack it on the far side: one transfer that restores the directory
layout and the unix executable bits.  When ``tar`` is unavailable they
fall back to a file-by-file loop.  ``build_files_map`` serves the
different, pre-boot case of seeding ``Sandbox(files=...)`` before the
sandbox exists.

Paths are kept relative where the caller passes them relative, so they
resolve under the workdir on the local backend and under the home dir
on cloud backends.
"""

from __future__ import annotations

import io
import os
import shlex
import tarfile
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

from bespokelabs.sandbox.types import SandboxResult

_METHODS = ("auto", "tar", "per_file")

# PEP 706 added the "data" extraction filter and exposed ``tarfile.data_filter``.
# It ships on 3.12+ and was backported to 3.8.17+, 3.9.17+, 3.10.12+, 3.11.4+ —
# so probe the attribute rather than the (major, minor) version, which would
# miss the patched 3.10 / 3.11 interpreters that *do* have the filter.
_HAS_DATA_FILTER = hasattr(tarfile, "data_filter")


class _Transferable(Protocol):
    """The slice of the Sandbox surface the transfer helpers rely on."""

    def execute_command(self, command: str, args: list[str] | None = None) -> SandboxResult: ...
    def write_file(self, path: str, content: bytes | str) -> None: ...
    def upload_file(self, local_path: str, remote_path: str) -> None: ...
    def download_file(self, remote_path: str, local_path: str) -> None: ...


def _iter_files(local_dir: Path) -> Iterator[tuple[Path, str]]:
    """Yield ``(abs_local_path, relative_posix_path)`` for every regular file under *local_dir*.

    Symlinks are skipped — both symlinked files and symlinked directories.
    The walk therefore never follows a link out of *local_dir*, so a tree
    that happens to contain a symlink to ``/etc/passwd`` (or any other host
    path) cannot smuggle that file's contents into the sandbox.  Directories
    themselves are not yielded (empty dirs are skipped); the relative path
    uses forward slashes so it is identical on every host.
    """
    matches: list[tuple[Path, str]] = []
    # os.walk(followlinks=False) does not descend into symlinked directories;
    # prune them from `dirs` as well so nothing *under* a link is considered,
    # then drop any symlinked file entries before reading them.
    for root, dirs, files in os.walk(local_dir, followlinks=False):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        for name in files:
            abs_path = Path(root) / name
            if abs_path.is_symlink():
                continue
            matches.append((abs_path, abs_path.relative_to(local_dir).as_posix()))
    matches.sort(key=lambda pair: pair[1])
    yield from matches


def _check_method(method: str) -> None:
    if method not in _METHODS:
        raise ValueError(f"method must be one of {_METHODS}, got {method!r}")


def _has_tar(sb: _Transferable) -> bool:
    """Return True if ``tar`` is on PATH inside the sandbox."""
    res = sb.execute_command("sh", ["-c", "command -v tar >/dev/null 2>&1 && echo yes || echo no"])
    return res.stdout.strip() == "yes"


def build_files_map(local_dir: str | Path, remote_dir: str) -> dict[str, bytes]:
    """Build a ``{remote_path: bytes}`` map for ``Sandbox(files=...)``.

    Use this to seed a directory tree *at creation* — the files land
    before preset setup commands run, so setup or an agent boots with
    them already in place.  For a live sandbox, prefer
    :meth:`Sandbox.upload_dir`.

    Note: ``files=`` writes go through ``write_file`` and do **not**
    preserve the unix executable bit; use ``upload_dir`` if that matters.
    Symlinks in the tree are skipped (links are never followed off-tree).
    """
    base = _normalize(local_dir, remote_dir)
    return {f"{base[1]}/{rel}": abs_path.read_bytes() for abs_path, rel in _iter_files(base[0])}


def upload_dir(sb: _Transferable, local_dir: str | Path, remote_dir: str, *, method: str = "auto") -> int:
    """Upload a local directory tree into a live sandbox; return the file count.

    See :meth:`Sandbox.upload_dir` for the user-facing contract.
    """
    _check_method(method)
    src, base = _normalize(local_dir, remote_dir)
    files = list(_iter_files(src))
    if not files:
        return 0
    use_tar = method == "tar" or (method == "auto" and _has_tar(sb))
    if use_tar:
        _upload_tar(sb, base, files)
    else:
        for abs_path, rel in files:
            sb.upload_file(str(abs_path), f"{base}/{rel}")
    return len(files)


def download_dir(sb: _Transferable, remote_dir: str, local_dir: str | Path, *, method: str = "auto") -> int:
    """Download a directory tree out of a live sandbox; return the file count.

    See :meth:`Sandbox.download_dir` for the user-facing contract.
    """
    _check_method(method)
    base = remote_dir.rstrip("/")
    dest = Path(local_dir).expanduser()
    use_tar = method == "tar" or (method == "auto" and _has_tar(sb))
    if use_tar:
        return _download_tar(sb, base, dest)
    return _download_per_file(sb, base, dest)


# -- internals ----------------------------------------------------------------


def _normalize(local_dir: str | Path, remote_dir: str) -> tuple[Path, str]:
    """Validate *local_dir* is a directory and strip a trailing slash off *remote_dir*."""
    src = Path(local_dir).expanduser()
    if not src.is_dir():
        raise NotADirectoryError(f"local_dir is not a directory: {src}")
    return src, remote_dir.rstrip("/")


def _upload_tar(sb: _Transferable, base: str, files: list[tuple[Path, str]]) -> None:
    """Pack *files* into one gzipped tar, upload it, and extract it under *base*."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for abs_path, rel in files:
            tar.add(str(abs_path), arcname=rel)

    archive = f".sbx_upload_{uuid.uuid4().hex}.tar.gz"
    sb.write_file(archive, buf.getvalue())
    qd, qa = shlex.quote(base), shlex.quote(archive)
    # Always remove the staged archive, then propagate the extract's exit code.
    script = f"mkdir -p {qd} && tar -xzf {qa} -C {qd}; rc=$?; rm -f {qa}; exit $rc"
    res = sb.execute_command("sh", ["-c", script])
    if res.exit_code != 0:
        raise RuntimeError(f"tar extract failed (exit {res.exit_code}): {res.stderr.strip()}")


def _download_tar(sb: _Transferable, base: str, dest: Path) -> int:
    """Tar the remote tree, download the one archive, and extract it into *dest*."""
    archive = f".sbx_download_{uuid.uuid4().hex}.tar.gz"
    qd, qa = shlex.quote(base), shlex.quote(archive)
    res = sb.execute_command("sh", ["-c", f"tar -czf {qa} -C {qd} ."])
    if res.exit_code != 0:
        raise RuntimeError(f"tar pack of '{base}' failed (exit {res.exit_code}): {res.stderr.strip()}")

    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sbx_dl_") as tmp:
        local_archive = os.path.join(tmp, "archive.tar.gz")
        sb.download_file(archive, local_archive)
        sb.execute_command("sh", ["-c", f"rm -f {qa}"])
        with tarfile.open(local_archive, "r:gz") as tar:
            members = [m for m in tar.getmembers() if m.isfile()]
            _safe_extract(tar, dest)
    return len(members)


def _download_per_file(sb: _Transferable, base: str, dest: Path) -> int:
    """Fallback: enumerate remote files with ``find`` and download each one."""
    res = sb.execute_command("sh", ["-c", f"find {shlex.quote(base)} -type f"])
    if res.exit_code != 0:
        raise RuntimeError(f"listing '{base}' failed (exit {res.exit_code}): {res.stderr.strip()}")
    count = 0
    for remote_path in res.stdout.splitlines():
        remote_path = remote_path.strip()
        if not remote_path:
            continue
        rel = remote_path[len(base):].lstrip("/")
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        sb.download_file(remote_path, str(target))
        count += 1
    return count


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract *tar* into *dest*, refusing any member that could escape *dest*.

    When the interpreter has the PEP 706 ``data`` filter, defer to it — it
    blocks absolute paths, ``..`` traversal, and links/devices that point
    outside the destination, which a name-only check cannot (it never sees
    ``member.linkname``).  On the few older interpreters without the filter,
    fall back to a conservative policy: reject links and device/special
    members outright, and any member whose path resolves outside *dest*.
    """
    if _HAS_DATA_FILTER:
        tar.extractall(dest, filter="data")
        return

    dest = dest.resolve()
    for member in tar.getmembers():
        if member.issym() or member.islnk():
            raise RuntimeError(f"refusing link member in archive: {member.name!r}")
        if member.isdev():
            raise RuntimeError(f"refusing device/special member in archive: {member.name!r}")
        target = (dest / member.name).resolve()
        if target != dest and dest not in target.parents:
            raise RuntimeError(f"refusing path-traversal in archive member: {member.name!r}")
    tar.extractall(dest)
