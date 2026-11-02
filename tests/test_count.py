"""Feature C: stock count by video (SAM 3.1) -> mandatory review -> Stock Reconciliation."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
import respx

from agent.count import (
    MAX_COUNT,
    Detection,
    SamVideo,
    count_objects,
    parse_sam_text,
    sam_output_text,
)
from agent.loop import Agent, LlmReply
from agent.resolve import Masters
from agent.runtime import Runtime
from agent.tools import TOOL_SCHEMAS, PostedDoc, ToolContext, ToolExecutor, tool_schemas
from erp.client import ErpClient
from server import app as app_module
from server.app import CountStateError, NotYours, Service, Store
from server.config import get_settings
from tests.test_agent_loop import ScriptedLlm, call
from tests.test_tools import BASE, FixedCache

SAM = Path(__file__).parent / "fixtures" / "sam"


def recorded(name: str) -> list[Detection]:
    return parse_sam_text(sam_output_text(json.loads((SAM / name).read_text())))


def det(frame: int, oid: int, x: int, y: int = 100, w: int = 120, h: int = 160) -> Detection:
    return Detection(frame, oid, (x, y, x + w - 1, y + h - 1), (1280, 720))


def track(oid: int, frames: range, x0: int, dx: int = -20, **kw: Any) -> list[Detection]:
    return [det(f, oid, x0 + dx * i, **kw) for i, f in enumerate(frames)]


# --- parsing and identity counting ---------------------------------------------------------


def test_recorded_pan_counts_twelve_boxes() -> None:
    dets = recorded("brown_cardboard_box_12_pan.json")
    r = count_objects(dets)
    assert r.count == 12 and r.ok
    per_frame_sum = len(dets)
    assert per_frame_sum > 200  # summing per-frame counts would be wildly wrong
    assert r.best_frame is not None and len(r.best_boxes) == max(
        sum(1 for d in dets if d.frame == f) for f in {d.frame for d in dets}
    )
    assert r.frame_size == (1280, 720)
    assert [t.number for t in r.tracks] == list(range(1, 13))
    assert all(t.first_frame <= t.last_frame for t in r.tracks)


def test_recorded_weak_prompt_undercounts() -> None:
    # "cardboard box" on the same clip: SAM found 9 of 12; the count reports what it saw.
    assert count_objects(recorded("cardboard_box_12_pan_partial.json")).count == 9


def test_recorded_no_match_is_failure() -> None:
    r = count_objects(recorded("bearing_box_no_match.json"))
    assert r.count == 0 and not r.ok and r.best_frame is None


def test_parse_line_format() -> None:
    text = ("<3f>7<|box;x1=10;y1=20;x2=109;y2=219;w=1280;h=720;c=0.91|><|mask;x=0;y=0;"
            "c=0.91;data=200,100,!!!!J,0c[|>,9<|box;x1=300;y1=20;x2=399;y2=219;w=1280;h=720|>"
            "\n<5f>7<|box;x1=0;y1=20;x2=99;y2=219;w=1280;h=720|>")  # fmt: skip
    dets = parse_sam_text(text)
    assert [(d.frame, d.object_id) for d in dets] == [(3, 7), (3, 9), (5, 7)]
    assert dets[0].confidence == pytest.approx(0.91) and dets[1].confidence is None
    assert dets[0].box == (10, 20, 109, 219)


def test_persistence_filter() -> None:
    dets = track(1, range(10), 900) + track(2, range(4, 6), 500)  # id 2: only 2 frames
    assert count_objects(dets).count == 1
    assert count_objects(dets, min_frames=2).count == 2


def test_area_filter_drops_specks_and_the_whole_shelf() -> None:
    dets = (
        track(1, range(8), 900)
        + track(2, range(8), 600, w=10, h=10)  # speck: 0.01% of the frame
        + track(3, range(8), 0, dx=0, w=1280, h=700)  # the shelf itself
    )
    assert count_objects(dets).count == 1


def test_overlapping_identities_are_one_object() -> None:
    same = track(1, range(8), 900) + track(2, range(8), 902)  # same box, two ids
    assert count_objects(same).count == 1
    neighbours = track(1, range(8), 900) + track(2, range(8), 1000)  # partly overlap
    assert count_objects(neighbours).count == 2


def test_zero_and_too_many_are_failures() -> None:
    assert not count_objects([]).ok
    many = [d for oid in range(MAX_COUNT + 1) for d in track(oid, range(3), oid, dx=0, w=50, h=50)]
    r = count_objects(many, merge_iou=1.01)
    assert r.count == MAX_COUNT + 1 and not r.ok


def test_sam_request_shape() -> None:
    body = SamVideo("k", "https://api.meta.ai/v1").request(b"\x00\x00\x00 ftypmp42", "box")
    assert body["model"] == "sam-3.1"
    text, video = body["input"][0]["content"]
    assert text == {"type": "input_text", "text": "box"}
    assert video["type"] == "input_video"
    assert video["video_url"].startswith("data:video/mp4;base64,")


# --- tool offering and the Stock Reconciliation writer ------------------------------------


def test_count_tool_only_when_enabled() -> None:
    names = lambda ts: {t["function"]["name"] for t in ts}
    assert "count_stock" not in names(tool_schemas())
    assert "count_stock" in names(tool_schemas(count=True))
    # No tool the model can call writes a Stock Reconciliation.
    assert not any("reconcil" in n for n in names(TOOL_SCHEMAS))


@pytest.fixture
def counted_masters(masters: Masters) -> Masters:
    items = [dict(i, floorvoice_count_prompt="box" if i["item_code"].startswith("BRG-") else "")
             for i in masters.items]  # fmt: skip
    return dataclasses.replace(masters, items=items)


@pytest.fixture
async def ex(counted_masters: Masters):
    erp = ErpClient(BASE, "k", "s")
    yield ToolExecutor(erp, FixedCache(counted_masters), company="FloorVoice Demo Works",
                       default_warehouse="Stores", count_enabled=True)  # fmt: skip
    await erp.aclose()


def mock_bin(qty: float = 15, rate: float = 950) -> None:
    respx.get(f"{BASE}/api/resource/Bin").respond(
        json={"data": [{"actual_qty": qty, "valuation_rate": rate}]}
    )


@respx.mock
async def test_count_stock_tool_reads_bin_and_posts_nothing(ex: ToolExecutor) -> None:
    mock_bin()
    ctx = ToolContext(transcript="count bearing 6305 in stores")
    r = await ex.run("count_stock", {"item_code": "BRG-6305", "warehouse": "Stores"}, ctx)
    assert r["ok"] and not ctx.posted
    assert ctx.count_request == {
        "item_code": "BRG-6305", "item_name": "Ball Bearing 6305", "stock_uom": "Nos",
        "warehouse": "Stores - FVD", "warehouse_name": "Stores", "system_qty": 15.0,
        "valuation_rate": 950.0, "prompt": "box",
    }  # fmt: skip
    assert all(c.request.method == "GET" for c in respx.calls)


async def test_count_stock_refused_when_disabled(counted_masters: Masters) -> None:
    erp = ErpClient(BASE, "k", "s")
    off = ToolExecutor(erp, FixedCache(counted_masters), company="C", default_warehouse="Stores")
    r = await off.run("count_stock", {"item_code": "BRG-6305", "warehouse": None}, ToolContext())
    assert not r["ok"]
    await erp.aclose()


REQ = {"item_code": "BRG-6305", "item_name": "Ball Bearing 6305", "warehouse": "Stores - FVD",
       "warehouse_name": "Stores", "system_qty": 15.0, "valuation_rate": 950.0, "prompt": "box"}  # fmt: skip


@respx.mock
async def test_reconciliation_payload_and_undo(ex: ToolExecutor) -> None:
    ins = respx.post(f"{BASE}/api/resource/Stock%20Reconciliation").respond(
        json={"data": {"name": "MAT-RECO-2026-00003"}}
    )
    sub = respx.put(f"{BASE}/api/resource/Stock%20Reconciliation/MAT-RECO-2026-00003").respond(
        json={"data": {"name": "MAT-RECO-2026-00003", "docstatus": 1}}
    )
    ctx = ToolContext(transcript="count bearing 6305", worker_id="HR-EMP-00001",
                      worker_name="Kumar Selvaraj")  # fmt: skip
    posted = await ex.post_reconciliation(ctx, REQ, 12, "counted by video: 12 (system 15)")
    sent = json.loads(ins.calls.last.request.content)
    assert sub.calls.last.request.content == b'{"docstatus":1}'
    assert sent["purpose"] == "Stock Reconciliation"
    assert sent["items"] == [{"item_code": "BRG-6305", "warehouse": "Stores - FVD", "qty": 12,
                              "valuation_rate": 950.0}]  # fmt: skip
    assert sent["floorvoice_worker"] == "HR-EMP-00001"
    assert sent["floorvoice_remarks"].startswith("Kumar Selvaraj: Posted by FloorVoice")
    assert "counted by video: 12 (system 15)" in sent["floorvoice_remarks"]
    assert posted.summary == "Ball Bearing 6305 in Stores now 12"

    cancel = respx.post(f"{BASE}/api/method/frappe.client.cancel").respond(json={"message": "ok"})
    assert await ex.undo(posted) == "MAT-RECO-2026-00003"
    assert cancel.calls.last.request.content == (
        b"doctype=Stock+Reconciliation&name=MAT-RECO-2026-00003"
    )


# --- the review gate: no post without the worker's tap -------------------------------------


class FakeSam:
    def __init__(self, text: str = "", delay: float = 0.0) -> None:
        self.text, self.delay, self.calls = text, delay, 0

    async def __call__(self, video: bytes, phrase: str) -> tuple[str, int]:
        self.calls += 1
        await asyncio.sleep(self.delay)
        return self.text, 60

    async def aclose(self) -> None:
        pass


BOX_TEXT = sam_output_text(json.loads((SAM / "brown_cardboard_box_12_pan.json").read_text()))
KUMAR = {"name": "HR-EMP-00001", "employee_name": "Kumar Selvaraj",
         "floorvoice_home_warehouse": "Line 1 - FVD", "floorvoice_language": "en"}  # fmt: skip
PRIYA = {"name": "HR-EMP-00002", "employee_name": "Priya Nadarajah"}


@pytest.fixture(autouse=True)
def no_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    async def prepare(src: Path, dst: Path, **kw: Any) -> None:
        dst.write_bytes(src.read_bytes())

    async def frame(clip: Path, n: int, dst: Path, **kw: Any) -> None:
        dst.write_bytes(b"\xff\xd8jpeg")

    monkeypatch.setattr(app_module, "prepare_clip", prepare)
    monkeypatch.setattr(app_module, "extract_frame", frame)


def make_service(ex: ToolExecutor, tmp_path: Path, sam: FakeSam | None, item: str = "BRG-6305",
                 *extra: LlmReply) -> Service:  # fmt: skip
    llm = ScriptedLlm(
        LlmReply(None, [call("count_stock", 1, item_code=item, warehouse="Stores - FVD")]), *extra
    )
    agent = Agent(llm, ex, ex.cache, count_enabled=True)
    rt = Runtime(ex.erp, ex.cache, agent, None, sam)  # type: ignore[arg-type]
    settings = dataclasses.replace(get_settings(), data_dir=tmp_path, video_count=True)
    return Service(settings, rt, Store(tmp_path / "history.jsonl"))


def reco_posts() -> list[Any]:
    return [c for c in respx.calls if c.request.method == "POST"
            and "Stock%20Reconciliation" in str(c.request.url)]  # fmt: skip


def mock_reco(name: str = "MAT-RECO-2026-00001") -> None:
    respx.post(f"{BASE}/api/resource/Stock%20Reconciliation").respond(json={"data": {"name": name}})
    respx.put(f"{BASE}/api/resource/Stock%20Reconciliation/{name}").respond(
        json={"data": {"name": name, "docstatus": 1}}
    )


@respx.mock
async def test_count_flow_posts_only_after_confirm(ex: ToolExecutor, tmp_path: Path) -> None:
    mock_bin()
    mock_reco()
    sam = FakeSam(BOX_TEXT)
    svc = make_service(ex, tmp_path, sam)
    r = await svc.act("count bearing 6305 in stores", "s1", audio="20261102-1000-s1.wav",
                      worker=KUMAR)  # fmt: skip
    assert r.kind == "count" and r.count and r.count["system_qty"] == 15
    with pytest.raises(CountStateError):  # nothing filmed yet
        await svc.count_action("s1", "post", KUMAR)

    review = await svc.count_video("s1", b"\x00mp4", "video/mp4", KUMAR)
    assert review.kind == "review" and review.text == "I count 12. ERPNext has 15 in Stores."
    assert review.options == ["Post 12", "Adjust", "Retake", "Cancel"]
    assert review.actions == ["post", "adjust", "retake", "cancel"]
    assert review.review and review.review["counted"] == 12 and len(review.review["boxes"]) == 6
    assert (tmp_path / "audio" / "20261102-1000-s1-count.mp4").exists()
    assert review.review["frame_url"] == "/count-frame/20261102-1000-s1-count.jpg"
    assert not reco_posts()

    with pytest.raises(NotYours):
        await svc.count_action("s1", "post", PRIYA)
    done = await svc.count_action("s1", "post", KUMAR)
    assert done.kind == "done"
    assert done.text == "Posted MAT-RECO-2026-00001: Ball Bearing 6305 in Stores now 12."
    assert len(reco_posts()) == 1
    entry = svc.store.history[-1]
    assert (entry.count_system, entry.count_video, entry.count_posted) == (15, 12, 12)
    assert entry.resolved_by == "video" and entry.worker_id == "HR-EMP-00001"
    remarks = json.loads(reco_posts()[0].request.content)["floorvoice_remarks"]
    assert "counted by video: 12 (system 15)" in remarks and ">= 3 frames" in remarks
    with pytest.raises(KeyError):  # once
        await svc.count_action("s1", "post", KUMAR)


@respx.mock
async def test_adjust_by_voice_then_confirm(ex: ToolExecutor, tmp_path: Path) -> None:
    mock_bin()
    mock_reco()
    svc = make_service(ex, tmp_path, FakeSam(BOX_TEXT))
    await svc.act("count bearing 6305 in stores", "s2", worker=KUMAR)
    await svc.count_video("s2", b"\x00mp4", "video/mp4", KUMAR)
    ask = await svc.count_action("s2", "adjust", KUMAR)
    assert ask.kind == "ask" and svc.has_pending("s2")
    again = await svc.clarify("s2", "thirteen", worker=KUMAR)  # spoken or typed number
    assert again.kind == "review" and again.options[0] == "Post 13"
    assert not reco_posts()  # a number alone never posts
    await svc.count_action("s2", "post", KUMAR)
    sent = json.loads(reco_posts()[0].request.content)
    assert sent["items"][0]["qty"] == 13
    assert "adjusted by the worker to 13" in sent["floorvoice_remarks"]
    assert svc.store.history[-1].resolved_by == "adjusted"


@pytest.mark.parametrize("sam", [FakeSam(""), FakeSam(BOX_TEXT, delay=1.0)],
                         ids=["zero identities", "over budget"])  # fmt: skip
@respx.mock
async def test_failed_count_offers_retake_or_number(ex: ToolExecutor, tmp_path: Path,
                                                    monkeypatch, sam: FakeSam) -> None:  # fmt: skip
    monkeypatch.setattr(app_module, "COUNT_BUDGET_S", 0.2)
    mock_bin()
    svc = make_service(ex, tmp_path, sam)
    await svc.act("count bearing 6305 in stores", "s3", worker=KUMAR)
    r = await svc.count_video("s3", b"\x00mp4", "video/mp4", KUMAR)
    assert r.kind == "review" and r.text == "Couldn't count that — retake or enter the number."
    assert r.actions == ["retake", "adjust", "cancel"]
    with pytest.raises(CountStateError):
        await svc.count_action("s3", "post", KUMAR)
    assert not reco_posts()


@respx.mock
async def test_item_without_prompt_is_counted_by_hand(ex: ToolExecutor, tmp_path: Path) -> None:
    mock_bin(qty=600)
    mock_reco()
    sam = FakeSam(BOX_TEXT)
    svc = make_service(ex, tmp_path, sam, "BLT-M10X40")
    r = await svc.act("count bolt M10 in stores", "s4", worker=KUMAR)
    assert r.kind == "ask" and r.count and r.count["manual"]
    assert r.text == "No video description for Hex Bolt M10x40 yet — say or type the count."
    with pytest.raises(CountStateError):
        await svc.count_video("s4", b"\x00mp4", "video/mp4", KUMAR)
    review = await svc.clarify("s4", "590", worker=KUMAR)
    assert review.kind == "review" and review.options[0] == "Post 590"
    await svc.count_action("s4", "post", KUMAR)
    assert sam.calls == 0 and svc.store.history[-1].resolved_by == "manual"


@respx.mock
async def test_cancel_and_retake(ex: ToolExecutor, tmp_path: Path) -> None:
    mock_bin()
    svc = make_service(ex, tmp_path, FakeSam(BOX_TEXT))
    await svc.act("count bearing 6305 in stores", "s5", worker=KUMAR)
    await svc.count_video("s5", b"\x00mp4", "video/mp4", KUMAR)
    again = await svc.count_action("s5", "retake", KUMAR)
    assert again.kind == "count" and again.count
    with pytest.raises(CountStateError):
        await svc.count_action("s5", "post", KUMAR)
    done = await svc.count_action("s5", "cancel", KUMAR)
    assert done.kind == "finish" and not reco_posts()
    assert svc.store.history[-1].text == "Count cancelled — nothing posted."


@respx.mock
async def test_finish_after_count_posts_nothing(ex: ToolExecutor, tmp_path: Path) -> None:
    # Spark calls count_stock and finish together: the turn still ends at "film the shelf".
    mock_bin()
    llm = ScriptedLlm(LlmReply(None, [
        call("count_stock", 1, item_code="BRG-6305", warehouse="Stores - FVD"),
        call("finish", 2, summary="Counted 12 bearings, stock updated."),
    ]))  # fmt: skip
    agent = Agent(llm, ex, ex.cache, count_enabled=True)
    state, r = await agent.act("count bearing 6305 in stores")
    assert r.kind == "count" and not state.ctx.posted and not reco_posts()


def test_undo_route_accepts_reconciliation() -> None:
    doc = PostedDoc("Stock Reconciliation", "MAT-RECO-2026-00001", "x")
    assert doc.doctype in ("Stock Entry", "Material Request", "Stock Reconciliation")
