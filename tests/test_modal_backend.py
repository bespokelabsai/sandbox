from __future__ import annotations

import unittest
from unittest import mock

from bespokelabs.sandbox import Sandbox, SandboxClient
from bespokelabs.sandbox.backends.modal import ModalClient
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


if __name__ == "__main__":
    unittest.main()
