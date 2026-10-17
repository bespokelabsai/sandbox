"""Google Cloud Batch backend tests.

The Batch and Cloud Logging clients are faked; these tests never call Google
Cloud.  They build real ``batch_v1`` request objects, so they need the
``[gcp-batch]`` extra and skip without it.
"""

from __future__ import annotations

import datetime
import os
import unittest
from unittest import mock

from bespokelabs.sandbox import JobSpec, JobState, Sandbox, SandboxClient
from bespokelabs.sandbox.exceptions import (
    BackendNotInstalledError,
    FeatureNotSupportedError,
    SandboxAuthenticationError,
    SandboxConfigurationError,
    SandboxConnectionError,
)

try:
    from google.api_core import exceptions as gexc
    from google.cloud import batch_v1

    _HAS_BATCH = True
except ImportError:  # pragma: no cover - exercised only without the extra
    _HAS_BATCH = False

if _HAS_BATCH:
    from bespokelabs.sandbox.backends.gcp_batch import (
        GcpBatchClient,
        machine_type_for,
        parse_gpu,
    )

JOB = "projects/proj/locations/us-central1/jobs/job-1"
_ENV = {"GOOGLE_CLOUD_PROJECT": "proj", "GOOGLE_CLOUD_REGION": "us-central1"}


def _when(seconds: int) -> datetime.datetime:
    return datetime.datetime(2026, 10, 17, 12, 0, seconds, tzinfo=datetime.UTC)


def _job(state: str, *, uid: str = "uid-1") -> object:
    return batch_v1.Job(
        name=JOB,
        uid=uid,
        status=batch_v1.JobStatus(state=batch_v1.JobStatus.State[state]),
        task_groups=[batch_v1.TaskGroup(name=f"{JOB}/taskGroups/group0")],
    )


def _task(*events: tuple[str, int, int | None]) -> object:
    status_events = []
    for state, second, exit_code in events:
        event = batch_v1.StatusEvent(
            task_state=batch_v1.TaskStatus.State[state],
            event_time=_when(second),
        )
        if exit_code is not None:
            event.task_execution = batch_v1.TaskExecution(exit_code=exit_code)
        status_events.append(event)
    return batch_v1.Task(
        name=f"{JOB}/taskGroups/group0/tasks/0",
        status=batch_v1.TaskStatus(status_events=status_events),
    )


@unittest.skipUnless(_HAS_BATCH, "google-cloud-batch not installed")
class GcpBatchTestCase(unittest.TestCase):

    def setUp(self) -> None:
        self.service = mock.MagicMock()
        self.backend = GcpBatchClient()
        self.backend._client = self.service
        patcher = mock.patch.dict(
            "bespokelabs.sandbox.backends.BACKENDS",
            {"gcp_batch": mock.Mock(return_value=self.backend)},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, _ENV)
        env.start()
        self.addCleanup(env.stop)
        self.client = SandboxClient("gcp_batch")


class BuildJobTests(GcpBatchTestCase):

    def test_maps_the_spec_onto_a_batch_job(self) -> None:
        job = self.backend.build_job(
            JobSpec(
                command=["python", "-m", "train", "--epochs", "3"],
                image="us-docker.pkg.dev/proj/repo/train:1",
                env_vars={"MODE": "fast"},
                cpu=4,
                memory_mb=16384,
                disk_mb=100 * 1024,
                timeout_secs=7200,
                allow_internet=False,
                labels={"Team": "Evals", "run/id": "R_1"},
                workdir="/work",
                backend_options={
                    "service_account": "runner@proj.iam.gserviceaccount.com",
                    "network": "global/networks/default",
                    "subnetwork": "regions/us-central1/subnetworks/default",
                    "no_external_ip": True,
                    "provisioning_model": "spot",
                    "allowed_locations": ["zones/us-central1-a"],
                    "max_retry_count": 2,
                },
            )
        )
        task = job.task_groups[0].task_spec
        self.assertEqual(job.task_groups[0].task_count, 1)
        container = task.runnables[0].container
        self.assertEqual(
            container.image_uri, "us-docker.pkg.dev/proj/repo/train:1"
        )
        self.assertEqual(container.entrypoint, "python")
        self.assertEqual(
            list(container.commands), ["-m", "train", "--epochs", "3"]
        )
        self.assertTrue(container.block_external_network)
        self.assertEqual(container.options, "--workdir=/work")
        self.assertEqual(dict(task.environment.variables), {"MODE": "fast"})
        self.assertEqual(task.compute_resource.cpu_milli, 4000)
        self.assertEqual(task.compute_resource.memory_mib, 16384)
        self.assertEqual(task.max_run_duration.total_seconds(), 7200)
        self.assertEqual(task.max_retry_count, 2)

        self.assertEqual(dict(job.labels), {"team": "evals", "run_id": "r_1"})
        allocation = job.allocation_policy
        self.assertEqual(dict(allocation.labels), dict(job.labels))
        self.assertEqual(
            allocation.service_account.email,
            "runner@proj.iam.gserviceaccount.com",
        )
        nic = allocation.network.network_interfaces[0]
        self.assertEqual(nic.network, "global/networks/default")
        self.assertTrue(nic.no_external_ip_address)
        self.assertEqual(
            list(allocation.location.allowed_locations),
            ["zones/us-central1-a"],
        )
        policy = allocation.instances[0].policy
        self.assertEqual(policy.boot_disk.size_gb, 100)
        self.assertEqual(
            policy.provisioning_model,
            batch_v1.AllocationPolicy.ProvisioningModel.SPOT,
        )
        self.assertFalse(allocation.instances[0].install_gpu_drivers)
        self.assertEqual(
            job.logs_policy.destination,
            batch_v1.LogsPolicy.Destination.CLOUD_LOGGING,
        )

    def test_shell_commands_run_under_sh(self) -> None:
        job = self.backend.build_job(
            JobSpec(command="echo hi && exit 3", image="img")
        )
        container = job.task_groups[0].task_spec.runnables[0].container
        self.assertEqual(container.entrypoint, "/bin/sh")
        self.assertEqual(list(container.commands), ["-c", "echo hi && exit 3"])

    def test_gpu_picks_accelerators_drivers_and_machine(self) -> None:
        job = self.backend.build_job(
            JobSpec(command="nvidia-smi", image="img", gpu="L4", cpu=6)
        )
        instance = job.allocation_policy.instances[0]
        self.assertTrue(instance.install_gpu_drivers)
        self.assertEqual(instance.policy.machine_type, "g2-standard-8")
        accel = instance.policy.accelerators[0]
        self.assertEqual((accel.type_, accel.count), ("nvidia-l4", 1))

        job = self.backend.build_job(
            JobSpec(
                command="x",
                image="img",
                gpu="H100:8",
                backend_options={"machine_type": "a3-megagpu-8g"},
            )
        )
        policy = job.allocation_policy.instances[0].policy
        self.assertEqual(policy.machine_type, "a3-megagpu-8g")
        self.assertEqual(policy.accelerators[0].type_, "nvidia-h100-80gb")

    def test_rejections(self) -> None:
        cases = [
            (JobSpec(command="x"), SandboxConfigurationError),
            (JobSpec(command="x", snapshot_id="s"), FeatureNotSupportedError),
            (
                JobSpec(command="x", image="i", files={"a": "b"}),
                FeatureNotSupportedError,
            ),
            (
                JobSpec(command="x", image="i", backend_options={"nope": 1}),
                SandboxConfigurationError,
            ),
            (
                JobSpec(command="x", image="i", gpu="L4:3"),
                SandboxConfigurationError,
            ),
            (
                JobSpec(
                    command="x",
                    image="i",
                    backend_options={"provisioning_model": "free"},
                ),
                SandboxConfigurationError,
            ),
        ]
        for spec, error in cases:
            with self.subTest(spec=spec):
                with self.assertRaises(error):
                    self.backend.build_job(spec)

    def test_interactive_sandboxes_are_not_supported(self) -> None:
        with self.assertRaises(FeatureNotSupportedError):
            Sandbox("gcp_batch")
        with self.assertRaises(FeatureNotSupportedError):
            self.client.job_sandbox(JOB)


class GpuSelectionTests(unittest.TestCase):

    @unittest.skipUnless(_HAS_BATCH, "google-cloud-batch not installed")
    def test_parse_gpu(self) -> None:
        self.assertEqual(parse_gpu("A100-80GB:2"), ("nvidia-a100-80gb", 2))
        self.assertEqual(parse_gpu("t4"), ("nvidia-tesla-t4", 1))
        self.assertEqual(parse_gpu("nvidia-l4:4"), ("nvidia-l4", 4))
        for bad in ("A10G", "L4:0", "L4:x"):
            with self.subTest(gpu=bad):
                with self.assertRaises(SandboxConfigurationError):
                    parse_gpu(bad)

    @unittest.skipUnless(_HAS_BATCH, "google-cloud-batch not installed")
    def test_machine_type_for(self) -> None:
        self.assertEqual(
            machine_type_for("nvidia-l4", 1, 1, 1024), "g2-standard-4"
        )
        # 16 GB nominal is not 16 GiB usable.
        self.assertEqual(
            machine_type_for("nvidia-l4", 1, 1, 16384), "g2-standard-8"
        )
        self.assertEqual(
            machine_type_for("nvidia-l4", 4, 1, 1024), "g2-standard-48"
        )
        self.assertEqual(
            machine_type_for("nvidia-tesla-t4", 1, 3, 1024), "n1-standard-4"
        )
        self.assertEqual(
            machine_type_for("nvidia-tesla-t4", 1, 2, 11 * 1024),
            "n1-highmem-2",
        )
        self.assertEqual(
            machine_type_for("nvidia-h100-80gb", 8, 1, 1024), "a3-highgpu-8g"
        )
        with self.assertRaises(SandboxConfigurationError):
            machine_type_for("nvidia-l4", 1, 64, 1024)
        with self.assertRaises(SandboxConfigurationError):
            machine_type_for("nvidia-rtx-pro-6000", 1, 1, 1024)


class SubmitTests(GcpBatchTestCase):

    def test_submit_returns_the_resource_name(self) -> None:
        self.service.create_job.return_value = batch_v1.Job(name=JOB)
        job_id = self.client.submit_job(
            JobSpec(
                command="true",
                image="img",
                backend_options={"job_id": "job-1"},
            )
        )
        self.assertEqual(job_id, JOB)
        request = self.service.create_job.call_args.kwargs["request"]
        self.assertEqual(request.parent, "projects/proj/locations/us-central1")
        self.assertEqual(request.job_id, "job-1")
        self.assertTrue(request.request_id)

    def test_generated_ids_and_explicit_location(self) -> None:
        self.service.create_job.return_value = batch_v1.Job()
        job_id = self.client.submit_job(
            JobSpec(
                command="true",
                image="img",
                backend_options={
                    "project": "other",
                    "region": "europe-west4",
                    "job_id_prefix": "pdk",
                },
            )
        )
        self.assertRegex(
            job_id,
            r"^projects/other/locations/europe-west4/jobs/pdk-[0-9a-f]+$",
        )

    def test_configuration_errors(self) -> None:
        with self.assertRaises(SandboxConfigurationError):
            self.client.submit_job(
                JobSpec(
                    command="true",
                    image="img",
                    backend_options={"job_id": "Bad_ID"},
                )
            )
        with mock.patch.dict(os.environ, {"GOOGLE_CLOUD_REGION": ""}):
            with self.assertRaises(SandboxConfigurationError):
                self.client.submit_job(JobSpec(command="true", image="img"))
        self.service.create_job.assert_not_called()

    def test_provider_errors_are_classified(self) -> None:
        self.service.create_job.side_effect = gexc.PermissionDenied("no")
        with self.assertRaises(SandboxAuthenticationError):
            self.client.submit_job(JobSpec(command="true", image="img"))
        self.service.create_job.side_effect = gexc.ServiceUnavailable("503")
        with self.assertRaises(SandboxConnectionError):
            self.client.submit_job(JobSpec(command="true", image="img"))


class StatusTests(GcpBatchTestCase):

    def test_pending_does_not_list_tasks(self) -> None:
        self.service.get_job.return_value = _job("QUEUED")
        status = self.client.job_status(JOB)
        self.assertEqual(status.state, JobState.PENDING)
        self.assertEqual(status.details["batch_state"], "QUEUED")
        self.service.list_tasks.assert_not_called()

    def test_running(self) -> None:
        self.service.get_job.return_value = _job("RUNNING")
        self.service.list_tasks.return_value = iter(
            [_task(("ASSIGNED", 1, None), ("RUNNING", 5, None))]
        )
        status = self.client.job_status(JOB)
        self.assertEqual(status.state, JobState.RUNNING)
        self.assertEqual(status.started_at, _when(5))
        self.assertIsNone(status.finished_at)

    def test_succeeded(self) -> None:
        self.service.get_job.return_value = _job("SUCCEEDED")
        self.service.list_tasks.return_value = iter(
            [_task(("RUNNING", 5, None), ("SUCCEEDED", 9, 0))]
        )
        status = self.client.job_status(JOB)
        self.assertEqual((status.state, status.exit_code), ("succeeded", 0))
        self.assertEqual(status.finished_at, _when(9))
        parent = self.service.list_tasks.call_args.kwargs["request"].parent
        self.assertEqual(parent, f"{JOB}/taskGroups/group0")

    def test_failed_and_timed_out(self) -> None:
        for code, reason in ((3, "exit_code"), (50005, "timeout")):
            with self.subTest(code=code):
                self.service.get_job.return_value = _job("FAILED")
                self.service.list_tasks.return_value = iter(
                    [_task(("RUNNING", 5, None), ("FAILED", 9, code))]
                )
                status = self.client.job_status(JOB)
                self.assertEqual(
                    (status.state, status.exit_code, status.reason),
                    (JobState.FAILED, code, reason),
                )

    def test_failure_without_an_exit_code(self) -> None:
        self.service.get_job.return_value = _job("FAILED")
        self.service.list_tasks.return_value = iter(
            [_task(("FAILED", 9, None))]
        )
        status = self.client.job_status(JOB)
        self.assertEqual(
            (status.state, status.exit_code, status.reason),
            (JobState.FAILED, None, "batch_failed"),
        )

    def test_missing_job_is_unknown(self) -> None:
        self.service.get_job.side_effect = gexc.NotFound("gone")
        status = self.client.job_status(JOB)
        self.assertEqual(
            (status.state, status.reason), (JobState.UNKNOWN, "job_not_found")
        )

    def test_wait_job(self) -> None:
        self.service.get_job.side_effect = [
            _job("SCHEDULED"),
            gexc.ServiceUnavailable("blip"),
            _job("SUCCEEDED"),
        ]
        self.service.list_tasks.return_value = iter(
            [_task(("SUCCEEDED", 9, 0))]
        )
        with mock.patch("bespokelabs.sandbox._jobs.time.sleep"):
            status = self.client.wait_job(JOB, timeout_secs=60, poll_secs=1)
        self.assertEqual(status.state, JobState.SUCCEEDED)

    def test_rejects_non_batch_ids(self) -> None:
        with self.assertRaises(SandboxConfigurationError):
            self.client.job_status("local:abc")


class CancelTests(GcpBatchTestCase):

    def test_cancel_uses_cancel_job_and_keeps_the_record(self) -> None:
        self.service.get_job.return_value = _job("RUNNING")
        self.service.list_tasks.return_value = iter([_task()])
        status = self.client.cancel_job(JOB)
        self.assertEqual(
            (status.state, status.reason), (JobState.CANCELLED, "cancelled")
        )
        self.service.cancel_job.assert_called_once_with(name=JOB)
        self.service.delete_job.assert_not_called()

        # The record survives, so a later read says cancelled, not gone.
        self.service.get_job.return_value = _job("CANCELLATION_IN_PROGRESS")
        self.service.list_tasks.return_value = iter([_task()])
        self.assertEqual(self.client.job_status(JOB).state, JobState.RUNNING)
        self.service.get_job.return_value = _job("CANCELLED")
        self.service.list_tasks.return_value = iter([_task()])
        later = self.client.job_status(JOB)
        self.assertEqual(
            (later.state, later.reason), (JobState.CANCELLED, "cancelled")
        )
        self.assertTrue(later.done)

    def test_cancel_in_progress_is_not_requested_twice(self) -> None:
        self.service.get_job.return_value = _job("CANCELLATION_IN_PROGRESS")
        self.service.list_tasks.return_value = iter([_task()])
        self.assertEqual(self.client.cancel_job(JOB).state, "cancelled")
        self.service.cancel_job.assert_not_called()
        self.service.delete_job.assert_not_called()

    def test_falls_back_to_delete_without_cancel_job_in_the_sdk(self) -> None:
        service = mock.MagicMock(spec=["get_job", "list_tasks", "delete_job"])
        service.get_job.return_value = _job("QUEUED")
        self.backend._client = service
        status = self.client.cancel_job(JOB)
        self.assertEqual(
            (status.state, status.reason),
            (JobState.CANCELLED, "cancelled_by_delete"),
        )
        service.delete_job.assert_called_once_with(name=JOB)

    def test_falls_back_to_delete_when_the_service_lacks_cancel(self) -> None:
        self.service.get_job.return_value = _job("QUEUED")
        self.service.cancel_job.side_effect = gexc.MethodNotImplemented("501")
        status = self.client.cancel_job(JOB)
        self.assertEqual(status.reason, "cancelled_by_delete")
        self.service.delete_job.assert_called_once_with(name=JOB)

    def test_cancel_errors_are_classified_not_swallowed(self) -> None:
        self.service.get_job.return_value = _job("QUEUED")
        self.service.cancel_job.side_effect = gexc.PermissionDenied("no")
        with self.assertRaises(SandboxAuthenticationError):
            self.client.cancel_job(JOB)
        self.service.delete_job.assert_not_called()

    def test_cancel_leaves_a_finished_job(self) -> None:
        self.service.get_job.return_value = _job("SUCCEEDED")
        self.service.list_tasks.return_value = iter([_task()])
        self.assertEqual(self.client.cancel_job(JOB).state, "succeeded")
        self.service.cancel_job.assert_not_called()
        self.service.delete_job.assert_not_called()

    def test_cancel_a_missing_job(self) -> None:
        self.service.get_job.side_effect = gexc.NotFound("gone")
        self.assertEqual(self.client.cancel_job(JOB).reason, "job_not_found")

    def test_job_vanishing_during_cancel(self) -> None:
        self.service.get_job.return_value = _job("QUEUED")
        self.service.cancel_job.side_effect = gexc.NotFound("gone")
        self.assertEqual(self.client.cancel_job(JOB).reason, "job_not_found")


class LogsTests(GcpBatchTestCase):

    def test_reads_cloud_logging_by_job_uid(self) -> None:
        self.service.get_job.return_value = _job("RUNNING", uid="u-42")
        logging_client = mock.MagicMock()
        logging_client.list_entries.return_value = [
            mock.Mock(payload="third"),
            mock.Mock(payload="second\n"),
        ]
        self.backend._logging_clients["proj"] = logging_client

        self.assertEqual(self.client.job_logs(JOB, tail=2), "second\nthird\n")
        kwargs = logging_client.list_entries.call_args.kwargs
        self.assertIn('labels.job_uid="u-42"', kwargs["filter_"])
        self.assertIn("projects/proj/logs/batch_task_logs", kwargs["filter_"])
        self.assertEqual(kwargs["max_results"], 2)
        self.assertEqual(kwargs["order_by"], "timestamp desc")

        logging_client.list_entries.return_value = [mock.Mock(payload="a")]
        self.assertEqual(self.client.job_logs(JOB), "a\n")
        self.assertIsNone(
            logging_client.list_entries.call_args.kwargs["order_by"]
        )


class InstallTests(unittest.TestCase):

    def test_missing_sdk_names_the_extra(self) -> None:
        with mock.patch.dict("sys.modules", {"google.cloud": None}):
            from bespokelabs.sandbox.backends.gcp_batch import (
                GcpBatchClient as Client,
            )

            with self.assertRaises(BackendNotInstalledError) as ctx:
                Client()
        self.assertIn("gcp-batch", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
