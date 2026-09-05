"""Tests for the one-key HTTP sandbox client."""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest import mock

from bespokelabs.sandbox.remote import RemoteSandboxClient, RemoteSandboxError


class FakeResponse:

    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode()

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def read(self) -> bytes:
        return self._body


class RemoteSandboxClientTest(unittest.TestCase):

    @mock.patch("bespokelabs.sandbox.remote.urllib.request.urlopen")
    def test_one_key_creates_and_executes_across_gateway(
        self, urlopen: mock.MagicMock
    ) -> None:
        urlopen.side_effect = [
            FakeResponse({"id": "sbx_1", "backend": "e2b"}),
            FakeResponse(
                {
                    "request_id": "stable-request",
                    "stdout": "42\n",
                    "stderr": "",
                    "exit_code": 0,
                    "usage": {"customer_cost_usd": "0.001"},
                }
            ),
            FakeResponse({"id": "sbx_1", "status": "destroyed"}),
        ]
        client = RemoteSandboxClient("https://sandbox.example", "bsk_live_test")

        with client.create(
            "e2b", cpu=2, idempotency_key="create-stable"
        ) as sandbox:
            result = sandbox.execute_code(
                "print(6 * 7)", idempotency_key="stable-request"
            )

        self.assertEqual(result.stdout, "42\n")
        self.assertEqual(result.usage["customer_cost_usd"], "0.001")
        create_request = urlopen.call_args_list[0].args[0]
        execute_request = urlopen.call_args_list[1].args[0]
        self.assertEqual(
            create_request.get_header("Authorization"),
            "Bearer bsk_live_test",
        )
        self.assertEqual(
            create_request.get_header("Idempotency-key"), "create-stable"
        )
        self.assertEqual(
            execute_request.get_header("Idempotency-key"), "stable-request"
        )
        self.assertEqual(json.loads(create_request.data)["backend"], "e2b")

    @mock.patch("bespokelabs.sandbox.remote.urllib.request.urlopen")
    def test_cost_query_encodes_filters(self, urlopen: mock.MagicMock) -> None:
        urlopen.return_value = FakeResponse({"items": []})
        client = RemoteSandboxClient("https://sandbox.example", "key")
        client.costs(
            group_by="day",
            start="2026-01-01T00:00:00+00:00",
            end="2026-02-01T00:00:00+00:00",
        )
        request = urlopen.call_args.args[0]
        self.assertIn("group_by=day", request.full_url)
        self.assertIn("%2B00%3A00", request.full_url)

    @mock.patch("bespokelabs.sandbox.remote.urllib.request.urlopen")
    def test_reconciliation_endpoints(self, urlopen: mock.MagicMock) -> None:
        urlopen.side_effect = [
            FakeResponse({"total": 0}),
            FakeResponse({"status": "ok"}),
        ]
        client = RemoteSandboxClient("https://sandbox.example", "key")

        self.assertEqual(client.reconciliation(), {"total": 0})
        self.assertEqual(client.reconcile("daytona"), {"status": "ok"})

        self.assertTrue(
            urlopen.call_args_list[0]
            .args[0]
            .full_url.endswith("/v1/reconciliation")
        )
        self.assertTrue(
            urlopen.call_args_list[1]
            .args[0]
            .full_url.endswith("/v1/reconciliation/daytona")
        )

    @mock.patch("bespokelabs.sandbox.remote.urllib.request.urlopen")
    def test_structured_provider_error_fields_are_exposed(
        self, urlopen: mock.MagicMock
    ) -> None:
        payload = {
            "detail": {
                "message": "Provider request timed out.",
                "code": "timeout",
                "backend": "daytona",
                "op": "create",
                "retryable": False,
                "outcome": "unknown",
                "context": {"cleanup_status": "unknown"},
            }
        }
        urlopen.side_effect = urllib.error.HTTPError(
            "https://sandbox.example/v1/sandboxes",
            504,
            "Gateway Timeout",
            {},
            io.BytesIO(json.dumps(payload).encode()),
        )
        client = RemoteSandboxClient("https://sandbox.example", "product-key")

        with self.assertRaises(RemoteSandboxError) as ctx:
            client.create("daytona", idempotency_key="ambiguous")

        error = ctx.exception
        self.assertEqual(str(error), "Provider request timed out.")
        self.assertEqual(error.status_code, 504)
        self.assertEqual(error.code, "timeout")
        self.assertEqual(error.backend, "daytona")
        self.assertEqual(error.op, "create")
        self.assertFalse(error.retryable)
        self.assertEqual(error.outcome, "unknown")
        self.assertEqual(error.context, {"cleanup_status": "unknown"})


if __name__ == "__main__":
    unittest.main()
