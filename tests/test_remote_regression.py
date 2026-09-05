"""Regression coverage for remote request failures and sandbox lifecycle."""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest import mock

from bespokelabs.sandbox.remote import (
    RemoteSandbox,
    RemoteSandboxClient,
    RemoteSandboxError,
)
from tests.test_remote import FakeResponse


class RemoteRegressionTest(unittest.TestCase):

    def setUp(self) -> None:
        patcher = mock.patch(
            "bespokelabs.sandbox.remote.urllib.request.urlopen"
        )
        self.urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        self.client = RemoteSandboxClient(
            "https://sandbox.example///", "product-key", timeout_secs=7.5
        )
        self.sandbox = RemoteSandbox(self.client, "sbx_1", "daytona")

    def test_failed_destroy_can_be_retried(self) -> None:
        self.urlopen.side_effect = [
            urllib.error.URLError("connection lost"),
            FakeResponse({"status": "destroyed"}),
        ]

        with self.assertRaises(RemoteSandboxError):
            self.sandbox.destroy()
        self.assertTrue(self.sandbox.is_alive)

        self.sandbox.destroy()
        self.sandbox.destroy()

        self.assertFalse(self.sandbox.is_alive)
        self.assertEqual(self.urlopen.call_count, 2)
        for call in self.urlopen.call_args_list:
            self.assertEqual(call.args[0].get_method(), "DELETE")
            self.assertEqual(
                call.args[0].full_url,
                "https://sandbox.example/v1/sandboxes/sbx_1",
            )

    def test_destroyed_sandbox_rejects_both_execution_methods(self) -> None:
        self.urlopen.return_value = FakeResponse({"status": "destroyed"})
        self.sandbox.destroy()
        self.urlopen.reset_mock()

        for execute in (
            self.sandbox.execute_code,
            self.sandbox.execute_command,
        ):
            with self.subTest(method=execute.__name__):
                with self.assertRaisesRegex(
                    RemoteSandboxError, "sandbox has been destroyed"
                ):
                    execute("echo hello")
        self.urlopen.assert_not_called()

    def test_context_manager_cleans_up_after_body_failure(self) -> None:
        self.urlopen.return_value = FakeResponse({"status": "destroyed"})
        failure = ValueError("caller failed")

        with self.assertRaises(ValueError) as ctx:
            with self.sandbox:
                raise failure

        self.assertIs(ctx.exception, failure)
        self.assertFalse(self.sandbox.is_alive)
        self.urlopen.assert_called_once()
        self.assertEqual(self.urlopen.call_args.args[0].get_method(), "DELETE")

    def test_empty_delete_response_marks_sandbox_destroyed(self) -> None:
        response = FakeResponse(None)
        response._body = b""
        self.urlopen.return_value = response

        self.sandbox.destroy()

        self.assertFalse(self.sandbox.is_alive)

    def test_connection_failure_is_structured_without_automatic_retry(
        self,
    ) -> None:
        failure = urllib.error.URLError("connection refused")
        self.urlopen.side_effect = failure

        with self.assertRaises(RemoteSandboxError) as ctx:
            self.sandbox.execute_code("print(42)")

        error = ctx.exception
        self.assertEqual(str(error), "control plane unavailable")
        self.assertEqual(error.code, "connection")
        self.assertEqual(error.op, "http_request")
        self.assertTrue(error.retryable)
        self.assertEqual(error.outcome, "unknown")
        self.assertIsNone(error.status_code)
        self.assertIs(error.__cause__, failure)
        self.urlopen.assert_called_once()

    def test_non_json_http_errors_preserve_status_and_cause(self) -> None:
        for body in (b"<html>Bad gateway</html>", b"\xff\xfe"):
            with self.subTest(body=body):
                failure = urllib.error.HTTPError(
                    "https://sandbox.example/v1/sandboxes",
                    502,
                    "Bad Gateway",
                    {},
                    io.BytesIO(body),
                )
                self.urlopen.side_effect = failure

                with self.assertRaises(RemoteSandboxError) as ctx:
                    self.client.list()

                self.assertEqual(ctx.exception.status_code, 502)
                self.assertEqual(str(ctx.exception), str(failure))
                self.assertIs(ctx.exception.__cause__, failure)

    def test_string_http_error_detail_is_preserved(self) -> None:
        self.urlopen.side_effect = urllib.error.HTTPError(
            "https://sandbox.example/v1/sandboxes",
            403,
            "Forbidden",
            {},
            io.BytesIO(json.dumps({"detail": "Access denied"}).encode()),
        )

        with self.assertRaises(RemoteSandboxError) as ctx:
            self.client.list()

        self.assertEqual(str(ctx.exception), "Access denied")
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertFalse(ctx.exception.retryable)

    def test_execution_preserves_arguments_and_nonzero_result(self) -> None:
        self.urlopen.return_value = FakeResponse(
            {
                "stdout": "partial output\n",
                "stderr": "command failed\n",
                "exit_code": 42,
                "request_id": "request-stable",
                "usage": {"customer_cost_usd": "0.003"},
            }
        )
        args = ["a b", "quotes'\"", "", "日本語"]

        result = self.sandbox.execute_command(
            "example", args, idempotency_key="request-stable"
        )

        request = self.urlopen.call_args.args[0]
        self.assertEqual(
            json.loads(request.data), {"command": "example", "args": args}
        )
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(
            request.get_header("Idempotency-key"), "request-stable"
        )
        self.assertEqual(result.exit_code, 42)
        self.assertEqual(result.stdout, "partial output\n")
        self.assertEqual(result.stderr, "command failed\n")
        self.assertEqual(result.request_id, "request-stable")
        self.assertEqual(result.usage, {"customer_cost_usd": "0.003"})

    def test_separate_executions_receive_distinct_idempotency_keys(
        self,
    ) -> None:
        self.urlopen.return_value = FakeResponse(
            {
                "stdout": "",
                "stderr": "",
                "exit_code": 0,
                "request_id": "server-request",
                "usage": {},
            }
        )

        self.sandbox.execute_code("print(1)")
        self.sandbox.execute_code("print(1)")

        keys = [
            call.args[0].get_header("Idempotency-key")
            for call in self.urlopen.call_args_list
        ]
        self.assertTrue(all(keys))
        self.assertNotEqual(keys[0], keys[1])

    def test_get_normalizes_base_url_and_forwards_timeout(self) -> None:
        self.urlopen.return_value = FakeResponse({"id": "sbx_1"})

        self.assertEqual(self.client.get("sbx_1"), {"id": "sbx_1"})

        request = self.urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url, "https://sandbox.example/v1/sandboxes/sbx_1"
        )
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertEqual(
            request.get_header("Authorization"), "Bearer product-key"
        )
        self.assertEqual(self.urlopen.call_args.kwargs, {"timeout": 7.5})


if __name__ == "__main__":
    unittest.main()
