"""Разбор входящих (Р-90): «Остановить всё», учёт, инструмент архива «ждут вашего ответа», смена владельца.

Решение об остановке: разбор — действие ассистента (модель + карточки), поэтому при «Остановить всё» он не
идёт, а прежняя простая пересылка «Вам написали» работает — владелец не теряет сигнал. Начатые разборы
снимаются и тоже приходят обычным уведомлением; ожидания ответа сохраняются и после «Продолжить»
напоминают снова.
"""

import pytest_asyncio

from shturman import authority, bridge, halt

from triage_helpers import (ANNA, OWNER, PETR, answer_last, at, cards, chat, enable, item, live, message,  # noqa: F401
                            model_jobs, notifications, setup_env, verdict)
from triage_helpers import core

MCP_MODULES = ("shturman.api_core", "shturman.mcp_server", "shturman.triage.service")


@pytest_asyncio.fixture
async def tm(make_client, conn, own_bot):
    return await setup_env(make_client, conn, MCP_MODULES)


async def _anna(env):
    return await chat(env.conn, env.owner_acc, ANNA, name="Анна Ким", username="anna_kim")


async def _stop(env):
    return await halt.stop(env.state, via="page")


async def _resume(env):
    with authority.owner_context(OWNER, chat_id=OWNER, action="telegram.callback"):
        async with env.conn.transaction():
            await halt.resume(env.conn, reading=False, via="bot")


async def test_stopped_assistant_does_not_read_and_plain_forwarding_works(tm):
    await enable(tm.conn)
    anna = await _anna(tm)
    # начатый разбор при остановке снимается и приходит обычным уведомлением
    _, started = await live(tm, tm.owner_acc, anna, await message(tm.conn, anna, 1, "Пришлите смету"))
    result = await _stop(tm)
    assert result["counts"]["triage"] == 1
    assert (await item(tm.conn, started))["skip_reason"] == "stopped"
    await answer_last(tm.conn, verdict(urgency="urgent"))         # модель ответила после остановки — карточки нет
    # новое сообщение при остановке: разбора нет, пересылка как раньше
    kind, item_id = await live(tm, tm.owner_acc, anna, await message(tm.conn, anna, 2, "Ау?"))
    assert kind == "personal" and item_id is None
    await tm.n.flush(at(15))
    [note] = await notifications(tm.conn)                          # одно уведомление о чате: «…и ещё 1»
    assert note.startswith("💬 Вам написали: Анна Ким") and "…и ещё 1 сообщение" in note
    assert await cards(tm.conn) == []
    assert len(await model_jobs(tm.conn)) == 1
    # база — вторая линия: строку разбора при остановке не вставить
    import asyncpg
    try:
        await tm.conn.execute(
            "INSERT INTO triage_items (account_id, chat_id, message_id, kind, nonce) VALUES ($1, $2, "
            "(SELECT max(id) FROM messages), 'personal', 'x')", tm.owner_acc, anna)
        raise AssertionError("вставка прошла при остановке")
    except asyncpg.PostgresError as exc:
        assert halt.is_halt_error(exc)
    await _resume(tm)
    kind, item_id = await live(tm, tm.owner_acc, anna, await message(tm.conn, anna, 3, "Снова я"))
    assert kind is None and isinstance(item_id, int)


async def test_reminders_wait_while_stopped_and_come_after_resume(tm):
    await enable(tm.conn)
    anna = await _anna(tm)
    _, item_id = await live(tm, tm.owner_acc, anna, await message(tm.conn, anna, 1, "Подпишите акт"))
    await answer_last(tm.conn, verdict(needs_reply=True, want="просит подписать акт"))
    await _stop(tm)
    await tm.t.sweep(at(180))
    assert await cards(tm.conn) == [] and (await item(tm.conn, item_id))["wait_state"] == "waiting"
    await _resume(tm)
    await tm.t.sweep(at(181))
    [reminder] = await cards(tm.conn)
    assert reminder["text"].startswith("⏰ Анна Ким ждёт ответа")


async def test_status_counts_and_the_status_line(tm):
    from shturman.executor import owner_views
    off = (await tm.client.get("/api/status")).json()
    assert off["triage_enabled"] is False
    assert owner_views.triage_line(off).startswith("Разбор входящих: выкл")
    await enable(tm.conn)
    anna = await _anna(tm)
    await live(tm, tm.owner_acc, anna, await message(tm.conn, anna, 1, "Срочно: смета"))
    await answer_last(tm.conn, verdict(urgency="urgent", want="смета"))
    st = (await tm.client.get("/api/status")).json()
    assert {k: st[k] for k in st if k.startswith("triage_")} == {
        "triage_enabled": True, "triage_analyzed_24h": 1, "triage_shown_24h": 1, "triage_reminded_24h": 0,
        "triage_waiting": 1, "triage_failed_24h": 0, "triage_in_progress": 0}
    assert owner_views.triage_line(st) == "Разбор входящих: вкл, за сутки: 1 разобрано, 1 показано, 1 ждут ответа."


async def _rpc(client, method, params=None):
    from conftest import MCP_AUTH
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    r = await client.post("/mcp", json=body, headers={**MCP_AUTH, "Accept": "application/json, text/event-stream",
                                                     "Host": "test"})
    assert r.status_code == 200, r.text
    return r.json()["result"]


async def test_awaiting_reply_tool_lists_who_waits_without_message_text(tm):
    off = await _rpc(tm.client, "tools/call", {"name": "list_awaiting_reply", "arguments": {}})
    assert off["structuredContent"]["status"] == "not_found"
    await enable(tm.conn)
    anna = await _anna(tm)
    petr = await chat(tm.conn, tm.owner_acc, PETR, name="Пётр")
    await live(tm, tm.owner_acc, anna, await message(tm.conn, anna, 1, "Пароль 4417. Пришлите смету по корпусу 3"))
    await answer_last(tm.conn, verdict(needs_reply=True, urgency="important", want="просит смету по корпусу 3",
                                       due="до пятницы"))
    hidden = await message(tm.conn, petr, 2, "Вопрос по договору", sender=PETR)
    await live(tm, tm.owner_acc, petr, hidden)
    await answer_last(tm.conn, verdict(needs_reply=True, want="вопрос по договору"))
    await tm.conn.execute("UPDATE messages SET agent_visible = false WHERE id = $1", hidden)   # скрыла защита позже
    out = (await _rpc(tm.client, "tools/call", {"name": "list_awaiting_reply", "arguments": {}}))["structuredContent"]
    [one] = out["items"]
    assert one["who"] == "Анна Ким" and one["kind"] == "personal" and one["urgency"] == "important"
    assert one["wants"].startswith("[untrusted]") and "просит смету по корпусу 3 (срок: до пятницы)" in one["wants"]
    assert "4417" not in str(out) and one["waiting_minutes"] >= 0 and one["reminded"] is False
    bad = await _rpc(tm.client, "tools/call", {"name": "list_awaiting_reply", "arguments": {"chat": "x"}})
    assert bad.get("isError") is True
    # владелец ответил — из списка уходит
    own = await message(tm.conn, anna, 3, "Пришлю", outgoing=True, sender=OWNER, sender_name="Евгений")
    await live(tm, tm.owner_acc, anna, own, outgoing=True)
    out = (await _rpc(tm.client, "tools/call", {"name": "list_awaiting_reply", "arguments": {}}))["structuredContent"]
    assert out.get("items") in ([], None)


async def test_owner_change_switches_triage_off_and_clears_rules(tm):
    await enable(tm.conn, important_people=[ANNA])
    with authority.owner_context(OWNER + 7, chat_id=OWNER + 7, action="test.rebind"):
        await bridge.set_owner(tm.conn, OWNER + 7, OWNER + 7)
    from shturman.triage import settings as ts
    s = await ts.load(tm.conn)
    assert s["enabled"] is False and s["important_people"] == []
