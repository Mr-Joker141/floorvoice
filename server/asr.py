"""Meta Muse Voice Transcribe client: realtime WebSocket session and file endpoint."""

from __future__ import annotations

import io
import json
import logging
import uuid
import wave
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
import websockets
from websockets.asyncio.client import ClientConnection

from server.config import ASR_MODEL, META_API_BASE, META_ASR_REALTIME_URL

log = logging.getLogger(__name__)

DEFAULT_LANGUAGES = ("Tamil", "English")


class AsrError(RuntimeError):
    pass


@dataclass
class TranscriptEvent:
    text: str
    final: bool
    raw: dict[str, Any] = field(default_factory=dict)


class RealtimeSession:
    """One push-to-talk utterance: open, stream PCM, end, read the final transcript."""

    def __init__(
        self,
        api_key: str,
        *,
        sample_rate: int = 24000,
        keywords: Sequence[str] = (),
        languages: Sequence[str] = DEFAULT_LANGUAGES,
    ) -> None:
        if sample_rate not in (16000, 24000):
            raise ValueError("sample_rate must be 16000 or 24000")
        self.api_key = api_key
        self.sample_rate = sample_rate
        self.keywords = list(keywords)
        self.languages = list(languages)
        self.session_id = uuid.uuid4().hex
        self._ws: ClientConnection | None = None

    def handshake(self) -> dict[str, Any]:
        msg: dict[str, Any] = {
            "authorization": {"accessToken": f"Bearer {self.api_key}"},
            "audioEncoding": f"PCM_{self.sample_rate // 1000}KHZ",
            "model": ASR_MODEL,
            "mode": "PUSH_TO_TALK",
            "partialMode": "CUMULATIVE",
            "emitAudioProgress": False,
        }
        if self.languages:
            msg["languageBias"] = self.languages
        if self.keywords:
            msg["keywords"] = self.keywords
        return msg

    async def open(self) -> None:
        url = f"{META_ASR_REALTIME_URL}?sessionId={self.session_id}"
        self._ws = await websockets.connect(url, max_size=2**22)
        await self._ws.send(json.dumps(self.handshake()))
        ack = json.loads(await self._ws.recv())
        if ack.get("type") == "error" or "sessionId" not in ack:
            raise AsrError(f"ASR handshake rejected: {ack}")
        log.debug("asr session open %s", ack)

    async def send_audio(self, pcm: bytes) -> None:
        assert self._ws is not None
        await self._ws.send(pcm)

    async def end(self) -> None:
        assert self._ws is not None
        await self._ws.send(json.dumps({"type": "endStream"}))

    async def events(self) -> AsyncIterator[TranscriptEvent]:
        """Yield transcript events until the final one (or the server closes)."""
        assert self._ws is not None
        try:
            async for frame in self._ws:
                if isinstance(frame, bytes):
                    continue
                msg = json.loads(frame)
                kind = msg.get("type")
                if kind == "transcript":
                    ev = TranscriptEvent(msg.get("transcript", ""), bool(msg.get("final")), msg)
                    yield ev
                    if ev.final:
                        return
                elif kind == "error":
                    raise AsrError(msg.get("message", "ASR error"))
                else:
                    log.debug("asr event %s", msg)
        except websockets.ConnectionClosed as exc:
            if exc.rcvd is None or exc.rcvd.code != 1000:
                raise AsrError(f"ASR connection closed: {exc}") from exc

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None


def wav_to_pcm(wav_bytes: bytes) -> tuple[bytes, int]:
    """Return (raw s16le mono PCM, sample rate) from a WAV file."""
    with wave.open(io.BytesIO(wav_bytes)) as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError("WAV must be mono 16-bit")
        return w.readframes(w.getnframes()), w.getframerate()


def pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


async def transcribe_file(
    api_key: str,
    wav_bytes: bytes,
    *,
    keywords: Sequence[str] = (),
    languages: Sequence[str] = DEFAULT_LANGUAGES,
    timeout: float = 60.0,
) -> str:
    """Transcribe a whole WAV (mono 16-bit, 16/24 kHz) via POST /asr/transcribe."""
    request: dict[str, Any] = {
        "model": ASR_MODEL,
        "audioEncoding": "WAV",
        "mode": "PUSH_TO_TALK",
    }
    if languages:
        request["languageBias"] = list(languages)
    if keywords:
        request["keywords"] = list(keywords)
    files = {
        "request": (None, json.dumps(request), "application/json"),
        "audio": ("audio.wav", wav_bytes, "audio/wav"),
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{META_API_BASE}/asr/transcribe",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            files=files,
        )
    if resp.status_code != 200:
        log.error("asr transcribe %s: %s", resp.status_code, resp.text)
        raise AsrError(f"ASR transcribe failed ({resp.status_code}): {resp.text[:300]}")
    return str(resp.json().get("transcript", ""))
