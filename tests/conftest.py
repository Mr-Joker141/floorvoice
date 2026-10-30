from __future__ import annotations

import pytest

from agent.resolve import Masters
from seed import demo_data as d


def wh(name: str) -> str:
    return f"{name} - {d.ABBR}"


@pytest.fixture
def masters() -> Masters:
    items = [
        {"item_code": c, "item_name": n, "stock_uom": u, "voice_aliases": ", ".join(a)}
        for c, n, _g, u, _r, a in d.ITEMS
    ]
    warehouses = [
        {
            "name": wh(str(w["warehouse_name"])),
            "warehouse_name": w["warehouse_name"],
            "voice_aliases": ", ".join(w["aliases"]),  # type: ignore[arg-type]
        }
        for w in d.WAREHOUSES
    ]
    job_cards = [
        {
            "name": f"PO-JOB{i + 1:05d}",
            "status": "Open",
            "docstatus": 0,
            "production_item": item,
            "item_name": next(n for c, n, *_ in d.ITEMS if c == item),
            "for_quantity": qty,
            "total_completed_qty": 0,
            "workstation": d.BOMS[item][1],
            "operation": d.BOMS[item][0],
        }
        for i, (item, qty, _wip) in enumerate(d.WORK_ORDERS)
    ]
    stock = {}
    for w, rows in d.OPENING_STOCK.items():
        for code, qty in rows.items():
            stock.setdefault(code, {})[wh(w)] = float(qty)
    return Masters(items, warehouses, job_cards, stock)
