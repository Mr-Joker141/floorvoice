"""FloorVoice API: WS /audio, POST /act, POST /clarify, GET /history, and the PWA."""

from __future__ import annotations

import hmac
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent.loop import AgentResult, AgentState
from agent.normalize import detect_language, token_count
from agent.runtime import Runtime, build_runtime
from server.config import ROOT, Settings, get_settings

log = logging.getLogger("floorvoice")

MIN_TOKENS = 3
PENDING_TTL = 300.0
KEY_HEADER = "X-FloorVoice-Key"


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
    transcript: str = ""
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

    def __init__(self, path: Path) -> None:
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


class AgentFailure(RuntimeError):
    pass


def _repeat_prompt(transcript: str) -> str:
    if detect_language(transcript) == "ta":
        return "சரியாக கேட்கவில்லை. மீண்டும் சொல்லுங்கள்."
    return "I didn't catch that. Please say it again."


def _docs(result: AgentResult) -> list[dict[str, str]]:
    return [{"doctype": p.doctype, "name": p.name, "summary": p.summary} for p in result.posted]


class Service:
    """Shared act/clarify logic for the HTTP endpoints and the audio WebSocket."""

    def __init__(self, settings: Settings, rt: Runtime, store: Store) -> None:
        self.settings = settings
        self.rt = rt
        self.store = store

    def has_pending(self, session_id: str) -> bool:
        return session_id in self.store.pending

    def _respond(
        self,
        session_id: str,
        result: AgentResult,
        entry: HistoryEntry,
        t0: float,
        state: AgentState,
        transcript: str,
    ) -> ActResponse:
        entry.kind = result.kind
        entry.text = result.text
        entry.documents = _docs(result)
        if result.kind == "ask":
            self.store.pending[session_id] = Pending(state, entry)
        else:
            self.store.record(entry)
        elapsed = int((time.monotonic() - t0) * 1000)
        log.info("%s %s %dms %r -> %r", session_id[:8], result.kind, elapsed, transcript,
                 result.text)  # fmt: skip
        return ActResponse(
            session_id=session_id,
            kind=result.kind,
            text=result.text,
            speech=result.speech,
            options=result.options,
            documents=entry.documents,
            transcript=transcript,
            elapsed_ms=elapsed,
        )

    async def act(
        self, transcript: str, session_id: str, home_warehouse: str | None = None
    ) -> ActResponse:
        t0 = time.monotonic()
        transcript = transcript.strip()
        if token_count(transcript) < MIN_TOKENS:
            text = _repeat_prompt(transcript)
            return ActResponse(session_id=session_id, kind="repeat", text=text, speech=text,
                               transcript=transcript, elapsed_ms=0)  # fmt: skip
        self.store.pending.pop(session_id, None)  # a new utterance abandons an open question
        entry = HistoryEntry(
            at=datetime.now().isoformat(timespec="seconds"),
            session_id=session_id,
            transcript=transcript,
            kind="",
            text="",
        )
        try:
            state, result = await self.rt.agent.act(transcript, home_warehouse=home_warehouse)
        except Exception as e:
            log.exception("agent failed for %r", transcript)
            raise AgentFailure(str(e)) from e
        return self._respond(session_id, result, entry, t0, state, transcript)

    async def clarify(self, session_id: str, answer: str) -> ActResponse:
        t0 = time.monotonic()
        answer = answer.strip()
        if not answer:
            text = _repeat_prompt(answer)
            return ActResponse(session_id=session_id, kind="repeat", text=text, speech=text,
                               elapsed_ms=0)  # fmt: skip
        pending = self.store.take_pending(session_id)
        if pending is None:
            raise KeyError(session_id)
        pending.entry.answers.append(answer)
        try:
            result = await self.rt.agent.clarify(pending.state, answer)
        except Exception as e:
            log.exception("agent failed on clarify %r", answer)
            raise AgentFailure(str(e)) from e
        return self._respond(session_id, result, pending.entry, t0, pending.state, answer)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for noisy in ("httpx", "httpx2", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    settings = get_settings()
    rt = build_runtime(settings)
    app.state.service = Service(settings, rt, Store(settings.data_dir / "history.jsonl"))
    try:
        await rt.cache.get()
    except Exception:  # ERPNext may be starting; the cache retries on first request
        log.exception("could not preload ERPNext masters")
    yield
    await rt.aclose()


app = FastAPI(title="FloorVoice", lifespan=lifespan)


def service() -> Service:
    return app.state.service  # type: ignore[no-any-return]


def key_ok(given: str | None) -> bool:
    expected = get_settings().access_key
    return not expected or hmac.compare_digest((given or "").encode(), expected.encode())


def require_key(request: Request) -> None:
    if not key_ok(request.headers.get(KEY_HEADER) or request.query_params.get("k")):
        raise HTTPException(401, "Missing or wrong access key")


@app.post("/act", response_model=ActResponse, dependencies=[Depends(require_key)])
async def act(req: ActRequest) -> ActResponse:
    try:
        return await service().act(req.transcript, req.session_id, req.home_warehouse)
    except AgentFailure as e:
        raise HTTPException(502, f"Agent error: {e}") from e


@app.post("/clarify", response_model=ActResponse, dependencies=[Depends(require_key)])
async def clarify(req: ClarifyRequest) -> ActResponse:
    try:
        return await service().clarify(req.session_id, req.answer)
    except KeyError as e:
        raise HTTPException(404, "No open question for this session") from e
    except AgentFailure as e:
        raise HTTPException(502, f"Agent error: {e}") from e


@app.get("/history", dependencies=[Depends(require_key)])
async def history(limit: int = 50) -> list[dict[str, Any]]:
    return [asdict(h) for h in reversed(service().store.history[-limit:])]


@app.get("/warehouses", dependencies=[Depends(require_key)])
async def warehouses() -> list[dict[str, str]]:
    m = await service().rt.cache.get()
    return [{"name": w["name"], "label": w["warehouse_name"]} for w in m.warehouses]


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.websocket("/audio")
async def audio(ws: WebSocket) -> None:
    from server.voice import handle_audio  # local import keeps app import light for tests

    if not key_ok(ws.query_params.get("k")):
        await ws.close(code=4401, reason="Missing or wrong access key")
        return
    await handle_audio(ws, service())


app.mount("/", StaticFiles(directory=ROOT / "pwa", html=True), name="pwa")
