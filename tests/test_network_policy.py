"""NetworkPolicy at create time and through set_network. SDKs are faked."""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from bespokelabs.sandbox import NetworkPolicy, Sandbox
from bespokelabs.sandbox.backends.daytona import DaytonaClient
from bespokelabs.sandbox.backends.docker import DockerClient
from bespokelabs.sandbox.backends.modal import ModalClient, ModalSession
from bespokelabs.sandbox.backends.tensorlake import TensorlakeClient
from bespokelabs.sandbox.exceptions import (
    FeatureNotSupportedError,
    SandboxConfigurationError,
)
from bespokelabs.sandbox.types import SandboxConfig

try:
    import daytona  # noqa: F401

    _HAS_DAYTONA = True
except ImportError:  # pragma: no cover - exercised only without the extra
    _HAS_DAYTONA = False

try:
    import modal  # noqa: F401

    _HAS_MODAL = True
except ImportError:  # pragma: no cover - exercised only without the extra
    _HAS_MODAL = False

if _HAS_DAYTONA:
    from bespokelabs.sandbox.backends.daytona import (
        DaytonaSession,
        _build_params,
    )

NONE = NetworkPolicy(mode="none")
DOMAINS = NetworkPolicy(mode="allowlist", domains=["hub.example"])
CIDRS = NetworkPolicy(mode="allowlist", cidrs=["10.0.0.0/8"])


class NetworkPolicyTests(unittest.TestCase):

    def test_invalid_policies_are_rejected(self) -> None:
        cases = [
            {"mode": "open"},
            {"mode": "allowlist"},
            {"mode": "public", "domains": ["a.example"]},
            {"mode": "none", "cidrs": ["10.0.0.0/8"]},
            {"mode": "allowlist", "cidrs": ["not-a-cidr"]},
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(SandboxConfigurationError):
                    NetworkPolicy(**kwargs)

    def test_single_string_is_one_entry(self) -> None:
        policy = NetworkPolicy(mode="allowlist", domains="hub.example")
        self.assertEqual(policy.domains, ("hub.example",))


class SandboxNetworkArgsTests(unittest.TestCase):

    def _config(self, backend: object, **kwargs) -> SandboxConfig:
        with Sandbox("docker", _backend_client=backend, **kwargs):
            pass
        return backend.create.call_args.args[0]

    def test_policy_reaches_config_and_derives_allow_internet(self) -> None:
        backend = mock.MagicMock(network_modes={"public", "none"})

        config = self._config(backend, network=NONE)

        self.assertEqual(config.network, NONE)
        self.assertFalse(config.allow_internet)

    def test_network_and_allow_internet_together_is_an_error(self) -> None:
        backend = mock.MagicMock()
        with self.assertRaises(SandboxConfigurationError):
            self._config(backend, network=NONE, allow_internet=True)
        backend.create.assert_not_called()

    def test_unenforceable_mode_raises_before_create(self) -> None:
        docker = object.__new__(DockerClient)
        with mock.patch.object(DockerClient, "create") as create:
            with self.assertRaises(FeatureNotSupportedError):
                self._config(docker, network=DOMAINS)
            create.assert_not_called()

    def test_network_backend_options_conflict_with_policy(self) -> None:
        cases = {
            DockerClient: [
                "network_mode",
                "network",
                "network_disabled",
                "networking_config",
            ],
            ModalClient: [
                "block_network",
                "outbound_cidr_allowlist",
                "outbound_domain_allowlist",
                "cidr_allowlist",
            ],
            TensorlakeClient: ["allow_internet_access"],
            DaytonaClient: [
                "network_block_all",
                "network_allow_list",
                "domain_allow_list",
            ],
        }
        for client_cls, keys in cases.items():
            for key in keys:
                with self.subTest(client=client_cls.__name__, key=key):
                    client = object.__new__(client_cls)
                    with mock.patch.object(client_cls, "create") as create:
                        with self.assertRaises(SandboxConfigurationError):
                            Sandbox(
                                "docker",
                                network=NetworkPolicy(),
                                backend_options={key: "x"},
                                _backend_client=client,
                            )
                        create.assert_not_called()

    def test_network_backend_options_still_pass_without_policy(self) -> None:
        backend = mock.MagicMock()
        config = self._config(backend, backend_options={"network_mode": "host"})
        self.assertEqual(config.backend_options, {"network_mode": "host"})

        backend = mock.MagicMock(
            network_modes={"none"}, network_option_keys={"network_mode"}
        )
        config = self._config(
            backend, network=NONE, backend_options={"ports": {"80/tcp": None}}
        )
        self.assertEqual(config.backend_options, {"ports": {"80/tcp": None}})

    def test_backend_without_network_support(self) -> None:
        with self.assertRaises(FeatureNotSupportedError):
            Sandbox("local", network=NONE)
        with Sandbox("local", network=NetworkPolicy()) as sb:
            with self.assertRaises(FeatureNotSupportedError):
                sb.set_network(NONE)


class ModalNetworkTests(unittest.TestCase):

    def _create_kwargs(self, policy: NetworkPolicy) -> dict:
        client = object.__new__(ModalClient)
        client._modal = mock.MagicMock()
        client.create(SandboxConfig(backend="modal", network=policy))
        return client._modal.Sandbox.create.call_args.kwargs

    def test_create_maps_each_mode(self) -> None:
        self.assertIs(self._create_kwargs(NONE)["block_network"], True)
        self.assertEqual(
            self._create_kwargs(CIDRS)["outbound_cidr_allowlist"],
            ["10.0.0.0/8"],
        )
        self.assertEqual(
            self._create_kwargs(DOMAINS)["outbound_domain_allowlist"],
            ["hub.example"],
        )
        kwargs = self._create_kwargs(NetworkPolicy())
        self.assertNotIn("block_network", kwargs)

    def test_old_sdk_without_allowlist_kwargs_is_unsupported(self) -> None:
        calls = []

        class OldSandbox:

            @staticmethod
            def create(
                *args,
                app=None,
                timeout=None,
                cpu=None,
                memory=None,
                block_network=False,
                cidr_allowlist=None,
            ):
                calls.append(block_network)
                return mock.MagicMock()

        client = object.__new__(ModalClient)
        client._modal = mock.MagicMock()
        client._modal.Sandbox = OldSandbox
        for policy in (CIDRS, DOMAINS):
            with self.subTest(policy=policy):
                with self.assertRaises(FeatureNotSupportedError):
                    client.create(
                        SandboxConfig(backend="modal", network=policy)
                    )
        self.assertEqual(calls, [])
        client.create(SandboxConfig(backend="modal", network=NONE))
        self.assertEqual(calls, [True])

    def test_set_network(self) -> None:
        sandbox = mock.MagicMock()
        session = ModalSession(sandbox=sandbox)
        setter = sandbox._experimental_set_outbound_network_policy

        session.set_network(DOMAINS)
        setter.assert_called_with(
            outbound_cidr_allowlist=None,
            outbound_domain_allowlist=["hub.example"],
        )
        session.set_network(NetworkPolicy())
        setter.assert_called_with()
        with self.assertRaises(FeatureNotSupportedError):
            session.set_network(NONE)

    @unittest.skipUnless(_HAS_MODAL, "Modal SDK not installed")
    def test_set_network_blocks_until_modal_applies_it(self) -> None:
        # Drive the real synchronicity-wrapped modal.Sandbox, faking only the
        # RPC, so a dropped coroutine would leave `sent` empty.
        modal = __import__("modal")
        from modal._utils.async_utils import synchronizer
        from modal.sandbox import _sandbox

        sent = []

        class Router:

            async def set_network_access(self, req):
                await asyncio.sleep(0.05)
                sent.append(req)

        async def router(task_id):
            return Router()

        impl = object.__new__(_sandbox._Sandbox)
        impl._task_id = "ta-1"
        impl._get_command_router_client = router
        sandbox = synchronizer._translate_out(impl)
        self.assertIsInstance(sandbox, modal.Sandbox)

        ModalSession(sandbox=sandbox).set_network(DOMAINS)

        self.assertEqual(len(sent), 1)
        self.assertEqual(
            list(sent[0].network_access.allowed_domains), ["hub.example"]
        )


@unittest.skipUnless(_HAS_DAYTONA, "Daytona SDK not installed")
class DaytonaNetworkTests(unittest.TestCase):

    def _params(self, policy: NetworkPolicy) -> object:
        config = SandboxConfig(backend="daytona", network=policy)
        return _build_params(config, create_token="tok")

    def test_create_maps_each_mode(self) -> None:
        self.assertIs(self._params(NONE).network_block_all, True)
        self.assertEqual(self._params(CIDRS).network_allow_list, "10.0.0.0/8")
        self.assertEqual(self._params(DOMAINS).domain_allow_list, "hub.example")
        public = self._params(NetworkPolicy())
        self.assertIsNone(public.network_block_all)

    def test_unsupported_allowlists_fail_before_create(self) -> None:
        fake = mock.MagicMock()
        client = DaytonaClient()
        client._client = fake
        for policy in (
            NetworkPolicy(
                mode="allowlist", domains=["a.example"], cidrs=["1.2.3.4/32"]
            ),
            NetworkPolicy(mode="allowlist", cidrs=["2001:db8::/32"]),
        ):
            with self.subTest(policy=policy):
                with self.assertRaises(SandboxConfigurationError):
                    client.create(
                        SandboxConfig(backend="daytona", network=policy)
                    )
        fake.create.assert_not_called()

    def test_set_network(self) -> None:
        sandbox = mock.MagicMock()
        session = DaytonaSession(client=mock.MagicMock(), sandbox=sandbox)

        session.set_network(NONE)
        sandbox.update_network_settings.assert_called_with(
            network_block_all=True
        )
        session.set_network(DOMAINS)
        sandbox.update_network_settings.assert_called_with(
            domain_allow_list="hub.example"
        )
        session.set_network(NetworkPolicy())
        sandbox.update_network_settings.assert_called_with(
            network_block_all=False
        )


if __name__ == "__main__":
    unittest.main()
