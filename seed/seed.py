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
import secrets
import sys
from typing import Any

from erp.client import ErpClient, ErpError
from erp.masters import hash_pin
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


# Employee fields for FloorVoice workers, and the worker link on the documents it posts.
WORKER_FIELDS: list[dict[str, Any]] = [
    {"dt": "Employee", "fieldname": "floorvoice_pin", "label": "FloorVoice PIN (hash)",
     "fieldtype": "Data", "insert_after": "employee_name", "read_only": 1, "hidden": 1},
    {"dt": "Employee", "fieldname": "floorvoice_home_warehouse", "label": "FloorVoice Home Line",
     "fieldtype": "Link", "options": "Warehouse", "insert_after": "employee_name"},
    {"dt": "Employee", "fieldname": "floorvoice_language", "label": "FloorVoice Language",
     "fieldtype": "Select", "options": "ta\nsi\nen", "insert_after": "floorvoice_home_warehouse"},
    {"dt": "Employee", "fieldname": "voice_aliases", "label": "Voice Aliases",
     "fieldtype": "Small Text", "insert_after": "floorvoice_language"},
    {"dt": "Stock Entry", "fieldname": "floorvoice_worker", "label": "FloorVoice Worker",
     "fieldtype": "Link", "options": "Employee", "insert_after": "stock_entry_type",
     "read_only": 1},
    {"dt": "Material Request", "fieldname": "floorvoice_worker", "label": "FloorVoice Worker",
     "fieldtype": "Link", "options": "Employee", "insert_after": "material_request_type",
     "read_only": 1},
]  # fmt: skip

# Video stock count: what SAM looks for on Item, and worker + remarks on the reconciliation.
COUNT_FIELDS: list[dict[str, Any]] = [
    {"dt": "Item", "fieldname": "floorvoice_count_prompt", "label": "FloorVoice Count Prompt",
     "fieldtype": "Data", "insert_after": "voice_aliases",
     "description": "Short noun phrase SAM 3.1 looks for when this item is counted by video, "
                    "e.g. 'bearing box'. Empty = count by entering the number."},
    {"dt": "Stock Reconciliation", "fieldname": "floorvoice_worker", "label": "FloorVoice Worker",
     "fieldtype": "Link", "options": "Employee", "insert_after": "purpose", "read_only": 1},
    {"dt": "Stock Reconciliation", "fieldname": "floorvoice_remarks",
     "label": "FloorVoice Remarks", "fieldtype": "Small Text", "insert_after": "floorvoice_worker",
     "read_only": 1},
]  # fmt: skip


async def count_fields(erp: ErpClient) -> None:
    for f in COUNT_FIELDS:
        await ensure(erp, "Custom Field", f"{f['dt']}-{f['fieldname']}", f)


def count_prompt_overrides(raw: str) -> dict[str, str]:
    """DEMO_COUNT_PROMPTS="BRG-6305=mug;CPL-L100=box": what the local demo shelf really holds
    (e.g. mugs standing in for bearing boxes), kept across demo_reset."""
    pairs = (p.split("=", 1) for p in raw.split(";") if "=" in p)
    return {
        code.strip(): phrase.strip() for code, phrase in pairs if code.strip() and phrase.strip()
    }


async def count_prompts(erp: ErpClient) -> None:
    prompts = d.COUNT_PROMPTS | count_prompt_overrides(os.environ.get("DEMO_COUNT_PROMPTS", ""))
    for code, phrase in prompts.items():
        await erp.update("Item", code, {"floorvoice_count_prompt": phrase})


async def count_demo_stock(erp: ErpClient) -> None:
    """Bring the COUNT_DEMO items' Stores quantity down to the demo figure, once."""
    mark = f"{SEED_MARK}: count demo"
    if await erp.get_list("Stock Entry", filters=[["remarks", "=", mark], ["docstatus", "=", 1]]):
        log.info("count demo stock already set")
        return
    rows = [
        {"item_code": code, "qty": d.OPENING_STOCK["Stores"][code] - target,
         "s_warehouse": wh("Stores")}
        for code, target in d.COUNT_DEMO.items()
        if d.OPENING_STOCK["Stores"][code] > target
    ]  # fmt: skip
    se = await erp.insert(
        "Stock Entry",
        {"stock_entry_type": "Material Issue", "purpose": "Material Issue",
         "company": d.COMPANY, "remarks": mark, "items": rows},
        submit=True,
    )  # fmt: skip
    log.info("count demo stock %s: %s", se["name"], d.COUNT_DEMO)


async def worker_fields(erp: ErpClient) -> None:
    for f in WORKER_FIELDS:
        await ensure(erp, "Custom Field", f"{f['dt']}-{f['fieldname']}", f)


def pin_salt() -> str:
    """FLOORVOICE_PIN_SALT from .env, generated and saved on first run."""
    salt = os.environ.get("FLOORVOICE_PIN_SALT", "")
    if not salt:
        salt = secrets.token_hex(16)
        write_env({"FLOORVOICE_PIN_SALT": salt})
        os.environ["FLOORVOICE_PIN_SALT"] = salt
        log.info("generated FLOORVOICE_PIN_SALT in .env")
    return salt


class PinCollision(ValueError):
    pass


def check_pins(workers: list[dict[str, str]]) -> None:
    """Every PIN must be 4 digits and unique; a collision is a seed error."""
    seen: dict[str, str] = {}
    for w in workers:
        pin, who = w["pin"], f"{w['first_name']} {w['last_name']}"
        if not (len(pin) == 4 and pin.isdigit()):
            raise PinCollision(f"{who}: PIN must be 4 digits")
        if pin in seen:
            raise PinCollision(f"{who} and {seen[pin]} have the same PIN")
        seen[pin] = who


async def workers(erp: ErpClient, salt: str) -> None:
    check_pins(d.WORKERS)
    existing = await erp.get_list(
        "Employee", fields=["name", "employee_name", "floorvoice_pin"],
        filters=[["company", "=", d.COMPANY]], limit=1000,
    )  # fmt: skip
    by_name = {e["employee_name"]: e for e in existing}
    for w in d.WORKERS:
        full = f"{w['first_name']} {w['last_name']}"
        pin_hash = hash_pin(w["pin"], salt)
        clash = [e for e in existing
                 if e.get("floorvoice_pin") == pin_hash and e["employee_name"] != full]  # fmt: skip
        if clash:
            raise PinCollision(f"{full}'s PIN is already used by {clash[0]['employee_name']}")
        fields = {
            "floorvoice_pin": pin_hash,
            "floorvoice_home_warehouse": wh(w["home"]),
            "floorvoice_language": w["language"],
            "voice_aliases": w["aliases"],
        }
        if full in by_name:
            await erp.update("Employee", by_name[full]["name"], fields)
            continue
        doc = await erp.insert("Employee", {
            "first_name": w["first_name"], "last_name": w["last_name"], "gender": w["gender"],
            "date_of_birth": "1990-01-01", "date_of_joining": "2024-01-15", "status": "Active",
            "company": d.COMPANY, **fields,
        })  # fmt: skip
        log.info("created Employee %s %s", doc["name"], full)


async def bot_roles(erp: ErpClient) -> None:
    """Give an existing bot user any role added to BOT_ROLES since it was created."""
    if not await erp.exists("User", d.BOT_USER):
        return
    user = await erp.get_doc("User", d.BOT_USER)
    have = {r["role"] for r in user.get("roles") or []}
    if missing := [r for r in d.BOT_ROLES if r not in have]:
        roles = [{"role": r} for r in sorted(have | set(missing))]
        await erp.update("User", d.BOT_USER, {"roles": roles})
        log.info("bot user: added roles %s", ", ".join(missing))


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
        await worker_fields(erp)
        await workers(erp, pin_salt())
        await count_fields(erp)
        await count_prompts(erp)
        await count_demo_stock(erp)
        await bot_roles(erp)
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
