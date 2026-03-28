from __future__ import annotations

import unittest

from bespokelabs.sandbox import Sandbox
from bespokelabs.sandbox.presets import get_preset


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


if __name__ == "__main__":
    unittest.main()
