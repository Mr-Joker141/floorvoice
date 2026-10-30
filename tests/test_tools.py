import json

import pytest
import respx

from agent.resolve import Masters
from agent.tools import MastersCache, ToolContext, ToolExecutor
from erp.client import ErpClient

BASE = "http://erp.test"


class FixedCache(MastersCache):
    def __init__(self, m: Masters) -> None:
        self.m = m

    async def get(self, *, refresh: bool = False) -> Masters:
        return self.m

    def invalidate(self) -> None:
        pass


@pytest.fixture
async def ex(masters: Masters):
    erp = ErpClient(BASE, "k", "s")
    yield ToolExecutor(
        erp, FixedCache(masters), company="FloorVoice Demo Works", default_warehouse="Stores"
    )
    await erp.aclose()


@pytest.fixture
def ctx() -> ToolContext:
    return ToolContext(transcript="move 2 bearing 6205 from stores to line 2")


@respx.mock
async def test_stock_transfer_payload(ex: ToolExecutor, ctx: ToolContext) -> None:
    route = respx.post(f"{BASE}/api/resource/Stock%20Entry").respond(
        json={"data": {"name": "MAT-STE-2026-00009"}}
    )
    submit = respx.put(f"{BASE}/api/resource/Stock%20Entry/MAT-STE-2026-00009").respond(
        json={"data": {"name": "MAT-STE-2026-00009", "docstatus": 1}}
    )
    r = await ex.run(
        "create_stock_entry",
        {"purpose": "Material Transfer", "item_code": "BRG-6205", "qty": 2,
         "from_warehouse": "Stores", "to_warehouse": "Line 2 - FVD"},
        ctx,
    )  # fmt: skip
    assert r["ok"] and r["name"] == "MAT-STE-2026-00009"
    sent = json.loads(route.calls.last.request.content)
    assert submit.calls.last.request.content == b'{"docstatus":1}'
    assert sent["stock_entry_type"] == "Material Transfer"
    assert sent["items"] == [
        {"item_code": "BRG-6205", "qty": 2.0, "s_warehouse": "Stores - FVD",
         "t_warehouse": "Line 2 - FVD"}
    ]  # fmt: skip
    assert "move 2 bearing" in sent["remarks"]
    assert ctx.posted[0].summary == "2 Ball Bearing 6205 Stores to Line 2"


@respx.mock
async def test_material_issue_has_no_target(ex: ToolExecutor, ctx: ToolContext) -> None:
    route = respx.post(f"{BASE}/api/resource/Stock%20Entry").respond(json={"data": {"name": "SE2"}})
    respx.put(f"{BASE}/api/resource/Stock%20Entry/SE2").respond(json={"data": {"name": "SE2"}})
    r = await ex.run(
        "create_stock_entry",
        {"purpose": "Material Issue", "item_code": "GRS-EP2", "qty": 2,
         "from_warehouse": "Line 2", "to_warehouse": "Stores"},
        ctx,
    )  # fmt: skip
    assert r["ok"]
    assert "t_warehouse" not in json.loads(route.calls.last.request.content)["items"][0]


@pytest.mark.parametrize(
    ("args", "error"),
    [
        ({"item_code": "NOPE", "qty": 1}, "unknown item_code"),
        ({"item_code": "BRG-6205", "qty": 0}, "greater than zero"),
        ({"item_code": "BRG-6205", "qty": "lots"}, "must be a number"),
        ({"item_code": "BRG-6205", "qty": 1, "from_warehouse": "Moon"}, "unknown from_warehouse"),
        ({"item_code": "BRG-6205", "qty": 1, "to_warehouse": "Stores"}, "are the same"),
        ({"item_code": "BRG-6205", "qty": 61}, "only 60 Nos"),
    ],
)
async def test_stock_entry_validation_never_hits_erp(
    ex: ToolExecutor, ctx: ToolContext, args: dict, error: str
) -> None:
    full = {"purpose": "Material Transfer", "from_warehouse": "Stores",
            "to_warehouse": "Line 1", **args}  # fmt: skip
    with respx.mock:  # any HTTP call would fail the test
        r = await ex.run("create_stock_entry", full, ctx)
    assert r["ok"] is False and error in r["error"]
    assert ctx.posted == []


@respx.mock
async def test_erp_error_is_surfaced(ex: ToolExecutor, ctx: ToolContext) -> None:
    respx.post(f"{BASE}/api/resource/Stock%20Entry").respond(
        417, json={"exception": "frappe.exceptions.ValidationError: Posting date is in the past"}
    )
    r = await ex.run(
        "create_stock_entry",
        {"purpose": "Material Transfer", "item_code": "BRG-6205", "qty": 1,
         "from_warehouse": "Stores", "to_warehouse": "Line 1"},
        ctx,
    )  # fmt: skip
    assert r == {"ok": False, "erp_error": "Posting date is in the past"}


def _job_card(done: float = 0, planned: float = 30) -> dict:
    return {"name": "PO-JOB00004", "docstatus": 0, "for_quantity": planned,
            "total_completed_qty": done, "time_required": 360, "item_name": "Pump Assembly",
            "time_logs": []}  # fmt: skip


@respx.mock
async def test_job_card_partial_keeps_draft(ex: ToolExecutor, ctx: ToolContext) -> None:
    respx.get(f"{BASE}/api/resource/Job%20Card/PO-JOB00004").respond(json={"data": _job_card()})
    put = respx.put(f"{BASE}/api/resource/Job%20Card/PO-JOB00004").respond(json={"data": {}})
    r = await ex.run("complete_job_card", {"name": "PO-JOB00004", "completed_qty": 10}, ctx)
    assert r["ok"] and r["completed"] is False
    sent = json.loads(put.calls.last.request.content)
    assert "docstatus" not in sent
    assert sent["time_logs"][0]["completed_qty"] == 10
    assert ctx.posted[0].summary == "10 Pump Assembly logged, 10 of 30 done"


@respx.mock
async def test_job_card_full_submits(ex: ToolExecutor, ctx: ToolContext) -> None:
    respx.get(f"{BASE}/api/resource/Job%20Card/PO-JOB00004").respond(
        json={"data": _job_card(done=10)}
    )
    put = respx.put(f"{BASE}/api/resource/Job%20Card/PO-JOB00004").respond(json={"data": {}})
    r = await ex.run("complete_job_card", {"name": "PO-JOB00004", "completed_qty": 20}, ctx)
    assert r["ok"] and r["completed"] is True
    assert json.loads(put.calls.last.request.content)["docstatus"] == 1


@respx.mock
async def test_job_card_over_completion_refused(ex: ToolExecutor, ctx: ToolContext) -> None:
    respx.get(f"{BASE}/api/resource/Job%20Card/PO-JOB00004").respond(json={"data": _job_card()})
    r = await ex.run("complete_job_card", {"name": "PO-JOB00004", "completed_qty": 50}, ctx)
    assert r == {"ok": False, "error": "PO-JOB00004 has only 30 pending of 30"}


async def test_unknown_job_card(ex: ToolExecutor, ctx: ToolContext) -> None:
    r = await ex.run("complete_job_card", {"name": "PO-JOB00099", "completed_qty": 1}, ctx)
    assert r["ok"] is False and "search_job_cards" in r["error"]


@respx.mock
async def test_material_request_defaults(ex: ToolExecutor, ctx: ToolContext) -> None:
    route = respx.post(f"{BASE}/api/resource/Material%20Request").respond(
        json={"data": {"name": "MAT-MR-2026-00001"}}
    )
    submit = respx.put(f"{BASE}/api/resource/Material%20Request/MAT-MR-2026-00001").respond(
        json={"data": {"name": "MAT-MR-2026-00001", "docstatus": 1}}
    )
    r = await ex.run(
        "create_material_request",
        {"item_code": "BRG-6205", "qty": 10, "purpose": "Purchase", "warehouse": None},
        ctx,
    )
    assert r["ok"]
    sent = json.loads(route.calls.last.request.content)
    assert sent["material_request_type"] == "Purchase"
    assert submit.called
    assert sent["items"][0]["warehouse"] == "Stores - FVD"


async def test_bad_arguments_reported(ex: ToolExecutor, ctx: ToolContext) -> None:
    r = await ex.run("search_items", {"q": "x"}, ctx)
    assert r["ok"] is False and "bad arguments" in r["error"]
    assert (await ex.run("drop_tables", {}, ctx))["ok"] is False
