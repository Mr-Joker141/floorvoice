"""Seed the FloorVoice demo company into an ERPNext v15 site over REST.

Idempotent: every step checks for existing records first. Run with:
    uv run python -m seed.seed
Needs ERP_URL and ERP_ADMIN_PASSWORD in .env. Writes ERP_API_KEY/ERP_API_SECRET
(for the FloorVoice bot user) back into .env.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from typing import Any

from erp.client import ErpClient, ErpError
from seed import demo_data as d
from server.config import ROOT

log = logging.getLogger("seed")
SEED_MARK = "FloorVoice seed"


def wh(name: str) -> str:
    return f"{name} - {d.ABBR}"


async def setup_wizard(erp: ErpClient) -> None:
    settings = await erp.get_doc("System Settings", "System Settings")
    if settings.get("setup_complete"):
        log.info("setup wizard already complete")
        return
    log.info("running setup wizard")
    await erp.call(
        "frappe.desk.page.setup_wizard.setup_wizard.setup_complete",
        args={
            "language": "English",
            "country": d.COUNTRY,
            "timezone": d.TIMEZONE,
            "currency": d.CURRENCY,
            "full_name": "FloorVoice Admin",
            "email": "admin@example.com",
            "company_name": d.COMPANY,
            "company_abbr": d.ABBR,
            "chart_of_accounts": "Standard",
            "fy_start_date": d.FY_START,
            "fy_end_date": d.FY_END,
            "setup_demo": 0,
        },
    )


async def ensure(erp: ErpClient, doctype: str, name: str, doc: dict[str, Any]) -> bool:
    """Insert `doc` unless `name` exists. Returns True if created."""
    if await erp.exists(doctype, name):
        return False
    await erp.insert(doctype, doc)
    log.info("created %s %s", doctype, name)
    return True


async def custom_fields(erp: ErpClient) -> None:
    for dt, after in (("Item", "item_name"), ("Warehouse", "warehouse_name")):
        await ensure(
            erp,
            "Custom Field",
            f"{dt}-voice_aliases",
            {
                "dt": dt,
                "fieldname": "voice_aliases",
                "label": "Voice Aliases",
                "fieldtype": "Small Text",
                "insert_after": after,
                "description": "Comma-separated spoken names (English/Tamil) for FloorVoice.",
            },
        )
    # Material Request has no Remarks field; FloorVoice keeps the spoken words here.
    await ensure(
        erp,
        "Custom Field",
        "Material Request-floorvoice_remarks",
        {
            "dt": "Material Request",
            "fieldname": "floorvoice_remarks",
            "label": "FloorVoice Remarks",
            "fieldtype": "Small Text",
            "insert_after": "material_request_type",
            "read_only": 1,
        },
    )


async def warehouses(erp: ErpClient) -> None:
    for w in d.WAREHOUSES:
        name = wh(str(w["warehouse_name"]))
        aliases = ", ".join(w["aliases"])  # type: ignore[arg-type]
        created = await ensure(
            erp,
            "Warehouse",
            name,
            {
                "warehouse_name": w["warehouse_name"],
                "company": d.COMPANY,
                "parent_warehouse": wh("All Warehouses"),
                "voice_aliases": aliases,
            },
        )
        if not created:
            await erp.update("Warehouse", name, {"voice_aliases": aliases})


async def items(erp: ErpClient) -> None:
    for code, name, group, uom, rate, aliases in d.ITEMS:
        doc = {
            "item_code": code,
            "item_name": name,
            "item_group": group,
            "stock_uom": uom,
            "is_stock_item": 1,
            "include_item_in_manufacturing": 1,
            "valuation_rate": rate,
            "description": name,
            "voice_aliases": ", ".join(aliases),
            "item_defaults": [{"company": d.COMPANY, "default_warehouse": wh("Stores")}],
        }
        if not await ensure(erp, "Item", code, doc):
            await erp.update("Item", code, {"voice_aliases": doc["voice_aliases"]})


async def opening_stock(erp: ErpClient) -> None:
    done = await erp.get_list(
        "Stock Entry", filters=[["remarks", "like", f"{SEED_MARK}%"], ["docstatus", "=", 1]]
    )
    if done:
        log.info("opening stock already posted")
        return
    rates = {code: rate for code, _, _, _, rate, _ in d.ITEMS}
    rows = [
        {"item_code": code, "qty": qty, "t_warehouse": wh(w), "basic_rate": rates[code]}
        for w, stock in d.OPENING_STOCK.items()
        for code, qty in stock.items()
    ]
    se = await erp.insert(
        "Stock Entry",
        {
            "stock_entry_type": "Material Receipt",
            "purpose": "Material Receipt",
            "company": d.COMPANY,
            "remarks": f"{SEED_MARK}: opening stock",
            "items": rows,
        },
        submit=True,
    )
    log.info("opening stock %s (%d rows)", se["name"], len(rows))


async def suppliers(erp: ErpClient) -> None:
    groups = await erp.get_list("Supplier Group", filters=[["is_group", "=", 0]], limit=50)
    names = [g["name"] for g in groups]
    group = "Raw Material" if "Raw Material" in names else (names[0] if names else "")
    for s in d.SUPPLIERS:
        await ensure(
            erp,
            "Supplier",
            str(s["supplier_name"]),
            {"supplier_name": s["supplier_name"], "supplier_group": group},
        )


async def manufacturing(erp: ErpClient) -> None:
    for op in d.OPERATIONS:
        await ensure(erp, "Operation", op, {"name": op, "__newname": op})
    for w in d.WORKSTATIONS:
        # High capacity so concurrent job-card time logs on one workstation don't collide.
        doc = {"workstation_name": w["workstation_name"], "hour_rate": w["hour_rate"],
               "production_capacity": 50}  # fmt: skip
        if not await ensure(erp, "Workstation", str(w["workstation_name"]), doc):
            await erp.update("Workstation", str(w["workstation_name"]), {"production_capacity": 50})
    for item, (op, ws, mins, comps) in d.BOMS.items():
        existing = await erp.get_list(
            "BOM", filters=[["item", "=", item], ["docstatus", "=", 1], ["is_default", "=", 1]]
        )
        if existing:
            continue
        bom = await erp.insert(
            "BOM",
            {
                "item": item,
                "company": d.COMPANY,
                "quantity": 1,
                "is_active": 1,
                "is_default": 1,
                "with_operations": 1,
                "rm_cost_as_per": "Valuation Rate",
                "items": [{"item_code": c, "qty": q} for c, q in comps],
                "operations": [{"operation": op, "workstation": ws, "time_in_mins": mins}],
            },
            submit=True,
        )
        log.info("created BOM %s", bom["name"])


async def work_orders(erp: ErpClient) -> None:
    existing = await erp.get_list(
        "Work Order", filters=[["docstatus", "=", 1], ["status", "!=", "Cancelled"]], limit=100
    )
    if len(existing) >= len(d.WORK_ORDERS):
        log.info("work orders already present (%d)", len(existing))
        return
    for item, qty, wip in d.WORK_ORDERS[len(existing) :]:
        boms = await erp.get_list(
            "BOM", filters=[["item", "=", item], ["is_default", "=", 1], ["docstatus", "=", 1]]
        )
        op, ws, mins, _ = d.BOMS[item]
        # The desk UI copies BOM operations client-side; over REST we pass them explicitly,
        # otherwise no Job Cards are created on submit.
        wo = await erp.insert(
            "Work Order",
            {
                "production_item": item,
                "bom_no": boms[0]["name"],
                "qty": qty,
                "company": d.COMPANY,
                "wip_warehouse": wh(wip),
                "fg_warehouse": wh("Stores"),
                "source_warehouse": wh("Stores"),
                "planned_start_date": "2026-10-30 08:00:00",
                "operations": [
                    {
                        "operation": op,
                        "workstation": ws,
                        "bom": boms[0]["name"],
                        "time_in_mins": mins * qty,
                    }
                ],
            },
            submit=True,
        )
        log.info("created Work Order %s", wo["name"])
    cards = await erp.get_list("Job Card", filters=[["docstatus", "=", 0]], limit=100)
    log.info("open job cards: %s", ", ".join(c["name"] for c in cards))


async def bot_user(erp: ErpClient) -> tuple[str, str]:
    await ensure(
        erp,
        "User",
        d.BOT_USER,
        {
            "email": d.BOT_USER,
            "first_name": "FloorVoice",
            "user_type": "System User",
            "send_welcome_email": 0,
            "roles": [{"role": r} for r in d.BOT_ROLES],
        },
    )
    secret = await erp.call("frappe.core.doctype.user.user.generate_keys", user=d.BOT_USER)
    user = await erp.get_doc("User", d.BOT_USER)
    return str(user["api_key"]), str(secret["api_secret"])


def write_env(updates: dict[str, str]) -> None:
    path = ROOT / ".env"
    text = path.read_text() if path.exists() else ""
    for key, value in updates.items():
        line = f"{key}={value}"
        if re.search(rf"^{key}=", text, flags=re.MULTILINE):
            text = re.sub(rf"^{key}=.*$", line, text, flags=re.MULTILINE)
        else:
            text += ("" if text.endswith("\n") or not text else "\n") + line + "\n"
    path.write_text(text)
    path.chmod(0o600)


async def main() -> int:
    url = os.environ.get("ERP_URL", "http://127.0.0.1:8090")
    password = os.environ.get("ERP_ADMIN_PASSWORD", "")
    if not password:
        print("ERP_ADMIN_PASSWORD is not set", file=sys.stderr)
        return 2
    async with ErpClient(url, timeout=600) as erp:
        await erp.login("Administrator", password)
        await setup_wizard(erp)
        await custom_fields(erp)
        await warehouses(erp)
        await items(erp)
        await opening_stock(erp)
        await suppliers(erp)
        await manufacturing(erp)
        await work_orders(erp)
        if "--keep-keys" not in sys.argv or not os.environ.get("ERP_API_KEY"):
            key, secret = await bot_user(erp)
            write_env({"ERP_API_KEY": key, "ERP_API_SECRET": secret})
            log.info("API keys for %s written to .env", d.BOT_USER)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        sys.exit(asyncio.run(main()))
    except ErpError as e:
        log.error("ERPNext error: %s", e)
        log.debug("raw: %s", e.raw)
        sys.exit(1)
