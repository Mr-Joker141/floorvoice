"""Export the ASR keyword-bias list from ERPNext masters to seed/keywords.json.

Usage: uv run python -m seed.export_keywords
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from erp.client import ErpClient
from erp.masters import load_keywords
from server.config import get_settings

OUT = Path(__file__).with_name("keywords.json")


async def main() -> None:
    s = get_settings()
    async with ErpClient(s.erp_url, s.erp_api_key, s.erp_api_secret) as erp:
        words = await load_keywords(erp)
    OUT.write_text(json.dumps(words, ensure_ascii=False, indent=1) + "\n")
    print(f"wrote {len(words)} keywords to {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
