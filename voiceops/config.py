"""Per-organization (property management company) configuration.

Each org gets its own limits, disclosures and knowledge base, so one deployment can
serve many property managers with different policies.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


@dataclass(frozen=True)
class Settings:
    org_id: str = os.environ.get("VOICEOPS_ORG", "harborview")
    db_path: str = os.environ.get("VOICEOPS_DB", str(ROOT / "var" / "voiceops.db"))
    pms_base_url: str = os.environ.get("PMS_BASE_URL", "http://127.0.0.1:8001")
    pms_client_id: str = os.environ.get("PMS_CLIENT_ID", "voiceops-agent")
    pms_client_secret: str = os.environ.get("PMS_CLIENT_SECRET", "demo-secret")
    # "scripted" runs the deterministic offline policy; "claude" calls the Anthropic API.
    model_provider: str = os.environ.get(
        "VOICEOPS_MODEL_PROVIDER", "claude" if os.environ.get("ANTHROPIC_API_KEY") else "scripted"
    )
    model: str = os.environ.get("VOICEOPS_MODEL", "claude-opus-5-5")
    # Voice turns are latency-bound, so the default effort is low; raise it per route if evals justify it.
    effort: str = os.environ.get("VOICEOPS_EFFORT", "low")
    twilio_auth_token: str | None = os.environ.get("TWILIO_AUTH_TOKEN")
    twilio_account_sid: str | None = os.environ.get("TWILIO_ACCOUNT_SID")
    twilio_from_number: str | None = os.environ.get("TWILIO_FROM_NUMBER")
    public_base_url: str | None = os.environ.get("PUBLIC_BASE_URL")


def settings() -> Settings:
    return Settings()


@lru_cache(maxsize=None)
def org_config(org_id: str) -> dict:
    return json.loads((CONFIG_DIR / "orgs" / f"{org_id}.json").read_text())


@lru_cache(maxsize=None)
def knowledge_base(org_id: str) -> str:
    return (CONFIG_DIR / "knowledge" / f"{org_id}.md").read_text()
