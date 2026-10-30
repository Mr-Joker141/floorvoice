"""Runtime configuration, read from environment (and .env at repo root)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

META_API_BASE = "https://api.meta.ai/v1"
META_ASR_REALTIME_URL = "wss://api.meta.ai/v1/asr/realtime"
ASR_MODEL = "muse-voice-transcribe-1.0"
AGENT_MODEL = "muse-spark-1.3"


@dataclass(frozen=True)
class Settings:
    meta_api_key: str
    erp_url: str
    erp_api_key: str
    erp_api_secret: str
    data_dir: Path


def get_settings() -> Settings:
    return Settings(
        meta_api_key=os.environ.get("META_API_KEY", ""),
        erp_url=os.environ.get("ERP_URL", "http://127.0.0.1:8080").rstrip("/"),
        erp_api_key=os.environ.get("ERP_API_KEY", ""),
        erp_api_secret=os.environ.get("ERP_API_SECRET", ""),
        data_dir=Path(os.environ.get("FLOORVOICE_DATA", ROOT / "data")),
    )
