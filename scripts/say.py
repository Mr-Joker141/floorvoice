"""Text-mode FloorVoice: run utterances through the agent against the real ERPNext.

Usage:
  uv run python -m scripts.say "move 2 bearing 6205 from stores to line 2"
  uv run python -m scripts.say "send me bearings" --answer "6205, ten"   # scripted clarify
  uv run python -m scripts.say                                            # interactive
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time

from agent.loop import AgentResult
from agent.runtime import build_runtime
from server.config import get_settings


def show(r: AgentResult, t0: float) -> None:
    print(f"  [{r.kind} {time.monotonic() - t0:.2f}s] {r.text}")
    if r.speech != r.text:
        print(f"  speech: {r.speech}")
    if r.options:
        print(f"  options: {r.options}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("utterance", nargs="?")
    ap.add_argument("--answer", action="append", default=[], help="clarification answers")
    ap.add_argument("--home", help="worker's home warehouse")
    args = ap.parse_args()
    rt = build_runtime(get_settings())
    try:
        await rt.cache.get()
        utterances = [args.utterance] if args.utterance else None
        while True:
            text = utterances.pop(0) if utterances else input("say> ").strip()
            if not text:
                break
            t0 = time.monotonic()
            state, r = await rt.agent.act(text, home_warehouse=args.home)
            show(r, t0)
            answers = list(args.answer)
            while r.kind == "ask":
                ans = answers.pop(0) if answers else input("answer> ").strip()
                t0 = time.monotonic()
                r = await rt.agent.clarify(state, ans)
                show(r, t0)
            if args.utterance:
                break
    finally:
        await rt.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    for noisy in ("httpx", "httpx2", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    asyncio.run(main())
