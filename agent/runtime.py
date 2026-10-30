"""Wire the agent to Muse Spark and ERPNext from settings."""

from __future__ import annotations

from dataclasses import dataclass

from agent.loop import Agent, SparkCompleter
from agent.tools import MastersCache, ToolExecutor
from erp.client import ErpClient
from server.config import AGENT_MODEL, META_API_BASE, Settings


@dataclass
class Runtime:
    erp: ErpClient
    cache: MastersCache
    agent: Agent

    async def aclose(self) -> None:
        await self.erp.aclose()


def build_runtime(s: Settings) -> Runtime:
    erp = ErpClient(s.erp_url, s.erp_api_key, s.erp_api_secret)
    cache = MastersCache(erp)
    executor = ToolExecutor(
        erp,
        cache,
        company=s.erp_company,
        default_warehouse=s.erp_default_warehouse,
        timezone=s.erp_timezone,
    )
    completer = SparkCompleter(s.meta_api_key, META_API_BASE, AGENT_MODEL, s.agent_reasoning)
    return Runtime(erp, cache, Agent(completer, executor, cache))
