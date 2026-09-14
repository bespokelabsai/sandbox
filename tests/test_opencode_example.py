from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bespokelabs.sandbox import AgentRunResult, Sandbox
from examples import opencode_glm


class OpenCodeExampleTests(unittest.TestCase):

    def test_default_workspace_survives_separate_invocations(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "opencode_glm"
            calls = []

            def create(backend, **kwargs):
                calls.append(kwargs)
                sandbox = Sandbox(backend, workdir=kwargs["workdir"])
                sandbox.run_agent = mock.Mock(return_value=AgentRunResult())
                return sandbox

            with (
                mock.patch.object(opencode_glm, "DEFAULT_WORKDIR", workspace),
                mock.patch.object(opencode_glm, "Sandbox", side_effect=create),
                mock.patch.dict("os.environ", {"ZHIPU_API_KEY": "test"}),
                mock.patch("builtins.print"),
            ):
                for flags in [[], ["--resume"]]:
                    with mock.patch("sys.argv", ["opencode_glm.py", *flags]):
                        with self.assertRaises(SystemExit) as error:
                            opencode_glm.main()
                        self.assertEqual(error.exception.code, 0)
                    self.assertTrue(workspace.is_dir())
                    marker = workspace / "session-marker"
                    if flags:
                        self.assertEqual(marker.read_text(), "preserved")
                    else:
                        marker.write_text("preserved")
            self.assertEqual(
                [call["workdir"] for call in calls], [str(workspace)] * 2
            )

    def test_explicit_workspace_overrides_default(self):
        with (
            mock.patch.object(opencode_glm, "Sandbox") as sandbox,
            mock.patch.dict("os.environ", {"ZHIPU_API_KEY": "test"}),
            mock.patch(
                "sys.argv",
                ["opencode_glm.py", "--workdir", "/custom/workspace"],
            ),
            mock.patch("builtins.print"),
        ):
            sandbox.return_value.__enter__.return_value.run_agent.return_value = (
                AgentRunResult()
            )
            with self.assertRaises(SystemExit):
                opencode_glm.main()
            self.assertEqual(
                sandbox.call_args.kwargs["workdir"], "/custom/workspace"
            )
