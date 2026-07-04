from __future__ import annotations

import json
import unittest

from bespokelabs.sandbox import AgentRunResult, Sandbox, Usage, pricing
from bespokelabs.sandbox._usage import parse_claude_result, parse_claude_usage, result_text
from bespokelabs.sandbox.types import SandboxResult


def _claude_json(
    *,
    result: str = "done",
    cost: float = 0.0234,
    input_tokens: int = 1234,
    output_tokens: int = 567,
    cache_read: int = 8900,
    cache_creation: int = 120,
) -> str:
    return json.dumps({
        "type": "result",
        "subtype": "success",
        "result": result,
        "total_cost_usd": cost,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
        },
    })


class UsageParsingTests(unittest.TestCase):
    def test_single_json_object(self) -> None:
        usage = parse_claude_usage(_claude_json())
        assert usage is not None
        self.assertEqual(usage.input_tokens, 1234)
        self.assertEqual(usage.output_tokens, 567)
        self.assertEqual(usage.cache_read_tokens, 8900)
        self.assertEqual(usage.cache_creation_tokens, 120)
        self.assertAlmostEqual(usage.llm_cost_usd, 0.0234)
        self.assertEqual(usage.total_tokens, 1234 + 567 + 8900 + 120)

    def test_stream_json_uses_last_result_record(self) -> None:
        stream = "\n".join([
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps({"type": "assistant", "message": {"content": "thinking"}}),
            _claude_json(result="final", cost=0.01, input_tokens=10, output_tokens=20,
                         cache_read=0, cache_creation=0),
        ])
        usage = parse_claude_usage(stream)
        assert usage is not None
        self.assertEqual(usage.input_tokens, 10)
        self.assertAlmostEqual(usage.llm_cost_usd, 0.01)
        self.assertEqual(result_text(parse_claude_result(stream)), "final")

    def test_text_output_returns_none(self) -> None:
        self.assertIsNone(parse_claude_usage("The answer is 42."))

    def test_empty_and_malformed_return_none(self) -> None:
        self.assertIsNone(parse_claude_usage(""))
        self.assertIsNone(parse_claude_usage("   "))
        self.assertIsNone(parse_claude_usage("{not valid json"))

    def test_record_without_usage_or_cost_returns_none(self) -> None:
        self.assertIsNone(parse_claude_usage(json.dumps({"type": "system", "subtype": "init"})))

    def test_cost_only_record_is_captured(self) -> None:
        usage = parse_claude_usage(json.dumps({"type": "result", "total_cost_usd": 0.5}))
        assert usage is not None
        self.assertAlmostEqual(usage.llm_cost_usd, 0.5)
        self.assertEqual(usage.total_tokens, 0)

    def test_non_numeric_token_values_coerce_to_zero(self) -> None:
        raw = json.dumps({"type": "result", "usage": {"input_tokens": None, "output_tokens": "x"}})
        usage = parse_claude_usage(raw)
        assert usage is not None
        self.assertEqual(usage.total_tokens, 0)

    def test_result_text_missing_returns_none(self) -> None:
        self.assertIsNone(result_text(parse_claude_result(json.dumps({"type": "result", "usage": {}}))))


class UsageArithmeticTests(unittest.TestCase):
    def test_add_sums_every_field(self) -> None:
        a = Usage(input_tokens=1, output_tokens=2, cache_read_tokens=3,
                  cache_creation_tokens=4, llm_cost_usd=0.1, compute_cost_usd=0.5)
        b = Usage(input_tokens=10, output_tokens=20, cache_read_tokens=30,
                  cache_creation_tokens=40, llm_cost_usd=0.9, compute_cost_usd=1.5)
        total = a + b
        self.assertEqual(total.input_tokens, 11)
        self.assertEqual(total.output_tokens, 22)
        self.assertEqual(total.cache_read_tokens, 33)
        self.assertEqual(total.cache_creation_tokens, 44)
        self.assertAlmostEqual(total.llm_cost_usd, 1.0)
        self.assertAlmostEqual(total.compute_cost_usd, 2.0)

    def test_total_cost_combines_llm_and_compute(self) -> None:
        usage = Usage(llm_cost_usd=0.02, compute_cost_usd=0.03)
        self.assertAlmostEqual(usage.total_cost_usd, 0.05)

    def test_add_wrong_type_returns_notimplemented(self) -> None:
        self.assertIs(Usage().__add__(5), NotImplemented)


class _RecordingSession:
    """Stand-in sandbox session that returns canned CLI output."""

    def __init__(self, stdout: str, *, exit_code: int = 0) -> None:
        self.stdout = stdout
        self.exit_code = exit_code
        self.calls: list[tuple[str, list[str] | None]] = []

    def execute_command(self, command: str, args: list[str] | None = None) -> SandboxResult:
        self.calls.append((command, args))
        return SandboxResult(stdout=self.stdout, stderr="", exit_code=self.exit_code)

    def destroy(self) -> None:
        pass


class RunAgentTests(unittest.TestCase):
    def _sandbox(self, stdout: str, *, exit_code: int = 0) -> tuple[Sandbox, _RecordingSession]:
        sb = Sandbox("local")
        session = _RecordingSession(stdout, exit_code=exit_code)
        sb._session = session  # type: ignore[assignment]
        return sb, session

    def test_builds_prompt_and_json_output_args(self) -> None:
        sb, session = self._sandbox(_claude_json())
        try:
            sb.run_agent("review the code")
        finally:
            sb._destroyed = True
        command, args = session.calls[0]
        self.assertEqual(command, "claude")
        self.assertEqual(args, ["-p", "review the code", "--output-format", "json"])

    def test_resume_and_extra_args(self) -> None:
        sb, session = self._sandbox(_claude_json())
        try:
            sb.run_agent("follow up", resume=True, extra_args=["--model", "opus"])
        finally:
            sb._destroyed = True
        _, args = session.calls[0]
        self.assertEqual(args, ["-p", "follow up", "--output-format", "json", "-c", "--model", "opus"])

    def test_result_carries_text_and_usage(self) -> None:
        sb, _ = self._sandbox(_claude_json(result="all good"))
        try:
            result = sb.run_agent("hi")
        finally:
            sb._destroyed = True
        self.assertIsInstance(result, AgentRunResult)
        self.assertEqual(result.text, "all good")
        self.assertEqual(result.usage.input_tokens, 1234)
        self.assertEqual(result.exit_code, 0)

    def test_text_output_falls_back_to_stdout(self) -> None:
        sb, _ = self._sandbox("plain text answer")
        try:
            result = sb.run_agent("hi", output_format="text")
        finally:
            sb._destroyed = True
        self.assertEqual(result.text, "plain text answer")
        self.assertEqual(result.usage, Usage())  # nothing parseable

    def test_usage_accumulates_across_calls(self) -> None:
        sb, _ = self._sandbox(
            _claude_json(input_tokens=100, output_tokens=10, cache_read=0, cache_creation=0, cost=0.01)
        )
        try:
            sb.run_agent("first")
            sb.run_agent("second")
        finally:
            sb._destroyed = True
        self.assertEqual(sb.usage.input_tokens, 200)
        self.assertEqual(sb.usage.output_tokens, 20)
        self.assertAlmostEqual(sb.usage.llm_cost_usd, 0.02)

    def test_resumed_sandbox_has_usage(self) -> None:
        # _from_session() bypasses __init__, so it must still seed _usage.
        from bespokelabs.sandbox.types import SandboxConfig

        sb = Sandbox._from_session("local", _RecordingSession(""), SandboxConfig(backend="local"))
        self.assertEqual(sb.usage, Usage())

    def test_compute_cost_uses_backend_pricing(self) -> None:
        sb, _ = self._sandbox(_claude_json())
        original = pricing.cost_per_second
        pricing.cost_per_second = lambda *a, **k: 0.001  # type: ignore[assignment]
        try:
            self.assertAlmostEqual(sb._compute_cost(10.0), 0.01)
        finally:
            pricing.cost_per_second = original  # type: ignore[assignment]
            sb._destroyed = True


if __name__ == "__main__":
    unittest.main()
