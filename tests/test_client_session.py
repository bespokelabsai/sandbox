"""Tests for the SandboxClient / Sandbox (client/session) split."""

from __future__ import annotations

import unittest
from unittest import mock

from bespokelabs.sandbox import Sandbox, SandboxClient, SandboxError
from bespokelabs.sandbox.types import SandboxResult


class SandboxClientLocalTests(unittest.TestCase):
    """End-to-end behavior of SandboxClient on the local backend."""

    def test_create_returns_working_sandbox(self) -> None:
        client = SandboxClient("local")
        with client.create() as sb:
            result = sb.execute_code('print("via client")')
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.stdout.strip(), "via client")

    def test_sandboxes_from_one_client_are_isolated(self) -> None:
        client = SandboxClient("local")
        sb1 = client.create()
        sb2 = client.create()
        self.addCleanup(sb1.destroy)
        self.addCleanup(sb2.destroy)

        sb1.write_file("/only_in_first.txt", "1")

        self.assertEqual(sb1.read_file("/only_in_first.txt"), b"1")
        names = [f.path for f in sb2.list_files("/")]
        self.assertNotIn("/only_in_first.txt", names)

    def test_destroying_one_session_leaves_client_usable(self) -> None:
        client = SandboxClient("local")
        sb1 = client.create()
        sb1.destroy()

        with client.create() as sb2:
            self.assertEqual(sb2.execute_command("echo ok").exit_code, 0)

    def test_unknown_backend_raises_at_construction(self) -> None:
        with self.assertRaises(SandboxError):
            SandboxClient("not-a-backend")

    def test_backend_name_is_normalized(self) -> None:
        self.assertEqual(SandboxClient("local").backend_name, "local")
        self.assertEqual(SandboxClient("  LOCAL ").backend_name, "local")


class _RecordingBackendClient:
    """Fake backend client that records instantiations and creates."""

    instances: list["_RecordingBackendClient"] = []

    def __init__(self) -> None:
        type(self).instances.append(self)
        self.create_calls = 0

    def create(self, config: object) -> mock.MagicMock:
        self.create_calls += 1
        session = mock.MagicMock()
        session.execute_command.return_value = SandboxResult()
        return session


class BackendClientReuseTests(unittest.TestCase):
    def setUp(self) -> None:
        _RecordingBackendClient.instances = []
        patcher = mock.patch.dict(
            "bespokelabs.sandbox.backends.BACKENDS",
            {"fake": _RecordingBackendClient},
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_backend_client_constructed_once_across_creates(self) -> None:
        client = SandboxClient("fake")
        client.create()
        client.create()

        self.assertEqual(len(_RecordingBackendClient.instances), 1)
        self.assertEqual(_RecordingBackendClient.instances[0].create_calls, 2)

    def test_direct_sandbox_constructor_builds_its_own_backend_client(self) -> None:
        Sandbox("fake")
        Sandbox("fake")

        self.assertEqual(len(_RecordingBackendClient.instances), 2)


if __name__ == "__main__":
    unittest.main()
