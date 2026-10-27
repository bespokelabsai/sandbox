"""Tests for sandbox lifecycle telemetry events."""

from __future__ import annotations

import logging
import unittest

from bespokelabs.sandbox import (
    ErrorCode,
    Sandbox,
    SandboxCreationError,
    SandboxExecutionError,
    telemetry,
)
from bespokelabs.sandbox.types import SandboxConfig, SandboxResult


class _Session:
    def __init__(self, exit_code: int = 0, fail: bool = False) -> None:
        self.exit_code = exit_code
        self.fail = fail
        self.destroyed = False

    def execute_command(self, command, args=None):
        if self.fail:
            raise SandboxExecutionError("secret-token", backend="fake")
        return SandboxResult(stdout="x", exit_code=self.exit_code)

    def execute_code(self, code, language="python"):
        return SandboxResult(exit_code=self.exit_code)

    def destroy(self):
        self.destroyed = True


class _Client:
    def __init__(self, session=None, error: Exception | None = None):
        self.session = session or _Session()
        self.error = error

    def create(self, config: SandboxConfig):
        if self.error:
            raise self.error
        return self.session


class TelemetryTests(unittest.TestCase):

    def setUp(self) -> None:
        self.events: list[telemetry.SandboxEvent] = []
        telemetry.add_listener(self.events.append)
        self.addCleanup(telemetry.remove_listener, self.events.append)

    def _sandbox(self, **kw) -> Sandbox:
        return Sandbox("local", _backend_client=_Client(**kw))

    def test_create_exec_destroy_events(self) -> None:
        sb = self._sandbox(session=_Session(exit_code=3))
        sb.execute_command("false")
        sb.execute_code("print(1)")
        sb.destroy()
        sb.destroy()  # idempotent: no second event

        self.assertEqual(
            [e.op for e in self.events],
            ["create", "setup", "exec", "exec", "destroy"],
        )
        self.assertTrue(all(e.outcome == "ok" for e in self.events))
        self.assertTrue(all(e.backend == "local" for e in self.events))
        # A non-zero exit is the command's result, not a sandbox failure.
        execs = [e for e in self.events if e.op == "exec"]
        self.assertEqual([e.exit_code for e in execs], [3, 3])
        destroy = self.events[-1]
        self.assertIsNotNone(destroy.lifetime_secs)
        self.assertGreaterEqual(destroy.lifetime_secs, 0)

    def test_run_agent_emits_exec_event(self) -> None:
        sb = self._sandbox()
        sb.run_agent("hi")
        execs = [e for e in self.events if e.op == "exec"]
        self.assertEqual(len(execs), 1)
        self.assertEqual(execs[0].extra.get("kind"), "agent")

    def test_listeners_run_when_logging_fails(self) -> None:
        class Boom(logging.Handler):
            def emit(self, record):
                raise RuntimeError("handler")

        h = Boom()
        telemetry.logger.addHandler(h)
        self.addCleanup(telemetry.logger.removeHandler, h)
        self._sandbox().execute_command("true")
        self.assertTrue(any(e.op == "exec" for e in self.events))

    def test_create_failure_records_code_and_type(self) -> None:
        with self.assertRaises(SandboxCreationError):
            self._sandbox(error=RuntimeError("provider said token=abc"))

        (event,) = self.events
        self.assertEqual(event.op, "create")
        self.assertEqual(event.outcome, "error")
        self.assertEqual(event.error_code, ErrorCode.CREATION_FAILED.value)
        self.assertEqual(event.error_type, "SandboxCreationError")

    def test_exec_failure_never_records_message(self) -> None:
        sb = self._sandbox(session=_Session(fail=True))
        self.events.clear()
        with self.assertRaises(SandboxExecutionError):
            sb.execute_command("echo", ["hello"])

        (event,) = self.events
        self.assertEqual(event.outcome, "error")
        self.assertEqual(event.error_code, ErrorCode.EXECUTION_FAILED.value)
        self.assertNotIn("secret-token", repr(event))
        self.assertNotIn("hello", repr(event))

    def test_non_sandbox_exception_is_unknown(self) -> None:
        with telemetry.span("exec", "fake"):
            pass
        with self.assertRaises(ValueError):
            with telemetry.span("exec", "fake"):
                raise ValueError("boom")
        self.assertEqual(self.events[-1].error_code, "unknown")
        self.assertEqual(self.events[-1].error_type, "ValueError")

    def test_broken_listener_does_not_break_sandbox(self) -> None:
        def bad(_event):
            raise RuntimeError("listener bug")

        telemetry.add_listener(bad)
        self.addCleanup(telemetry.remove_listener, bad)
        with self.assertLogs("bespokelabs.sandbox.telemetry", "ERROR"):
            sb = self._sandbox()
            self.assertEqual(sb.execute_command("true").exit_code, 0)
        self.assertEqual(self.events[0].op, "create")

    def test_failures_log_at_warning_successes_at_info(self) -> None:
        with self.assertLogs(
            "bespokelabs.sandbox.telemetry", logging.INFO
        ) as ctx:
            sb = self._sandbox()
            sb.destroy()
        self.assertTrue(all(r.levelno == logging.INFO for r in ctx.records))
        with self.assertLogs(
            "bespokelabs.sandbox.telemetry", logging.WARNING
        ) as ctx:
            with self.assertRaises(SandboxCreationError):
                self._sandbox(error=RuntimeError("x"))
        self.assertEqual(ctx.records[0].levelno, logging.WARNING)
        self.assertIn("error_code=creation_failed", ctx.output[0])

    def test_resumed_sandbox_has_lifetime(self) -> None:
        sb = Sandbox._from_session(
            "local", _Session(), SandboxConfig(backend="local")
        )
        sb.destroy()
        self.assertEqual(self.events[-1].op, "destroy")
        self.assertIsNotNone(self.events[-1].lifetime_secs)


if __name__ == "__main__":
    unittest.main()
