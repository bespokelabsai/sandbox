"""Tests for directory transfer: Sandbox.upload_dir / download_dir / build_files_map.

All run against the local backend, which needs no external services.
"""

from __future__ import annotations

import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bespokelabs.sandbox import AsyncSandbox, Sandbox, WorkspaceError, _transfer, build_files_map
from bespokelabs.sandbox.exceptions import SandboxError


def _make_tree(root: Path) -> Path:
    """Create a small tree with a nested dir and one executable file."""
    src = root / "skill"
    (src / "scripts").mkdir(parents=True)
    (src / "reference").mkdir(parents=True)
    (src / "SKILL.md").write_text("---\nname: demo\n---\n")
    greet = src / "scripts" / "greet.sh"
    greet.write_text("#!/usr/bin/env bash\necho hi\n")
    greet.chmod(0o755)
    (src / "reference" / "notes.md").write_text("notes\n")
    return src


class UploadDirTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.src = _make_tree(Path(self._tmp.name))

    def _assert_uploaded(self, sb: Sandbox, dest: str, *, expect_exec: bool) -> None:
        listing = sb.execute_command("sh", ["-c", f"find {dest} -type f | sort"])
        rels = sorted(line[len(dest):].lstrip("/") for line in listing.stdout.splitlines() if line.strip())
        self.assertEqual(rels, ["SKILL.md", "reference/notes.md", "scripts/greet.sh"])
        self.assertEqual(sb.read_file(f"{dest}/SKILL.md").decode(), "---\nname: demo\n---\n")
        xbit = sb.execute_command("sh", ["-c", f"test -x {dest}/scripts/greet.sh && echo yes || echo no"])
        self.assertEqual(xbit.stdout.strip(), "yes" if expect_exec else "no")

    def test_upload_dir_tar_preserves_tree_and_exec_bit(self) -> None:
        sb = Sandbox("local")
        self.addCleanup(sb.destroy)
        n = sb.upload_dir(self.src, "dest/tar", method="tar")
        self.assertEqual(n, 3)
        self._assert_uploaded(sb, "dest/tar", expect_exec=True)

    def test_upload_dir_per_file_preserves_tree(self) -> None:
        sb = Sandbox("local")
        self.addCleanup(sb.destroy)
        n = sb.upload_dir(self.src, "dest/loop", method="per_file")
        self.assertEqual(n, 3)
        self._assert_uploaded(sb, "dest/loop", expect_exec=True)

    def test_upload_dir_auto_defaults_to_tar(self) -> None:
        sb = Sandbox("local")
        self.addCleanup(sb.destroy)
        n = sb.upload_dir(self.src, "dest/auto")
        self.assertEqual(n, 3)
        self._assert_uploaded(sb, "dest/auto", expect_exec=True)

    def test_trailing_slash_on_remote_dir_is_normalized(self) -> None:
        sb = Sandbox("local")
        self.addCleanup(sb.destroy)
        sb.upload_dir(self.src, "dest/slash/", method="tar")
        # No double-slash dir was created.
        self.assertEqual(sb.read_file("dest/slash/SKILL.md").decode().splitlines()[0], "---")

    def test_empty_source_is_a_noop(self) -> None:
        sb = Sandbox("local")
        self.addCleanup(sb.destroy)
        empty = Path(self._tmp.name) / "empty"
        empty.mkdir()
        self.assertEqual(sb.upload_dir(empty, "dest/empty"), 0)

    def test_missing_source_raises(self) -> None:
        sb = Sandbox("local")
        self.addCleanup(sb.destroy)
        with self.assertRaises(NotADirectoryError):
            sb.upload_dir(Path(self._tmp.name) / "nope", "dest/x")

    def test_invalid_method_raises(self) -> None:
        sb = Sandbox("local")
        self.addCleanup(sb.destroy)
        with self.assertRaises(ValueError):
            sb.upload_dir(self.src, "dest/x", method="bogus")

    def test_upload_after_destroy_raises(self) -> None:
        sb = Sandbox("local")
        sb.destroy()
        with self.assertRaises(SandboxError):
            sb.upload_dir(self.src, "dest/x")


class DownloadDirTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.src = _make_tree(Path(self._tmp.name))

    def _roundtrip(self, method: str) -> None:
        out = Path(self._tmp.name) / f"out_{method}"
        with Sandbox("local") as sb:
            sb.upload_dir(self.src, "rt", method="tar")
            n = sb.download_dir("rt", out, method=method)
        rels = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
        self.assertEqual(rels, ["SKILL.md", "reference/notes.md", "scripts/greet.sh"])
        self.assertEqual(n, 3)
        self.assertEqual((out / "reference" / "notes.md").read_text(), "notes\n")

    def test_download_dir_tar_roundtrip(self) -> None:
        self._roundtrip("tar")
        # tar preserves the exec bit on the way back out.
        out = Path(self._tmp.name) / "out_tar"
        self.assertTrue(os.access(out / "scripts" / "greet.sh", os.X_OK))

    def test_download_dir_per_file_roundtrip(self) -> None:
        self._roundtrip("per_file")

    def test_download_creates_local_parent(self) -> None:
        with Sandbox("local") as sb:
            sb.upload_dir(self.src, "rt2", method="tar")
            nested = Path(self._tmp.name) / "a" / "b" / "c"  # does not exist yet
            n = sb.download_dir("rt2", nested, method="tar")
        self.assertEqual(n, 3)
        self.assertTrue((nested / "SKILL.md").is_file())


class BuildFilesMapTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.src = _make_tree(Path(self._tmp.name))

    def test_keys_and_bytes(self) -> None:
        fm = build_files_map(self.src, "remote/dir")
        self.assertEqual(
            sorted(fm),
            ["remote/dir/SKILL.md", "remote/dir/reference/notes.md", "remote/dir/scripts/greet.sh"],
        )
        self.assertEqual(fm["remote/dir/reference/notes.md"], b"notes\n")

    def test_seeds_sandbox_at_creation(self) -> None:
        fm = build_files_map(self.src, "seeded")
        with Sandbox("local", files=fm) as sb:
            self.assertEqual(sb.read_file("seeded/SKILL.md").decode().splitlines()[0], "---")

    def test_missing_source_raises(self) -> None:
        with self.assertRaises(NotADirectoryError):
            build_files_map(Path(self._tmp.name) / "nope", "remote")


class AsyncTransferTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_upload_download_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = _make_tree(Path(tmp))
            out = Path(tmp) / "out"
            async with await AsyncSandbox.create("local") as sb:
                n_up = await sb.upload_dir(src, "rt", method="tar")
                n_down = await sb.download_dir("rt", out, method="tar")
            self.assertEqual(n_up, 3)
            self.assertEqual(n_down, 3)
            self.assertTrue((out / "scripts" / "greet.sh").is_file())


class SymlinkSourceTests(unittest.TestCase):
    """A symlink in the source tree must never pull a host file into the sandbox."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        # A "secret" host file living OUTSIDE the directory being moved.
        self.secret = self.root / "secret.txt"
        self.secret.write_text("TOPSECRET")
        self.src = self.root / "src"
        self.src.mkdir()
        (self.src / "real.txt").write_text("ok")

    def test_iter_files_skips_symlinked_file(self) -> None:
        (self.src / "link.txt").symlink_to(self.secret)
        rels = [rel for _, rel in _transfer._iter_files(self.src)]
        self.assertEqual(rels, ["real.txt"])

    def test_iter_files_does_not_descend_symlinked_dir(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "host.txt").write_text("host-only")
        (self.src / "linkdir").symlink_to(outside, target_is_directory=True)
        rels = [rel for _, rel in _transfer._iter_files(self.src)]
        self.assertEqual(rels, ["real.txt"])

    def test_build_files_map_skips_symlink(self) -> None:
        (self.src / "link.txt").symlink_to(self.secret)
        fm = build_files_map(self.src, "remote")
        self.assertEqual(sorted(fm), ["remote/real.txt"])

    def test_upload_dir_does_not_smuggle_symlinked_file(self) -> None:
        (self.src / "link.txt").symlink_to(self.secret)
        for method in ("tar", "per_file"):
            with Sandbox("local") as sb:
                n = sb.upload_dir(self.src, f"d/{method}", method=method)
                listing = sb.execute_command("sh", ["-c", f"find d/{method} -type f | sort"]).stdout
            self.assertEqual(n, 1, method)
            self.assertIn(f"d/{method}/real.txt", listing)
            self.assertNotIn("link.txt", listing)
            self.assertNotIn("secret", listing)


class SafeExtractTests(unittest.TestCase):
    """download_dir's local extraction must reject members that escape the destination."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.dest = self.root / "out"
        self.dest.mkdir()

    def _symlink_archive(self, linkname: str) -> Path:
        archive = self.root / "evil.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            info = tarfile.TarInfo("evil")
            info.type = tarfile.SYMTYPE
            info.linkname = linkname
            tar.addfile(info)
        return archive

    def test_rejects_absolute_symlink_member(self) -> None:
        target = self.root / "pwned.txt"  # outside dest
        archive = self._symlink_archive(str(target))
        with tarfile.open(archive, "r:gz") as tar:
            with self.assertRaises((tarfile.TarError, WorkspaceError)):
                _transfer._safe_extract(tar, self.dest)
        self.assertFalse(target.exists())

    def test_rejects_parent_traversal_member(self) -> None:
        archive = self.root / "evil2.tar.gz"
        payload = b"x"
        with tarfile.open(archive, "w:gz") as tar:
            info = tarfile.TarInfo("../escape.txt")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        with tarfile.open(archive, "r:gz") as tar:
            with self.assertRaises((tarfile.TarError, WorkspaceError)):
                _transfer._safe_extract(tar, self.dest)
        self.assertFalse((self.root / "escape.txt").exists())

    def test_manual_fallback_rejects_links_without_data_filter(self) -> None:
        # Force the pre-PEP-706 path (interpreters lacking tarfile.data_filter):
        # the manual policy must reject link members outright, even relative ones.
        archive = self._symlink_archive("anything")
        with mock.patch.object(_transfer, "_HAS_DATA_FILTER", False):
            with tarfile.open(archive, "r:gz") as tar:
                with self.assertRaises(WorkspaceError):
                    _transfer._safe_extract(tar, self.dest)

    def test_extracts_normal_members(self) -> None:
        archive = self.root / "good.tar.gz"
        payload = b"hello"
        with tarfile.open(archive, "w:gz") as tar:
            info = tarfile.TarInfo("sub/file.txt")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        with tarfile.open(archive, "r:gz") as tar:
            _transfer._safe_extract(tar, self.dest)
        self.assertEqual((self.dest / "sub" / "file.txt").read_bytes(), payload)


if __name__ == "__main__":
    unittest.main()
