"""Feature B: worker PIN on every transaction."""

import dataclasses
import json

import pytest
import respx
from fastapi.testclient import TestClient

from agent.loop import Agent, LlmReply
from agent.resolve import Masters
from agent.runtime import Runtime
from agent.tools import PostedDoc, ToolContext, ToolExecutor
from agent.vision import NO_MATCH
from erp.client import ErpClient
from erp.masters import hash_pin
from seed import demo_data as d
from seed.seed import PinCollision, check_pins
from server import app as app_module
from server.app import PIN_UNKNOWN, HistoryEntry, Service, Store, UnknownWorker
from server.config import get_settings
from tests.test_agent_loop import FakeExecutor, ScriptedLlm, call
from tests.test_photo import FakeVision
from tests.test_tools import BASE, FixedCache, _job_card

SALT = "test-salt"
KUMAR, PRIYA, NIMAL = "HR-EMP-00001", "HR-EMP-00002", "HR-EMP-00003"


@pytest.fixture
def staffed(masters: Masters) -> Masters:
    masters.workers = [
        {"name": f"HR-EMP-{i:05d}", "employee_name": f"{w['first_name']} {w['last_name']}",
         "floorvoice_pin": hash_pin(w["pin"], SALT),
         "floorvoice_home_warehouse": f"{w['home']} - FVD", "floorvoice_language": w["language"]}
        for i, w in enumerate(d.WORKERS, start=1)
    ]  # fmt: skip
    return masters


class UndoExecutor(FakeExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.undone: list[str] = []

    async def undo(self, doc: PostedDoc) -> str:
        self.undone.append(doc.name)
        return doc.name


def make_service(staffed: Masters, tmp_path, *replies: LlmReply) -> tuple[Service, ScriptedLlm]:
    llm = ScriptedLlm(*replies)
    agent = Agent(llm, UndoExecutor(), FixedCache(staffed))  # type: ignore[arg-type]
    rt = Runtime(None, FixedCache(staffed), agent, FakeVision(NO_MATCH))  # type: ignore[arg-type]
    settings = dataclasses.replace(get_settings(), data_dir=tmp_path, pin_salt=SALT)
    return Service(settings, rt, Store(tmp_path / "history.jsonl")), llm


@pytest.fixture
def client(staffed: Masters, tmp_path, monkeypatch):
    monkeypatch.setenv("FLOORVOICE_KEY", "")  # access key checked elsewhere; PIN is the subject
    svc, _ = make_service(staffed, tmp_path)
    app_module.app.state.service = svc
    return TestClient(app_module.app)


def pin(p: str) -> dict[str, str]:
    return {"X-FloorVoice-Pin": p}


# --- hashing and lookup ---------------------------------------------------------------------


def test_hash_pin_is_salted_and_stable() -> None:
    h = hash_pin("1111", SALT)
    assert h == hash_pin("1111", SALT) and len(h) == 64
    assert h != hash_pin("1111", "other-salt") and h != hash_pin("1112", SALT)
    assert "1111" not in h
    with pytest.raises(ValueError):
        hash_pin("1111", "")


def test_seed_rejects_pin_collision() -> None:
    check_pins(d.WORKERS)
    twins = [*d.WORKERS, {**d.WORKERS[0], "first_name": "Other"}]
    with pytest.raises(PinCollision, match="same PIN"):
        check_pins(twins)
    with pytest.raises(PinCollision, match="4 digits"):
        check_pins([{**d.WORKERS[0], "pin": "12a4"}])


async def test_lookup(staffed: Masters, tmp_path) -> None:
    svc, _ = make_service(staffed, tmp_path)
    assert (await svc.worker("2222"))["name"] == PRIYA
    for bad in ("9999", "", None, "12a4", "11111", " 22"):
        with pytest.raises(UnknownWorker):
            await svc.worker(bad)
    svc.settings = dataclasses.replace(svc.settings, pin_salt="")
    with pytest.raises(UnknownWorker):
        await svc.worker("2222")


# --- unknown PIN rejected on every endpoint -------------------------------------------------

ENDPOINTS = [
    ("get", "/me", {}),
    ("get", "/history", {}),
    ("post", "/act", {"json": {"transcript": "send me ten bearings", "session_id": "s"}}),
    ("post", "/clarify", {"json": {"session_id": "s", "answer": "6205"}}),
    ("post", "/clarify-photo",
     {"data": {"session_id": "s"}, "files": {"image": ("p.jpg", b"\xff\xd8", "image/jpeg")}}),
    ("post", "/undo", {"json": {"doctype": "Stock Entry", "name": "MAT-STE-2026-00001"}}),
]  # fmt: skip


@pytest.mark.parametrize(("method", "path", "kwargs"), ENDPOINTS)
@pytest.mark.parametrize("given", ["9999", "", "abcd"])
def test_unknown_pin_rejected(client: TestClient, method, path, kwargs, given) -> None:
    r = getattr(client, method)(path, headers=pin(given), **kwargs)
    assert r.status_code == 403 and r.json()["detail"] == PIN_UNKNOWN


def test_unknown_pin_rejected_on_audio_socket(client: TestClient) -> None:
    with client.websocket_connect("/audio") as ws:
        ws.send_json({"type": "start", "session_id": "s", "sample_rate": 24000, "pin": "9999"})
        assert ws.receive_json() == {"type": "error", "message": PIN_UNKNOWN, "code": "pin"}


def test_me(client: TestClient) -> None:
    r = client.get("/me", headers=pin("3333"))
    assert r.json() == {"id": NIMAL, "name": "Nimal Perera", "home_warehouse": "Line 1 - FVD",
                        "language": "si"}  # fmt: skip


# --- worker context drives warehouse and language -------------------------------------------


def _context(llm: ScriptedLlm) -> dict:
    return json.loads(llm.seen[0][1]["content"].split("Context: ", 1)[1].split("\n\n")[0])


async def test_home_line_and_language_from_worker(staffed: Masters, tmp_path) -> None:
    svc, llm = make_service(staffed, tmp_path, LlmReply(None, [call("finish", summary="ok")]))
    await svc.act("මට බෝල්ට් දහයක් එවන්න", "s1", worker=staffed.workers[3])  # Dilani: Line 2, si
    ctx = _context(llm)
    assert ctx["home_warehouse"] == "Line 2 - FVD" and ctx["language"] == "Sinhala"
    entry = svc.store.history[-1]
    assert (entry.worker_id, entry.worker_name) == ("HR-EMP-00004", "Dilani Fernando")


async def test_romanized_sinhala_follows_worker_language(staffed: Masters, tmp_path) -> None:
    svc, llm = make_service(staffed, tmp_path, LlmReply(None, [call("finish", summary="ok")]))
    await svc.act("job card tuna ivarai kali visak", "s1", worker=staffed.workers[2])  # Nimal: si
    assert llm.seen[0][1]["content"].startswith("Utterance: job card 3 ivarai kali 20")
    assert _context(llm)["language"] == "Sinhala"


async def test_language_override_beats_worker(staffed: Masters, tmp_path) -> None:
    svc, llm = make_service(staffed, tmp_path, LlmReply(None, [call("finish", summary="ok")]))
    await svc.act("send me ten bolts", "s1", speech="en", worker=staffed.workers[2])  # Nimal: si
    ctx = _context(llm)
    assert ctx["language"] == "English" and ctx["home_warehouse"] == "Line 1 - FVD"


@pytest.mark.parametrize(
    ("text", "worker", "lang"),
    [
        ("බෙයාරිං 10ක් එවන්න", 0, "Sinhala"),  # Kumar (ta) types Sinhala
        ("பத்து bearing அனுப்பு", 2, "Tamil"),  # Nimal (si) types Tamil
        ("send me ten bolts", 0, "English"),  # English text: English reply, as before
    ],
)
async def test_typed_script_picks_reply_language(staffed: Masters, tmp_path, text, worker,
                                                 lang) -> None:  # fmt: skip
    svc, llm = make_service(staffed, tmp_path, LlmReply(None, [call("finish", summary="ok")]))
    await svc.act(text, "s1", worker=staffed.workers[worker], typed=True)
    assert _context(llm)["language"] == lang


async def test_spoken_sinhala_letters_keep_tamil_worker_language(staffed: Masters,
                                                                 tmp_path) -> None:  # fmt: skip
    svc, llm = make_service(staffed, tmp_path, LlmReply(None, [call("finish", summary="ok")]))
    await svc.act("බෙයාරිං 10ක් එවන්න", "s1", worker=staffed.workers[0])  # voice: ASR may misscript
    assert _context(llm)["language"] == "Tamil"


async def test_worker_reaches_tool_context(staffed: Masters, tmp_path) -> None:
    svc, _ = make_service(
        staffed, tmp_path, LlmReply(None, [call("ask_user", question="How many?")])
    )
    await svc.act("send me bearing 6205", "s1", worker=staffed.workers[0])
    ctx = svc.store.pending["s1"].state.ctx
    assert (ctx.worker_id, ctx.worker_name) == (KUMAR, "Kumar Selvaraj")


def test_clarify_by_other_worker_rejected(client: TestClient) -> None:
    svc: Service = app_module.app.state.service
    svc.rt.agent.completer = ScriptedLlm(LlmReply(None, [call("ask_user", question="How many?")]))
    assert client.post("/act", headers=pin("1111"),
                       json={"transcript": "send me bearing 6205", "session_id": "q"}).json()[
        "kind"] == "ask"  # fmt: skip
    r = client.post("/clarify", headers=pin("2222"), json={"session_id": "q", "answer": "10"})
    assert r.status_code == 403
    assert "q" in svc.store.pending  # Kumar can still answer


# --- undo only by the poster, once ----------------------------------------------------------


def _posted(svc: Service, worker_id: str, worker_name: str, name: str, session: str) -> None:
    svc.store.record(HistoryEntry(
        at="2026-10-31T10:00:00", session_id=session, transcript="move 2 bearing", kind="done",
        text=f"Posted {name}", worker_id=worker_id, worker_name=worker_name,
        documents=[{"doctype": "Stock Entry", "name": name, "summary": "2 Ball Bearing 6205",
                    "url": "", "undo": "{}"}],
    ))  # fmt: skip


def test_undo_by_other_worker_rejected(client: TestClient) -> None:
    svc: Service = app_module.app.state.service
    _posted(svc, KUMAR, "Kumar Selvaraj", "MAT-STE-2026-00030", "u1")
    body = {"doctype": "Stock Entry", "name": "MAT-STE-2026-00030"}
    r = client.post("/undo", headers=pin("2222"), json=body)
    assert (
        r.status_code == 403
        and r.json()["detail"] == "Only Kumar Selvaraj can undo MAT-STE-2026-00030"
    )
    assert svc.rt.agent.executor.undone == []
    assert client.post("/undo", headers=pin("1111"), json=body).status_code == 200
    assert svc.rt.agent.executor.undone == ["MAT-STE-2026-00030"]
    assert client.post("/undo", headers=pin("1111"), json=body).status_code == 409  # once


# --- history per worker ---------------------------------------------------------------------


def test_history_filters_by_worker(client: TestClient) -> None:
    svc: Service = app_module.app.state.service
    _posted(svc, KUMAR, "Kumar Selvaraj", "MAT-STE-2026-00031", "h1")
    _posted(svc, PRIYA, "Priya Nadarajah", "MAT-STE-2026-00032", "h2")
    _posted(svc, KUMAR, "Kumar Selvaraj", "MAT-STE-2026-00033", "h3")
    mine = client.get("/history", headers=pin("1111")).json()
    assert [h["session_id"] for h in mine] == ["h3", "h1"]
    assert all(h["worker_id"] == KUMAR for h in mine)
    everyone = client.get("/history?all=1", headers=pin("1111")).json()
    assert [h["session_id"] for h in everyone] == ["h3", "h2", "h1"]
    assert client.get("/history", headers=pin("6666")).json() == []


# --- documents are stamped with the worker --------------------------------------------------


@pytest.fixture
async def ex(masters: Masters):
    erp = ErpClient(BASE, "k", "s")
    yield ToolExecutor(erp, FixedCache(masters), company="FloorVoice Demo Works",
                       default_warehouse="Line 1 - FVD")  # fmt: skip
    await erp.aclose()


@pytest.fixture
def kumar_ctx() -> ToolContext:
    return ToolContext(transcript="move 2 bearing 6205 from stores to line 2", worker_id=KUMAR,
                       worker_name="Kumar Selvaraj")  # fmt: skip


@respx.mock
async def test_stock_entry_stamped(ex: ToolExecutor, kumar_ctx: ToolContext) -> None:
    route = respx.post(f"{BASE}/api/resource/Stock%20Entry").respond(json={"data": {"name": "SE1"}})
    respx.put(f"{BASE}/api/resource/Stock%20Entry/SE1").respond(json={"data": {"name": "SE1"}})
    await ex.run("create_stock_entry", {"purpose": "Material Transfer", "item_code": "BRG-6205",
                 "qty": 2, "from_warehouse": "Stores", "to_warehouse": "Line 2"}, kumar_ctx)  # fmt: skip
    sent = json.loads(route.calls.last.request.content)
    assert sent["floorvoice_worker"] == KUMAR
    assert sent["remarks"].startswith("Kumar Selvaraj: Posted by FloorVoice from voice: move 2")


@respx.mock
async def test_material_request_stamped(ex: ToolExecutor, kumar_ctx: ToolContext) -> None:
    route = respx.post(f"{BASE}/api/resource/Material%20Request").respond(
        json={"data": {"name": "MR1"}}
    )
    respx.put(f"{BASE}/api/resource/Material%20Request/MR1").respond(json={"data": {"name": "MR1"}})
    await ex.run("create_material_request", {"item_code": "BRG-6205", "qty": 10,
                 "purpose": "Material Transfer", "warehouse": None}, kumar_ctx)  # fmt: skip
    sent = json.loads(route.calls.last.request.content)
    assert sent["floorvoice_worker"] == KUMAR
    assert sent["floorvoice_remarks"].startswith("Kumar Selvaraj: ")


@respx.mock
async def test_job_card_time_log_stamped(ex: ToolExecutor, kumar_ctx: ToolContext) -> None:
    respx.get(f"{BASE}/api/resource/Job%20Card/PO-JOB00004").respond(json={"data": _job_card()})
    put = respx.put(f"{BASE}/api/resource/Job%20Card/PO-JOB00004").respond(json={"data": {}})
    await ex.run("complete_job_card", {"name": "PO-JOB00004", "completed_qty": 10}, kumar_ctx)
    sent = json.loads(put.calls.last.request.content)
    assert sent["time_logs"][-1]["employee"] == KUMAR
    assert sent["remarks"].startswith("Kumar Selvaraj: ")


@respx.mock
async def test_no_worker_no_stamp(ex: ToolExecutor) -> None:
    route = respx.post(f"{BASE}/api/resource/Stock%20Entry").respond(json={"data": {"name": "SE2"}})
    respx.put(f"{BASE}/api/resource/Stock%20Entry/SE2").respond(json={"data": {"name": "SE2"}})
    ctx = ToolContext(transcript="move 2 bearing 6205 from stores to line 2")
    await ex.run("create_stock_entry", {"purpose": "Material Transfer", "item_code": "BRG-6205",
                 "qty": 2, "from_warehouse": "Stores", "to_warehouse": "Line 2"}, ctx)  # fmt: skip
    sent = json.loads(route.calls.last.request.content)
    assert sent["floorvoice_worker"] is None and sent["remarks"].startswith("Posted by")
