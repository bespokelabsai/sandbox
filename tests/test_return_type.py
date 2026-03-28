"""Tests for the return_type parameter on execute_code / execute_command."""

from __future__ import annotations

import dataclasses
import unittest

from bespokelabs.sandbox import Sandbox, SandboxExecutionError


@dataclasses.dataclass
class Stats:
    mean: float
    count: int


@dataclasses.dataclass
class Greeting:
    message: str
    extra: str = ""


class TestReturnTypeExecuteCode(unittest.TestCase):
    """Test return_type with execute_code on the local backend."""

    def setUp(self):
        self.sb = Sandbox("local")

    def tearDown(self):
        self.sb.destroy()

    def test_basic_dataclass(self):
        code = 'import json; print(json.dumps({"mean": 3.14, "count": 42}))'
        result = self.sb.execute_code(code, return_type=Stats)
        self.assertIsInstance(result, Stats)
        self.assertAlmostEqual(result.mean, 3.14)
        self.assertEqual(result.count, 42)

    def test_extra_fields_ignored(self):
        """Extra JSON keys not in the dataclass should be silently dropped."""
        code = 'import json; print(json.dumps({"mean": 1.0, "count": 2, "extra_key": "ignored"}))'
        result = self.sb.execute_code(code, return_type=Stats)
        self.assertIsInstance(result, Stats)
        self.assertEqual(result.count, 2)

    def test_json_in_markdown_fence(self):
        code = r'print("```json\n{\"message\": \"hello\"}\n```")'
        result = self.sb.execute_code(code, return_type=Greeting)
        self.assertIsInstance(result, Greeting)
        self.assertEqual(result.message, "hello")

    def test_json_with_surrounding_text(self):
        code = 'print("Here is the result:\\n{\\\"message\\\": \\\"world\\\"}\\nDone.")'
        result = self.sb.execute_code(code, return_type=Greeting)
        self.assertIsInstance(result, Greeting)
        self.assertEqual(result.message, "world")

    def test_non_zero_exit_raises(self):
        code = "import sys; sys.exit(1)"
        with self.assertRaises(SandboxExecutionError):
            self.sb.execute_code(code, return_type=Stats)

    def test_non_json_stdout_raises(self):
        code = 'print("this is not json at all")'
        with self.assertRaises(SandboxExecutionError):
            self.sb.execute_code(code, return_type=Stats)

    def test_without_return_type_unchanged(self):
        """Without return_type, normal SandboxResult is returned."""
        from bespokelabs.sandbox.types import SandboxResult
        code = 'print("hello")'
        result = self.sb.execute_code(code)
        self.assertIsInstance(result, SandboxResult)


class TestReturnTypeExecuteCommand(unittest.TestCase):
    """Test return_type with execute_command."""

    def setUp(self):
        self.sb = Sandbox("local")

    def tearDown(self):
        self.sb.destroy()

    def test_command_with_return_type(self):
        result = self.sb.execute_command(
            "echo", args=['{"message": "from_cmd"}'], return_type=Greeting,
        )
        self.assertIsInstance(result, Greeting)
        self.assertEqual(result.message, "from_cmd")


if __name__ == "__main__":
    unittest.main()
