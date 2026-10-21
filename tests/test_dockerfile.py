"""Building sandboxes from a Dockerfile. Provider SDKs are faked."""

from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest import mock

from bespokelabs.sandbox import Sandbox
from bespokelabs.sandbox.backends.docker import DockerClient
from bespokelabs.sandbox.backends.modal import ModalClient
from bespokelabs.sandbox.exceptions import (
    BackendNotInstalledError,
    FeatureNotSupportedError,
    SandboxConfigurationError,
)
from bespokelabs.sandbox.types import SandboxConfig, SandboxResult

try:
    import daytona  # noqa: F401

    _HAS_DAYTONA = True
except ImportError:  # pragma: no cover - exercised only without the extra
    _HAS_DAYTONA = False

if _HAS_DAYTONA:
    from bespokelabs.sandbox.backends.daytona import (
        DaytonaClient,
        _build_params,
    )


class _TmpDockerfile(unittest.TestCase):

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "app.txt").write_text("hi")
        self.dockerfile = self.root / "Dockerfile"
        self.dockerfile.write_text(
            "FROM python:3.12-slim\nCOPY app.txt /app.txt\n"
        )


class SandboxDockerfileArgsTests(_TmpDockerfile):

    def _backend(self) -> mock.MagicMock:
        backend = mock.MagicMock()
        backend.supports_dockerfile = True
        backend.create.return_value.execute_command.return_value = (
            SandboxResult()
        )
        return backend

    def _sandbox(self, backend: mock.MagicMock, **kwargs) -> SandboxConfig:
        with mock.patch.dict(
            "bespokelabs.sandbox.backends.BACKENDS",
            {"docker": mock.Mock(return_value=backend)},
        ):
            with Sandbox("docker", **kwargs):
                pass
        return backend.create.call_args.args[0]

    def test_paths_resolve_and_context_defaults_to_dockerfile_dir(
        self,
    ) -> None:
        config = self._sandbox(self._backend(), dockerfile=self.dockerfile)

        self.assertEqual(config.dockerfile, str(self.dockerfile.resolve()))
        self.assertEqual(config.context_dir, str(self.root.resolve()))
        self.assertIsNone(config.image)

    def test_dockerfile_replaces_preset_image(self) -> None:
        backend = self._backend()

        config = self._sandbox(
            backend, preset="claude-code", dockerfile=self.dockerfile
        )

        self.assertIsNone(config.image)
        # The preset image is not used, so its setup commands run instead.
        backend.create.return_value.execute_command.assert_called()

    def test_invalid_combinations_raise_before_create(self) -> None:
        cases = [
            {"context_dir": self.root},
            {"dockerfile": self.dockerfile, "image": "python:3.12"},
            {"dockerfile": self.dockerfile, "snapshot_id": "snap"},
            {"dockerfile": self.root / "missing"},
            {"dockerfile": self.dockerfile, "context_dir": self.root / "x"},
        ]
        for kwargs in cases:
            backend = self._backend()
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(SandboxConfigurationError):
                    self._sandbox(backend, **kwargs)
                backend.create.assert_not_called()

    def test_unsupported_backend_raises(self) -> None:
        with self.assertRaises(FeatureNotSupportedError):
            Sandbox("local", dockerfile=self.dockerfile)

    def test_unsupported_backend_raises_before_its_sdk_loads(self) -> None:
        backend_cls = mock.Mock(
            side_effect=BackendNotInstalledError("no SDK"),
            supports_dockerfile=False,
        )
        with mock.patch.dict(
            "bespokelabs.sandbox.backends.BACKENDS", {"e2b": backend_cls}
        ):
            with self.assertRaises(FeatureNotSupportedError):
                Sandbox("e2b", dockerfile=self.dockerfile)
        backend_cls.assert_not_called()


class DockerDockerfileTests(_TmpDockerfile):

    def test_builds_locally_and_runs_the_built_image(self) -> None:
        client = object.__new__(DockerClient)
        client._client = mock.MagicMock()
        client._client.images.build.return_value = (
            mock.MagicMock(id="sha256:built"),
            iter(()),
        )

        client.create(
            SandboxConfig(
                backend="docker",
                dockerfile=str(self.dockerfile),
                context_dir=str(self.root),
            )
        )

        client._client.images.build.assert_called_once_with(
            path=str(self.root),
            dockerfile=str(self.dockerfile),
            rm=True,
            forcerm=True,
        )
        client._client.images.pull.assert_not_called()
        _, kwargs = client._client.containers.run.call_args
        self.assertEqual(kwargs["image"], "sha256:built")


class ModalDockerfileTests(_TmpDockerfile):

    def test_uses_image_from_dockerfile_with_context(self) -> None:
        client = object.__new__(ModalClient)
        client._modal = mock.MagicMock()

        client.create(
            SandboxConfig(
                backend="modal",
                dockerfile=str(self.dockerfile),
                context_dir=str(self.root),
            )
        )

        modal = client._modal
        modal.Image.from_dockerfile.assert_called_once_with(
            str(self.dockerfile), context_dir=str(self.root)
        )
        _, kwargs = modal.Sandbox.create.call_args
        self.assertIs(kwargs["image"], modal.Image.from_dockerfile.return_value)

    def test_modal_without_context_dir_raises_clearly(self) -> None:
        client = object.__new__(ModalClient)
        client._modal = mock.MagicMock()

        # modal<0.74 signature: a context_mount, no context_dir.
        def from_dockerfile(path, context_mount=None, force_build=False):
            raise AssertionError("must not be called")

        client._modal.Image.from_dockerfile = from_dockerfile

        with self.assertRaises(FeatureNotSupportedError):
            client.create(
                SandboxConfig(
                    backend="modal",
                    dockerfile=str(self.dockerfile),
                    context_dir=str(self.root),
                )
            )
        client._modal.Sandbox.create.assert_not_called()


@unittest.skipUnless(_HAS_DAYTONA, "Daytona SDK not installed")
class DaytonaDockerfileTests(_TmpDockerfile):

    def test_builds_declaratively_with_copy_context(self) -> None:
        from daytona import CreateSandboxFromImageParams, Image

        params = _build_params(
            SandboxConfig(
                backend="daytona",
                dockerfile=str(self.dockerfile),
                context_dir=str(self.root),
            ),
            create_token="tok",
        )

        self.assertIsInstance(params, CreateSandboxFromImageParams)
        self.assertIsInstance(params.image, Image)
        self.assertIn("COPY app.txt", params.image.dockerfile())

    def test_other_context_dir_is_rejected_before_create(self) -> None:
        fake = mock.MagicMock()
        client = DaytonaClient()
        client._client = fake

        with self.assertRaises(SandboxConfigurationError):
            client.create(
                SandboxConfig(
                    backend="daytona",
                    dockerfile=str(self.dockerfile),
                    context_dir=str(self.root.parent),
                )
            )
        fake.create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
