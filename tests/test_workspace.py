"""Tests for declarative workspace manifests (bespokelabs.sandbox.workspace)."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from bespokelabs.sandbox import (
    File,
    GitRepo,
    LocalDir,
    LocalFile,
    Manifest,
    Sandbox,
    SandboxConfigurationError,
    WorkspaceEntry,
    WorkspaceError,
)
from bespokelabs.sandbox.types import SandboxResult


class _RecordingSandbox:
    """A minimal sandbox stand-in that records calls (for unit-testing entries)."""

    backend_name = "fake"

    def __init__(self, exit_code: int = 0) -> None:
        self.calls: list[tuple] = []
        self._exit_code = exit_code

    def execute_command(self, command: str, args: list[str] | None = None) -> SandboxResult:
        self.calls.append(("exec", command, args))
        return SandboxResult(stdout="", stderr="boom" if self._exit_code else "", exit_code=self._exit_code)

    def write_file(self, path: str, content: bytes | str) -> None:
        self.calls.append(("write", path, content))

    def upload_file(self, local: str, remote: str) -> None:
        self.calls.append(("upload_file", local, remote))

    def upload_dir(self, local, remote: str, *, method: str = "auto") -> int:
        self.calls.append(("upload_dir", str(local), remote, method))
        return 1


def _tree(root: Path) -> Path:
    d = root / "d"
    d.mkdir()
    (d / "a.txt").write_text("AAA")
    sh = d / "run.sh"
    sh.write_text("#!/bin/sh\necho hi\n")
    sh.chmod(0o755)
    return d


class ManifestValidationTests(unittest.TestCase):
    def test_rejects_non_entry_value(self) -> None:
        with self.assertRaises(SandboxConfigurationError):
            Manifest(entries={"x": "not-an-entry"})  # type: ignore[dict-item]

    def test_rejects_blank_destination(self) -> None:
        with self.assertRaises(SandboxConfigurationError):
            Manifest(entries={"  ": File("x")})


class EntryUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_file_writes_content(self) -> None:
        sb = _RecordingSandbox()
        File("hello").materialize(sb, "a/b.txt")
        self.assertIn(("write", "a/b.txt", "hello"), sb.calls)

    def test_local_file_uploads_plain_file_without_chmod(self) -> None:
        p = self.root / "x.txt"
        p.write_text("data")  # not executable
        sb = _RecordingSandbox()
        LocalFile(p).materialize(sb, "dest/x.txt")
        self.assertEqual(sb.calls, [("upload_file", str(p), "dest/x.txt")])

    def test_local_file_preserves_exec_bit_on_upload_only_backend(self) -> None:
        # _RecordingSandbox.upload_file records bytes only (like a cloud backend
        # that doesn't chmod), so the entry must restore the bit via a command.
        p = self.root / "run.sh"
        p.write_text("#!/bin/sh\necho hi\n")
        p.chmod(0o755)
        sb = _RecordingSandbox()
        LocalFile(p).materialize(sb, "bin/run.sh")
        self.assertEqual(sb.calls[0], ("upload_file", str(p), "bin/run.sh"))
        self.assertTrue(
            any(c[0] == "exec" and "chmod +x" in c[2][1] and "bin/run.sh" in c[2][1] for c in sb.calls),
            sb.calls,
        )

    def test_local_file_chmod_failure_raises_workspace_error(self) -> None:
        p = self.root / "run.sh"
        p.write_text("#!/bin/sh\n")
        p.chmod(0o755)
        sb = _RecordingSandbox(exit_code=1)  # the chmod command fails
        with self.assertRaises(WorkspaceError):
            LocalFile(p).materialize(sb, "bin/run.sh")

    def test_local_file_missing_raises_config_error(self) -> None:
        with self.assertRaises(SandboxConfigurationError):
            LocalFile(self.root / "nope.txt").materialize(_RecordingSandbox(), "d")

    def test_local_dir_uploads_with_method(self) -> None:
        d = _tree(self.root)
        sb = _RecordingSandbox()
        LocalDir(d, method="tar").materialize(sb, "skills/d")
        self.assertEqual(sb.calls, [("upload_dir", str(d), "skills/d", "tar")])

    def test_local_dir_missing_raises_config_error(self) -> None:
        with self.assertRaises(SandboxConfigurationError):
            LocalDir(self.root / "nope").materialize(_RecordingSandbox(), "d")

    def test_git_repo_builds_command(self) -> None:
        sb = _RecordingSandbox()
        GitRepo("https://x/y", ref="main").materialize(sb, "repo")
        self.assertEqual(sb.calls[0][0], "exec")
        self.assertEqual(
            sb.calls[0][2][1],
            'mkdir -p "$(dirname repo)" && git clone --depth 1 --branch main https://x/y repo',
        )

    def test_git_repo_without_depth_or_ref(self) -> None:
        sb = _RecordingSandbox()
        GitRepo("https://x/y", depth=None).materialize(sb, "repo")
        self.assertEqual(sb.calls[0][2][1], 'mkdir -p "$(dirname repo)" && git clone https://x/y repo')

    def test_git_repo_creates_nested_parent_dir(self) -> None:
        sb = _RecordingSandbox()
        GitRepo("https://x/y", depth=None).materialize(sb, "work/nested/repo")
        self.assertEqual(
            sb.calls[0][2][1],
            'mkdir -p "$(dirname work/nested/repo)" && git clone https://x/y work/nested/repo',
        )

    def test_git_repo_failure_raises_workspace_error(self) -> None:
        sb = _RecordingSandbox(exit_code=1)
        with self.assertRaises(WorkspaceError) as ctx:
            GitRepo("https://x/y").materialize(sb, "repo")
        self.assertEqual(ctx.exception.op, "materialize")
        self.assertEqual(ctx.exception.backend, "fake")
        self.assertEqual(ctx.exception.context.get("exit_code"), 1)


class ManifestApplyTests(unittest.TestCase):
    def test_apply_returns_count_and_records_in_order(self) -> None:
        sb = _RecordingSandbox()
        n = Manifest(entries={"a.txt": File("1"), "b.txt": File("2")}).apply(sb)
        self.assertEqual(n, 2)
        self.assertEqual([c[1] for c in sb.calls], ["a.txt", "b.txt"])

    def test_wraps_unexpected_error_as_workspace_error(self) -> None:
        class Boom(WorkspaceEntry):
            def materialize(self, sb, dest):
                raise RuntimeError("kaboom")

        with self.assertRaises(WorkspaceError) as ctx:
            Manifest(entries={"x": Boom()}).apply(_RecordingSandbox())
        self.assertEqual(ctx.exception.context.get("dest"), "x")
        self.assertIsInstance(ctx.exception.__cause__, RuntimeError)


class ManifestIntegrationTests(unittest.TestCase):
    """End-to-end materialization on the local backend."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_seeds_files_dir_and_preserves_exec_bit(self) -> None:
        d = _tree(self.root)
        solo = self.root / "solo.txt"
        solo.write_text("SOLO")
        manifest = Manifest(entries={
            "config.json": File('{"k": 1}'),
            "seed/solo.txt": LocalFile(solo),
            "skills/d": LocalDir(d),
        })
        with Sandbox("local", workspace=manifest) as sb:
            self.assertEqual(sb.read_file("config.json").decode(), '{"k": 1}')
            self.assertEqual(sb.read_file("seed/solo.txt").decode(), "SOLO")
            self.assertEqual(sb.read_file("skills/d/a.txt").decode(), "AAA")
            xbit = sb.execute_command("sh", ["-c", "test -x skills/d/run.sh && echo yes || echo no"])
            self.assertEqual(xbit.stdout.strip(), "yes")

    def test_later_entry_overlays_earlier(self) -> None:
        d = _tree(self.root)  # d/a.txt == "AAA"
        manifest = Manifest(entries={
            "x": LocalDir(d),
            "x/a.txt": File("BBB"),  # overwrites the dir's a.txt
        })
        with Sandbox("local", workspace=manifest) as sb:
            self.assertEqual(sb.read_file("x/a.txt").decode(), "BBB")

    def test_workspace_overlays_files_kwarg(self) -> None:
        # files= is written first, then the workspace manifest overlays it.
        manifest = Manifest(entries={"c.txt": File("from-workspace")})
        with Sandbox("local", files={"c.txt": "from-files"}, workspace=manifest) as sb:
            self.assertEqual(sb.read_file("c.txt").decode(), "from-workspace")

    def test_bad_entry_aborts_creation(self) -> None:
        manifest = Manifest(entries={"x": LocalDir(self.root / "does-not-exist")})
        with self.assertRaises(SandboxConfigurationError):
            Sandbox("local", workspace=manifest)

    def test_custom_entry_subclass(self) -> None:
        class Upper(WorkspaceEntry):
            def __init__(self, text: str) -> None:
                self.text = text

            def materialize(self, sb, dest):
                sb.write_file(dest, self.text.upper())

        with Sandbox("local", workspace=Manifest(entries={"u.txt": Upper("hi")})) as sb:
            self.assertEqual(sb.read_file("u.txt").decode(), "HI")

    def test_apply_to_live_sandbox(self) -> None:
        with Sandbox("local") as sb:
            n = Manifest(entries={"live.txt": File("yo")}).apply(sb)
            self.assertEqual(n, 1)
            self.assertEqual(sb.read_file("live.txt").decode(), "yo")

    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_git_repo_clones_into_nested_destination(self) -> None:
        # A real clone into a multi-level dest proves git's missing-parent
        # limitation is handled (the mkdir -p runs first).
        origin = self.root / "origin"
        origin.mkdir()
        (origin / "hello.txt").write_text("hi")
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        }
        subprocess.run(["git", "init", "-q"], cwd=origin, check=True, env=env)
        subprocess.run(["git", "add", "."], cwd=origin, check=True, env=env)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=origin, check=True, env=env)

        manifest = Manifest(entries={"work/nested/repo": GitRepo(f"file://{origin}", depth=None)})
        with Sandbox("local", workspace=manifest) as sb:
            self.assertEqual(sb.read_file("work/nested/repo/hello.txt").decode(), "hi")


if __name__ == "__main__":
    unittest.main()
