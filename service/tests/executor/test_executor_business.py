"""Бизнес-режим через бота согласований: от подключения до отправленного черновика."""

import dataclasses
import time

import pytest_asyncio

from shturman import bridge, events
from shturman.executor import binding
from shturman.executor import service as executor_service
from shturman.outbox import policy

from exec_fakes import (  # noqa: F401 — rig — фикстура
    BOT_ID, IVAN, IVAN_USER, OWNER, OWNER_USER, STRANGER_USER, TOKEN, FakeTelegram, bind, buttons_of,
    private, rig, until,
)

BC = "bc-service-owner"
RIGHTS = {"can_reply": True, "can_read_messages": True}


def link(user=OWNER_USER, *, bc=BC, enabled=True, rights=RIGHTS) -> dict:
    return {"id": bc, "user": user, "user_chat_id": user["id"], "date": int(time.time()),
            "is_enabled": enabled, "rights": rights}


def bmsg(mid, text, *, sender=IVAN_USER, bc=BC, ago=0, **extra) -> dict:
    return {"message_id": mid, "date": int(time.time()) - ago, "chat": private(IVAN_USER), "from": sender,
            "business_connection_id": bc, "text": text, **extra}


async def connections(conn) -> list[tuple]:
    rows = await conn.fetch("SELECT id, via, enabled, can_reply FROM business_connections ORDER BY id")
    return [tuple(r) for r in rows]


async def texts(conn) -> list[str]:
    return [r["text"] for r in await conn.fetch("SELECT text FROM messages ORDER BY tg_message_id")]


# --- подключение ---

async def test_connection_is_accepted_only_from_the_owner_bound_in_this_bot(rig):
    rig.tg.push(business_connection=link())                 # владелец ещё не привязан
    await rig.bot.poll_once()
    assert await connections(rig.conn) == []

    await bridge.set_owner(rig.conn, OWNER, OWNER)          # запись о владельце есть, привязки через бота нет
    rig.tg.push(business_connection=link())
    await rig.bot.poll_once()
    assert await connections(rig.conn) == []

    await bind(rig)
    rig.tg.push(business_connection=link(STRANGER_USER, bc="bc-stranger"))   # бота подключил к себе посторонний
    rig.tg.push(business_connection=link())
    await rig.bot.poll_once()
    assert await connections(rig.conn) == [(BC, "service", True, True)]
    assert rig.bot.counters["business_refused"] == 3 and rig.bot.counters["business_connections"] == 1

    rig.tg.push(business_connection=link(enabled=False, rights={"can_reply": False}))
    await rig.bot.poll_once()
    assert await connections(rig.conn) == [(BC, "service", False, False)]


async def test_messages_edits_and_deletes_are_archived_and_announced(rig):
    live, gone = [], []

    async def on_live(payload):
        live.append(payload)

    async def on_gone(payload):
        gone.append(payload)

    rig.state.events.subscribe(events.MESSAGE_LIVE, on_live)
    rig.state.events.subscribe(events.MESSAGES_DELETED, on_gone)
    await bind(rig)
    rig.tg.push(business_connection=link())
    rig.tg.push(business_message=bmsg(40, "Здравствуйте!", sender=OWNER_USER, ago=60))
    rig.tg.push(business_message=bmsg(41, "Когда будет смета?"))
    rig.tg.push(edited_business_message=bmsg(41, "Когда будет смета по фасадам?", edit_date=int(time.time()) + 1))
    await rig.bot.poll_once()
    await rig.state.events.drain()
    assert await texts(rig.conn) == ["Здравствуйте!", "Когда будет смета по фасадам?"]
    assert [(e["source"], e["outgoing"], e["edited"]) for e in live] == [
        ("business", True, False), ("business", False, False), ("business", False, True)]

    rig.tg.push(deleted_business_messages={"business_connection_id": BC, "chat": private(IVAN_USER),
                                           "message_ids": [41]})
    await rig.bot.poll_once()
    await rig.state.events.drain()
    assert len(gone) == 1 and len(gone[0]["message_ids"]) == 1
    assert await rig.conn.fetchval("SELECT count(*) FROM messages WHERE deleted_at IS NOT NULL") == 1
    assert rig.bot.counters["business_messages"] == 3 and rig.bot.counters["business_deleted"] == 1


async def test_unknown_connection_is_fetched_from_telegram_once(rig):
    await bind(rig)
    rig.tg.connections[BC] = link()                         # обновление о подключении до сервиса не дошло
    rig.tg.push(business_message=bmsg(41, "Когда будет смета?"))
    rig.tg.push(business_message=bmsg(42, "И договор"))
    await rig.bot.poll_once()
    assert await connections(rig.conn) == [(BC, "service", True, True)]
    assert await texts(rig.conn) == ["Когда будет смета?", "И договор"]
    assert len(rig.tg.calls("getBusinessConnection")) == 1

    # чужое подключение: Telegram его знает, но создал его не владелец — второй раз не спрашиваем
    rig.tg.connections["bc-x"] = link(STRANGER_USER, bc="bc-x")
    rig.tg.push(business_message=bmsg(50, "Чужая переписка", bc="bc-x"))
    rig.tg.push(business_message=bmsg(51, "Ещё чужая", bc="bc-x"))
    rig.tg.push(business_message=bmsg(52, "Нет такого подключения", bc="bc-none"))
    await rig.bot.poll_once()
    assert len(rig.tg.calls("getBusinessConnection")) == 3
    assert len(await texts(rig.conn)) == 2 and rig.bot.counters["business_rejected"] == 3


# --- два пути доставки ---

async def test_with_own_bot_business_goes_only_through_it(rig):
    await bind(rig)
    rig.tg.push(business_connection=link())
    await rig.bot.poll_once()
    rig.tg.push(business_message=bmsg(61, "Через бота согласований"))
    await rig.bot.poll_once()

    # держатель токена API не может ни завести своё подключение, ни писать в архив, ни перехватить чужое
    for path, body in (
        ("/api/ingest/business/connection", {"connection": link(bc="bc-plugin")}),
        ("/api/ingest/business/message", {"message": bmsg(70, "Поддельное сообщение")}),
        ("/api/ingest/business/connection", {"connection": link(enabled=False)}),
        ("/api/ingest/business/deleted", {"business_connection_id": BC, "chat": private(IVAN_USER), "message_ids": [61]}),
    ):
        refused = await rig.client.post(path, json=body)
        assert refused.status_code == 403 and refused.json()["code"] == "own_bot"
    assert await texts(rig.conn) == ["Через бота согласований"]
    assert await connections(rig.conn) == [(BC, "service", True, True)]
    assert await rig.conn.fetchval("SELECT count(*) FROM messages WHERE deleted_at IS NOT NULL") == 0

    # подключение, оставшееся от бота в Hermes (записано до включения своего бота), своему боту не подчиняется
    account = await rig.conn.fetchval("SELECT account_id FROM business_connections WHERE id = $1", BC)
    await rig.conn.execute(
        "INSERT INTO business_connections (id, account_id, can_reply, enabled, via) VALUES ('bc-plugin', $1, true, true, 'plugin')",
        account)
    rig.tg.push(business_message=bmsg(71, "Не тот путь", bc="bc-plugin"))
    rig.tg.push(business_connection=link(bc="bc-plugin", enabled=False))
    await rig.bot.poll_once()
    assert await texts(rig.conn) == ["Через бота согласований"]
    assert await connections(rig.conn) == [("bc-plugin", "plugin", True, True), (BC, "service", True, True)]

    # отправка идёт через того бота, через которого пришло подключение
    for bc in ("bc-plugin", BC):
        await bridge.request_business_send(rig.conn, handler="x", business_connection_id=bc, chat_id=IVAN, text="т")
    rows = await rig.conn.fetch("SELECT payload->>'business_connection_id' AS bc, executor FROM jobs ORDER BY id")
    assert [(r["bc"], r["executor"]) for r in rows] == [("bc-plugin", "plugin"), (BC, "builtin")]


# --- полный путь ---

@pytest_asyncio.fixture
async def service(make_client, conn, config):
    tg = FakeTelegram()
    executor_service.TEST_OVERRIDES.update(bot_transport=tg.transport(), poll=0, idle=0.02)
    cfg = dataclasses.replace(config, bot_token=TOKEN, sending=True, send_daily_hard_cap=1000)
    try:
        client, state = await make_client(
            "shturman.api_core", "shturman.executor.service", "shturman.ingest_api", "shturman.outbox.service", cfg=cfg)
        await policy.update(conn, {"min_pause_seconds": 0, "part_pause_seconds": 0})
        yield client, state, tg
    finally:
        executor_service.TEST_OVERRIDES.clear()


async def test_business_reply_from_connection_to_sent_draft_without_hermes(service, conn):
    client, state, tg = service

    # 1. Оператор создаёт ссылку, владелец открывает её в боте согласований.
    code, _ = await binding.create_code(conn)
    tg.text(f"/start {code}")
    await until(lambda: binding.bound_owner(conn, BOT_ID))

    # 2. Владелец подключает этого бота в Telegram Business; идёт переписка.
    tg.push(business_connection=link())
    tg.push(business_message=bmsg(40, "Здравствуйте, Иван!", sender=OWNER_USER, ago=3600))
    tg.push(business_message=bmsg(41, "Когда будет смета?"))
    await until(lambda: conn.fetchval("SELECT count(*) = 2 FROM messages"))
    assert await connections(conn) == [(BC, "service", True, True)]
    chat_id = await conn.fetchval("SELECT id FROM chats")
    incoming = await conn.fetchval("SELECT id FROM messages WHERE tg_message_id = 41")

    # 3. Ассистент готовит черновик (здесь — запросом к шлюзу отправки, как это делает инструмент агента).
    made = await client.post("/api/outbox/drafts", json={
        "chat_id": chat_id, "text": "Добрый день! Смету пришлю в пятницу.", "reply_to_message_id": incoming})
    assert made.status_code == 200, made.text
    draft_id = made.json()["draft_id"]

    # 4. Карточка приходит владельцу в бота согласований; плагину она не выдаётся.
    assert (await client.post("/api/jobs/claim", json={"limit": 20})).json()["jobs"] == []
    cards = await until(lambda: [p for p in tg.sent() if f"№ {draft_id}" in p["text"]])
    assert cards[0]["chat_id"] == OWNER and "Смету пришлю в пятницу" in cards[0]["text"]
    card_id = tg.last_message_id()
    assert tg.sent(business=True) == []                     # до нажатия ничего не отправлено
    assert await conn.fetchval("SELECT status FROM outbox_drafts WHERE id = $1", draft_id) == "pending"

    # 5. Владелец нажимает «Отправить» — нажатие приходит от Telegram прямо сервису.
    tg.press(buttons_of(cards[0])["Отправить"], message_id=card_id,
             markup=cards[0]["reply_markup"])
    await until(lambda: conn.fetchval("SELECT status = 'sent' FROM outbox_drafts WHERE id = $1", draft_id))

    # 6. Сообщение ушло от имени владельца через бизнес-подключение бота согласований — один раз.
    sent = tg.sent(business=True)
    assert sent == [{"chat_id": IVAN, "text": "Добрый день! Смету пришлю в пятницу.",
                     "business_connection_id": BC, "reply_parameters": {"message_id": 41}}]
    row = await conn.fetchrow("SELECT status, sent_tg_message_ids FROM outbox_drafts WHERE id = $1", draft_id)
    assert list(row["sent_tg_message_ids"]) == [tg.last_message_id()]
    job = await conn.fetchrow("SELECT executor, status, worker FROM jobs WHERE kind = 'business.send'")
    assert tuple(job) == ("builtin", "done", "builtin")
    assert tg.calls("answerCallbackQuery")[-1]["text"] == "Принято, отправляю."
    await until(lambda: [p for p in tg.calls("editMessageText") if "отправлен" in p["text"]])
    assert all(p["message_id"] == card_id for p in tg.calls("editMessageText"))

    # 7. Telegram возвращает отправленное сообщение как обычное бизнес-сообщение — оно ложится в архив.
    tg.push(business_message=bmsg(tg.last_message_id(), "Добрый день! Смету пришлю в пятницу.", sender=OWNER_USER,
                                  sender_business_bot={"id": BOT_ID, "is_bot": True, "first_name": "Согласования"}))
    await until(lambda: conn.fetchval("SELECT count(*) = 3 FROM messages"))
    status = (await client.get("/api/executor/status")).json()
    assert status["jobs"]["done"]["business.send"] == 1 and status["jobs"]["failed"] == {}
    assert len(tg.sent(business=True)) == 1


async def test_with_sending_switched_off_the_press_sends_nothing(service, conn):
    client, state, tg = service
    code, _ = await binding.create_code(conn)
    tg.text(f"/start {code}")
    tg.push(business_connection=link())
    tg.push(business_message=bmsg(40, "Здравствуйте, Иван!", sender=OWNER_USER, ago=3600))
    tg.push(business_message=bmsg(41, "Когда будет смета?"))
    await until(lambda: conn.fetchval("SELECT count(*) = 2 FROM messages"))
    made = await client.post("/api/outbox/drafts", json={
        "chat_id": await conn.fetchval("SELECT id FROM chats"), "text": "Добрый день!"})
    assert made.status_code == 200, made.text
    cards = await until(lambda: [p for p in tg.sent() if "Отправить" in buttons_of(p)])
    state.config = dataclasses.replace(state.config, sending=False)      # владелец выключил отправку
    tg.press(buttons_of(cards[0])["Отправить"], message_id=tg.last_message_id())
    await until(lambda: tg.calls("answerCallbackQuery"))
    row = await conn.fetchrow("SELECT status, approved_at FROM outbox_drafts WHERE id = $1",
                              made.json()["draft_id"])
    assert tuple(row) == ("pending", None)
    assert "Пока нельзя:" in tg.calls("answerCallbackQuery")[-1]["text"]
    assert tg.sent(business=True) == []
