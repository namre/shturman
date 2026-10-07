"""Приём бизнес-сообщений от плагина и управление исключениями чатов — через HTTP на настоящей базе."""

import json
from types import SimpleNamespace

import pytest
import pytest_asyncio

from shturman import events
from shturman.importer import import_export

from conftest import IVAN, MARIA, OWNER, as_file

T = 1789200000
BC = "bc-owner-1"
OWNER_USER = {"id": OWNER, "is_bot": False, "first_name": "Евгений", "last_name": "Тестов"}
IVAN_USER = {"id": IVAN, "is_bot": False, "first_name": "Иван", "last_name": "Петров", "username": "ivan_p"}
MARIA_USER = {"id": MARIA, "is_bot": False, "first_name": "Мария"}
URL = "https://example.org/doc"


def private(user):
    return {k: v for k, v in user.items() if k != "is_bot"} | {"type": "private"}


def bmsg(mid, text=None, *, sender=IVAN_USER, partner=IVAN_USER, ts=T, bc=BC, **extra):
    m = {"message_id": mid, "date": ts, "chat": private(partner), "from": sender, "business_connection_id": bc}
    if text is not None:
        m["text"] = text
    m.update(extra)
    return m


def connection(user=OWNER_USER, *, bc=BC, enabled=True, **extra):
    return {"connection": {"id": bc, "user": user, "user_chat_id": user["id"], "date": T,
                           "is_enabled": enabled, **extra}}


def russian(response) -> bool:
    return any("а" <= ch <= "я" for ch in response.json()["error"])


@pytest_asyncio.fixture
async def svc(make_client, conn):
    """Сервис с привязанным владельцем и подписчиком на события."""
    client, state = await make_client("shturman.api_core", "shturman.ingest_api")
    live, gone = [], []

    async def on_live(payload):
        live.append(payload)

    async def on_gone(payload):
        gone.append(payload)

    state.events.subscribe(events.MESSAGE_LIVE, on_live)
    state.events.subscribe(events.MESSAGES_DELETED, on_gone)
    assert (await client.put("/api/owner", json={"user_id": OWNER, "chat_id": OWNER})).status_code == 200

    async def send(message, edited=False):
        r = await client.post("/api/ingest/business/message", json={"message": message, "edited": edited})
        await state.events.drain()
        return r

    return SimpleNamespace(client=client, state=state, conn=conn, live=live, gone=gone, send=send)


@pytest_asyncio.fixture
async def linked(svc):
    r = await svc.client.post("/api/ingest/business/connection",
                              json=connection(rights={"can_reply": True, "can_read_messages": True}))
    assert r.status_code == 200
    return svc


# --- подключение ---

async def test_connection_is_accepted_only_from_known_owner(make_client, conn):
    client, _ = await make_client("shturman.api_core", "shturman.ingest_api")
    # владелец сервису ещё не известен — подключение не принимается ни от кого
    r = await client.post("/api/ingest/business/connection", json=connection())
    assert (r.status_code, r.json()["code"]) == (409, "owner_unknown") and russian(r)
    await client.put("/api/owner", json={"user_id": OWNER, "chat_id": OWNER})
    # подключить бота к себе может любой пользователь Telegram
    r = await client.post("/api/ingest/business/connection", json=connection(MARIA_USER, bc="bc-stranger"))
    assert (r.status_code, r.json()["code"]) == (403, "not_owner")
    assert await conn.fetchval("SELECT count(*) FROM business_connections") == 0
    assert await conn.fetchval("SELECT count(*) FROM accounts") == 0
    r = await client.post("/api/ingest/business/message", json={"message": bmsg(1, "привет", bc="bc-stranger")})
    assert (r.status_code, r.json()["code"]) == (409, "unknown_connection")
    assert await conn.fetchval("SELECT count(*) FROM chats") == 0


async def test_connection_lifecycle(svc):
    client, conn = svc.client, svc.conn
    r = await client.post("/api/ingest/business/connection", json=connection(rights={"can_reply": True}))
    assert r.status_code == 200 and r.json()["can_reply"] is True and r.json()["enabled"] is True
    account = await conn.fetchrow("SELECT id, role, tg_user_id, label FROM accounts")
    assert (account["role"], account["tg_user_id"], account["label"]) == ("owner", OWNER, "Евгений Тестов")
    assert r.json()["account_id"] == account["id"]
    assert (await svc.send(bmsg(1, "первое"))).json()["stored"] is True

    # права отозваны: в новом Bot API поле rights без can_reply, в старом — can_reply: false
    for body in (connection(rights={"can_read_messages": True}), connection(can_reply=False), connection()):
        assert (await client.post("/api/ingest/business/connection", json=body)).json()["can_reply"] is False
    assert (await client.post("/api/ingest/business/connection", json=connection(can_reply=True))).json()["can_reply"]

    # отключение: сообщения больше не принимаются и событий нет
    await client.post("/api/ingest/business/connection", json=connection(enabled=False))
    row = await conn.fetchrow("SELECT enabled, can_reply FROM business_connections WHERE id = $1", BC)
    assert (row["enabled"], row["can_reply"]) == (False, False)
    off = await svc.send(bmsg(2, "после отключения"))
    assert off.status_code == 200 and off.json() == {"stored": False, "message_id": None,
                                                     "reason": "connection_disabled"}
    gone = await client.post("/api/ingest/business/deleted", json={
        "business_connection_id": BC, "chat": private(IVAN_USER), "message_ids": [1]})
    assert gone.json() == {"deleted": 0, "reason": "connection_disabled"}
    assert await conn.fetchval("SELECT count(*) FROM messages WHERE deleted_at IS NULL") == 1
    assert len(svc.live) == 1 and svc.gone == []

    await client.post("/api/ingest/business/connection", json=connection(rights={"can_reply": True}))
    assert (await svc.send(bmsg(2, "снова включено"))).json()["stored"] is True
    assert await conn.fetchval("SELECT count(*) FROM business_connections") == 1
    assert await conn.fetchval("SELECT count(*) FROM accounts") == 1


# --- сообщения ---

async def test_new_message_is_stored_and_announced_once(linked):
    svc, conn = linked, linked.conn
    text = f"Договор лежит {URL}, посмотрите"
    r = await svc.send(bmsg(3, text, reply_to_message=bmsg(2, "жду"),
                            entities=[{"type": "url", "offset": 14, "length": len(URL)}]))
    assert r.status_code == 200
    body = r.json()
    assert body["stored"] is True and body["new"] is True
    row = await conn.fetchrow(
        """SELECT m.*, p.tg_id AS sender_tg_id FROM messages m JOIN peers p ON p.id = m.sender_peer_id""")
    assert row["id"] == body["message_id"]
    assert (row["tg_message_id"], row["text"], row["is_outgoing"], row["sources"]) == (3, text, False, ["business"])
    assert (row["reply_to_tg_id"], row["sender_tg_id"], row["sender_name"]) == (2, IVAN, "Иван Петров")
    assert json.loads(row["entities"]) == [{"type": "link", "text": URL}]
    chat = await conn.fetchrow(
        "SELECT c.type, c.title, p.username, p.is_bot FROM chats c JOIN peers p ON p.id = c.peer_id")
    assert tuple(chat) == ("personal_chat", "Иван Петров", "ivan_p", False)
    account_id = await conn.fetchval("SELECT id FROM accounts")
    assert svc.live == [{"account_id": account_id,
                         "chat_id": row["chat_id"], "message_id": row["id"], "source": "business",
                         "outgoing": False, "edited": False, "via_bot": False}]

    # повторная доставка того же сообщения архив не меняет и событием не становится
    again = await svc.send(bmsg(3, text, entities=[{"type": "url", "offset": 14, "length": len(URL)}]))
    assert again.json() == {"stored": True, "message_id": row["id"], "new": False, "changed": False}
    assert len(svc.live) == 1
    assert await conn.fetchval("SELECT count(*) FROM messages") == 1
    assert await conn.fetchval("SELECT count(*) FROM message_versions") == 0


async def test_edit_keeps_old_text_and_stale_copy_changes_nothing(linked):
    svc, conn = linked, linked.conn
    first = (await svc.send(bmsg(1, "Пришлю смету к пятнице."))).json()
    edit = await svc.send(bmsg(1, "Пришлю смету к понедельнику.", edit_date=T + 600), edited=True)
    assert edit.json() == {"stored": True, "message_id": first["message_id"], "new": False, "changed": True}
    row = await conn.fetchrow("SELECT text, edited_at FROM messages")
    assert "понедельнику" in row["text"] and row["edited_at"] is not None
    assert await conn.fetchval("SELECT text FROM message_versions") == "Пришлю смету к пятнице."
    assert [e["edited"] for e in svc.live] == [False, True]
    assert svc.live[1]["message_id"] == first["message_id"]

    # та же правка ещё раз и запоздавшая исходная версия: ни события, ни новой версии
    await svc.send(bmsg(1, "Пришлю смету к понедельнику.", edit_date=T + 600), edited=True)
    await svc.send(bmsg(1, "Пришлю смету к пятнице."))
    assert len(svc.live) == 2
    assert await conn.fetchval("SELECT count(*) FROM message_versions") == 1
    assert "понедельнику" in await conn.fetchval("SELECT text FROM messages")

    # правка сообщения, которого в архиве не было, — это новая строка
    late = await svc.send(bmsg(9, "исправлено", edit_date=T + 700), edited=True)
    assert late.json()["new"] is True and svc.live[-1]["edited"] is True


async def test_live_edit_wins_even_when_edit_time_is_not_enough(linked):
    svc, conn = linked, linked.conn
    await svc.send(bmsg(1, "первая версия"))
    # две правки в одну и ту же секунду: вторая всё равно становится текущим текстом
    await svc.send(bmsg(1, "вторая версия", edit_date=T + 600), edited=True)
    same_second = await svc.send(bmsg(1, "третья версия", edit_date=T + 600), edited=True)
    assert same_second.json()["changed"] is True
    assert await conn.fetchval("SELECT text FROM messages") == "третья версия"
    # правка без времени правки
    no_date = await svc.send(bmsg(1, "четвёртая версия"), edited=True)
    assert no_date.json()["changed"] is True
    assert await conn.fetchval("SELECT text FROM messages") == "четвёртая версия"
    versions = [r["text"] for r in await conn.fetch("SELECT text FROM message_versions ORDER BY id")]
    assert versions == ["первая версия", "вторая версия", "третья версия"]
    assert [e["edited"] for e in svc.live] == [False, True, True, True]
    # запоздавшая копия более ранней правки текущий текст не трогает и событием не становится
    stale = await svc.send(bmsg(1, "вторая версия", edit_date=T + 300), edited=True)
    assert stale.json()["changed"] is False and len(svc.live) == 4
    assert await conn.fetchval("SELECT text FROM messages") == "четвёртая версия"
    assert await conn.fetchval("SELECT count(*) FROM message_versions") == 3


async def test_outgoing_and_sent_through_bot(linked):
    svc, conn = linked, linked.conn
    await svc.send(bmsg(1, "Добрый день"))
    await svc.send(bmsg(2, "Хорошо, жду.", sender=OWNER_USER))
    await svc.send(bmsg(3, "Спасибо, получил.", sender=OWNER_USER,
                        sender_business_bot={"id": 9001, "is_bot": True, "first_name": "Штурман"}))
    await svc.send(bmsg(4, "гифка", via_bot={"id": 9002, "is_bot": True, "first_name": "gif", "username": "gif"}))
    rows = await conn.fetch("SELECT is_outgoing FROM messages ORDER BY tg_message_id")
    assert [r["is_outgoing"] for r in rows] == [False, True, True, False]
    assert [(e["outgoing"], e["via_bot"]) for e in svc.live] == [
        (False, False), (True, False), (True, True), (False, True)]
    # исходящие лежат в чате собеседника, а не в «чате с собой»
    assert await conn.fetchval("SELECT count(*) FROM chats") == 1


async def test_bot_partner_is_stored_like_any_other_chat(linked):
    svc, conn = linked, linked.conn
    helper = {"id": 5005, "is_bot": True, "first_name": "Помощник", "username": "helper_bot"}
    r = await svc.send(bmsg(1, "Заказ принят", sender=helper, partner=helper))
    assert r.json()["stored"] is True
    row = await conn.fetchrow("SELECT c.type, p.is_bot FROM chats c JOIN peers p ON p.id = c.peer_id")
    assert tuple(row) == ("bot_chat", True)
    assert len(svc.live) == 1


async def test_media_and_service_messages(linked):
    svc, conn = linked, linked.conn
    await svc.send(bmsg(1, voice={"file_id": "v", "duration": 7}))
    await svc.send(bmsg(2, photo=[{"file_id": "p", "width": 1, "height": 1}], caption="Фото объекта"))
    await svc.send(bmsg(3, pinned_message=bmsg(2)))
    rows = await conn.fetch(
        "SELECT kind, text, media_type, media_path, service_action FROM messages ORDER BY tg_message_id")
    assert [tuple(r) for r in rows] == [
        ("message", "", "voice_message", None, None),
        ("message", "Фото объекта", "photo", None, None),
        ("service", "", None, None, "pin_message"),
    ]


async def test_deleted_messages_are_marked_and_announced(linked):
    svc, conn, client = linked, linked.conn, linked.client
    ids = [(await svc.send(bmsg(i, f"сообщение {i}"))).json()["message_id"] for i in (1, 2, 3)]
    r = await client.post("/api/ingest/business/deleted", json={
        "business_connection_id": BC, "chat": private(IVAN_USER), "message_ids": [1, 3, 99]})
    await svc.state.events.drain()
    assert r.json() == {"deleted": 2}
    assert sorted(svc.gone[0]["message_ids"]) == [ids[0], ids[2]]
    assert await conn.fetchval("SELECT count(*) FROM messages WHERE deleted_at IS NOT NULL") == 2

    # повтор и удаление в незнакомом чате: ничего не меняется, чат не заводится, события нет
    for chat in (private(IVAN_USER), private(MARIA_USER)):
        r = await client.post("/api/ingest/business/deleted", json={
            "business_connection_id": BC, "chat": chat, "message_ids": [1, 3]})
        assert r.json() == {"deleted": 0}
    await svc.state.events.drain()
    assert len(svc.gone) == 1 and await conn.fetchval("SELECT count(*) FROM chats") == 1
    r = await client.post("/api/ingest/business/deleted", json={
        "business_connection_id": "нет такого", "chat": private(IVAN_USER), "message_ids": [1]})
    assert (r.status_code, r.json()["code"]) == (409, "unknown_connection")


async def test_excluded_and_service_chats_store_nothing(linked):
    svc, conn, client = linked, linked.conn, linked.client
    await svc.send(bmsg(1, "до исключения"))
    chat_id = await conn.fetchval("SELECT id FROM chats")
    assert (await client.put(f"/api/chats/{chat_id}/excluded", json={"excluded": True})).status_code == 200
    r = await svc.send(bmsg(2, "секрет"))
    assert r.json() == {"stored": False, "message_id": None, "reason": "excluded"}
    edit = await svc.send(bmsg(1, "правка в исключённом чате", edit_date=T + 5), edited=True)
    assert edit.json()["stored"] is False
    assert await conn.fetchval("SELECT text FROM messages") == "до исключения"

    telegram = {"id": 777000, "is_bot": False, "first_name": "Telegram"}
    father = {"id": 424242, "is_bot": True, "first_name": "BotFather", "username": "BotFather"}
    for who in (telegram, father):
        r = await svc.send(bmsg(5, "Login code: 12345", sender=who, partner=who))
        assert r.json() == {"stored": False, "message_id": None, "reason": "excluded"}
    assert await conn.fetchval("SELECT count(*) FROM messages") == 1
    assert await conn.fetchval("SELECT count(*) FROM messages WHERE text LIKE '%12345%'") == 0
    assert len(svc.live) == 1


async def test_unknown_connection_asks_to_resend_it(svc):
    r = await svc.send(bmsg(1, "привет"))
    assert (r.status_code, r.json()["code"]) == (409, "unknown_connection") and russian(r)
    assert await svc.conn.fetchval("SELECT count(*) FROM chats") == 0 and svc.live == []
    await svc.client.post("/api/ingest/business/connection", json=connection())
    assert (await svc.send(bmsg(1, "привет"))).json()["stored"] is True


async def test_message_without_id_is_skipped(linked):
    r = await linked.send(bmsg(0, "ещё не отправлено"))
    assert r.status_code == 200 and r.json() == {"stored": False, "message_id": None, "reason": "no_message_id"}
    assert await linked.conn.fetchval("SELECT count(*) FROM chats") == 0 and linked.live == []


# --- одно сообщение из двух источников ---

def business_copies():
    """Те же сообщения чата с Иваном, что в образце экспорта (кроме звонка: бот его не получает)."""
    return [
        bmsg(1, "Добрый день! Пришлю смету по фасадам к пятнице.", ts=T),
        bmsg(2, "Хорошо, жду. Сроки монтажа не сдвигаем.", ts=T + 60, sender=OWNER_USER),
        bmsg(3, f"Договор лежит {URL}, посмотрите", ts=T + 120, reply_to_message=bmsg(2, "…"),
             entities=[{"type": "url", "offset": 14, "length": len(URL)}]),
        bmsg(5, ts=T + 400, voice={"file_id": "v", "duration": 7}),
        bmsg(6, ts=T + 500, photo=[{"file_id": "p", "width": 1, "height": 1}], caption="Фото объекта"),
    ]


async def ivan_rows(conn):
    return await conn.fetch(
        """SELECT m.* FROM messages m JOIN chats c ON c.id = m.chat_id JOIN peers p ON p.id = c.peer_id
           WHERE p.tg_id = $1 ORDER BY m.tg_message_id""", IVAN)


async def test_export_then_business_is_one_row_without_false_edit(linked, sample_export):
    svc, conn = linked, linked.conn
    await import_export(conn, as_file(sample_export))
    before = [dict(r) for r in await ivan_rows(conn)]
    for message in business_copies():
        body = (await svc.send(message)).json()
        assert (body["stored"], body["new"], body["changed"]) == (True, False, False)
    after = [dict(r) for r in await ivan_rows(conn)]
    assert len(after) == 6 and await conn.fetchval("SELECT count(*) FROM messages") == 8
    assert await conn.fetchval("SELECT count(*) FROM message_versions") == 0
    assert [r["sources"] for r in after] == [["import", "business"]] * 3 + [["import"]] + [["import", "business"]] * 2
    for old, new in zip(before, after):
        assert {k: v for k, v in old.items() if k != "sources"} == {k: v for k, v in new.items() if k != "sources"}
    assert svc.live == []            # прошлое из экспорта событием не становится
    assert await conn.fetchval("SELECT count(*) FROM accounts") == 1


async def test_business_then_export_is_one_row_without_false_edit(linked, sample_export):
    svc, conn = linked, linked.conn
    for message in business_copies():
        assert (await svc.send(message)).json()["new"] is True
    stats = await import_export(conn, as_file(sample_export))
    assert (stats.messages_new, stats.messages_known, stats.versions_added) == (3, 5, 0)
    rows = await ivan_rows(conn)
    assert len(rows) == 6 and await conn.fetchval("SELECT count(*) FROM message_versions") == 0
    by_id = {r["tg_message_id"]: r for r in rows}
    assert by_id[1]["sources"] == ["business", "import"] and by_id[4]["sources"] == ["import"]
    assert [by_id[i]["is_outgoing"] for i in (1, 2, 3, 5, 6)] == [False, True, False, False, False]
    assert json.loads(by_id[3]["entities"]) == [{"type": "link", "text": URL}] and by_id[3]["reply_to_tg_id"] == 2
    # экспорт дополняет запись тем, чего бизнес-бот не даёт: путём к файлу
    assert (by_id[5]["media_type"], by_id[5]["media_path"]) == ("voice_message",
                                                               "chats/chat_01/voice_messages/audio_1.ogg")
    assert by_id[6]["media_type"] == "photo" and len(svc.live) == 5


# --- список чатов и исключения ---

async def test_chat_list_has_counters_but_no_text(linked, sample_export):
    svc, conn, client = linked, linked.conn, linked.client
    await import_export(conn, as_file(sample_export))
    await svc.send(bmsg(50, "Свежее сообщение с тайной", ts=T + 9000))
    body = (await client.get("/api/chats")).json()
    assert body["total"] == 4 and [c["title"] for c in body["chats"]][0] == "Иван Петров"
    ivan = body["chats"][0]
    assert (ivan["kind"], ivan["type"], ivan["peer"], ivan["messages"], ivan["excluded"], ivan["locked"]) == (
        "user", "personal_chat", f"user:{IVAN}", 7, False, False)
    assert ivan["last_message_at"].startswith("2026-") and ivan["account_role"] == "owner"
    assert "тайной" not in json.dumps(body, ensure_ascii=False) and "смету" not in json.dumps(body, ensure_ascii=False)
    assert [c["title"] for c in body["chats"]][-1] == "Пустой" and body["chats"][-1]["last_message_at"] is None

    assert [c["title"] for c in (await client.get("/api/chats", params={"query": "стройка"})).json()["chats"]] == [
        "Стройка: новости"]
    assert (await client.get("/api/chats", params={"query": "@ivan_p"})).json()["total"] == 1
    assert (await client.get("/api/chats", params={"query": "%"})).json()["total"] == 0   # не шаблон, а знак
    page = (await client.get("/api/chats", params={"limit": 2, "offset": 1})).json()
    assert len(page["chats"]) == 2 and page["total"] == 4 and page["chats"][0]["title"] != "Иван Петров"
    assert (await client.get("/api/chats", params={"excluded": "true"})).json()["total"] == 0
    for bad in ({"limit": "0"}, {"limit": "abc"}, {"offset": "-1"}, {"excluded": "может быть"}, {"limit": "9" * 30}):
        r = await client.get("/api/chats", params=bad)
        assert r.status_code == 400 and russian(r)


async def test_exclude_purge_and_return(linked, sample_export):
    svc, conn, client = linked, linked.conn, linked.client
    await import_export(conn, as_file(sample_export))
    # правка, чтобы у сообщения была история версий: она должна уйти вместе с ним
    await svc.send(bmsg(1, "Пришлю смету к понедельнику.", edit_date=T + 600), edited=True)
    assert await conn.fetchval("SELECT count(*) FROM message_versions") == 1
    chat_id = await conn.fetchval(
        "SELECT c.id FROM chats c JOIN peers p ON p.id = c.peer_id WHERE p.tg_id = $1", IVAN)

    r = await client.put(f"/api/chats/{chat_id}/excluded", json={"excluded": True})
    assert r.json() == {"id": chat_id, "excluded": True, "purged": 0}
    assert await conn.fetchval("SELECT count(*) FROM messages WHERE chat_id = $1", chat_id) == 6
    listed = (await client.get("/api/chats", params={"excluded": "true"})).json()
    assert [c["id"] for c in listed["chats"]] == [chat_id]

    r = await client.put(f"/api/chats/{chat_id}/excluded", json={"excluded": True, "purge": True})
    assert r.json() == {"id": chat_id, "excluded": True, "purged": 6}
    assert await conn.fetchval("SELECT count(*) FROM messages WHERE chat_id = $1", chat_id) == 0
    assert await conn.fetchval("SELECT count(*) FROM message_versions") == 0
    assert await conn.fetchval("SELECT count(*) FROM messages") == 2          # остальные чаты не тронуты
    # запрет помнится для всех источников
    assert (await svc.send(bmsg(60, "после исключения"))).json()["stored"] is False
    assert (await import_export(conn, as_file(sample_export))).chats_excluded == 1
    assert await conn.fetchval("SELECT count(*) FROM messages WHERE chat_id = $1", chat_id) == 0

    # вернуть чат можно только явным отдельным действием
    r = await client.put(f"/api/chats/{chat_id}/excluded", json={"excluded": False})
    assert r.json() == {"id": chat_id, "excluded": False, "purged": 0}
    assert (await svc.send(bmsg(61, "снова в архиве"))).json()["stored"] is True

    for body, status in (({"excluded": False, "purge": True}, 400), ({"purge": True}, 400),
                         ({"excluded": "да"}, 400), ({"excluded": True, "purge": 1}, 400)):
        r = await client.put(f"/api/chats/{chat_id}/excluded", json=body)
        assert r.status_code == status and russian(r)
    for missing in (999999, 2 ** 70):
        r = await client.put(f"/api/chats/{missing}/excluded", json={"excluded": True})
        assert r.status_code == 404 and russian(r)


async def test_service_chat_cannot_be_returned(linked):
    svc, conn, client = linked, linked.conn, linked.client
    telegram = {"id": 777000, "is_bot": False, "first_name": "Telegram"}
    await svc.send(bmsg(1, "Login code: 12345", sender=telegram, partner=telegram))
    chat = (await client.get("/api/chats")).json()["chats"][0]
    assert (chat["excluded"], chat["locked"], chat["messages"]) == (True, True, 0)
    r = await client.put(f"/api/chats/{chat['id']}/excluded", json={"excluded": False})
    assert (r.status_code, r.json()["code"]) == (409, "locked") and russian(r)
    assert await conn.fetchval("SELECT excluded FROM chats WHERE id = $1", chat["id"]) is True


# --- неверные запросы ---

GROUP = {"id": -1003001, "type": "supergroup", "title": "Подрядчики"}


@pytest.mark.parametrize("path,payload", [
    ("/api/ingest/business/connection", {}),
    ("/api/ingest/business/connection", {"connection": "строка"}),
    ("/api/ingest/business/connection", {"connection": {"id": "", "user": OWNER_USER, "is_enabled": True}}),
    ("/api/ingest/business/connection", {"connection": {"id": "x", "user": {"id": "1000"}, "is_enabled": True}}),
    ("/api/ingest/business/connection", {"connection": {"id": "x", "user": {"id": 2 ** 70}, "is_enabled": True}}),
    ("/api/ingest/business/connection", {"connection": {"id": "x", "user": OWNER_USER}}),
    ("/api/ingest/business/connection", {"connection": {"id": "x", "user": OWNER_USER, "is_enabled": "да"}}),
    ("/api/ingest/business/connection",
     {"connection": {"id": "x", "user": {"id": 77, "is_bot": True}, "is_enabled": True}}),
    ("/api/ingest/business/message", {}),
    ("/api/ingest/business/message", {"message": []}),
    ("/api/ingest/business/message", {"message": bmsg(1, "x"), "edited": "да"}),
    ("/api/ingest/business/message", {"message": bmsg(1, "x", bc=None)}),
    ("/api/ingest/business/message", {"message": bmsg(1, "x", chat=GROUP)}),
    ("/api/ingest/business/message", {"message": bmsg(1, "x", chat=None)}),
    ("/api/ingest/business/message", {"message": bmsg("один", "x")}),
    ("/api/ingest/business/message", {"message": bmsg(2 ** 70, "x")}),
    ("/api/ingest/business/message", {"message": bmsg(1, "x", ts="вчера")}),
    ("/api/ingest/business/message", {"message": bmsg(1, "x", ts=10 ** 30)}),
    ("/api/ingest/business/message", {"message": bmsg(1, "x", sender={"id": None})}),
    ("/api/ingest/business/message", {"message": bmsg(1, "я" * 20000)}),
    ("/api/ingest/business/deleted", {}),
    ("/api/ingest/business/deleted", {"business_connection_id": BC, "chat": private(IVAN_USER)}),
    ("/api/ingest/business/deleted", {"business_connection_id": BC, "chat": GROUP, "message_ids": [1]}),
    ("/api/ingest/business/deleted", {"business_connection_id": BC, "chat": private(IVAN_USER),
                                      "message_ids": [1, "2"]}),
    ("/api/ingest/business/deleted", {"business_connection_id": BC, "chat": private(IVAN_USER),
                                      "message_ids": [2 ** 70]}),
    ("/api/ingest/business/deleted", {"business_connection_id": BC, "chat": private(IVAN_USER),
                                      "message_ids": "1,2"}),
])
async def test_malformed_json_bodies_are_400_in_russian(linked, path, payload):
    r = await linked.client.post(path, json=payload)
    assert r.status_code == 400, r.text
    assert russian(r)
    assert await linked.conn.fetchval("SELECT count(*) FROM messages") == 0


async def test_not_json_and_oversized_bodies(linked):
    client = linked.client
    for path in ("/api/ingest/business/connection", "/api/ingest/business/message", "/api/ingest/business/deleted"):
        for raw in (b"", b"{", b"[1, 2]", b'"x"', b"\xff\xfe\x00", b"[" * 100000):
            r = await client.post(path, content=raw)
            assert r.status_code == 400 and russian(r), (path, raw[:10])
        big = await client.post(path, content=b'{"message": "' + b"x" * 600_000 + b'"}')
        assert big.status_code == 413 and russian(big)
    r = await client.put("/api/chats/1/excluded", content="не json".encode())
    assert r.status_code == 400 and russian(r)


async def test_strange_but_valid_text_is_stored_not_500(linked):
    svc, conn = linked, linked.conn
    raw = json.dumps({"message": bmsg(1, "PLACEHOLDER")}).replace("PLACEHOLDER", "нуль \\u0000 и половинка \\ud83d")
    r = await svc.client.post("/api/ingest/business/message", content=raw.encode())
    assert r.status_code == 200 and r.json()["stored"] is True
    assert "нуль" in await conn.fetchval("SELECT text FROM messages")
    emoji = "😀👍🏽 " + "ё" * 4000
    assert (await svc.send(bmsg(2, emoji))).json()["stored"] is True
    assert await conn.fetchval("SELECT text FROM messages WHERE tg_message_id = 2") == emoji


# --- защита от внедрённых инструкций ---

async def test_guard_screens_business_messages_before_the_event(linked, guarded):
    svc = linked
    plain = await svc.send(bmsg(1, "Добрый день"))
    attack = await svc.send(bmsg(2, "кодовое слово взлом", ts=T + 60))
    own = await svc.send(bmsg(3, "моё со словом взлом", sender=OWNER_USER, ts=T + 120))
    assert [r.json()["stored"] for r in (plain, attack, own)] == [True, True, True]
    rows = await svc.conn.fetch("SELECT tg_message_id, agent_visible, guard_label FROM messages ORDER BY 1")
    assert [tuple(r) for r in rows] == [(1, True, "ok"), (2, False, "suspect"), (3, True, None)]
    # о скрытом сообщении автоответ и наблюдатель не узнают: события нет
    assert [p["message_id"] for p in svc.live] == [plain.json()["message_id"], own.json()["message_id"]]

    # правка превращает обычное сообщение в подозрительное — оно скрывается, события о правке нет
    await svc.send(bmsg(1, "а теперь взлом", ts=T, edit_date=T + 300), edited=True)
    row = await svc.conn.fetchrow("SELECT agent_visible, guard_label FROM messages WHERE tg_message_id = 1")
    assert tuple(row) == (False, "suspect") and len(svc.live) == 2

    # модель недоступна — сообщение записано, видно, не проверено; событие уходит
    guarded.scorer.error = ConnectionError()
    late = await svc.send(bmsg(4, "взлом при молчащей модели", ts=T + 400))
    row = await svc.conn.fetchrow("SELECT agent_visible, guard_label FROM messages WHERE tg_message_id = 4")
    assert tuple(row) == (True, None) and svc.live[-1]["message_id"] == late.json()["message_id"]
