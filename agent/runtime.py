"""Wire the agent to Muse Spark and ERPNext from settings."""

from __future__ import annotations

from dataclasses import dataclass

from agent.count import SamVideo
from agent.loop import Agent, SparkCompleter
from agent.tools import MastersCache, ToolExecutor, tool_schemas
from agent.vision import PartVision, SparkVision
from erp.client import ErpClient
from server.config import AGENT_MODEL, META_API_BASE, Settings


@dataclass
class Runtime:
    erp: ErpClient
    cache: MastersCache
    agent: Agent
    vision: PartVision
    sam: SamVideo | None = None  # video stock count (FEATURE_VIDEO_COUNT)

    async def aclose(self) -> None:
        await self.erp.aclose()
        if self.sam is not None:
            await self.sam.aclose()


def build_runtime(s: Settings) -> Runtime:
    erp = ErpClient(s.erp_url, s.erp_api_key, s.erp_api_secret)
    cache = MastersCache(erp)
    executor = ToolExecutor(
        erp,
        cache,
        company=s.erp_company,
        default_warehouse=s.erp_default_warehouse,
        timezone=s.erp_timezone,
        count_enabled=s.video_count,
    )
    completer = SparkCompleter(s.meta_api_key, META_API_BASE, AGENT_MODEL, s.agent_reasoning,
                               tools=tool_schemas(count=s.video_count))  # fmt: skip
    # The label reader keeps its own (minimal) reasoning effort; the agent's is too slow for
    # the 6 s photo budget.
    vision = SparkVision(s.meta_api_key, META_API_BASE, AGENT_MODEL)
    sam = SamVideo(s.meta_api_key, META_API_BASE, s.sam_model) if s.video_count else None
    agent = Agent(completer, executor, cache, count_enabled=s.video_count)
    return Runtime(erp, cache, agent, vision, sam)
