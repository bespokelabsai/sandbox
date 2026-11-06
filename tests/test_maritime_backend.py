"""Tests for the Maritime backend against a fake HTTP transport."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
from unittest import mock

from bespokelabs.sandbox import NetworkPolicy, Sandbox, SandboxClient
from bespokelabs.sandbox.backends import BACKENDS
from bespokelabs.sandbox.backends import maritime as mt
from bespokelabs.sandbox.exceptions import (
    FeatureNotSupportedError,
    SandboxAuthenticationError,
    SandboxConfigurationError,
    SandboxConnectionError,
    SandboxCreationError,
    SandboxExecutionError,
    SandboxNotFoundError,
    SandboxTimeoutError,
)
from bespokelabs.sandbox.types import SandboxConfig

API = "https://api.maritime.sh/v1"
DP = "https://api-sbx-test.sb.mtm.sh/v1"


def _sandbox(**overrides: object) -> dict:
    body = {
        "id": "sbx-test",
        "state": "running",
        "dataPlaneUrl": DP,
        "secret": "sekret",
        "metadata": {},
        "createdAt": "2026-11-04T23:06:40.414250031Z",
    }
    body.update(overrides)
    return body


class _Response:

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeApi:
    """Records requests and answers from a route table."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.routes: dict[tuple[str, str], list] = {}

    def on(self, method: str, url: str, *replies: object) -> None:
        self.routes.setdefault((method, url), []).extend(replies)

    def __call__(self, request: object, timeout: float = 0) -> _Response:
        url = request.full_url
        data = request.data
        self.requests.append(
            {
                "method": request.get_method(),
                "url": url,
                "headers": {k.lower(): v for k, v in request.header_items()},
                "data": data,
                "timeout": timeout,
            }
        )
        key = (request.get_method(), url.split("?")[0])
        exact = (request.get_method(), url)
        replies = self.routes.get(exact) or self.routes.get(key)
        if not replies:
            raise AssertionError(f"unexpected request {key}")
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, tuple):
            status, body = reply
            raise urllib.error.HTTPError(
                url, status, "error", {}, io.BytesIO(json.dumps(body).encode())
            )
        if isinstance(reply, bytes):
            return _Response(reply)
        return _Response(json.dumps(reply).encode())

    def last(self, method: str, prefix: str) -> dict:
        for req in reversed(self.requests):
            if req["method"] == method and req["url"].startswith(prefix):
                return req
        raise AssertionError(f"no {method} {prefix}")

    def body(self, method: str, prefix: str) -> dict:
        return json.loads(self.last(method, prefix)["data"])


class MaritimeTestCase(unittest.TestCase):

    def setUp(self) -> None:
        self.api = FakeApi()
        patcher = mock.patch.object(
            mt.urllib.request, "urlopen", side_effect=self.api
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(
            os.environ, {"MARITIME_API_KEY": "mk_test"}, clear=False
        )
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MARITIME_API_URL", None)
        self.api.on("POST", f"{API}/sandboxes", _sandbox())
        self.api.on("DELETE", f"{API}/sandboxes/sbx-test", _sandbox())

    def create(self, **config: object) -> mt.MaritimeSession:
        return mt.MaritimeClient().create(
            SandboxConfig(backend="maritime", **config)
        )


class RegistrationTests(unittest.TestCase):

    def test_registered_without_an_sdk(self) -> None:
        self.assertIs(BACKENDS["maritime"], mt.MaritimeClient)
        mt.MaritimeClient()  # no import, no network

    def test_missing_key_is_an_authentication_error(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SandboxAuthenticationError):
                mt.MaritimeClient().create(SandboxConfig(backend="maritime"))


class CreateTests(MaritimeTestCase):

    def test_payload_maps_config(self) -> None:
        session = self.create(
            cpu=1.5,
            memory_mb=1500,
            disk_mb=2048,
            env_vars={"A": "1"},
            timeout_secs=30,
            timeout_secs_explicit=True,
            template="code-interpreter",
            backend_options={"metadata": {"run": "7"}, "onIdle": "kill"},
        )
        body = self.api.body("POST", f"{API}/sandboxes")
        self.assertEqual(body["vcpu"], 2)
        self.assertEqual(body["memoryMib"], 2048)  # whole GiB
        self.assertEqual(body["diskGib"], 5)  # API minimum
        self.assertEqual(body["from"], "code-interpreter")
        self.assertEqual(body["env"], {"A": "1"})
        self.assertEqual(body["timeLimitSeconds"], 60)  # API minimum
        self.assertEqual(body["onIdle"], "kill")
        self.assertEqual(
            body["metadata"],
            {"created_by": "bespokelabs-sandbox", "run": "7"},
        )
        req = self.api.last("POST", f"{API}/sandboxes")
        self.assertEqual(req["headers"]["authorization"], "Bearer mk_test")
        self.assertTrue(
            req["headers"]["idempotency-key"].startswith("bespoke-")
        )
        self.assertEqual(session.provider_resource_id, "sbx-test")

    def test_defaults_leave_lifetime_to_the_platform(self) -> None:
        self.create()
        body = self.api.body("POST", f"{API}/sandboxes")
        self.assertEqual(body["from"], "base")
        self.assertEqual((body["vcpu"], body["memoryMib"]), (1, 1024))
        for key in ("timeLimitSeconds", "env", "network", "diskGib"):
            self.assertNotIn(key, body)

    def test_resources_are_clamped(self) -> None:
        self.create(cpu=64, memory_mb=10**7)
        body = self.api.body("POST", f"{API}/sandboxes")
        self.assertEqual((body["vcpu"], body["memoryMib"]), (16, 65536))

    def test_snapshot_id_is_the_source(self) -> None:
        self.create(snapshot_id="my-snap")
        self.assertEqual(
            self.api.body("POST", f"{API}/sandboxes")["from"], "my-snap"
        )

    def test_unsupported_sources_raise_before_any_request(self) -> None:
        for kwargs, error in (
            ({"image": "python:3.12"}, FeatureNotSupportedError),
            ({"gpu": "L4"}, FeatureNotSupportedError),
            ({"dockerfile": "Dockerfile"}, FeatureNotSupportedError),
            (
                {"snapshot_id": "a", "template": "base"},
                SandboxConfigurationError,
            ),
            (
                {
                    "network": NetworkPolicy(
                        mode="allowlist", cidrs=("10.0.0.0/8",)
                    )
                },
                FeatureNotSupportedError,
            ),
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(error):
                    self.create(**kwargs)
        self.assertEqual(self.api.requests, [])

    def test_network_modes(self) -> None:
        cases = (
            (NetworkPolicy(mode="public"), {"egress": "all"}),
            (NetworkPolicy(mode="none"), {"egress": "none"}),
            (
                NetworkPolicy(mode="allowlist", domains=("pypi.org",)),
                {"egress": "allowlist", "hosts": ["pypi.org"]},
            ),
        )
        for policy, expected in cases:
            with self.subTest(policy=policy):
                self.create(network=policy)
                self.assertEqual(
                    self.api.body("POST", f"{API}/sandboxes")["network"],
                    expected,
                )

    def test_allow_internet_false_blocks_egress(self) -> None:
        self.create(allow_internet=False)
        self.assertEqual(
            self.api.body("POST", f"{API}/sandboxes")["network"],
            {"egress": "none"},
        )

    def test_api_url_override(self) -> None:
        with mock.patch.dict(
            os.environ, {"MARITIME_API_URL": "http://localhost:8000/"}
        ):
            self.api.on(
                "POST", "http://localhost:8000/v1/sandboxes", _sandbox()
            )
            self.create()
        self.api.last("POST", "http://localhost:8000/v1/sandboxes")

    def test_error_classification(self) -> None:
        cases = (
            ((401, {"code": "unauthenticated"}), SandboxAuthenticationError),
            ((402, {"code": "plan_required"}), SandboxCreationError),
            ((400, {"code": "invalid_request"}), SandboxConfigurationError),
            ((404, {"code": "snapshot_not_found"}), SandboxNotFoundError),
            (urllib.error.URLError("down"), SandboxConnectionError),
        )
        for reply, error in cases:
            with self.subTest(reply=reply):
                self.api.routes[("POST", f"{API}/sandboxes")] = [reply]
                with self.assertRaises(error):
                    self.create()

    def test_capacity_errors_are_retryable(self) -> None:
        self.api.routes[("POST", f"{API}/sandboxes")] = [
            (503, {"code": "out_of_capacity", "message": "busy"})
        ]
        with self.assertRaises(SandboxCreationError) as ctx:
            self.create()
        self.assertTrue(ctx.exception.retryable)
        self.assertIn("busy", str(ctx.exception))

    def test_create_without_data_plane_fails(self) -> None:
        self.api.routes[("POST", f"{API}/sandboxes")] = [
            _sandbox(dataPlaneUrl=None)
        ]
        with self.assertRaises(SandboxCreationError):
            self.create()


class CommandTests(MaritimeTestCase):

    def test_command_runs_through_bash_with_token(self) -> None:
        self.api.on(
            "POST",
            f"{DP}/commands",
            {"stdout": "hi\n", "stderr": "e", "exitCode": 3},
        )
        session = self.create(
            workdir="/work", env_vars={"K": "v"}, timeout_secs=90
        )
        result = session.execute_command("echo", ["a b", "$HOME"])
        self.assertEqual(
            (result.stdout, result.stderr, result.exit_code), ("hi\n", "e", 3)
        )
        req = self.api.last("POST", f"{DP}/commands")
        self.assertEqual(req["headers"]["x-maritime-token"], "sekret")
        self.assertNotIn("authorization", req["headers"])
        body = json.loads(req["data"])
        self.assertEqual(body["cmd"], ["bash", "-c", "echo 'a b' '$HOME'"])
        self.assertEqual(body["cwd"], "/work")
        self.assertEqual(body["env"], {"K": "v"})
        self.assertEqual(body["timeoutSeconds"], 90)
        self.assertEqual(req["timeout"], 120)

    def test_execute_code_uses_the_interpreter(self) -> None:
        self.api.on("POST", f"{DP}/commands", {"stdout": "", "exitCode": 0})
        session = self.create()
        session.execute_code("print(1)")
        session.execute_code("console.log(1)", "node")
        bodies = [
            json.loads(r["data"])["cmd"][2]
            for r in self.api.requests
            if r["url"] == f"{DP}/commands"
        ]
        self.assertEqual(
            bodies, ["python3 -c 'print(1)'", "node -c 'console.log(1)'"]
        )

    def test_command_timeout_is_capped_at_an_hour(self) -> None:
        self.api.on("POST", f"{DP}/commands", {"exitCode": 0})
        self.create(timeout_secs=10**6).execute_command("true")
        self.assertEqual(
            self.api.body("POST", f"{DP}/commands")["timeoutSeconds"], 3600
        )

    def test_killed_command_reports_137(self) -> None:
        self.api.on(
            "POST", f"{DP}/commands", {"exitCode": None, "signal": "SIGKILL"}
        )
        self.assertEqual(self.create().execute_command("x").exit_code, 137)

    def test_command_timeout_raises(self) -> None:
        self.api.on(
            "POST",
            f"{DP}/commands",
            (504, {"code": "command_timeout", "message": "killed"}),
        )
        with self.assertRaises(SandboxTimeoutError):
            self.create().execute_command("sleep 100")


class FileTests(MaritimeTestCase):

    def test_write_read_and_relative_paths(self) -> None:
        self.api.on("PUT", f"{DP}/files", b"")
        self.api.on("GET", f"{DP}/files", b"\x00bin")
        session = self.create()
        session.write_file("/tmp/x", "text")
        put = self.api.last("PUT", f"{DP}/files")
        self.assertEqual(put["data"], b"text")
        self.assertEqual(
            put["headers"]["content-type"], "application/octet-stream"
        )
        self.assertIn("path=%2Ftmp%2Fx", put["url"])
        session.write_file("rel/y.txt", b"\x01")
        self.assertIn(
            "path=%2Fhome%2Fuser%2Frel%2Fy.txt",
            self.api.last("PUT", f"{DP}/files")["url"],
        )
        self.assertEqual(session.read_file("/tmp/x"), b"\x00bin")

    def test_relative_paths_follow_workdir(self) -> None:
        self.api.on("GET", f"{DP}/files", b"")
        self.create(workdir="/work").read_file("a.txt")
        self.assertIn(
            "path=%2Fwork%2Fa.txt", self.api.last("GET", f"{DP}/files")["url"]
        )

    def test_upload_and_download(self) -> None:
        self.api.on("PUT", f"{DP}/files", b"")
        self.api.on("GET", f"{DP}/files", b"payload")
        session = self.create()
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src")
            with open(src, "wb") as fh:
                fh.write(b"\x00\xff")
            session.upload_file(src, "/tmp/up")
            self.assertEqual(
                self.api.last("PUT", f"{DP}/files")["data"], b"\x00\xff"
            )
            dst = os.path.join(tmp, "dst")
            session.download_file("/tmp/up", dst)
            with open(dst, "rb") as fh:
                self.assertEqual(fh.read(), b"payload")

    def test_list_files_follows_cursor(self) -> None:
        self.api.on(
            "GET",
            f"{DP}/files/list",
            {
                "data": [{"path": "/d/a", "type": "file", "sizeBytes": 4}],
                "nextCursor": "c1",
            },
            {"data": [{"path": "/d/b", "type": "dir", "sizeBytes": 0}]},
        )
        entries = self.create().list_files("/d")
        self.assertEqual(
            [(e.path, e.is_dir, e.size) for e in entries],
            [("/d/a", False, 4), ("/d/b", True, None)],
        )
        self.assertIn(
            "cursor=c1", self.api.last("GET", f"{DP}/files/list")["url"]
        )

    def test_missing_file_is_not_found(self) -> None:
        self.api.on("GET", f"{DP}/files", (404, {"code": "file_not_found"}))
        with self.assertRaises(SandboxNotFoundError):
            self.create().read_file("/nope")


class LifecycleTests(MaritimeTestCase):

    def test_expose_port_returns_the_tokened_url(self) -> None:
        url = "https://8080-sbx-test.sb.mtm.sh/?token=t"
        self.api.on("POST", f"{API}/sandboxes/sbx-test/urls", {"url": url})
        exposed = self.create().expose_port(8080)
        self.assertEqual((exposed.port, exposed.url), (8080, url))
        self.assertEqual(
            self.api.body("POST", f"{API}/sandboxes/sbx-test/urls"),
            {"port": 8080, "token": True},
        )

    def test_set_network_patches(self) -> None:
        self.api.on("PATCH", f"{API}/sandboxes/sbx-test", _sandbox())
        self.create().set_network(NetworkPolicy(mode="none"))
        self.assertEqual(
            self.api.body("PATCH", f"{API}/sandboxes/sbx-test"),
            {"network": {"egress": "none"}},
        )

    def test_snapshot_saves_a_named_snapshot(self) -> None:
        self.api.on(
            "POST",
            f"{API}/sandboxes/sbx-test/save",
            {"name": "bespoke-x", "createdAt": "2026-11-04T00:00:00Z"},
        )
        info = self.create().snapshot()
        self.assertEqual(
            (info.snapshot_id, info.backend), ("bespoke-x", "maritime")
        )
        name = self.api.body("POST", f"{API}/sandboxes/sbx-test/save")["name"]
        self.assertTrue(name.startswith("bespoke-"))

    def test_destroy_is_idempotent_and_detaches(self) -> None:
        session = self.create()
        session.destroy()
        session.destroy()
        deletes = [r for r in self.api.requests if r["method"] == "DELETE"]
        self.assertEqual(len(deletes), 1)
        self.assertIsNone(session.provider_resource_id)
        with self.assertRaises(SandboxNotFoundError):
            session.execute_command("true")

    def test_destroy_failure_can_be_retried(self) -> None:
        self.api.routes[("DELETE", f"{API}/sandboxes/sbx-test")] = [
            (500, {"code": "internal_error"}),
            _sandbox(state="ended"),
        ]
        session = self.create()
        with self.assertRaises(SandboxExecutionError):
            session.destroy()
        session.destroy()
        self.assertIsNone(session.provider_resource_id)

    def test_destroy_of_a_gone_sandbox_is_quiet(self) -> None:
        self.api.routes[("DELETE", f"{API}/sandboxes/sbx-test")] = [
            (404, {"code": "sandbox_not_found"})
        ]
        self.create().destroy()

    def test_session_state_round_trip(self) -> None:
        session = self.create(
            workdir="/w", env_vars={"K": "v"}, timeout_secs=99
        )
        state = session.session_state()
        self.assertEqual(
            state,
            {
                "sandbox_id": "sbx-test",
                "workdir": "/w",
                "env_vars": {"K": "v"},
                "command_timeout_secs": 99,
            },
        )
        self.api.on(
            "GET",
            f"{API}/sandboxes/sbx-test",
            _sandbox(state="paused", secret="new"),
        )
        self.api.on("POST", f"{DP}/commands", {"exitCode": 0})
        resumed = mt.MaritimeClient().resume(state)
        resumed.execute_command("true")
        req = self.api.last("POST", f"{DP}/commands")
        self.assertEqual(req["headers"]["x-maritime-token"], "new")
        body = json.loads(req["data"])
        self.assertEqual(
            (body["cwd"], body["env"], body["timeoutSeconds"]),
            ("/w", {"K": "v"}, 99),
        )

    def test_resume_of_an_ended_sandbox_raises(self) -> None:
        self.api.on("GET", f"{API}/sandboxes/sbx-test", _sandbox(state="ended"))
        with self.assertRaises(SandboxNotFoundError):
            mt.MaritimeClient().resume({"sandbox_id": "sbx-test"})

    def test_list_sandboxes_filters_and_pages(self) -> None:
        self.api.on(
            "GET",
            f"{API}/sandboxes",
            {
                "data": [
                    _sandbox(id="sbx-a", metadata={"run": "7"}),
                    # The server filter is not trusted on its own.
                    _sandbox(id="sbx-b", metadata={"run": "8"}),
                ],
                "nextCursor": "next",
            },
            {"data": [_sandbox(id="sbx-c", metadata={"run": "7", "x": "y"})]},
        )
        listings = SandboxClient("maritime").list_sandboxes({"run": "7"})
        self.assertEqual(
            [item.provider_resource_id for item in listings], ["sbx-a", "sbx-c"]
        )
        self.assertEqual(listings[0].session_state.backend, "maritime")
        self.assertEqual(
            listings[0].session_state.data, {"sandbox_id": "sbx-a"}
        )
        self.assertIsNotNone(listings[0].created_at)
        first = urllib.parse.parse_qs(
            urllib.parse.urlsplit(self.api.requests[0]["url"]).query
        )
        self.assertEqual(first["metadata[run]"], ["7"])
        self.assertNotIn("ended", first["state"])
        self.assertIn(
            "cursor=next", self.api.last("GET", f"{API}/sandboxes")["url"]
        )


class SandboxFacadeTests(MaritimeTestCase):

    def test_sandbox_round_trip(self) -> None:
        self.api.on("POST", f"{DP}/commands", {"stdout": "ok", "exitCode": 0})
        with Sandbox("maritime") as sb:
            self.assertEqual(sb.execute_command("echo ok").stdout, "ok")
        self.api.last("DELETE", f"{API}/sandboxes/sbx-test")

    def test_network_option_clash_is_rejected(self) -> None:
        with self.assertRaises(SandboxConfigurationError):
            Sandbox(
                "maritime",
                network=NetworkPolicy(mode="none"),
                backend_options={"network": {"egress": "all"}},
            )


if __name__ == "__main__":
    unittest.main()
