"""WS /audio: relay push-to-talk PCM to Muse Voice Transcribe, then act on the transcript.

Client protocol (one utterance per connection):
  -> {"type": "start", "session_id": str, "mode": "act"|"clarify", "sample_rate": 24000,
      "home_warehouse": str|null}
  -> binary frames: s16le mono PCM at sample_rate
  -> {"type": "stop"}            (or {"type": "cancel"} to drop the utterance)
  <- {"type": "partial", "text": str}         while speaking
  <- {"type": "final", "text": str}           after stop
  <- {"type": "result", ...ActResponse}       agent outcome
  <- {"type": "error", "message": str}
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import WebSocket, WebSocketDisconnect

from erp.masters import keywords_from_masters
from server.asr import AsrError, RealtimeSession, pcm_to_wav

if TYPE_CHECKING:
    from server.app import Service

log = logging.getLogger("floorvoice.voice")

FINAL_TIMEOUT = 10.0
MAX_UTTERANCE_S = 60


async def asr_keywords(svc: Service) -> list[str]:
    m = await svc.rt.cache.get()
    return keywords_from_masters(m.items, m.warehouses, m.job_cards)


async def handle_audio(ws: WebSocket, svc: Service) -> None:
    await ws.accept()
    try:
        start = await ws.receive_json()
    except (WebSocketDisconnect, ValueError):
        return
    if start.get("type") != "start":
        await ws.close(code=1008)
        return
    session_id = str(start.get("session_id") or "")[:64]
    mode = start.get("mode", "act")
    rate = int(start.get("sample_rate", 24000))
    home = start.get("home_warehouse")
    if not session_id or rate not in (16000, 24000):
        await _send(ws, {"type": "error", "message": "bad start message"})
        await ws.close(code=1008)
        return

    asr = RealtimeSession(
        svc.settings.meta_api_key, sample_rate=rate, keywords=await asr_keywords(svc)
    )
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    opened = asyncio.Event()
    pcm = bytearray()
    t_release = 0.0

    async def forward() -> None:
        await asr.open()
        opened.set()
        while (chunk := await queue.get()) is not None:
            await asr.send_audio(chunk)
        await asr.end()

    async def transcripts() -> str:
        await opened.wait()
        final = ""
        async for ev in asr.events():
            if ev.final:
                final = ev.text
            else:
                await _send(ws, {"type": "partial", "text": ev.text})
        return final

    fwd = asyncio.create_task(forward())
    evs = asyncio.create_task(transcripts())
    cancelled = False
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                cancelled = True
                break
            if (data := msg.get("bytes")) is not None:
                if len(pcm) < rate * 2 * MAX_UTTERANCE_S:
                    pcm.extend(data)
                    queue.put_nowait(data)
            elif (text := msg.get("text")) is not None:
                kind = _json_type(text)
                if kind == "stop":
                    t_release = time.monotonic()
                    queue.put_nowait(None)
                    break
                if kind == "cancel":
                    cancelled = True
                    break
            if fwd.done() and fwd.exception():
                break  # ASR failed to open; report below

        if cancelled:
            return
        try:
            await fwd
            final = await asyncio.wait_for(evs, FINAL_TIMEOUT)
        except (AsrError, TimeoutError, OSError) as e:
            log.error("ASR failed for session %s: %s", session_id[:8], e)
            await _send(ws, {"type": "error", "message": "Speech recognition failed. Try again."})
            return
        t_final = time.monotonic()
        await _send(ws, {"type": "final", "text": final})
        log.info(
            "%s asr final %.2fs after release, %.1fs audio: %r",
            session_id[:8], t_final - t_release, len(pcm) / (rate * 2), final,
        )  # fmt: skip
        audio_path = _save_wav(svc, session_id, bytes(pcm), rate)
        try:
            if mode == "clarify" and svc.has_pending(session_id):
                resp = await svc.clarify(session_id, final)
            else:
                resp = await svc.act(final, session_id, home)
        except Exception as e:  # AgentFailure, ERP down, ...
            log.exception("act failed for %r", final)
            await _send(ws, {"type": "error", "message": f"Could not complete: {e}"})
            return
        payload: dict[str, Any] = {"type": "result", **resp.model_dump()}
        payload["asr_ms"] = int((t_final - t_release) * 1000)
        payload["total_ms"] = int((time.monotonic() - t_release) * 1000)
        payload["audio"] = audio_path.name if audio_path else None
        await _send(ws, payload)
    except WebSocketDisconnect:
        pass
    finally:
        for task in (fwd, evs):
            task.cancel()
        for task in (fwd, evs):
            with contextlib.suppress(BaseException):
                await task
        await asr.close()
        with contextlib.suppress(Exception):
            await ws.close()


def _json_type(text: str) -> str:
    try:
        return str(json.loads(text).get("type", ""))
    except (ValueError, AttributeError):
        return ""


async def _send(ws: WebSocket, msg: dict[str, Any]) -> None:
    with contextlib.suppress(Exception):
        await ws.send_json(msg)


def _save_wav(svc: Service, session_id: str, pcm: bytes, rate: int) -> Path | None:
    if not pcm:
        return None
    folder = svc.settings.data_dir / "audio"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{time.strftime('%Y%m%d-%H%M%S')}-{session_id[:12]}.wav"
    path.write_bytes(pcm_to_wav(pcm, rate))
    return path
