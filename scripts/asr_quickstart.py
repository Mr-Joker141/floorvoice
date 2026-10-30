"""Transcribe WAV files with Muse Voice Transcribe via both the file and realtime APIs.

Usage: uv run python -m scripts.asr_quickstart samples/*.wav [--keywords-file seed/keywords.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

from server.asr import RealtimeSession, transcribe_file, wav_to_pcm
from server.config import get_settings

FRAME_MS = 80


async def realtime(api_key: str, wav: bytes, keywords: list[str]) -> str:
    pcm, rate = wav_to_pcm(wav)
    session = RealtimeSession(api_key, sample_rate=rate, keywords=keywords)
    await session.open()
    frame = rate * 2 * FRAME_MS // 1000

    async def pump() -> None:
        for i in range(0, len(pcm), frame):
            await session.send_audio(pcm[i : i + frame])
            await asyncio.sleep(FRAME_MS / 1000)
        await session.end()

    sender = asyncio.create_task(pump())
    final = ""
    try:
        async for ev in session.events():
            print(f"    {'FINAL' if ev.final else 'part '}: {ev.text}")
            final = ev.text
    finally:
        await sender
        await session.close()
    return final


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("wavs", nargs="+", type=Path)
    ap.add_argument("--keywords-file", type=Path)
    ap.add_argument("--mode", choices=["file", "realtime", "both"], default="both")
    args = ap.parse_args()
    key = get_settings().meta_api_key
    if not key:
        print("META_API_KEY is not set", file=sys.stderr)
        return 2
    keywords = json.loads(args.keywords_file.read_text()) if args.keywords_file else []
    for path in args.wavs:
        wav = path.read_bytes()
        print(f"== {path}")
        if args.mode in ("file", "both"):
            t = time.monotonic()
            text = await transcribe_file(key, wav, keywords=keywords)
            print(f"  file     ({time.monotonic() - t:.2f}s): {text}")
        if args.mode in ("realtime", "both"):
            t = time.monotonic()
            text = await realtime(key, wav, keywords)
            print(f"  realtime ({time.monotonic() - t:.2f}s): {text}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(main()))
