import unittest
from datetime import datetime, timezone

from k8s_cost_agent.reporting.html import build_report_rows, render_html
from tests.unit.helpers import SETTINGS


GIBIBYTE = 1024**3


def stats_record(name, criticality="tier-2"):
    """Build one minimal Phase 3 record for report tests."""
    return {
        "name": name,
        "namespace": "cost-agent-demo",
        "criticality": criticality,
        "requested_cpu_cores": 1.0,
        "requested_memory_bytes": GIBIBYTE,
        "sample_count": 20,
        "restart_count": 0,
        "last_restart_oomkilled": False,
    }


def recommendation_record(
    name,
    approved_cpu=0.5,
    approved_memory=GIBIBYTE / 2,
    cpu_approved=True,
    memory_approved=True,
    reasoning="Stable usage supports a smaller request.",
):
    """Build one minimal guarded Phase 5 result for report tests."""
    return {
        "workload": name,
        "recommendation": {
            "recommended_cpu": 0.2,
            "recommended_memory": GIBIBYTE / 4,
            "confidence": "high",
            "reasoning": reasoning,
        },
        "guardrails": {
            "cpu": {
                "approved": cpu_approved,
                "approved_value": approved_cpu,
                "reason": "passed deterministic safety checks"
                if cpu_approved
                else "manual review only",
                "confidence": "normal",
            },
            "memory": {
                "approved": memory_approved,
                "approved_value": approved_memory,
                "reason": "passed deterministic safety checks"
                if memory_approved
                else "manual review only",
                "confidence": "normal",
            },
        },
        "tool_trace": [
            {
                "tool": "get_workload_stats",
                "arguments": {"name": name},
                "result": {"name": name},
            }
        ],
        "model": "fake-model",
    }


class HtmlReportTests(unittest.TestCase):
    def test_rows_are_sorted_by_approved_savings(self):
        stats = [stats_record("blocked", "tier-0"), stats_record("approved")]
        recommendations = [
            recommendation_record(
                "blocked",
                approved_cpu=1.0,
                approved_memory=GIBIBYTE,
                cpu_approved=False,
                memory_approved=False,
            ),
            recommendation_record(
                "approved", approved_cpu=0.25, approved_memory=GIBIBYTE / 4
            ),
        ]

        rows = build_report_rows(stats, recommendations)

        self.assertEqual([row["name"] for row in rows], ["approved", "blocked"])
        self.assertEqual(rows[0]["savings_score"], 75.0)
        self.assertEqual(rows[1]["savings_score"], 0.0)
        self.assertEqual(rows[1]["effective_confidence"], "manual review")

    def test_rejected_raw_proposal_is_not_counted_as_savings(self):
        rows = build_report_rows(
            [stats_record("blocked", "tier-0")],
            [
                recommendation_record(
                    "blocked",
                    # Even inconsistent edited input cannot turn a rejection
                    # into reportable savings.
                    approved_cpu=0.1,
                    approved_memory=GIBIBYTE / 4,
                    cpu_approved=False,
                    memory_approved=False,
                )
            ],
        )

        self.assertEqual(rows[0]["proposed_cpu"], 0.2)
        self.assertEqual(rows[0]["approved_cpu"], 1.0)
        self.assertEqual(rows[0]["cpu_savings_percent"], 0.0)

    def test_approved_growth_is_rejected_as_invalid_pipeline_output(self):
        with self.assertRaisesRegex(ValueError, "cannot grow requests"):
            build_report_rows(
                [stats_record("invalid")],
                [recommendation_record("invalid", approved_cpu=1.1)],
            )

    def test_mismatched_workloads_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "different workloads"):
            build_report_rows(
                [stats_record("stats-only")],
                [recommendation_record("recommendation-only")],
            )

    def test_html_escapes_model_generated_content(self):
        rows = build_report_rows(
            [stats_record("demo")],
            [recommendation_record("demo", reasoning="<script>alert(1)</script>")],
        )

        output = render_html(
            rows,
            SETTINGS.report,
            datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc),
        )

        self.assertNotIn("<script>alert(1)</script>", output)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", output)
        self.assertIn("2026-09-08 12:00 UTC", output)
        self.assertIn("LLM proposed", output)
        self.assertIn(SETTINGS.report.title, output)

    def test_rendered_report_is_a_standalone_html_document(self):
        rows = build_report_rows(
            [stats_record("demo")], [recommendation_record("demo")]
        )

        output = render_html(rows, SETTINGS.report)

        self.assertIn("<!doctype html>", output)
        self.assertNotIn("<link ", output)
        self.assertNotIn("<script", output)


if __name__ == "__main__":
    unittest.main()
