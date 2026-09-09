import unittest
from unittest.mock import patch

from k8s_cost_agent.pipeline import run_pipeline
from tests.unit.helpers import SETTINGS


class PipelineTests(unittest.TestCase):
    @patch("k8s_cost_agent.pipeline.report_stage")
    @patch("k8s_cost_agent.pipeline.recommend_stage")
    @patch("k8s_cost_agent.pipeline.analyze_stage")
    @patch("k8s_cost_agent.pipeline.collect_stage")
    @patch("k8s_cost_agent.pipeline.read_api_key", return_value="test-key")
    def test_run_pipeline_executes_every_stage_with_one_command(
        self,
        read_key,
        collect,
        analyze,
        recommend,
        report,
    ):
        statuses = []
        analyze.return_value = [{"name": "first"}, {"name": "second"}]
        recommend.return_value = [
            {"workload": "first"},
            {"workload": "second"},
        ]

        result = run_pipeline(SETTINGS, statuses.append)

        read_key.assert_called_once_with(SETTINGS)
        collect.assert_called_once_with(SETTINGS, statuses.append)
        analyze.assert_called_once_with(SETTINGS, statuses.append)
        recommend.assert_called_once_with(
            SETTINGS, statuses.append, "test-key"
        )
        report.assert_called_once_with(SETTINGS, statuses.append)
        self.assertEqual(result["workloads"], 2)
        self.assertEqual(result["recommendations"], 2)
        self.assertEqual(len(statuses), 4)


if __name__ == "__main__":
    unittest.main()
