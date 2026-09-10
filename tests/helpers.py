"""Shared configuration and prompt fixtures used by the offline tests."""

from pathlib import Path

from k8s_cost_agent.config import load_settings


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SETTINGS = load_settings(PROJECT_ROOT / "config/settings.toml")
PROMPT_TEMPLATE = SETTINGS.paths.recommendation_prompt.read_text(encoding="utf-8")
