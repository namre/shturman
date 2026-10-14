"""Экран «Чаты» приложения «Штурман» (решение владельца Р-80): выбор чатов из Mini App.

Главное, что здесь проверяется:
  * маршруты — только под сессией приложения: без неё, с ключом страницы, с токенами внутреннего API и
    архива, на адресе дашборда — закрыты; чужой аккаунт Telegram в приложение не входит;
  * это то же, что шаг «Какие чаты читать» на странице: поиск, вид, «только выбранные», включение
    и выключение с глубиной истории, исключение и возврат — и применяется сразу;
  * смягчения из Р-80: список диалогов — из памяти, в Telegram — только по «Обновить»; каждое изменение —
    отдельным сообщением владельцу в бот согласований и записью «из приложения» в журнале страницы;
    по одному чату; исключение не стирает уже сохранённое; исключённый чат не включается;
  * «Telegram просит подождать» — ответ 429 с retry_after, как на странице.
"""

import json
from datetime import datetime, timedelta, timezone


from setup_fakes import API, API_HASH, ORIGIN, bind_owner, stand  # noqa: F401, I001 — stand — фикстура; первым: добавляет пути
from conftest import API_AUTH, MCP_AUTH
import exec_fakes  # noqa: E402
from tg_fakes import (C_NEWS, C_SUPER, G_FAMILY, HELPER, IVAN, MARIA, U_BOT, U_IVAN, U_MARIA, U_TELEGRAM,  # noqa: E402
                      World, user, wait_for)
from tg_fakes import msg as tg_msg  # noqa: E402

from shturman.setup_page import miniapp  # noqa: E402
from test_miniapp import MODULES, OWNER, STRANGER, TOKEN, App, signed  # noqa: E402

APP_API = API + "/app"
IVAN_KEY = {"peer_class": "user", "tg_id": IVAN}


def crowded() -> World:
    world = World(HELPER)
    world.authorized = False
    world.dialogs = [U_IVAN, U_MARIA, U_BOT, U_TELEGRAM, G_FAMILY, C_SUPER, C_NEWS]
    world.dialogs += [user(20_000 + i, "Клиент", f"Номер {i}") for i in range(40)]
    world.add(*(tg_msg(i, ("user", IVAN), f"сообщение {i}", sender=IVAN) for i in range(1, 6)))
    return world


async def ready(stand, conn, **changes):
    """Свой бот согласований, привязанный владелец, ключи приложения, подключённый аккаунт-помощник;
    приложение вошло по данным, подписанным для бота согласований."""
    s = await stand(modules=MODULES, bot_token=TOKEN, tg_api_id=1234567, tg_api_hash=API_HASH, world=crowded(),
                    **changes)
    await s.page.login(conn)
    await bind_owner(s)
    started = await s.page.post("/tg/login", {"role": "assistant"})
    assert started.status_code == 200, started.text
    s.world.me = HELPER
    s.world.last.scan.set_result(HELPER)
    login_id = started.json()["login_id"]
    await wait_for(lambda: s.manager.flows[login_id].done)
    s.account = (await s.page.get(f"/tg/login/{login_id}")).json()["account_id"]
    await wait_for(lambda: s.manager.runtimes["assistant"].status == "running")
    s.rt = s.manager.runtimes["assistant"]
    s.app = App(s.api._transport, ORIGIN)
    assert (await s.app.sign_in(signed())).status_code == 200
    return s


def dialog_requests(s) -> int:
    """Сколько раз сервис спрашивал у «Telegram» список диалогов."""
    return sum(1 for c in s.world.clients for r in c.requests if type(r).__name__ == "GetDialogsRequest")


async def app_rows(conn):
    return [(r["action"], r["outcome"], r["detail"]) for r in await conn.fetch(
        "SELECT action, outcome, detail FROM setup_audit WHERE action LIKE 'app.%' AND action NOT LIKE 'app.log%' ORDER BY id")]


async def told(s, words: str) -> list[str]:
    """Сообщения владельцу в бот согласований, в которых есть `words` (ждёт, пока очередь их отправит)."""
    def found():
        return [m["text"] for m in s.telegram.sent() if words in m["text"] and m["chat_id"] == OWNER["id"]]
    await exec_fakes.until(found)
    return found()


async def loaded(s):
    """Список диалогов в памяти сервиса — как после «Обновить список из Telegram»."""
    got = await s.app.get(f"/chats/{s.account}/dialogs", params={"refresh": "1"})
    assert got.status_code == 200 and got.json()["loaded"] is True, got.text
    return got.json()


# --- доступ ----------------------------------------------------------------------------------------

async def test_chat_routes_need_the_app_session_and_nothing_else_opens_them(stand, conn):
    s = await ready(stand, conn)
    account = s.account
    reads = ["/chats", f"/chats/{account}/dialogs", f"/chats/{account}/dialogs?refresh=1"]
    writes = [(f"/chats/{account}/sync", {**IVAN_KEY, "enabled": True, "months": 3}),
              (f"/chats/{account}/exclude", {**IVAN_KEY, "excluded": True})]
    page_key = s.page.key
    for headers in ({}, {"X-Shturman-App": page_key}, {"X-Shturman-Session": page_key}, API_AUTH, MCP_AUTH,
                    {"X-Shturman-App": "x" * 43}):
        for path in reads:
            got = await s.api.get(APP_API + path, headers={"Sec-Fetch-Site": "same-origin", **headers})
            assert got.status_code == 401, (path, headers)
        for path, body in writes:
            got = await s.api.post(APP_API + path, json=body, headers={
                "Origin": ORIGIN, "X-Shturman-Setup": "1", "Sec-Fetch-Site": "same-origin", **headers})
            assert got.status_code == 401, (path, headers)
    # чужой аккаунт Telegram не входит, и ключа у него нет
    stranger = App(s.api._transport, ORIGIN)
    assert (await stranger.sign_in(signed(STRANGER, query_id="stranger"))).status_code == 401
    assert (await stranger.get("/chats")).status_code == 401
    # сессия приложения не открывает те же действия на странице, и наоборот
    assert (await s.api.get(API + f"/tg/accounts/{account}/dialogs",
                            headers={"Sec-Fetch-Site": "same-origin", "X-Shturman-App": s.app.key})).status_code == 401
    # запрос с чужого сайта и с соседнего порта (дашборд) — отказ и с ключом приложения
    for site, origin in (("cross-site", "https://evil.example"), ("same-site", "http://test:8080")):
        got = await s.app.http.post(APP_API + f"/chats/{account}/sync", json={**IVAN_KEY, "enabled": True, "months": 3},
                                    headers={**s.app.headers(), "Origin": origin, "Sec-Fetch-Site": site})
        assert got.status_code == 403
    assert await conn.fetchval("SELECT count(*) FROM tg_sync_chats WHERE enabled") == 0
    assert await app_rows(conn) == []


async def test_chat_routes_are_not_served_on_the_dashboard_address(stand, conn):
    s = await ready(stand, conn, setup_origin="https://assistant.example.com:8443",
                    dashboard_origin="https://assistant.example.com")
    for host in ("assistant.example.com", "assistant.example.com:443"):
        for method, path, body in (("GET", "/chats", None), ("GET", f"/chats/{s.account}/dialogs", None),
                                   ("POST", f"/chats/{s.account}/sync", {**IVAN_KEY, "enabled": True, "months": 3}),
                                   ("POST", f"/chats/{s.account}/exclude", {**IVAN_KEY, "excluded": True})):
            got = await s.app.http.request(method, APP_API + path, json=body,
                                           headers={**s.app.headers(), "Host": host})
            assert got.status_code == 421, (host, path)
    assert await conn.fetchval("SELECT count(*) FROM tg_sync_chats WHERE enabled") == 0


def test_chat_routes_are_app_routes():
    from shturman.setup_page import service as setup_service

    paths = {r.path: r for r in setup_service.routes()}
    for path in ("/chats", "/chats/{account_id:int}/dialogs", "/chats/{account_id:int}/sync",
                 "/chats/{account_id:int}/exclude"):
        route = paths[miniapp.APP_API + path]
        assert getattr(route.endpoint, "miniapp", False), path


# --- список ----------------------------------------------------------------------------------------

async def test_accounts_say_which_one_is_yours_and_whether_it_is_connected(stand, conn):
    s = await ready(stand, conn)
    body = (await s.app.get("/chats")).json()
    (account,) = body["accounts"]
    assert set(account) == {"account_id", "role", "role_name", "label", "status", "backfill_months", "chats_enabled"}
    assert (account["role"], account["status"], account["chats_enabled"]) == ("assistant", "running", 0)
    assert body["mine"] is None
    assert (await s.page.put("/scenario", {"scenario": "staff"})).status_code == 200
    assert (await s.app.get("/chats")).json()["mine"] == "assistant"
    # аккаунт на паузе: список его чатов недоступен, приложение говорит, что он не подключён
    assert (await s.page.post(f"/tg/accounts/{s.account}/pause")).status_code == 200
    assert (await s.app.get("/chats")).json()["accounts"][0]["status"] == "paused"
    got = await s.app.get(f"/chats/{s.account}/dialogs")
    assert got.status_code == 409 and "не подключён" in got.json()["error"]


async def test_no_keys_or_no_account_means_an_empty_screen(stand, conn):
    s = await stand(modules=MODULES, bot_token=TOKEN)
    await s.page.login(conn)
    await bind_owner(s)
    s.app = App(s.api._transport, ORIGIN)
    assert (await s.app.sign_in(signed())).status_code == 200
    assert (await s.app.get("/chats")).json() == {"accounts": [], "mine": None}
    got = await s.app.get("/chats/1/dialogs")
    assert got.status_code == 409 and got.json()["code"] == "no_keys"


async def test_list_comes_from_memory_and_telegram_is_asked_only_by_refresh(stand, conn):
    s = await ready(stand, conn)
    base = f"/chats/{s.account}/dialogs"
    s.rt.dialogs = None                              # в памяти списка нет
    asked = dialog_requests(s)
    empty = (await s.app.get(base)).json()
    assert empty == {"total": 0, "offset": 0, "items": [], "loaded": False, "age": None, "enabled_total": 0}
    assert dialog_requests(s) == asked               # в Telegram не ходили
    first = await loaded(s)
    assert dialog_requests(s) == asked + 1 and first["total"] == 47 and first["age"] == 0
    # список в Telegram поменялся, а срок «свежести» давно вышел — приложение всё равно берёт из памяти
    s.world.dialogs.append(user(30_001, "Новый", "Собеседник"))
    s.rt.dialogs_at -= 100_000
    again = (await s.app.get(base, params={"q": "Новый"})).json()
    assert again["total"] == 0 and again["age"] >= 100_000 and dialog_requests(s) == asked + 1
    # страница настройки в той же ситуации перечитала бы список сама; приложение — только по кнопке
    assert (await s.app.get(base, params={"q": "Новый", "refresh": "1"})).json()["total"] == 1
    assert dialog_requests(s) == asked + 2

    # поиск, вид, «только выбранные», страницы — как на странице
    found = (await s.app.get(base, params={"q": "иван"})).json()
    assert [i["title"] for i in found["items"]] == ["Иван Петров"]
    item = found["items"][0]
    assert set(item) >= {"peer_class", "tg_id", "title", "username", "type", "kind", "enabled", "excluded", "locked"}
    assert (await s.app.get(base, params={"q": "@STROYKA"})).json()["items"][0]["title"] == "Стройка: новости"
    kinds = {k: (await s.app.get(base, params={"kind": k})).json()["total"] for k in ("personal", "group", "channel")}
    assert kinds == {"personal": 45, "group": 2, "channel": 1}
    assert (await s.app.get(base, params={"kind": "всё"})).status_code == 400
    page = (await s.app.get(base, params={"offset": "40", "limit": "20"})).json()
    assert len(page["items"]) == 8 and page["total"] == 48
    telegram = (await s.app.get(base, params={"q": "Telegram"})).json()["items"][0]
    assert telegram["locked"] is True and telegram["excluded"] is True
    assert (await s.app.get(base, params={"only": "enabled"})).json()["total"] == 0
    # список — не изменение: ни журнала, ни сообщений владельцу
    assert await app_rows(conn) == []


async def test_telegram_asks_to_wait_and_the_app_gets_retry_after(stand, conn):
    s = await ready(stand, conn)
    await loaded(s)
    s.rt.dialogs_at -= 100
    s.rt.pacer.flood(40)
    got = await s.app.get(f"/chats/{s.account}/dialogs", params={"refresh": "1"})
    assert got.status_code == 429
    assert "подождать" in got.json()["error"] and 40 <= got.json()["retry_after"] <= 43
    # пока Telegram просит ждать, уже загруженный список по-прежнему открывается и чаты включаются
    assert (await s.app.get(f"/chats/{s.account}/dialogs")).json()["total"] == 47
    on = await s.app.post(f"/chats/{s.account}/sync", {**IVAN_KEY, "enabled": True, "months": 1})
    assert on.status_code == 200 and on.json()["enabled"] == 1


# --- включение и выключение ----------------------------------------------------------------------

async def test_enable_and_disable_with_depth_are_told_to_the_owner_and_logged(stand, conn):
    s = await ready(stand, conn)
    await loaded(s)
    url = f"/chats/{s.account}/sync"
    before = datetime.now(timezone.utc)
    on = await s.app.post(url, {**IVAN_KEY, "enabled": True, "months": 3})
    assert on.status_code == 200, on.text
    assert on.json() == {"chats": [{**IVAN_KEY, "enabled": True}], "enabled": 1, "enabled_total": 1}
    since = await conn.fetchval("SELECT backfill_since FROM tg_sync_chats WHERE tg_id = $1", IVAN)
    assert before - timedelta(days=93) <= since <= before - timedelta(days=88)
    await exec_fakes.until(lambda: conn.fetchval("SELECT count(*) = 5 FROM messages"))       # история догружена
    (text,) = await told(s, "Из приложения включено чтение чата")
    assert text.startswith("Из приложения включено чтение чата «Иван Петров» (глубина: 3 месяца).")
    assert "Выйти на всех устройствах" in text
    # «вся история» и глубина по умолчанию из настройки аккаунта — те же варианты, что на странице
    maria = {"peer_class": "user", "tg_id": MARIA}
    assert (await s.app.post(url, {**maria, "enabled": True, "months": None})).json()["enabled"] == 1
    assert await conn.fetchval("SELECT backfill_since FROM tg_sync_chats WHERE tg_id = $1", MARIA) is None
    await told(s, "«Мария» (глубина: вся история)")
    assert (await s.app.get(f"/chats/{s.account}/dialogs", params={"only": "enabled"})).json()["total"] == 2

    off = await s.app.post(url, {**IVAN_KEY, "enabled": False})
    assert off.json() == {"chats": [{**IVAN_KEY, "enabled": False}], "enabled": 0, "enabled_total": 1}
    assert await conn.fetchval("SELECT enabled FROM tg_sync_chats WHERE tg_id = $1", IVAN) is False
    assert await conn.fetchval("SELECT count(*) FROM messages") == 5                    # сохранённое на месте
    (text,) = await told(s, "Из приложения выключено чтение чата")
    assert text == "Из приложения выключено чтение чата «Иван Петров»."

    assert await app_rows(conn) == [
        ("app.sync_on", "ok", f"чат user:{IVAN}; глубина: 3 месяца"),
        ("app.sync_on", "ok", f"чат user:{MARIA}; глубина: вся история"),
        ("app.sync_off", "ok", f"чат user:{IVAN}")]
    dumped = json.dumps(await app_rows(conn), ensure_ascii=False)
    assert "Иван" not in dumped and "Мария" not in dumped
    # в журнале на странице — отдельной строкой «из приложения», среди важных
    overview = (await s.page.get("/overview")).json()
    key_titles = [r["title"] for r in overview["audit_key"]]
    assert key_titles[:3] == ["Из приложения в Telegram: выключено чтение чата",
                              "Из приложения в Telegram: включено чтение чата",
                              "Из приложения в Telegram: включено чтение чата"]
    # из приложения — без карточки подтверждения: владелец удостоверен Telegram
    assert await conn.fetchval("SELECT count(*) FROM pending_actions") == 0


async def test_one_chat_at_a_time_with_a_chosen_depth(stand, conn):
    s = await ready(stand, conn)
    await loaded(s)
    url = f"/chats/{s.account}/sync"
    bad = [({**IVAN_KEY, "enabled": True}, "глубину"),                                 # глубина не выбрана
           ({**IVAN_KEY, "enabled": True, "months": 0}, "Глубина"),
           ({**IVAN_KEY, "enabled": True, "months": 601}, "Глубина"),
           ({**IVAN_KEY, "enabled": True, "months": "12"}, "Глубина"),
           ({**IVAN_KEY, "enabled": True, "months": True}, "Глубина"),
           ({"enabled": True, "kind": "group", "months": 3}, "по одному"),               # все группы разом — только на странице
           ({"enabled": True, "chats": [IVAN_KEY], "months": 3}, "по одному"),
           ({"peer_class": "x", "tg_id": 1, "enabled": True, "months": 3}, "peer_class"),
           ({**IVAN_KEY, "enabled": "да", "months": 3}, "enabled")]
    for body, words in bad:
        got = await s.app.post(url, body)
        assert got.status_code == 400 and words in got.json()["error"], (body, got.text)
    # чат, которого нет среди загруженных диалогов, — не найден, в Telegram за ним не ходим
    asked = dialog_requests(s)
    missing = await s.app.post(url, {"peer_class": "user", "tg_id": 123456, "enabled": True, "months": 3})
    assert missing.status_code == 404 and dialog_requests(s) == asked
    # списка в памяти нет — включить нельзя, пока его не обновят
    s.rt.dialogs = None
    gone = await s.app.post(url, {**IVAN_KEY, "enabled": True, "months": 3})
    assert gone.status_code == 409 and "Обновить список из Telegram" in gone.json()["error"]
    assert dialog_requests(s) == asked
    assert await conn.fetchval("SELECT count(*) FROM tg_sync_chats WHERE enabled") == 0
    assert await app_rows(conn) == []


# --- исключение -------------------------------------------------------------------------------------

async def test_exclusion_keeps_saved_messages_and_an_excluded_chat_is_not_enabled(stand, conn):
    s = await ready(stand, conn)
    await loaded(s)
    assert (await s.page.post(f"/tg/accounts/{s.account}/sync", {"enabled": True, "chats": [IVAN_KEY]})).status_code == 200
    await exec_fakes.until(lambda: conn.fetchval("SELECT count(*) = 5 FROM messages"))
    url = f"/chats/{s.account}/exclude"
    # стереть сохранённое отсюда нельзя — только на странице
    for purge in (True, "да", 1):
        refused = await s.app.post(url, {**IVAN_KEY, "excluded": True, "purge": purge})
        assert refused.status_code == 400 and refused.json()["code"] == "page_only"
    assert await conn.fetchval("SELECT excluded FROM chats WHERE title = 'Иван Петров'") is False

    out = await s.app.post(url, {**IVAN_KEY, "excluded": True})
    assert out.status_code == 200 and out.json() == {"excluded": True, "purged": 0}
    assert await conn.fetchval("SELECT excluded FROM chats WHERE title = 'Иван Петров'") is True
    await exec_fakes.until(lambda: conn.fetchval("SELECT NOT enabled FROM tg_sync_chats WHERE tg_id = $1", IVAN))
    assert await conn.fetchval("SELECT count(*) FROM messages") == 5                    # сообщения остались
    (text,) = await told(s, "исключён из архива")
    assert text.startswith("Из приложения чат «Иван Петров» исключён из архива") and "уже сохранённое осталось" in text
    row = (await s.app.get(f"/chats/{s.account}/dialogs", params={"q": "Иван"})).json()["items"][0]
    assert row["excluded"] is True and row["enabled"] is False

    # исключённый чат из приложения не включается; изменения нет — и сообщения владельцу нет
    sent_before = len(s.telegram.sent())
    on = await s.app.post(f"/chats/{s.account}/sync", {**IVAN_KEY, "enabled": True, "months": 3})
    assert on.status_code == 200 and on.json()["enabled"] == 0
    assert on.json()["chats"][0]["excluded"] is True and "исключён" in on.json()["chats"][0]["error"]
    assert await conn.fetchval("SELECT enabled FROM tg_sync_chats WHERE tg_id = $1", IVAN) is False

    back = await s.app.post(url, {**IVAN_KEY, "excluded": False})
    assert back.json() == {"excluded": False, "purged": 0}
    assert await conn.fetchval("SELECT excluded FROM chats WHERE title = 'Иван Петров'") is False
    await told(s, "возвращён в архив")
    assert len(s.telegram.sent()) == sent_before + 1           # только о возврате; об отказе — ничего
    # служебный чат Telegram вернуть нельзя; незнакомый чат — не найден
    assert (await s.app.post(url, {"peer_class": "user", "tg_id": 777000, "excluded": False})).json()["code"] == "locked"
    assert (await s.app.post(url, {"peer_class": "user", "tg_id": 123456, "excluded": True})).status_code == 404
    assert await app_rows(conn) == [
        ("app.chat_exclude", "ok", f"чат user:{IVAN}"),
        ("app.sync_on", "refused", f"чат user:{IVAN}; чат исключён из архива"),
        ("app.chat_include", "ok", f"чат user:{IVAN}")]
    assert await conn.fetchval("SELECT count(*) FROM messages") == 5


async def test_long_and_odd_chat_titles_are_shortened_in_the_message_to_the_owner(stand, conn):
    s = await ready(stand, conn)
    long_name = "Очень " * 30 + "длинное название"
    s.world.dialogs.append(user(30_002, long_name))
    await loaded(s)
    on = await s.app.post(f"/chats/{s.account}/sync",
                          {"peer_class": "user", "tg_id": 30_002, "enabled": True, "months": 12})
    assert on.status_code == 200 and on.json()["enabled"] == 1
    (text,) = await told(s, "Из приложения включено чтение чата")
    title = text.split("«", 1)[1].split("»", 1)[0]
    assert len(title) <= miniapp.TITLE_LIMIT and title.endswith("…")
    assert "(глубина: 1 год)" in text


def test_depth_words():
    from shturman.setup_page import chats

    assert [chats.depth_words(m) for m in (1, 3, 6, 12, 24, 60, None, 36, 21, 11)] == [
        "1 месяц", "3 месяца", "6 месяцев", "1 год", "2 года", "5 лет", "вся история", "3 года", "21 месяц",
        "11 месяцев"]


def test_audit_titles_say_from_the_app():
    from shturman.setup_page import audit

    for action in ("app.sync_on", "app.sync_off", "app.chat_exclude", "app.chat_include"):
        assert audit.ACTIONS[action].startswith("Из приложения") and action in audit.IMPORTANT
