"""RunPod backend tests with fully fake API and SSH transports."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest import mock

from bespokelabs.sandbox.backends.runpod import (
    RunpodClient,
    RunpodSession,
    _create_payload,
    _RunpodApi,
    _RunpodApiError,
)
from bespokelabs.sandbox.exceptions import (
    FeatureNotSupportedError,
    SandboxConfigurationError,
    SandboxCreationError,
    SandboxExecutionError,
)
from bespokelabs.sandbox.types import SandboxConfig


class _FakeApi:

    def __init__(self, pod: dict) -> None:
        self.pod = pod
        self.created: list[dict] = []
        self.deleted: list[str] = []
        self.get_calls: list[str] = []
        self.delete_error: Exception | None = None

    def create_pod(self, payload: dict) -> dict:
        self.created.append(payload)
        return dict(self.pod)

    def get_pod(self, pod_id: str) -> dict:
        self.get_calls.append(pod_id)
        return dict(self.pod)

    def delete_pod(self, pod_id: str) -> None:
        self.deleted.append(pod_id)
        if self.delete_error is not None:
            raise self.delete_error


def _ready_pod() -> dict:
    return {
        "id": "pod-123",
        "desiredStatus": "RUNNING",
        "publicIp": "203.0.113.7",
        "portMappings": {"22": 31022},
        "adjustedCostPerHr": 1.25,
    }


def _client(api: _FakeApi) -> RunpodClient:
    client = object.__new__(RunpodClient)
    client._ssh_path = "/usr/bin/ssh"
    client._new_api = mock.Mock(return_value=api)
    return client


def _completed(
    *, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _session(api: _FakeApi | None = None) -> RunpodSession:
    return RunpodSession(
        api=api or _FakeApi(_ready_pod()),
        pod_id="pod-123",
        host="203.0.113.7",
        port=31022,
        ssh_path="/usr/bin/ssh",
        ssh_user="root",
        ssh_private_key_path="/keys/runpod",
        ssh_connect_timeout_secs=15,
        ssh_strict_host_key_checking="accept-new",
        ssh_known_hosts_file="/keys/known_hosts",
        timeout_secs=120,
        workdir="/workspace/project files",
        cost_per_hour=1.25,
        api_base_url="https://rest.runpod.io/v1",
        api_timeout_secs=30,
    )


class RunpodPayloadTests(unittest.TestCase):

    def test_maps_unified_gpu_and_resource_configuration(self) -> None:
        config = SandboxConfig(
            backend="runpod",
            cpu=8,
            memory_mb=32 * 1024,
            disk_mb=50 * 1024 + 1,
            gpu="NVIDIA H100 80GB HBM3:2",
            image="registry.example/image:tag",
            env_vars={"TOKEN": "value"},
        )

        payload = _create_payload(config, {"interruptible": True})

        self.assertEqual(payload["gpuTypeIds"], ["NVIDIA H100 80GB HBM3"])
        self.assertEqual(payload["gpuCount"], 2)
        self.assertEqual(payload["minVCPUPerGPU"], 4)
        self.assertEqual(payload["minRAMPerGPU"], 16)
        self.assertEqual(payload["containerDiskInGb"], 51)
        self.assertEqual(payload["imageName"], "registry.example/image:tag")
        self.assertEqual(payload["env"], {"TOKEN": "value"})
        self.assertTrue(payload["interruptible"])

    def test_native_gpu_fallback_list_can_supply_the_gpu_request(self) -> None:
        payload = _create_payload(
            SandboxConfig(backend="runpod", cpu=8, memory_mb=16 * 1024),
            {
                "gpuTypeIds": ["NVIDIA L40S", "NVIDIA RTX A6000"],
                "gpuCount": 2,
                "gpuTypePriority": "availability",
            },
        )

        self.assertEqual(
            payload["gpuTypeIds"], ["NVIDIA L40S", "NVIDIA RTX A6000"]
        )
        self.assertEqual(payload["minVCPUPerGPU"], 4)
        self.assertEqual(payload["minRAMPerGPU"], 8)

    def test_rejects_invalid_gpu_counts(self) -> None:
        for gpu in ("NVIDIA L40S:0", "NVIDIA L40S:many"):
            with self.subTest(gpu=gpu):
                with self.assertRaises(SandboxConfigurationError):
                    _create_payload(
                        SandboxConfig(backend="runpod", gpu=gpu), {}
                    )

    def test_template_replaces_default_image(self) -> None:
        payload = _create_payload(
            SandboxConfig(
                backend="runpod", gpu="NVIDIA L40S", template="tpl-1"
            ),
            {},
        )

        self.assertEqual(payload["templateId"], "tpl-1")
        self.assertNotIn("imageName", payload)

    def test_uses_a_versioned_runpod_image_by_default(self) -> None:
        payload = _create_payload(
            SandboxConfig(backend="runpod", gpu="NVIDIA L40S"), {}
        )

        self.assertEqual(
            payload["imageName"],
            "runpod/pytorch:1.0.3-cu1281-torch291-ubuntu2404",
        )

    def test_requires_a_gpu_request(self) -> None:
        with self.assertRaises(SandboxConfigurationError):
            _create_payload(SandboxConfig(backend="runpod"), {})

    def test_public_ip_cannot_be_disabled(self) -> None:
        with self.assertRaises(SandboxConfigurationError):
            _create_payload(
                SandboxConfig(backend="runpod", gpu="NVIDIA L40S"),
                {"supportPublicIp": False},
            )

    def test_ssh_port_is_added_to_native_ports(self) -> None:
        payload = _create_payload(
            SandboxConfig(backend="runpod", gpu="NVIDIA L40S"),
            {"ports": ["8888/http"]},
        )

        self.assertEqual(payload["ports"], ["8888/http", "22/tcp"])


class RunpodApiTests(unittest.TestCase):

    @mock.patch("bespokelabs.sandbox.backends.runpod.urllib.request.urlopen")
    def test_rest_request_uses_bearer_auth_and_json(
        self, urlopen: mock.Mock
    ) -> None:
        response = urlopen.return_value.__enter__.return_value
        response.read.return_value = b'{"id": "pod-123"}'
        api = _RunpodApi(
            api_key="secret",
            base_url="https://api.example/v1/",
            timeout_secs=7,
        )

        result = api.create_pod({"gpuCount": 2})

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.example/v1/pods")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        self.assertEqual(request.data, b'{"gpuCount": 2}')
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 7)
        self.assertEqual(result, {"id": "pod-123"})

    @mock.patch("bespokelabs.sandbox.backends.runpod.urllib.request.urlopen")
    def test_http_error_retains_status_and_response_detail(
        self, urlopen: mock.Mock
    ) -> None:
        urlopen.side_effect = urllib.error.HTTPError(
            "https://api.example/v1/pods",
            429,
            "Too Many Requests",
            {},
            BytesIO(b"capacity unavailable"),
        )
        api = _RunpodApi(
            api_key="secret",
            base_url="https://api.example/v1",
            timeout_secs=7,
        )

        with self.assertRaises(_RunpodApiError) as ctx:
            api.create_pod({})

        self.assertEqual(ctx.exception.status_code, 429)
        self.assertIn("capacity unavailable", str(ctx.exception))


class RunpodClientTests(unittest.TestCase):

    @mock.patch(
        "bespokelabs.sandbox.backends.runpod.subprocess.run",
        return_value=_completed(),
    )
    def test_create_waits_for_ssh_and_returns_a_live_session(
        self, run: mock.Mock
    ) -> None:
        api = _FakeApi(_ready_pod())
        client = _client(api)
        config = SandboxConfig(
            backend="runpod",
            gpu="NVIDIA L40S",
            backend_options={
                "poll_interval_secs": 0,
                "ssh_private_key_path": "/keys/runpod",
                "interruptible": True,
            },
        )

        session = client.create(config)

        self.assertEqual(api.created[0]["gpuTypeIds"], ["NVIDIA L40S"])
        self.assertTrue(api.created[0]["interruptible"])
        self.assertNotIn("poll_interval_secs", api.created[0])
        self.assertNotIn("ssh_private_key_path", api.created[0])
        self.assertEqual(session.cost_per_hour, 1.25)
        ssh_args = run.call_args.args[0]
        self.assertIn("root@203.0.113.7", ssh_args)
        self.assertIn("31022", ssh_args)
        self.assertIn("/keys/runpod", ssh_args)

    def test_create_failure_terminates_the_billing_pod(self) -> None:
        pod = {"id": "pod-123", "desiredStatus": "TERMINATED"}
        api = _FakeApi(pod)
        client = _client(api)

        with self.assertRaises(SandboxCreationError):
            client.create(SandboxConfig(backend="runpod", gpu="NVIDIA L40S"))

        self.assertEqual(api.deleted, ["pod-123"])

    def test_cleanup_failure_preserves_pod_id_for_manual_recovery(self) -> None:
        pod = {"id": "pod-123", "desiredStatus": "TERMINATED"}
        api = _FakeApi(pod)
        api.delete_error = RuntimeError("delete unavailable")
        client = _client(api)

        with self.assertRaises(SandboxCreationError) as ctx:
            client.create(SandboxConfig(backend="runpod", gpu="NVIDIA L40S"))

        self.assertEqual(ctx.exception.context["pod_id"], "pod-123")
        self.assertIn(
            "delete unavailable", ctx.exception.context["cleanup_error"]
        )

    def test_rejects_network_isolation_before_api_call(self) -> None:
        api = _FakeApi(_ready_pod())
        client = _client(api)

        with self.assertRaises(SandboxConfigurationError):
            client.create(
                SandboxConfig(
                    backend="runpod",
                    gpu="NVIDIA L40S",
                    allow_internet=False,
                )
            )

        self.assertEqual(api.created, [])

    def test_rejects_snapshot_restore_before_api_call(self) -> None:
        api = _FakeApi(_ready_pod())
        client = _client(api)

        with self.assertRaises(FeatureNotSupportedError):
            client.create(
                SandboxConfig(
                    backend="runpod",
                    gpu="NVIDIA L40S",
                    snapshot_id="snap-1",
                )
            )

        self.assertEqual(api.created, [])

    def test_resume_refreshes_the_public_endpoint(self) -> None:
        api = _FakeApi(_ready_pod())
        client = _client(api)

        session = client.resume(
            {
                "pod_id": "pod-123",
                "workdir": "/workspace/repo",
                "timeout_secs": 90,
            }
        )

        self.assertEqual(api.get_calls, ["pod-123"])
        self.assertEqual(session.session_state()["workdir"], "/workspace/repo")


class RunpodSessionTests(unittest.TestCase):

    @mock.patch("bespokelabs.sandbox.backends.runpod.subprocess.run")
    def test_execute_command_quotes_arguments_and_workdir(
        self, run: mock.Mock
    ) -> None:
        run.return_value = _completed(stdout=b"ok\n")

        result = _session().execute_command(
            "python", ["-c", "print('hello world')"]
        )

        self.assertEqual(result.stdout, "ok\n")
        args = run.call_args.args[0]
        self.assertEqual(args[-2], "root@203.0.113.7")
        self.assertIn("'/workspace/project files'", args[-1])
        self.assertIn("print", args[-1])
        self.assertEqual(run.call_args.kwargs["timeout"], 120)

    @mock.patch("bespokelabs.sandbox.backends.runpod.subprocess.run")
    def test_write_file_streams_binary_content_over_ssh(
        self, run: mock.Mock
    ) -> None:
        run.return_value = _completed()
        data = b"\x00\xffbinary\n"

        _session().write_file("/workspace/new dir/file.bin", data)

        self.assertEqual(run.call_args.kwargs["input"], data)
        self.assertIn("mkdir -p", run.call_args.args[0][-1])

    @mock.patch("bespokelabs.sandbox.backends.runpod.subprocess.run")
    def test_list_files_parses_null_delimited_find_output(
        self, run: mock.Mock
    ) -> None:
        run.return_value = _completed(
            stdout=(
                b"d\t4096\t/workspace/folder\0"
                b"f\t12\t/workspace/a file.txt\0"
            )
        )

        files = _session().list_files("/workspace")

        self.assertEqual(len(files), 2)
        self.assertTrue(files[0].is_dir)
        self.assertEqual(files[1].path, "/workspace/a file.txt")
        self.assertEqual(files[1].size, 12)

    @mock.patch("bespokelabs.sandbox.backends.runpod.subprocess.run")
    def test_file_failure_raises_structured_error(self, run: mock.Mock) -> None:
        run.return_value = _completed(returncode=1, stderr=b"not found")

        with self.assertRaises(SandboxExecutionError) as ctx:
            _session().read_file("/missing")

        self.assertEqual(ctx.exception.backend, "runpod")
        self.assertEqual(ctx.exception.context["exit_code"], 1)

    def test_session_state_has_no_api_key_and_destroy_is_idempotent(
        self,
    ) -> None:
        api = _FakeApi(_ready_pod())
        session = _session(api)

        state = session.session_state()
        session.destroy()
        session.destroy()

        self.assertNotIn("api_key", state)
        self.assertEqual(state["pod_id"], "pod-123")
        self.assertEqual(api.deleted, ["pod-123"])

    def test_failed_destroy_can_be_retried(self) -> None:
        api = _FakeApi(_ready_pod())
        api.delete_error = RuntimeError("temporary failure")
        session = _session(api)

        session.destroy()
        self.assertEqual(session.session_state()["pod_id"], "pod-123")

        api.delete_error = None
        session.destroy()
        self.assertEqual(api.deleted, ["pod-123", "pod-123"])

    @mock.patch("bespokelabs.sandbox.backends.runpod.subprocess.run")
    def test_upload_and_download_preserve_binary_data(
        self, run: mock.Mock
    ) -> None:
        run.side_effect = [_completed(), _completed(stdout=b"remote\x00data")]
        session = _session()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, "source.bin")
            destination = Path(directory, "destination.bin")
            source.write_bytes(b"local\x00data")

            session.upload_file(str(source), "/workspace/source.bin")
            session.download_file(
                "/workspace/destination.bin", str(destination)
            )

            self.assertEqual(destination.read_bytes(), b"remote\x00data")
            self.assertEqual(
                run.call_args_list[0].kwargs["input"], b"local\x00data"
            )


if __name__ == "__main__":
    unittest.main()
