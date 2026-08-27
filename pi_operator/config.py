"""Runtime configuration. All tunables live here so runs are reproducible from env alone."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PI_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- LLM ---
    anthropic_api_key: str = ""
    model: str = "claude-sonnet-5"
    planner_model: str = "claude-sonnet-5"

    # --- target under automation ---
    target: str = "mockdms"
    target_base_url: str = "http://localhost:8080"
    target_username: str = ""
    target_password: str = ""

    # --- guardrails: every run is bounded on three axes ---
    max_steps: int = 60
    max_wall_seconds: int = 600
    max_usd: float = 1.50

    # --- browser ---
    headless: bool = True
    viewport_width: int = 1440
    viewport_height: int = 900
    nav_timeout_ms: int = 20_000

    # --- artifacts ---
    runs_dir: Path = REPO_ROOT / "runs"

    def model_post_init(self, _ctx) -> None:
        # ANTHROPIC_API_KEY is conventionally unprefixed; accept it without PI_.
        if not self.anthropic_api_key:
            import os

            object.__setattr__(self, "anthropic_api_key", os.getenv("ANTHROPIC_API_KEY", ""))


settings = Settings()
