"""Tests for session_state/resume, backend_options, and declarative workspace."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from bespokelabs.sandbox import (
    Sandbox,
    SandboxClient,
    SandboxError,
    SandboxSessionState,
)
from bespokelabs.sandbox.exceptions import FeatureNotSupportedError
from bespokelabs.sandbox.types import SandboxResult


class SessionStateSerializationTests(unittest.TestCase):
    def test_roundtrip_json(self) -> None:
        state = SandboxSessionState(backend="docker", data={"container_id": "abc", "timeout": 600})
        restored = SandboxSessionState.from_json(state.to_json())
        self.assertEqual(restored.backend, "docker")
        self.assertEqual(restored.data["container_id"], "abc")


class LocalResumeTests(unittest.TestCase):
    """Local sandboxes reattach by workdir; state survives a fresh client."""

    def test_resume_reattaches_to_same_workdir(self) -> None:
        with tempfile.TemporaryDirectory() as workdir:
            sb = Sandbox("local", workdir=workdir)
            sb.write_file("/marker.txt", "persisted")

            state = sb.session_state()
            self.assertEqual(state.backend, "local")

            # Reattach from raw JSON, as another process would.
            resumed = Sandbox.resume(SandboxSessionState.from_json(state.to_json()))
            self.addCleanup(resumed.destroy)

            self.assertEqual(resumed.read_file("/marker.txt"), b"persisted")
            result = resumed.execute_command("cat /marker.txt")
            self.assertEqual(result.stdout.strip(), "persisted")

    def test_resume_via_client(self) -> None:
        with tempfile.TemporaryDirectory() as workdir:
            sb = Sandbox("local", workdir=workdir, env_vars={"FOO": "bar"})
            state = sb.session_state()

            resumed = SandboxClient("local").resume(state)
            self.addCleanup(resumed.destroy)

            # env overlay is carried through resume
            result = resumed.execute_command("echo $FOO")
            self.assertEqual(result.stdout.strip(), "bar")

    def test_resume_missing_workdir_raises(self) -> None:
        state = SandboxSessionState(backend="local", data={"workdir": "/nonexistent/xyz"})
        with self.assertRaises(SandboxError):
            Sandbox.resume(state)

    def test_session_state_does_not_leak_host_environ(self) -> None:
        with tempfile.TemporaryDirectory() as workdir:
            sb = Sandbox("local", workdir=workdir, env_vars={"ONLY_THIS": "1"})
            self.addCleanup(sb.destroy)
            env = sb.session_state().data["env_vars"]
            self.assertEqual(env, {"ONLY_THIS": "1"})


class BackendMismatchTests(unittest.TestCase):
    def test_resume_wrong_backend_raises(self) -> None:
        state = SandboxSessionState(backend="docker", data={"container_id": "x"})
        with self.assertRaises(SandboxError):
            SandboxClient("local").resume(state)


class RayUnsupportedTests(unittest.TestCase):
    def test_session_state_not_supported(self) -> None:
        from bespokelabs.sandbox.backends.ray import RaySession

        session = object.__new__(RaySession)
        with self.assertRaises(FeatureNotSupportedError):
            session.session_state()


class DeclarativeWorkspaceTests(unittest.TestCase):
    """files= and git_repo= materialize before setup runs."""

    def test_files_written_on_create(self) -> None:
        sb = Sandbox(
            "local",
            files={"/app/config.json": '{"k": 1}', "/app/data.bin": b"\x00\x01"},
        )
        self.addCleanup(sb.destroy)

        self.assertEqual(sb.read_file("/app/config.json"), b'{"k": 1}')
        self.assertEqual(sb.read_file("/app/data.bin"), b"\x00\x01")

    @unittest.skipUnless(shutil.which("git"), "git not available")
    def test_git_repo_cloned_on_create(self) -> None:
        # Build a tiny local repo to clone (file:// avoids network).
        with tempfile.TemporaryDirectory() as origin:
            subprocess.run(["git", "init", "-q", origin], check=True)
            with open(os.path.join(origin, "hello.txt"), "w") as f:
                f.write("from-repo")
            env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
            subprocess.run(["git", "-C", origin, "add", "."], check=True)
            subprocess.run(["git", "-C", origin, "commit", "-qm", "init"], check=True, env=env)

            sb = Sandbox("local", git_repo=f"file://{origin}")
            self.addCleanup(sb.destroy)

            repo_name = os.path.basename(origin)
            self.assertEqual(sb.read_file(f"/{repo_name}/hello.txt"), b"from-repo")

    def test_git_clone_failure_destroys_sandbox(self) -> None:
        # A recording session whose git clone "fails"; create must surface
        # the error and destroy the partially-built sandbox.
        session = mock.MagicMock()
        session.execute_command.return_value = SandboxResult(exit_code=128, stderr="boom")

        client = mock.MagicMock()
        client.create.return_value = session

        with mock.patch.dict(
            "bespokelabs.sandbox.backends.BACKENDS", {"fake": lambda: client}, clear=False,
        ):
            with self.assertRaises(SandboxError):
                Sandbox("fake", git_repo="https://example.com/x.git")
            session.destroy.assert_called_once()


class AsyncResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_resume_reattaches(self) -> None:
        from bespokelabs.sandbox import AsyncSandbox, AsyncSandboxClient

        with tempfile.TemporaryDirectory() as workdir:
            sb = await AsyncSandbox.create("local", workdir=workdir)
            await sb.execute_command("echo async-persist > /m.txt")
            state = sb.session_state()  # sync — no I/O

            resumed = await AsyncSandboxClient("local").resume(state)
            self.addAsyncCleanup(resumed.destroy)
            self.assertEqual(await resumed.read_file("/m.txt"), b"async-persist\n")


class BackendOptionsTests(unittest.TestCase):
    def test_backend_options_threaded_into_config(self) -> None:
        sb = Sandbox("local", backend_options={"unused": "ok"})
        self.addCleanup(sb.destroy)
        self.assertEqual(sb._config.backend_options, {"unused": "ok"})

    def test_backend_options_forwarded_to_docker_run(self) -> None:
        # Verify the escape hatch reaches containers.run kwargs.
        from bespokelabs.sandbox.backends.docker import DockerClient
        from bespokelabs.sandbox.types import SandboxConfig

        fake_docker = mock.MagicMock()
        fake_container = mock.MagicMock()
        fake_docker.from_env.return_value.containers.run.return_value = fake_container

        client = object.__new__(DockerClient)
        client._docker = fake_docker
        client._client = None
        import threading
        client._connect_lock = threading.Lock()

        client.create(SandboxConfig(backend="docker", backend_options={"hostname": "myhost"}))

        _, kwargs = fake_docker.from_env.return_value.containers.run.call_args
        self.assertEqual(kwargs["hostname"], "myhost")


if __name__ == "__main__":
    unittest.main()
