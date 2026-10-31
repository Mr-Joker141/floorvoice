"""Muse Spark agent loop: utterance -> tool calls -> ERPNext write -> readback."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal, Protocol, TypeVar

from openai import AsyncOpenAI

from agent.normalize import detect_language, normalize_numbers
from agent.prompt import SYSTEM_PROMPT
from agent.readback import readback
from agent.resolve import candidates, code_numbers, confusable, spoken_numbers
from agent.tools import TOOL_SCHEMAS, MastersCache, PostedDoc, ToolContext, ToolExecutor

log = logging.getLogger(__name__)

MAX_STEPS = 6
# Spark time allowed per turn (act or clarify). With ~0.4-1 s of ASR this keeps
# release-to-confirmation under the 8 s target; over budget we ask the worker to repeat.
TURN_BUDGET_S = 6.0
HEDGE_AFTER_S = 2.5
ALT_WAIT_S = 3.0  # longest wait for the second transcript before writing anyway
WRITE_WITH_ITEM = {"create_stock_entry", "create_material_request"}
T = TypeVar("T")


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class LlmReply:
    content: str | None
    tool_calls: list[ToolCall]


class Completer(Protocol):
    async def __call__(self, messages: list[dict[str, Any]]) -> LlmReply: ...


async def hedged(make: Callable[[], Awaitable[T]], hedge_after: float | None) -> T:
    """Run `make()`; if it hasn't finished after `hedge_after` s, race a second copy.

    Spark's latency is usually ~1.5 s with occasional 6-8 s outliers; a hedged request
    cuts that tail at the cost of a duplicate call on the slow ones.
    """
    first = asyncio.ensure_future(make())
    if hedge_after is None:
        return await first
    done, _ = await asyncio.wait({first}, timeout=hedge_after)
    if done:
        return first.result()
    log.info("spark slow after %.1fs, sending hedge request", hedge_after)
    second = asyncio.ensure_future(make())
    pending = {first, second}
    try:
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.exception() is None:
                    return task.result()
                if not pending:
                    raise task.exception()  # type: ignore[misc]
        raise RuntimeError("unreachable")
    finally:
        for task in pending:
            task.cancel()


class SparkCompleter:
    """Chat Completions call to Muse Spark with the FloorVoice tools."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        reasoning_effort: str = "low",
        hedge_after: float | None = HEDGE_AFTER_S,
    ):
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=30.0)
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.hedge_after = hedge_after

    async def __call__(self, messages: list[dict[str, Any]]) -> LlmReply:
        return await hedged(lambda: self._once(messages), self.hedge_after)

    async def _once(self, messages: list[dict[str, Any]]) -> LlmReply:
        t = time.monotonic()
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,  # type: ignore[arg-type]
            tools=TOOL_SCHEMAS,  # type: ignore[arg-type]
            reasoning_effort=self.reasoning_effort,  # type: ignore[arg-type]
        )
        msg = resp.choices[0].message
        calls = [
            ToolCall(c.id, c.function.name, c.function.arguments)
            for c in (msg.tool_calls or [])
            if c.type == "function"
        ]
        content = msg.content
        if not calls and content and "invoke name=" in content:
            # Spark occasionally returns its tool call as markup in `content` instead of
            # `tool_calls` (see NOTES.md). Recover it rather than reading markup aloud.
            log.warning("spark leaked tool-call markup in content: %r", content[:500])
            calls = parse_leaked_calls(content)
            content = LEAK_BLOCK.sub("", content).strip() or None
        log.info(
            "spark %.2fs calls=%s usage=%s",
            time.monotonic() - t,
            [c.name for c in calls],
            resp.usage.model_dump(include={"prompt_tokens", "completion_tokens"})
            if resp.usage
            else None,
        )
        return LlmReply(content, calls)


LEAK_BLOCK = re.compile(r"<(\w+:)?function_calls>.*?(</(\w+:)?function_calls>|$)", re.DOTALL)
LEAK_INVOKE = re.compile(r'<(?:\w+:)?invoke name="([^"]+)">(.*?)</(?:\w+:)?invoke>', re.DOTALL)
LEAK_PARAM = re.compile(r'<(?:\w+:)?parameter name="([^"]+)">(.*?)</(?:\w+:)?parameter>', re.DOTALL)
_PARAM_TYPES: dict[str, dict[str, Any]] = {
    t["function"]["name"]: t["function"]["parameters"]["properties"] for t in TOOL_SCHEMAS
}


def _coerce(tool: str, param: str, raw: str) -> Any:
    value = html.unescape(raw.strip())
    types = _PARAM_TYPES.get(tool, {}).get(param, {}).get("type", "string")
    types = types if isinstance(types, list) else [types]
    if value == "null" and "null" in types:
        return None
    if "number" in types:
        try:
            return float(value)
        except ValueError:
            return value
    if "array" in types:
        try:
            return json.loads(value)
        except ValueError:
            return [value]
    return value


def parse_leaked_calls(content: str) -> list[ToolCall]:
    """Recover tool calls that the model wrote as XML-ish markup inside `content`."""
    calls = []
    for i, (name, body) in enumerate(LEAK_INVOKE.findall(content)):
        name = name.rsplit(".", 1)[-1]  # "default.finish" -> "finish"
        args = {p: _coerce(name, p, v) for p, v in LEAK_PARAM.findall(body)}
        calls.append(ToolCall(f"leak_{uuid.uuid4().hex[:12]}_{i}", name, json.dumps(args)))
    return calls


@dataclass
class AgentState:
    """Conversation for one utterance, kept between /act and /clarify."""

    transcript: str
    language: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    pending_call_id: str | None = None
    ctx: ToolContext = field(default_factory=ToolContext)
    # Second, independent transcript of the same audio (file endpoint), used to catch a
    # misheard part number before writing. None in text mode and after a clarification.
    alt_transcript: asyncio.Future[str] | None = None


@dataclass
class AgentResult:
    kind: Literal["done", "ask", "finish", "error"]
    text: str
    speech: str = ""
    options: list[str] = field(default_factory=list)
    posted: list[PostedDoc] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.speech = self.speech or self.text


class Agent:
    def __init__(
        self,
        completer: Completer,
        executor: ToolExecutor,
        cache: MastersCache,
        *,
        turn_budget: float = TURN_BUDGET_S,
    ) -> None:
        self.completer = completer
        self.executor = executor
        self.cache = cache
        self.turn_budget = turn_budget

    async def start(
        self,
        transcript: str,
        *,
        home_warehouse: str | None = None,
        alt_transcript: asyncio.Future[str] | None = None,
    ) -> AgentState:
        text = normalize_numbers(transcript.strip())
        lang = detect_language(text)
        masters = await self.cache.get()
        cands = candidates(masters, text)
        home = masters.warehouse(home_warehouse or "") or masters.warehouse(
            self.executor.default_warehouse
        )
        context = {
            "today": date.today().isoformat(),
            "language": "Tamil" if lang == "ta" else "English",
            "home_warehouse": home["name"] if home else self.executor.default_warehouse,
            "candidates": cands,
        }
        state = AgentState(transcript=transcript, language=lang, alt_transcript=alt_transcript)
        state.ctx.transcript = transcript
        state.messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Utterance: {text}\n\nContext: "
                + json.dumps(context, ensure_ascii=False)
                + ("\n\nReply language: Tamil (Tamil script)." if lang == "ta" else ""),
            },
        ]
        return state

    async def act(
        self,
        transcript: str,
        *,
        home_warehouse: str | None = None,
        alt_transcript: asyncio.Future[str] | None = None,
    ) -> tuple[AgentState, AgentResult]:
        state = await self.start(
            transcript, home_warehouse=home_warehouse, alt_transcript=alt_transcript
        )
        return state, await self.run(state)

    async def clarify(self, state: AgentState, answer: str) -> AgentResult:
        answer = normalize_numbers(answer.strip())
        if state.pending_call_id:
            state.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": state.pending_call_id,
                    "content": json.dumps({"answer": answer}, ensure_ascii=False),
                }
            )
        else:
            state.messages.append({"role": "user", "content": answer})
        state.pending_call_id = None
        state.alt_transcript = None  # the worker has now said explicitly what they meant
        return await self.run(state)

    async def run(self, state: AgentState) -> AgentResult:
        deadline = time.monotonic() + self.turn_budget
        for _ in range(MAX_STEPS):
            try:
                reply = await asyncio.wait_for(
                    self.completer(state.messages), max(0.1, deadline - time.monotonic())
                )
            except TimeoutError:
                log.warning("turn over %.1fs budget for %r", self.turn_budget, state.transcript)
                if state.ctx.posted:
                    return self._done(state)
                return AgentResult("error", _sorry(state.language, state.transcript))
            assistant: dict[str, Any] = {"role": "assistant", "content": reply.content or ""}
            if reply.tool_calls:
                assistant["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": c.arguments},
                    }
                    for c in reply.tool_calls
                ]
            state.messages.append(assistant)

            if not reply.tool_calls:
                return self._plain_reply(state, reply.content or "")

            terminal: tuple[ToolCall, dict[str, Any]] | None = None
            confirm: tuple[str, list[str]] | None = None
            posted_before = len(state.ctx.posted)
            for call in reply.tool_calls:
                args = _parse_args(call.arguments)
                if args is None:
                    self._tool_result(
                        state, call.id, {"ok": False, "error": "arguments must be a JSON object"}
                    )
                    continue
                if confirm:
                    self._tool_result(state, call.id, {"ok": False, "error": "skipped"})
                    continue
                if call.name in WRITE_WITH_ITEM and (
                    confirm := await self._number_doubt(state, args, deadline)
                ):
                    self._tool_result(state, call.id, {
                        "ok": False,
                        "error": "not posted: speech recognition is unsure of the item "
                        "number; the worker is being asked which one",
                    })  # fmt: skip
                    continue
                if call.name in ("ask_user", "finish"):
                    if terminal is None:
                        terminal = (call, args)
                    else:
                        self._tool_result(state, call.id, {"ok": False, "error": "ignored"})
                    continue
                result = await self.executor.run(call.name, args, state.ctx)
                log.info("tool %s %s -> %s", call.name, args, _short_json(result))
                self._tool_result(state, call.id, result)

            if confirm:
                if terminal:
                    self._tool_result(state, terminal[0].id, {"ok": False, "error": "ignored"})
                return self._ask(state, *confirm)
            if len(state.ctx.posted) > posted_before:
                if terminal:
                    self._tool_result(state, terminal[0].id, {"ok": True})
                return self._done(state)
            if terminal:
                call, args = terminal
                if call.name == "ask_user":
                    state.pending_call_id = call.id
                    question = str(args.get("question") or "").strip()
                    options = [str(o) for o in (args.get("options") or []) if str(o).strip()]
                    return AgentResult("ask", question or _fallback_ask(state), options=options)
                self._tool_result(state, call.id, {"ok": True})
                return AgentResult("finish", str(args.get("summary") or "").strip() or "OK.")
        log.warning("agent hit MAX_STEPS for %r", state.transcript)
        return AgentResult("error", _sorry(state.language, state.transcript))

    async def _number_doubt(
        self, state: AgentState, args: dict[str, Any], deadline: float
    ) -> tuple[str, list[str]] | None:
        """(question, options) if the second transcript disagrees on the item number."""
        if state.alt_transcript is None:
            return None
        m = await self.cache.get()
        numbers = code_numbers(str(args.get("item_code", "")))
        heard = spoken_numbers(normalize_numbers(state.transcript))
        doubtful = {n: confusable(m, n) for n in numbers & heard if confusable(m, n)}
        if not doubtful:
            return None
        wait = max(0.5, min(ALT_WAIT_S, deadline - time.monotonic() - 0.5))
        try:
            alt = await asyncio.wait_for(asyncio.shield(state.alt_transcript), wait)
        except (TimeoutError, Exception) as e:  # noqa: BLE001 - check is best-effort
            log.info("second transcript unavailable (%s); posting as heard", type(e).__name__)
            return None
        alt_numbers = spoken_numbers(normalize_numbers(alt))
        for number, siblings in doubtful.items():
            rivals = sorted(siblings & alt_numbers)
            if number not in alt_numbers and rivals:
                log.warning("number doubt %s vs %s: %r / %r", number, rivals,
                            state.transcript, alt)  # fmt: skip
                choices = [number, *rivals]
                names = []
                for n in choices:
                    item = next((i for i in m.items if n in code_numbers(i["item_code"])), None)
                    names.append(item["item_name"] if item else n)
                if state.language == "ta":
                    question = " இல்லை ".join(f"{n}-ஆ" for n in choices) + "?"
                else:
                    question = f"Did you say {' or '.join(choices)}?"
                return question, names
        return None

    def _ask(self, state: AgentState, question: str, options: list[str]) -> AgentResult:
        """Server-initiated question, recorded as an ask_user call so clarify resumes it."""
        call_id = f"verify_{uuid.uuid4().hex[:12]}"
        args = {"question": question, "options": options}
        state.messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": "ask_user", "arguments": json.dumps(args)}}],
        })  # fmt: skip
        state.pending_call_id = call_id
        state.alt_transcript = None
        return AgentResult("ask", question, options=options)

    def _done(self, state: AgentState) -> AgentResult:
        display, speech = readback(state.ctx.posted, state.language)
        return AgentResult("done", display, speech, posted=list(state.ctx.posted))

    def _plain_reply(self, state: AgentState, content: str) -> AgentResult:
        content = content.strip()
        if state.ctx.posted:
            return self._done(state)
        if not content:
            return AgentResult("error", _sorry(state.language, state.transcript))
        if content.endswith("?"):
            return AgentResult("ask", content)
        return AgentResult("finish", content)

    @staticmethod
    def _tool_result(state: AgentState, call_id: str, result: dict[str, Any]) -> None:
        state.messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps(result, ensure_ascii=False),
            }
        )


def _parse_args(raw: str) -> dict[str, Any] | None:
    try:
        args = json.loads(raw or "{}")
    except ValueError:
        return None
    return args if isinstance(args, dict) else None


def _short_json(obj: Any, limit: int = 300) -> str:
    s = json.dumps(obj, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + "…"


def _sorry(lang: str, heard: str = "") -> str:
    heard = heard.strip().rstrip(".,")
    if lang == "ta":
        prefix = f'நான் கேட்டது: "{heard}". ' if heard else ""
        return prefix + "பதிவு செய்ய முடியவில்லை. பொருள், அளவு, இடம் சொல்லி மீண்டும் சொல்லுங்கள்."
    prefix = f'I heard "{heard}". ' if heard else ""
    return prefix + "I couldn't post that. Please say it again with the item, quantity and place."


def _fallback_ask(state: AgentState) -> str:
    return "எதை சொல்கிறீர்கள்?" if state.language == "ta" else "Which one do you mean?"
