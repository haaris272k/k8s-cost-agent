import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from k8s_cost_agent.analysis.guardrails import validate_recommendation
from tests.unit.helpers import SETTINGS


NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


def workload(**overrides):
    """Build a safe baseline workload and apply test-specific overrides."""
    result = {
        "name": "demo-workload",
        "requested_cpu_cores": 1.0,
        "requested_memory_bytes": 1024 * 1024**3,
        "percentile_rank": 95.0,
        "percentile": {
            "current_cpu_usage_cores": 0.4,
            "current_memory_usage_bytes": 200 * 1024**2,
        },
        "percentile_to_avg_ratio": {
            "current_cpu_usage_cores": 1.1,
            "current_memory_usage_bytes": 1.1,
        },
        "restart_count": 0,
        "last_restart_oomkilled": False,
        "criticality": "tier-2",
    }
    result.update(overrides)
    return result


class GuardrailTests(unittest.TestCase):
    def test_tier_zero_is_always_rejected(self):
        result = validate_recommendation(
            workload(criticality="tier-0"),
            "cpu",
            0.6,
            SETTINGS.guardrails,
            now=NOW,
        )
        self.assertFalse(result["approved"])
        self.assertEqual(result["reason"], "tier-0, manual review only")

    def test_recent_restart_is_rejected(self):
        result = validate_recommendation(
            workload(
                restart_count=1,
                last_restart_at=(NOW - timedelta(hours=2)).isoformat(),
            ),
            "cpu",
            0.6,
            SETTINGS.guardrails,
            now=NOW,
        )
        self.assertFalse(result["approved"])
        self.assertIn("recent restart", result["reason"])

    def test_old_oomkill_can_be_approved(self):
        result = validate_recommendation(
            workload(
                restart_count=1,
                last_restart_oomkilled=True,
                last_restart_at=(NOW - timedelta(hours=48)).isoformat(),
            ),
            "cpu",
            0.6,
            SETTINGS.guardrails,
            now=NOW,
        )
        self.assertTrue(result["approved"])

    def test_value_below_percentile_safety_floor_is_rejected(self):
        result = validate_recommendation(
            workload(), "cpu", 0.41, SETTINGS.guardrails, now=NOW
        )
        self.assertFalse(result["approved"])
        self.assertIn("safety floor", result["reason"])

    def test_value_above_current_request_is_rejected(self):
        result = validate_recommendation(
            workload(), "cpu", 1.1, SETTINGS.guardrails, now=NOW
        )
        self.assertFalse(result["approved"])
        self.assertIn("exceeds current request", result["reason"])

    def test_high_variance_is_approved_with_low_confidence(self):
        result = validate_recommendation(
            workload(
                percentile_to_avg_ratio={
                    "current_cpu_usage_cores": 2.0,
                    "current_memory_usage_bytes": 1.1,
                }
            ),
            "cpu",
            0.6,
            SETTINGS.guardrails,
            now=NOW,
        )
        self.assertTrue(result["approved"])
        self.assertEqual(result["confidence"], "low")

    def test_safety_margin_comes_from_configuration(self):
        stricter_policy = replace(SETTINGS.guardrails, safety_margin=2.0)

        result = validate_recommendation(
            workload(), "cpu", 0.6, stricter_policy, now=NOW
        )

        self.assertFalse(result["approved"])
        self.assertIn("safety floor", result["reason"])

    def test_v1_p95_artifacts_remain_supported(self):
        legacy = workload()
        legacy["p95"] = legacy.pop("percentile")
        legacy["p95_to_avg_ratio"] = legacy.pop(
            "percentile_to_avg_ratio"
        )

        result = validate_recommendation(
            legacy, "cpu", 0.6, SETTINGS.guardrails, now=NOW
        )

        self.assertTrue(result["approved"])


if __name__ == "__main__":
    unittest.main()
