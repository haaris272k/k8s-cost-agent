import contextlib
import io
import unittest
from unittest.mock import patch

from k8s_cost_agent.cli import main
from tests.unit.helpers import SETTINGS


class CliTests(unittest.TestCase):
    def test_check_config_needs_no_live_services(self):
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            main(["--config", str(SETTINGS.source), "check-config"])

        self.assertIn("Configuration is valid", output.getvalue())

    @patch(
        "k8s_cost_agent.cli.recommend_stage",
        side_effect=Exception("API_KEY_INVALID"),
    )
    def test_invalid_api_key_has_concise_guidance(self, _recommend):
        with self.assertRaisesRegex(SystemExit, "rejected the configured API key"):
            main(["--config", str(SETTINGS.source), "recommend"])

    @patch(
        "k8s_cost_agent.cli.recommend_stage",
        side_effect=Exception("429 RESOURCE_EXHAUSTED"),
    )
    def test_exhausted_quota_has_concise_guidance(self, _recommend):
        with self.assertRaisesRegex(SystemExit, "quota is exhausted"):
            main(["--config", str(SETTINGS.source), "recommend"])


if __name__ == "__main__":
    unittest.main()
