"""Сведение модулей: полный сервис поднимается, владелец меняется безопасно, обязательства видны агенту."""

import json

import pytest

from shturman import bridge, confirm, jobs, store
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


async def test_rebinding_the_same_owner_after_reset_restores_his_business_connection(make_client, conn):
    client, _ = await make_client("shturman.api_core", "shturman.outbox.service")
    account = await store.ensure_account(conn, 1000, "Владелец")
    await conn.execute("INSERT INTO business_connections (id, account_id, can_reply) VALUES ('bc1', $1, true)", account)
    await client.put("/api/owner", json={"user_id": 1000, "chat_id": 1000})
    await client.delete("/api/owner")
    assert await conn.fetchval("SELECT enabled FROM business_connections") is False
    await client.put("/api/owner", json={"user_id": 1000, "chat_id": 1000})
    assert await conn.fetchval("SELECT enabled FROM business_connections") is True


async def test_unclaimed_notice_expires_and_finished_jobs_lose_their_text(conn):
    seen = []

    @bridge.on_failure("t.stale")
    async def stale(c, job, error):
        seen.append(job["id"])

    old = await bridge.notify_owner(conn, "Вчерашняя сводка", handler="t.stale")
    fresh = await bridge.notify_owner(conn, "Свежая сводка")
    await conn.execute("UPDATE jobs SET created_at = now() - interval '2 days' WHERE id = $1", old)
    assert await bridge.reap_lost(conn) == 1 and seen == [old]
    assert [j["id"] for j in await jobs.claim(conn, [bridge.NOTIFY_OWNER], worker="w")] == [fresh]
    await bridge.deliver_result(conn, fresh, {"message_id": 5})

    assert await jobs.scrub_finished(conn, after=0) == 2
    rows = await conn.fetch("SELECT payload, result FROM jobs")
    assert all(json.loads(r["payload"]) == {} and r["result"] is None for r in rows)
    # строка осталась: повтор того же уведомления по ключу по-прежнему отсекается
    await jobs.enqueue(conn, bridge.NOTIFY_OWNER, {"text": "x"}, dedup_key="k")
    await conn.execute("UPDATE jobs SET status = 'done', finished_at = now() - interval '1 day' WHERE dedup_key = 'k'")
    await jobs.scrub_finished(conn)
    assert await jobs.enqueue(conn, bridge.NOTIFY_OWNER, {"text": "x"}, dedup_key="k") is None


def test_owner_message_is_cut_by_telegram_units_not_characters():
    text = "🙂" * 3000                      # 3000 знаков, но 6000 единиц UTF-16
    cut = bridge.fit_message(text)
    assert bridge.utf16_len(cut) <= 4096 and cut.endswith("…")
    assert bridge.fit_message("привет") == "привет"


async def test_oversized_and_malformed_requests_are_client_errors(make_client):
    client, _ = await make_client("shturman.api_core")
    big = await client.post("/api/jobs/claim", content=b'{"worker": "' + b"x" * (1024 * 1024 + 10) + b'"}',
                            headers={"Content-Type": "application/json"})
    assert big.status_code == 413
    assert (await client.post("/api/jobs/claim", json={"limit": "много"})).status_code == 400


def test_config_repr_hides_secrets(config):
    assert "test-api-token" not in repr(config) and "test-mcp-token" not in repr(config)
    assert config.sending is False and config.send_daily_hard_cap == 50


# --- свой исполнитель и подтверждения владельца ---

@pytest.fixture
def own_bot():
    bridge.set_builtin({bridge.NOTIFY_OWNER, bridge.NOTIFY_EDIT})
    yield
    bridge.set_builtin(())


async def test_builtin_jobs_are_never_handed_to_the_plugin(make_client, conn, own_bot):
    client, _ = await make_client("shturman.api_core")
    await bridge.notify_owner(conn, "Карточка с кнопкой")
    assert (await client.post("/api/jobs/claim", json={})).json()["jobs"] == []
    mine = await jobs.claim(conn, [bridge.NOTIFY_OWNER], worker="builtin", executor="builtin")
    assert len(mine) == 1


async def test_with_own_bot_plugin_cannot_press_buttons_or_rebind_owner(make_client, conn, own_bot):
    client, _ = await make_client("shturman.api_core")
    await bridge.set_owner(conn, 1000, 1000)
    for call in (client.post("/api/callbacks/telegram", json={"data": "sh:cf:y:1:x", "from_user_id": 1000}),
                 client.put("/api/owner", json={"user_id": 6666, "chat_id": 6666}),
                 client.delete("/api/owner")):
        assert (await call).status_code == 403
    assert (await bridge.get_owner(conn))["user_id"] == 1000
    assert (await client.get("/api/status")).json()["own_bot"] is True


async def test_plugin_cannot_close_a_job_of_the_builtin_executor(make_client, conn, own_bot):
    """Держатель токена API не подкладывает результат заданию своего исполнителя сервиса."""
    client, _ = await make_client("shturman.api_core")
    await bridge.notify_owner(conn, "Карточка")
    job = (await jobs.claim(conn, [bridge.NOTIFY_OWNER], worker="builtin", executor="builtin"))[0]
    forged = await client.post(f"/api/jobs/{job['id']}/complete", json={"result": {"message_id": 1}})
    assert forged.status_code == 409
    failed = await client.post(f"/api/jobs/{job['id']}/fail", json={"error": "подлог", "retry_in": None})
    assert failed.json()["status"] == "unknown"
    assert (await jobs.get(conn, job["id"]))["status"] == "running"
    assert await bridge.deliver_result(conn, job["id"], {"message_id": 7}, executor="builtin") is True


async def test_with_own_bot_plugin_cannot_feed_business_updates(make_client, conn, own_bot):
    """Бизнес-поток со своим ботом идёт только через него: по HTTP подключение не записать."""
    client, _ = await make_client("shturman.api_core", "shturman.ingest_api")
    await bridge.set_owner(conn, 1000, 1000)
    link = {"id": "forged", "user": {"id": 1000, "first_name": "В"}, "is_enabled": True}
    for path, payload in (("/api/ingest/business/connection", {"connection": link}),
                          ("/api/ingest/business/message", {"message": {}}),
                          ("/api/ingest/business/deleted", {})):
        answer = await client.post(path, json=payload)
        assert answer.status_code == 403 and answer.json()["code"] == "own_bot"
    assert await conn.fetchval("SELECT count(*) FROM business_connections") == 0


async def test_sensitive_action_waits_for_owner_press_in_own_bot(conn, own_bot):
    done = []

    @confirm.applier("t.widen")
    async def widen(c, payload):
        done.append(payload)
        return "Готово"

    await bridge.set_owner(conn, 1000, 1000)
    out = await confirm.request(conn, "t.widen", "Включить автоответ", {"x": 1})
    assert out["status"] == "pending_confirmation" and done == []
    card = (await jobs.claim(conn, [bridge.NOTIFY_OWNER], worker="b", executor="builtin"))[0]
    yes, no = (b["data"] for b in card["payload"]["buttons"][0])
    await bridge.deliver_result(conn, card["id"], {"message_id": 77})

    wrong = yes.rsplit(":", 1)[0] + ":forged"
    assert (await bridge.dispatch_callback(conn, wrong, 1000))["answer"] == "Действие уже недоступно."
    assert (await bridge.dispatch_callback(conn, yes, 6666))["answer"] == "Кнопка недоступна." and done == []
    ok = await bridge.dispatch_callback(conn, yes, 1000)
    assert ok["answer"] == "Сделано." and done == [{"x": 1}] and "Готово" in ok["edit_text"]
    assert (await bridge.dispatch_callback(conn, yes, 1000))["answer"] == "Действие уже недоступно."  # один раз
    assert done == [{"x": 1}]


async def test_rejected_expired_and_failed_actions_change_nothing(conn, own_bot):
    done = []

    @confirm.applier("t.act")
    async def act(c, payload):
        if payload.get("boom"):
            raise RuntimeError("нельзя")
        done.append(payload)

    await bridge.set_owner(conn, 1000, 1000)

    async def card_buttons():
        card = (await jobs.claim(conn, [bridge.NOTIFY_OWNER], worker="b", executor="builtin"))[0]
        await bridge.deliver_result(conn, card["id"], {"message_id": 5})
        return [b["data"] for b in card["payload"]["buttons"][0]]

    await confirm.request(conn, "t.act", "Стереть чат", {})
    _, no = await card_buttons()
    assert (await bridge.dispatch_callback(conn, no, 1000))["answer"] == "Отклонено."

    await confirm.request(conn, "t.act", "Стереть чат ещё раз", {})
    yes, _ = await card_buttons()
    await conn.execute("UPDATE pending_actions SET expires_at = now() - interval '1 minute' WHERE status = 'pending'")
    assert (await bridge.dispatch_callback(conn, yes, 1000))["answer"] == "Срок вышел."

    await confirm.request(conn, "t.act", "Сломанное действие", {"boom": True})
    yes, _ = await card_buttons()
    assert (await bridge.dispatch_callback(conn, yes, 1000))["answer"] == "Не получилось."
    assert done == []
    statuses = [r["status"] for r in await conn.fetch("SELECT status FROM pending_actions ORDER BY id")]
    assert statuses == ["rejected", "expired", "failed"]


async def test_without_own_bot_action_applies_at_once(conn):
    done = []

    @confirm.applier("t.now")
    async def now(c, payload):
        done.append(1)

    assert (await confirm.request(conn, "t.now", "Действие", {}))["status"] == "applied" and done == [1]


def test_sending_needs_own_bot(monkeypatch):
    from shturman.config import Config

    base = {"SHTURMAN_DSN": "postgresql://x", "SHTURMAN_API_TOKEN": "a" * 40, "SHTURMAN_MCP_TOKEN": "b" * 40,
            "SHTURMAN_SENDING": "on"}
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("SHTURMAN_BOT_TOKEN", raising=False)
    assert Config.from_env().sending is False          # без своего бота отправка не включается
    monkeypatch.setenv("SHTURMAN_BOT_TOKEN", "123:abc")
    cfg = Config.from_env()
    assert cfg.sending is True and cfg.own_bot is True and "123:abc" not in repr(cfg)
