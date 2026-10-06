"""Сведение модулей: полный сервис поднимается, владелец меняется безопасно, обязательства видны агенту."""

import json

from shturman import bridge, jobs, store
from shturman.app import MODULES
from shturman.records import ChatRecord

from conftest import MCP_AUTH


async def rpc(client, method, params=None, rid=1):
    body = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
    headers = {**MCP_AUTH, "Accept": "application/json, text/event-stream", "Host": "test"}
    r = await client.post("/mcp", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["result"]


async def test_whole_service_starts_and_lists_every_agent_tool_as_read_only(make_client):
    client, state = await make_client(*MODULES)
    assert (await client.get("/api/status")).status_code == 200
    tools = (await rpc(client, "tools/list"))["tools"]
    names = {t["name"] for t in tools}
    assert names == {"search_messages", "get_context", "list_chats", "get_chat_history", "find_person",
                     "list_commitments", "get_commitment", "get_person_page", "search_pages"}
    assert all(t["annotations"]["readOnlyHint"] is True for t in tools)
    out = await rpc(client, "tools/call", {"name": "list_commitments", "arguments": {"view": "open"}})
    assert out["structuredContent"]["items"] == []


async def test_owner_change_stops_what_the_previous_owner_set_up(make_client, conn):
    client, _ = await make_client("shturman.api_core", "shturman.outbox.service")
    await client.put("/api/owner", json={"user_id": 1000, "chat_id": 1000})
    account = await store.ensure_account(conn, 1000, "Владелец")
    chat_id, _ = await store.ensure_chat(conn, account, ChatRecord("user", 2001, "personal_chat", "Иван"))
    await conn.execute("INSERT INTO business_connections (id, account_id, can_reply) VALUES ('bc1', $1, true)", account)
    await conn.execute("INSERT INTO outbox_trusted (tg_user_id) VALUES (2001)")
    await conn.execute(
        "INSERT INTO outbox_accounts (account_id, autoreply_enabled) VALUES ($1, true)", account)

    # тот же владелец повторно — ничего не меняется
    await client.put("/api/owner", json={"user_id": 1000, "chat_id": 1000})
    assert await conn.fetchval("SELECT enabled FROM business_connections") is True

    await client.put("/api/owner", json={"user_id": 7777, "chat_id": 7777})
    assert await conn.fetchval("SELECT enabled FROM business_connections") is False
    assert await conn.fetchval("SELECT count(*) FROM outbox_trusted") == 0
    assert await conn.fetchval("SELECT autoreply_enabled FROM outbox_accounts") is False


async def test_unbound_owner_means_nobody_can_press_buttons(make_client, conn):
    client, _ = await make_client("shturman.api_core", "shturman.outbox.service")
    await client.put("/api/owner", json={"user_id": 1000, "chat_id": 1000})
    assert (await client.delete("/api/owner")).json() == {"ok": True}
    assert await bridge.get_owner(conn) is None
    out = await client.post("/api/callbacks/telegram", json={"data": "sh:ob:s:1:x", "from_user_id": 1000})
    assert out.json()["answer"] == "Кнопка недоступна."
    assert (await client.get("/api/status")).json()["owner_known"] is False
