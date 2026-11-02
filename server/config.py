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
SAM_MODEL = "sam-3.1"  # video stock count (pinned; Meta Model API /v1/responses)


@dataclass(frozen=True)
class Settings:
    meta_api_key: str
    erp_url: str
    erp_api_key: str
    erp_api_secret: str
    data_dir: Path
    erp_company: str
    erp_default_warehouse: str
    erp_timezone: str
    agent_reasoning: str
    access_key: str
    pin_salt: str  # per-site salt for worker PIN hashes (seed writes one to .env)
    video_count: bool = False  # FEATURE_VIDEO_COUNT: count stock by filming the shelf
    sam_model: str = SAM_MODEL
    count_min_frames: int = 3  # an object must persist this many frames to be counted
    count_min_area: float = 0.002  # ... and cover at least this fraction of the frame


def get_settings() -> Settings:
    return Settings(
        meta_api_key=os.environ.get("META_API_KEY", ""),
        erp_url=os.environ.get("ERP_URL", "http://127.0.0.1:8080").rstrip("/"),
        erp_api_key=os.environ.get("ERP_API_KEY", ""),
        erp_api_secret=os.environ.get("ERP_API_SECRET", ""),
        data_dir=Path(os.environ.get("FLOORVOICE_DATA", ROOT / "data")),
        erp_company=os.environ.get("ERP_COMPANY", "FloorVoice Demo Works"),
        erp_default_warehouse=os.environ.get("ERP_DEFAULT_WAREHOUSE", "Stores - FVD"),
        erp_timezone=os.environ.get("ERP_TIMEZONE", "Asia/Colombo"),
        agent_reasoning=os.environ.get("AGENT_REASONING", "low"),
        access_key=os.environ.get("FLOORVOICE_KEY", ""),
        pin_salt=os.environ.get("FLOORVOICE_PIN_SALT", ""),
        video_count=os.environ.get("FEATURE_VIDEO_COUNT", "false").lower() in ("1", "true", "yes"),
        sam_model=os.environ.get("SAM_MODEL", SAM_MODEL),
        count_min_frames=int(os.environ.get("COUNT_MIN_FRAMES", "3")),
        count_min_area=float(os.environ.get("COUNT_MIN_AREA", "0.002")),
    )
