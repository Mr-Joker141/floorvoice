"""FloorVoice HTTP API: /act, /clarify, /history (audio WebSocket arrives on Day 3)."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from agent.loop import AgentResult, AgentState
from agent.normalize import detect_language, token_count
from agent.runtime import Runtime, build_runtime
from server.config import get_settings

log = logging.getLogger("floorvoice")

MIN_TOKENS = 3
PENDING_TTL = 300.0


class ActRequest(BaseModel):
    transcript: str
    session_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    home_warehouse: str | None = None


class ClarifyRequest(BaseModel):
    session_id: str
    answer: str


class ActResponse(BaseModel):
    session_id: str
    kind: str  # done | ask | finish | error | repeat
    text: str
    speech: str
    options: list[str] = []
    documents: list[dict[str, str]] = []
    elapsed_ms: int


@dataclass
class HistoryEntry:
    at: str
    session_id: str
    transcript: str
    kind: str
    text: str
    documents: list[dict[str, str]] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)


@dataclass
class Pending:
    state: AgentState
    entry: HistoryEntry
    created: float = field(default_factory=time.monotonic)


class Store:
    """In-memory pending conversations + history (appended to data/history.jsonl)."""

    def __init__(self, path: Any) -> None:
        self.path = path
        self.pending: dict[str, Pending] = {}
        self.history: list[HistoryEntry] = []
        if path.exists():
            for line in path.read_text().splitlines():
                try:
                    self.history.append(HistoryEntry(**json.loads(line)))
                except (ValueError, TypeError):
                    continue

    def record(self, entry: HistoryEntry) -> None:
        self.history.append(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")

    def take_pending(self, session_id: str) -> Pending | None:
        now = time.monotonic()
        for sid in [s for s, p in self.pending.items() if now - p.created > PENDING_TTL]:
            del self.pending[sid]
        return self.pending.pop(session_id, None)


def _repeat_prompt(transcript: str) -> str:
    if detect_language(transcript) == "ta":
        return "சரியாக கேட்கவில்லை. மீண்டும் சொல்லுங்கள்."
    return "I didn't catch that. Please say it again."


def _docs(result: AgentResult) -> list[dict[str, str]]:
    return [{"doctype": p.doctype, "name": p.name, "summary": p.summary} for p in result.posted]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    rt = build_runtime(settings)
    app.state.rt = rt
    app.state.store = Store(settings.data_dir / "history.jsonl")
    try:
        await rt.cache.get()
    except Exception:  # ERPNext may be starting; the cache retries on first request
        log.exception("could not preload ERPNext masters")
    yield
    await rt.aclose()


app = FastAPI(title="FloorVoice", lifespan=lifespan)


def _rt() -> Runtime:
    return app.state.rt  # type: ignore[no-any-return]


def _store() -> Store:
    return app.state.store  # type: ignore[no-any-return]


def _respond(
    session_id: str, result: AgentResult, entry: HistoryEntry, t0: float, state: AgentState
) -> ActResponse:
    entry.kind = result.kind
    entry.text = result.text
    entry.documents = _docs(result)
    if result.kind == "ask":
        _store().pending[session_id] = Pending(state, entry)
    else:
        _store().record(entry)
    elapsed = int((time.monotonic() - t0) * 1000)
    log.info("%s %s %dms %r -> %r", session_id[:8], result.kind, elapsed, entry.transcript,
             result.text)  # fmt: skip
    return ActResponse(
        session_id=session_id,
        kind=result.kind,
        text=result.text,
        speech=result.speech,
        options=result.options,
        documents=entry.documents,
        elapsed_ms=elapsed,
    )


@app.post("/act", response_model=ActResponse)
async def act(req: ActRequest) -> ActResponse:
    t0 = time.monotonic()
    transcript = req.transcript.strip()
    if token_count(transcript) < MIN_TOKENS:
        text = _repeat_prompt(transcript)
        return ActResponse(session_id=req.session_id, kind="repeat", text=text, speech=text,
                           elapsed_ms=int((time.monotonic() - t0) * 1000))  # fmt: skip
    _store().pending.pop(req.session_id, None)  # a new utterance abandons any open question
    entry = HistoryEntry(
        at=datetime.now().isoformat(timespec="seconds"),
        session_id=req.session_id,
        transcript=transcript,
        kind="",
        text="",
    )
    try:
        state, result = await _rt().agent.act(transcript, home_warehouse=req.home_warehouse)
    except Exception as e:
        log.exception("agent failed for %r", transcript)
        raise HTTPException(502, f"Agent error: {e}") from e
    return _respond(req.session_id, result, entry, t0, state)


@app.post("/clarify", response_model=ActResponse)
async def clarify(req: ClarifyRequest) -> ActResponse:
    t0 = time.monotonic()
    pending = _store().take_pending(req.session_id)
    if pending is None:
        raise HTTPException(404, "No open question for this session")
    pending.entry.answers.append(req.answer)
    try:
        result = await _rt().agent.clarify(pending.state, req.answer)
    except Exception as e:
        log.exception("agent failed on clarify %r", req.answer)
        raise HTTPException(502, f"Agent error: {e}") from e
    return _respond(req.session_id, result, pending.entry, t0, pending.state)


@app.get("/history")
async def history(limit: int = 50) -> list[dict[str, Any]]:
    return [asdict(h) for h in reversed(_store().history[-limit:])]


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
