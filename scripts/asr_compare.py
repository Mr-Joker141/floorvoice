"""Compare Voice Transcribe settings on recorded clips (file endpoint, repeated runs).

Usage: uv run python -m scripts.asr_compare data/audio/a.wav data/audio/b.wav [--runs 3]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from server.asr import AsrError, transcribe_file
from server.config import get_settings

FULL = json.loads(Path("seed/keywords.json").read_text())
# Only what the demo phrases need: numbers-as-codes, places, verbs.
SHORT = ["bearing", "6201", "6204", "6205", "6305", "store", "stores", "line one", "line two",
         "job card", "pieces", "மாத்து", "இருந்து", "லைன்", "ஸ்டோர்"]  # fmt: skip

CONFIGS = {
    "default (ta+en, 169 kw)": {"languages": ("Tamil", "English"), "keywords": FULL},
    "ta+en, no keywords": {"languages": ("Tamil", "English"), "keywords": ()},
    "ta+en, 15 keywords": {"languages": ("Tamil", "English"), "keywords": SHORT},
    "Tamil only, 169 kw": {"languages": ("Tamil",), "keywords": FULL},
    "no language bias, 169 kw": {"languages": (), "keywords": FULL},
    "en+ta order, 169 kw": {"languages": ("English", "Tamil"), "keywords": FULL},
}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("wavs", nargs="+", type=Path)
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()
    key = get_settings().meta_api_key
    for wav in args.wavs:
        data = wav.read_bytes()
        print(f"== {wav.name}")
        for name, cfg in CONFIGS.items():
            outs = []
            for _ in range(args.runs):
                try:
                    outs.append(await transcribe_file(key, data, **cfg))  # type: ignore[arg-type]
                except AsrError as e:
                    outs.append(f"ERROR {e}")
            print(f"  {name:28s}", " | ".join(outs))


if __name__ == "__main__":
    asyncio.run(main())
