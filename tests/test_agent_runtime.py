from __future__ import annotations

import unittest

from bespokelabs.sandbox._agent_runtime import (
    build_inside_shell_script,
    build_patch_apply_command,
    normalize_sandbox_path,
    prepare_inside_command,
)


class AgentRuntimeTests(unittest.TestCase):
    def test_normalize_sandbox_path(self) -> None:
        self.assertEqual(normalize_sandbox_path("prompt.txt"), "/prompt.txt")
        self.assertEqual(normalize_sandbox_path("/tmp/prompt.txt"), "/tmp/prompt.txt")

    def test_prepare_inside_command_injects_python_preamble(self) -> None:
        command = ["python3", "-c", "print(open('/hello.txt').read())"]

        prepared = prepare_inside_command(command)

        self.assertEqual(command, ["python3", "-c", "print(open('/hello.txt').read())"])
        self.assertEqual(prepared[:2], ["python3", "-c"])
        self.assertIn("SANDBOX_ROOT", prepared[2])
        self.assertIn("print(open('/hello.txt').read())", prepared[2])

    def test_prepare_inside_command_injects_shell_prelude(self) -> None:
        prepared = prepare_inside_command(["bash", "-c", "cat /hello.txt > /out.txt"])

        self.assertEqual(prepared[:2], ["bash", "-c"])
        self.assertIn("__sb_run", prepared[2])
        self.assertIn("${SANDBOX_ROOT}/out.txt", prepared[2])

    def test_build_inside_shell_script_uses_normalized_paths(self) -> None:
        script = build_inside_shell_script(
            command=["python3", "reader.py"],
            input_mode="file",
            prompt="hello",
            cwd="/workspace",
            env={"AGENT_NAME": "runner"},
            input_path="/prompt.txt",
        )

        self.assertIn("export AGENT_NAME=runner", script)
        self.assertIn('cd "${SANDBOX_ROOT:-}/workspace"', script)
        self.assertIn('"${SANDBOX_ROOT:-}/prompt.txt"', script)

    def test_build_patch_apply_command_rebases_patch_path(self) -> None:
        command = build_patch_apply_command(patch_path="/tmp/agent.patch", strip=1)

        self.assertEqual(command, 'patch -p1 < "${SANDBOX_ROOT:-}/tmp/agent.patch"')


if __name__ == "__main__":
    unittest.main()

