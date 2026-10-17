"""Google Cloud Batch backend: native detached jobs, no interactive sandboxes.

A job is one Batch job with one task running one container runnable.  The
job id is the job's resource name (``projects/<p>/locations/<region>/jobs/
<id>``), so status, logs and cancel need no configuration beyond credentials.

Configuration (``JobSpec.backend_options`` wins over the environment):

  project          GCP project; else ``GOOGLE_CLOUD_PROJECT``, else the
                   Application Default Credentials project.
  region           Batch location; else ``GOOGLE_CLOUD_REGION``, else
                   ``CLOUDSDK_COMPUTE_REGION``.
  service_account  Email of the service account the VMs run as.
  network /        Network and subnetwork URLs or paths for the VMs;
  subnetwork       ``no_external_ip`` (bool) drops their external IP.
  machine_type     Overrides the machine type chosen for ``cpu`` /
                   ``memory_mb`` / ``gpu``.
  provisioning_model  ``STANDARD`` (default), ``SPOT``, ``PREEMPTIBLE`` or
                   ``FLEX_START``.
  allowed_locations   List such as ``["zones/us-central1-a"]``.
  max_retry_count  Task retries (default 0).
  container_options   Extra ``docker run`` options for the container.
  gpu_driver_version  Passed to Batch with ``install_gpu_drivers``.
  job_id           Explicit Batch job id; else ``<job_id_prefix>-<random>``
                   (``job_id_prefix`` defaults to ``job``).

Credentials are Application Default Credentials (``gcloud auth
application-default login`` locally, the attached service account on GCP).
"""

from __future__ import annotations

import datetime
import math
import os
import re
import threading
import uuid
from typing import Any

from bespokelabs.sandbox.exceptions import (
    BackendNotInstalledError,
    FeatureNotSupportedError,
    SandboxAuthenticationError,
    SandboxConfigurationError,
    SandboxConnectionError,
    SandboxError,
    SandboxExecutionError,
    SandboxNotFoundError,
    SandboxTimeoutError,
)
from bespokelabs.sandbox.types import (
    JobSpec,
    JobState,
    JobStatus,
    SandboxConfig,
)

_INSTALL_HINT = (
    "Google Cloud Batch SDK not installed. Run: "
    "pip install 'bespokelabs-sandbox[gcp-batch]'"
)

_OPTION_KEYS = frozenset(
    {
        "project",
        "region",
        "service_account",
        "network",
        "subnetwork",
        "no_external_ip",
        "machine_type",
        "provisioning_model",
        "allowed_locations",
        "max_retry_count",
        "container_options",
        "gpu_driver_version",
        "job_id",
        "job_id_prefix",
    }
)

_JOB_NAME_RE = re.compile(
    r"projects/(?P<project>[^/]+)/locations/(?P<region>[^/]+)"
    r"/jobs/(?P<job>[^/]+)"
)
_JOB_ID_RE = re.compile(r"[a-z]([a-z0-9-]{0,61}[a-z0-9])?")
_LABEL_RE = re.compile(r"[^a-z0-9_-]")

# Batch job states -> JobState.  STATE_UNSPECIFIED is a job Batch has not
# classified yet, not an unknown outcome, so it reads as pending.  A job
# being cancelled still has VMs running, so it is not terminal yet.
_STATES = {
    "STATE_UNSPECIFIED": JobState.PENDING,
    "QUEUED": JobState.PENDING,
    "SCHEDULED": JobState.PENDING,
    "RUNNING": JobState.RUNNING,
    "SUCCEEDED": JobState.SUCCEEDED,
    "FAILED": JobState.FAILED,
    "CANCELLATION_IN_PROGRESS": JobState.RUNNING,
    "CANCELLED": JobState.CANCELLED,
    "DELETION_IN_PROGRESS": JobState.CANCELLED,
}

# Exit codes Batch reserves for its own failures
# (https://cloud.google.com/batch/docs/troubleshooting#reserved-exit-codes).
_RESERVED_EXIT_CODES = {
    50001: "vm_preempted",
    50002: "vm_lost",
    50003: "vm_rebooted",
    50004: "vm_unresponsive",
    50005: "timeout",
    50006: "vm_recreated",
}

# Friendly GPU names (Modal-style, as JobSpec.gpu uses elsewhere) -> Compute
# Engine accelerator types.  A raw ``nvidia-*`` type is accepted as is.
_GPU_ALIASES = {
    "T4": "nvidia-tesla-t4",
    "P4": "nvidia-tesla-p4",
    "P100": "nvidia-tesla-p100",
    "V100": "nvidia-tesla-v100",
    "L4": "nvidia-l4",
    "A100": "nvidia-tesla-a100",
    "A100-40GB": "nvidia-tesla-a100",
    "A100-80GB": "nvidia-a100-80gb",
    "H100": "nvidia-h100-80gb",
    "H200": "nvidia-h200-141gb",
    "B200": "nvidia-b200",
}

# Accelerator-optimized machine types: (machine type, GPUs, vCPUs, GB).
# The machine type fixes the GPU count, so the smallest one with the
# requested count and enough CPU and memory is chosen.
_GPU_MACHINES: dict[str, list[tuple[str, int, int, int]]] = {
    "nvidia-l4": [
        ("g2-standard-4", 1, 4, 16),
        ("g2-standard-8", 1, 8, 32),
        ("g2-standard-12", 1, 12, 48),
        ("g2-standard-16", 1, 16, 64),
        ("g2-standard-32", 1, 32, 128),
        ("g2-standard-24", 2, 24, 96),
        ("g2-standard-48", 4, 48, 192),
        ("g2-standard-96", 8, 96, 384),
    ],
    "nvidia-tesla-a100": [
        ("a2-highgpu-1g", 1, 12, 85),
        ("a2-highgpu-2g", 2, 24, 170),
        ("a2-highgpu-4g", 4, 48, 340),
        ("a2-highgpu-8g", 8, 96, 680),
        ("a2-megagpu-16g", 16, 96, 1360),
    ],
    "nvidia-a100-80gb": [
        ("a2-ultragpu-1g", 1, 12, 170),
        ("a2-ultragpu-2g", 2, 24, 340),
        ("a2-ultragpu-4g", 4, 48, 680),
        ("a2-ultragpu-8g", 8, 96, 1360),
    ],
    "nvidia-h100-80gb": [
        ("a3-highgpu-1g", 1, 26, 234),
        ("a3-highgpu-2g", 2, 52, 468),
        ("a3-highgpu-4g", 4, 104, 936),
        ("a3-highgpu-8g", 8, 208, 1872),
    ],
    "nvidia-h200-141gb": [("a3-ultragpu-8g", 8, 224, 2952)],
    "nvidia-b200": [("a4-highgpu-8g", 8, 224, 3968)],
}

# Older GPUs attach to N1 VMs, sized here from cpu / memory_mb.
_N1_GPUS = frozenset(
    {
        "nvidia-tesla-t4",
        "nvidia-tesla-p4",
        "nvidia-tesla-p100",
        "nvidia-tesla-v100",
    }
)
_N1_SHAPES = [
    (family, vcpus, gb_per_vcpu)
    for family, gb_per_vcpu in (("n1-standard", 3.75), ("n1-highmem", 6.5))
    for vcpus in (1, 2, 4, 8, 16, 32, 64, 96)
    if not (family == "n1-highmem" and vcpus == 1)
]


def _n1_price(vcpus: int, gb_per_vcpu: float) -> float:
    """Relative on-demand price of an N1 shape (us-central1 vCPU/GB rates)."""
    return vcpus * (0.031611 + gb_per_vcpu * 0.004237)


# The VM keeps part of its memory for the OS and the Batch agent.
_USABLE_MEMORY = 0.9


def parse_gpu(gpu: str) -> tuple[str, int]:
    """Parse ``"<type>[:<count>]"`` into a Compute Engine accelerator type."""
    name, _, count_text = gpu.strip().partition(":")
    try:
        count = int(count_text) if count_text else 1
    except ValueError:
        count = 0
    if count < 1:
        raise SandboxConfigurationError(
            f"Invalid GPU request {gpu!r}: the count must be a positive integer",
            backend="gcp_batch",
        )
    key = name.rstrip("!").upper()
    if key in _GPU_ALIASES:
        return _GPU_ALIASES[key], count
    if name.lower().startswith("nvidia-"):
        return name.lower(), count
    raise SandboxConfigurationError(
        f"Unknown GPU type {name!r}; use one of "
        f"{', '.join(sorted(_GPU_ALIASES))} or a Compute Engine accelerator "
        "type such as 'nvidia-l4'",
        backend="gcp_batch",
    )


def machine_type_for(
    accelerator: str, count: int, cpu: float, memory_mb: int
) -> str:
    """Pick the smallest machine type that carries the GPUs and fits the task."""

    def fits(vcpus: int, gb: float) -> bool:
        return vcpus >= cpu and gb * 1024 * _USABLE_MEMORY >= memory_mb

    if accelerator in _N1_GPUS:
        for family, vcpus, gb_per_vcpu in sorted(
            _N1_SHAPES, key=lambda shape: _n1_price(shape[1], shape[2])
        ):
            if fits(vcpus, vcpus * gb_per_vcpu):
                return f"{family}-{vcpus}"
    else:
        machines = _GPU_MACHINES.get(accelerator)
        if machines is None:
            raise SandboxConfigurationError(
                f"No machine type is known for {accelerator!r}; pass "
                "backend_options['machine_type']",
                backend="gcp_batch",
            )
        candidates = [m for m in machines if m[1] == count]
        if not candidates:
            counts = sorted({m[1] for m in machines})
            raise SandboxConfigurationError(
                f"{accelerator} comes in counts of {counts}, not {count}",
                backend="gcp_batch",
            )
        for machine, _, vcpus, gb in candidates:
            if fits(vcpus, gb):
                return machine
    raise SandboxConfigurationError(
        f"No machine type with {count}x {accelerator} fits cpu={cpu}, "
        f"memory_mb={memory_mb}; pass backend_options['machine_type']",
        backend="gcp_batch",
    )


def _label(value: str) -> str:
    """Coerce a string into a GCP label key/value (lowercase, <= 63 chars)."""
    return _LABEL_RE.sub("_", value.lower())[:63]


def _labels(labels: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in labels.items():
        clean_key = _label(key)
        if not clean_key or not clean_key[0].isalpha():
            clean_key = f"l_{clean_key}"[:63]
        out[clean_key] = _label(value)
    return out


def _classify(exc: Exception, op: str) -> SandboxError:
    """Translate google-api-core errors into the public error taxonomy."""
    if isinstance(exc, SandboxError):
        return exc
    name = type(exc).__name__
    common = {"backend": "gcp_batch", "op": op}
    if name in {"PermissionDenied", "Unauthenticated", "Forbidden"}:
        return SandboxAuthenticationError(
            f"Google Cloud Batch {op} was not authorized: {exc}", **common
        )
    if name in {"InvalidArgument", "BadRequest", "FailedPrecondition"}:
        return SandboxConfigurationError(
            f"Google Cloud Batch rejected {op}: {exc}", **common
        )
    if name in {"DeadlineExceeded", "GatewayTimeout"} or isinstance(
        exc, TimeoutError
    ):
        return SandboxTimeoutError(
            f"Google Cloud Batch {op} timed out: {exc}", **common
        )
    if name in {
        "ServiceUnavailable",
        "InternalServerError",
        "BadGateway",
        "TooManyRequests",
        "ResourceExhausted",
        "Aborted",
        "RetryError",
    } or isinstance(exc, ConnectionError):
        return SandboxConnectionError(
            f"Google Cloud Batch {op} failed: {exc}", **common
        )
    return SandboxExecutionError(
        f"Google Cloud Batch {op} failed: {exc}", **common
    )


def _is_not_found(exc: Exception) -> bool:
    return type(exc).__name__ == "NotFound" or getattr(exc, "code", None) == 404


def _is_unimplemented(exc: Exception) -> bool:
    return (
        type(exc).__name__ in {"MethodNotImplemented", "NotImplemented"}
        or getattr(exc, "code", None) == 501
    )


def _as_datetime(value: Any) -> datetime.datetime | None:
    """Normalize a proto timestamp (DatetimeWithNanoseconds) to aware UTC."""
    if not isinstance(value, datetime.datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    if value.timestamp() <= 0:
        return None
    return value


def _enum_name(value: Any) -> str:
    return getattr(value, "name", None) or str(value)


class GcpBatchClient:
    """Job-only backend on Google Cloud Batch.

    ``create()`` / ``resume()`` raise FeatureNotSupportedError: Batch runs
    jobs, not interactive sandboxes.  Use ``SandboxClient("gcp_batch")``'s
    ``submit_job`` / ``job_status`` / ``wait_job`` / ``job_logs`` /
    ``cancel_job`` (Batch CancelJob; delete only as a fallback).
    """

    #: Interactive sandboxes are not available on this backend.
    supports_sessions = False

    def __init__(self) -> None:
        try:
            from google.cloud import batch_v1  # type: ignore[import-untyped]
        except ImportError as exc:
            raise BackendNotInstalledError(_INSTALL_HINT) from exc
        self._batch = batch_v1
        self._client: Any = None
        self._logging_clients: dict[str, Any] = {}
        self._lock = threading.Lock()

    # -- Interactive API (unsupported) -----------------------------------

    def create(self, config: SandboxConfig) -> Any:
        del config
        raise FeatureNotSupportedError(
            "gcp_batch runs detached jobs only; use "
            "SandboxClient('gcp_batch').submit_job(JobSpec(...))",
            backend="gcp_batch",
            op="create",
        )

    def resume(self, data: dict) -> Any:
        del data
        raise FeatureNotSupportedError(
            "gcp_batch runs detached jobs only; there is no sandbox to resume",
            backend="gcp_batch",
            op="resume",
        )

    # -- Clients ---------------------------------------------------------

    def _service(self) -> Any:
        if self._client is None:
            with self._lock:
                if self._client is None:
                    try:
                        self._client = self._batch.BatchServiceClient()
                    except Exception as exc:
                        raise _classify(exc, "connect") from exc
        return self._client

    def _logging(self, project: str) -> Any:
        client = self._logging_clients.get(project)
        if client is None:
            try:
                from google.cloud import (
                    logging_v2,  # type: ignore[import-untyped]
                )
            except ImportError as exc:
                raise BackendNotInstalledError(_INSTALL_HINT) from exc
            with self._lock:
                client = self._logging_clients.get(project)
                if client is None:
                    client = logging_v2.Client(project=project)
                    self._logging_clients[project] = client
        return client

    # -- Submit ----------------------------------------------------------

    def build_job(self, spec: JobSpec) -> Any:
        """Translate a JobSpec into a ``batch_v1.Job`` (no network I/O)."""
        batch = self._batch
        options = spec.backend_options
        unknown = sorted(set(options) - _OPTION_KEYS)
        if unknown:
            raise SandboxConfigurationError(
                f"Unknown gcp_batch backend_options: {', '.join(unknown)}",
                backend="gcp_batch",
                op="submit_job",
            )
        if spec.snapshot_id or spec.template:
            raise FeatureNotSupportedError(
                "gcp_batch runs container images; snapshot_id and template "
                "are not supported",
                backend="gcp_batch",
                op="submit_job",
            )
        if not spec.image:
            raise SandboxConfigurationError(
                "gcp_batch jobs need JobSpec.image (a container image URI, "
                "e.g. in Artifact Registry)",
                backend="gcp_batch",
                op="submit_job",
            )
        if spec.files or spec.workspace is not None:
            raise FeatureNotSupportedError(
                "gcp_batch cannot seed files before a job starts; bake them "
                "into the image or read them from Cloud Storage",
                backend="gcp_batch",
                op="submit_job",
            )

        if isinstance(spec.command, list):
            entrypoint, commands = spec.command[0], spec.command[1:]
        else:
            entrypoint, commands = "/bin/sh", ["-c", spec.command]
        container = batch.Runnable.Container(
            image_uri=spec.image,
            entrypoint=entrypoint,
            commands=commands,
            block_external_network=not spec.allow_internet,
        )
        container_options = []
        if spec.workdir:
            if re.search(r"\s", spec.workdir):
                raise SandboxConfigurationError(
                    "gcp_batch workdir cannot contain whitespace",
                    backend="gcp_batch",
                    op="submit_job",
                )
            container_options.append(f"--workdir={spec.workdir}")
        if options.get("container_options"):
            container_options.append(str(options["container_options"]))
        if container_options:
            container.options = " ".join(container_options)

        task = batch.TaskSpec(
            runnables=[batch.Runnable(container=container)],
            compute_resource=batch.ComputeResource(
                cpu_milli=int(math.ceil(spec.cpu * 1000)),
                memory_mib=int(spec.memory_mb),
            ),
            max_run_duration=datetime.timedelta(seconds=int(spec.timeout_secs)),
            max_retry_count=int(options.get("max_retry_count", 0)),
        )
        if spec.env_vars:
            task.environment = batch.Environment(variables=dict(spec.env_vars))

        policy = batch.AllocationPolicy.InstancePolicy()
        install_drivers = False
        machine_type = options.get("machine_type")
        if spec.gpu:
            accelerator, count = parse_gpu(spec.gpu)
            accel = batch.AllocationPolicy.Accelerator(
                type_=accelerator, count=count
            )
            if options.get("gpu_driver_version"):
                accel.driver_version = str(options["gpu_driver_version"])
            policy.accelerators = [accel]
            install_drivers = True
            machine_type = machine_type or machine_type_for(
                accelerator, count, spec.cpu, spec.memory_mb
            )
        if machine_type:
            policy.machine_type = str(machine_type)
        if spec.disk_mb:
            policy.boot_disk = batch.AllocationPolicy.Disk(
                size_gb=int(math.ceil(spec.disk_mb / 1024))
            )
        if options.get("provisioning_model"):
            model = str(options["provisioning_model"]).upper()
            try:
                policy.provisioning_model = (
                    batch.AllocationPolicy.ProvisioningModel[model]
                )
            except KeyError as exc:
                raise SandboxConfigurationError(
                    f"Unknown provisioning_model {model!r}",
                    backend="gcp_batch",
                    op="submit_job",
                ) from exc

        allocation = batch.AllocationPolicy(
            instances=[
                batch.AllocationPolicy.InstancePolicyOrTemplate(
                    policy=policy, install_gpu_drivers=install_drivers
                )
            ]
        )
        labels = _labels(spec.labels)
        if labels:
            # On the VMs too, so their cost is attributed like the job's.
            allocation.labels = labels
        if options.get("service_account"):
            allocation.service_account = batch.ServiceAccount(
                email=str(options["service_account"])
            )
        if options.get("allowed_locations"):
            allocation.location = batch.AllocationPolicy.LocationPolicy(
                allowed_locations=list(options["allowed_locations"])
            )
        if (
            options.get("network")
            or options.get("subnetwork")
            or options.get("no_external_ip")
        ):
            interface = batch.AllocationPolicy.NetworkInterface(
                no_external_ip_address=bool(options.get("no_external_ip"))
            )
            if options.get("network"):
                interface.network = str(options["network"])
            if options.get("subnetwork"):
                interface.subnetwork = str(options["subnetwork"])
            allocation.network = batch.AllocationPolicy.NetworkPolicy(
                network_interfaces=[interface]
            )

        return batch.Job(
            task_groups=[batch.TaskGroup(task_spec=task, task_count=1)],
            allocation_policy=allocation,
            labels=labels,
            logs_policy=batch.LogsPolicy(
                destination=batch.LogsPolicy.Destination.CLOUD_LOGGING
            ),
        )

    def submit_job(self, spec: JobSpec) -> str:
        options = spec.backend_options
        job = self.build_job(spec)
        project = self._project(options)
        region = self._region(options)
        job_id = options.get("job_id") or (
            f"{options.get('job_id_prefix', 'job')}-{uuid.uuid4().hex[:16]}"
        )
        if not _JOB_ID_RE.fullmatch(str(job_id)):
            raise SandboxConfigurationError(
                f"Invalid Batch job id {job_id!r}: use lowercase letters, "
                "digits and hyphens, starting with a letter (max 63 chars)",
                backend="gcp_batch",
                op="submit_job",
            )
        parent = f"projects/{project}/locations/{region}"
        request = self._batch.CreateJobRequest(
            parent=parent,
            job_id=str(job_id),
            job=job,
            # Makes the SDK's own retries of this create idempotent.
            request_id=str(uuid.uuid4()),
        )
        try:
            created = self._service().create_job(request=request)
        except Exception as exc:
            raise _classify(exc, "submit_job") from exc
        return getattr(created, "name", None) or f"{parent}/jobs/{job_id}"

    # -- Reattach --------------------------------------------------------

    @staticmethod
    def _parse(job_id: str) -> dict[str, str]:
        match = _JOB_NAME_RE.fullmatch(str(job_id))
        if match is None:
            raise SandboxConfigurationError(
                f"Not a Cloud Batch job name: {job_id!r}",
                backend="gcp_batch",
                op="decode_job_id",
            )
        return match.groupdict()

    def _get(self, job_id: str, op: str) -> Any | None:
        self._parse(job_id)
        try:
            return self._service().get_job(name=job_id)
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise _classify(exc, op) from exc

    def _task(self, job: Any) -> Any | None:
        """Return the job's single task, for its exit code and timings."""
        groups = list(getattr(job, "task_groups", None) or [])
        group = getattr(groups[0], "name", "") if groups else ""
        parent = group or f"{job.name}/taskGroups/group0"
        try:
            pager = self._service().list_tasks(
                request=self._batch.ListTasksRequest(parent=parent, page_size=1)
            )
            for task in pager:
                return task
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise _classify(exc, "job_status") from exc
        return None

    def _gone(self, job_id: str) -> JobStatus:
        return JobStatus(
            job_id=job_id,
            state=JobState.UNKNOWN,
            reason="job_not_found",
            details={"backend": "gcp_batch"},
        )

    def _status(self, job_id: str, job: Any) -> JobStatus:
        batch_state = _enum_name(job.status.state)
        state = _STATES.get(batch_state, JobState.PENDING)
        details: dict[str, Any] = {
            "backend": "gcp_batch",
            "batch_state": batch_state,
            "uid": getattr(job, "uid", None),
        }
        events = list(getattr(job.status, "status_events", None) or [])
        if events:
            details["last_event"] = getattr(events[-1], "description", None)
        exit_code = None
        started_at = finished_at = None
        if state is not JobState.PENDING:
            task = self._task(job)
            for event in list(
                getattr(getattr(task, "status", None), "status_events", None)
                or []
            ):
                task_state = _enum_name(getattr(event, "task_state", ""))
                when = _as_datetime(getattr(event, "event_time", None))
                if task_state == "RUNNING" and started_at is None:
                    started_at = when
                if task_state in {"SUCCEEDED", "FAILED"}:
                    finished_at = when
                # Proto3 reads an unset message as zeros; only a present
                # task_execution carries a real exit code.
                if task_state in {"SUCCEEDED", "FAILED"} and (
                    "task_execution" in event
                ):
                    exit_code = int(event.task_execution.exit_code)
        reason = None
        if state is JobState.FAILED:
            reason = (
                "batch_failed"
                if exit_code is None
                else _RESERVED_EXIT_CODES.get(exit_code, "exit_code")
            )
        elif state is JobState.CANCELLED:
            reason = "cancelled"
        if state is JobState.SUCCEEDED and exit_code is None:
            exit_code = 0
        return JobStatus(
            job_id=job_id,
            state=state,
            exit_code=exit_code,
            started_at=started_at,
            finished_at=finished_at,
            reason=reason,
            details=details,
        )

    def job_status(self, job_id: str) -> JobStatus:
        job = self._get(job_id, "job_status")
        if job is None:
            return self._gone(job_id)
        return self._status(job_id, job)

    def job_logs(self, job_id: str, tail: int | None = None) -> str:
        """Read the task's stdout/stderr from Cloud Logging.

        Batch writes them to the ``batch_task_logs`` log, labelled with the
        job's uid.  Entries can lag the task by a few seconds.
        """
        parts = self._parse(job_id)
        job = self._get(job_id, "job_logs")
        if job is None:
            raise SandboxNotFoundError(
                f"Cloud Batch job {job_id} no longer exists",
                backend="gcp_batch",
                op="job_logs",
            )
        project = parts["project"]
        log_filter = (
            f'logName="projects/{project}/logs/batch_task_logs" '
            f'AND labels.job_uid="{job.uid}"'
        )
        if tail == 0:
            return ""
        try:
            from google.cloud.logging_v2 import (
                DESCENDING,  # type: ignore[import-untyped]
            )

            entries = self._logging(project).list_entries(
                resource_names=[f"projects/{project}"],
                filter_=log_filter,
                order_by=DESCENDING if tail else None,
                max_results=tail,
            )
            lines = [self._entry_text(entry) for entry in entries]
        except SandboxError:
            raise
        except Exception as exc:
            raise _classify(exc, "job_logs") from exc
        if tail:
            lines.reverse()
        return "".join(
            line if line.endswith("\n") else line + "\n" for line in lines
        )

    @staticmethod
    def _entry_text(entry: Any) -> str:
        payload = getattr(entry, "payload", None)
        if isinstance(payload, str):
            return payload
        if isinstance(payload, dict):
            return str(payload.get("message", payload))
        return str(payload or "")

    def cancel_job(self, job_id: str) -> JobStatus:
        """Cancel a pending or running job; a finished job is left as is.

        Uses Batch's CancelJob, which stops the job's VMs and keeps its
        record, so later status reads ``cancelled``.  The returned operation
        is not awaited: the job reports CANCELLATION_IN_PROGRESS (``running``)
        until Batch settles it as CANCELLED.  When the installed SDK or the
        service lacks CancelJob, the job is deleted instead (also stopping
        its VMs) and the result says so with ``reason="cancelled_by_delete"``;
        a deleted job later reads as ``unknown`` / ``job_not_found``.
        """
        job = self._get(job_id, "cancel_job")
        if job is None:
            return self._gone(job_id)
        status = self._status(job_id, job)
        if status.done:
            return status
        reason = "cancelled"
        if status.details.get("batch_state") != "CANCELLATION_IN_PROGRESS":
            reason = self._cancel_or_delete(job_id)
            if reason is None:
                return self._gone(job_id)
        return JobStatus(
            job_id=job_id,
            state=JobState.CANCELLED,
            started_at=status.started_at,
            reason=reason,
            details=status.details,
        )

    def _cancel_or_delete(self, job_id: str) -> str | None:
        """Stop the job; the JobStatus reason, or ``None`` if it is gone."""
        service = self._service()
        cancel = getattr(service, "cancel_job", None)
        if callable(cancel) and hasattr(self._batch, "CancelJobRequest"):
            try:
                cancel(name=job_id)
                return "cancelled"
            except Exception as exc:
                if _is_not_found(exc):
                    return None
                if not _is_unimplemented(exc):
                    raise _classify(exc, "cancel_job") from exc
        try:
            service.delete_job(name=job_id)
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise _classify(exc, "cancel_job") from exc
        return "cancelled_by_delete"

    # -- Configuration ---------------------------------------------------

    @staticmethod
    def _project(options: dict) -> str:
        project = options.get("project") or os.environ.get(
            "GOOGLE_CLOUD_PROJECT"
        )
        if not project:
            try:
                import google.auth  # type: ignore[import-untyped]

                _, project = google.auth.default()
            except Exception:
                project = None
        if not project:
            raise SandboxConfigurationError(
                "No GCP project: set GOOGLE_CLOUD_PROJECT or "
                "backend_options['project']",
                backend="gcp_batch",
                op="submit_job",
            )
        return str(project)

    @staticmethod
    def _region(options: dict) -> str:
        region = (
            options.get("region")
            or os.environ.get("GOOGLE_CLOUD_REGION")
            or os.environ.get("CLOUDSDK_COMPUTE_REGION")
        )
        if not region:
            raise SandboxConfigurationError(
                "No Cloud Batch region: set GOOGLE_CLOUD_REGION or "
                "backend_options['region']",
                backend="gcp_batch",
                op="submit_job",
            )
        return str(region)
