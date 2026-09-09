import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from k8s_cost_agent.recommendation.gemini import (
    WorkloadTools,
    _function_response_part,
    _generate_content_with_retry,
    _is_invalid_api_key_error,
    _is_model_availability_error,
    _is_rate_limit_error,
    _model_candidates,
    _parse_batch_recommendations,
    _parse_recommendation,
    _prompt_for_workloads,
    generate_recommendations,
    recommend_workloads,
)
from tests.unit.helpers import PROMPT_TEMPLATE, SETTINGS


class FakeResponse:
    """Small fake matching the Gemini response attribute used by the agent."""

    text = json.dumps(
        {
            "recommended_cpu": 0.1,
            "recommended_memory": 100000000,
            "confidence": "high",
            "reasoning": "The workload has low stable usage.",
        }
    )
    function_calls = []


def workload(name="demo-workload"):
    """Return one safe Phase 3-shaped workload record."""
    return {
        "name": name,
        "requested_cpu_cores": 0.4,
        "requested_memory_bytes": 512 * 1024**2,
        "percentile_rank": 95.0,
        "percentile": {
            "current_cpu_usage_cores": 0.08,
            "current_memory_usage_bytes": 100 * 1024**2,
        },
        "percentile_to_avg_ratio": {
            "current_cpu_usage_cores": 1.1,
            "current_memory_usage_bytes": 1.1,
        },
        "restart_count": 0,
        "last_restart_oomkilled": False,
        "criticality": "tier-2",
    }


class GeminiTests(unittest.TestCase):
    def test_tools_return_data_and_record_trace(self):
        tools = WorkloadTools([workload()])

        self.assertEqual(tools.get_criticality("demo-workload"), {"criticality": "tier-2"})
        self.assertEqual(tools.get_incident_history("demo-workload")["restart_count"], 0)
        self.assertEqual(tools.get_workload_stats("demo-workload")["name"], "demo-workload")
        self.assertEqual(len(tools.trace), 3)

    def test_function_response_preserves_parallel_call_id(self):
        """Gemini must be able to match each result to its parallel call."""
        response_part = SimpleNamespace(
            function_response=SimpleNamespace(id=None)
        )
        fake_types = SimpleNamespace(
            Part=SimpleNamespace(
                from_function_response=lambda **_kwargs: response_part
            )
        )
        function_call = SimpleNamespace(name="get_criticality", id="call-123")

        result = _function_response_part(
            fake_types, function_call, {"criticality": "tier-2"}
        )

        self.assertEqual(result.function_response.id, "call-123")

    def test_parse_rejects_incomplete_model_output(self):
        with self.assertRaises(ValueError):
            _parse_recommendation('{"recommended_cpu": 0.1}')

    def test_parse_accepts_json_code_fences(self):
        result = _parse_recommendation(
            '```json\n{"recommended_cpu": 0.1, "recommended_memory": 100, '
            '"confidence": "low", "reasoning": "test"}\n```'
        )
        self.assertEqual(result["recommended_cpu"], 0.1)

    def test_parse_rejects_empty_model_output(self):
        with self.assertRaisesRegex(ValueError, "empty recommendation"):
            _parse_recommendation("")

    def test_batch_parser_preserves_expected_workload_order(self):
        payload = {
            "recommendations": [
                {"workload": "second", **json.loads(FakeResponse.text)},
                {"workload": "first", **json.loads(FakeResponse.text)},
            ]
        }

        results = _parse_batch_recommendations(
            json.dumps(payload), ["first", "second"]
        )

        self.assertEqual(len(results), 2)

    def test_prompt_values_are_injected_into_external_template(self):
        prompt = _prompt_for_workloads(["first", "second"], PROMPT_TEMPLATE)

        self.assertIn('["first", "second"]', prompt)
        self.assertIn("6 read-only lookups", prompt)

    @patch("k8s_cost_agent.recommendation.gemini._model_response")
    def test_batch_proposals_all_pass_through_guardrails(self, model_response):
        records = [workload("first"), workload("second")]
        tools = WorkloadTools(records)
        for name in ("first", "second"):
            tools.get_workload_stats(name)
            tools.get_incident_history(name)
            tools.get_criticality(name)
        model_response.return_value = json.dumps(
            {
                "recommendations": [
                    {"workload": name, **json.loads(FakeResponse.text)}
                    for name in ("first", "second")
                ]
            }
        )

        results = recommend_workloads(
            object(),
            tools,
            ["first", "second"],
            "fake-model",
            SETTINGS.gemini,
            SETTINGS.guardrails,
            PROMPT_TEMPLATE,
        )

        self.assertEqual(
            [result["workload"] for result in results], ["first", "second"]
        )
        self.assertTrue(all(result["guardrails"]["cpu"]["approved"] for result in results))
        self.assertTrue(all(len(result["tool_trace"]) == 3 for result in results))

    def test_batch_conversation_uses_two_api_requests(self):
        """Parallel tool calls plus final JSON should fit within two requests."""
        records = [workload("first"), workload("second")]
        function_calls = [
            SimpleNamespace(
                name=tool_name,
                args={"name": name},
                id=f"{name}-{tool_name}",
            )
            for name in ("first", "second")
            for tool_name in (
                "get_workload_stats",
                "get_incident_history",
                "get_criticality",
            )
        ]
        tool_response = SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(
                        parts=[
                            SimpleNamespace(function_call=call)
                            for call in function_calls
                        ]
                    )
                )
            ]
        )
        final_json = json.dumps(
            {
                "recommendations": [
                    {"workload": name, **json.loads(FakeResponse.text)}
                    for name in ("first", "second")
                ]
            }
        )
        final_response = SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(
                        parts=[SimpleNamespace(text=final_json)]
                    )
                )
            ]
        )
        generate_content = Mock(side_effect=[tool_response, final_response])

        class BatchFakeTypes:
            class GenerateContentConfig:
                def __init__(self, **kwargs):
                    self.values = kwargs

            class Part:
                @staticmethod
                def from_text(*, text):
                    return SimpleNamespace(text=text)

                @staticmethod
                def from_function_response(**_kwargs):
                    return SimpleNamespace(
                        function_response=SimpleNamespace(id=None)
                    )

            class Content:
                def __init__(self, role, parts):
                    self.role = role
                    self.parts = parts

        model = {
            "client": SimpleNamespace(
                models=SimpleNamespace(generate_content=generate_content)
            ),
            "model_name": "fake-model",
            "tool": object(),
            "types": BatchFakeTypes,
            "thinking_config": "minimal-thinking",
        }

        results = recommend_workloads(
            model,
            WorkloadTools(records),
            ["first", "second"],
            "fake-model",
            SETTINGS.gemini,
            SETTINGS.guardrails,
            PROMPT_TEMPLATE,
        )

        self.assertEqual(generate_content.call_count, 2)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(len(result["tool_trace"]) == 3 for result in results))

    @patch("k8s_cost_agent.recommendation.gemini._model_response")
    def test_guardrails_reject_unsafe_model_proposal(self, model_response):
        tools = WorkloadTools([workload()])
        tools.get_workload_stats("demo-workload")
        tools.get_incident_history("demo-workload")
        tools.get_criticality("demo-workload")
        model_response.return_value = json.dumps(
            {
                "recommendations": [
                    {
                        "workload": "demo-workload",
                        "recommended_cpu": 0.01,
                        "recommended_memory": 2 * 1024**3,
                        "confidence": "high",
                        "reasoning": "Unsafe test proposal.",
                    }
                ]
            }
        )

        result = recommend_workloads(
            object(),
            tools,
            ["demo-workload"],
            "fake-model",
            SETTINGS.gemini,
            SETTINGS.guardrails,
            PROMPT_TEMPLATE,
        )[0]

        self.assertFalse(result["guardrails"]["cpu"]["approved"])
        self.assertFalse(result["guardrails"]["memory"]["approved"])

    def test_model_candidates_include_fallbacks_without_duplicates(self):
        """Configured model is tried first, followed by unique fallbacks."""
        candidates = _model_candidates(
            "gemini-2.5-flash", SETTINGS.gemini.fallback_models
        )
        self.assertEqual(candidates[0], "gemini-2.5-flash")
        self.assertEqual(len(candidates), len(set(candidates)))

    def test_only_model_availability_errors_trigger_fallback(self):
        """Authentication and quota errors must not be silently retried."""
        self.assertTrue(_is_model_availability_error(Exception("model not found")))
        self.assertFalse(_is_model_availability_error(Exception("quota exceeded")))

    def test_invalid_api_key_error_is_identified(self):
        """The SDK's invalid-key reason should produce credential guidance."""
        self.assertTrue(
            _is_invalid_api_key_error(Exception("reason: API_KEY_INVALID"))
        )
        self.assertFalse(_is_invalid_api_key_error(Exception("quota exceeded")))

    def test_rate_limit_error_is_identified(self):
        error = RuntimeError("429 RESOURCE_EXHAUSTED")
        self.assertTrue(_is_rate_limit_error(error))
        self.assertFalse(_is_rate_limit_error(Exception("model not found")))

    @patch(
        "k8s_cost_agent.recommendation.gemini._recommend_batch_with_fallback"
    )
    def test_generate_recommendations_uses_one_batch(self, recommend):
        """The whole stats file should use one quota-efficient conversation."""
        records = [{"name": "first"}, {"name": "second"}]
        recommend.return_value = [
            {"workload": "first"},
            {"workload": "second"},
        ]
        results = generate_recommendations(
            records,
            "fake-key",
            SETTINGS.gemini,
            SETTINGS.guardrails,
            PROMPT_TEMPLATE,
        )

        self.assertEqual(
            [result["workload"] for result in results], ["first", "second"]
        )
        recommend.assert_called_once()
        self.assertEqual(recommend.call_args.args[0], records)

    def test_rate_limit_retry_honors_server_delay(self):
        """A transient 429 with RetryInfo should be retried visibly and once."""
        rate_limit_error = RuntimeError("RESOURCE_EXHAUSTED: retry in 0.1s")
        rate_limit_error.status_code = 429
        generate_content = Mock(
            side_effect=[rate_limit_error, FakeResponse()]
        )
        model = {
            "client": SimpleNamespace(
                models=SimpleNamespace(generate_content=generate_content)
            ),
            "model_name": "fake-model",
        }
        statuses = []

        with patch("k8s_cost_agent.recommendation.gemini.time.sleep") as sleep:
            result = _generate_content_with_retry(
                model,
                "prompt",
                object(),
                SETTINGS.gemini,
                statuses.append,
            )

        self.assertIsInstance(result, FakeResponse)
        self.assertEqual(generate_content.call_count, 2)
        sleep.assert_called_once_with(0.6)
        self.assertIn("rate limit", statuses[0].lower())


if __name__ == "__main__":
    unittest.main()
