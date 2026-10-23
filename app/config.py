"""Runtime configuration, read from environment / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:  # python-dotenv is optional at import time
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:  # pragma: no cover
    pass


def _path(env: str, default: str) -> Path:
    p = Path(os.environ.get(env) or default)
    return p if p.is_absolute() else ROOT / p


@dataclass
class Config:
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5-5"
    models_file: Path = ROOT / "knowledge/models.dummy.yaml"
    business_file: Path = ROOT / "knowledge/business.dummy.yaml"
    calendar_provider: str = "mock"
    google_calendar_id: str = "primary"
    google_credentials_file: Path = ROOT / "data/credentials.json"
    google_token_file: Path = ROOT / "data/token.json"
    db_path: Path = ROOT / "data/leads.db"
    mock_calendar_file: Path = ROOT / "data/mock_calendar.json"
    port: int = 5050

    @property
    def ai_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
            anthropic_model=os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-5-5",
            models_file=_path("MODELS_FILE", "knowledge/models.dummy.yaml"),
            business_file=_path("BUSINESS_FILE", "knowledge/business.dummy.yaml"),
            calendar_provider=(os.environ.get("CALENDAR_PROVIDER") or "mock").lower(),
            google_calendar_id=os.environ.get("GOOGLE_CALENDAR_ID") or "primary",
            google_credentials_file=_path("GOOGLE_CREDENTIALS_FILE", "data/credentials.json"),
            google_token_file=_path("GOOGLE_TOKEN_FILE", "data/token.json"),
            db_path=_path("DB_PATH", "data/leads.db"),
            mock_calendar_file=_path("MOCK_CALENDAR_FILE", "data/mock_calendar.json"),
            port=int(os.environ.get("PORT") or 5050),
        )
