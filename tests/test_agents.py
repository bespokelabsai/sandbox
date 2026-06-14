from __future__ import annotations

import unittest

from bespokelabs.sandbox import AgentSpec, Sandbox, SandboxError
from bespokelabs.sandbox.types import SandboxResult


class AgentSpecTests(unittest.TestCase):
    def test_inside_requires_command(self) -> None:
        with self.assertRaises(ValueError):
            AgentSpec(name="empty", placement="inside", command=[])

    def test_external_rejects_inside_command(self) -> None:
        with self.assertRaises(ValueError):
            AgentSpec(name="bad", placement="external", command=["echo"])

    def test_unknown_capability_raises(self) -> None:
        with self.assertRaises(ValueError):
            AgentSpec.external(name="agent", capabilities=["database"])  # type: ignore[list-item]


class InsideAgentSessionTests(unittest.TestCase):
    def test_inside_agent_argv_input(self) -> None:
        with Sandbox("local") as sb:
            agent = sb.agent(AgentSpec.inside(
                name="upper",
                command=["python3", "-c", "import sys; print(sys.argv[1].upper())"],
                input_mode="argv",
            ))

            result = agent.run("hello")

            self.assertIsInstance(result, SandboxResult)
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.stdout.strip(), "HELLO")

    def test_inside_agent_stdin_input(self) -> None:
        with Sandbox("local") as sb:
            agent = sb.agent(AgentSpec.inside(
                name="reverse",
                command=["python3", "-c", "import sys; print(sys.stdin.read().strip()[::-1])"],
            ))

            result = agent.run("drawer")

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.stdout.strip(), "reward")

    def test_inside_agent_file_input(self) -> None:
        with Sandbox("local") as sb:
            agent = sb.agent(AgentSpec.inside(
                name="file-reader",
                command=[
                    "python3",
                    "-c",
                    "import pathlib, sys; print(pathlib.Path(sys.argv[1]).read_text().strip())",
                ],
                input_mode="file",
                input_path="/tmp/prompt.txt",
            ))

            result = agent.run("from file")

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.stdout.strip(), "from file")

    def test_inside_agent_file_input_does_not_require_files_capability(self) -> None:
        with Sandbox("local") as sb:
            agent = sb.agent(AgentSpec.inside(
                name="file-reader",
                command=[
                    "python3",
                    "-c",
                    "import pathlib, sys; print(pathlib.Path(sys.argv[1]).read_text().strip())",
                ],
                input_mode="file",
                input_path="/tmp/prompt.txt",
                capabilities=[],
            ))

            result = agent.run("from launch plumbing")

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.stdout.strip(), "from launch plumbing")

    def test_inside_agent_honors_env_and_cwd(self) -> None:
        with Sandbox("local") as sb:
            sb.write_file("/workspace/marker.txt", "ok")
            agent = sb.agent(AgentSpec.inside(
                name="env-cwd",
                command=[
                    "python3",
                    "-c",
                    "import os, pathlib; print(os.environ['AGENT_NAME'], pathlib.Path('marker.txt').read_text())",
                ],
                cwd="/workspace",
                env={"AGENT_NAME": "runner"},
                input_mode="none",
            ))

            result = agent.run("ignored")

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.stdout.strip(), "runner ok")


class ExternalAgentSessionTests(unittest.TestCase):
    def test_external_agent_runner_receives_context(self) -> None:
        def runner(ctx, prompt: str) -> str:
            ctx.write_file("/prompt.txt", prompt)
            return ctx.read_file("/prompt.txt").decode()

        with Sandbox("local") as sb:
            agent = sb.agent(AgentSpec.external(
                name="outside",
                capabilities=["files"],
                runner=runner,
            ))

            self.assertEqual(agent.run("external prompt"), "external prompt")

    def test_agent_tools_enforce_capabilities(self) -> None:
        with Sandbox("local") as sb:
            tools = sb.agent_tools(capabilities=["files"])
            tools.write_file("/allowed.txt", "ok")

            self.assertEqual(tools.read_file("/allowed.txt"), b"ok")
            with self.assertRaises(SandboxError):
                tools.shell("echo should-not-run")

    def test_empty_capabilities_allow_nothing(self) -> None:
        with Sandbox("local") as sb:
            tools = sb.agent_tools(capabilities=[])

            with self.assertRaises(SandboxError):
                tools.shell("echo should-not-run")
            with self.assertRaises(SandboxError):
                tools.write_file("/blocked.txt", "no")

    def test_external_agent_without_runner_cannot_run(self) -> None:
        with Sandbox("local") as sb:
            agent = sb.agent(AgentSpec.external(name="outside", capabilities=["shell"]))

            with self.assertRaises(SandboxError):
                agent.run("prompt")


if __name__ == "__main__":
    unittest.main()
