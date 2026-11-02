"""Feature A: a label photo answers an item question through the same path as a chip tap."""

import asyncio
import dataclasses
import json
from typing import Any

import pytest

from agent.loop import Agent, LlmReply
from agent.resolve import Masters, option_items
from agent.runtime import Runtime
from agent.tools import ToolContext
from agent.vision import (
    NO_MATCH,
    PhotoMatch,
    SparkVision,
    identify,
    parse_photo_reply,
    photo_schema,
)
from server.app import Service, Store
from server.config import get_settings
from tests.test_agent_loop import FakeExecutor, ScriptedLlm, call
from tests.test_tools import FixedCache

BEARINGS = ["BRG-6201", "BRG-6204", "BRG-6205", "BRG-6305"]
JPEG = b"\xff\xd8\xff\xe0fake-jpeg"


# --- structured output parsing and candidate validation ----------------------------------


def test_parse_valid_code() -> None:
    reply = json.dumps({"item_code": "BRG-6205", "label_text": "6205-2RS", "confidence": "high"})
    m = parse_photo_reply(reply, BEARINGS)
    assert m == PhotoMatch("BRG-6205", "6205-2RS", "high")
    assert m.usable


def test_parse_out_of_set_code_is_no_match() -> None:
    reply = json.dumps({"item_code": "BRG-6206", "label_text": "6206", "confidence": "high"})
    m = parse_photo_reply(reply, BEARINGS)
    assert m.item_code is None and not m.usable
    assert m.label_text == "6206"  # kept for the log, never posted


def test_parse_null_code() -> None:
    reply = json.dumps({"item_code": None, "label_text": "", "confidence": "low"})
    assert parse_photo_reply(reply, BEARINGS) == PhotoMatch(None, "", "low")


@pytest.mark.parametrize(
    "reply",
    [
        "not json",
        "",
        None,
        "[1, 2]",
        '{"item_code": "BRG-6205", "label_text": "x", "confidence": "certain"}',
        '{"item_code": "BRG-6205", "label_text": "x"}',
    ],
)
def test_parse_malformed_is_no_match(reply: str | None) -> None:
    assert not parse_photo_reply(reply, BEARINGS).usable


def test_low_confidence_is_not_usable() -> None:
    reply = json.dumps({"item_code": "BRG-6205", "label_text": "62?5", "confidence": "low"})
    assert not parse_photo_reply(reply, BEARINGS).usable


def test_schema_limits_codes_to_candidates() -> None:
    schema = photo_schema(BEARINGS)["json_schema"]["schema"]
    assert schema["properties"]["item_code"]["enum"] == [*BEARINGS, None]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"item_code", "label_text", "confidence"}


def test_request_shape_sends_image_as_data_url() -> None:
    v = SparkVision("k", "http://api.test/v1", "muse-spark-1.3", hedge_after=None)
    body = v.request(
        JPEG, "image/jpeg", [{"item_code": "BRG-6205", "item_name": "Ball Bearing 6205"}]
    )
    text, image = body["messages"][0]["content"]
    assert body["messages"][0]["role"] == "user"
    assert text["type"] == "text" and "BRG-6205" in text["text"]
    assert image["type"] == "image_url"
    assert image["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert body["response_format"]["type"] == "json_schema"
    assert body["reasoning_effort"] == "minimal"  # "low" ran past the 6 s budget on a phone


def test_runtime_vision_uses_minimal_reasoning() -> None:
    from agent.runtime import build_runtime

    rt = build_runtime(get_settings())
    assert rt.vision.reasoning_effort == "minimal"  # type: ignore[attr-defined]


async def test_identify_times_out_to_no_match() -> None:
    async def slow(image: bytes, mime: str, items: list[dict[str, Any]]) -> PhotoMatch:
        await asyncio.sleep(1)
        return PhotoMatch("BRG-6205", "", "high")

    assert await identify(slow, JPEG, "image/jpeg", [], budget=0.05) == NO_MATCH


async def test_identify_error_is_no_match() -> None:
    async def broken(image: bytes, mime: str, items: list[dict[str, Any]]) -> PhotoMatch:
        raise RuntimeError("400 invalid image")

    assert await identify(broken, JPEG, "image/jpeg", []) == NO_MATCH


# --- when the camera button is offered ----------------------------------------------------


def test_option_items_for_item_question(masters: Masters) -> None:
    opts = ["Ball Bearing 6201", "Ball Bearing 6205", "6305"]
    pairs = option_items(masters, opts)
    assert [i["item_code"] for _, i in pairs] == ["BRG-6201", "BRG-6205", "BRG-6305"]


@pytest.mark.parametrize(
    "options",
    [
        ["Line 1", "Line 2"],  # warehouse question
        ["5", "10", "20"],  # quantity question
        ["PO-JOB00003", "PO-JOB00004"],  # job card question
        ["Ball Bearing 6205"],  # one option is not a choice
        ["Ball Bearing 6205", "Line 2"],  # mixed
        [],
    ],
)
def test_option_items_not_item_question(masters: Masters, options: list[str]) -> None:
    assert option_items(masters, options) == []


# --- photo resumes the pending turn exactly like a chip tap -------------------------------


def _ask_then_write() -> list[LlmReply]:
    return [
        LlmReply(None, [call("ask_user", 1, question="Which bearing?",
                             options=["Ball Bearing 6205", "Ball Bearing 6305"])]),
        LlmReply(None, [call("create_material_request", 2, item_code="BRG-6205", qty=10,
                             purpose="Material Transfer", warehouse=None)]),
    ]  # fmt: skip


class FakeVision:
    def __init__(self, match: PhotoMatch) -> None:
        self.match = match
        self.seen: list[list[str]] = []

    async def __call__(self, image: bytes, mime: str, items: list[dict[str, Any]]) -> PhotoMatch:
        self.seen.append([i["item_code"] for i in items])
        return self.match


def make_service(
    masters: Masters, tmp_path, vision: FakeVision
) -> tuple[Service, ScriptedLlm, FakeExecutor]:
    llm, ex = ScriptedLlm(*_ask_then_write()), FakeExecutor()
    agent = Agent(llm, ex, FixedCache(masters))  # type: ignore[arg-type]
    rt = Runtime(None, FixedCache(masters), agent, vision)  # type: ignore[arg-type]
    settings = dataclasses.replace(get_settings(), data_dir=tmp_path)
    return Service(settings, rt, Store(tmp_path / "history.jsonl")), llm, ex


async def test_photo_resume_matches_chip_tap(masters: Masters, tmp_path) -> None:
    chip_svc, chip_llm, chip_ex = make_service(masters, tmp_path / "chip", FakeVision(NO_MATCH))
    r = await chip_svc.act("send me ten bearings", "s1")
    assert r.kind == "ask" and r.photo
    chip = await chip_svc.clarify("s1", "Ball Bearing 6205", via="chip")

    vision = FakeVision(PhotoMatch("BRG-6205", "6205-2RS 25x52x15", "high"))
    svc, llm, ex = make_service(masters, tmp_path / "photo", vision)
    await svc.act("send me ten bearings", "s2", audio="20261031-120000-s2.wav")
    photo = await svc.clarify_photo("s2", JPEG, "image/jpeg")

    assert vision.seen == [["BRG-6205", "BRG-6305"]]
    assert ex.calls == chip_ex.calls  # same executor call
    assert llm.seen[1][-1] == chip_llm.seen[1][-1]  # same tool result handed to Spark
    assert (photo.kind, photo.text) == (chip.kind, chip.text) == ("done", chip.text)
    entry = svc.store.history[-1]
    assert entry.resolved_by == "photo" and chip_svc.store.history[-1].resolved_by == "chip"
    assert entry.photos == ["20261031-120000-s2.jpg"]
    assert photo.elapsed_ms >= 0
    assert (tmp_path / "photo" / "audio" / "20261031-120000-s2.jpg").read_bytes() == JPEG


async def test_photo_label_reaches_remarks(masters: Masters, tmp_path) -> None:
    vision = FakeVision(PhotoMatch("BRG-6205", "6205-2RS", "medium"))
    svc, _llm, _ex = make_service(masters, tmp_path, vision)
    await svc.act("send me ten bearings", "s3")
    state = svc.store.pending["s3"].state
    await svc.clarify_photo("s3", JPEG, "image/jpeg")
    assert state.ctx.photo_label == "6205-2RS"


@pytest.mark.parametrize(
    "match",
    [
        PhotoMatch("BRG-6205", "62?5", "low"),
        PhotoMatch(None, "", "low"),
        NO_MATCH,  # over budget or API error
    ],
)
async def test_unreadable_photo_keeps_question(masters: Masters, tmp_path, match) -> None:
    svc, llm, ex = make_service(masters, tmp_path, FakeVision(match))
    await svc.act("send me ten bearings", "s4")
    r = await svc.clarify_photo("s4", JPEG, "image/jpeg")
    assert (r.kind, r.text) == ("ask", "Couldn't read the label — tap one")
    assert r.options == ["Ball Bearing 6205", "Ball Bearing 6305"] and r.photo
    assert ex.calls == [] and len(llm.seen) == 1  # nothing posted, Spark not resumed
    assert "s4" in svc.store.pending  # the chips still answer it
    done = await svc.clarify("s4", "Ball Bearing 6205", via="chip")
    assert done.kind == "done"


async def test_photo_refused_for_non_item_question(masters: Masters, tmp_path) -> None:
    llm = ScriptedLlm(LlmReply(None, [call("ask_user", 1, question="Which line?",
                                           options=["Line 1", "Line 2"])]))  # fmt: skip
    agent = Agent(llm, FakeExecutor(), FixedCache(masters))  # type: ignore[arg-type]
    vision = FakeVision(PhotoMatch("BRG-6205", "", "high"))
    rt = Runtime(None, FixedCache(masters), agent, vision)  # type: ignore[arg-type]
    svc = Service(dataclasses.replace(get_settings(), data_dir=tmp_path), rt,
                  Store(tmp_path / "h.jsonl"))  # fmt: skip
    r = await svc.act("move 2 bearing 6205 from stores", "s5")
    assert r.kind == "ask" and not r.photo
    with pytest.raises(ValueError):
        await svc.clarify_photo("s5", JPEG, "image/jpeg")
    assert vision.seen == []


async def test_number_doubt_question_offers_photo(masters: Masters) -> None:
    from tests.test_agent_loop import FakeExecutor as Ex

    llm = ScriptedLlm(LlmReply(None, [call("create_stock_entry", 1, purpose="Material Transfer",
                                           item_code="BRG-6205", qty=2, from_warehouse="Stores",
                                           to_warehouse="Line 2")]))  # fmt: skip
    agent = Agent(llm, Ex(), FixedCache(masters))  # type: ignore[arg-type]
    alt: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    alt.set_result("move 2 bearing 6305 from stores to line 2")
    state, r = await agent.act("move 2 bearing 6205 from stores to line 2", alt_transcript=alt)
    assert r.kind == "ask"
    assert [p["item_code"] for p in r.photo_items] == ["BRG-6205", "BRG-6305"]
    assert state.photo_items == r.photo_items


def test_remarks_include_label_text() -> None:
    from agent.tools import ToolExecutor

    ex = ToolExecutor(None, None, company="C", default_warehouse="Stores")  # type: ignore[arg-type]
    ctx = ToolContext(transcript="send me bearings", photo_label="6205-2RS")
    assert ex._remarks(ctx).endswith('| Item confirmed from label photo: "6205-2RS"')
