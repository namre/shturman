"""Уведомления, наблюдатель и повтор после сбоя на странице «Переписка» и в приложении (Р-84).

Главное:
  * со страницы владелец меняет уведомления и правила наблюдателя сразу, без карточки в боте; в журнал —
    коды и числа, без имён, названий и слов правил;
  * в приложении уведомления можно только сузить или перенастроить по времени; расширение отвергается
    (как массовое включение чатов, Р-80), а изменение бот сообщает владельцу;
  * срабатывания наблюдателя показываются без текста сообщения;
  * повтор возвращает в работу только то, что не получилось из-за сбоя модели.
"""

import json

from setup_fakes import API, ORIGIN, bind_owner, stand  # noqa: F401, I001 — stand — фикстура; первым: добавляет пути
import exec_fakes  # noqa: E402

from shturman import spheres, store  # noqa: E402
from shturman.notify import settings as ns  # noqa: E402
from shturman.records import ChatRecord, MessageRecord  # noqa: E402

from test_miniapp import OWNER, TOKEN, App, signed  # noqa: E402

MODULES = ("shturman.api_core", "shturman.executor.service", "shturman.ingest_api", "shturman.tg.service",
           "shturman.outbox.service", "shturman.notify.service", "shturman.setup_page.service")
SECRET = ("Анна", "Ким", "Стройка", "фасад", "подрядчик", "Ищем")


async def ready(stand, conn):
    s = await stand(modules=MODULES, bot_token=TOKEN)
    await s.page.login(conn)
    await bind_owner(s)
    s.app = App(s.api._transport, ORIGIN)
    assert (await s.app.sign_in(signed())).status_code == 200
    s.acc = await store.ensure_account(conn, OWNER["id"], "Владелец", "owner")
    return s


async def audit_rows(conn, *actions):
    return [(r["action"], r["outcome"], r["detail"]) for r in await conn.fetch(
        "SELECT action, outcome, detail FROM setup_audit WHERE action = ANY($1::text[]) ORDER BY id", list(actions))]


def no_secrets(value) -> None:
    dumped = json.dumps(value, ensure_ascii=False)
    assert not any(word in dumped for word in SECRET), dumped


async def told(s, words: str) -> list[str]:
    def found():
        return [m["text"] for m in s.telegram.sent() if words in m["text"] and m["chat_id"] == OWNER["id"]]
    await exec_fakes.until(found)
    return found()


# --- уведомления --------------------------------------------------------------------------------------

async def test_page_changes_notifications_at_once_and_logs_only_codes(stand, conn):
    s = await ready(stand, conn)
    body = (await s.page.get("/notify")).json()
    assert body["default"] is True and body["own_bot"] is True and body["settings"]["personal"] == "all"
    assert body["timezone"] and body["describe"][0] == "• личные сообщения: от всех"
    anna, _ = await store.ensure_chat(conn, s.acc, ChatRecord("user", 2001, "personal_chat", "Анна Ким"))
    bot, _ = await store.ensure_chat(conn, s.acc, ChatRecord("user", 4040, "bot_chat", "Бот", is_bot=True))
    people = (await s.page.get("/notify/people", params={"q": "анна"})).json()["items"]
    assert [(p["tg_id"], p["name"]) for p in people] == [(2001, "Анна Ким")]
    assert [p["tg_id"] for p in (await s.page.get("/notify/people")).json()["items"]] == [2001]   # бот — нет

    r = await s.page.put("/notify", {"personal": "people", "people": [2001], "groups": False, "aliases": ["Евгений"],
                                     "mode": "digest", "digest_minutes": 60})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["default"] is False and got["people"] == [{"tg_id": 2001, "name": "Анна Ким", "username": None}]
    # расширение со страницы — тоже сразу: это сам владелец, карточки в боте нет
    r = await s.page.put("/notify", {"personal": "all", "groups": True})
    assert r.status_code == 200 and (await ns.load(conn))["personal"] == "all"
    assert await conn.fetchval("SELECT count(*) FROM pending_actions") == 0
    rows = await audit_rows(conn, "notify.settings")
    assert [a for a, _, _ in rows] == ["notify.settings"] * 2 and "людей: 1" in rows[0][2]
    no_secrets(rows)
    assert "Евгений" not in json.dumps(rows, ensure_ascii=False)
    important = (await s.page.get("/overview")).json()["audit_key"]
    assert any(item["action"] == "notify.settings" for item in important)

    bad = await s.page.put("/notify", {"personal": "spheres", "spheres": [12345]})
    assert bad.status_code == 400 and "сфер" in bad.json()["error"]
    assert (await s.page.put("/notify", {"quiet_to": "8 утра"})).status_code == 400


async def test_app_can_only_narrow_and_owner_is_told(stand, conn):
    s = await ready(stand, conn)
    assert (await s.app.get("/notify")).json()["settings"]["personal"] == "all"
    r = await s.app.put("/notify", {"personal": "off", "preview": False, "quiet_from": "22:00"})
    assert r.status_code == 200, r.text
    assert (await ns.load(conn))["personal"] == "off"
    text = (await told(s, "Из приложения изменены уведомления"))[0]
    assert "личные сообщения: выключены" in text and "тихие часы: 22:00–08:00" in text

    for widen in ({"personal": "all"}, {"preview": True}, {"aliases": ["Евгений"]}):
        refused = await s.app.put("/notify", widen)
        assert refused.status_code == 400 and refused.json()["code"] == "page_only", widen
    assert (await ns.load(conn))["personal"] == "off" and (await ns.load(conn))["preview"] is False
    rows = await audit_rows(conn, "app.notify")
    assert [o for _, o, _ in rows] == ["ok", "refused", "refused", "refused"]
    # страница — может
    assert (await s.page.put("/notify", {"personal": "all", "preview": True})).status_code == 200


async def test_app_session_cannot_use_page_routes_and_back(stand, conn):
    s = await ready(stand, conn)
    page_key, app_key = s.page.key, s.app.key
    s.page.key, s.app.key = app_key, page_key
    assert (await s.page.put("/notify", {"personal": "off"})).status_code == 401
    assert (await s.app.put("/notify", {"personal": "off"})).status_code == 401
    assert (await ns.load(conn))["personal"] == "all"


# --- наблюдатель ------------------------------------------------------------------------------------------

async def _group(conn, acc, tg_id=3001, title="Стройка: чат"):
    chat_id, _ = await store.ensure_chat(conn, acc, ChatRecord("channel", tg_id, "private_supergroup", title))
    return chat_id


async def test_watch_rules_from_the_page_apply_at_once_with_spheres_and_templates(stand, conn):
    s = await ready(stand, conn)
    grp = await _group(conn, s.acc)
    work = (await spheres.create_sphere(conn, "Работа"))["id"]
    listed = (await s.page.get("/watch")).json()
    assert [t["id"] for t in listed["templates"]] == ["money", "urgent", "name"]
    money = listed["templates"][0]
    assert {"срок", "аванс", "договор"} <= set(money["keywords"])

    r = await s.page.post("/watch", {"name": money["name"], "description": money["description"],
                                     "keywords": money["keywords"], "use_lemmas": True, "sphere_ids": [work]})
    assert r.status_code == 200, r.text
    rule = r.json()["rule"]
    assert rule["chat_ids"] == [] and rule["sphere_ids"] == [work] and rule["enabled"] is True
    assert await conn.fetchval("SELECT count(*) FROM pending_actions") == 0      # без карточки в боте

    r = await s.page.put(f"/watch/{rule['id']}", {"chat_ids": [grp], "keywords": ["фасад", "подрядчик"]})
    assert r.status_code == 200 and r.json()["rule"]["chat_ids"] == [grp]
    shown = (await s.page.get("/watch")).json()["rules"][0]
    assert shown["chats"][0]["title"] == "Стройка: чат" and shown["spheres"] == [{"id": work, "title": "Работа"}]
    assert (await s.page.put(f"/watch/{rule['id']}", {"enabled": False})).json()["rule"]["enabled"] is False

    # неверное — понятный отказ: личный чат, несуществующая сфера, пустые «где»
    personal, _ = await store.ensure_chat(conn, s.acc, ChatRecord("user", 2001, "personal_chat", "Анна Ким"))
    for bad in ({"chat_ids": [personal]}, {"sphere_ids": [999]}):
        assert (await s.page.put(f"/watch/{rule['id']}", bad)).status_code == 400, bad
    assert (await s.page.post("/watch", {"name": "x", "description": "y", "keywords": ["слово"]})).status_code == 400

    chats = (await s.page.get("/watch/chats", params={"q": "строй"})).json()["items"]
    assert [c["id"] for c in chats] == [grp]                                     # только группы и каналы
    assert (await s.page.delete(f"/watch/{rule['id']}")).status_code == 200
    assert await conn.fetchval("SELECT count(*) FROM watch_rules") == 0
    rows = await audit_rows(conn, "watch.create", "watch.update", "watch.delete")
    assert [a for a, _, _ in rows] == ["watch.create", "watch.update", "watch.update", "watch.delete"]
    no_secrets(rows)


async def test_watch_in_a_sphere_sees_its_groups_but_not_personal_chats(stand, conn):
    from shturman.outbox import watcher

    s = await ready(stand, conn)
    grp, other = await _group(conn, s.acc), await _group(conn, s.acc, 3002, "Другое")
    personal, _ = await store.ensure_chat(conn, s.acc, ChatRecord("user", 2001, "personal_chat", "Анна Ким"))
    work = (await spheres.create_sphere(conn, "Работа"))["id"]
    await spheres.add_chats(conn, work, s.acc, [("channel", 3001), ("user", 2001)])
    await s.page.post("/watch", {"name": "Фасады", "description": "ищут подрядчика", "keywords": ["фасад"],
                                 "sphere_ids": [work]})
    asked = []

    async def fake_request(conn_, **kw):
        asked.append(kw["context"]["hit_id"])

    import shturman.bridge as bridge_mod
    original = bridge_mod.request_structured
    bridge_mod.request_structured = fake_request
    try:
        for chat_id, n in ((grp, 1), (other, 2), (personal, 3)):
            mid = await _put(conn, chat_id, n, "Нужен подрядчик на фасад")
            await watcher.on_message(type("M", (), {"state": s.state})(), {"chat_id": chat_id, "message_id": mid,
                                                                           "outgoing": False})
    finally:
        bridge_mod.request_structured = original
    hits = await conn.fetch("SELECT chat_id FROM watch_hits")
    assert [h["chat_id"] for h in hits] == [grp] and len(asked) == 1


async def _put(conn, chat_id, n, text):
    from datetime import datetime, timezone
    record = MessageRecord(tg_message_id=n, sent_at=datetime.now(timezone.utc), kind="message", sender_class="user",
                           sender_tg_id=2002, sender_name="Пётр", text=text, entities=None, reply_to_tg_id=None,
                           forwarded_from=None, edited_at=None, media_type=None, media_path=None, service_action=None)
    result = await store.upsert_messages(conn, [(chat_id, record, False)], source="session", owner_tg_id=OWNER["id"])
    return result.new_ids[0]


async def test_hits_are_shown_without_message_text(stand, conn):
    s = await ready(stand, conn)
    grp = await _group(conn, s.acc)
    rule = (await s.page.post("/watch", {"name": "Фасады", "description": "ищут подрядчика", "keywords": ["фасад"],
                                         "chat_ids": [grp]})).json()["rule"]
    mid = await _put(conn, grp, 15, "Ищем подрядчика на фасад, пишите")
    await conn.execute(
        """INSERT INTO watch_hits (rule_id, message_id, chat_id, content_hash, matched, status, reason, notified, decided_at)
           VALUES ($1, $2, $3, 'h', '{фасад}', 'relevant', 'ищут исполнителя', true, now())""",
        rule["id"], mid, grp)
    items = (await s.page.get("/watch/hits", params={"rule_id": rule["id"]})).json()["items"]
    assert len(items) == 1
    hit = items[0]
    assert (hit["status_text"], hit["matched"], hit["reason"], hit["chat_title"]) == \
        ("важно", ["фасад"], "ищут исполнителя", "Стройка: чат")
    assert "Ищем подрядчика" not in json.dumps(items, ensure_ascii=False) and "text" not in hit


# --- повтор после сбоя ------------------------------------------------------------------------------------

async def test_retry_from_the_page_returns_only_model_failures_and_is_logged(stand, conn):
    s = await ready(stand, conn)
    anna, _ = await store.ensure_chat(conn, s.acc, ChatRecord("user", 2001, "personal_chat", "Анна Ким"))
    await conn.execute(
        """INSERT INTO setup_state (key, value) VALUES ('media', '{"enabled": true}'::jsonb)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""")
    ids = []
    for n, error in enumerate(["model:плагин не ответил", "lost_job", "TimeoutError"], start=1):
        mid = await _put(conn, anna, n, "")
        await conn.execute("UPDATE messages SET media_type = 'photo', media_state = 'failed', media_error = $2, "
                           "media_attempts = 4, media_at = now() WHERE id = $1", mid, error)
        ids.append(mid)
    before = (await s.page.get("/retry")).json()
    assert (before["media"], before["media_not_model"], before["media_enabled"]) == (2, 1, True)
    assert before["last"] is None
    assert (await s.page.post("/retry", {"since": "вчера"})).status_code == 400
    r = await s.page.post("/retry", {})
    assert r.status_code == 200 and (r.json()["media"], r.json()["requests"]) == (2, 0)
    states = {row["id"]: (row["media_state"], row["media_attempts"]) for row in await conn.fetch(
        "SELECT id, media_state, media_attempts FROM messages WHERE id = ANY($1::bigint[])", ids)}
    assert states == {ids[0]: ("pending", 0), ids[1]: ("pending", 0), ids[2]: ("failed", 4)}
    after = (await s.page.get("/retry")).json()
    assert after["media"] == 0 and after["last"]["media"] == 2 and after["last"]["by"] == "page"
    status = (await s.api.get("/api/status")).json()
    assert status["retry_last_media"] == 2 and status["retry_media_failed"] == 0
    rows = await audit_rows(conn, "retry.failed")
    assert rows == [("retry.failed", "ok", "вложений: 2; запросов обработки: 0")]
