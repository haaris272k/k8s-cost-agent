import unittest
from dataclasses import replace

from k8s_cost_agent.analysis.statistics import summarize_workload, trend
from tests.unit.helpers import SETTINGS


def samples(cpu_values, memory_values=None):
    """Build simple workload samples for the stats-engine tests."""
    memory_values = memory_values or cpu_values
    return [
        {
            "name": "demo-workload",
            "current_cpu_usage_cores": cpu,
            "current_memory_usage_bytes": memory,
        }
        for cpu, memory in zip(cpu_values, memory_values)
    ]


class StatisticsTests(unittest.TestCase):
    def test_steady_usage_has_ratio_near_one(self):
        """Consistent samples should have P95 approximately equal to average."""
        result = summarize_workload(
            samples([10, 10, 10, 10, 10]), SETTINGS.statistics
        )

        self.assertAlmostEqual(
            result["percentile_to_avg_ratio"]["current_cpu_usage_cores"], 1.0
        )

    def test_spiky_usage_has_ratio_above_one(self):
        """A high usage spike should make P95 meaningfully exceed average."""
        result = summarize_workload(
            samples([1, 1, 1, 1, 10]), SETTINGS.statistics
        )

        self.assertGreater(
            result["percentile_to_avg_ratio"]["current_cpu_usage_cores"], 1.5
        )

    def test_small_change_is_stable(self):
        """A change below the 15 percent threshold is not a trend."""
        self.assertEqual(
            trend(
                [100, 100, 110, 110],
                SETTINGS.statistics.trend_change_threshold,
                SETTINGS.statistics.minimum_trend_samples,
            ),
            "stable",
        )

    def test_large_change_is_increasing(self):
        """A change at or above the threshold is classified as increasing."""
        self.assertEqual(
            trend(
                [100, 100, 120, 120],
                SETTINGS.statistics.trend_change_threshold,
                SETTINGS.statistics.minimum_trend_samples,
            ),
            "increasing",
        )

    def test_trend_threshold_comes_from_configuration(self):
        sensitive = replace(
            SETTINGS.statistics, trend_change_threshold=0.05
        )

        result = trend(
            [100, 100, 110, 110],
            sensitive.trend_change_threshold,
            sensitive.minimum_trend_samples,
        )

        self.assertEqual(result, "increasing")


if __name__ == "__main__":
    unittest.main()
