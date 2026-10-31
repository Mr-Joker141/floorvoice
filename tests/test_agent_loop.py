import asyncio
import json
from typing import Any

import pytest

from agent.loop import Agent, LlmReply, ToolCall
from agent.resolve import Masters
from agent.tools import PostedDoc, ToolContext
from tests.test_tools import FixedCache


class ScriptedLlm:
    def __init__(self, *replies: LlmReply) -> None:
        self.replies = list(replies)
        self.seen: list[list[dict[str, Any]]] = []

    async def __call__(self, messages: list[dict[str, Any]]) -> LlmReply:
        self.seen.append([dict(m) for m in messages])
        return self.replies.pop(0)


class FakeExecutor:
    default_warehouse = "Stores - FVD"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def run(self, name: str, args: dict, ctx: ToolContext) -> dict:
        self.calls.append((name, args))
        if name.startswith("create_") or name == "complete_job_card":
            ctx.posted.append(PostedDoc("Material Request", "MAT-MR-2026-00007", "10 things"))
            return {"ok": True, "name": "MAT-MR-2026-00007"}
        return {"items": []}


def call(name: str, i: int = 1, **args: Any) -> ToolCall:
    return ToolCall(f"call_{i}", name, json.dumps(args))


@pytest.fixture
def make(masters: Masters):
    def _make(*replies: LlmReply) -> tuple[Agent, ScriptedLlm, FakeExecutor]:
        llm, ex = ScriptedLlm(*replies), FakeExecutor()
        return Agent(llm, ex, FixedCache(masters)), llm, ex  # type: ignore[arg-type]

    return _make


async def test_first_message_has_candidates_and_digits(make) -> None:
    agent, llm, _ = make(LlmReply(None, [call("finish", summary="ok")]))
    await agent.act("send me ten bearings", home_warehouse="Line 1")
    user = llm.seen[0][1]["content"]
    assert user.startswith("Utterance: send me 10 bearings")
    ctx = json.loads(user.split("Context: ", 1)[1])
    assert ctx["home_warehouse"] == "Line 1 - FVD"
    assert len(ctx["candidates"]["items"]) == 4
    assert llm.seen[0][0]["role"] == "system"


async def test_write_then_server_readback(make) -> None:
    agent, llm, _ex = make(
        LlmReply(None, [call("create_material_request", item_code="BRG-6205", qty=10,
                             purpose="Purchase", warehouse=None)])
    )  # fmt: skip
    _, r = await agent.act("order ten bearing 6205")
    assert r.kind == "done"
    assert r.text == "Posted MAT-MR-2026-00007: 10 things."
    assert r.speech == "Done. 10 things. Material request 7."
    assert len(llm.seen) == 1  # no extra round-trip for the confirmation


async def test_ask_then_clarify_resumes_with_tool_result(make) -> None:
    agent, llm, _ex = make(
        LlmReply(None, [call("ask_user", 1, question="Which bearing?", options=["6201", "6205"])]),
        LlmReply(None, [call("create_material_request", 2, item_code="BRG-6205", qty=10,
                             purpose="Material Transfer", warehouse=None)]),
    )  # fmt: skip
    state, r = await agent.act("send me bearings")
    assert (r.kind, r.text, r.options) == ("ask", "Which bearing?", ["6201", "6205"])
    assert state.pending_call_id == "call_1"
    r2 = await agent.clarify(state, "six two zero five, ten")
    assert r2.kind == "done"
    tool_msg = llm.seen[1][-1]
    assert tool_msg == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": json.dumps({"answer": "6205, 10"}),
    }


async def test_parallel_searches_then_write(make) -> None:
    agent, llm, ex = make(
        LlmReply("looking", [call("search_items", 1, query="seal"),
                             call("search_warehouses", 2, query="line 1")]),
        LlmReply(None, [call("create_stock_entry", 3, purpose="Material Transfer",
                             item_code="SEAL-35", qty=5, from_warehouse="Stores",
                             to_warehouse="Line 1")]),
    )  # fmt: skip
    _, r = await agent.act("move five seals to line one")
    assert r.kind == "done"
    assert [c[0] for c in ex.calls] == ["search_items", "search_warehouses", "create_stock_entry"]
    # every tool call id got a result before the next request
    second = llm.seen[1]
    assert [m.get("tool_call_id") for m in second if m["role"] == "tool"] == ["call_1", "call_2"]


async def test_plain_text_question_becomes_ask(make) -> None:
    agent, _, _ = make(LlmReply("How many do you need?", []))
    state, r = await agent.act("send me grease please")
    assert r.kind == "ask" and state.pending_call_id is None


async def test_finish_without_write(make) -> None:
    agent, _, _ = make(LlmReply(None, [call("finish", summary="I can only post stock moves.")]))
    _, r = await agent.act("what's the weather today")
    assert (r.kind, r.text) == ("finish", "I can only post stock moves.")


async def test_bad_json_arguments_are_reported_back(make) -> None:
    agent, llm, _ = make(
        LlmReply(None, [ToolCall("c1", "search_items", "{not json")]),
        LlmReply(None, [call("finish", summary="ok")]),
    )
    await agent.act("move five seals")
    assert "JSON object" in llm.seen[1][-1]["content"]


async def test_step_limit(make) -> None:
    agent, _, _ = make(*[LlmReply(None, [call("search_items", i, query="x")]) for i in range(10)])
    _, r = await agent.act("loop forever")
    assert r.kind == "error"


async def test_tamil_readback(make) -> None:
    agent, _, _ = make(
        LlmReply(None, [call("create_material_request", item_code="BLT-M8X30", qty=10,
                             purpose="Material Transfer", warehouse=None)])
    )  # fmt: skip
    _, r = await agent.act("பத்து போல்ட் M8 வேண்டும்")
    assert r.text.startswith("பதிவானது MAT-MR-2026-00007")


def test_parse_leaked_tool_call_markup() -> None:
    from agent.loop import parse_leaked_calls

    content = (
        '<atem:function_calls>\n<atem:invoke name="default.create_stock_entry">\n'
        '<atem:parameter name="purpose">Material Transfer</atem:parameter>\n'
        '<atem:parameter name="item_code">6205</atem:parameter>\n'
        '<atem:parameter name="qty">2</atem:parameter>\n'
        '<atem:parameter name="from_warehouse">Stores &amp; Co</atem:parameter>\n'
        '<atem:parameter name="to_warehouse">null</atem:parameter>\n'
        "</atem:invoke>\n</atem:function_calls>"
    )
    [c] = parse_leaked_calls(content)
    assert c.name == "create_stock_entry"
    assert json.loads(c.arguments) == {
        "purpose": "Material Transfer", "item_code": "6205", "qty": 2.0,
        "from_warehouse": "Stores & Co", "to_warehouse": None,
    }  # fmt: skip


async def test_turn_budget_turns_slow_model_into_repeat(masters: Masters) -> None:
    import asyncio

    class SlowLlm:
        async def __call__(self, messages: list[dict[str, Any]]) -> LlmReply:
            await asyncio.sleep(1)
            return LlmReply(None, [call("finish", summary="late")])

    agent = Agent(SlowLlm(), FakeExecutor(), FixedCache(masters), turn_budget=0.05)  # type: ignore[arg-type]
    _, r = await agent.act("move five seals to line one")
    assert r.kind == "error" and "say it again" in r.text


async def test_hedged_returns_faster_copy() -> None:
    import asyncio

    from agent.loop import hedged

    delays = iter([1.0, 0.01])
    started: list[float] = []

    async def make() -> str:
        d = next(delays)
        started.append(d)
        await asyncio.sleep(d)
        return f"slept {d}"

    assert await hedged(make, 0.05) == "slept 0.01"
    assert started == [1.0, 0.01]


async def test_hedged_fast_path_sends_once() -> None:
    from agent.loop import hedged

    calls = 0

    async def make() -> int:
        nonlocal calls
        calls += 1
        return calls

    assert await hedged(make, 0.5) == 1 and calls == 1


async def test_hedged_survives_one_failure() -> None:
    import asyncio

    from agent.loop import hedged

    n = 0

    async def make() -> str:
        nonlocal n
        n += 1
        if n == 1:
            await asyncio.sleep(0.1)
            raise RuntimeError("boom")
        await asyncio.sleep(0.2)
        return "ok"

    assert await hedged(make, 0.01) == "ok"


def _future(value: str | Exception) -> asyncio.Future[str]:
    import asyncio

    fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    if isinstance(value, Exception):
        fut.set_exception(value)
    else:
        fut.set_result(value)
    return fut


def _transfer(code: str, i: int = 1) -> ToolCall:
    return call("create_stock_entry", i, purpose="Material Transfer", item_code=code, qty=2,
                from_warehouse="Stores", to_warehouse="Line 2")  # fmt: skip


async def test_number_doubt_asks_instead_of_posting(make) -> None:
    agent, llm, ex = make(LlmReply(None, [_transfer("BRG-6305")]),
                          LlmReply(None, [_transfer("BRG-6205", 2)]))  # fmt: skip
    alt = _future("இரண்டு bearing 6205 store ல இருந்து line two க்கு மாற்று")
    state, r = await agent.act("இரண்டு bearing 6305 store ல line two கு மாற்று", alt_transcript=alt)
    assert r.kind == "ask"
    assert r.text == "6305-ஆ இல்லை 6205-ஆ?"
    assert r.options == ["Ball Bearing 6305", "Ball Bearing 6205"]
    assert ex.calls == []  # nothing written
    assert state.pending_call_id.startswith("verify_")
    r2 = await agent.clarify(state, "Ball Bearing 6205")
    assert r2.kind == "done"
    assert ex.calls[0][1]["item_code"] == "BRG-6205"
    # the conversation stays well-formed: every tool call got exactly one result
    ids = [
        c["id"] for m in llm.seen[1] if m["role"] == "assistant" for c in m.get("tool_calls", [])
    ]
    results = [m["tool_call_id"] for m in llm.seen[1] if m["role"] == "tool"]
    assert sorted(ids) == sorted(results)


@pytest.mark.parametrize(
    "alt",
    ["move 2 bearing 6305 from stores to line two", "move two bearing from stores", RuntimeError()],
)
async def test_number_doubt_posts_when_no_conflict(make, alt) -> None:
    agent, _, ex = make(LlmReply(None, [_transfer("BRG-6305")]))
    _, r = await agent.act("move 2 bearing 6305 from stores to line 2", alt_transcript=_future(alt))
    assert r.kind == "done" and ex.calls[0][1]["item_code"] == "BRG-6305"


async def test_no_doubt_check_for_items_without_neighbours(make) -> None:
    agent, _, _ex = make(LlmReply(None, [call("create_stock_entry", purpose="Material Issue",
                                             item_code="GRS-EP2", qty=2, from_warehouse="Line 2",
                                             to_warehouse=None)]))  # fmt: skip
    _, r = await agent.act("issue 2 kg grease from line 2", alt_transcript=_future("issue 9 kg"))
    assert r.kind == "done"
