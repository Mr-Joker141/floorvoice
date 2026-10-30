"""Read ERPNext masters (items, warehouses, job cards) for search and ASR keyword biasing."""

from __future__ import annotations

from typing import Any

from erp.client import ErpClient


def split_aliases(raw: str | None) -> list[str]:
    return [a.strip() for a in (raw or "").replace("\n", ",").split(",") if a.strip()]


async def load_items(erp: ErpClient) -> list[dict[str, Any]]:
    return await erp.get_list(
        "Item",
        fields=["item_code", "item_name", "stock_uom", "voice_aliases"],
        filters=[["disabled", "=", 0], ["is_stock_item", "=", 1]],
        limit=1000,
    )


async def load_warehouses(erp: ErpClient) -> list[dict[str, Any]]:
    return await erp.get_list(
        "Warehouse",
        fields=["name", "warehouse_name", "voice_aliases"],
        filters=[["is_group", "=", 0], ["disabled", "=", 0]],
        limit=200,
    )


async def load_job_cards(erp: ErpClient) -> list[dict[str, Any]]:
    """Open and completed (not cancelled) job cards; `status` tells them apart."""
    return await erp.get_list(
        "Job Card",
        fields=["name", "status", "docstatus", "production_item", "item_name", "for_quantity",
                "total_completed_qty", "workstation", "operation", "work_order"],
        filters=[["docstatus", "in", [0, 1]], ["status", "!=", "Cancelled"]],
        order_by="name asc",
        limit=500,
    )  # fmt: skip


def is_open(job_card: dict[str, Any]) -> bool:
    return job_card.get("docstatus", 0) == 0 and job_card.get("status") != "Completed"


def keywords_from_masters(
    items: list[dict[str, Any]],
    warehouses: list[dict[str, Any]],
    job_cards: list[dict[str, Any]],
) -> list[str]:
    """Build the ASR biasing list: codes, names and spoken aliases, de-duplicated."""
    words: list[str] = []
    for it in items:
        words += [it["item_code"], it["item_name"], *split_aliases(it.get("voice_aliases"))]
    for w in warehouses:
        words += [w["warehouse_name"], *split_aliases(w.get("voice_aliases"))]
    for jc in filter(is_open, job_cards):
        words += [jc["name"], jc.get("workstation") or "", jc.get("operation") or ""]
    words += ["job card", "material request", "stock entry", "transfer", "issue"]
    seen: set[str] = set()
    out: list[str] = []
    for w in words:
        key = w.strip().casefold()
        if key and key not in seen:
            seen.add(key)
            out.append(w.strip())
    return out


async def load_keywords(erp: ErpClient) -> list[str]:
    return keywords_from_masters(
        await load_items(erp), await load_warehouses(erp), await load_job_cards(erp)
    )


async def load_stock(erp: ErpClient) -> dict[str, dict[str, float]]:
    """item_code -> {warehouse: actual_qty} for positive balances (from Bin)."""
    bins = await erp.get_list(
        "Bin",
        fields=["item_code", "warehouse", "actual_qty"],
        filters=[["actual_qty", ">", 0]],
        limit=5000,
    )
    stock: dict[str, dict[str, float]] = {}
    for b in bins:
        stock.setdefault(b["item_code"], {})[b["warehouse"]] = float(b["actual_qty"])
    return stock
