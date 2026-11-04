"""Declared backend capabilities: the checks built on them, and drift tests.

The drift tests keep each backend's ``capabilities`` declaration in step
with what its adapter does and with the README feature tables, so the
declaration stays the single source of truth.
"""

from __future__ import annotations

import inspect
import pathlib
import re
import unittest
import warnings
from unittest import mock

from bespokelabs.sandbox import (
    AsyncSandboxClient,
    BackendCapabilities,
    FeatureNotSupportedError,
    NetworkPolicy,
    Sandbox,
    SandboxClient,
    SandboxConfigurationError,
    UnsupportedOptionWarning,
    backend_capabilities,
)
from bespokelabs.sandbox.backends import BACKENDS
from bespokelabs.sandbox.capabilities import (
    CREATE_OPTIONS,
    UNDECLARED,
    capabilities_of,
)
from bespokelabs.sandbox.types import SandboxResult

README = pathlib.Path(__file__).resolve().parents[1] / "README.md"

# README matrix column order.
COLUMNS = {
    "Local": "local",
    "Safehouse": "safehouse",
    "Docker": "docker",
    "Ray": "ray",
    "Daytona": "daytona",
    "Tensorlake": "tensorlake",
    "Modal": "modal",
    "RunPod": "runpod",
    "E2B": "e2b",
}

# Options an adapter reads only to reject them, so they are not declared.
READ_TO_REJECT = {"runpod": {"allow_internet", "snapshot_id"}}

# Same order as the Sandbox(...) signature.
OPTION_ORDER = [
    "cpu",
    "memory_mb",
    "disk_mb",
    "gpu",
    "timeout_secs",
    "image",
    "template",
    "env_vars",
    "allow_internet",
    "app_name",
    "snapshot_id",
    "workdir",
    "backend_options",
]


def _module(backend: str):
    return inspect.getmodule(BACKENDS[backend])


def _session_class(backend: str) -> type | None:
    module = _module(backend)
    for name, obj in vars(module).items():
        if (
            name.endswith("Session")
            and inspect.isclass(obj)
            and obj.__module__ == module.__name__
        ):
            return obj
    return None


def _raises_not_supported(call) -> bool:
    try:
        call()
    except FeatureNotSupportedError:
        return True
    except Exception:  # noqa: BLE001 - a bare object fails in other ways
        return False
    return False


def options_table() -> str:
    """Render the README "Creation options" table from declarations."""
    header = "| Option | " + " | ".join(COLUMNS) + " |"
    rule = "|---|" + "---|" * len(COLUMNS)
    rows = [header, rule]
    for option in OPTION_ORDER:
        cells = [
            "Yes" if option in BACKENDS[b].capabilities.options else "No"
            for b in COLUMNS.values()
        ]
        # True is every backend's behaviour; only False asks for anything.
        label = (
            "allow_internet=False"
            if option == "allow_internet"
            else f"{option}="
        )
        rows.append(f"| `{label}` | " + " | ".join(cells) + " |")
    return "\n".join(rows)


def _matrix_rows() -> dict[str, dict[str, str]]:
    text = README.read_text()
    section = text.split("## Feature Support Matrix", 1)[1]
    rows: dict[str, dict[str, str]] = {}
    for line in section.splitlines():
        if line.startswith("## "):
            break
        if not line.startswith("| ") or line.startswith("| Feature"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) == len(COLUMNS) + 1:
            rows[cells[0]] = dict(zip(COLUMNS.values(), cells[1:], strict=True))
    return rows


class DeclarationTests(unittest.TestCase):

    def test_every_backend_declares_capabilities(self) -> None:
        for name, cls in BACKENDS.items():
            with self.subTest(backend=name):
                self.assertIsInstance(
                    vars(cls).get("capabilities"), BackendCapabilities
                )

    def test_unknown_option_names_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            BackendCapabilities(options=frozenset({"gpus"}))

    def test_declared_options_match_the_config_fields_the_adapter_reads(
        self,
    ) -> None:
        for name in BACKENDS:
            source = inspect.getsource(_module(name))
            read = set(re.findall(r"\bconfig\.(\w+)", source)) & CREATE_OPTIONS
            read -= READ_TO_REJECT.get(name, set())
            with self.subTest(backend=name):
                self.assertEqual(BACKENDS[name].capabilities.options, read)

    def test_declared_session_methods_match_the_adapter(self) -> None:
        for name, cls in BACKENDS.items():
            caps = cls.capabilities
            session_cls = _session_class(name)
            with self.subTest(backend=name):
                if not caps.sessions:
                    self.assertIsNone(session_cls)
                    self.assertTrue(
                        _raises_not_supported(
                            lambda cls=cls: object.__new__(cls).create(None)
                        )
                    )
                    continue
                self.assertEqual(
                    caps.expose_port, hasattr(session_cls, "expose_port")
                )
                self.assertEqual(
                    bool(caps.set_network), hasattr(session_cls, "set_network")
                )
                bare = object.__new__(session_cls)
                self.assertEqual(
                    not caps.snapshot, _raises_not_supported(bare.snapshot)
                )
                self.assertEqual(
                    not caps.resume, _raises_not_supported(bare.session_state)
                )

    def test_declared_jobs_match_the_adapter(self) -> None:
        for name, cls in BACKENDS.items():
            with self.subTest(backend=name):
                self.assertEqual(
                    cls.capabilities.jobs,
                    callable(getattr(cls, "submit_job", None)),
                )


class ReadmeTests(unittest.TestCase):

    def test_options_table_matches_declarations(self) -> None:
        table = options_table()
        self.assertTrue(
            table in README.read_text(),
            "README creation options table is stale; replace it with:\n\n"
            + table,
        )

    def test_feature_matrix_matches_declarations(self) -> None:
        rows = _matrix_rows()
        modes = {"all": {"public", "none", "allowlist"}}

        def mode_set(cell: str) -> set[str]:
            return modes.get(cell) or {m.strip() for m in cell.split(",")}

        for backend in COLUMNS.values():
            caps = BACKENDS[backend].capabilities
            with self.subTest(backend=backend):
                self.assertEqual(
                    rows["`snapshot`"][backend] != "No", caps.snapshot
                )
                self.assertEqual(
                    rows["`expose_port`"][backend] != "No", caps.expose_port
                )
                self.assertEqual(
                    rows["`session_state()` / resume"][backend] != "No",
                    caps.resume,
                )
                self.assertEqual(
                    rows["`dockerfile=`"][backend] != "Rejected",
                    caps.dockerfile,
                )
                self.assertEqual(
                    mode_set(rows["`network=` modes"][backend]),
                    set(caps.network_modes),
                )
                cell = rows["`set_network()`"][backend]
                if cell == "No":
                    expected = set()
                elif cell == "Yes":
                    expected = set(caps.network_modes)
                else:
                    expected = mode_set(cell)
                self.assertEqual(set(caps.set_network), expected)
                self.assertEqual(
                    rows["`gpu=` reservation"][backend] == "Yes",
                    "gpu" in caps.options,
                )


class LookupTests(unittest.TestCase):

    def test_backend_capabilities_needs_no_sdk(self) -> None:
        with mock.patch.dict(
            BACKENDS,
            {"e2b": mock.Mock(capabilities=BACKENDS["e2b"].capabilities)},
        ):
            caps = backend_capabilities(" E2B ")
        self.assertFalse(caps.snapshot)
        self.assertIn("template", caps.options)

    def test_unknown_backend(self) -> None:
        with self.assertRaises(SandboxConfigurationError):
            backend_capabilities("nope")

    def test_sandbox_and_clients_expose_capabilities(self) -> None:
        local = BACKENDS["local"].capabilities
        with Sandbox("local") as sb:
            self.assertIs(sb.capabilities, local)
        self.assertIs(SandboxClient("local").capabilities, local)
        self.assertIs(AsyncSandboxClient("local").capabilities, local)

    def test_resumed_sandbox_keeps_capabilities(self) -> None:
        with Sandbox("local") as sb:
            resumed = Sandbox.resume(sb.session_state())
            self.assertIs(resumed.capabilities, BACKENDS["local"].capabilities)


class StrictOptionTests(unittest.TestCase):

    def _fake(self, **caps) -> mock.MagicMock:
        backend = mock.MagicMock(capabilities=BackendCapabilities(**caps))
        backend.create.return_value.execute_command.return_value = (
            SandboxResult()
        )
        return backend

    def _create(self, backend, **kwargs) -> None:
        with Sandbox("docker", _backend_client=backend, **kwargs):
            pass

    def test_default_warns_and_names_every_ignored_option(self) -> None:
        backend = self._fake(options=frozenset({"cpu"}))
        with self.assertWarns(UnsupportedOptionWarning) as caught:
            self._create(backend, cpu=2, gpu="H100", workdir="/w")
        self.assertIn("gpu=, workdir=", str(caught.warning))
        self.assertIn("strict=True", str(caught.warning))
        # The warning points at the caller, not at package internals.
        self.assertEqual(caught.filename, __file__)
        backend.create.assert_called_once()

    def test_strict_raises_before_anything_is_created(self) -> None:
        backend = self._fake()
        with self.assertRaises(FeatureNotSupportedError) as caught:
            self._create(backend, disk_mb=1024, strict=True)
        self.assertIn("disk_mb=", str(caught.exception))
        self.assertEqual(caught.exception.op, "create")
        backend.create.assert_not_called()

    def test_strict_false_accepts_silently(self) -> None:
        backend = self._fake()
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            self._create(backend, gpu="H100", strict=False)
        backend.create.assert_called_once()

    def test_supported_and_unset_options_are_quiet(self) -> None:
        backend = self._fake(options=frozenset({"gpu"}))
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            self._create(
                backend,
                gpu="H100",
                env_vars={},
                backend_options={},
                allow_internet=True,
                strict=True,
            )

    def test_allow_internet_false_is_checked(self) -> None:
        with self.assertRaises(FeatureNotSupportedError):
            self._create(self._fake(), allow_internet=False, strict=True)

    def test_preset_values_are_not_checked(self) -> None:
        # The preset supplies an image and resources; E2B applies none of
        # them, but the caller did not ask for them either.
        backend = self._fake(options=frozenset({"env_vars"}))
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            self._create(backend, preset="claude-code", strict=True)

    def test_real_backend_without_gpu(self) -> None:
        with self.assertRaises(FeatureNotSupportedError):
            Sandbox("local", gpu="H100", strict=True)

    def test_client_create_threads_strict(self) -> None:
        with self.assertRaises(FeatureNotSupportedError):
            SandboxClient("local").create(cpu=4, strict=True)

    def test_undeclared_client_is_not_checked(self) -> None:
        backend = mock.MagicMock()
        self.assertIs(capabilities_of(backend), UNDECLARED)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            self._create(backend, gpu="H100", strict=True)


class OperationGateTests(unittest.TestCase):

    def test_unsupported_operations_raise_up_front(self) -> None:
        session = mock.MagicMock()
        backend = mock.MagicMock(capabilities=BackendCapabilities())
        backend.create.return_value = session
        with Sandbox("docker", _backend_client=backend) as sb:
            for op, call in {
                "expose_port": lambda: sb.expose_port(8080),
                "set_network": lambda: sb.set_network(NetworkPolicy()),
                "snapshot": sb.snapshot,
                "session_state": sb.session_state,
            }.items():
                with self.subTest(op=op):
                    with self.assertRaises(FeatureNotSupportedError):
                        call()
        session.expose_port.assert_not_called()
        session.set_network.assert_not_called()
        session.snapshot.assert_not_called()
        session.session_state.assert_not_called()

    def test_set_network_mode_outside_the_declared_set(self) -> None:
        session = mock.MagicMock()
        backend = mock.MagicMock(
            capabilities=BackendCapabilities(set_network=frozenset({"public"}))
        )
        backend.create.return_value = session
        with Sandbox("docker", _backend_client=backend) as sb:
            with self.assertRaises(FeatureNotSupportedError):
                sb.set_network(NetworkPolicy(mode="none"))
            sb.set_network(NetworkPolicy())
        session.set_network.assert_called_once_with(NetworkPolicy())

    def test_resume_on_a_backend_without_it(self) -> None:
        state = mock.Mock(backend="ray", data={})
        backend = mock.MagicMock(capabilities=BACKENDS["ray"].capabilities)
        with self.assertRaises(FeatureNotSupportedError):
            Sandbox._resume_with("ray", backend, state)
        backend.resume.assert_not_called()

    def test_job_only_backend_refuses_sessions_without_its_sdk(self) -> None:
        backend_cls = mock.Mock(
            side_effect=AssertionError("must not be built"),
            capabilities=BACKENDS["gcp_batch"].capabilities,
        )
        with mock.patch.dict(BACKENDS, {"gcp_batch": backend_cls}):
            with self.assertRaises(FeatureNotSupportedError):
                Sandbox("gcp_batch")


if __name__ == "__main__":
    unittest.main()
