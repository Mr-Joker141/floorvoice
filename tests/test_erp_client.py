import json

import httpx
import pytest
import respx

from erp.client import ErpClient, ErpError

BASE = "http://erp.test"


@pytest.fixture
async def erp():
    client = ErpClient(BASE, "key", "secret")
    yield client
    await client.aclose()


@respx.mock
async def test_token_auth_and_list(erp: ErpClient) -> None:
    route = respx.get(f"{BASE}/api/resource/Item").respond(json={"data": [{"name": "A"}]})
    rows = await erp.get_list("Item", fields=["name"], filters=[["disabled", "=", 0]], limit=5)
    assert rows == [{"name": "A"}]
    req = route.calls.last.request
    assert req.headers["Authorization"] == "token key:secret"
    assert json.loads(req.url.params["filters"]) == [["disabled", "=", 0]]
    assert req.url.params["limit_page_length"] == "5"


@respx.mock
async def test_insert_submit_is_draft_then_submit(erp: ErpClient) -> None:
    post = respx.post(f"{BASE}/api/resource/Stock%20Entry").respond(
        json={"data": {"name": "MAT-STE-1", "docstatus": 0}}
    )
    put = respx.put(f"{BASE}/api/resource/Stock%20Entry/MAT-STE-1").respond(
        json={"data": {"name": "MAT-STE-1", "docstatus": 1}}
    )
    doc = await erp.insert("Stock Entry", {"purpose": "Material Transfer"}, submit=True)
    assert doc == {"name": "MAT-STE-1", "docstatus": 1}
    assert json.loads(post.calls.last.request.content) == {"purpose": "Material Transfer"}
    assert json.loads(put.calls.last.request.content) == {"docstatus": 1}


@respx.mock
async def test_server_messages_become_plain_error(erp: ErpClient) -> None:
    msg = json.dumps({"message": "<strong>Ball Bearing</strong>: insufficient stock &amp; more"})
    respx.post(f"{BASE}/api/resource/Stock%20Entry").respond(
        417,
        json={
            "exception": "erpnext.stock.NegativeStockError: ...",
            "_server_messages": json.dumps([msg]),
        },
    )
    with pytest.raises(ErpError) as exc:
        await erp.insert("Stock Entry", {}, submit=True)
    assert str(exc.value) == "Ball Bearing : insufficient stock & more"
    assert exc.value.status == 417


@respx.mock
async def test_exception_fallback(erp: ErpClient) -> None:
    respx.put(f"{BASE}/api/resource/Job%20Card/JC1").respond(
        500, json={"exception": "frappe.exceptions.ValidationError: Time logs are required"}
    )
    with pytest.raises(ErpError, match="^Time logs are required$"):
        await erp.submit("Job Card", "JC1")


@respx.mock
async def test_non_json_error(erp: ErpClient) -> None:
    respx.get(f"{BASE}/api/resource/Item/X").mock(return_value=httpx.Response(502, text="bad"))
    with pytest.raises(ErpError, match="HTTP 502"):
        await erp.get_doc("Item", "X")


@respx.mock
async def test_exists(erp: ErpClient) -> None:
    respx.get(f"{BASE}/api/resource/Item/A").respond(json={"data": {"name": "A"}})
    respx.get(f"{BASE}/api/resource/Item/B").respond(404, json={"exc_type": "DoesNotExistError"})
    assert await erp.exists("Item", "A") is True
    assert await erp.exists("Item", "B") is False


@respx.mock
async def test_upload_file_and_comment(erp: ErpClient) -> None:
    up = respx.post(f"{BASE}/api/method/upload_file").respond(
        json={"message": {"file_url": "/private/files/a.wav"}}
    )
    cm = respx.post(f"{BASE}/api/resource/Comment").respond(json={"data": {"name": "c1"}})
    f = await erp.upload_file("Stock Entry", "SE1", "a.wav", b"RIFF")
    assert f["file_url"] == "/private/files/a.wav"
    body = up.calls.last.request.content
    assert b'name="docname"' in body and b"SE1" in body and b"RIFF" in body
    await erp.add_comment("Stock Entry", "SE1", "heard: hello")
    sent = json.loads(cm.calls.last.request.content)
    assert sent["reference_name"] == "SE1" and sent["content"] == "heard: hello"
