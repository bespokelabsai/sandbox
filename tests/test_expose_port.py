"""Sandbox.expose_port across backends. Provider SDKs are faked."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from bespokelabs.sandbox import ExposedPort, Sandbox
from bespokelabs.sandbox.backends.docker import DockerSession
from bespokelabs.sandbox.backends.e2b import E2BSession
from bespokelabs.sandbox.backends.modal import ModalSession
from bespokelabs.sandbox.exceptions import (
    FeatureNotSupportedError,
    SandboxConfigurationError,
)

try:
    import daytona  # noqa: F401

    _HAS_DAYTONA = True
except ImportError:  # pragma: no cover - exercised only without the extra
    _HAS_DAYTONA = False

if _HAS_DAYTONA:
    from bespokelabs.sandbox.backends.daytona import DaytonaSession


class SandboxExposePortTests(unittest.TestCase):

    def test_unsupported_backend_raises(self) -> None:
        with Sandbox("local") as sb:
            with self.assertRaises(FeatureNotSupportedError):
                sb.expose_port(8080)

    def test_invalid_port_is_rejected(self) -> None:
        with Sandbox("local") as sb:
            for port in (0, 70000, "8080", True):
                with self.subTest(port=port):
                    with self.assertRaises(SandboxConfigurationError):
                        sb.expose_port(port)

    def test_headers_stay_out_of_repr(self) -> None:
        exposed = ExposedPort(8080, "https://x", {"X-Token": "secret"})
        self.assertNotIn("secret", repr(exposed))


@unittest.skipUnless(_HAS_DAYTONA, "Daytona SDK not installed")
class DaytonaExposePortTests(unittest.TestCase):

    def test_preview_link_token_goes_in_headers(self) -> None:
        sandbox = mock.MagicMock()
        sandbox.get_preview_link.return_value = SimpleNamespace(
            url="https://8080-sbx.proxy.example", token="tok"
        )
        session = DaytonaSession(client=mock.MagicMock(), sandbox=sandbox)

        exposed = session.expose_port(8080)

        sandbox.get_preview_link.assert_called_once_with(8080)
        self.assertEqual(exposed.url, "https://8080-sbx.proxy.example")
        self.assertEqual(exposed.headers, {"X-Daytona-Preview-Token": "tok"})


class ModalExposePortTests(unittest.TestCase):

    def test_returns_tunnel_url(self) -> None:
        sandbox = mock.MagicMock()
        sandbox.tunnels.return_value = {
            8080: SimpleNamespace(url="https://abc.modal.host")
        }

        exposed = ModalSession(sandbox=sandbox).expose_port(8080)

        self.assertEqual(exposed.url, "https://abc.modal.host")
        self.assertEqual(exposed.headers, {})

    def test_old_sdk_tunnel_list(self) -> None:
        # modal<0.64.153 returns a list of TunnelData protos.
        sandbox = mock.MagicMock()
        sandbox.tunnels.return_value = [
            SimpleNamespace(container_port=9000, host="x.modal.host", port=443),
            SimpleNamespace(
                container_port=8080, host="abc.modal.host", port=443
            ),
        ]
        session = ModalSession(sandbox=sandbox)

        self.assertEqual(
            session.expose_port(8080).url, "https://abc.modal.host"
        )
        sandbox.tunnels.return_value[1].port = 8443
        self.assertEqual(
            session.expose_port(8080).url, "https://abc.modal.host:8443"
        )
        with self.assertRaises(SandboxConfigurationError):
            session.expose_port(7000)

    def test_port_not_opened_at_creation_raises(self) -> None:
        sandbox = mock.MagicMock()
        sandbox.tunnels.return_value = {}

        with self.assertRaises(SandboxConfigurationError):
            ModalSession(sandbox=sandbox).expose_port(8080)


class E2BExposePortTests(unittest.TestCase):

    def test_host_and_traffic_token(self) -> None:
        sandbox = mock.MagicMock(traffic_access_token="tok")
        sandbox.get_host.return_value = "8080-sbx.e2b.app"

        exposed = E2BSession(sandbox=sandbox).expose_port(8080)

        sandbox.get_host.assert_called_once_with(8080)
        self.assertEqual(exposed.url, "https://8080-sbx.e2b.app")
        self.assertEqual(exposed.headers, {"e2b-traffic-access-token": "tok"})

    def test_public_sandbox_needs_no_headers(self) -> None:
        sandbox = mock.MagicMock(traffic_access_token=None)
        sandbox.get_host.return_value = "8080-sbx.e2b.app"

        exposed = E2BSession(sandbox=sandbox).expose_port(8080)

        self.assertEqual(exposed.headers, {})


class DockerExposePortTests(unittest.TestCase):

    def _session(self, ports: dict) -> DockerSession:
        container = mock.MagicMock()
        container.ports = ports
        return DockerSession(container=container, timeout=60)

    def test_published_port_maps_to_host_address(self) -> None:
        session = self._session(
            {"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "49153"}]}
        )

        exposed = session.expose_port(8080)

        self.assertEqual(exposed.url, "http://127.0.0.1:49153")

    def test_unpublished_port_raises(self) -> None:
        with self.assertRaises(SandboxConfigurationError):
            self._session({"9000/tcp": None}).expose_port(8080)


if __name__ == "__main__":
    unittest.main()
