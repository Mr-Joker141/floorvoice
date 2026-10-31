"""Live end-to-end demo flows against real Muse Spark + ERPNext.

Posts real documents. Run explicitly: FLOORVOICE_LIVE=1 uv run pytest tests/test_live.py -v
"""

import dataclasses
import os
import time
from pathlib import Path

import pytest

from agent.runtime import Runtime, build_runtime
from agent.vision import PHOTO_BUDGET_S
from server.app import Service, Store
from server.config import get_settings

pytestmark = pytest.mark.skipif(
    os.environ.get("FLOORVOICE_LIVE") != "1", reason="set FLOORVOICE_LIVE=1 to hit live APIs"
)
BUDGET_S = 8.0


@pytest.fixture
async def rt():
    runtime = build_runtime(get_settings())
    await runtime.cache.get()
    yield runtime
    await runtime.aclose()


async def test_tamil_english_stock_transfer(rt: Runtime) -> None:
    t = time.monotonic()
    _, r = await rt.agent.act("இரண்டு bearing 6205 store-ல இருந்து line two-க்கு மாத்து")
    assert r.kind == "done", r.text
    assert time.monotonic() - t < BUDGET_S
    [doc] = r.posted
    se = await rt.erp.get_doc("Stock Entry", doc.name)
    assert se["docstatus"] == 1
    row = se["items"][0]
    assert (row["item_code"], row["qty"]) == ("BRG-6205", 2)
    assert (row["s_warehouse"], row["t_warehouse"]) == ("Stores - FVD", "Line 2 - FVD")


async def test_english_job_completion(rt: Runtime) -> None:
    cards = (await rt.cache.get(refresh=True)).job_cards
    open_cards = [c for c in cards if c["status"] == "Open" and not c.get("total_completed_qty")]
    if not open_cards:
        pytest.skip("no untouched job card left; run scripts/demo_reset.sh")
    card = open_cards[0]
    n = int(card["name"][-5:])
    t = time.monotonic()
    _, r = await rt.agent.act(f"job card {n} completed, 5 pieces")
    assert r.kind == "done", r.text
    assert time.monotonic() - t < BUDGET_S
    jc = await rt.erp.get_doc("Job Card", card["name"])
    assert jc["total_completed_qty"] == 5


async def test_ambiguous_material_request(rt: Runtime) -> None:
    t = time.monotonic()
    state, r = await rt.agent.act("send me bearings", home_warehouse="Line 1")
    assert r.kind == "ask", r.text
    assert time.monotonic() - t < BUDGET_S
    assert any("6205" in o for o in r.options)
    t = time.monotonic()
    r = await rt.agent.clarify(state, "6205, ten pieces")
    assert r.kind == "done", r.text
    assert time.monotonic() - t < BUDGET_S
    mr = await rt.erp.get_doc("Material Request", r.posted[0].name)
    assert (mr["docstatus"], mr["status"]) == (1, "Pending")
    assert (mr["items"][0]["item_code"], mr["items"][0]["qty"]) == ("BRG-6205", 10)
    assert mr["items"][0]["warehouse"] == "Line 1 - FVD"


FIXTURES = Path(__file__).parent / "fixtures"
# A real phone photo of a labelled 6205 box, if one has been added; otherwise the synthetic label.
LABEL_PHOTO = next(
    p for p in (FIXTURES / "label_6205.jpg", FIXTURES / "label_6205_synthetic.jpg") if p.exists()
)


async def test_photo_answers_item_question(rt: Runtime, tmp_path) -> None:
    svc = Service(dataclasses.replace(get_settings(), data_dir=tmp_path), rt,
                  Store(tmp_path / "history.jsonl"))  # fmt: skip
    r = await svc.act("send me ten bearings", "live-photo", "Line 1 - FVD")
    assert r.kind == "ask" and r.photo, r.text
    t = time.monotonic()
    r = await svc.clarify_photo("live-photo", LABEL_PHOTO.read_bytes(), "image/jpeg")
    elapsed = time.monotonic() - t
    assert r.kind == "done", r.text
    assert elapsed < PHOTO_BUDGET_S + BUDGET_S
    mr = await rt.erp.get_doc("Material Request", r.documents[0]["name"])
    assert (mr["items"][0]["item_code"], mr["items"][0]["qty"]) == ("BRG-6205", 10)
    assert svc.store.history[-1].resolved_by == "photo"
    assert "label photo" in mr["floorvoice_remarks"] and "6205" in mr["floorvoice_remarks"]
