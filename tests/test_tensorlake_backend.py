from __future__ import annotations

import unittest
from types import SimpleNamespace

from bespokelabs.sandbox.backends.tensorlake import TensorlakeSession


class _FakeTensorlakeSandbox:
    sandbox_id = "sbx-test"

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def run(self, command: str, args: list[str]) -> object:
        self.calls.append((command, args))
        return SimpleNamespace(stdout="ok", stderr="", exit_code=0)

    def close(self) -> None:
        pass


class TensorlakeSessionTests(unittest.TestCase):
    def test_execute_command_runs_from_default_writable_workdir_with_user_npm_path(self) -> None:
        sandbox = _FakeTensorlakeSandbox()
        session = TensorlakeSession(client=object(), sandbox=sandbox)

        result = session.execute_command(
            "git",
            ["clone", "--depth", "1", "https://github.com/acme/project.git", "project"],
        )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(sandbox.calls[0][0], "bash")
        self.assertEqual(
            sandbox.calls[0][1],
            [
                "-c",
                'export PATH="$HOME/.npm-global/bin:$PATH"; '
                "mkdir -p /tmp && cd /tmp && "
                "git clone --depth 1 https://github.com/acme/project.git project",
            ],
        )

    def test_execute_command_quotes_custom_workdir(self) -> None:
        sandbox = _FakeTensorlakeSandbox()
        session = TensorlakeSession(
            client=object(),
            sandbox=sandbox,
            workdir="/tmp/sandbox work",
        )

        session.execute_command("pwd")

        self.assertEqual(
            sandbox.calls[0][1],
            [
                "-c",
                'export PATH="$HOME/.npm-global/bin:$PATH"; '
                "mkdir -p '/tmp/sandbox work' && cd '/tmp/sandbox work' && pwd",
            ],
        )

    def test_session_state_preserves_workdir(self) -> None:
        session = TensorlakeSession(
            client=object(),
            sandbox=_FakeTensorlakeSandbox(),
            workdir="/tmp/project",
        )

        self.assertEqual(
            session.session_state(),
            {"sandbox_id": "sbx-test", "workdir": "/tmp/project"},
        )


if __name__ == "__main__":
    unittest.main()
