"""Photo disambiguation: Muse Spark reads a part / box label and picks one of the candidates.

Only used when a pending question is over an item. The model may answer with one of the
candidate item codes or null; anything else is treated as "couldn't read it".
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from openai import AsyncOpenAI

from agent.loop import hedged

log = logging.getLogger(__name__)

PHOTO_BUDGET_S = 6.0
# A vision call takes ~2.4-3.8 s, so the text agent's 2.5 s hedge would double most calls.
VISION_HEDGE_AFTER_S = 3.5
Confidence = Literal["high", "medium", "low"]
CONFIDENCES: tuple[Confidence, ...] = ("high", "medium", "low")

VISION_PROMPT = """\
You identify an industrial part from a photo taken on a factory floor. The photo shows the \
part itself, its box, or its label. Read any printed text (part numbers, sizes, brand codes).

Choose exactly one item from the candidate list below, or null if the photo does not clearly \
show one of them. Never invent an item that is not in the list. Distinguish close part numbers \
digit by digit (6205 is not 6305).

confidence: "high" when a printed code on the label matches the candidate; "medium" when the \
match is strong but partly inferred (e.g. size and type match, code partly hidden); "low" when \
you are guessing. label_text: the text you read on the label, verbatim, "" if none.

Candidates:
{candidates}"""


@dataclass(frozen=True)
class PhotoMatch:
    item_code: str | None
    label_text: str
    confidence: Confidence

    @property
    def usable(self) -> bool:
        return self.item_code is not None and self.confidence in ("high", "medium")


NO_MATCH = PhotoMatch(None, "", "low")


def photo_schema(codes: list[str]) -> dict[str, Any]:
    """response_format for the reply; item_code is limited to the candidate codes."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "part_match",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "item_code": {"type": ["string", "null"], "enum": [*codes, None]},
                    "label_text": {"type": "string"},
                    "confidence": {"type": "string", "enum": list(CONFIDENCES)},
                },
                "required": ["item_code", "label_text", "confidence"],
                "additionalProperties": False,
            },
        },
    }


def parse_photo_reply(content: str | None, codes: list[str]) -> PhotoMatch:
    """Validate the model's JSON. Out-of-set codes, bad JSON or bad fields -> no match."""
    try:
        data = json.loads(content or "")
    except ValueError:
        log.warning("photo reply is not JSON: %r", (content or "")[:200])
        return NO_MATCH
    if not isinstance(data, dict):
        return NO_MATCH
    label = str(data.get("label_text") or "").strip()[:300]
    confidence = data.get("confidence")
    if confidence not in CONFIDENCES:
        return PhotoMatch(None, label, "low")
    code = data.get("item_code")
    if code is not None and code not in codes:
        log.warning("photo reply chose %r, not a candidate %s", code, codes)
        return PhotoMatch(None, label, "low")
    return PhotoMatch(code, label, confidence)


def candidate_lines(items: list[dict[str, Any]]) -> str:
    return "\n".join(
        json.dumps({"item_code": i["item_code"], "item_name": i["item_name"],
                    "aliases": i.get("voice_aliases") or ""}, ensure_ascii=False)
        for i in items
    )  # fmt: skip


class PartVision(Protocol):
    async def __call__(
        self, image: bytes, mime: str, items: list[dict[str, Any]]
    ) -> PhotoMatch: ...


class SparkVision:
    """Muse Spark image understanding (Chat Completions, image_url data URL)."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        # Reading one label needs no deliberation. Measured on a real phone photo of a
        # handwritten "6205" (1280 px): "low" median 4.3 s, max 5.0 s, one phone attempt
        # over the 6 s budget; "minimal" median 1.7 s, max 3.0 s, still 8/8 correct, and
        # a photo with no label still comes back as no match.
        reasoning_effort: str = "minimal",
        hedge_after: float | None = VISION_HEDGE_AFTER_S,
    ) -> None:
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=30.0)
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.hedge_after = hedge_after

    def request(self, image: bytes, mime: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        data_url = f"data:{mime};base64,{base64.b64encode(image).decode()}"
        return {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": VISION_PROMPT.format(candidates=candidate_lines(items)),
                        },
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            "response_format": photo_schema([i["item_code"] for i in items]),
            "reasoning_effort": self.reasoning_effort,
        }

    async def __call__(self, image: bytes, mime: str, items: list[dict[str, Any]]) -> PhotoMatch:
        body = self.request(image, mime, items)

        async def once() -> str | None:
            t = time.monotonic()
            resp = await self.client.chat.completions.create(**body)
            log.info("spark vision %.2fs usage=%s", time.monotonic() - t,
                     resp.usage.model_dump(include={"prompt_tokens", "completion_tokens"})
                     if resp.usage else None)  # fmt: skip
            return resp.choices[0].message.content

        content = await hedged(once, self.hedge_after)
        return parse_photo_reply(content, [i["item_code"] for i in items])


async def identify(
    vision: PartVision,
    image: bytes,
    mime: str,
    items: list[dict[str, Any]],
    budget: float = PHOTO_BUDGET_S,
) -> PhotoMatch:
    """Run the vision call within its own budget; any failure is "couldn't read it"."""
    try:
        return await asyncio.wait_for(vision(image, mime, items), budget)
    except TimeoutError:
        log.warning("photo identification over %.1fs budget", budget)
    except Exception:  # the chips are always the fallback
        log.exception("photo identification failed")
    return NO_MATCH
