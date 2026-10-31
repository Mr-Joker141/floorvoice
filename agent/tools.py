"""Agent tools: JSON schemas for Muse Spark and their executor against ERPNext."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from agent import resolve
from agent.resolve import Masters
from erp import masters as erp_masters
from erp.client import ErpClient, ErpError

log = logging.getLogger(__name__)

STOCK_PURPOSES = ["Material Transfer", "Material Issue"]
MR_PURPOSES = ["Purchase", "Material Transfer"]
TERMINAL_TOOLS = {"ask_user", "finish"}


def _fn(name: str, description: str, props: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": list(props),
                "additionalProperties": False,
            },
        },
    }


_STR = {"type": "string"}
_NUM = {"type": "number"}
_OPT_STR = {"type": ["string", "null"]}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    _fn(
        "search_items",
        "Fuzzy-search stock items by code, name or spoken alias. Returns up to 5 matches with "
        "uom and stock per warehouse. Several equal matches mean the reference is ambiguous.",
        {"query": {**_STR, "description": "What the worker called the item, e.g. 'bearing 6205'"}},
    ),
    _fn(
        "search_warehouses",
        "Fuzzy-search warehouses (e.g. 'stores', 'line 2'). Returns full warehouse names.",
        {"query": _STR},
    ),
    _fn(
        "search_job_cards",
        "Search job cards by number ('4'), name, item or workstation. Returns name, item, "
        "status (Completed cards cannot take more quantity), pending_qty and workstation.",
        {"query": _STR},
    ),
    _fn(
        "create_stock_entry",
        "Post and submit a Stock Entry. Material Transfer needs from_warehouse and "
        "to_warehouse; Material Issue needs from_warehouse only (to_warehouse null).",
        {
            "purpose": {"type": "string", "enum": STOCK_PURPOSES},
            "item_code": _STR,
            "qty": {**_NUM, "description": "Quantity in the item's stock UOM, > 0"},
            "from_warehouse": {**_OPT_STR, "description": "Full warehouse name from search"},
            "to_warehouse": {**_OPT_STR, "description": "Full warehouse name from search"},
        },
    ),
    _fn(
        "complete_job_card",
        "Record produced quantity on an open Job Card. The card is submitted (completed) "
        "when the total produced reaches its planned quantity.",
        {"name": _STR, "completed_qty": {**_NUM, "description": "Quantity produced now, > 0"}},
    ),
    _fn(
        "create_material_request",
        "Create and submit a Material Request. purpose 'Purchase' to buy, 'Material Transfer' "
        "to request stock moved to a warehouse. warehouse null = default (Stores).",
        {
            "item_code": _STR,
            "qty": _NUM,
            "purpose": {"type": "string", "enum": MR_PURPOSES},
            "warehouse": {**_OPT_STR, "description": "Target warehouse full name, or null"},
        },
    ),
    _fn(
        "ask_user",
        "Ask the worker ONE short clarifying question and stop. Use only when a mandatory "
        "field is missing or several masters match equally. Offer the candidates as options.",
        {
            "question": _STR,
            "options": {"type": ["array", "null"], "items": {"type": "string"}},
        },
    ),
    _fn(
        "finish",
        "End the turn with the spoken confirmation (under 20 words, include the ERP "
        "document name), or a plain explanation if nothing could be posted.",
        {"summary": _STR},
    ),
]


@dataclass
class PostedDoc:
    doctype: str
    name: str
    summary: str
    # How to reverse it (see ToolExecutor.undo); JSON-able.
    undo: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolContext:
    """Per-turn info the executor stamps onto ERP documents."""

    transcript: str = ""
    posted: list[PostedDoc] = field(default_factory=list)


class MastersCache:
    def __init__(self, erp: ErpClient, ttl: float = 60.0) -> None:
        self.erp = erp
        self.ttl = ttl
        self._masters: Masters | None = None
        self._loaded = 0.0
        self._lock = asyncio.Lock()

    async def get(self, *, refresh: bool = False) -> Masters:
        async with self._lock:
            stale = time.monotonic() - self._loaded > self.ttl
            if self._masters is None or stale or refresh:
                items, whs, jcs, stock = await asyncio.gather(
                    erp_masters.load_items(self.erp),
                    erp_masters.load_warehouses(self.erp),
                    erp_masters.load_job_cards(self.erp),
                    erp_masters.load_stock(self.erp),
                )
                self._masters = Masters(items, whs, jcs, stock)
                self._loaded = time.monotonic()
            return self._masters

    def invalidate(self) -> None:
        self._loaded = 0.0


class ToolError(ValueError):
    """Invalid tool arguments; reported back to the model, not raised to the user."""


def _qty(value: Any) -> float:
    try:
        q = float(value)
    except (TypeError, ValueError) as e:
        raise ToolError(f"qty must be a number, got {value!r}") from e
    if q <= 0:
        raise ToolError("qty must be greater than zero")
    return q


def _short(m: Masters, warehouse: str) -> str:
    """'Line 2 - FVD' -> 'Line 2' for read-back."""
    w = m.warehouse(warehouse)
    return str(w["warehouse_name"]) if w else warehouse


def _fmt(q: float) -> str:
    return str(int(q)) if q == int(q) else f"{q:g}"


class ToolExecutor:
    def __init__(
        self,
        erp: ErpClient,
        cache: MastersCache,
        *,
        company: str,
        default_warehouse: str,
        timezone: str = "Asia/Colombo",
    ) -> None:
        self.erp = erp
        self.cache = cache
        self.company = company
        self.default_warehouse = default_warehouse
        self.tz = ZoneInfo(timezone)

    async def run(self, name: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        """Execute one tool call; always returns a JSON-able result for the model."""
        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            return {"ok": False, "error": f"unknown tool {name}"}
        try:
            return dict(await handler(ctx, **args))
        except ToolError as e:
            return {"ok": False, "error": str(e)}
        except TypeError as e:
            return {"ok": False, "error": f"bad arguments: {e}"}
        except ErpError as e:
            log.error("ERPNext error in %s %s (transcript=%r): %s", name, args, ctx.transcript, e)
            return {"ok": False, "erp_error": str(e)}

    # --- read tools -------------------------------------------------------------------

    async def _t_search_items(self, ctx: ToolContext, query: str) -> dict[str, Any]:
        return {"items": resolve.search_items(await self.cache.get(), query)}

    async def _t_search_warehouses(self, ctx: ToolContext, query: str) -> dict[str, Any]:
        return {"warehouses": resolve.search_warehouses(await self.cache.get(), query)}

    async def _t_search_job_cards(self, ctx: ToolContext, query: str) -> dict[str, Any]:
        return {"job_cards": resolve.search_job_cards(await self.cache.get(), query)}

    # --- write tools ------------------------------------------------------------------

    def _remarks(self, ctx: ToolContext) -> str:
        return f"Posted by FloorVoice from voice: {ctx.transcript}"[:1000]

    def _wh(self, m: Masters, ref: str | None, label: str) -> str:
        if not ref:
            raise ToolError(f"{label} is required")
        w = m.warehouse(ref)
        if w is None:
            raise ToolError(f"unknown {label} {ref!r}; use search_warehouses first")
        return str(w["name"])

    def _item(self, m: Masters, code: str) -> dict[str, Any]:
        item = m.item(code)
        if item is None:
            raise ToolError(f"unknown item_code {code!r}; use search_items first")
        return item

    async def _t_create_stock_entry(
        self,
        ctx: ToolContext,
        purpose: str,
        item_code: str,
        qty: float,
        from_warehouse: str | None = None,
        to_warehouse: str | None = None,
    ) -> dict[str, Any]:
        if purpose not in STOCK_PURPOSES:
            raise ToolError(f"purpose must be one of {STOCK_PURPOSES}")
        m = await self.cache.get()
        item = self._item(m, item_code)
        q = _qty(qty)
        src = self._wh(m, from_warehouse, "from_warehouse")
        dst = self._wh(m, to_warehouse, "to_warehouse") if purpose == "Material Transfer" else None
        if dst == src:
            raise ToolError("from_warehouse and to_warehouse are the same")
        available = m.stock.get(item_code, {}).get(src, 0.0)
        if available < q:
            return {
                "ok": False,
                "error": f"only {_fmt(available)} {item['stock_uom']} of {item['item_name']} "
                f"in {src}",
            }
        row: dict[str, Any] = {"item_code": item_code, "qty": q, "s_warehouse": src}
        if dst:
            row["t_warehouse"] = dst
        doc = await self.erp.insert(
            "Stock Entry",
            {
                "stock_entry_type": purpose,
                "purpose": purpose,
                "company": self.company,
                "remarks": self._remarks(ctx),
                "items": [row],
            },
            submit=True,
        )
        self.cache.invalidate()
        where = f"{_short(m, src)} to {_short(m, dst)}" if dst else f"from {_short(m, src)}"
        summary = f"{_fmt(q)} {item['item_name']} {where}"
        ctx.posted.append(PostedDoc("Stock Entry", doc["name"], summary))
        return {"ok": True, "doctype": "Stock Entry", "name": doc["name"], "posted": summary}

    async def _t_complete_job_card(
        self, ctx: ToolContext, name: str, completed_qty: float
    ) -> dict[str, Any]:
        m = await self.cache.get()
        known = m.job_card(name)
        if known is None:
            raise ToolError(f"no job card {name!r}; use search_job_cards first")
        if not erp_masters.is_open(known):
            return {"ok": False, "error": f"{name} is already completed"}
        q = _qty(completed_qty)
        jc = await self.erp.get_doc("Job Card", name)
        if jc.get("docstatus") != 0:
            return {"ok": False, "error": f"{name} is already completed"}
        planned = float(jc.get("for_quantity") or 0)
        done = float(jc.get("total_completed_qty") or 0)
        pending = planned - done
        if q > pending:
            return {
                "ok": False,
                "error": f"{name} has only {_fmt(pending)} pending of {_fmt(planned)}",
            }
        now = datetime.now(self.tz).replace(tzinfo=None, microsecond=0)
        mins_per_unit = float(jc.get("time_required") or 0) / planned if planned else 0.0
        start = now - timedelta(minutes=max(1.0, mins_per_unit * q))
        logs = list(jc.get("time_logs") or [])
        new_log = {
            "from_time": start.isoformat(sep=" "),
            "to_time": now.isoformat(sep=" "),
            "completed_qty": q,
        }
        logs.append(new_log)
        changes: dict[str, Any] = {"time_logs": logs, "remarks": self._remarks(ctx)}
        complete = abs(q - pending) < 1e-9
        if complete:
            changes["docstatus"] = 1
        await self.erp.update("Job Card", name, changes)
        self.cache.invalidate()
        item = jc.get("item_name") or jc.get("production_item")
        total = done + q
        if complete:
            summary = f"{_fmt(total)} {item} completed"
        else:
            summary = f"{_fmt(q)} {item} logged, {_fmt(total)} of {_fmt(planned)} done"
        ctx.posted.append(
            PostedDoc("Job Card", name, summary, {"time_log": new_log, "submitted": complete})
        )
        return {
            "ok": True,
            "doctype": "Job Card",
            "name": name,
            "completed": complete,
            "total_completed_qty": total,
            "for_quantity": planned,
            "posted": summary,
        }

    async def _t_create_material_request(
        self,
        ctx: ToolContext,
        item_code: str,
        qty: float,
        purpose: str,
        warehouse: str | None = None,
    ) -> dict[str, Any]:
        if purpose not in MR_PURPOSES:
            raise ToolError(f"purpose must be one of {MR_PURPOSES}")
        m = await self.cache.get()
        item = self._item(m, item_code)
        q = _qty(qty)
        wh = self._wh(m, warehouse or self.default_warehouse, "warehouse")
        needed_by = (date.today() + timedelta(days=3)).isoformat()
        doc = await self.erp.insert(
            "Material Request",
            {
                "material_request_type": purpose,
                "company": self.company,
                "schedule_date": needed_by,
                "set_warehouse": wh,
                "items": [
                    {"item_code": item_code, "qty": q, "warehouse": wh, "schedule_date": needed_by}
                ],
            },
            submit=True,
        )
        kind = "purchase" if purpose == "Purchase" else "transfer"
        summary = f"{kind} request for {_fmt(q)} {item['item_name']} to {_short(m, wh)}"
        ctx.posted.append(PostedDoc("Material Request", doc["name"], summary))
        return {"ok": True, "doctype": "Material Request", "name": doc["name"], "posted": summary}

    # --- undo ---------------------------------------------------------------------------

    async def undo(self, doc: PostedDoc) -> str:
        """Reverse a document FloorVoice posted. Returns the ERPNext name now holding the
        state (differs only for an amended job card)."""
        try:
            if doc.doctype in ("Stock Entry", "Material Request"):
                await self.erp.call("frappe.client.cancel", doctype=doc.doctype, name=doc.name)
                return doc.name
            if doc.doctype == "Job Card":
                return await self._undo_job_card(doc)
            raise ToolError(f"cannot undo {doc.doctype}")
        finally:
            self.cache.invalidate()

    async def _undo_job_card(self, doc: PostedDoc) -> str:
        ours = doc.undo.get("time_log") or {}

        def keep(row: dict[str, Any]) -> bool:
            same = str(row.get("to_time", "")).startswith(str(ours.get("to_time", "")))
            return not (same and float(row.get("completed_qty") or 0) == ours.get("completed_qty"))

        jc = await self.erp.get_doc("Job Card", doc.name)
        if jc.get("docstatus") == 0:
            logs = [r for r in jc.get("time_logs") or [] if keep(r)]
            await self.erp.update("Job Card", doc.name, {"time_logs": logs})
            return doc.name
        # Submitted (completed): cancel, then amend into an open draft without our log,
        # as ERPNext's "Amend" button does.
        await self.erp.call("frappe.client.cancel", doctype="Job Card", name=doc.name)
        draft = {k: v for k, v in jc.items() if k not in _SYSTEM_FIELDS}
        draft["amended_from"] = doc.name
        draft["status"] = "Open"
        for table, rows in list(draft.items()):
            if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                draft[table] = [{k: v for k, v in r.items() if k not in _SYSTEM_FIELDS}
                                for r in rows if table != "time_logs" or keep(r)]  # fmt: skip
        amended = await self.erp.insert("Job Card", draft)
        return str(amended["name"])


_SYSTEM_FIELDS = {
    "name", "owner", "creation", "modified", "modified_by", "docstatus", "idx", "parent",
    "parentfield", "parenttype", "amended_from", "total_completed_qty", "status",
}  # fmt: skip
