"""Muse Spark agent loop: utterance -> tool calls -> ERPNext write -> readback."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal, Protocol

from openai import AsyncOpenAI

from agent.normalize import detect_language, normalize_numbers
from agent.prompt import SYSTEM_PROMPT
from agent.readback import readback
from agent.resolve import candidates
from agent.tools import TOOL_SCHEMAS, MastersCache, PostedDoc, ToolContext, ToolExecutor

log = logging.getLogger(__name__)

MAX_STEPS = 6


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


class SparkCompleter:
    """Chat Completions call to Muse Spark with the FloorVoice tools."""

    def __init__(self, api_key: str, base_url: str, model: str, reasoning_effort: str = "low"):
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=30.0)
        self.model = model
        self.reasoning_effort = reasoning_effort

    async def __call__(self, messages: list[dict[str, Any]]) -> LlmReply:
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
        log.info(
            "spark %.2fs calls=%s usage=%s",
            time.monotonic() - t,
            [c.name for c in calls],
            resp.usage.model_dump(include={"prompt_tokens", "completion_tokens"})
            if resp.usage
            else None,
        )
        return LlmReply(msg.content, calls)


@dataclass
class AgentState:
    """Conversation for one utterance, kept between /act and /clarify."""

    transcript: str
    language: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    pending_call_id: str | None = None
    ctx: ToolContext = field(default_factory=ToolContext)


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
    ) -> None:
        self.completer = completer
        self.executor = executor
        self.cache = cache

    async def start(self, transcript: str, *, home_warehouse: str | None = None) -> AgentState:
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
        state = AgentState(transcript=transcript, language=lang)
        state.ctx.transcript = transcript
        state.messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Utterance: {text}\n\nContext: "
                + json.dumps(context, ensure_ascii=False),
            },
        ]
        return state

    async def act(self, transcript: str, *, home_warehouse: str | None = None) -> tuple[
        AgentState, AgentResult
    ]:  # fmt: skip
        state = await self.start(transcript, home_warehouse=home_warehouse)
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
        return await self.run(state)

    async def run(self, state: AgentState) -> AgentResult:
        for _ in range(MAX_STEPS):
            reply = await self.completer(state.messages)
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
            posted_before = len(state.ctx.posted)
            for call in reply.tool_calls:
                args = _parse_args(call.arguments)
                if args is None:
                    self._tool_result(
                        state, call.id, {"ok": False, "error": "arguments must be a JSON object"}
                    )
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
        return AgentResult("error", _sorry(state.language))

    def _done(self, state: AgentState) -> AgentResult:
        display, speech = readback(state.ctx.posted, state.language)
        return AgentResult("done", display, speech, posted=list(state.ctx.posted))

    def _plain_reply(self, state: AgentState, content: str) -> AgentResult:
        content = content.strip()
        if state.ctx.posted:
            return self._done(state)
        if not content:
            return AgentResult("error", _sorry(state.language))
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


def _sorry(lang: str) -> str:
    if lang == "ta":
        return "மன்னிக்கவும், அதை பதிவு செய்ய முடியவில்லை. மீண்டும் சொல்லுங்கள்."
    return "Sorry, I couldn't post that. Please say it again."


def _fallback_ask(state: AgentState) -> str:
    return "எதை சொல்கிறீர்கள்?" if state.language == "ta" else "Which one do you mean?"
