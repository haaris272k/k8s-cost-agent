"""Turn chronological usage samples into one summary per workload.

The input is a JSON list of collector snapshots. Each snapshot is itself a
list of workload dictionaries produced by the collection layer. This module
adds average usage, a configured percentile, and simple trend signals to the
latest record for each workload.

These calculations contain no LLM behavior. Their output becomes evidence for
both Gemini and the deterministic guardrails.
"""

from typing import Any

from k8s_cost_agent.config import StatisticsSettings


# These names match the collector's normalized units: cores and bytes.
CPU_FIELD = "current_cpu_usage_cores"
MEMORY_FIELD = "current_memory_usage_bytes"


def average(values: list[float]) -> float:
    """Return the arithmetic mean of a non-empty list of numbers."""
    if not values:
        raise ValueError("Cannot calculate an average from no samples")
    return sum(values) / len(values)


def percentile(values: list[float], percentile_rank: float) -> float:
    """Return a linearly interpolated percentile from a non-empty list.

    Sorting is performed on a copy, so the caller's sample order is preserved
    for trend calculations. For example, P95 estimates the value below which
    95 percent of the observed usage falls.
    """
    if not values:
        raise ValueError("Cannot calculate a percentile from no samples")
    if not 0 <= percentile_rank <= 100:
        raise ValueError("Percentile rank must be between 0 and 100")

    # A percentile can fall between two samples. Linear interpolation moves a
    # proportional distance between the samples on either side of that point.
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile_rank / 100
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    fraction = position - lower_index
    return ordered[lower_index] + (
        ordered[upper_index] - ordered[lower_index]
    ) * fraction


def trend(
    values: list[float],
    change_threshold: float,
    minimum_samples: int,
) -> str:
    """Compare the first and second halves of a sample window.

    A change at or above the configured threshold is ``increasing`` or
    ``decreasing``. Smaller changes are ``stable``. Windows below the configured
    sample minimum return ``insufficient_data``.
    """
    if len(values) < minimum_samples:
        return "insufficient_data"

    # For an odd number of values, the second half receives the extra sample.
    # Sample order is intentionally preserved because a trend is time-based.
    midpoint = len(values) // 2
    first_half_average = average(values[:midpoint])
    second_half_average = average(values[midpoint:])

    if first_half_average == 0:
        if second_half_average == 0:
            return "stable"
        return "increasing"

    relative_change = (
        second_half_average - first_half_average
    ) / first_half_average
    if relative_change >= change_threshold:
        return "increasing"
    if relative_change <= -change_threshold:
        return "decreasing"
    return "stable"


def percentile_to_average_ratio(
    values: list[float], percentile_rank: float
) -> float | None:
    """Return the configured percentile divided by average.

    A ratio near ``1.0`` indicates consistent usage. A larger ratio indicates
    that occasional high values spread the P95 above typical usage.
    """
    mean = average(values)
    if mean == 0:
        # A zero average has no meaningful ratio and would require division by 0.
        return None
    return percentile(values, percentile_rank) / mean


def _usage_values(samples: list[dict[str, Any]], field: str) -> list[float]:
    """Extract one numeric usage field while preserving sample order."""
    return [float(sample.get(field, 0.0)) for sample in samples]


def summarize_workload(
    samples: list[dict[str, Any]], settings: StatisticsSettings
) -> dict[str, Any]:
    """Add usage statistics to the latest record for one workload."""
    if not samples:
        raise ValueError("A workload must have at least one sample")

    cpu_values = _usage_values(samples, CPU_FIELD)
    memory_values = _usage_values(samples, MEMORY_FIELD)
    # Start with the newest Kubernetes metadata and incident state, then attach
    # statistics calculated from the full observation window.
    latest_record = dict(samples[-1])
    latest_record.update(
        {
            "sample_count": len(samples),
            "avg": {
                CPU_FIELD: average(cpu_values),
                MEMORY_FIELD: average(memory_values),
            },
            "percentile_rank": settings.percentile_rank,
            "percentile": {
                CPU_FIELD: percentile(cpu_values, settings.percentile_rank),
                MEMORY_FIELD: percentile(
                    memory_values, settings.percentile_rank
                ),
            },
            "percentile_to_avg_ratio": {
                CPU_FIELD: percentile_to_average_ratio(
                    cpu_values, settings.percentile_rank
                ),
                MEMORY_FIELD: percentile_to_average_ratio(
                    memory_values, settings.percentile_rank
                ),
            },
            "trend": {
                "cpu": trend(
                    cpu_values,
                    settings.trend_change_threshold,
                    settings.minimum_trend_samples,
                ),
                "memory": trend(
                    memory_values,
                    settings.trend_change_threshold,
                    settings.minimum_trend_samples,
                ),
            },
        }
    )
    return latest_record


def summarize_history(
    history: list[list[dict[str, Any]]], settings: StatisticsSettings
) -> list[dict[str, Any]]:
    """Summarize all workloads in a history of collector snapshots.

    Snapshots must appear in chronological order. Workloads are matched by
    their ``name`` field, and the result is sorted by workload name for stable
    JSON output.
    """
    # Convert a snapshot-oriented structure into workload-oriented timelines.
    # This also handles workloads appearing or disappearing during collection.
    samples_by_workload: dict[str, list[dict[str, Any]]] = {}
    for snapshot in history:
        for sample in snapshot:
            name = sample.get("name")
            if not name:
                raise ValueError("Every workload sample must have a name")
            samples_by_workload.setdefault(name, []).append(sample)

    return [
        summarize_workload(samples_by_workload[name], settings)
        for name in sorted(samples_by_workload)
    ]
