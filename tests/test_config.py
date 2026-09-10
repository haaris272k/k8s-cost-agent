"""Tests for TOML path resolution and secret lookup behavior."""

import unittest
from pathlib import Path

from k8s_cost_agent.config import load_settings, read_api_key


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def test_default_configuration_loads_and_resolves_project_paths(self):
        settings = load_settings(PROJECT_ROOT / "config/settings.toml")

        self.assertEqual(settings.paths.base_dir, PROJECT_ROOT)
        self.assertEqual(
            settings.paths.samples, PROJECT_ROOT / "artifacts/samples.json"
        )
        self.assertEqual(settings.guardrails.safety_margin, 1.1)
        self.assertEqual(settings.gemini.thinking_level, "minimal")

    def test_api_key_uses_configured_environment_variable(self):
        settings = load_settings(PROJECT_ROOT / "config/settings.toml")

        self.assertEqual(
            read_api_key(settings, {"GEMINI_API_KEY": "test-value"}),
            "test-value",
        )

    def test_api_key_error_does_not_include_a_secret(self):
        settings = load_settings(PROJECT_ROOT / "config/settings.toml")

        with self.assertRaisesRegex(ValueError, "GEMINI_API_KEY"):
            read_api_key(settings, {})


if __name__ == "__main__":
    unittest.main()
