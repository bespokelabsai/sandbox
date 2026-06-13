from __future__ import annotations

import unittest
from unittest import mock

from bespokelabs.sandbox import Sandbox
from bespokelabs.sandbox.presets import PRESETS, SandboxPreset, get_preset, register_preset
from bespokelabs.sandbox.types import SandboxResult


class PresetTests(unittest.TestCase):
    def test_codex_preset_is_registered_with_expected_defaults(self) -> None:
        preset = get_preset("codex")

        self.assertEqual(preset.description, "Sandbox with Codex CLI installed")
        self.assertEqual(preset.setup_commands, ["npm install -g @openai/codex"])
        self.assertEqual(preset.memory_mb, 2048)
        self.assertEqual(preset.timeout_secs, 1800)
        self.assertTrue(preset.allow_internet)

    def test_list_presets_includes_codex(self) -> None:
        presets = Sandbox.list_presets()

        self.assertIn("codex", presets)
        self.assertEqual(presets["codex"].setup_commands, ["npm install -g @openai/codex"])


# Prebuilt container image presets added to the roster. Each is backed by a
# Dockerfile under images/<slug>/ and a ghcr.io image pushed by CI.
NEW_IMAGE_PRESETS = [
    "node",
    "go",
    "rust",
    "java",
    "ruby",
    "php",
    "dotnet",
    "cpp",
    "r",
    "pytorch",
    "tensorflow",
    "huggingface",
    "nlp",
    "llm",
    "scientific",
    "scraping",
    "playwright",
    "selenium",
    "fastapi",
    "dataeng",
    "pdf",
    "image",
]


class NewImagePresetTests(unittest.TestCase):
    """The prebuilt-image presets should each be registered, retrievable,
    listed, described, and pinned to a ghcr.io preset image."""

    def test_catalog_grew_to_expected_size(self) -> None:
        # 8 original presets + 21 brand-new slugs (`node` was upgraded in
        # place rather than added) = 29 total.
        self.assertEqual(len(PRESETS), 29)

    def test_each_new_preset_is_registered_and_retrievable(self) -> None:
        for name in NEW_IMAGE_PRESETS:
            with self.subTest(preset=name):
                preset = get_preset(name)
                self.assertEqual(preset.name, name)

    def test_each_new_preset_appears_in_list_presets(self) -> None:
        presets = Sandbox.list_presets()
        for name in NEW_IMAGE_PRESETS:
            with self.subTest(preset=name):
                self.assertIn(name, presets)

    def test_each_new_preset_has_non_empty_description(self) -> None:
        for name in NEW_IMAGE_PRESETS:
            with self.subTest(preset=name):
                self.assertTrue(get_preset(name).description)

    def test_each_new_preset_has_pinned_ghcr_image(self) -> None:
        for name in NEW_IMAGE_PRESETS:
            with self.subTest(preset=name):
                preset = get_preset(name)
                self.assertEqual(
                    preset.image,
                    f"ghcr.io/bespokelabsai/sandbox/{name}:v1",
                )

    def test_each_new_preset_has_setup_commands_fallback(self) -> None:
        # Images pre-bake the tools, but setup_commands stay populated so the
        # presets also work on backends without image support (e.g. local).
        for name in NEW_IMAGE_PRESETS:
            with self.subTest(preset=name):
                self.assertTrue(get_preset(name).setup_commands)


class PresetImageResolutionTests(unittest.TestCase):
    """Verify how Sandbox(preset=..., backend=...) resolves the image field
    and decides whether to run setup_commands. Covers the OCI-vs-tensorlake
    split: preset.image targets docker/daytona/modal, preset.tensorlake_image
    targets tensorlake."""

    PRESET_NAME = "_test_dual_image"

    def setUp(self) -> None:
        register_preset(SandboxPreset(
            name=self.PRESET_NAME,
            description="dual-image preset for testing",
            image="ghcr.io/test/img:latest",
            tensorlake_image="tl-name",
            setup_commands=["echo hi"],
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
        # so Tensorlake falls back to running setup_commands.
        sb = Sandbox(backend="tensorlake", preset="claude-code")

        self.assertIsNone(sb._config.image)
        sb._session.execute_command.assert_called()

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


if __name__ == "__main__":
    unittest.main()
