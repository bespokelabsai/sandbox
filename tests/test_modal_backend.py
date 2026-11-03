from __future__ import annotations

import unittest
from unittest import mock

from bespokelabs.sandbox import Sandbox, SandboxClient
from bespokelabs.sandbox.backends.modal import ModalClient
from bespokelabs.sandbox.exceptions import (
    FeatureNotSupportedError,
    SandboxConfigurationError,
    SandboxExecutionError,
)
from bespokelabs.sandbox.types import SandboxConfig


class ModalClientGpuTests(unittest.TestCase):

    def _client(self) -> tuple[ModalClient, mock.MagicMock]:
        modal = mock.MagicMock()
        modal.App.lookup.return_value = mock.sentinel.app
        modal.Sandbox.create.return_value = mock.MagicMock(object_id="sb-modal")

        client = object.__new__(ModalClient)
        client._modal = modal
        return client, modal

    def test_create_forwards_gpu_reservation(self) -> None:
        client, modal = self._client()

        client.create(SandboxConfig(backend="modal", gpu="H100:2"))

        modal.Sandbox.create.assert_called_once_with(
            app=mock.sentinel.app,
            timeout=600,
            cpu=1.0,
            memory=1024,
            gpu="H100:2",
        )

    def test_create_omits_gpu_when_not_requested(self) -> None:
        client, modal = self._client()

        client.create(SandboxConfig(backend="modal"))

        _, kwargs = modal.Sandbox.create.call_args
        self.assertNotIn("gpu", kwargs)

    def test_backend_options_can_override_gpu(self) -> None:
        client, modal = self._client()

        client.create(
            SandboxConfig(
                backend="modal",
                gpu="L4",
                backend_options={"gpu": "A100-80GB"},
            )
        )

        _, kwargs = modal.Sandbox.create.call_args
        self.assertEqual(kwargs["gpu"], "A100-80GB")


class ModalGpuPublicApiTests(unittest.TestCase):

    def test_sandbox_threads_gpu_into_config(self) -> None:
        backend = mock.MagicMock()
        backend.create.return_value = mock.MagicMock()

        with mock.patch.dict(
            "bespokelabs.sandbox.backends.BACKENDS",
            {"modal": mock.Mock(return_value=backend)},
        ):
            with Sandbox("modal", gpu="A100"):
                pass

        config = backend.create.call_args.args[0]
        self.assertEqual(config.gpu, "A100")

    def test_sandbox_client_threads_gpu_into_config(self) -> None:
        backend = mock.MagicMock()
        backend.create.return_value = mock.MagicMock()

        with mock.patch.dict(
            "bespokelabs.sandbox.backends.BACKENDS",
            {"modal": mock.Mock(return_value=backend)},
        ):
            client = SandboxClient("modal")
            with client.create(gpu="L4"):
                pass

        config = backend.create.call_args.args[0]
        self.assertEqual(config.gpu, "L4")



class ModalListSandboxesTests(unittest.TestCase):
    """list_sandboxes finds running sandboxes by tag and never guesses."""

    def _client(self, *sandboxes: mock.MagicMock) -> tuple[ModalClient, mock.MagicMock]:
        modal = mock.MagicMock()
        modal.Sandbox.list.return_value = iter(sandboxes)
        client = object.__new__(ModalClient)
        client._modal = modal
        return client, modal

    @staticmethod
    def _sandbox(object_id: str, tags: dict[str, str]) -> mock.MagicMock:
        sandbox = mock.MagicMock(object_id=object_id)
        sandbox.get_tags.return_value = tags
        return sandbox

    def test_lists_running_sandboxes_by_tag(self) -> None:
        client, modal = self._client(self._sandbox("sb-1", {"team": "x", "run": "1"}))

        (only,) = client.list_sandboxes({"team": "x"})

        modal.Sandbox.list.assert_called_once_with(tags={"team": "x"})
        self.assertEqual(only.provider_resource_id, "sb-1")
        self.assertEqual(only.labels, {"team": "x", "run": "1"})
        self.assertTrue(only.labels_complete)
        self.assertIsNone(only.created_at)
        self.assertEqual(only.session_state.backend, "modal")
        self.assertEqual(only.session_state.data, {"sandbox_id": "sb-1"})

    def test_a_sandbox_whose_own_tags_do_not_match_is_dropped(self) -> None:
        client, _ = self._client(self._sandbox("sb-2", {"team": "y"}))

        self.assertEqual(client.list_sandboxes({"team": "x"}), [])

    def test_an_sdk_without_tag_read_back_keeps_the_server_side_match(self) -> None:
        old = mock.MagicMock(spec=["object_id"], object_id="sb-old")
        client, _ = self._client(old)

        (only,) = client.list_sandboxes({"team": "x"})

        self.assertEqual(only.provider_resource_id, "sb-old")
        self.assertEqual(only.labels, {"team": "x"})
        self.assertFalse(only.labels_complete)

    def test_a_failed_tag_read_raises_instead_of_returning_part_of_the_list(self) -> None:
        broken = self._sandbox("sb-3", {})
        broken.get_tags.side_effect = RuntimeError("gone")
        client, _ = self._client(self._sandbox("sb-1", {"team": "x"}), broken)

        with self.assertRaises(SandboxExecutionError):
            client.list_sandboxes({"team": "x"})


class SandboxClientListSandboxesTests(unittest.TestCase):

    def test_an_unfiltered_listing_is_refused(self) -> None:
        client = object.__new__(SandboxClient)
        client._backend_name = "modal"
        client._backend_client = mock.MagicMock()
        for labels in ({}, {"team": ""}):
            with self.assertRaises(SandboxConfigurationError):
                client.list_sandboxes(labels)
        client._backend_client.list_sandboxes.assert_not_called()

    def test_a_backend_that_cannot_list_says_so(self) -> None:
        client = SandboxClient("local")

        with self.assertRaises(FeatureNotSupportedError):
            client.list_sandboxes({"team": "x"})

if __name__ == "__main__":
    unittest.main()
