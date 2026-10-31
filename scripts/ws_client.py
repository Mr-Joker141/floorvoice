"""Simulate the PWA: stream a WAV over WS /audio in real time and print what comes back.

Usage: uv run python -m scripts.ws_client samples/en_transfer.wav [--url ws://127.0.0.1:8765]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from pathlib import Path

import websockets

from server.asr import wav_to_pcm
from server.config import get_settings

FRAME_MS = 80


async def run(
    url: str, wav: Path, session_id: str, mode: str, speech: str = "", pin: str = "1111"
) -> dict:
    pcm, rate = wav_to_pcm(wav.read_bytes())
    key = get_settings().access_key
    async with websockets.connect(f"{url}/audio?k={key}") as ws:
        await ws.send(json.dumps({"type": "start", "session_id": session_id, "mode": mode,
                                  "sample_rate": rate, "pin": pin,
                                  "speech": speech}))  # fmt: skip
        frame = rate * 2 * FRAME_MS // 1000
        for i in range(0, len(pcm), frame):
            await ws.send(pcm[i : i + frame])
            await asyncio.sleep(FRAME_MS / 1000)
        t = time.monotonic()
        await ws.send(json.dumps({"type": "stop"}))
        async for raw in ws:
            msg = json.loads(raw)
            stamp = f"+{time.monotonic() - t:.2f}s"
            if msg["type"] == "partial":
                continue
            print(stamp, json.dumps(msg, ensure_ascii=False))
            if msg["type"] in ("result", "error"):
                return msg
    return {}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("wav", type=Path)
    ap.add_argument("--url", default="ws://127.0.0.1:8765")
    ap.add_argument(
        "--speech",
        default="",
        choices=("", "ta", "si", "en"),
        help="language override; default: the worker's",
    )
    ap.add_argument("--pin", default="1111", help="worker PIN (seeded demo workers: 1111-6666)")
    ap.add_argument("--answer-wav", type=Path, help="spoken answer if the agent asks")
    args = ap.parse_args()
    sid = uuid.uuid4().hex
    r = await run(args.url, args.wav, sid, "act", args.speech, args.pin)
    if r.get("kind") == "ask" and args.answer_wav:
        await run(args.url, args.answer_wav, sid, "clarify", args.speech, args.pin)


if __name__ == "__main__":
    asyncio.run(main())
