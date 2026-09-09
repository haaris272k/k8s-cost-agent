"""Deterministic safety checks for Phase 3 workload recommendations.

This module is intentionally independent of Kubernetes, Prometheus, and any
LLM. It receives a Phase 3 workload dictionary plus one proposed resource
value, then decides whether that proposal is safe to approve.

The LLM is not part of this decision. A future LLM may suggest a value, but
this module remains the final authority for Phase 4.
"""

from datetime import datetime, timezone
from typing import Any

from k8s_cost_agent.config import GuardrailSettings


SUPPORTED_RESOURCES = {"cpu", "memory"}


def _parse_quantity(value: str | int | float, resource: str) -> float:
    """Convert a Kubernetes quantity into a numeric base unit.

    CPU is returned in cores, so ``500m`` becomes ``0.5``. Memory is returned
    in bytes, so ``512Mi`` becomes ``536870912``. Numeric inputs are already
    assumed to be in the correct base unit for the selected resource.
    """
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Invalid {resource} quantity: {value!r}")

    units = {
        "cpu": {"m": 0.001, "": 1.0},
        "memory": {
            "Ki": 1024.0,
            "Mi": 1024.0**2,
            "Gi": 1024.0**3,
            "Ti": 1024.0**4,
            "K": 1000.0,
            "M": 1000.0**2,
            "G": 1000.0**3,
            "T": 1000.0**4,
            "": 1.0,
        },
    }
    if resource not in units:
        raise ValueError(f"Unsupported resource: {resource}")

    for suffix, multiplier in sorted(
        units[resource].items(), key=lambda item: -len(item[0])
    ):
        if value.endswith(suffix):
            number = value[: -len(suffix)] if suffix else value
            try:
                return float(number) * multiplier
            except ValueError:
                break
    raise ValueError(f"Invalid {resource} quantity: {value!r}")


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO timestamp and normalize it to UTC when possible."""
    if not value:
        return None
    if isinstance(value, datetime):
        timestamp = value
    elif isinstance(value, str):
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError(f"Invalid incident timestamp: {value!r}")
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _has_recent_incident(
    workload_stats: dict[str, Any],
    recent_window_hours: float,
    now: datetime | None = None,
) -> bool:
    """Return whether a restart or OOMKill falls inside the safety window.

    Phase 2 currently exposes counts and the latest OOM flag, but not always a
    timestamp. When an incident exists without a timestamp, this function
    fails closed and treats it as recent rather than approving blindly.
    """
    restart_count = int(workload_stats.get("restart_count", 0))
    oom_killed = bool(workload_stats.get("last_restart_oomkilled", False))
    if restart_count <= 0 and not oom_killed:
        return False

    timestamp = _parse_timestamp(workload_stats.get("last_restart_at"))
    if timestamp is None:
        return True

    current_time = now or datetime.now(timezone.utc)
    age_hours = (current_time - timestamp).total_seconds() / 3600
    return 0 <= age_hours <= recent_window_hours


def _is_high_variance(
    workload_stats: dict[str, Any],
    resource: str,
    settings: GuardrailSettings,
) -> bool:
    """Check whether the configured spread ratio marks usage as variable."""
    ratios = workload_stats.get("percentile_to_avg_ratio")
    if not isinstance(ratios, dict):
        ratios = workload_stats.get("p95_to_avg_ratio", {})
    ratio = ratios.get(_usage_field(resource))
    return ratio is not None and float(ratio) >= settings.high_variance_ratio


def _usage_field(resource: str) -> str:
    """Return the Phase 3 usage field name for a resource."""
    return (
        "current_cpu_usage_cores"
        if resource == "cpu"
        else "current_memory_usage_bytes"
    )


def validate_recommendation(
    workload_stats: dict[str, Any],
    resource: str,
    proposed_value: str | int | float,
    settings: GuardrailSettings,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate one proposed CPU or memory value against Phase 4 guardrails.

    Args:
        workload_stats: One Phase 3 workload record.
        resource: Either ``cpu`` or ``memory``.
        proposed_value: CPU in cores or memory in bytes. Kubernetes quantity
            strings such as ``500m`` and ``512Mi`` are also accepted.
        settings: Deterministic policy thresholds loaded from configuration.
        now: Optional clock value used by callers and deterministic tests.

    Returns:
        A dictionary containing ``approved``, ``approved_value``, ``reason``,
        and ``confidence``. Rejected proposals return the current request as
        ``approved_value`` so callers cannot accidentally apply the proposal.
    """
    if resource not in SUPPORTED_RESOURCES:
        raise ValueError(f"resource must be one of {sorted(SUPPORTED_RESOURCES)}")
    current_request_key = (
        "requested_cpu_cores" if resource == "cpu" else "requested_memory_bytes"
    )
    current_request = _parse_quantity(workload_stats[current_request_key], resource)
    proposed = _parse_quantity(proposed_value, resource)
    percentiles = workload_stats.get("percentile")
    if not isinstance(percentiles, dict):
        percentiles = workload_stats.get("p95", {})
    observed_percentile = float(
        percentiles.get(_usage_field(resource), 0.0)
    )
    minimum_safe_value = observed_percentile * settings.safety_margin
    confidence = (
        "low"
        if _is_high_variance(workload_stats, resource, settings)
        else "normal"
    )

    criticality = workload_stats.get("criticality")
    if criticality in settings.protected_criticalities:
        return {
            "approved": False,
            "approved_value": current_request,
            "reason": f"{criticality}, manual review only",
            "confidence": confidence,
        }
    if _has_recent_incident(
        workload_stats, settings.recent_incident_hours, now
    ):
        return {
            "approved": False,
            "approved_value": current_request,
            "reason": "recent restart or OOMKill, manual review only",
            "confidence": confidence,
        }
    if proposed < minimum_safe_value:
        return {
            "approved": False,
            "approved_value": current_request,
            "reason": f"below percentile safety floor ({minimum_safe_value:g})",
            "confidence": confidence,
        }
    if proposed > current_request:
        return {
            "approved": False,
            "approved_value": current_request,
            "reason": "proposed value exceeds current request",
            "confidence": confidence,
        }

    return {
        "approved": True,
        "approved_value": proposed,
        "reason": "passed deterministic safety checks",
        "confidence": confidence,
    }
