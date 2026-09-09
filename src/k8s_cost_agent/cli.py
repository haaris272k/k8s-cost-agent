"""Single command-line interface for full and stage-specific execution."""

import argparse
import sys
from pathlib import Path
from typing import Sequence

from requests.exceptions import RequestException

from k8s_cost_agent.config import DEFAULT_CONFIG_PATH, load_settings
from k8s_cost_agent.pipeline import (
    analyze_stage,
    collect_stage,
    recommend_stage,
    report_stage,
    run_pipeline,
)
from k8s_cost_agent.recommendation.gemini import (
    _is_invalid_api_key_error,
    _is_rate_limit_error,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="k8s-cost-agent",
        description="Read-only Kubernetes resource recommendation pipeline",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="TOML settings file (default: %(default)s)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="Run the complete pipeline")
    commands.add_parser("collect", help="Collect and persist metric history")
    commands.add_parser("analyze", help="Calculate and persist statistics")
    commands.add_parser("recommend", help="Generate and guard LLM proposals")
    commands.add_parser("report", help="Render the HTML report")
    commands.add_parser("check-config", help="Validate configuration and paths")
    return parser


def _status(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def main(argv: Sequence[str] | None = None) -> None:
    """Load one configuration and dispatch the requested pipeline stage."""
    args = _parser().parse_args(argv)
    try:
        settings = load_settings(args.config)
        if args.command == "check-config":
            print(f"Configuration is valid: {settings.source}")
            return
        if args.command == "collect":
            collect_stage(settings, _status)
        elif args.command == "analyze":
            analyze_stage(settings, _status)
        elif args.command == "recommend":
            recommend_stage(settings, _status)
        elif args.command == "report":
            report_stage(settings, _status)
        else:
            result = run_pipeline(settings, _status)
            print(
                f"Pipeline complete for {result['workloads']} workloads. "
                f"Report: {result['report']}"
            )
    except RequestException as error:
        raise SystemExit(
            "Unable to reach configured Prometheus endpoint. "
            "Check prometheus.url and the local port-forward."
        ) from error
    except Exception as error:
        if _is_invalid_api_key_error(error):
            raise SystemExit(
                "Gemini rejected the configured API key. Check the secret in "
                f"{settings.paths.environment} and Google AI Studio."
            ) from None
        if _is_rate_limit_error(error):
            raise SystemExit(
                "Gemini quota is exhausted after the configured bounded retries. "
                "Wait for the quota window to reset and rerun the command."
            ) from None
        if isinstance(error, ValueError):
            raise SystemExit(str(error)) from None
        raise
