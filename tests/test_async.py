"""Tests for the async API (AsyncSandboxClient / AsyncSandbox)."""

from __future__ import annotations

import asyncio
import time
import unittest
from unittest import mock

from pydantic import BaseModel

from bespokelabs.sandbox import AsyncSandbox, AsyncSandboxClient, Sandbox, SandboxError
from bespokelabs.sandbox.exceptions import BackendNotInstalledError
from bespokelabs.sandbox.types import SandboxResult


class Point(BaseModel):
    x: int
    y: int


class AsyncSandboxClientLocalTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end behavior of the async API on the local backend."""

    async def test_create_returns_working_sandbox(self) -> None:
        client = AsyncSandboxClient("local")
        sb = await client.create()
        try:
            result = await sb.execute_code('print("via async client")')
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.stdout.strip(), "via async client")
        finally:
            await sb.destroy()

    async def test_async_context_manager_destroys(self) -> None:
        client = AsyncSandboxClient("local")
        async with await client.create() as sb:
            self.assertTrue(sb.is_alive)
            result = await sb.execute_command("echo ok")
            self.assertEqual(result.exit_code, 0)
        self.assertFalse(sb.is_alive)

    async def test_one_step_classmethod(self) -> None:
        async with await AsyncSandbox.create("local") as sb:
            self.assertEqual(sb.backend_name, "local")
            result = await sb.execute_code("print(1 + 1)")
            self.assertEqual(result.stdout.strip(), "2")

    async def test_concurrent_creates_are_isolated(self) -> None:
        client = AsyncSandboxClient("local")
        sb1, sb2 = await asyncio.gather(client.create(), client.create())
        try:
            await sb1.write_file("/only_in_first.txt", "1")

            self.assertEqual(await sb1.read_file("/only_in_first.txt"), b"1")
            names = [f.path for f in await sb2.list_files("/")]
            self.assertNotIn("/only_in_first.txt", names)
        finally:
            await asyncio.gather(sb1.destroy(), sb2.destroy())

    async def test_commands_run_concurrently(self) -> None:
        client = AsyncSandboxClient("local")
        sb1, sb2 = await asyncio.gather(client.create(), client.create())
        try:
            start = time.monotonic()
            await asyncio.gather(
                sb1.execute_command("sleep 1"),
                sb2.execute_command("sleep 1"),
            )
            elapsed = time.monotonic() - start
            # Serial execution would take >= 2s; generous slack for CI.
            self.assertLess(elapsed, 1.8)
        finally:
            await asyncio.gather(sb1.destroy(), sb2.destroy())

    async def test_return_type_passthrough(self) -> None:
        async with await AsyncSandbox.create("local") as sb:
            point = await sb.execute_code(
                'import json; print(json.dumps({"x": 1, "y": 2}))',
                return_type=Point,
            )
            self.assertEqual((point.x, point.y), (1, 2))

    async def test_use_after_destroy_raises(self) -> None:
        sb = await AsyncSandbox.create("local")
        await sb.destroy()
        with self.assertRaises(SandboxError):
            await sb.execute_code("print(1)")

    async def test_wraps_existing_sync_sandbox(self) -> None:
        sync_sb = Sandbox("local")
        sb = AsyncSandbox(sync_sb)
        try:
            result = await sb.execute_command("echo wrapped")
            self.assertEqual(result.stdout.strip(), "wrapped")
        finally:
            await sb.destroy()
        self.assertFalse(sync_sb.is_alive)

    def test_unknown_backend_raises_at_construction(self) -> None:
        with self.assertRaises(SandboxError):
            AsyncSandboxClient("not-a-backend")


class AsyncBackendClientReuseTests(unittest.IsolatedAsyncioTestCase):
    async def test_backend_client_constructed_once_across_concurrent_creates(self) -> None:
        instances: list[object] = []

        class RecordingBackendClient:
            def __init__(self) -> None:
                instances.append(self)

            def create(self, config: object) -> mock.MagicMock:
                session = mock.MagicMock()
                session.execute_command.return_value = SandboxResult()
                return session

        with mock.patch.dict(
            "bespokelabs.sandbox.backends.BACKENDS",
            {"fake": RecordingBackendClient},
            clear=False,
        ):
            client = AsyncSandboxClient("fake")
            await asyncio.gather(client.create(), client.create(), client.create())

        self.assertEqual(len(instances), 1)

    async def test_missing_sdk_surfaces_at_create(self) -> None:
        class MissingBackendClient:
            def __init__(self) -> None:
                raise BackendNotInstalledError("SDK not installed")

        with mock.patch.dict(
            "bespokelabs.sandbox.backends.BACKENDS",
            {"missing": MissingBackendClient},
            clear=False,
        ):
            client = AsyncSandboxClient("missing")
            with self.assertRaises(BackendNotInstalledError):
                await client.create()


if __name__ == "__main__":
    unittest.main()
