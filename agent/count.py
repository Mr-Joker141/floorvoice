"""Stock count by video: SAM 3.1 tracks every matching object across a short shelf pan.

The count is the number of distinct object identities that persist for a few frames and are
not specks, never a sum of per-frame counts (a pan shows each box in many frames). Nothing
here posts anything: the worker reviews the number before a Stock Reconciliation is made.

SAM 3.1 (Meta Model API, POST /v1/responses, model "sam-3.1") answers one noun phrase per
request with one text line per frame that has matches:

    <23f>0<|box;x1=215;y1=262;x2=413;y2=558;w=1280;h=720;c=0.70|><|mask;...|>,1<|box;...|>...

"<Nf>" is the frame index, the integer before each box is the object id (stable while the
object stays in view), boxes are in source pixels. Frames without matches have no line.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import shutil
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

log = logging.getLogger(__name__)

SAM_MODEL = "sam-3.1"
COUNT_FPS = 5  # 12 s pan -> ~60 frames; SAM bills >= 50 frames per request
MAX_CLIP_S = 15.0
MAX_FRAMES = 75
MAX_EDGE = 1280
COUNT_BUDGET_S = 20.0  # upload received -> review card
MIN_FRAMES = 3  # an identity must persist this many frames to count
MIN_AREA = 0.002  # ... and cover at least this fraction of the frame (no specks)
MAX_AREA = 0.6  # a "box" covering most of the frame is the shelf, not an item
MERGE_IOU = 0.8  # two ids boxing the same thing in the same frames are one object
MAX_COUNT = 500

_LINE = re.compile(r"^<(\d+)f>(.*)$")
# The mask payload is base85 ('!'..'u'), so it never contains '|'.
_RECORD = re.compile(
    r"(\d+)<\|box;x1=(\d+);y1=(\d+);x2=(\d+);y2=(\d+);w=(\d+);h=(\d+)(?:;c=([\d.]+))?\|>"
    r"(?:<\|mask;[^|]*\|>)?"
)


@dataclass(frozen=True)
class Detection:
    frame: int
    object_id: int
    box: tuple[int, int, int, int]  # x1, y1, x2, y2 (inclusive)
    frame_size: tuple[int, int]  # w, h
    confidence: float | None = None

    @property
    def area(self) -> float:
        x1, y1, x2, y2 = self.box
        w, h = self.frame_size
        return max(0, x2 - x1 + 1) * max(0, y2 - y1 + 1) / float(w * h or 1)


def parse_sam_text(text: str) -> list[Detection]:
    out: list[Detection] = []
    for line in (text or "").splitlines():
        m = _LINE.match(line.strip())
        if not m:
            continue
        frame = int(m.group(1))
        for r in _RECORD.finditer(m.group(2)):
            oid, x1, y1, x2, y2, w, h, c = r.groups()
            out.append(Detection(frame, int(oid), (int(x1), int(y1), int(x2), int(y2)),
                                 (int(w), int(h)), float(c) if c else None))  # fmt: skip
    return out


def sam_output_text(response: dict[str, Any]) -> str:
    """Concatenated output_text of a /v1/responses body (empty when nothing matched)."""
    parts = [
        c.get("text") or ""
        for o in response.get("output") or []
        for c in o.get("content") or []
        if c.get("type") == "output_text"
    ]
    return "".join(parts)


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]) + 1)
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]) + 1)
    inter = ix * iy
    area = lambda r: (r[2] - r[0] + 1) * (r[3] - r[1] + 1)
    union = area(a) + area(b) - inter
    return inter / union if union else 0.0


@dataclass
class Track:
    number: int  # 1-based, in order of first appearance; what the review overlay shows
    object_ids: list[int]
    first_frame: int
    last_frame: int
    frames: int


@dataclass
class CountResult:
    count: int
    tracks: list[Track] = field(default_factory=list)
    best_frame: int | None = None  # frame with the most counted objects in view
    best_boxes: list[dict[str, int]] = field(default_factory=list)  # {n, x1, y1, x2, y2}
    frame_size: tuple[int, int] = (0, 0)
    raw_ids: int = 0  # identities before filtering
    frames_with_matches: int = 0

    @property
    def ok(self) -> bool:
        return 0 < self.count <= MAX_COUNT


def count_objects(
    dets: list[Detection],
    *,
    min_frames: int = MIN_FRAMES,
    min_area: float = MIN_AREA,
    max_area: float = MAX_AREA,
    merge_iou: float = MERGE_IOU,
) -> CountResult:
    """Distinct objects across the clip: identities that persist >= min_frames frames with
    a median box area in [min_area, max_area] of the frame; ids that box the same object
    in all the frames they share are merged."""
    by_id: dict[int, list[Detection]] = defaultdict(list)
    for d in dets:
        by_id[d.object_id].append(d)
    kept = {
        oid: sorted(ds, key=lambda d: d.frame)
        for oid, ds in by_id.items()
        if len({d.frame for d in ds}) >= min_frames
        and min_area <= statistics.median(d.area for d in ds) <= max_area
    }
    # Union ids that are the same object (SAM sometimes reports one box twice).
    parent = {oid: oid for oid in kept}

    def root(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    ids = sorted(kept)
    boxes = {oid: {d.frame: d.box for d in ds} for oid, ds in kept.items()}
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            shared = boxes[a].keys() & boxes[b].keys()
            if len(shared) >= min_frames and all(
                _iou(boxes[a][f], boxes[b][f]) >= merge_iou for f in shared
            ):
                parent[root(b)] = root(a)
    groups: dict[int, list[int]] = defaultdict(list)
    for oid in ids:
        groups[root(oid)].append(oid)
    ordered = sorted(groups.values(), key=lambda g: min(kept[o][0].frame for o in g))
    tracks: list[Track] = []
    number_of: dict[int, int] = {}
    for n, members in enumerate(ordered, start=1):
        frames = sorted({d.frame for o in members for d in kept[o]})
        tracks.append(Track(n, members, frames[0], frames[-1], len(frames)))
        for o in members:
            number_of[o] = n
    result = CountResult(len(tracks), tracks, raw_ids=len(by_id),
                         frames_with_matches=len({d.frame for d in dets}))  # fmt: skip
    if not tracks:
        return result
    in_view: dict[int, dict[int, tuple[int, int, int, int]]] = defaultdict(dict)
    for oid, ds in kept.items():
        for d in ds:
            in_view[d.frame].setdefault(number_of[oid], d.box)
    best = max(sorted(in_view), key=lambda f: len(in_view[f]))
    result.best_frame = best
    result.best_boxes = [
        {"n": n, "x1": b[0], "y1": b[1], "x2": b[2], "y2": b[3]}
        for n, b in sorted(in_view[best].items())
    ]
    result.frame_size = next(d.frame_size for d in dets if d.frame == best)
    return result


# --- SAM 3.1 client ------------------------------------------------------------------------


class Segmenter(Protocol):
    async def __call__(self, video: bytes, phrase: str) -> tuple[str, int]: ...


class SamVideo:
    """One concept prompt over one clip; returns (output text, frames billed)."""

    def __init__(self, api_key: str, base_url: str, model: str = SAM_MODEL) -> None:
        self.url = f"{base_url.rstrip('/')}/responses"
        self.model = model
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def request(self, video: bytes, phrase: str) -> dict[str, Any]:
        b64 = base64.b64encode(video).decode()
        return {
            "model": self.model,
            "input": [{"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": phrase},
                {"type": "input_video", "video_url": f"data:video/mp4;base64,{b64}"},
            ]}],
            "metadata": {"mask_encoding": "one_bit", "include_confidence": "true"},
        }  # fmt: skip

    async def __call__(self, video: bytes, phrase: str) -> tuple[str, int]:
        t = time.monotonic()
        resp = await self.client.post(self.url, json=self.request(video, phrase))
        resp.raise_for_status()
        body = resp.json()
        frames = int((body.get("usage") or {}).get("video_frames_processed") or 0)
        text = sam_output_text(body)
        log.info("sam %.2fs %r frames_billed=%d chars=%d", time.monotonic() - t, phrase,
                 frames, len(text))  # fmt: skip
        return text, frames

    async def aclose(self) -> None:
        await self.client.aclose()


# --- video preparation (ffmpeg) -------------------------------------------------------------


def ffmpeg_bin() -> str:
    found = shutil.which("ffmpeg") or str(Path.home() / ".local/bin/ffmpeg")
    if not Path(found).exists():
        raise RuntimeError("ffmpeg not found")
    return found


async def _run(*args: str, timeout: float) -> None:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        raise
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {err.decode(errors='replace')[-300:]}")


async def prepare_clip(src: Path, dst: Path, *, timeout: float = 8.0) -> None:
    """Phone clip (MP4/MOV/WebM, any codec) -> H.264 MP4 at COUNT_FPS, <= MAX_CLIP_S,
    <= MAX_FRAMES frames, long edge <= MAX_EDGE, no audio, faststart (what SAM accepts)."""
    scale = f"scale='trunc(min(1,{MAX_EDGE}/max(iw,ih))*iw/2)*2':-2"
    await _run(
        ffmpeg_bin(), "-nostdin", "-loglevel", "error", "-y", "-i", str(src),
        "-t", str(MAX_CLIP_S), "-an", "-vf", f"fps={COUNT_FPS},{scale}",
        "-frames:v", str(MAX_FRAMES), "-c:v", "libx264", "-preset", "veryfast",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(dst),
        timeout=timeout,
    )  # fmt: skip


async def extract_frame(clip: Path, frame: int, dst: Path, *, timeout: float = 5.0) -> None:
    """One frame of the prepared clip as JPEG (the review picture)."""
    await _run(
        ffmpeg_bin(), "-nostdin", "-loglevel", "error", "-y", "-i", str(clip),
        "-vf", f"select=eq(n\\,{int(frame)})", "-frames:v", "1", "-q:v", "3", str(dst),
        timeout=timeout,
    )  # fmt: skip
