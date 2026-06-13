"""Regression tests for all sandbox backends.

Each backend is tested with the same suite of cases:
  - Execute Python code and verify stdout
  - Execute a shell command and verify stdout
  - Computation (actual code runs, not just echoing)
  - Multi-line output
  - Error handling (non-zero exit code for bad code)
  - File write/read roundtrip

Tests are skipped automatically when the backend SDK is not installed
or required credentials are missing, so this file is safe to run in
any environment — it will exercise whatever backends are available.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import unittest

from bespokelabs.sandbox import Sandbox

# ---------------------------------------------------------------------------
# Helpers to detect which backends are available
# ---------------------------------------------------------------------------

def _can_use_tensorlake() -> bool:
    try:
        from tensorlake.sandbox import SandboxClient  # noqa: F401
        return True
    except Exception:
        return False


def _can_use_daytona() -> bool:
    if not os.environ.get("DAYTONA_API_KEY"):
        return False
    try:
        from daytona import Daytona  # noqa: F401
        return True
    except Exception:
        return False


def _can_use_e2b() -> bool:
    if not os.environ.get("E2B_API_KEY"):
        return False
    try:
        from e2b_code_interpreter import Sandbox as E2BSandbox  # noqa: F401
        return True
    except Exception:
        return False


def _can_use_modal() -> bool:
    try:
        import modal  # noqa: F401
        # Modal requires token auth, not just env vars — try a lightweight API call
        modal.App.lookup("sandbox-regression-probe", create_if_missing=True)
        return True
    except Exception:
        return False


def _can_use_docker() -> bool:
    try:
        import docker  # noqa: F401
        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        return False


def _can_use_safehouse() -> bool:
    if platform.system() != "Darwin":
        return False
    safehouse = shutil.which("safehouse")
    if not safehouse:
        return False
    try:
        result = subprocess.run(
            [safehouse, "--", "/usr/bin/true"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Shared regression test mixin
# ---------------------------------------------------------------------------

class _RegressionMixin:
    """Common regression tests executed against every backend.

    Subclasses set ``backend_name`` and optionally override ``sandbox_kwargs``.
    """

    backend_name: str
    sandbox_kwargs: dict = {}

    def _make_sandbox(self) -> Sandbox:
        sb = Sandbox(self.backend_name, **self.sandbox_kwargs)
        self.addCleanup(sb.destroy)
        return sb

    # -- execute_code ------------------------------------------------------

    def test_execute_python_hello(self) -> None:
        """Send print('hello') and verify stdout."""
        sb = self._make_sandbox()
        result = sb.execute_code('print("hello")')
        self.assertEqual(result.exit_code, 0)
        self.assertIn("hello", result.stdout)

    def test_execute_python_computation(self) -> None:
        """Run actual computation to prove code really executes."""
        sb = self._make_sandbox()
        result = sb.execute_code("print(sum(range(101)))")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("5050", result.stdout)

    def test_execute_python_multiline(self) -> None:
        """Multi-line code with loop produces expected output."""
        sb = self._make_sandbox()
        code = "for i in range(3):\n    print(f'line{i}')"
        result = sb.execute_code(code)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("line0", result.stdout)
        self.assertIn("line1", result.stdout)
        self.assertIn("line2", result.stdout)

    def test_execute_python_error_gives_nonzero_exit(self) -> None:
        """Syntax/runtime errors should produce a non-zero exit code."""
        sb = self._make_sandbox()
        result = sb.execute_code("raise ValueError('boom')")
        self.assertNotEqual(result.exit_code, 0)

    def test_execute_python_imports(self) -> None:
        """Standard library imports work inside the sandbox."""
        sb = self._make_sandbox()
        code = "import json; print(json.dumps({'a': 1}))"
        result = sb.execute_code(code)
        self.assertEqual(result.exit_code, 0)
        self.assertIn('"a"', result.stdout)

    # -- execute_command ---------------------------------------------------

    def test_execute_command_echo(self) -> None:
        """Run a simple shell command."""
        sb = self._make_sandbox()
        result = sb.execute_command("echo hello-from-shell")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("hello-from-shell", result.stdout)

    def test_execute_command_exit_code(self) -> None:
        """Command that fails returns non-zero exit code."""
        sb = self._make_sandbox()
        result = sb.execute_command("exit 42")
        self.assertEqual(result.exit_code, 42)

    # -- file operations ---------------------------------------------------

    def test_write_and_read_file(self) -> None:
        """Write a file then read it back."""
        sb = self._make_sandbox()
        sb.write_file("/tmp/test_regression.txt", "regression-test-data")
        content = sb.read_file("/tmp/test_regression.txt")
        self.assertIn(b"regression-test-data", content)

    def test_write_file_then_cat_via_command(self) -> None:
        """Write a file via API, read it via shell command."""
        sb = self._make_sandbox()
        sb.write_file("/tmp/test_cat.txt", "cat-me")
        result = sb.execute_command("cat /tmp/test_cat.txt")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("cat-me", result.stdout)

    def test_code_writes_file_read_via_api(self) -> None:
        """Code writes a file, API reads it back."""
        sb = self._make_sandbox()
        sb.execute_command("mkdir -p /tmp")
        result = sb.execute_code(
            "with open('/tmp/from_code.txt', 'w') as f: f.write('from-python')"
        )
        self.assertEqual(result.exit_code, 0)
        content = sb.read_file("/tmp/from_code.txt")
        self.assertIn(b"from-python", content)


# ---------------------------------------------------------------------------
# Backend-specific test classes
# ---------------------------------------------------------------------------

@unittest.skipUnless(_can_use_tensorlake(), "Tensorlake SDK not available")
class TensorlakeRegressionTests(_RegressionMixin, unittest.TestCase):
    backend_name = "tensorlake"
    sandbox_kwargs = {"memory_mb": 2048}


@unittest.skipUnless(_can_use_daytona(), "Daytona SDK or DAYTONA_API_KEY not available")
class DaytonaRegressionTests(_RegressionMixin, unittest.TestCase):
    backend_name = "daytona"


@unittest.skipUnless(_can_use_e2b(), "E2B SDK or E2B_API_KEY not available")
class E2BRegressionTests(_RegressionMixin, unittest.TestCase):
    backend_name = "e2b"


@unittest.skipUnless(_can_use_modal(), "Modal SDK not available")
class ModalRegressionTests(_RegressionMixin, unittest.TestCase):
    backend_name = "modal"


@unittest.skipUnless(_can_use_docker(), "Docker not available")
class DockerRegressionTests(_RegressionMixin, unittest.TestCase):
    backend_name = "docker"


@unittest.skipUnless(_can_use_safehouse(), "Safehouse CLI not available or unusable in this environment")
class SafehouseRegressionTests(_RegressionMixin, unittest.TestCase):
    backend_name = "safehouse"


class LocalRegressionTests(_RegressionMixin, unittest.TestCase):
    backend_name = "local"


if __name__ == "__main__":
    unittest.main()
