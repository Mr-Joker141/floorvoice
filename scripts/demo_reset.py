"""Reset the demo ERPNext site to its freshly-seeded state before filming.

Cancels every submitted Stock Entry (except the seeded opening stock) and Material Request
(ERPNext v15 keeps cancelled stock entries: their ledger rows still link to them), deletes
all Job Cards and Work Orders and resets their naming counters, re-runs the seed (5 work
orders -> job cards PO-JOB00001..5) and archives the FloorVoice history. Masters are kept.

Only for the dedicated demo site. Usage:
    uv run python -m scripts.demo_reset          # shows what will go, asks to confirm
    uv run python -m scripts.demo_reset --yes
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time

from erp.client import ErpClient, ErpError
from seed.seed import SEED_MARK
from server.config import get_settings

BENCH_CONTAINER = os.environ.get("ERP_BENCH_CONTAINER", "nch-uat-backend")
# Order matters: job cards before work orders.
DOCTYPES = ["Stock Entry", "Material Request", "Job Card", "Work Order"]
# Deleted (not just cancelled), so their numbering can restart: demo says "job card five".
DELETE = {"Job Card", "Work Order"}
SERIES = {"MFG-WO-2026-": 0, "PO-JOB": 0}


async def doomed(erp: ErpClient, doctype: str) -> list[dict]:
    fields = ["name", "docstatus"] + (["remarks"] if doctype == "Stock Entry" else [])
    rows = await erp.get_list(doctype, fields=fields, order_by="name desc", limit=1000)
    if doctype == "Stock Entry":
        rows = [r for r in rows if not (r.get("remarks") or "").startswith(SEED_MARK)]
    if doctype not in DELETE:
        rows = [r for r in rows if r["docstatus"] != 2]  # already cancelled
    return rows


async def remove(erp: ErpClient, doctype: str, row: dict) -> None:
    if row["docstatus"] == 1:
        await erp.call("frappe.client.cancel", doctype=doctype, name=row["name"])
    if doctype in DELETE or row["docstatus"] == 0:
        await erp._request("DELETE", f"/api/resource/{doctype}/{row['name']}")


def reset_series(site: str) -> None:
    sql = "".join(f"update tabSeries set current={v} where name='{k}';" for k, v in SERIES.items())
    subprocess.run(
        ["docker", "exec", "-i", BENCH_CONTAINER, "bench", "--site", site, "mariadb"],
        input=sql.encode(),
        check=True,
        capture_output=True,
    )


async def purge(site: str, yes: bool) -> int:
    s = get_settings()
    async with ErpClient(s.erp_url, timeout=120) as erp:
        await erp.login("Administrator", os.environ["ERP_ADMIN_PASSWORD"])
        plan = {dt: await doomed(erp, dt) for dt in DOCTYPES}
        for dt, rows in plan.items():
            print(f"{dt:17s} {len(rows):3d}  {', '.join(r['name'] for r in rows[:6])}"
                  + (" …" if len(rows) > 6 else ""))  # fmt: skip
        if not yes and input(f"Cancel/delete these on {site}? [y/N] ").strip().lower() != "y":
            print("aborted")
            return 1
        for dt, rows in plan.items():
            for row in rows:
                try:
                    await remove(erp, dt, row)
                except ErpError as e:
                    print(f"  {dt} {row['name']}: {e}", file=sys.stderr)
                    return 1
            print(f"{'deleted' if dt in DELETE else 'cancelled'} {len(rows)} {dt}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    s = get_settings()
    site = os.environ.get("ERP_SITE", "floorvoice.local")
    if site != "floorvoice.local" and not os.environ.get("FLOORVOICE_RESET_ANY_SITE"):
        print(f"refusing to reset site {site!r}", file=sys.stderr)
        return 2
    if rc := asyncio.run(purge(site, args.yes)):
        return rc
    reset_series(site)
    print("naming counters reset")
    subprocess.run([sys.executable, "-m", "seed.seed", "--keep-keys"], check=True)
    hist = s.data_dir / "history.jsonl"
    if hist.exists():
        hist.rename(hist.with_name(f"history-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"))
        print("history archived")
    print("done — restart the server so it reloads job cards (or wait 60 s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
