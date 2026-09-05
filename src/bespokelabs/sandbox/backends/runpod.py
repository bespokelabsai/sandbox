"""RunPod GPU Pod backend."""

from __future__ import annotations

import json
import math
import os
import pathlib
import posixpath
import shlex
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

from bespokelabs.sandbox.exceptions import (
    BackendNotInstalledError,
    FeatureNotSupportedError,
    SandboxConfigurationError,
    SandboxConnectionError,
    SandboxCreationError,
    SandboxError,
    SandboxExecutionError,
    SandboxNotFoundError,
    SandboxTimeoutError,
)
from bespokelabs.sandbox.types import (
    FileInfo,
    SandboxConfig,
    SandboxResult,
    SnapshotInfo,
)

_API_BASE_URL = "https://rest.runpod.io/v1"
_DEFAULT_IMAGE = "runpod/pytorch:1.0.3-cu1281-torch291-ubuntu2404"
_DEFAULT_WORKDIR = "/workspace"
_DEFAULT_CREATE_TIMEOUT_SECS = 600.0
_DEFAULT_API_TIMEOUT_SECS = 30.0
_DEFAULT_POLL_INTERVAL_SECS = 2.0
_DEFAULT_SSH_CONNECT_TIMEOUT_SECS = 15

_TRANSPORT_OPTIONS = {
    "api_base_url",
    "api_timeout_secs",
    "create_timeout_secs",
    "poll_interval_secs",
    "ssh_connect_timeout_secs",
    "ssh_known_hosts_file",
    "ssh_private_key_path",
    "ssh_strict_host_key_checking",
    "ssh_user",
}


class _RunpodApiError(Exception):
    """An unsuccessful or malformed response from the RunPod API."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class _RunpodApi:
    """Small REST client for the Pod endpoints used by this backend."""

    def __init__(
        self, *, api_key: str, base_url: str, timeout_secs: float
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_secs = timeout_secs

    def create_pod(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/pods", payload)

    def get_pod(self, pod_id: str) -> dict[str, Any]:
        return self._request("GET", f"/pods/{pod_id}")

    def delete_pod(self, pod_id: str) -> None:
        self._request("DELETE", f"/pods/{pod_id}")

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self._timeout_secs
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace").strip()
            raise _RunpodApiError(
                f"RunPod API returned HTTP {exc.code}: {detail or exc.reason}",
                status_code=exc.code,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise _RunpodApiError(
                f"Could not connect to the RunPod API: {exc}"
            ) from exc

        if not raw:
            return {}
        try:
            result = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise _RunpodApiError(
                "RunPod API returned an invalid JSON response"
            ) from exc
        if not isinstance(result, dict):
            raise _RunpodApiError(
                "RunPod API returned an unexpected response shape"
            )
        return result


class RunpodClient:
    """Factory for SSH-backed RunPod GPU Pods."""

    def __init__(self) -> None:
        ssh_path = shutil.which("ssh")
        if ssh_path is None:
            raise BackendNotInstalledError(
                "RunPod requires an OpenSSH client with the 'ssh' command"
            )
        self._ssh_path = ssh_path

    def create(self, config: SandboxConfig) -> RunpodSession:
        self._validate_config(config)
        native_options, transport = _split_options(config.backend_options)
        api = self._new_api(transport)
        payload = _create_payload(config, native_options)
        pod_id: str | None = None

        try:
            pod = api.create_pod(payload)
            pod_id = str(pod["id"])
            return self._wait_until_ready(
                api=api,
                pod_id=pod_id,
                initial_pod=pod,
                config=config,
                transport=transport,
            )
        except BaseException as exc:
            cleanup_error: Exception | None = None
            if pod_id is not None:
                try:
                    api.delete_pod(pod_id)
                except Exception as delete_exc:
                    cleanup_error = delete_exc
            if not isinstance(exc, Exception):
                raise
            if isinstance(exc, SandboxError):
                if pod_id is not None:
                    exc.context.setdefault("pod_id", pod_id)
                if cleanup_error is not None:
                    exc.context.setdefault("cleanup_error", str(cleanup_error))
                raise
            raise SandboxCreationError(
                f"Failed to create RunPod Pod: {exc}",
                backend="runpod",
                op="create",
                retryable=True,
                context={
                    **({"pod_id": pod_id} if pod_id is not None else {}),
                    **(
                        {"cleanup_error": str(cleanup_error)}
                        if cleanup_error is not None
                        else {}
                    ),
                },
            ) from exc

    def resume(self, data: dict) -> RunpodSession:
        transport = _transport_from_state(data)
        api = self._new_api(transport)
        pod_id = str(data.get("pod_id", ""))
        if not pod_id:
            raise SandboxConfigurationError(
                "RunPod session state is missing pod_id",
                backend="runpod",
                op="resume",
            )
        try:
            pod = api.get_pod(pod_id)
        except _RunpodApiError as exc:
            if exc.status_code == 404:
                raise SandboxNotFoundError(
                    f"RunPod Pod '{pod_id}' no longer exists",
                    backend="runpod",
                    op="resume",
                ) from exc
            raise SandboxCreationError(
                f"Cannot resume RunPod Pod '{pod_id}': {exc}",
                backend="runpod",
                op="resume",
                retryable=True,
            ) from exc

        endpoint = _ssh_endpoint(pod)
        if endpoint is None:
            raise SandboxCreationError(
                f"RunPod Pod '{pod_id}' has no public SSH endpoint",
                backend="runpod",
                op="resume",
                retryable=True,
                context={"desired_status": pod.get("desiredStatus")},
            )
        return self._make_session(
            api=api,
            pod=pod,
            endpoint=endpoint,
            workdir=str(data.get("workdir", _DEFAULT_WORKDIR)),
            timeout_secs=int(data.get("timeout_secs", 600)),
            transport=transport,
        )

    def _new_api(self, transport: Mapping[str, Any]) -> _RunpodApi:
        api_key = os.environ.get("RUNPOD_API_KEY")
        if not api_key:
            raise SandboxConfigurationError(
                "RUNPOD_API_KEY is required for the RunPod backend",
                backend="runpod",
                op="authenticate",
            )
        return _RunpodApi(
            api_key=api_key,
            base_url=str(transport["api_base_url"]),
            timeout_secs=float(transport["api_timeout_secs"]),
        )

    def _wait_until_ready(
        self,
        *,
        api: _RunpodApi,
        pod_id: str,
        initial_pod: dict[str, Any],
        config: SandboxConfig,
        transport: Mapping[str, Any],
    ) -> RunpodSession:
        deadline = time.monotonic() + float(transport["create_timeout_secs"])
        pod = initial_pod
        last_ssh_error = ""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SandboxTimeoutError(
                    f"RunPod Pod '{pod_id}' did not become SSH-ready within "
                    f"{transport['create_timeout_secs']} seconds.",
                    backend="runpod",
                    op="create",
                    context={"desired_status": pod.get("desiredStatus")},
                )
            endpoint = _ssh_endpoint(pod)
            if endpoint is not None:
                session = self._make_session(
                    api=api,
                    pod=pod,
                    endpoint=endpoint,
                    workdir=config.workdir or _DEFAULT_WORKDIR,
                    timeout_secs=config.timeout_secs,
                    transport=transport,
                )
                try:
                    probe = session._run_remote("true", timeout_secs=remaining)
                except SandboxTimeoutError as exc:
                    raise SandboxTimeoutError(
                        f"RunPod Pod '{pod_id}' did not become SSH-ready "
                        f"within {transport['create_timeout_secs']} seconds.",
                        backend="runpod",
                        op="create",
                        context={"desired_status": pod.get("desiredStatus")},
                    ) from exc
                if probe.returncode == 0:
                    return session
                last_ssh_error = probe.stderr.decode(errors="replace").strip()

            if pod.get("desiredStatus") == "TERMINATED":
                raise SandboxCreationError(
                    f"RunPod Pod '{pod_id}' terminated while starting",
                    backend="runpod",
                    op="create",
                    retryable=True,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                detail = (
                    f" Last SSH error: {last_ssh_error}"
                    if last_ssh_error
                    else ""
                )
                raise SandboxTimeoutError(
                    f"RunPod Pod '{pod_id}' did not become SSH-ready within "
                    f"{transport['create_timeout_secs']} seconds.{detail}",
                    backend="runpod",
                    op="create",
                    context={"desired_status": pod.get("desiredStatus")},
                )
            time.sleep(min(float(transport["poll_interval_secs"]), remaining))
            pod = api.get_pod(pod_id)

    def _make_session(
        self,
        *,
        api: _RunpodApi,
        pod: Mapping[str, Any],
        endpoint: tuple[str, int],
        workdir: str,
        timeout_secs: int,
        transport: Mapping[str, Any],
    ) -> RunpodSession:
        return RunpodSession(
            api=api,
            pod_id=str(pod["id"]),
            host=endpoint[0],
            port=endpoint[1],
            ssh_path=self._ssh_path,
            ssh_user=str(transport["ssh_user"]),
            ssh_private_key_path=transport["ssh_private_key_path"],
            ssh_connect_timeout_secs=int(transport["ssh_connect_timeout_secs"]),
            ssh_strict_host_key_checking=str(
                transport["ssh_strict_host_key_checking"]
            ),
            ssh_known_hosts_file=transport["ssh_known_hosts_file"],
            timeout_secs=timeout_secs,
            workdir=workdir,
            cost_per_hour=_cost_per_hour(pod),
            api_base_url=str(transport["api_base_url"]),
            api_timeout_secs=float(transport["api_timeout_secs"]),
        )

    @staticmethod
    def _validate_config(config: SandboxConfig) -> None:
        if not config.allow_internet:
            raise SandboxConfigurationError(
                "RunPod Pods do not support disabling internet access",
                backend="runpod",
                op="create",
            )
        if config.snapshot_id:
            raise FeatureNotSupportedError(
                "RunPod Pods cannot be restored from filesystem snapshots",
                backend="runpod",
                op="create",
            )


class RunpodSession:
    """One live RunPod Pod accessed over its public SSH endpoint."""

    def __init__(
        self,
        *,
        api: _RunpodApi,
        pod_id: str,
        host: str,
        port: int,
        ssh_path: str,
        ssh_user: str,
        ssh_private_key_path: str | None,
        ssh_connect_timeout_secs: int,
        ssh_strict_host_key_checking: str,
        ssh_known_hosts_file: str | None,
        timeout_secs: int,
        workdir: str,
        cost_per_hour: float | None,
        api_base_url: str,
        api_timeout_secs: float,
    ) -> None:
        self._api = api
        self._pod_id: str | None = pod_id
        self._host = host
        self._port = port
        self._ssh_path = ssh_path
        self._ssh_user = ssh_user
        self._ssh_private_key_path = ssh_private_key_path
        self._ssh_connect_timeout_secs = ssh_connect_timeout_secs
        self._ssh_strict_host_key_checking = ssh_strict_host_key_checking
        self._ssh_known_hosts_file = ssh_known_hosts_file
        self._timeout_secs = timeout_secs
        self._workdir = workdir
        self.cost_per_hour = cost_per_hour
        self._api_base_url = api_base_url
        self._api_timeout_secs = api_timeout_secs

    def execute_code(
        self, code: str, language: str = "python"
    ) -> SandboxResult:
        return self.execute_command(language, ["-c", code])

    def execute_command(
        self, command: str, args: list[str] | None = None
    ) -> SandboxResult:
        command_line = (
            command
            if not args
            else f"{shlex.quote(command)} "
            + " ".join(shlex.quote(arg) for arg in args)
        )
        script = (
            f"mkdir -p -- {shlex.quote(self._workdir)} && "
            f"cd -- {shlex.quote(self._workdir)} && {command_line}"
        )
        completed = self._run_remote(_bash_command(script))
        return SandboxResult(
            stdout=completed.stdout.decode(errors="replace"),
            stderr=completed.stderr.decode(errors="replace"),
            exit_code=completed.returncode,
        )

    def list_files(self, path: str = "/") -> list[FileInfo]:
        command = (
            f"find {shlex.quote(path)} -mindepth 1 -maxdepth 1 "
            r"-printf '%y\t%s\t%p\0'"
        )
        completed = self._run_remote(_bash_command(command))
        self._raise_for_file_error("list_files", completed)
        files: list[FileInfo] = []
        for record in completed.stdout.split(b"\0"):
            if not record:
                continue
            parts = record.split(b"\t", 2)
            if len(parts) != 3:
                continue
            file_type, size, file_path = parts
            files.append(
                FileInfo(
                    path=file_path.decode(errors="replace"),
                    is_dir=file_type == b"d",
                    size=int(size) if size.isdigit() else None,
                )
            )
        return files

    def read_file(self, path: str) -> bytes:
        completed = self._run_remote(
            _bash_command(f"cat -- {shlex.quote(path)}")
        )
        self._raise_for_file_error("read_file", completed)
        return completed.stdout

    def write_file(self, path: str, content: bytes | str) -> None:
        data = content if isinstance(content, bytes) else content.encode()
        parent = posixpath.dirname(path) or "."
        script = (
            f"mkdir -p -- {shlex.quote(parent)} && "
            f"cat > {shlex.quote(path)}"
        )
        completed = self._run_remote(_bash_command(script), input_data=data)
        self._raise_for_file_error("write_file", completed)

    def upload_file(self, local_path: str, remote_path: str) -> None:
        try:
            data = pathlib.Path(local_path).read_bytes()
            self.write_file(remote_path, data)
        except SandboxExecutionError:
            raise
        except OSError as exc:
            raise SandboxExecutionError(
                f"RunPod upload_file failed: {exc}",
                backend="runpod",
                op="upload_file",
            ) from exc

    def download_file(self, remote_path: str, local_path: str) -> None:
        try:
            pathlib.Path(local_path).write_bytes(self.read_file(remote_path))
        except SandboxExecutionError:
            raise
        except OSError as exc:
            raise SandboxExecutionError(
                f"RunPod download_file failed: {exc}",
                backend="runpod",
                op="download_file",
            ) from exc

    def snapshot(self) -> SnapshotInfo:
        raise FeatureNotSupportedError(
            "Filesystem snapshots are not supported by the RunPod backend",
            backend="runpod",
            op="snapshot",
        )

    def session_state(self) -> dict:
        if self._pod_id is None:
            raise SandboxNotFoundError(
                "The RunPod Pod has already been destroyed",
                backend="runpod",
                op="session_state",
            )
        return {
            "pod_id": self._pod_id,
            "workdir": self._workdir,
            "timeout_secs": self._timeout_secs,
            "ssh_user": self._ssh_user,
            "ssh_private_key_path": self._ssh_private_key_path,
            "ssh_connect_timeout_secs": self._ssh_connect_timeout_secs,
            "ssh_strict_host_key_checking": (
                self._ssh_strict_host_key_checking
            ),
            "ssh_known_hosts_file": self._ssh_known_hosts_file,
            "api_base_url": self._api_base_url,
            "api_timeout_secs": self._api_timeout_secs,
            "cost_per_hour": self.cost_per_hour,
        }

    @property
    def provider_resource_id(self) -> str | None:
        """Return the RunPod Pod ID while it is attached."""
        return self._pod_id

    def destroy(self) -> None:
        pod_id = self._pod_id
        if pod_id is None:
            return
        try:
            self._api.delete_pod(pod_id)
        except _RunpodApiError as exc:
            if exc.status_code == 404:
                self._pod_id = None
                return
            raise SandboxConnectionError(
                f"Failed to delete RunPod Pod '{pod_id}': {exc}",
                backend="runpod",
                op="destroy",
                context={"pod_id": pod_id},
            ) from exc
        except Exception as exc:
            raise SandboxConnectionError(
                f"Failed to delete RunPod Pod '{pod_id}': {exc}",
                backend="runpod",
                op="destroy",
                context={"pod_id": pod_id},
            ) from exc
        self._pod_id = None

    def _run_remote(
        self,
        remote_command: str,
        *,
        input_data: bytes | None = None,
        timeout_secs: float | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        args = [
            self._ssh_path,
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={self._ssh_connect_timeout_secs}",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=2",
            "-o",
            f"StrictHostKeyChecking={self._ssh_strict_host_key_checking}",
        ]
        if self._ssh_known_hosts_file:
            args.extend(
                [
                    "-o",
                    f"UserKnownHostsFile={self._ssh_known_hosts_file}",
                ]
            )
        if self._ssh_private_key_path:
            args.extend(["-i", self._ssh_private_key_path])
        args.extend(
            [
                "-p",
                str(self._port),
                f"{self._ssh_user}@{self._host}",
                remote_command,
            ]
        )
        effective_timeout = (
            self._timeout_secs if timeout_secs is None else timeout_secs
        )
        try:
            return subprocess.run(
                args,
                input=input_data,
                capture_output=True,
                check=False,
                timeout=effective_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise SandboxTimeoutError(
                f"RunPod SSH operation timed out after "
                f"{effective_timeout} seconds",
                backend="runpod",
                op="ssh",
            ) from exc
        except OSError as exc:
            raise SandboxConnectionError(
                f"RunPod SSH connection failed: {exc}",
                backend="runpod",
                op="ssh",
            ) from exc

    @staticmethod
    def _raise_for_file_error(
        operation: str, completed: subprocess.CompletedProcess[bytes]
    ) -> None:
        if completed.returncode == 0:
            return
        stderr = completed.stderr.decode(errors="replace").strip()
        raise SandboxExecutionError(
            f"RunPod {operation} failed with exit code "
            f"{completed.returncode}: {stderr}",
            backend="runpod",
            op=operation,
            context={"exit_code": completed.returncode},
        )


def _split_options(
    backend_options: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    native = dict(backend_options)
    transport: dict[str, Any] = {
        "api_base_url": native.pop("api_base_url", _API_BASE_URL),
        "api_timeout_secs": native.pop(
            "api_timeout_secs", _DEFAULT_API_TIMEOUT_SECS
        ),
        "create_timeout_secs": native.pop(
            "create_timeout_secs", _DEFAULT_CREATE_TIMEOUT_SECS
        ),
        "poll_interval_secs": native.pop(
            "poll_interval_secs", _DEFAULT_POLL_INTERVAL_SECS
        ),
        "ssh_connect_timeout_secs": native.pop(
            "ssh_connect_timeout_secs", _DEFAULT_SSH_CONNECT_TIMEOUT_SECS
        ),
        "ssh_known_hosts_file": native.pop("ssh_known_hosts_file", None),
        "ssh_private_key_path": native.pop("ssh_private_key_path", None),
        "ssh_strict_host_key_checking": native.pop(
            "ssh_strict_host_key_checking", "accept-new"
        ),
        "ssh_user": native.pop("ssh_user", "root"),
    }
    _validate_transport(transport)
    transport["api_base_url"] = str(transport["api_base_url"])
    transport["api_timeout_secs"] = float(transport["api_timeout_secs"])
    transport["create_timeout_secs"] = float(transport["create_timeout_secs"])
    transport["poll_interval_secs"] = float(transport["poll_interval_secs"])
    transport["ssh_connect_timeout_secs"] = math.ceil(
        float(transport["ssh_connect_timeout_secs"])
    )
    transport["ssh_user"] = str(transport["ssh_user"])
    for key in ("ssh_known_hosts_file", "ssh_private_key_path"):
        if transport[key] is not None:
            transport[key] = os.path.expanduser(str(transport[key]))
    return native, transport


def _transport_from_state(data: Mapping[str, Any]) -> dict[str, Any]:
    options = {key: data[key] for key in _TRANSPORT_OPTIONS if key in data}
    _, transport = _split_options(options)
    return transport


def _validate_transport(transport: Mapping[str, Any]) -> None:
    for key in (
        "api_timeout_secs",
        "create_timeout_secs",
        "ssh_connect_timeout_secs",
    ):
        try:
            value = float(transport[key])
        except (TypeError, ValueError) as exc:
            raise SandboxConfigurationError(
                f"RunPod backend_options['{key}'] must be a number"
            ) from exc
        if value <= 0:
            raise SandboxConfigurationError(
                f"RunPod backend_options['{key}'] must be positive"
            )
    try:
        poll_interval = float(transport["poll_interval_secs"])
    except (TypeError, ValueError) as exc:
        raise SandboxConfigurationError(
            "RunPod backend_options['poll_interval_secs'] must be a number"
        ) from exc
    if poll_interval < 0:
        raise SandboxConfigurationError(
            "RunPod backend_options['poll_interval_secs'] cannot be negative"
        )
    strict = str(transport["ssh_strict_host_key_checking"])
    if strict not in {"yes", "no", "accept-new"}:
        raise SandboxConfigurationError(
            "RunPod backend_options['ssh_strict_host_key_checking'] must be "
            "'yes', 'no', or 'accept-new'"
        )


def _create_payload(
    config: SandboxConfig, native_options: Mapping[str, Any]
) -> dict[str, Any]:
    gpu_type_ids, gpu_count = _gpu_request(config.gpu)
    payload: dict[str, Any] = {
        "cloudType": "SECURE",
        "computeType": "GPU",
        "gpuCount": gpu_count,
        "gpuTypeIds": gpu_type_ids,
        "name": "bespokelabs-sandbox",
        "ports": ["22/tcp"],
        "supportPublicIp": True,
    }
    if config.template:
        payload["templateId"] = config.template
    else:
        payload["imageName"] = config.image or _DEFAULT_IMAGE
    if config.disk_mb is not None:
        payload["containerDiskInGb"] = max(1, math.ceil(config.disk_mb / 1024))
    if config.env_vars:
        payload["env"] = dict(config.env_vars)

    payload.update(native_options)
    if payload.get("computeType") != "GPU":
        raise SandboxConfigurationError(
            "The RunPod backend only supports GPU Pods"
        )
    if payload.get("supportPublicIp") is not True:
        raise SandboxConfigurationError(
            "The RunPod backend requires supportPublicIp=True for SSH"
        )
    effective_gpu_count = _positive_integer(
        payload.get("gpuCount"), option="gpuCount"
    )
    payload["gpuCount"] = effective_gpu_count
    payload.setdefault(
        "minRAMPerGPU",
        max(1, math.ceil(config.memory_mb / 1024 / effective_gpu_count)),
    )
    payload.setdefault(
        "minVCPUPerGPU", max(1, math.ceil(config.cpu / effective_gpu_count))
    )
    raw_ports = payload.get("ports")
    if not isinstance(raw_ports, list):
        raise SandboxConfigurationError(
            "RunPod backend_options['ports'] must be a list"
        )
    ports = list(raw_ports)
    if "22/tcp" not in ports:
        ports.append("22/tcp")
    payload["ports"] = ports
    gpu_ids = payload.get("gpuTypeIds")
    if not isinstance(gpu_ids, list) or not gpu_ids:
        raise SandboxConfigurationError(
            "RunPod requires gpu= or backend_options['gpuTypeIds']"
        )
    return payload


def _gpu_request(gpu: str | None) -> tuple[list[str], int]:
    if not gpu:
        return [], 1
    gpu_type = gpu.strip()
    gpu_count = 1
    if ":" in gpu_type:
        possible_type, possible_count = gpu_type.rsplit(":", 1)
        if not possible_count.isdigit():
            raise SandboxConfigurationError(
                f"Invalid RunPod GPU count in reservation: {gpu!r}"
            )
        gpu_type = possible_type.strip()
        gpu_count = int(possible_count)
    if not gpu_type or gpu_count < 1:
        raise SandboxConfigurationError(
            f"Invalid RunPod GPU reservation: {gpu!r}"
        )
    return [gpu_type], gpu_count


def _positive_integer(value: Any, *, option: str) -> int:
    if isinstance(value, bool):
        raise SandboxConfigurationError(
            f"RunPod backend_options['{option}'] must be a positive integer"
        )
    try:
        integer = int(value)
    except (TypeError, ValueError) as exc:
        raise SandboxConfigurationError(
            f"RunPod backend_options['{option}'] must be a positive integer"
        ) from exc
    if integer < 1 or integer != value:
        raise SandboxConfigurationError(
            f"RunPod backend_options['{option}'] must be a positive integer"
        )
    return integer


def _ssh_endpoint(pod: Mapping[str, Any]) -> tuple[str, int] | None:
    host = pod.get("publicIp")
    mappings = pod.get("portMappings")
    if not host or not isinstance(mappings, Mapping):
        return None
    port = mappings.get("22", mappings.get(22))
    if port is None:
        return None
    try:
        return str(host), int(port)
    except (TypeError, ValueError):
        return None


def _cost_per_hour(pod: Mapping[str, Any]) -> float | None:
    value = pod.get("adjustedCostPerHr", pod.get("costPerHr"))
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _bash_command(script: str) -> str:
    return f"bash -lc {shlex.quote(script)}"
