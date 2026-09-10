"""Connect the independent pipeline stages and persist their outputs.

Each stage has one input and one output boundary, which makes it possible to
run the complete flow or resume from a saved artifact. This module coordinates
the work; calculation and safety decisions remain in their owning modules.
"""

from pathlib import Path
from typing import Any, Callable

from k8s_cost_agent.analysis.statistics import summarize_history
from k8s_cost_agent.collection.history import collect_history
from k8s_cost_agent.config import Settings, read_api_key
from k8s_cost_agent.recommendation.gemini import generate_recommendations
from k8s_cost_agent.reporting.html import build_report_rows, render_html
from k8s_cost_agent.storage import read_json_list, write_json, write_text


StatusCallback = Callable[[str], None]


def collect_stage(
    settings: Settings,
    status_callback: StatusCallback | None = None,
) -> list[list[dict[str, Any]]]:
    """Read Kubernetes/Prometheus and write the configured samples JSON."""
    history = collect_history(settings, status_callback)
    write_json(settings.paths.samples, history)
    if status_callback:
        status_callback(f"Saved samples to {settings.paths.samples}")
    return history


def analyze_stage(
    settings: Settings,
    status_callback: StatusCallback | None = None,
) -> list[dict[str, Any]]:
    """Read samples, calculate statistics, and write workload statistics JSON."""
    history = read_json_list(settings.paths.samples, "Sample history")
    statistics = summarize_history(history, settings.statistics)
    write_json(settings.paths.statistics, statistics)
    if status_callback:
        status_callback(f"Saved workload statistics to {settings.paths.statistics}")
    return statistics


def recommend_stage(
    settings: Settings,
    status_callback: StatusCallback | None = None,
    api_key: str | None = None,
) -> list[dict[str, Any]]:
    """Read statistics, ask Gemini, validate proposals, and write decisions.

    ``api_key`` is injectable so the full pipeline can read the secret once and
    pass it in without storing it in an artifact.
    """
    workload_statistics = read_json_list(
        settings.paths.statistics, "Workload statistics"
    )
    prompt_template = settings.paths.recommendation_prompt.read_text(
        encoding="utf-8"
    )
    if status_callback:
        status_callback(
            "Generating one batched Gemini recommendation conversation "
            f"(model={settings.gemini.model}, "
            f"thinking={settings.gemini.thinking_level})"
        )
    recommendations = generate_recommendations(
        workload_statistics,
        api_key or read_api_key(settings),
        settings.gemini,
        settings.guardrails,
        prompt_template,
        status_callback,
    )
    write_json(settings.paths.recommendations, recommendations)
    if status_callback:
        status_callback(
            f"Saved guarded recommendations to {settings.paths.recommendations}"
        )
    return recommendations


def report_stage(
    settings: Settings,
    status_callback: StatusCallback | None = None,
) -> list[dict[str, Any]]:
    """Read statistics and guarded decisions, then write the HTML report."""
    statistics = read_json_list(
        settings.paths.statistics, "Workload statistics"
    )
    recommendations = read_json_list(
        settings.paths.recommendations, "Guarded recommendations"
    )
    rows = build_report_rows(statistics, recommendations)
    write_text(settings.paths.report, render_html(rows, settings.report))
    if status_callback:
        status_callback(f"Saved HTML report to {settings.paths.report}")
    return rows


def run_pipeline(
    settings: Settings,
    status_callback: StatusCallback | None = None,
) -> dict[str, Path | int]:
    """Run collection, analysis, recommendation, and reporting in order.

    Returns:
        A small completion summary containing workload counts and report path.
    """
    # Check the credential before a potentially long sampling window. This
    # avoids collecting for several minutes only to discover a missing key.
    api_key = read_api_key(settings)
    if status_callback:
        status_callback("[1/4] Collecting Kubernetes and Prometheus history")
    collect_stage(settings, status_callback)
    if status_callback:
        status_callback("[2/4] Calculating deterministic statistics")
    statistics = analyze_stage(settings, status_callback)
    if status_callback:
        status_callback("[3/4] Generating and validating Gemini proposals")
    recommendations = recommend_stage(settings, status_callback, api_key)
    if status_callback:
        status_callback("[4/4] Rendering the report")
    report_stage(settings, status_callback)
    return {
        "workloads": len(statistics),
        "recommendations": len(recommendations),
        "report": settings.paths.report,
    }
