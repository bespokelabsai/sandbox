"""Tests for the return_type parameter on execute_code / execute_command."""

from __future__ import annotations

import unittest

from pydantic import BaseModel

from bespokelabs.sandbox import Sandbox, SandboxExecutionError, json_schema


class Stats(BaseModel):
    mean: float
    count: int


class Greeting(BaseModel):
    message: str
    extra: str = ""


class TestReturnTypeExecuteCode(unittest.TestCase):
    """Test return_type with execute_code on the local backend."""

    def setUp(self):
        self.sb = Sandbox("local")

    def tearDown(self):
        self.sb.destroy()

    def test_basic_model(self):
        code = 'import json; print(json.dumps({"mean": 3.14, "count": 42}))'
        result = self.sb.execute_code(code, return_type=Stats)
        self.assertIsInstance(result, Stats)
        self.assertAlmostEqual(result.mean, 3.14)
        self.assertEqual(result.count, 42)

    def test_extra_fields_ignored(self):
        """Extra JSON keys not in the model should be silently dropped."""
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

    def test_missing_required_field_raises(self):
        """Missing a required field should raise SandboxExecutionError, not ValidationError."""
        code = 'import json; print(json.dumps({"mean": 3.14}))'  # missing 'count'
        with self.assertRaises(SandboxExecutionError):
            self.sb.execute_code(code, return_type=Stats)

    def test_wrong_type_raises(self):
        """Wrong field type should raise SandboxExecutionError."""
        code = 'import json; print(json.dumps({"mean": "not_a_number", "count": 42}))'
        # Pydantic coerces strings to floats when possible, so use something truly invalid
        code = 'import json; print(json.dumps({"mean": "abc", "count": 42}))'
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
            "echo",
            args=['{"message": "from_cmd"}'],
            return_type=Greeting,
        )
        self.assertIsInstance(result, Greeting)
        self.assertEqual(result.message, "from_cmd")


class TestJsonSchema(unittest.TestCase):
    """Test json_schema() helper."""

    def test_pydantic_model_schema(self):
        schema = json_schema(Stats)
        self.assertIn("Return ONLY a JSON object matching this schema:", schema)
        self.assertIn('"mean"', schema)
        self.assertIn('"count"', schema)

    def test_pydantic_optional_field(self):
        class WithOptional(BaseModel):
            name: str
            value: int | None = None

        schema = json_schema(WithOptional)
        self.assertIn('"name"', schema)
        self.assertIn('"value"', schema)


if __name__ == "__main__":
    unittest.main()
