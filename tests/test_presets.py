from __future__ import annotations

import unittest
from unittest import mock

from bespokelabs.sandbox import Sandbox
from bespokelabs.sandbox.presets import PRESETS, SandboxPreset, get_preset, register_preset
from bespokelabs.sandbox.types import SandboxResult


BUILT_IN_PRESETS = {"claude-code", "codex"}


class PresetTests(unittest.TestCase):
    def test_builtin_presets_are_limited_to_agent_clis(self) -> None:
        self.assertEqual(set(PRESETS), BUILT_IN_PRESETS)

    def test_codex_preset_is_registered_with_expected_defaults(self) -> None:
        preset = get_preset("codex")

        self.assertEqual(preset.description, "Sandbox with Codex CLI installed")
        self.assertEqual(preset.image, "ghcr.io/bespokelabsai/sandbox/codex:v2")
        self.assertEqual(preset.setup_commands, ["npm install -g @openai/codex"])
        self.assertEqual(
            preset.backend_setup_commands["tensorlake"],
            [
                "mkdir -p $HOME/.npm-global && npm config set prefix $HOME/.npm-global && npm install -g @openai/codex",
            ],
        )
        self.assertEqual(preset.memory_mb, 2048)
        self.assertEqual(preset.timeout_secs, 1800)
        self.assertTrue(preset.allow_internet)

    def test_claude_code_preset_is_registered_with_expected_defaults(self) -> None:
        preset = get_preset("claude-code")

        self.assertEqual(preset.description, "Sandbox with Claude Code (Anthropic CLI) installed")
        self.assertEqual(preset.image, "ghcr.io/bespokelabsai/sandbox/claude-code:v2")
        self.assertEqual(preset.setup_commands, ["npm install -g @anthropic-ai/claude-code"])
        self.assertEqual(
            preset.backend_setup_commands["tensorlake"],
            [
                "mkdir -p $HOME/.npm-global && npm config set prefix $HOME/.npm-global && npm install -g @anthropic-ai/claude-code",
            ],
        )
        self.assertEqual(preset.memory_mb, 2048)
        self.assertEqual(preset.timeout_secs, 1800)
        self.assertTrue(preset.allow_internet)

    def test_list_presets_includes_only_builtin_agent_clis(self) -> None:
        presets = Sandbox.list_presets()

        self.assertEqual(set(presets), BUILT_IN_PRESETS)


class PresetImageResolutionTests(unittest.TestCase):
    """Verify how Sandbox(preset=..., backend=...) resolves the image field
    and decides whether to run setup_commands. Covers the OCI-vs-tensorlake
    split: preset.image targets docker/daytona/modal, preset.tensorlake_image
    targets tensorlake."""

    PRESET_NAME = "_test_dual_image"
    BACKEND_ONLY_PRESET_NAME = "_test_backend_only_setup"

    def setUp(self) -> None:
        register_preset(SandboxPreset(
            name=self.PRESET_NAME,
            description="dual-image preset for testing",
            image="ghcr.io/test/img:latest",
            tensorlake_image="tl-name",
            setup_commands=["echo hi"],
        ))
        register_preset(SandboxPreset(
            name=self.BACKEND_ONLY_PRESET_NAME,
            description="backend-only setup preset for testing",
            backend_setup_commands={
                "tensorlake": ["echo tensorlake"],
            },
        ))

        def _make_backend_client() -> mock.MagicMock:
            session = mock.MagicMock()
            session.execute_command.return_value = SandboxResult(
                stdout="", stderr="", exit_code=0,
            )
            client = mock.MagicMock()
            client.create.return_value = session
            return client

        self._backends_patch = mock.patch.dict(
            "bespokelabs.sandbox.backends.BACKENDS",
            {
                "docker": lambda: _make_backend_client(),
                "tensorlake": lambda: _make_backend_client(),
            },
            clear=False,
        )
        self._backends_patch.start()

    def tearDown(self) -> None:
        self._backends_patch.stop()
        PRESETS.pop(self.PRESET_NAME, None)
        PRESETS.pop(self.BACKEND_ONLY_PRESET_NAME, None)

    def test_docker_inherits_oci_image_and_skips_setup(self) -> None:
        sb = Sandbox(backend="docker", preset=self.PRESET_NAME)

        self.assertEqual(sb._config.image, "ghcr.io/test/img:latest")
        sb._session.execute_command.assert_not_called()

    def test_tensorlake_inherits_tensorlake_image_and_skips_setup(self) -> None:
        sb = Sandbox(backend="tensorlake", preset=self.PRESET_NAME)

        self.assertEqual(sb._config.image, "tl-name")
        sb._session.execute_command.assert_not_called()

    def test_tensorlake_without_tensorlake_image_runs_setup(self) -> None:
        # Built-in `claude-code` preset has image= set but no tensorlake_image,
        # so Tensorlake falls back to its backend-specific setup commands.
        sb = Sandbox(backend="tensorlake", preset="claude-code")

        self.assertIsNone(sb._config.image)
        sb._session.execute_command.assert_called_once_with(
            "mkdir -p $HOME/.npm-global && npm config set prefix $HOME/.npm-global && npm install -g @anthropic-ai/claude-code"
        )

    def test_backend_only_setup_commands_run(self) -> None:
        sb = Sandbox(backend="tensorlake", preset=self.BACKEND_ONLY_PRESET_NAME)

        sb._session.execute_command.assert_called_once_with("echo tensorlake")

    def test_explicit_image_override_on_tensorlake_runs_setup(self) -> None:
        # Explicit override doesn't match preset.tensorlake_image, so we
        # don't treat it as the preset's pre-baked image → setup runs.
        sb = Sandbox(
            backend="tensorlake",
            preset=self.PRESET_NAME,
            image="user-override",
        )

        self.assertEqual(sb._config.image, "user-override")
        sb._session.execute_command.assert_called()

    def test_tensorlake_git_repo_uses_relative_destination(self) -> None:
        sb = Sandbox(backend="tensorlake", git_repo="https://github.com/acme/project.git")

        sb._session.execute_command.assert_called_once_with(
            "git clone --depth 1 https://github.com/acme/project.git project",
            None,
        )


if __name__ == "__main__":
    unittest.main()
