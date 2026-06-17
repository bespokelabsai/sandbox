"""Tests for the structured error taxonomy in bespokelabs.sandbox.exceptions."""

from __future__ import annotations

import unittest

from pydantic import BaseModel

from bespokelabs.sandbox import (
    BackendNotInstalledError,
    CommandFailedError,
    ErrorCode,
    FeatureNotSupportedError,
    Sandbox,
    SandboxConfigurationError,
    SandboxConnectionError,
    SandboxCreationError,
    SandboxError,
    SandboxExecutionError,
    SandboxNotFoundError,
    SandboxTimeoutError,
    WorkspaceError,
)


class BackwardCompatTests(unittest.TestCase):
    def test_plain_message_unchanged(self) -> None:
        err = SandboxError("boom")
        self.assertEqual(str(err), "boom")
        self.assertEqual(err.message, "boom")
        self.assertEqual(err.code, ErrorCode.UNKNOWN)
        self.assertFalse(err.retryable)
        self.assertEqual(err.context, {})
        self.assertIsNone(err.backend)
        self.assertIsNone(err.op)

    def test_subclass_plain_message_has_no_suffix(self) -> None:
        # Even though subclasses carry a non-UNKNOWN code, a bare message must
        # render exactly as before (no structured fields -> no suffix).
        self.assertEqual(str(SandboxCreationError("nope")), "nope")
        self.assertEqual(str(SandboxExecutionError("nope")), "nope")


class StructuredContextTests(unittest.TestCase):
    def test_enriched_str_includes_fields(self) -> None:
        err = SandboxCreationError(
            "create failed", backend="daytona", op="create", context={"exit_code": 1}
        )
        s = str(err)
        self.assertIn("create failed", s)
        self.assertIn("backend=daytona", s)
        self.assertIn("op=create", s)
        self.assertIn("code=creation_failed", s)
        self.assertIn("exit_code=1", s)

    def test_retryable_shown_only_when_true(self) -> None:
        self.assertIn("retryable", str(SandboxTimeoutError("t", op="exec")))
        self.assertNotIn("retryable", str(SandboxCreationError("c", op="create")))

    def test_cause_is_preserved(self) -> None:
        root = ValueError("root")
        try:
            try:
                raise root
            except ValueError as exc:
                raise SandboxCreationError("wrapped", backend="modal") from exc
        except SandboxCreationError as e:
            self.assertIs(e.__cause__, root)

    def test_per_instance_overrides(self) -> None:
        err = SandboxCreationError("x", retryable=True, code=ErrorCode.CONNECTION)
        self.assertTrue(err.retryable)
        self.assertEqual(err.code, ErrorCode.CONNECTION)


class SubclassDefaultsTests(unittest.TestCase):
    def test_codes_and_retryability(self) -> None:
        cases = [
            (BackendNotInstalledError, ErrorCode.BACKEND_NOT_INSTALLED, False),
            (SandboxConfigurationError, ErrorCode.CONFIGURATION, False),
            (SandboxCreationError, ErrorCode.CREATION_FAILED, False),
            (SandboxExecutionError, ErrorCode.EXECUTION_FAILED, False),
            (CommandFailedError, ErrorCode.COMMAND_FAILED, False),
            (SandboxTimeoutError, ErrorCode.TIMEOUT, True),
            (SandboxConnectionError, ErrorCode.CONNECTION, True),
            (SandboxNotFoundError, ErrorCode.NOT_FOUND, False),
            (FeatureNotSupportedError, ErrorCode.FEATURE_NOT_SUPPORTED, False),
            (WorkspaceError, ErrorCode.WORKSPACE, False),
        ]
        for cls, code, retryable in cases:
            err = cls("msg")
            self.assertEqual(err.code, code, cls.__name__)
            self.assertEqual(err.retryable, retryable, cls.__name__)
            self.assertIsInstance(err, SandboxError, cls.__name__)

    def test_command_failed_is_execution_error_and_carries_fields(self) -> None:
        err = CommandFailedError("boom", exit_code=42, stdout="out", stderr="err")
        self.assertIsInstance(err, SandboxExecutionError)
        self.assertEqual(err.exit_code, 42)
        self.assertEqual(err.stdout, "out")
        self.assertEqual(err.stderr, "err")
        self.assertEqual(err.context["exit_code"], 42)


class IntegrationTests(unittest.TestCase):
    def test_unknown_backend_is_configuration_error(self) -> None:
        with self.assertRaises(SandboxConfigurationError) as ctx:
            Sandbox("not-a-backend")
        # Still catchable as the base type (backward compatible).
        self.assertIsInstance(ctx.exception, SandboxError)
        self.assertEqual(ctx.exception.context.get("backend"), "not-a-backend")

    def test_nonzero_exit_with_return_type_raises_command_failed(self) -> None:
        class M(BaseModel):
            x: int = 0

        with Sandbox("local") as sb:
            with self.assertRaises(CommandFailedError) as ctx:
                sb.execute_command("sh", ["-c", "echo bad >&2; exit 3"], return_type=M)
        err = ctx.exception
        self.assertEqual(err.exit_code, 3)
        self.assertEqual(err.code, ErrorCode.COMMAND_FAILED)
        self.assertIn("exit_code", err.context)
        # Old code catching SandboxExecutionError still works.
        self.assertIsInstance(err, SandboxExecutionError)


if __name__ == "__main__":
    unittest.main()
