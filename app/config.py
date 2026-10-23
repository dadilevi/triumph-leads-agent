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
    models_file: Path = ROOT / "knowledge/models.yaml"
    business_file: Path = ROOT / "knowledge/business.dummy.yaml"
    calendar_provider: str = "mock"
    google_calendar_id: str = "primary"
    google_credentials_file: Path = ROOT / "data/credentials.json"
    google_token_file: Path = ROOT / "data/token.json"
    db_path: Path = ROOT / "data/leads.db"
    mock_calendar_file: Path = ROOT / "data/mock_calendar.json"
    port: int = 5050
    # conversation
    channel: str = "simulator"          # wame / simulator / cloud
    agent_mode: str = "supervised"      # supervised (human approves each reply) / auto
    debounce_seconds: float = 0.0       # wait for the customer to finish typing several messages
    notify_phone: str = ""              # salesperson's number for alerts (best effort)
    # WhatsApp Business Cloud API
    whatsapp_phone_number_id: str = ""
    whatsapp_token: str = ""
    whatsapp_app_secret: str = ""       # verifies webhook signatures
    whatsapp_verify_token: str = ""     # the string you type in Meta's webhook setup
    whatsapp_opening_template: str = "" # approved template name for the first message
    whatsapp_template_lang: str = "he"
    whatsapp_api_version: str = "v21.0"

    @property
    def ai_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
            anthropic_model=os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-5-5",
            models_file=_path("MODELS_FILE", "knowledge/models.yaml"),
            business_file=_path("BUSINESS_FILE", "knowledge/business.dummy.yaml"),
            calendar_provider=(os.environ.get("CALENDAR_PROVIDER") or "mock").lower(),
            google_calendar_id=os.environ.get("GOOGLE_CALENDAR_ID") or "primary",
            google_credentials_file=_path("GOOGLE_CREDENTIALS_FILE", "data/credentials.json"),
            google_token_file=_path("GOOGLE_TOKEN_FILE", "data/token.json"),
            db_path=_path("DB_PATH", "data/leads.db"),
            mock_calendar_file=_path("MOCK_CALENDAR_FILE", "data/mock_calendar.json"),
            port=int(os.environ.get("PORT") or 5050),
            channel=(os.environ.get("CHANNEL") or "simulator").lower(),
            agent_mode=(os.environ.get("AGENT_MODE") or "supervised").lower(),
            debounce_seconds=float(os.environ.get("DEBOUNCE_SECONDS") or 0),
            notify_phone=os.environ.get("NOTIFY_PHONE", "").strip(),
            whatsapp_phone_number_id=os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "").strip(),
            whatsapp_token=os.environ.get("WHATSAPP_TOKEN", "").strip(),
            whatsapp_app_secret=os.environ.get("WHATSAPP_APP_SECRET", "").strip(),
            whatsapp_verify_token=os.environ.get("WHATSAPP_VERIFY_TOKEN", "").strip(),
            whatsapp_opening_template=os.environ.get("WHATSAPP_OPENING_TEMPLATE", "").strip(),
            whatsapp_template_lang=os.environ.get("WHATSAPP_TEMPLATE_LANG") or "he",
            whatsapp_api_version=os.environ.get("WHATSAPP_API_VERSION") or "v21.0",
        )
