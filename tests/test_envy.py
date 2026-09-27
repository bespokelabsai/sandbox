from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from bespokelabs.sandbox import SandboxResult
from bespokelabs.sandbox.envy import (
    EnvyError,
    EnvySupervisor,
    SandboxConversation,
)


class StubSupervisor(EnvySupervisor):

    def _request(self, route, data=None, *, token=None):
        if route == "/enrollment/me":
            return {"human": {"id": "owner"}}
        if route == "/agents/register":
            return {"token": "test-agent-token", "agent": {"id": "a"}}
        if route == "/state":
            return {
                "actor": {"id": "a"},
                "channels": [{"id": "c", "name": "general"}],
            }
        if route.startswith("/channels/"):
            return {"messages": [{"id": 1, "content": "Original request"}]}
        return {}


class EnvySupervisorTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "listener.json"
        self.delivery = {
            "id": 7,
            "receipt": "r",
            "message": {
                "id": 2,
                "parent_id": 1,
                "channel_id": "c",
                "content": "hello",
            },
        }

    def supervisor(self, **kwargs):
        return StubSupervisor(
            name="test-agent",
            state_path=self.state,
            user_token="human-token",
            **kwargs,
        )

    def test_private_persistent_identity_and_exclusive_process_lock(self):
        with self.supervisor() as first:
            key = first.state["registrationKey"]
            self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(BlockingIOError), self.supervisor():
                pass
        with self.supervisor() as second:
            self.assertEqual(second.state["registrationKey"], key)

    def test_reject_changed_owner_or_url_without_overwriting_state(self):
        with self.supervisor() as first:
            first.state["owner_id"] = "another-owner"
            first._save()
        before = self.state.read_bytes()
        with (
            self.assertRaisesRegex(ValueError, "another human"),
            self.supervisor(),
        ):
            pass
        self.assertEqual(self.state.read_bytes(), before)
        with (
            self.assertRaisesRegex(ValueError, "another Envy"),
            self.supervisor(url="http://localhost:8791"),
        ):
            pass
        with self.assertRaises(ValueError):
            self.supervisor(url="http://untrusted.example")

    def test_lost_send_response_replays_cached_output_after_restart(self):
        calls = []

        def handler(prompt, session, remember):
            calls.append(prompt)
            remember("conversation-123")
            return "Reply from agent"

        with self.supervisor() as first:
            request = first._request

            def failed_send(route, data=None, **kwargs):
                if route == "/messages":
                    raise ConnectionError("lost response")
                return request(route, data, **kwargs)

            first._request = Mock(side_effect=failed_send)
            with self.assertRaises(ConnectionError):
                first._process(self.delivery, handler)
            sent = [
                c.args[1]
                for c in first._request.call_args_list
                if c.args[0] == "/messages"
            ][0]
            self.assertIn("Original request", calls[0])
            self.assertIn("untrusted", calls[0])
        with self.supervisor() as second:
            self.assertEqual(second.state["session_id"], "conversation-123")
            second._request = Mock(wraps=second._request)
            second._process(self.delivery, handler)
            resent = [
                c.args[1]
                for c in second._request.call_args_list
                if c.args[0] == "/messages"
            ][0]
            self.assertEqual(sent, resent)
            self.assertEqual(second.state["completed"], {})
        self.assertEqual(len(calls), 1)

    def test_failed_handler_releases_without_ack_and_no_reply_acknowledges(
        self,
    ):
        with self.supervisor() as listener:
            listener._settle = Mock()
            with self.assertRaises(RuntimeError):
                listener._process(
                    self.delivery, Mock(side_effect=RuntimeError("failed"))
                )
            self.assertEqual(listener._settle.call_args.args[1], "release")
            self.assertEqual(listener.state["completed"], {})
            listener._request = Mock(wraps=listener._request)
            listener._process(self.delivery, lambda *_: "ENVY_NO_REPLY")
            self.assertEqual(listener._settle.call_args.args[1], "ack")
            self.assertFalse(
                any(
                    c.args[0] == "/messages"
                    for c in listener._request.call_args_list
                )
            )

    def test_long_replies_are_split_with_stable_ids_and_original_thread(self):
        with self.supervisor() as listener:
            listener._request = Mock(wraps=listener._request)
            listener._process(self.delivery, lambda *_: "🙂" * 9000)
            messages = [
                c.args[1]
                for c in listener._request.call_args_list
                if c.args[0] == "/messages"
            ]
            self.assertEqual(
                [m["clientId"] for m in messages],
                ["envy-delivery-7-0", "envy-delivery-7-1", "envy-delivery-7-2"],
            )
            self.assertTrue(all(m["parentId"] == 1 for m in messages))
            self.assertEqual(
                "".join(m["content"] for m in messages), "🙂" * 9000
            )

    def test_lost_lease_keeps_completed_reply_without_sending_or_ack(self):
        with self.supervisor() as listener:
            listener._settle = Mock(side_effect=EnvyError(409))
            listener._request = Mock(wraps=listener._request)
            with self.assertRaises(EnvyError):
                listener._process(self.delivery, lambda *_: "cached")
            self.assertEqual(listener.state["completed"]["7"], "cached")
            self.assertFalse(
                any(
                    c.args[0] == "/messages"
                    for c in listener._request.call_args_list
                )
            )


class SandboxConversationTests(unittest.TestCase):

    def runner(self, stdout, exit_code=0, harness="codex"):
        sandbox = Mock()
        sandbox.agent.return_value.run.return_value = SandboxResult(
            stdout=stdout, stderr="", exit_code=exit_code
        )
        return SandboxConversation(sandbox, harness=harness), sandbox

    def test_codex_resumes_exact_session_and_preserves_permissions(self):
        events = [
            {"type": "thread.started", "thread_id": "session-1"},
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "done"},
            },
            {"type": "turn.completed"},
        ]
        runner, sandbox = self.runner("\n".join(json.dumps(e) for e in events))
        remember = Mock()
        self.assertEqual(runner("message", None, remember), "done")
        remember.assert_called_once_with("session-1")
        self.assertEqual(
            sandbox.agent.call_args.args[0].command,
            ["codex", "exec", "--json", "-"],
        )
        runner("followup", "session-1", remember)
        self.assertEqual(
            sandbox.agent.call_args.args[0].command,
            ["codex", "exec", "resume", "--json", "session-1", "-"],
        )
        self.assertEqual(sandbox.agent.call_args.args[0].input_mode, "stdin")

    def test_codex_failure_remembers_session_and_rejects_incomplete_turn(self):
        runner, _ = self.runner(
            '{"type":"thread.started","thread_id":"failed-session"}\n{"type":"turn.failed"}'
        )
        remember = Mock()
        with self.assertRaises(RuntimeError):
            runner("message", None, remember)
        remember.assert_called_once_with("failed-session")

    def test_claude_resumes_exact_session_and_rejects_error_results(self):
        runner, sandbox = self.runner(
            json.dumps(
                {"session_id": "c1", "result": "done", "is_error": False}
            ),
            harness="claude-code",
        )
        remember = Mock()
        self.assertEqual(runner("message", "c1", remember), "done")
        self.assertEqual(
            sandbox.agent.call_args.args[0].command[-2:], ["--resume", "c1"]
        )
        failed, _ = self.runner(
            json.dumps(
                {"session_id": "c1", "result": "failed", "is_error": True}
            ),
            harness="claude-code",
        )
        with self.assertRaises(RuntimeError):
            failed("message", "c1", remember)


if __name__ == "__main__":
    unittest.main()
