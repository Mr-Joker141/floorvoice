"""FloorVoice API: WS /audio, POST /act /clarify /clarify-photo /undo, GET /me /history, PWA."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent.count import (
    COUNT_BUDGET_S,
    COUNT_FPS,
    count_objects,
    extract_frame,
    parse_sam_text,
    prepare_clip,
)
from agent.loop import AgentResult, AgentState, count_prompt
from agent.normalize import (
    detect_language,
    looks_roman_sinhala,
    normalize_numbers,
    reply_language,
    token_count,
)
from agent.readback import readback
from agent.runtime import Runtime, build_runtime
from agent.tools import PostedDoc, ToolContext, ToolError
from agent.vision import identify
from erp.client import ErpError
from erp.masters import hash_pin
from server.config import ROOT, Settings, get_settings

log = logging.getLogger("floorvoice")

MIN_TOKENS = 3
PHOTO_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
MAX_PHOTO_BYTES = 8_000_000
VIDEO_TYPES = {"video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
               "video/x-matroska": ".mkv"}  # fmt: skip
MAX_VIDEO_BYTES = 80_000_000
COUNT_ACTIONS = ("post", "adjust", "retake", "cancel")
_FRAME_NAME = re.compile(r"^[\w.-]+-count(-\d+)?\.jpg$")
PENDING_TTL = 300.0
KEY_HEADER = "X-FloorVoice-Key"
PIN_HEADER = "X-FloorVoice-Pin"
PIN_UNKNOWN = "PIN not recognised"


class ActRequest(BaseModel):
    transcript: str
    session_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    speech: str = ""  # manual language override (ta | si | en); default: the worker's


class ClarifyRequest(BaseModel):
    session_id: str
    answer: str
    via: str = "voice"  # how the worker answered: voice | chip (tapped option)


class ActResponse(BaseModel):
    session_id: str
    kind: str  # done | ask | finish | error | repeat
    text: str
    speech: str
    options: list[str] = []
    documents: list[dict[str, str]] = []
    transcript: str = ""
    elapsed_ms: int
    language: str = ""
    photo: bool = False  # the open question is over an item: offer "Show me the part"
    label_text: str = ""  # what was read on the label, when a photo could not decide
    # Stock count. kind "count": film the shelf (count = what and where). kind "review":
    # the number to confirm; options are labels, actions the matching /count-action verbs.
    count: dict[str, Any] | None = None
    review: dict[str, Any] | None = None
    actions: list[str] = []


@dataclass
class HistoryEntry:
    at: str
    session_id: str
    transcript: str
    kind: str
    text: str
    documents: list[dict[str, str]] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)
    language: str = ""
    # How the item was settled: auto (no item question) | voice | chip | photo.
    resolved_by: str = "auto"
    audio: str = ""  # WAV of the press in data/audio/
    photos: list[str] = field(default_factory=list)  # label photos, next to the WAV
    worker_id: str = ""  # Employee who spoke
    worker_name: str = ""
    # Stock count by video: ERPNext's quantity, the video count, what was posted, the clip.
    count_system: float | None = None
    count_video: int | None = None
    count_posted: float | None = None
    clips: list[str] = field(default_factory=list)


@dataclass
class Pending:
    state: AgentState
    entry: HistoryEntry
    created: float = field(default_factory=time.monotonic)


@dataclass
class CountPending:
    """A stock count between "film the shelf" and the worker's confirm tap."""

    req: dict[str, Any]  # item_code, item_name, warehouse, warehouse_name, system_qty, ...
    entry: HistoryEntry
    ctx: ToolContext
    language: str
    stage: str = "capture"  # capture | review | adjust
    counted: int | None = None  # distinct objects SAM tracked
    proposed: float | None = None  # the number on the "Post N" chip
    params: str = ""  # counting parameters, for Remarks
    created: float = field(default_factory=time.monotonic)


class Store:
    """In-memory pending conversations + history (appended to data/history.jsonl)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.pending: dict[str, Pending] = {}
        self.counts: dict[str, CountPending] = {}
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

    def save_all(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(asdict(h), ensure_ascii=False) + "\n" for h in self.history]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("".join(lines))
        tmp.replace(self.path)

    def find_doc(self, doctype: str, name: str) -> tuple[HistoryEntry, dict[str, str]] | None:
        for entry in reversed(self.history):
            for doc in entry.documents:
                if doc["doctype"] == doctype and doc["name"] == name:
                    return entry, doc
        return None

    def take_pending(self, session_id: str) -> Pending | None:
        now = time.monotonic()
        for sid in [s for s, p in self.pending.items() if now - p.created > PENDING_TTL]:
            del self.pending[sid]
        return self.pending.pop(session_id, None)


class CountStateError(ValueError):
    """A count action that does not fit the count's stage (e.g. post before review)."""


class AgentFailure(RuntimeError):
    pass


class UnknownWorker(LookupError):
    pass


class NotYours(PermissionError):
    """Another worker's question or document."""


def _repeat_prompt(transcript: str, speech: str = "") -> str:
    lang = reply_language(transcript, speech)
    if lang == "ta":
        return "சரியாக கேட்கவில்லை. மீண்டும் சொல்லுங்கள்."
    if lang == "si":
        return "හරියට ඇහුණේ නැහැ. ආයෙත් කියන්න."
    return "I didn't catch that. Please say it again."


def _unreadable(lang: str) -> str:
    return {
        "ta": "லேபிளைப் படிக்க முடியவில்லை — ஒன்றைத் தட்டுங்கள்",
        "si": "ලේබලය කියවන්න බැරි වුණා — එකක් තට්ටු කරන්න",
    }.get(lang, "Couldn't read the label — tap one")


def _count_texts(lang: str) -> dict[str, str]:
    if lang == "ta":
        return {"review": "நான் எண்ணியது {n}. ERPNext-ல் {wh}-ல் {sys} உள்ளது.",
                "how_many": "எத்தனை உள்ளன? எண்ணைச் சொல்லுங்கள்.",
                "failed": "எண்ண முடியவில்லை — மீண்டும் படம் பிடிக்கவும் அல்லது எண்ணைச் சொல்லவும்.",
                "manual": "{item}-க்கு வீடியோ விளக்கம் இல்லை — எண்ணைச் சொல்லுங்கள்.",
                "confirm": "{n} பதிவு செய்யவா? ERPNext-ல் {wh}-ல் {sys} உள்ளது.",
                "cancelled": "எண்ணிக்கை ரத்து — எதுவும் பதிவு செய்யப்படவில்லை."}  # fmt: skip
    if lang == "si":
        return {"review": "මම ගණන් කළේ {n}. ERPNext හි {wh} තුළ {sys} ඇත.",
                "how_many": "කීයක් තියෙනවද? ගණන කියන්න.",
                "failed": "ගණන් කරන්න බැරි වුණා — නැවත වීඩියෝ කරන්න හෝ ගණන කියන්න.",
                "manual": "{item} සඳහා වීඩියෝ විස්තරයක් නැහැ — ගණන කියන්න.",
                "confirm": "{n} සටහන් කරන්නද? ERPNext හි {wh} තුළ {sys} ඇත.",
                "cancelled": "ගණන් කිරීම අවලංගුයි — කිසිවක් සටහන් කළේ නැහැ."}  # fmt: skip
    return {"review": "I count {n}. ERPNext has {sys} in {wh}.",
            "how_many": "How many are there? Say or type the number.",
            "failed": "Couldn't count that — retake or enter the number.",
            "manual": "No video description for {item} yet — say or type the count.",
            "confirm": "Post {n}? ERPNext has {sys} in {wh}.",
            "cancelled": "Count cancelled — nothing posted."}  # fmt: skip


def _num(q: float | None) -> str:
    if q is None:
        return "?"
    return str(int(q)) if q == int(q) else f"{q:g}"


def erp_public_url() -> str:
    """Public ERPNext desk URL for links (env ERP_PUBLIC_URL or scripts/erp_tunnel.sh)."""
    if url := os.environ.get("ERP_PUBLIC_URL"):
        return url.rstrip("/")
    try:
        return (get_settings().data_dir / "erp_public_url").read_text().strip().rstrip("/")
    except OSError:
        return ""


def erp_link(doctype: str, name: str) -> str:
    base = erp_public_url()
    return f"{base}/app/{doctype.lower().replace(' ', '-')}/{name}" if base else ""


def _docs(result: AgentResult) -> list[dict[str, str]]:
    return [
        {"doctype": p.doctype, "name": p.name, "summary": p.summary,
         "url": erp_link(p.doctype, p.name), "undo": json.dumps(p.undo)}
        for p in result.posted
    ]  # fmt: skip


class Service:
    """Shared act/clarify logic for the HTTP endpoints and the audio WebSocket."""

    def __init__(self, settings: Settings, rt: Runtime, store: Store) -> None:
        self.settings = settings
        self.rt = rt
        self.store = store

    def has_pending(self, session_id: str) -> bool:
        cp = self.store.counts.get(session_id)
        return session_id in self.store.pending or (cp is not None and cp.stage == "adjust")

    async def worker(self, pin: str | None) -> dict[str, Any]:
        """Employee for a PIN, from the masters cache (60 s). UnknownWorker if none."""
        pin = (pin or "").strip()
        if not (pin.isdigit() and len(pin) == 4 and self.settings.pin_salt):
            raise UnknownWorker(PIN_UNKNOWN)
        found = (await self.rt.cache.get()).worker(hash_pin(pin, self.settings.pin_salt))
        if found is None:
            raise UnknownWorker(PIN_UNKNOWN)
        return found

    def _owned_pending(self, session_id: str, worker: dict[str, Any] | None) -> Pending | None:
        pending = self.store.pending.get(session_id)
        if pending and worker and pending.entry.worker_id not in ("", worker["name"]):
            raise NotYours("This question belongs to another worker")
        return pending

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
        entry.language = state.language
        entry.text = result.text
        entry.documents = _docs(result)
        count = review = None
        if result.kind == "ask":
            self.store.pending[session_id] = Pending(state, entry)
        elif result.kind == "count" and result.count is not None:
            cp = CountPending(result.count, entry, state.ctx, state.language)
            entry.count_system = float(result.count["system_qty"])
            self.store.counts[session_id] = cp
            count = self._count_view(cp)
            if not result.count.get("prompt") or self.rt.sam is None:
                # Nothing to tell SAM what to look for: the worker gives the number.
                cp.stage = "adjust"
                text = _count_texts(state.language)["manual"].format(item=cp.req["item_name"])
                result = AgentResult("ask", text)
                entry.kind = "count"
                count["manual"] = True
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
            language=state.language,
            photo=result.kind == "ask" and bool(result.photo_items),
            count=count,
            review=review,
        )

    async def act(
        self,
        transcript: str,
        session_id: str,
        home_warehouse: str | None = None,
        *,
        alt_transcript: asyncio.Future[str] | None = None,
        speech: str = "",
        audio: str = "",
        worker: dict[str, Any] | None = None,
        typed: bool = False,
    ) -> ActResponse:
        """speech: manual language override; otherwise the worker's preferred language.
        Typed Sinhala or Tamil (including Sinhala in English letters, "mata bearing 10k
        ewanna") is what the worker meant, unlike ASR output, which can come back in the
        wrong script, so it picks the reply language.
        The worker's home line is the default destination / requesting warehouse."""
        t0 = time.monotonic()
        if typed:
            written = "si" if looks_roman_sinhala(transcript) else detect_language(transcript)
            speech = written if written in ("si", "ta") else speech
        if worker:
            home_warehouse = worker.get("floorvoice_home_warehouse") or home_warehouse
            speech = speech or worker.get("floorvoice_language") or ""
        transcript = transcript.strip()
        if token_count(transcript) < MIN_TOKENS:
            text = _repeat_prompt(transcript, speech)
            return ActResponse(session_id=session_id, kind="repeat", text=text, speech=text,
                               transcript=transcript, elapsed_ms=0)  # fmt: skip
        self.store.pending.pop(session_id, None)  # a new utterance abandons an open question
        self.store.counts.pop(session_id, None)
        entry = HistoryEntry(
            at=datetime.now().isoformat(timespec="seconds"),
            session_id=session_id,
            transcript=transcript,
            kind="",
            text="",
            audio=audio,
            worker_id=str(worker["name"]) if worker else "",
            worker_name=str(worker.get("employee_name") or worker["name"]) if worker else "",
        )
        try:
            state, result = await self.rt.agent.act(
                transcript,
                home_warehouse=home_warehouse,
                alt_transcript=alt_transcript,
                speech=speech,
                worker=worker,
            )
        except Exception as e:
            log.exception("agent failed for %r", transcript)
            raise AgentFailure(str(e)) from e
        return self._respond(session_id, result, entry, t0, state, transcript)

    async def clarify(
        self,
        session_id: str,
        answer: str,
        *,
        via: str = "voice",
        photo_label: str = "",
        worker: dict[str, Any] | None = None,
    ) -> ActResponse:
        """Resume the open question with the worker's answer (spoken, tapped or from a
        label photo, which arrives here as the chip it picked)."""
        t0 = time.monotonic()
        answer = answer.strip()
        cp = self.store.counts.get(session_id)
        if cp is not None and cp.stage == "adjust":
            self._owned_count(session_id, worker)
            return self._count_answer(session_id, cp, answer, t0)
        self._owned_pending(session_id, worker)
        if not answer:
            pending = self.store.pending.get(session_id)
            text = _repeat_prompt(answer, pending.state.language if pending else "")
            return ActResponse(session_id=session_id, kind="repeat", text=text, speech=text,
                               elapsed_ms=0)  # fmt: skip
        pending = self.store.take_pending(session_id)
        if pending is None:
            raise KeyError(session_id)
        pending.entry.answers.append(answer)
        if pending.state.photo_items:  # the question was over an item
            pending.entry.resolved_by = via if via in ("voice", "chip", "photo") else "voice"
        if photo_label:
            pending.state.ctx.photo_label = photo_label
        try:
            result = await self.rt.agent.clarify(pending.state, answer)
        except Exception as e:
            log.exception("agent failed on clarify %r", answer)
            raise AgentFailure(str(e)) from e
        return self._respond(session_id, result, pending.entry, t0, pending.state, answer)

    async def clarify_photo(
        self, session_id: str, image: bytes, mime: str, worker: dict[str, Any] | None = None
    ) -> ActResponse:
        """Answer an open item question from a photo of the part or its label."""
        t0 = time.monotonic()
        pending = self._owned_pending(session_id, worker)
        if pending is None:
            raise KeyError(session_id)
        if not pending.state.photo_items:
            raise ValueError("the open question is not about an item")
        pending.entry.photos.append(self._save_photo(pending.entry, image, mime))
        items = pending.state.photo_items
        match = await identify(self.rt.vision, image, mime, items)
        log.info("%s photo %dms -> %s", session_id[:8], (time.monotonic() - t0) * 1000, match)
        option = next((i["option"] for i in items if i["item_code"] == match.item_code), None)
        if match.usable and option is not None:
            resp = await self.clarify(session_id, option, via="photo",
                                      photo_label=match.label_text, worker=worker)  # fmt: skip
            resp.elapsed_ms = int((time.monotonic() - t0) * 1000)  # include the vision call
            return resp
        # Low confidence, no match or over budget: back to the same question and chips.
        lang = pending.state.language
        text = _unreadable(lang)
        return ActResponse(
            session_id=session_id, kind="ask", text=text, speech=text,
            options=[i["option"] for i in items], transcript=pending.entry.transcript,
            elapsed_ms=int((time.monotonic() - t0) * 1000), language=lang, photo=True,
            label_text=match.label_text,
        )  # fmt: skip

    # --- stock count by video ------------------------------------------------------------

    def _owned_count(self, session_id: str, worker: dict[str, Any] | None) -> CountPending:
        cp = self.store.counts.get(session_id)
        if cp is None:
            raise KeyError(session_id)
        if worker and cp.entry.worker_id not in ("", worker["name"]):
            raise NotYours("This count belongs to another worker")
        if time.monotonic() - cp.created > PENDING_TTL * 2:
            self.store.counts.pop(session_id, None)
            raise KeyError(session_id)
        return cp

    @staticmethod
    def _count_view(cp: CountPending) -> dict[str, Any]:
        r = cp.req
        return {"item_code": r["item_code"], "item_name": r["item_name"],
                "warehouse": r["warehouse"], "warehouse_name": r["warehouse_name"],
                "system_qty": r["system_qty"], "stock_uom": r.get("stock_uom", ""),
                "manual": False}  # fmt: skip

    def _count_reply(
        self, session_id: str, cp: CountPending, kind: str, text: str, t0: float, *,
        options: list[str] | None = None, actions: list[str] | None = None,
        review: dict[str, Any] | None = None, count: dict[str, Any] | None = None,
    ) -> ActResponse:  # fmt: skip
        elapsed = int((time.monotonic() - t0) * 1000)
        log.info("%s count %s %dms -> %r", session_id[:8], kind, elapsed, text)
        return ActResponse(
            session_id=session_id, kind=kind, text=text, speech=text, options=options or [],
            actions=actions or [], transcript=cp.entry.transcript, elapsed_ms=elapsed,
            language=cp.language, review=review, count=count,
        )  # fmt: skip

    def _review(self, session_id: str, cp: CountPending, t0: float,
                picture: dict[str, Any] | None = None) -> ActResponse:  # fmt: skip
        """The mandatory review card: nothing posts until one of these chips is tapped."""
        texts = _count_texts(cp.language)
        fmt = {"n": _num(cp.proposed), "sys": _num(cp.req["system_qty"]),
               "wh": cp.req["warehouse_name"]}  # fmt: skip
        adjusted = cp.counted is None or cp.proposed != cp.counted
        text = (texts["confirm"] if adjusted else texts["review"]).format(**fmt)
        review = {"counted": cp.counted, "proposed": cp.proposed,
                  "system_qty": cp.req["system_qty"], **(picture or {})}  # fmt: skip
        cp.stage = "review"
        return self._count_reply(
            session_id, cp, "review", text, t0, review=review,
            options=[f"Post {_num(cp.proposed)}", "Adjust", "Retake", "Cancel"],
            actions=["post", "adjust", "retake", "cancel"],
        )  # fmt: skip

    def _count_failed(self, session_id: str, cp: CountPending, t0: float) -> ActResponse:
        cp.stage = "capture"
        cp.counted = cp.proposed = None
        return self._count_reply(
            session_id, cp, "review", _count_texts(cp.language)["failed"], t0,
            options=["Retake", "Enter number", "Cancel"], actions=["retake", "adjust", "cancel"],
            review={"counted": None, "proposed": None, "system_qty": cp.req["system_qty"]},
        )  # fmt: skip

    async def count_video(
        self, session_id: str, video: bytes, mime: str, worker: dict[str, Any] | None = None
    ) -> ActResponse:
        """Film -> ffmpeg -> SAM 3.1 -> distinct identities -> review card (never a post)."""
        t0 = time.monotonic()
        cp = self._owned_count(session_id, worker)
        if cp.stage == "adjust" and cp.counted is None and not cp.req.get("prompt"):
            raise CountStateError("this item is counted by entering the number")
        if self.rt.sam is None:
            raise CountStateError("video counting is not enabled")
        folder = self.settings.data_dir / "audio"
        folder.mkdir(parents=True, exist_ok=True)
        stem = self._stem(cp.entry, folder, "-count")
        original = folder / f"{stem}-orig{VIDEO_TYPES.get(mime, '.mp4')}"
        prepared = folder / f"{stem}.mp4"
        frame_jpg = folder / f"{stem}.jpg"
        original.write_bytes(video)
        cp.entry.clips.append(prepared.name)
        sam = self.rt.sam
        prompt = cp.req["prompt"]

        async def pipeline() -> tuple[Any, int]:
            await prepare_clip(original, prepared)
            text, billed = await sam(prepared.read_bytes(), prompt)
            result = count_objects(parse_sam_text(text), min_frames=self.settings.count_min_frames,
                                   min_area=self.settings.count_min_area)  # fmt: skip
            if result.ok and result.best_frame is not None:
                await extract_frame(prepared, result.best_frame, frame_jpg)
            return result, billed

        try:
            result, billed = await asyncio.wait_for(pipeline(), COUNT_BUDGET_S)
        except TimeoutError:
            log.warning("%s count over %.0fs budget", session_id[:8], COUNT_BUDGET_S)
            return self._count_failed(session_id, cp, t0)
        except Exception:
            log.exception("%s count failed", session_id[:8])
            return self._count_failed(session_id, cp, t0)
        log.info("%s count %r -> %d (raw ids %d, %d frames with matches, %d billed)",
                 session_id[:8], prompt, result.count, result.raw_ids,
                 result.frames_with_matches, billed)  # fmt: skip
        if not result.ok:  # zero, or an implausible > 500
            return self._count_failed(session_id, cp, t0)
        (folder / f"{stem}.json").write_text(json.dumps({
            "prompt": prompt, "count": result.count, "best_frame": result.best_frame,
            "boxes": result.best_boxes, "frame_size": result.frame_size, "billed": billed,
            "tracks": [asdict(t) for t in result.tracks],
        }))  # fmt: skip
        cp.counted = result.count
        cp.proposed = float(result.count)
        cp.entry.count_video = result.count
        cp.params = (f"identities persisting >= {self.settings.count_min_frames} frames, "
                     f"area >= {self.settings.count_min_area:.1%} of frame, {COUNT_FPS} fps, "
                     f"{billed} frames, SAM prompt \"{prompt}\"")  # fmt: skip
        w, h = result.frame_size
        picture = {"frame_url": f"/count-frame/{frame_jpg.name}", "boxes": result.best_boxes,
                   "width": w, "height": h}  # fmt: skip
        return self._review(session_id, cp, t0, picture)

    def _count_answer(
        self, session_id: str, cp: CountPending, answer: str, t0: float
    ) -> ActResponse:
        """A spoken or typed number after "Adjust" / "Enter number": back to the review card."""
        cp.entry.answers.append(answer)
        m = re.search(
            r"\d+(?:\.\d+)?", normalize_numbers(answer, sinhala_latin=cp.language == "si")
        )
        if not m:
            return self._count_reply(
                session_id, cp, "ask", _count_texts(cp.language)["how_many"], t0
            )
        cp.proposed = float(m.group())
        return self._review(session_id, cp, t0)

    async def count_action(
        self, session_id: str, action: str, worker: dict[str, Any] | None = None
    ) -> ActResponse:
        """The worker's tap on the review card. Only "post" writes, and only from review."""
        t0 = time.monotonic()
        cp = self._owned_count(session_id, worker)
        texts = _count_texts(cp.language)
        if action == "adjust":
            cp.stage = "adjust"
            return self._count_reply(session_id, cp, "ask", texts["how_many"], t0)
        if action == "retake":
            cp.stage = "capture"
            cp.counted = cp.proposed = None
            return self._count_reply(session_id, cp, "count", count_prompt(cp.language, cp.req),
                                     t0, count=self._count_view(cp))  # fmt: skip
        if action == "cancel":
            self.store.counts.pop(session_id, None)
            cp.entry.kind, cp.entry.text = "finish", texts["cancelled"]
            cp.entry.language = cp.language
            self.store.record(cp.entry)
            return self._count_reply(session_id, cp, "finish", texts["cancelled"], t0)
        if action != "post":
            raise CountStateError(f"unknown action {action!r}")
        if cp.stage != "review" or cp.proposed is None:
            raise CountStateError("nothing to post yet: film the shelf or enter the number first")
        qty = cp.proposed
        note = (f"counted by video: {cp.counted} (system {_num(cp.req['system_qty'])})"
                if cp.counted is not None else
                f"counted by hand: {_num(qty)} (system {_num(cp.req['system_qty'])})")  # fmt: skip
        if cp.counted is not None and qty != cp.counted:
            note += f" | adjusted by the worker to {_num(qty)}"
        if cp.params:
            note += f" | {cp.params}"
        self.store.counts.pop(session_id, None)
        posted = await self.rt.agent.executor.post_reconciliation(cp.ctx, cp.req, qty, note)
        entry = cp.entry
        entry.count_posted = qty
        entry.resolved_by = ("manual" if cp.counted is None
                             else "video" if qty == cp.counted else "adjusted")  # fmt: skip
        display, speech = readback([posted], cp.language)
        entry.kind, entry.text, entry.language = "done", display, cp.language
        entry.documents = [{"doctype": posted.doctype, "name": posted.name,
                            "summary": posted.summary, "url": erp_link(posted.doctype, posted.name),
                            "undo": json.dumps(posted.undo)}]  # fmt: skip
        self.store.record(entry)
        resp = self._count_reply(session_id, cp, "done", display, t0)
        resp.speech = speech
        resp.documents = entry.documents
        return resp

    def _stem(self, entry: HistoryEntry, folder: Path, suffix: str = "") -> str:
        base = Path(entry.audio).stem if entry.audio else (
            f"{time.strftime('%Y%m%d-%H%M%S')}-{entry.session_id[:12]}"
        )  # fmt: skip
        stem, n = f"{base}{suffix}", 2
        while (folder / f"{stem}.mp4").exists() or (folder / f"{stem}.jpg").exists():
            stem = f"{base}{suffix}-{n}"
            n += 1
        return stem

    def _save_photo(self, entry: HistoryEntry, image: bytes, mime: str) -> str:
        folder = self.settings.data_dir / "audio"
        folder.mkdir(parents=True, exist_ok=True)
        stem = Path(entry.audio).stem if entry.audio else (
            f"{time.strftime('%Y%m%d-%H%M%S')}-{entry.session_id[:12]}"
        )  # fmt: skip
        ext = PHOTO_TYPES.get(mime, ".jpg")
        path = folder / f"{stem}{ext}"
        n = 2
        while path.exists():
            path = folder / f"{stem}-{n}{ext}"
            n += 1
        path.write_bytes(image)
        return path.name


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


async def current_worker(request: Request) -> dict[str, Any]:
    """The worker behind this call (X-FloorVoice-Pin), in addition to the access key."""
    try:
        return await service().worker(request.headers.get(PIN_HEADER))
    except UnknownWorker as e:
        raise HTTPException(403, PIN_UNKNOWN) from e


Worker = Annotated[dict[str, Any], Depends(current_worker)]


def worker_view(w: dict[str, Any]) -> dict[str, str]:
    return {"id": w["name"], "name": w.get("employee_name") or w["name"],
            "home_warehouse": w.get("floorvoice_home_warehouse") or "",
            "language": w.get("floorvoice_language") or ""}  # fmt: skip


@app.get("/me", dependencies=[Depends(require_key)])
async def me(worker: Worker) -> dict[str, str]:
    return worker_view(worker)


@app.post("/act", response_model=ActResponse, dependencies=[Depends(require_key)])
async def act(req: ActRequest, worker: Worker) -> ActResponse:
    try:
        return await service().act(
            req.transcript, req.session_id, speech=req.speech, worker=worker, typed=True
        )
    except AgentFailure as e:
        raise HTTPException(502, f"Agent error: {e}") from e


@app.post("/clarify", response_model=ActResponse, dependencies=[Depends(require_key)])
async def clarify(req: ClarifyRequest, worker: Worker) -> ActResponse:
    try:
        return await service().clarify(req.session_id, req.answer, via=req.via, worker=worker)
    except KeyError as e:
        raise HTTPException(404, "No open question for this session") from e
    except NotYours as e:
        raise HTTPException(403, str(e)) from e
    except AgentFailure as e:
        raise HTTPException(502, f"Agent error: {e}") from e


@app.post("/clarify-photo", response_model=ActResponse, dependencies=[Depends(require_key)])
async def clarify_photo(
    session_id: Annotated[str, Form()], image: Annotated[UploadFile, File()], worker: Worker
) -> ActResponse:
    mime = (image.content_type or "").split(";")[0]
    if mime not in PHOTO_TYPES:
        raise HTTPException(415, "Send a JPEG, PNG or WebP photo")
    data = await image.read(MAX_PHOTO_BYTES + 1)
    if not data or len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(413, "Photo is empty or too large")
    try:
        return await service().clarify_photo(session_id, data, mime, worker)
    except KeyError as e:
        raise HTTPException(404, "No open question for this session") from e
    except NotYours as e:
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    except AgentFailure as e:
        raise HTTPException(502, f"Agent error: {e}") from e


class CountActionRequest(BaseModel):
    session_id: str
    action: str  # post | adjust | retake | cancel


@app.post("/count-video", response_model=ActResponse, dependencies=[Depends(require_key)])
async def count_video(
    session_id: Annotated[str, Form()], video: Annotated[UploadFile, File()], worker: Worker
) -> ActResponse:
    mime = (video.content_type or "").split(";")[0]
    if mime not in VIDEO_TYPES:
        raise HTTPException(415, "Send an MP4, MOV or WebM clip")
    data = await video.read(MAX_VIDEO_BYTES + 1)
    if not data or len(data) > MAX_VIDEO_BYTES:
        raise HTTPException(413, "Clip is empty or too large")
    try:
        return await service().count_video(session_id, data, mime, worker)
    except KeyError as e:
        raise HTTPException(404, "No count open for this session") from e
    except NotYours as e:
        raise HTTPException(403, str(e)) from e
    except CountStateError as e:
        raise HTTPException(409, str(e)) from e


@app.post("/count-action", response_model=ActResponse, dependencies=[Depends(require_key)])
async def count_action(req: CountActionRequest, worker: Worker) -> ActResponse:
    if req.action not in COUNT_ACTIONS:
        raise HTTPException(422, f"action must be one of {', '.join(COUNT_ACTIONS)}")
    try:
        return await service().count_action(req.session_id, req.action, worker)
    except KeyError as e:
        raise HTTPException(404, "No count open for this session") from e
    except NotYours as e:
        raise HTTPException(403, str(e)) from e
    except CountStateError as e:
        raise HTTPException(409, str(e)) from e
    except (ErpError, ToolError) as e:
        log.error("stock reconciliation failed: %s", e)
        raise HTTPException(409, f"ERPNext refused: {e}") from e


@app.get("/count-frame/{name}", dependencies=[Depends(require_key)])
async def count_frame(name: str) -> FileResponse:
    """The review picture (the frame with the most counted objects in view)."""
    path = get_settings().data_dir / "audio" / name
    if not _FRAME_NAME.match(name) or not path.is_file():
        raise HTTPException(404, "No such frame")
    return FileResponse(path, media_type="image/jpeg")


class UndoRequest(BaseModel):
    doctype: str
    name: str


def _undo_text(lang: str, doc: dict[str, str], holder: str) -> str:
    reopened = holder != doc["name"]
    if lang == "ta":
        tail = f" {holder} ஆக மீண்டும் திறக்கப்பட்டது." if reopened else ""
        return f"ரத்து செய்யப்பட்டது {doc['name']}: {doc['summary']}.{tail}"
    if lang == "si":
        tail = f" {holder} ලෙස නැවත විවෘත කළා." if reopened else ""
        return f"අවලංගු කළා {doc['name']}: {doc['summary']}.{tail}"
    tail = f" Reopened as {holder}." if reopened else ""
    return f"Cancelled {doc['name']}: {doc['summary']}.{tail}"


@app.post("/undo", dependencies=[Depends(require_key)])
async def undo(req: UndoRequest, worker: Worker) -> dict[str, Any]:
    """Reverse a document FloorVoice posted (only those in its own history), once, and only
    by the worker who posted it."""
    svc = service()
    found = svc.store.find_doc(req.doctype, req.name)
    if found is None:
        raise HTTPException(404, "FloorVoice did not post that document")
    entry, doc = found
    if entry.worker_id != worker["name"]:
        who = entry.worker_name or "another worker"
        raise HTTPException(403, f"Only {who} can undo {req.name}")
    if doc.get("undone"):
        raise HTTPException(409, f"{req.name} was already undone")
    posted = PostedDoc(
        doc["doctype"], doc["name"], doc["summary"], json.loads(doc.get("undo") or "{}")
    )
    try:
        holder = await svc.rt.agent.executor.undo(posted)
    except (ErpError, ToolError) as e:
        log.error("undo %s %s failed: %s", req.doctype, req.name, e)
        raise HTTPException(409, f"ERPNext refused: {e}") from e
    doc["undone"] = "1"
    doc["now"] = holder
    svc.store.save_all()
    lang = entry.language or reply_language(entry.transcript)
    text = _undo_text(lang, doc, holder)
    speech = {"ta": "ரத்து செய்யப்பட்டது.", "si": "අවලංගු කළා."}.get(lang, "Cancelled.")
    log.info("undo %s %s -> %s", req.doctype, req.name, holder)
    return {"ok": True, "text": text, "speech": speech, "name": holder,
            "url": erp_link(req.doctype, holder)}  # fmt: skip


@app.get("/history", dependencies=[Depends(require_key)])
async def history(worker: Worker, limit: int = 50, all: int = 0) -> list[dict[str, Any]]:
    """The current worker's rows; ?all=1 for everyone's (supervisor view)."""
    rows = [asdict(h) for h in reversed(service().store.history)
            if all or h.worker_id == worker["name"]][:limit]  # fmt: skip
    for row in rows:  # links follow the current tunnel URL, not the one stored at posting
        for doc in row["documents"]:
            doc["url"] = erp_link(doc["doctype"], doc["name"])
    return rows


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
