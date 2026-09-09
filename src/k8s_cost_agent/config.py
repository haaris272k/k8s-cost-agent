"""Load and validate all environment-specific application settings."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from dotenv import load_dotenv


DEFAULT_CONFIG_PATH = Path("config/settings.toml")
THINKING_LEVELS = ("minimal", "low", "medium", "high")
KUBERNETES_CONFIG_MODES = ("auto", "kubeconfig", "in_cluster")


@dataclass(frozen=True)
class PathSettings:
    """Resolved input, output, prompt, and environment file locations."""

    base_dir: Path
    environment: Path
    samples: Path
    statistics: Path
    recommendations: Path
    report: Path
    recommendation_prompt: Path


@dataclass(frozen=True)
class KubernetesSettings:
    namespace: str
    criticality_annotation: str
    config_mode: str


@dataclass(frozen=True)
class PrometheusSettings:
    url: str
    disable_ssl: bool
    cpu_metric: str
    memory_metric: str
    cpu_rate_window: str
    namespace_label: str
    pod_label: str


@dataclass(frozen=True)
class SamplingSettings:
    count: int
    interval_seconds: float
    debug_prometheus: bool


@dataclass(frozen=True)
class StatisticsSettings:
    percentile_rank: float
    trend_change_threshold: float
    minimum_trend_samples: int


@dataclass(frozen=True)
class GuardrailSettings:
    safety_margin: float
    high_variance_ratio: float
    recent_incident_hours: float
    protected_criticalities: tuple[str, ...]


@dataclass(frozen=True)
class GeminiSettings:
    api_key_environment_variable: str
    model: str
    fallback_models: tuple[str, ...]
    thinking_level: str
    thinking_model_prefixes: tuple[str, ...]
    max_rate_limit_retries: int
    retry_delay_buffer_seconds: float
    max_retry_delay_seconds: float
    max_tool_rounds: int
    max_output_tokens: int


@dataclass(frozen=True)
class ReportSettings:
    title: str
    subtitle: str


@dataclass(frozen=True)
class Settings:
    """Complete validated configuration used by every pipeline stage."""

    source: Path
    paths: PathSettings
    kubernetes: KubernetesSettings
    prometheus: PrometheusSettings
    sampling: SamplingSettings
    statistics: StatisticsSettings
    guardrails: GuardrailSettings
    gemini: GeminiSettings
    report: ReportSettings


def _table(document: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = document.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"Missing configuration table: [{name}]")
    return value


def _value(
    table: Mapping[str, Any], table_name: str, key: str, expected_type: type
) -> Any:
    if key not in table:
        raise ValueError(f"Missing configuration value: {table_name}.{key}")
    value = table[key]
    if expected_type in {int, float} and isinstance(value, bool):
        raise ValueError(f"{table_name}.{key} must be a number")
    if expected_type is float and isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, expected_type):
        type_name = expected_type.__name__
        raise ValueError(f"{table_name}.{key} must be a {type_name}")
    if expected_type is str and not value.strip():
        raise ValueError(f"{table_name}.{key} cannot be empty")
    return value


def _string_tuple(
    table: Mapping[str, Any],
    table_name: str,
    key: str,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    value = table.get(key)
    if not isinstance(value, list) or (not value and not allow_empty):
        qualifier = "a string list" if allow_empty else "a non-empty string list"
        raise ValueError(f"{table_name}.{key} must be {qualifier}")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"{table_name}.{key} must contain only non-empty strings")
    return tuple(value)


def _resolved_path(base_dir: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base_dir / path).resolve()


def _validate_identifier(value: str, label: str) -> None:
    if not re.fullmatch(r"[a-zA-Z_:][a-zA-Z0-9_:]*", value):
        raise ValueError(f"{label} is not a valid Prometheus identifier")


def load_settings(path: str | Path = DEFAULT_CONFIG_PATH) -> Settings:
    """Load a TOML file, resolve its paths, and fail early on invalid values."""
    source = Path(path).expanduser().resolve()
    try:
        with source.open("rb") as config_file:
            document = tomllib.load(config_file)
    except FileNotFoundError as error:
        raise ValueError(f"Configuration file does not exist: {source}") from error
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"Invalid TOML configuration in {source}: {error}") from error

    paths_data = _table(document, "paths")
    configured_base = _value(paths_data, "paths", "base_dir", str)
    base_dir = _resolved_path(source.parent, configured_base)
    paths = PathSettings(
        base_dir=base_dir,
        environment=_resolved_path(
            base_dir, _value(paths_data, "paths", "environment", str)
        ),
        samples=_resolved_path(
            base_dir, _value(paths_data, "paths", "samples", str)
        ),
        statistics=_resolved_path(
            base_dir, _value(paths_data, "paths", "statistics", str)
        ),
        recommendations=_resolved_path(
            base_dir, _value(paths_data, "paths", "recommendations", str)
        ),
        report=_resolved_path(
            base_dir, _value(paths_data, "paths", "report", str)
        ),
        recommendation_prompt=_resolved_path(
            base_dir,
            _value(paths_data, "paths", "recommendation_prompt", str),
        ),
    )
    load_dotenv(paths.environment, override=False)

    kubernetes_data = _table(document, "kubernetes")
    kubernetes = KubernetesSettings(
        namespace=_value(kubernetes_data, "kubernetes", "namespace", str),
        criticality_annotation=_value(
            kubernetes_data, "kubernetes", "criticality_annotation", str
        ),
        config_mode=_value(kubernetes_data, "kubernetes", "config_mode", str),
    )
    if kubernetes.config_mode not in KUBERNETES_CONFIG_MODES:
        raise ValueError(
            "kubernetes.config_mode must be one of "
            + ", ".join(KUBERNETES_CONFIG_MODES)
        )

    prometheus_data = _table(document, "prometheus")
    prometheus = PrometheusSettings(
        url=_value(prometheus_data, "prometheus", "url", str),
        disable_ssl=_value(
            prometheus_data, "prometheus", "disable_ssl", bool
        ),
        cpu_metric=_value(prometheus_data, "prometheus", "cpu_metric", str),
        memory_metric=_value(
            prometheus_data, "prometheus", "memory_metric", str
        ),
        cpu_rate_window=_value(
            prometheus_data, "prometheus", "cpu_rate_window", str
        ),
        namespace_label=_value(
            prometheus_data, "prometheus", "namespace_label", str
        ),
        pod_label=_value(prometheus_data, "prometheus", "pod_label", str),
    )
    for value, label in (
        (prometheus.cpu_metric, "prometheus.cpu_metric"),
        (prometheus.memory_metric, "prometheus.memory_metric"),
        (prometheus.namespace_label, "prometheus.namespace_label"),
        (prometheus.pod_label, "prometheus.pod_label"),
    ):
        _validate_identifier(value, label)
    if not re.fullmatch(
        r"[1-9][0-9]*(?:ms|s|m|h|d|w|y)",
        prometheus.cpu_rate_window,
    ):
        raise ValueError(
            "prometheus.cpu_rate_window must be one Prometheus duration"
        )

    sampling_data = _table(document, "sampling")
    sampling = SamplingSettings(
        count=_value(sampling_data, "sampling", "count", int),
        interval_seconds=_value(
            sampling_data, "sampling", "interval_seconds", float
        ),
        debug_prometheus=_value(
            sampling_data, "sampling", "debug_prometheus", bool
        ),
    )
    if sampling.count < 1:
        raise ValueError("sampling.count must be at least 1")
    if sampling.interval_seconds < 0:
        raise ValueError("sampling.interval_seconds cannot be negative")

    statistics_data = _table(document, "statistics")
    statistics = StatisticsSettings(
        percentile_rank=_value(
            statistics_data, "statistics", "percentile_rank", float
        ),
        trend_change_threshold=_value(
            statistics_data, "statistics", "trend_change_threshold", float
        ),
        minimum_trend_samples=_value(
            statistics_data, "statistics", "minimum_trend_samples", int
        ),
    )
    if not 0 <= statistics.percentile_rank <= 100:
        raise ValueError("statistics.percentile_rank must be between 0 and 100")
    if statistics.trend_change_threshold < 0:
        raise ValueError("statistics.trend_change_threshold cannot be negative")
    if statistics.minimum_trend_samples < 2:
        raise ValueError("statistics.minimum_trend_samples must be at least 2")

    guardrail_data = _table(document, "guardrails")
    guardrails = GuardrailSettings(
        safety_margin=_value(
            guardrail_data, "guardrails", "safety_margin", float
        ),
        high_variance_ratio=_value(
            guardrail_data, "guardrails", "high_variance_ratio", float
        ),
        recent_incident_hours=_value(
            guardrail_data, "guardrails", "recent_incident_hours", float
        ),
        protected_criticalities=_string_tuple(
            guardrail_data, "guardrails", "protected_criticalities"
        ),
    )
    if guardrails.safety_margin < 1:
        raise ValueError("guardrails.safety_margin must be at least 1")
    if guardrails.high_variance_ratio < 1:
        raise ValueError("guardrails.high_variance_ratio must be at least 1")
    if guardrails.recent_incident_hours < 0:
        raise ValueError("guardrails.recent_incident_hours cannot be negative")
    gemini_data = _table(document, "gemini")
    gemini = GeminiSettings(
        api_key_environment_variable=_value(
            gemini_data, "gemini", "api_key_environment_variable", str
        ),
        model=_value(gemini_data, "gemini", "model", str),
        fallback_models=_string_tuple(
            gemini_data,
            "gemini",
            "fallback_models",
            allow_empty=True,
        ),
        thinking_level=_value(
            gemini_data, "gemini", "thinking_level", str
        ),
        thinking_model_prefixes=_string_tuple(
            gemini_data,
            "gemini",
            "thinking_model_prefixes",
            allow_empty=True,
        ),
        max_rate_limit_retries=_value(
            gemini_data, "gemini", "max_rate_limit_retries", int
        ),
        retry_delay_buffer_seconds=_value(
            gemini_data, "gemini", "retry_delay_buffer_seconds", float
        ),
        max_retry_delay_seconds=_value(
            gemini_data, "gemini", "max_retry_delay_seconds", float
        ),
        max_tool_rounds=_value(
            gemini_data, "gemini", "max_tool_rounds", int
        ),
        max_output_tokens=_value(
            gemini_data, "gemini", "max_output_tokens", int
        ),
    )
    if gemini.thinking_level not in THINKING_LEVELS:
        raise ValueError(
            "gemini.thinking_level must be one of " + ", ".join(THINKING_LEVELS)
        )
    if not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*", gemini.api_key_environment_variable
    ):
        raise ValueError(
            "gemini.api_key_environment_variable must be a valid "
            "environment variable name"
        )
    if gemini.max_rate_limit_retries < 0:
        raise ValueError("gemini.max_rate_limit_retries cannot be negative")
    if gemini.retry_delay_buffer_seconds < 0:
        raise ValueError("gemini.retry_delay_buffer_seconds cannot be negative")
    if gemini.max_retry_delay_seconds <= 0:
        raise ValueError("gemini.max_retry_delay_seconds must be positive")
    if gemini.max_tool_rounds < 1:
        raise ValueError("gemini.max_tool_rounds must be at least 1")
    if gemini.max_output_tokens < 1:
        raise ValueError("gemini.max_output_tokens must be at least 1")

    report_data = _table(document, "report")
    report = ReportSettings(
        title=_value(report_data, "report", "title", str),
        subtitle=_value(report_data, "report", "subtitle", str),
    )

    if not paths.recommendation_prompt.is_file():
        raise ValueError(
            "Recommendation prompt file does not exist: "
            f"{paths.recommendation_prompt}"
        )
    output_paths = (
        paths.samples,
        paths.statistics,
        paths.recommendations,
        paths.report,
    )
    if len(set(output_paths)) != len(output_paths):
        raise ValueError("Configured artifact output paths must be distinct")

    return Settings(
        source=source,
        paths=paths,
        kubernetes=kubernetes,
        prometheus=prometheus,
        sampling=sampling,
        statistics=statistics,
        guardrails=guardrails,
        gemini=gemini,
        report=report,
    )


def read_api_key(
    settings: Settings, environment: Mapping[str, str] | None = None
) -> str:
    """Read the configured secret without logging or returning its source file."""
    values = environment if environment is not None else os.environ
    variable = settings.gemini.api_key_environment_variable
    api_key = values.get(variable, "").strip()
    if not api_key:
        raise ValueError(
            f"Required secret {variable} is not set; add it to "
            f"{settings.paths.environment} or export it in the shell"
        )
    return api_key
