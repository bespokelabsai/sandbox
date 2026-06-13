from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from bespokelabs.sandbox.backends._prelude import PYTHON_PREAMBLE, SHELL_PRELUDE
from bespokelabs.sandbox.backends.safehouse import SafehouseClient, SafehouseSession
from bespokelabs.sandbox.types import SandboxConfig, SandboxResult


class SafehouseSessionTests(unittest.TestCase):
    def _create_session(self, **config_kwargs: object) -> SafehouseSession:
        config = SandboxConfig(backend="safehouse", **config_kwargs)
        with mock.patch("bespokelabs.sandbox.backends.safehouse.platform.system", return_value="Darwin"), \
             mock.patch("bespokelabs.sandbox.backends.safehouse.shutil.which", return_value="/usr/local/bin/safehouse"):
            session = SafehouseClient().create(config)
        self.addCleanup(session.destroy)
        return session

    def test_create_sets_home_and_sandbox_root(self) -> None:
        with tempfile.TemporaryDirectory() as workdir:
            session = self._create_session(workdir=workdir, env_vars={"FOO": "bar"})
            self.assertEqual(session._workdir, os.path.abspath(workdir))
            self.assertFalse(session._owns_workdir)
            self.assertEqual(session._env["SANDBOX_ROOT"], session._workdir)
            self.assertEqual(session._env["HOME"], session._workdir)
            self.assertEqual(session._env["FOO"], "bar")

    def test_execute_code_wraps_python_with_preamble(self) -> None:
        session = self._create_session()
        with mock.patch.object(session, "_resolve_interpreter", return_value="python3.13t"), \
             mock.patch.object(session, "_run", return_value=SandboxResult(stdout="ok")) as run:
            session.execute_code("print('hi')", language="python3.13t")

        cmd = run.call_args.args[0]
        self.assertEqual(
            cmd[:4],
            ["/usr/local/bin/safehouse", f"--workdir={session._workdir}", "--env", "--"],
        )
        self.assertEqual(cmd[4:6], ["python3.13t", "-c"])
        self.assertTrue(cmd[6].startswith(PYTHON_PREAMBLE))
        self.assertIn("exec(compile", cmd[6])
        self.assertIn("print('hi')", cmd[6])

    def test_execute_command_rewrites_nested_shell_redirects(self) -> None:
        session = self._create_session()
        with mock.patch.object(session, "_run", return_value=SandboxResult()) as run:
            session.execute_command("sh", ["-c", "echo hi >/tmp/out.txt"])

        cmd = run.call_args.args[0]
        self.assertEqual(
            cmd[:4],
            ["/usr/local/bin/safehouse", f"--workdir={session._workdir}", "--env", "--"],
        )
        self.assertEqual(cmd[4:6], ["bash", "-c"])
        self.assertTrue(cmd[6].startswith(SHELL_PRELUDE))
        self.assertIn("${SANDBOX_ROOT}/tmp/out.txt", cmd[6])

    def test_execute_command_rewrites_absolute_args(self) -> None:
        session = self._create_session()
        with mock.patch.object(session, "_run", return_value=SandboxResult()) as run:
            session.execute_command("cat", ["/tmp/in.txt"])

        cmd = run.call_args.args[0]
        self.assertEqual(cmd[4], "cat")
        self.assertEqual(cmd[5], os.path.join(session._workdir, "tmp/in.txt"))


if __name__ == "__main__":
    unittest.main()
