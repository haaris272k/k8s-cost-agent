"""Gemini recommendation agent for Phase 5.

Gemini may inspect Phase 3 data through three read-only tools and propose CPU
and memory values. Every proposal is then passed through Phase 4 guardrails.
The model never writes to Kubernetes and never has the final say.
"""

import json
import re
import time
from dataclasses import dataclass
from string import Template
from typing import Any, Callable

from k8s_cost_agent.analysis.guardrails import validate_recommendation
from k8s_cost_agent.config import GeminiSettings, GuardrailSettings

RECOMMENDATION_FIELDS = {
    "recommended_cpu": {"type": "number"},
    "recommended_memory": {"type": "number"},
    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    "reasoning": {"type": "string"},
}
RECOMMENDATION_REQUIRED = [
    "recommended_cpu",
    "recommended_memory",
    "confidence",
    "reasoning",
]
BATCH_RECOMMENDATION_SCHEMA = {
    "type": "object",
    "properties": {
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "workload": {"type": "string"},
                    **RECOMMENDATION_FIELDS,
                },
                "required": ["workload", *RECOMMENDATION_REQUIRED],
            },
        }
    },
    "required": ["recommendations"],
}


@dataclass
class ToolTrace:
    """One read-only tool call made while analyzing a workload."""

    name: str
    arguments: dict[str, Any]
    result: Any

    def as_dict(self) -> dict[str, Any]:
        """Return the trace entry as JSON-compatible data."""
        return {
            "tool": self.name,
            "arguments": self.arguments,
            "result": self.result,
        }


class WorkloadTools:
    """Read-only tool implementations backed by Phase 3 JSON data."""

    def __init__(self, workload_stats: list[dict[str, Any]]):
        self.workload_by_name = {
            workload["name"]: workload for workload in workload_stats
        }
        self.trace: list[ToolTrace] = []

    def _get_workload(self, name: str) -> dict[str, Any]:
        """Find a workload or raise a clear tool error."""
        try:
            return self.workload_by_name[name]
        except KeyError as error:
            raise ValueError(f"Unknown workload: {name}") from error

    def get_workload_stats(self, name: str) -> dict[str, Any]:
        """Return the complete Phase 3 stats record for one workload."""
        result = self._get_workload(name)
        self.trace.append(ToolTrace("get_workload_stats", {"name": name}, result))
        return result

    def get_incident_history(self, name: str) -> dict[str, Any]:
        """Return restart and OOM information for one workload."""
        workload = self._get_workload(name)
        result = {
            "restart_count": workload.get("restart_count", 0),
            "last_restart_oomkilled": workload.get(
                "last_restart_oomkilled", False
            ),
            "last_restart_at": workload.get("last_restart_at"),
        }
        self.trace.append(ToolTrace("get_incident_history", {"name": name}, result))
        return result

    def get_criticality(self, name: str) -> dict[str, str]:
        """Return the manual criticality classification for one workload."""
        result = {"criticality": self._get_workload(name).get("criticality", "unknown")}
        self.trace.append(ToolTrace("get_criticality", {"name": name}, result))
        return result


@dataclass
class Recommendation:
    """The model proposal, guardrail decisions, and reasoning trace."""

    workload: str
    recommendation: dict[str, Any]
    cpu_validation: dict[str, Any]
    memory_validation: dict[str, Any]
    tool_trace: list[dict[str, Any]]
    model_name: str

    def as_dict(self) -> dict[str, Any]:
        """Return the complete Phase 5 result as JSON-compatible data."""
        return {
            "workload": self.workload,
            "recommendation": self.recommendation,
            "guardrails": {
                "cpu": self.cpu_validation,
                "memory": self.memory_validation,
            },
            "tool_trace": self.tool_trace,
            "model": self.model_name,
        }


def _tool_functions(tools: WorkloadTools) -> list[Callable[..., Any]]:
    """Return Gemini-callable wrappers for the three read-only tools."""
    return [tools.get_workload_stats, tools.get_incident_history, tools.get_criticality]


def _parse_recommendation(text: str) -> dict[str, Any]:
    """Parse and validate the model's structured JSON recommendation."""
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text[3:-3].strip()
        if text.startswith("json"):
            text = text[4:].strip()
    if not text:
        raise ValueError("Gemini returned an empty recommendation")
    try:
        recommendation = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError("Gemini did not return valid JSON") from error

    required = {
        "recommended_cpu",
        "recommended_memory",
        "confidence",
        "reasoning",
    }
    missing = required - recommendation.keys()
    if missing:
        raise ValueError(f"Gemini response is missing fields: {sorted(missing)}")
    if recommendation["confidence"] not in {"high", "medium", "low"}:
        raise ValueError("confidence must be high, medium, or low")
    if not isinstance(recommendation["reasoning"], str):
        raise ValueError("reasoning must be a string")
    return recommendation


def _parse_batch_recommendations(
    text: str, workload_names: list[str]
) -> list[dict[str, Any]]:
    """Parse one recommendation for every expected workload in input order."""
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text[3:-3].strip()
        if text.startswith("json"):
            text = text[4:].strip()
    if not text:
        raise ValueError("Gemini returned an empty recommendation batch")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError("Gemini did not return valid batch JSON") from error

    records = payload.get("recommendations") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise ValueError("Gemini batch response must contain a recommendations list")

    by_name = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("workload"), str):
            raise ValueError("Every batch recommendation must name its workload")
        name = record["workload"]
        if name in by_name:
            raise ValueError(f"Gemini returned duplicate workload: {name}")
        proposal = {
            field: record[field]
            for field in RECOMMENDATION_REQUIRED
            if field in record
        }
        by_name[name] = _parse_recommendation(json.dumps(proposal))

    expected = set(workload_names)
    actual = set(by_name)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise ValueError(
            "Gemini returned the wrong workload set "
            f"(missing={missing}, unexpected={unexpected})"
        )
    return [by_name[name] for name in workload_names]


def _prompt_for_workloads(
    workload_names: list[str], prompt_template: str
) -> str:
    """Create one quota-efficient prompt for every workload."""
    return Template(prompt_template).substitute(
        workload_names=json.dumps(workload_names),
        tool_call_count=len(workload_names) * 3,
    )


def _function_response_part(types: Any, function_call: Any, result: Any) -> Any:
    """Build a tool response and preserve Gemini's function-call identifier."""
    part = types.Part.from_function_response(
        name=function_call.name,
        response={"result": result},
    )
    call_id = getattr(function_call, "id", None)
    if call_id and getattr(part, "function_response", None):
        part.function_response.id = call_id
    return part


def _rate_limit_retry_delay(
    error: Exception, settings: GeminiSettings
) -> float | None:
    """Return Google's requested 429 retry delay, capped at one minute."""
    status_code = getattr(error, "status_code", getattr(error, "code", None))
    message = str(error)
    if status_code != 429 and "resource_exhausted" not in message.lower():
        return None

    delay_match = re.search(r"retry in ([0-9.]+)s", message, re.IGNORECASE)
    if not delay_match:
        delay_match = re.search(
            r"retrydelay['\"]?\s*:\s*['\"]?([0-9.]+)s",
            message,
            re.IGNORECASE,
        )
    if not delay_match:
        return None
    return min(
        max(
            float(delay_match.group(1))
            + settings.retry_delay_buffer_seconds,
            settings.retry_delay_buffer_seconds,
        ),
        settings.max_retry_delay_seconds,
    )


def _generate_content_with_retry(
    model: dict[str, Any],
    contents: Any,
    config: Any,
    settings: GeminiSettings,
    status_callback: Callable[[str], None] | None = None,
) -> Any:
    """Call Gemini and visibly honor a bounded server-provided 429 delay."""
    for retries_used in range(settings.max_rate_limit_retries + 1):
        try:
            return model["client"].models.generate_content(
                model=model["model_name"],
                contents=contents,
                config=config,
            )
        except Exception as error:
            delay = _rate_limit_retry_delay(error, settings)
            if (
                delay is None
                or retries_used == settings.max_rate_limit_retries
            ):
                raise
            if status_callback:
                status_callback(
                    "Gemini rate limit reached; "
                    f"retrying in {delay:g}s "
                    f"({retries_used + 1}/"
                    f"{settings.max_rate_limit_retries})..."
                )
            time.sleep(delay)
    raise AssertionError("unreachable")


def _response_parts(response: Any) -> list[Any]:
    """Read candidate parts directly without triggering SDK text warnings."""
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return []
    content = getattr(candidates[0], "content", None)
    return list(getattr(content, "parts", None) or [])


def _model_response(
    model: dict[str, Any],
    prompt: str,
    tools: WorkloadTools,
    response_schema: dict[str, Any],
    settings: GeminiSettings,
    status_callback: Callable[[str], None] | None = None,
) -> str:
    """Send a prompt and handle Gemini function calls until text is returned."""
    types = model["types"]
    contents: Any = prompt

    for _ in range(settings.max_tool_rounds):
        config = types.GenerateContentConfig(
            tools=[model["tool"]],
            response_mime_type="application/json",
            response_json_schema=response_schema,
            thinking_config=model.get("thinking_config"),
            max_output_tokens=settings.max_output_tokens,
        )
        response = _generate_content_with_retry(
            model, contents, config, settings, status_callback
        )
        parts = _response_parts(response)
        function_calls = [
            part.function_call
            for part in parts
            if getattr(part, "function_call", None)
        ]
        if not function_calls:
            if parts:
                text = "".join(
                    part.text for part in parts if getattr(part, "text", None)
                )
            else:
                text = getattr(response, "text", "") or ""
            if not text.strip():
                finish_reason = getattr(response, "finish_reason", "unknown")
                raise ValueError(
                    "Gemini returned no recommendation text "
                    f"(finish_reason={finish_reason})"
                )
            return text

        if status_callback:
            status_callback(
                f"Gemini requested {len(function_calls)} tool lookups; "
                "sending their results..."
            )

        function_response_parts = []
        for function_call in function_calls:
            function = getattr(tools, function_call.name)
            result = function(**dict(function_call.args))
            function_response_parts.append(
                _function_response_part(types, function_call, result)
            )
        history = (
            [types.Content(role="user", parts=[types.Part.from_text(text=prompt)])]
            if isinstance(contents, str)
            else list(contents)
        )
        contents = [
            *history,
            response.candidates[0].content,
            types.Content(role="user", parts=function_response_parts),
        ]
    raise ValueError("Gemini exceeded the maximum number of response turns")


def _model_candidates(
    requested_model: str, fallback_models: tuple[str, ...]
) -> list[str]:
    """Return the requested model followed by unique known fallbacks."""
    return list(dict.fromkeys([requested_model, *fallback_models]))


def _is_model_availability_error(error: Exception) -> bool:
    """Identify errors that justify trying another model name."""
    message = str(error).lower()
    status_code = getattr(error, "status_code", None)
    return (
        status_code == 503
        or "high demand" in message
        or "temporarily unavailable" in message
        or "not found" in message
        or "is not found" in message
        or "not supported" in message
        or "unknown model" in message
    )


def _is_invalid_api_key_error(error: Exception) -> bool:
    """Identify Gemini errors caused by a missing, malformed, or revoked key."""
    message = str(error).lower()
    return (
        "api_key_invalid" in message
        or "api key not valid" in message
        or "invalid api key" in message
    )


def _is_rate_limit_error(error: Exception) -> bool:
    """Identify Gemini quota errors without treating them as model fallback."""
    status_code = getattr(error, "status_code", getattr(error, "code", None))
    message = str(error).lower()
    return status_code == 429 or "resource_exhausted" in message


def recommend_workloads(
    model: Any,
    tools: WorkloadTools,
    workload_names: list[str],
    model_name: str,
    gemini_settings: GeminiSettings,
    guardrail_settings: GuardrailSettings,
    prompt_template: str,
    status_callback: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """Generate one guarded recommendation per workload in one conversation."""
    raw_response = _model_response(
        model,
        _prompt_for_workloads(workload_names, prompt_template),
        tools,
        BATCH_RECOMMENDATION_SCHEMA,
        gemini_settings,
        status_callback,
    )
    proposals = _parse_batch_recommendations(raw_response, workload_names)

    required_tools = {
        "get_workload_stats",
        "get_incident_history",
        "get_criticality",
    }
    called_tools: dict[str, set[str]] = {name: set() for name in workload_names}
    for entry in tools.trace:
        name = entry.arguments.get("name")
        if name in called_tools:
            called_tools[name].add(entry.name)
    for name in workload_names:
        missing = required_tools - called_tools[name]
        if missing:
            raise ValueError(
                f"Gemini skipped required tools for {name}: {sorted(missing)}"
            )

    results = []
    for name, proposal in zip(workload_names, proposals):
        stats = tools.workload_by_name[name]
        trace = [
            entry.as_dict()
            for entry in tools.trace
            if entry.arguments.get("name") == name
        ]
        results.append(
            Recommendation(
                name,
                proposal,
                validate_recommendation(
                    stats,
                    "cpu",
                    proposal["recommended_cpu"],
                    guardrail_settings,
                ),
                validate_recommendation(
                    stats,
                    "memory",
                    proposal["recommended_memory"],
                    guardrail_settings,
                ),
                trace,
                model_name,
            ).as_dict()
        )
    return results


def create_model(
    api_key: str,
    tools: WorkloadTools,
    model_name: str,
    settings: GeminiSettings,
) -> Any:
    """Create a new Google Gen AI client and declare the read-only tools."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    declarations = []
    for function in _tool_functions(tools):
        declarations.append(
            types.FunctionDeclaration(
                name=function.__name__,
                description=function.__doc__ or function.__name__,
                parameters_json_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                    },
                    "required": ["name"],
                },
            )
        )
    return {
        "client": client,
        "model_name": model_name,
        "tool": types.Tool(function_declarations=declarations),
        "types": types,
        "thinking_config": (
            types.ThinkingConfig(
                thinking_level=settings.thinking_level.upper()
            )
            if any(
                model_name.startswith(prefix)
                for prefix in settings.thinking_model_prefixes
            )
            else None
        ),
    }


def _recommend_batch_with_fallback(
    workload_stats: list[dict[str, Any]],
    api_key: str,
    gemini_settings: GeminiSettings,
    guardrail_settings: GuardrailSettings,
    prompt_template: str,
    status_callback: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """Recommend all workloads, trying fallbacks only when appropriate."""
    last_model_error = None
    workload_names = [workload["name"] for workload in workload_stats]
    candidates = _model_candidates(
        gemini_settings.model, gemini_settings.fallback_models
    )
    for candidate in candidates:
        tools = WorkloadTools(workload_stats)
        model = create_model(
            api_key,
            tools,
            candidate,
            gemini_settings,
        )
        try:
            return recommend_workloads(
                model,
                tools,
                workload_names,
                candidate,
                gemini_settings,
                guardrail_settings,
                prompt_template,
                status_callback,
            )
        except Exception as error:
            if not _is_model_availability_error(error):
                raise
            last_model_error = error
        finally:
            close = getattr(model["client"], "close", None)
            if close:
                close()

    raise RuntimeError(
        "No available Gemini model succeeded. Tried: " + ", ".join(candidates)
    ) from last_model_error


def generate_recommendations(
    workload_stats: list[dict[str, Any]],
    api_key: str,
    gemini_settings: GeminiSettings,
    guardrail_settings: GuardrailSettings,
    prompt_template: str,
    status_callback: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """Generate all guarded recommendations in one quota-efficient batch."""
    if not isinstance(workload_stats, list):
        raise ValueError("Workload statistics must be a list")
    if not workload_stats:
        return []

    return _recommend_batch_with_fallback(
        workload_stats,
        api_key,
        gemini_settings,
        guardrail_settings,
        prompt_template,
        status_callback,
    )
