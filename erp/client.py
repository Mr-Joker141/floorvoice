"""Thin async ERPNext REST client (token auth, /api/resource, /api/method)."""

from __future__ import annotations

import html
import json
import logging
import re
from typing import Any, Self

import httpx

log = logging.getLogger(__name__)


class ErpError(RuntimeError):
    """An ERPNext error with a plain-language message for the worker."""

    def __init__(self, message: str, status: int | None = None, raw: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.raw = raw


_TAG = re.compile(r"<[^>]+>")


def _plain(text: str) -> str:
    return " ".join(html.unescape(_TAG.sub(" ", text)).split())


def error_message(resp: httpx.Response) -> str:
    """Extract the human message ERPNext attaches to a failed response."""
    try:
        body = resp.json()
    except ValueError:
        return f"ERPNext returned HTTP {resp.status_code}"
    msgs: list[str] = []
    try:
        server_msgs = json.loads(body.get("_server_messages") or "[]")
    except ValueError:
        server_msgs = []
    for raw in server_msgs:
        try:
            msgs.append(_plain(json.loads(raw).get("message", "")))
        except (ValueError, AttributeError):
            msgs.append(_plain(str(raw)))
    msgs = [m for m in msgs if m]
    if msgs:
        return "; ".join(msgs)
    exc = body.get("exception") or body.get("exc_type") or body.get("message")
    if exc:
        # "frappe.exceptions.ValidationError: Qty must be ..." -> "Qty must be ..."
        return _plain(str(exc).split(": ", 1)[-1])
    return f"ERPNext returned HTTP {resp.status_code}"


class ErpClient:
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        api_secret: str = "",
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {"Accept": "application/json"}
        if api_key and api_secret:
            headers["Authorization"] = f"token {api_key}:{api_secret}"
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout, transport=transport
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _request(self, method: str, path: str, **kw: Any) -> Any:
        resp = await self._http.request(method, path, **kw)
        if resp.status_code >= 400:
            msg = error_message(resp)
            log.log(
                logging.DEBUG if resp.status_code == 404 else logging.WARNING,
                "ERPNext %s %s -> %s: %s",
                method,
                path,
                resp.status_code,
                msg,
            )
            raise ErpError(msg, resp.status_code, resp.text)
        body = resp.json()
        return body["data"] if "data" in body else body.get("message")

    async def login(self, user: str, password: str) -> None:
        """Session (cookie) login; only used by seeding before API keys exist."""
        await self._request("POST", "/api/method/login", data={"usr": user, "pwd": password})

    async def get_list(
        self,
        doctype: str,
        *,
        fields: list[str] | None = None,
        filters: list[list[Any]] | dict[str, Any] | None = None,
        or_filters: list[list[Any]] | None = None,
        order_by: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "fields": json.dumps(fields or ["name"]),
            "limit_page_length": limit,
        }
        if filters:
            params["filters"] = json.dumps(filters)
        if or_filters:
            params["or_filters"] = json.dumps(or_filters)
        if order_by:
            params["order_by"] = order_by
        return list(await self._request("GET", f"/api/resource/{doctype}", params=params))

    async def get_doc(self, doctype: str, name: str) -> dict[str, Any]:
        return dict(await self._request("GET", f"/api/resource/{doctype}/{name}"))

    async def exists(self, doctype: str, name: str) -> bool:
        try:
            await self._request("GET", f"/api/resource/{doctype}/{name}")
        except ErpError as e:
            if e.status == 404:
                return False
            raise
        return True

    async def insert(
        self, doctype: str, doc: dict[str, Any], *, submit: bool = False
    ) -> dict[str, Any]:
        """Insert a document; with submit=True, save as draft then submit it.

        (Inserting with docstatus=1 in one call skips status hooks, e.g. a Material
        Request stays "Draft" although submitted.)
        """
        created = dict(await self._request("POST", f"/api/resource/{doctype}", json=doc))
        if submit:
            created = await self.submit(doctype, created["name"])
        return created

    async def update(self, doctype: str, name: str, changes: dict[str, Any]) -> dict[str, Any]:
        return dict(await self._request("PUT", f"/api/resource/{doctype}/{name}", json=changes))

    async def submit(self, doctype: str, name: str) -> dict[str, Any]:
        return await self.update(doctype, name, {"docstatus": 1})

    async def call(self, method: str, **kwargs: Any) -> Any:
        data = {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in kwargs.items()}
        return await self._request("POST", f"/api/method/{method}", data=data)

    async def upload_file(
        self, doctype: str, name: str, filename: str, content: bytes, *, private: bool = True
    ) -> dict[str, Any]:
        files = {"file": (filename, content)}
        data = {
            "doctype": doctype,
            "docname": name,
            "is_private": "1" if private else "0",
            "file_name": filename,
        }
        return dict(await self._request("POST", "/api/method/upload_file", data=data, files=files))

    async def add_comment(self, doctype: str, name: str, text: str) -> dict[str, Any]:
        return await self.insert(
            "Comment",
            {
                "comment_type": "Comment",
                "reference_doctype": doctype,
                "reference_name": name,
                "content": text,
            },
        )
