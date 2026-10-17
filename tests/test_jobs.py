"""Detached job API tests.

The local-backend cases run real detached processes on the host; every other
backend is faked.  No test touches the network or a provider account.
"""

from __future__ import annotations

import asyncio
import shutil
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

from bespokelabs.sandbox import (
    AsyncSandboxClient,
    JobSpec,
    JobState,
    JobStatus,
    SandboxClient,
    _jobs,
    job_backend,
)
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
from bespokelabs.sandbox.types import SandboxResult

_HAS_SH = shutil.which("sh") is not None


def _wait(client: SandboxClient, job_id: str, **kwargs: object) -> JobStatus:
    return client.wait_job(job_id, timeout_secs=30, poll_secs=0.1, **kwargs)


class JobSpecTests(unittest.TestCase):

    def test_defaults(self) -> None:
        spec = JobSpec(command=["echo", "hi"])
        self.assertEqual(spec.timeout_secs, 3600)
        self.assertEqual(spec.cpu, 1.0)
        self.assertEqual(spec.memory_mb, 1024)
        self.assertEqual(spec.env_vars, {})

    def test_rejects_empty_or_malformed_commands(self) -> None:
        for command in ("", "   ", [], ["ok", 3], 42):
            with self.subTest(command=command):
                with self.assertRaises(SandboxConfigurationError):
                    JobSpec(command=command)  # type: ignore[arg-type]

    def test_tuple_command_becomes_a_list(self) -> None:
        self.assertEqual(JobSpec(command=("a", "b")).command, ["a", "b"])

    def test_rejects_image_and_snapshot_together(self) -> None:
        with self.assertRaises(SandboxConfigurationError):
            JobSpec(command="true", image="python:3.12", snapshot_id="snap")

    def test_rejects_non_positive_resources(self) -> None:
        for field in ("cpu", "memory_mb", "timeout_secs", "disk_mb"):
            with self.subTest(field=field):
                with self.assertRaises(SandboxConfigurationError):
                    JobSpec(command="true", **{field: 0})

    def test_rejects_env_names_the_shell_cannot_export(self) -> None:
        for name in ("1A", "A-B", "A B", ""):
            with self.subTest(name=name):
                with self.assertRaises(SandboxConfigurationError):
                    JobSpec(command="true", env_vars={name: "x"})
        with self.assertRaises(SandboxConfigurationError):
            JobSpec(command="true", env_vars={"A": 1})  # type: ignore[dict-item]

    def test_status_done(self) -> None:
        for state in JobState:
            status = JobStatus(job_id="j", state=state)
            self.assertEqual(
                status.done,
                state not in (JobState.PENDING, JobState.RUNNING),
            )


class JobIdTests(unittest.TestCase):

    def test_round_trip(self) -> None:
        payload = {"v": 1, "state": {"sandbox_id": "sb-1"}, "dir": "/tmp/x"}
        job_id = _jobs.encode_job_id("daytona", payload)
        self.assertTrue(job_id.startswith("daytona:"))
        self.assertEqual(_jobs.decode_job_id(job_id), ("daytona", payload))
        self.assertEqual(job_backend(job_id), "daytona")

    def test_gcp_batch_resource_names(self) -> None:
        self.assertEqual(
            job_backend("projects/p/locations/us-central1/jobs/job-1"),
            "gcp_batch",
        )

    def test_malformed_ids(self) -> None:
        bad = [
            "",
            "no-colon",
            "Docker:abc",
            "docker:!!!",
            _jobs.encode_job_id("docker", {"v": 99, "state": {}, "dir": "d"}),
            _jobs.encode_job_id("docker", {"v": 1, "dir": "d"}),
        ]
        for job_id in bad:
            with self.subTest(job_id=job_id):
                with self.assertRaises(SandboxConfigurationError):
                    job_backend(job_id)

    def test_client_rejects_another_backends_id(self) -> None:
        job_id = _jobs.encode_job_id(
            "docker", {"v": 1, "state": {"container_id": "c"}, "dir": "d"}
        )
        with self.assertRaises(SandboxConfigurationError):
            SandboxClient("local").job_status(job_id)


class ProviderVerdictTests(unittest.TestCase):

    def test_not_found_anywhere_in_the_chain(self) -> None:
        class DaytonaNotFoundError(Exception):
            pass

        try:
            try:
                raise DaytonaNotFoundError("404")
            except DaytonaNotFoundError as inner:
                raise SandboxExecutionError("wrapped") from inner
        except SandboxExecutionError as exc:
            self.assertTrue(_jobs.is_gone(exc))

    def test_status_code_404_and_modal_termination(self) -> None:
        err = RuntimeError("gone")
        err.status_code = 404  # type: ignore[attr-defined]
        self.assertTrue(_jobs.is_gone(err))

        class SandboxTerminatedError(Exception):
            pass

        self.assertTrue(_jobs.is_gone(SandboxTerminatedError()))
        self.assertTrue(_jobs.is_gone(SandboxNotFoundError("x")))

    def test_transport_noise_is_not_a_verdict(self) -> None:
        err = RuntimeError("bad gateway")
        err.status_code = 502  # type: ignore[attr-defined]
        self.assertFalse(_jobs.is_gone(err))
        self.assertFalse(_jobs.is_gone(TimeoutError()))
        self.assertFalse(_jobs.is_gone(SandboxConnectionError("reset")))


class StatusMappingTests(unittest.TestCase):

    def _status(self, **facts: str) -> JobStatus:
        return _jobs._status_from_facts("j", facts, {"backend": "x"})

    def test_exit_codes(self) -> None:
        ok = self._status(
            alive="0",
            pid="10",
            **{"exit-code": "0", "started-at": "100", "finished-at": "160"},
        )
        self.assertEqual(ok.state, JobState.SUCCEEDED)
        self.assertEqual(ok.exit_code, 0)
        self.assertEqual(
            ok.started_at, datetime.fromtimestamp(100, tz=timezone.utc)
        )
        self.assertEqual(
            ok.finished_at, datetime.fromtimestamp(160, tz=timezone.utc)
        )
        failed = self._status(alive="0", pid="10", **{"exit-code": "3"})
        self.assertEqual(
            (failed.state, failed.exit_code, failed.reason),
            (JobState.FAILED, 3, "exit_code"),
        )
        timed_out = self._status(
            alive="0", pid="10", **{"exit-code": "124", "timed-out": ""}
        )
        self.assertEqual(timed_out.reason, "timeout")

    def test_a_recorded_exit_code_wins_over_liveness(self) -> None:
        # A zombie launcher (or a launcher about to exit) still answers kill -0.
        status = self._status(alive="1", pid="10", **{"exit-code": "0"})
        self.assertEqual(status.state, JobState.SUCCEEDED)

    def test_running_pending_lost_and_missing(self) -> None:
        self.assertEqual(
            self._status(alive="1", pid="10").state, JobState.RUNNING
        )
        self.assertEqual(self._status(alive="0").state, JobState.PENDING)
        lost = self._status(alive="0", pid="10")
        self.assertEqual(
            (lost.state, lost.reason), (JobState.UNKNOWN, "process_lost")
        )
        missing = self._status(missing="1")
        self.assertEqual(
            (missing.state, missing.reason),
            (JobState.UNKNOWN, "job_files_missing"),
        )

    def test_torn_exit_code_reads_as_not_yet(self) -> None:
        status = self._status(alive="1", pid="10", **{"exit-code": "1x"})
        self.assertEqual(status.state, JobState.RUNNING)

    def test_cancel_marker(self) -> None:
        self.assertEqual(
            self._status(alive="0", pid="1", cancelled="").state,
            JobState.CANCELLED,
        )
        killed = self._status(
            alive="0", pid="1", cancelled="", **{"exit-code": "143"}
        )
        self.assertEqual(killed.state, JobState.CANCELLED)
        finished = self._status(
            alive="0", pid="1", cancelled="", **{"exit-code": "0"}
        )
        self.assertEqual(finished.state, JobState.SUCCEEDED)


# -- Fake provider -------------------------------------------------------------


class _FakeSession:
    """A provider session that answers job control scripts from a table."""

    def __init__(self, sandbox_id: str, probe: str = "") -> None:
        self.sandbox_id = sandbox_id
        self.probe = probe or "alive=1\npid=42\nprobe=ok\n"
        self.commands: list[str] = []
        self.files: dict[str, bytes | str] = {}
        self.destroyed = False
        self.exec_error: Exception | None = None

    @property
    def provider_resource_id(self) -> str:
        return self.sandbox_id

    def execute_command(
        self, command: str, args: list[str] | None = None
    ) -> SandboxResult:
        del args
        if self.exec_error is not None:
            raise self.exec_error
        self.commands.append(command)
        if "submitted=" in command:
            return SandboxResult(stdout="submitted=42\n")
        if "probe=ok" in command:
            return SandboxResult(stdout=self.probe)
        if "tail -n" in command or "then cat" in command:
            return SandboxResult(stdout="line\n")
        return SandboxResult()

    def write_file(self, path: str, content: bytes | str) -> None:
        self.files[path] = content

    def session_state(self) -> dict:
        return {"sandbox_id": self.sandbox_id}

    def destroy(self) -> None:
        self.destroyed = True


class _FakeBackend:

    def __init__(self) -> None:
        self.configs: list[object] = []
        self.sessions: dict[str, _FakeSession] = {}
        self.resume_error: Exception | None = None

    def create(self, config: object) -> _FakeSession:
        self.configs.append(config)
        session = _FakeSession(f"sb-{len(self.configs)}")
        self.sessions[session.sandbox_id] = session
        return session

    def resume(self, data: dict) -> _FakeSession:
        if self.resume_error is not None:
            raise self.resume_error
        return self.sessions[data["sandbox_id"]]


def _client(name: str) -> tuple[SandboxClient, _FakeBackend]:
    backend = _FakeBackend()
    with mock.patch.dict(
        "bespokelabs.sandbox.backends.BACKENDS",
        {name: mock.Mock(return_value=backend)},
    ):
        return SandboxClient(name), backend


class GenericRunnerTests(unittest.TestCase):

    def test_submit_creates_launches_and_encodes_a_reattachable_id(
        self,
    ) -> None:
        client, backend = _client("docker")
        job_id = client.submit_job(
            JobSpec(
                command=["python", "train.py"],
                image="python:3.12-slim",
                env_vars={"TOKEN": "it's secret"},
                cpu=2,
                memory_mb=4096,
                timeout_secs=600,
                labels={"team": "evals"},
                files={"/work/train.py": "print(1)"},
                workdir="/work",
            )
        )

        config = backend.configs[0]
        self.assertEqual(config.image, "python:3.12-slim")
        self.assertEqual((config.cpu, config.memory_mb), (2, 4096))
        # The sandbox outlives the command by an hour so outputs stay readable.
        self.assertEqual(config.timeout_secs, 600 + 3600)
        self.assertTrue(config.timeout_secs_explicit)
        self.assertEqual(config.backend_options["labels"], {"team": "evals"})
        # Env vars go to the command only, not the sandbox (or the job id).
        self.assertEqual(config.env_vars, {})

        session = backend.sessions["sb-1"]
        self.assertEqual(session.files["/work/train.py"], "print(1)")
        launch = next(v for k, v in session.files.items() if "launch.sh" in k)
        self.assertIn("export TOKEN='it'\"'\"'s secret'", launch)
        self.assertIn("cd /work", launch)
        self.assertIn("timeout 600 python train.py", launch)
        self.assertIn("nohup", session.commands[-1])

        _, payload = _jobs.decode_job_id(job_id)
        self.assertEqual(payload["state"], {"sandbox_id": "sb-1"})
        self.assertNotIn("secret", job_id)

        # A brand new client (a restarted worker) finds the job by id alone.
        fresh, _ = _client("docker")
        fresh._backend_client = backend
        status = fresh.job_status(job_id)
        self.assertEqual(status.state, JobState.RUNNING)
        self.assertEqual(status.details["sandbox_id"], "sb-1")

    def test_shell_string_commands_run_under_sh(self) -> None:
        client, backend = _client("docker")
        client.submit_job(JobSpec(command="echo $HOME | wc -c"))
        launch = next(
            v
            for k, v in backend.sessions["sb-1"].files.items()
            if "launch.sh" in k
        )
        self.assertIn("timeout 3600 sh -c 'echo $HOME | wc -c'", launch)

    def test_backend_label_keys_and_daytona_defaults(self) -> None:
        expected = {
            "daytona": "labels",
            "docker": "labels",
            "modal": "tags",
            "e2b": "metadata",
        }
        for name, key in expected.items():
            with self.subTest(backend=name):
                client, backend = _client(name)
                client.submit_job(
                    JobSpec(
                        command="true",
                        labels={"a": "1"},
                        backend_options={key: {"b": "2"}},
                    )
                )
                options = backend.configs[0].backend_options
                self.assertEqual(options[key], {"a": "1", "b": "2"})
        client, backend = _client("daytona")
        client.submit_job(JobSpec(command="true"))
        self.assertEqual(
            backend.configs[0].backend_options["auto_stop_interval"], 0
        )
        client, backend = _client("daytona")
        client.submit_job(
            JobSpec(command="true", backend_options={"auto_stop_interval": 30})
        )
        self.assertEqual(
            backend.configs[0].backend_options["auto_stop_interval"], 30
        )

    def test_modal_lifetime_is_capped_at_a_day(self) -> None:
        client, backend = _client("modal")
        client.submit_job(JobSpec(command="true", timeout_secs=23 * 3600))
        self.assertEqual(backend.configs[0].timeout_secs, 24 * 3600)
        with self.assertRaises(SandboxConfigurationError):
            client.submit_job(JobSpec(command="true", timeout_secs=25 * 3600))

    def test_unsupported_backends(self) -> None:
        for name in ("runpod", "ray"):
            with self.subTest(backend=name):
                client, _ = _client(name)
                with self.assertRaises(FeatureNotSupportedError):
                    client.submit_job(JobSpec(command="true"))

    def test_failed_launch_destroys_the_sandbox(self) -> None:
        client, backend = _client("docker")
        with mock.patch.object(
            _FakeSession,
            "write_file",
            side_effect=SandboxExecutionError("disk full"),
        ):
            with self.assertRaises(SandboxExecutionError):
                client.submit_job(JobSpec(command="true"))
        self.assertTrue(backend.sessions["sb-1"].destroyed)

    def test_unconfirmed_launch_destroys_the_sandbox(self) -> None:
        client, backend = _client("docker")
        with mock.patch.object(
            _FakeSession,
            "execute_command",
            return_value=SandboxResult(stdout=""),
        ):
            with self.assertRaises(SandboxExecutionError):
                client.submit_job(JobSpec(command="true"))
        self.assertTrue(backend.sessions["sb-1"].destroyed)

    def test_gone_sandbox_is_unknown_not_success(self) -> None:
        client, backend = _client("docker")
        job_id = client.submit_job(JobSpec(command="true"))

        class NotFound(Exception):
            pass

        try:
            raise SandboxCreationError("resume failed") from NotFound()
        except SandboxCreationError as exc:
            backend.resume_error = exc
        status = client.job_status(job_id)
        self.assertEqual(
            (status.state, status.reason), (JobState.UNKNOWN, "sandbox_gone")
        )
        self.assertTrue(status.done)
        # Cancelling a gone job is not an error.
        self.assertEqual(client.cancel_job(job_id).reason, "sandbox_gone")
        with self.assertRaises(SandboxNotFoundError):
            client.job_logs(job_id)

    def test_probe_not_found_is_gone_and_other_failures_raise(self) -> None:
        client, backend = _client("docker")
        job_id = client.submit_job(JobSpec(command="true"))
        session = backend.sessions["sb-1"]
        err = RuntimeError("404")
        err.status_code = 404  # type: ignore[attr-defined]
        session.exec_error = err
        self.assertEqual(client.job_status(job_id).reason, "sandbox_gone")

        session.exec_error = SandboxConnectionError("reset")
        with self.assertRaises(SandboxConnectionError):
            client.job_status(job_id)

        session.exec_error = None
        session.probe = "garbage"
        with self.assertRaises(SandboxConnectionError):
            client.job_status(job_id)

    def test_cancel_marks_kills_and_destroys(self) -> None:
        client, backend = _client("docker")
        job_id = client.submit_job(JobSpec(command="sleep 100"))
        status = client.cancel_job(job_id)
        session = backend.sessions["sb-1"]
        self.assertEqual(status.state, JobState.CANCELLED)
        self.assertTrue(any("cancelled" in c for c in session.commands))
        self.assertTrue(session.destroyed)

    def test_cancel_keeps_a_finished_jobs_state(self) -> None:
        client, backend = _client("docker")
        job_id = client.submit_job(JobSpec(command="true"))
        session = backend.sessions["sb-1"]
        session.probe = "alive=0\npid=42\nexit-code=0\nprobe=ok\n"
        self.assertEqual(client.cancel_job(job_id).state, JobState.SUCCEEDED)
        self.assertFalse(any("cancelled=1" in c for c in session.commands))
        self.assertTrue(session.destroyed)

    def test_logs_tail(self) -> None:
        client, backend = _client("docker")
        job_id = client.submit_job(JobSpec(command="true"))
        self.assertEqual(client.job_logs(job_id, tail=5), "line\n")
        self.assertIn("tail -n 5", backend.sessions["sb-1"].commands[-1])
        with self.assertRaises(SandboxConfigurationError):
            client.job_logs(job_id, tail=-1)

    def test_job_sandbox_reattaches(self) -> None:
        client, backend = _client("docker")
        job_id = client.submit_job(JobSpec(command="true"))
        sb = client.job_sandbox(job_id)
        self.assertEqual(sb.provider_resource_id, "sb-1")


class WaitPolicyTests(unittest.TestCase):

    def _wait(
        self,
        outcomes: list[object],
        *,
        timeout_secs: float | None = None,
    ) -> tuple[object, list[JobStatus]]:
        polled: list[JobStatus] = []
        queue = list(outcomes)

        def status(job_id: str) -> JobStatus:
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return JobStatus(job_id=job_id, state=item)

        with mock.patch.object(_jobs.time, "sleep"):
            result = _jobs.wait_sync(
                status,
                "job",
                timeout_secs=timeout_secs,
                poll_secs=1,
                on_poll=polled.append,
            )
        return result, polled

    def test_returns_terminal_status_and_reports_every_poll(self) -> None:
        result, polled = self._wait(
            [JobState.PENDING, JobState.RUNNING, JobState.SUCCEEDED]
        )
        self.assertEqual(result.state, JobState.SUCCEEDED)
        self.assertEqual(
            [p.state for p in polled],
            [JobState.PENDING, JobState.RUNNING, JobState.SUCCEEDED],
        )

    def test_unknown_is_terminal(self) -> None:
        result, _ = self._wait([JobState.RUNNING, JobState.UNKNOWN])
        self.assertEqual(result.state, JobState.UNKNOWN)

    def test_gives_up_after_five_failures_before_the_job_is_seen(self) -> None:
        errors = [SandboxConnectionError(f"502 #{i}") for i in range(5)]
        with self.assertRaises(SandboxConnectionError) as ctx:
            self._wait([*errors, JobState.SUCCEEDED])
        self.assertIn("#4", str(ctx.exception))

    def test_tolerates_four_failures_before_the_job_is_seen(self) -> None:
        errors = [SandboxConnectionError("502")] * 4
        result, _ = self._wait([*errors, JobState.SUCCEEDED])
        self.assertEqual(result.state, JobState.SUCCEEDED)

    def test_rides_out_failures_once_the_job_was_seen(self) -> None:
        errors = [SandboxConnectionError("502")] * 20
        result, _ = self._wait([JobState.RUNNING, *errors, JobState.FAILED])
        self.assertEqual(result.state, JobState.FAILED)

    def test_fatal_errors_raise_at_once(self) -> None:
        with self.assertRaises(SandboxAuthenticationError):
            self._wait(
                [JobState.RUNNING, SandboxAuthenticationError("bad key")]
            )

    def test_backoff_doubles_and_is_capped(self) -> None:
        waiter = _jobs._Waiter("job", None, poll_secs=10)
        waiter.observed(JobStatus(job_id="job", state=JobState.RUNNING))
        delays = [waiter.failed(SandboxConnectionError("x")) for _ in range(8)]
        self.assertEqual(delays[:4], [10, 20, 40, 60])
        self.assertTrue(all(d == 60 for d in delays[4:]))

    def test_deadline(self) -> None:
        with self.assertRaises(SandboxTimeoutError) as ctx:
            self._wait([JobState.RUNNING] * 3, timeout_secs=0)
        self.assertEqual(ctx.exception.context["state"], "running")


@unittest.skipUnless(_HAS_SH, "needs a POSIX shell")
class LocalJobTests(unittest.TestCase):
    """Real detached processes on the local backend."""

    def setUp(self) -> None:
        self.client = SandboxClient("local")
        self.jobs: list[str] = []

    def tearDown(self) -> None:
        for job_id in self.jobs:
            try:
                self.client.cancel_job(job_id)
            except Exception:
                pass

    def _submit(self, spec: JobSpec) -> str:
        job_id = self.client.submit_job(spec)
        self.jobs.append(job_id)
        return job_id

    def test_success_with_env_files_and_logs(self) -> None:
        job_id = self._submit(
            JobSpec(
                command="echo hello $NAME; cat seed.txt; echo oops >&2",
                env_vars={"NAME": "o'brien"},
                files={"seed.txt": "seeded\n"},
            )
        )
        self.assertEqual(job_backend(job_id), "local")
        # Reattach from a fresh client, as a restarted worker would.
        status = _wait(SandboxClient("local"), job_id)
        self.assertEqual((status.state, status.exit_code), ("succeeded", 0))
        self.assertIsNotNone(status.started_at)
        self.assertIsNotNone(status.finished_at)
        self.assertEqual(
            self.client.job_logs(job_id), "hello o'brien\nseeded\noops\n"
        )
        self.assertEqual(self.client.job_logs(job_id, tail=1), "oops\n")
        self.assertEqual(
            self.client.job_sandbox(job_id).read_file("/seed.txt"),
            b"seeded\n",
        )

    def test_failure_exit_code(self) -> None:
        job_id = self._submit(JobSpec(command=["sh", "-c", "exit 7"]))
        status = _wait(self.client, job_id)
        self.assertEqual(
            (status.state, status.exit_code, status.reason),
            (JobState.FAILED, 7, "exit_code"),
        )

    def test_timeout(self) -> None:
        job_id = self._submit(JobSpec(command=["sleep", "30"], timeout_secs=1))
        status = _wait(self.client, job_id)
        self.assertEqual((status.state, status.reason), ("failed", "timeout"))

    def test_cancel_then_gone(self) -> None:
        job_id = self._submit(JobSpec(command="sleep 30"))
        deadline = time.monotonic() + 10
        while self.client.job_status(job_id).state != JobState.RUNNING:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.1)
        self.assertEqual(self.client.cancel_job(job_id).state, "cancelled")
        # The sandbox (its host workdir) is gone: a clear terminal state.
        status = self.client.job_status(job_id)
        self.assertEqual(
            (status.state, status.reason), (JobState.UNKNOWN, "sandbox_gone")
        )


@unittest.skipUnless(_HAS_SH, "needs a POSIX shell")
class AsyncJobTests(unittest.IsolatedAsyncioTestCase):

    async def test_async_round_trip_with_coroutine_on_poll(self) -> None:
        client = AsyncSandboxClient("local")
        job_id = await client.submit_job(JobSpec(command="echo async"))
        seen: list[JobState] = []

        async def on_poll(status: JobStatus) -> None:
            seen.append(status.state)
            await asyncio.sleep(0)

        try:
            status = await client.wait_job(
                job_id, timeout_secs=30, poll_secs=0.1, on_poll=on_poll
            )
            self.assertEqual(status.state, JobState.SUCCEEDED)
            self.assertEqual(seen[-1], JobState.SUCCEEDED)
            self.assertEqual(await client.job_logs(job_id), "async\n")
            sb = await client.job_sandbox(job_id)
            self.assertTrue(sb.is_alive)
        finally:
            final = await client.cancel_job(job_id)
        self.assertEqual(final.state, JobState.SUCCEEDED)
        self.assertEqual((await client.job_status(job_id)).state, "unknown")


class ControlPlaneDefaultsTests(unittest.TestCase):

    def test_job_only_backends_are_not_enabled_by_default(self) -> None:
        from bespokelabs.sandbox.control_plane.service import ControlPlane

        plane = ControlPlane(store=mock.MagicMock())
        self.assertNotIn("gcp_batch", plane._allowed_backends)
        self.assertIn("local", plane._allowed_backends)


if __name__ == "__main__":
    unittest.main()
