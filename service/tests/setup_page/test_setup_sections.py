"""Разделы страницы настройки на уровне маршрутов: бот, ключи, аккаунты, чаты, выгрузка,
бизнес-режим, своя модель, «что собрано», журнал действий.

Главное, что здесь проверяется помимо самих действий: со страницы они применяются сразу, без
карточки в боте согласований, — в том числе когда свой бот есть и тот же запрос через
внутренний API ждал бы подтверждения."""

import json
import logging
import time

from setup_fakes import (  # noqa: F401, I001 — stand — фикстура; первым: добавляет пути
    API, API_HASH, BUSY_TOKEN, LLM_KEY, TOKEN, bind_owner, save_bot, stand)
from conftest import as_file, full_export, msg
from exec_fakes import BOT_NAME, IVAN_USER, OWNER, OWNER_USER, STRANGER_USER, private, refusal, until
from telethon import errors
from tg_fakes import (C_NEWS, C_SUPER, G_FAMILY, HELPER, IVAN, ME, PASSWORD, U_BOT, U_IVAN, U_MARIA, U_TELEGRAM,
                      World, user, wait_for)
from tg_fakes import msg as tg_msg

from shturman import bridge
from shturman.setup_page import audit
from shturman.setup_page import secrets_store as ss

KEYS = {"api_id": "1234567", "api_hash": API_HASH}


async def ready(stand, conn, **changes):
    """Вошедший владелец со своим ботом, привязкой и ключами приложения."""
    s = await stand(**changes)
    await s.page.login(conn)
    assert (await save_bot(s)).status_code == 200
    await bind_owner(s)
    assert (await s.page.put("/tg/keys", KEYS)).status_code == 200
    return s


async def audit_rows(conn) -> list[tuple[str, str, str]]:
    return [(r["action"], r["outcome"], r["detail"]) for r in
            await conn.fetch("SELECT action, outcome, detail FROM setup_audit ORDER BY id")]


async def connect(s, role="assistant", world_user=HELPER, **extra):
    started = await s.page.post("/tg/login", {"role": role, **extra})
    assert started.status_code == 200, started.text
    s.world.me = world_user
    s.world.last.scan.set_result(world_user)
    login_id = started.json()["login_id"]
    await wait_for(lambda: s.manager.flows[login_id].done)
    done = (await s.page.get(f"/tg/login/{login_id}")).json()
    assert done["status"] == "completed", done
    await wait_for(lambda: s.manager.runtimes[role].status == "running")
    return done["account_id"]


# --- 1. бот согласований ---

async def test_bot_token_is_checked_confirmed_and_applied_without_restart(stand, conn):
    s = await stand()
    await s.page.login(conn)
    assert bridge.owns_bot() is False and (await s.page.get("/state")).json()["bot"]["configured"] is False
    # до своего бота плагин может сообщить владельца по внутреннему API
    assert (await s.api.put("/api/owner", json={"user_id": STRANGER_USER["id"], "chat_id": STRANGER_USER["id"]})).status_code == 200

    asked = await s.page.post("/bot/token", {"token": TOKEN})
    assert asked.json() == {"status": "confirm", "bot": {"username": BOT_NAME, "name": "Согласования"}}
    assert s.state.config.bot_token == "" and bridge.owns_bot() is False          # первый шаг ничего не сохраняет
    assert not ss.SecretStore(s.config.data_dir).path.exists()

    saved = await s.page.post("/bot/token", {"token": TOKEN, "separate": True})
    assert saved.json()["status"] == "saved"
    assert bridge.owns_bot() is True and s.state.config.own_bot is True
    await until(lambda: s.state.extras["executor"].bot.polling)
    state = (await s.page.get("/state")).json()["bot"]
    assert state["configured"] and state["username"] == BOT_NAME and state["polling"] is True
    assert state["problem"] is None and state["owner_bound"] is False       # запись плагина владельцем не считается
    # с этого мгновения внутренний API не принимает ни владельца, ни нажатий
    for response in (await s.api.put("/api/owner", json={"user_id": 1, "chat_id": 1}),
                     await s.api.post("/api/callbacks/telegram", json={"data": "sh:cf:y:1:x", "from_user_id": OWNER})):
        assert response.status_code == 403 and response.json()["code"] == "own_bot"
    assert (await s.api.get("/api/status")).json()["own_bot"] is True
    assert ("bot.token", "ok", "бот проверен запросом к Telegram") in await audit_rows(conn)


async def test_wrong_tokens_are_explained_and_not_saved(stand, conn):
    s = await stand()
    await s.page.login(conn)
    cases = [("", "empty", "Вставьте токен"), ("просто текст", "bad_token_format", "не похоже на токен"),
             ("7000000009:UNKNOWN-token-000000000000000000000", "token_rejected", "не принял этот токен")]
    for token, code, words in cases:
        got = await s.page.post("/bot/token", {"token": token, "separate": True})
        assert got.status_code == 422 and got.json()["code"] == code and words in got.json()["error"], got.text
        assert token not in got.text or not token
    s.telegram.script["getMe"] = [OSError("сеть"), refusal(429, "Too Many Requests", retry_after=5)]
    assert (await s.page.post("/bot/token", {"token": TOKEN})).json()["code"] == "no_connection"
    assert (await s.page.post("/bot/token", {"token": TOKEN})).json()["code"] == "too_many_requests"
    assert bridge.owns_bot() is False and not ss.SecretStore(s.config.data_dir).path.exists()


async def test_token_of_a_bot_that_is_already_polled_is_refused(stand, conn):
    """Токен бота, с которым владелец разговаривает с ассистентом: узнать его можно только по 409."""
    s = await stand()
    await s.page.login(conn)
    asked = await s.page.post("/bot/token", {"token": BUSY_TOKEN})
    assert asked.json()["status"] == "confirm" and asked.json()["bot"]["username"] == "ivan_assistant_bot"
    refused = await s.page.post("/bot/token", {"token": BUSY_TOKEN, "separate": True})
    assert refused.status_code == 422 and refused.json()["code"] == "other_poller"
    assert "другая программа" in refused.json()["error"] and "нового бота" in refused.json()["error"]
    probes = [name for name, _ in s.telegram.requests if name.startswith("busy:")]
    assert probes == ["busy:getMe", "busy:getMe", "busy:getUpdates"]
    assert bridge.owns_bot() is False and s.state.config.bot_token == ""
    assert ("bot.token", "refused", "ботом уже пользуется другая программа") in await audit_rows(conn)
    # webhook у бота — та же беда, но слова другие
    s.telegram.script["getUpdates"] = [refusal(409, "Conflict: can't use getUpdates method while webhook is active")]
    hooked = await s.page.post("/bot/token", {"token": TOKEN, "separate": True})
    assert hooked.json()["code"] == "webhook" and "webhook" in hooked.json()["error"]
    # если чужой опрос проявился уже после сохранения, страница объясняет это в состоянии бота
    assert (await save_bot(s)).status_code == 200
    bot = s.state.extras["executor"].bot
    await until(lambda: bot.polling)
    s.telegram.script["getUpdates"] = [refusal(409, "Conflict: terminated by other getUpdates request")] * 3
    await until(lambda: bot.problem == "other_poller")          # дальше опрос ждёт несколько секунд перед повтором
    state = (await s.page.get("/state")).json()["bot"]
    assert state["problem"] == "other_poller" and "другая программа" in state["problem_text"]


async def test_probe_does_not_confirm_updates_or_change_their_list(stand, conn):
    s = await stand()
    await s.page.login(conn)
    s.telegram.text("сообщение, которое ждёт своего бота")
    before = list(s.telegram.updates)
    await s.page.post("/bot/token", {"token": TOKEN})
    assert s.telegram.calls("getUpdates") == []                       # первый шаг — только getMe
    await s.page.post("/bot/token", {"token": TOKEN, "separate": True})
    probe = s.telegram.calls("getUpdates")[0]
    assert "offset" not in probe and "allowed_updates" not in probe and probe["limit"] == 1
    assert before[0] in s.telegram.updates or s.telegram.calls("getUpdates")[1:]   # обновление пробой не съедено


async def test_owner_is_bound_by_the_link_from_the_page(stand, conn):
    s = await stand()
    await s.page.login(conn)
    assert (await s.page.post("/bot/bind")).json()["code"] == "no_bot"
    await save_bot(s)
    await until(lambda: s.state.extras["executor"].bot.identity)
    issued = (await s.page.post("/bot/bind")).json()
    assert issued["link"].startswith(f"https://t.me/{BOT_NAME}?start=") and issued["minutes"] == 15
    assert issued["rebind"] is False
    code = issued["link"].split("start=")[1]
    assert code not in json.dumps(await audit_rows(conn), ensure_ascii=False)
    assert code not in (await conn.fetchval("SELECT string_agg(t::text, ' ') FROM executor_bind_codes t"))
    # новая ссылка отменяет прежнюю
    second = (await s.page.post("/bot/bind")).json()["link"].split("start=")[1]
    s.telegram.text(f"/start {code}", user=OWNER_USER)
    s.telegram.text(f"/start {second}", user=OWNER_USER)
    await until(lambda: bridge.get_owner(conn))
    state = (await s.page.get("/state")).json()["bot"]
    assert state["owner_bound"] is True and state["owner_name"] == "Евгений Тестов" and state["owner_bound_at"]
    assert (await bridge.get_owner(conn))["user_id"] == OWNER
    assert (await s.page.post("/bot/bind")).json()["rebind"] is True
    # имя владельца через внутренний API не видно
    assert "Евгений" not in (await s.api.get("/api/executor/status")).text


async def test_removing_the_token_stops_the_bot(stand, conn):
    s = await ready(stand, conn)
    assert bridge.owns_bot() is True
    assert (await s.page.delete("/bot/token")).status_code == 200
    assert bridge.owns_bot() is False and s.state.config.bot_token == ""
    assert s.state.extras["executor"].bot is None
    assert not [t for t in s.state._tasks if t.get_name().startswith("executor-")]
    state = (await s.page.get("/state")).json()["bot"]
    assert state["configured"] is False and state["owner_bound"] is False
    assert not ss.SecretStore(s.config.data_dir).has(ss.BOT_TOKEN)
    assert (await s.browser().post("/login/code/request")).json() == {"result": "no_owner"}


# --- 2. ключи приложения ---

async def test_tg_keys_are_validated_saved_and_applied(stand, conn):
    s = await stand()
    await s.page.login(conn)
    assert s.manager.configured is False
    assert (await s.page.post("/tg/login", {"role": "owner", "confirm_owner": True})).json()["code"] == "no_keys"
    for body, code in (({"api_id": "12ab", "api_hash": API_HASH}, "bad_api_id"), ({"api_id": "", "api_hash": API_HASH}, "bad_api_id"),
                       ({"api_id": "123", "api_hash": "короткий"}, "bad_api_hash"), ({"api_id": 0, "api_hash": API_HASH}, "bad_api_id"),
                       ({"api_id": True, "api_hash": API_HASH}, "bad_api_id")):
        got = await s.page.put("/tg/keys", body)
        assert got.status_code == 422 and got.json()["code"] == code
    assert (await s.page.put("/tg/keys", {"api_id": " 1234567 ", "api_hash": API_HASH.upper()})).status_code == 200
    assert s.manager.configured is True and (s.manager.config.tg_api_id, s.manager.config.tg_api_hash) == (1234567, API_HASH)
    assert s.state.config is s.manager.config
    assert (await s.api.get("/api/tg/accounts")).status_code == 200            # и внутренний API видит модуль настроенным
    assert (await s.page.delete("/tg/keys")).status_code == 200
    assert s.manager.configured is False


async def test_keys_cannot_change_under_connected_accounts(stand, conn):
    s = await ready(stand, conn)
    await connect(s)
    for response in (await s.page.put("/tg/keys", {"api_id": "7654321", "api_hash": "f" * 32}), await s.page.delete("/tg/keys")):
        assert response.status_code == 422 and response.json()["code"] == "accounts_connected"
    assert s.manager.config.tg_api_id == 1234567 and ss.SecretStore(s.config.data_dir).get(ss.TG_API_ID) == "1234567"
    # те же ключи заново — не смена: сохранять можно
    assert (await s.page.put("/tg/keys", KEYS)).status_code == 200


async def test_real_client_factory_reads_keys_entered_after_start(stand, conn):
    from shturman.tg.client import NotConfigured, RequestPolicy
    from shturman.tg.manager import TgManager

    s = await stand()
    manager = TgManager(s.state.config, s.state.pool, s.state.events)
    path = s.config.data_dir / "sessions" / "probe.session"
    try:
        manager.client_factory("owner", path, RequestPolicy("owner"), None)
        raise AssertionError("без ключей клиент создаваться не должен")
    except NotConfigured:
        pass
    await manager.reconfigure(__import__("dataclasses").replace(s.state.config, tg_api_id=1234567, tg_api_hash=API_HASH))
    client = manager.client_factory("owner", path, RequestPolicy("owner"), None)
    assert client.api_id == 1234567
    client.session.close()


# --- 3. аккаунты ---

async def test_account_login_from_the_page_needs_no_card_in_the_bot(stand, conn, caplog):
    caplog.set_level(logging.DEBUG)
    world = World(HELPER)
    world.authorized, world.password = False, PASSWORD
    s = await ready(stand, conn, world=world)
    # тот же запрос через внутренний API при своём боте ждёт владельца…
    via_api = await s.api.post("/api/tg/login", json={"role": "assistant"})
    assert via_api.status_code == 202 and via_api.json()["status"] == "pending_confirmation"
    assert world.clients == []
    # …а со страницы вход начинается сразу
    started = await s.page.post("/tg/login", {"role": "assistant"})
    assert started.status_code == 200
    flow = started.json()
    assert flow["status"] == "pending" and flow["link"].startswith("tg://login?token=") and flow["expires_at"]
    assert "qr_svg" not in flow                                   # QR рисует браузер, готовой картинки нет
    url = f"/tg/login/{flow['login_id']}"
    assert (await s.page.get("/state")).json()["tg"]["logins"] == [{"login_id": flow["login_id"], "role": "assistant"}]

    world.last.scan.set_exception(errors.SessionPasswordNeededError(None))
    await wait_for(lambda: s.manager.flows[flow["login_id"]].status == "password_required")
    waiting = (await s.page.get(url)).json()
    assert waiting["status"] == "password_required" and waiting["hint"] == "кличка кота" and "link" not in waiting
    assert (await s.page.post(url + "/password", {})).status_code == 400
    wrong = (await s.page.post(url + "/password", {"password": "не тот"})).json()
    assert wrong["status"] == "password_required" and wrong["attempts_left"] == 2 and wrong["error"]
    done = await s.page.post(url + "/password", {"password": PASSWORD})
    assert done.json()["status"] == "completed"
    await wait_for(lambda: s.manager.runtimes["assistant"].status == "running")

    account = (await s.page.get("/state")).json()["tg"]["accounts"][0]
    assert (account["role"], account["status"], account["label"], account["role_name"]) == \
           ("assistant", "running", "Помощник", "аккаунт-помощник")
    assert account["can_send"] is False                           # отправка выключена
    rows = await audit_rows(conn)
    assert ("tg.login", "ok", "роль: аккаунт-помощник") in rows and ("tg.login_done", "ok", "роль: аккаунт-помощник") in rows
    assert [a for a, _, _ in rows].count("tg.login_done") == 1
    everything = caplog.text + json.dumps(rows, ensure_ascii=False) + done.text + (await s.page.get("/state")).text
    assert PASSWORD not in everything and "QRTOKEN" not in caplog.text and "QRTOKEN" not in json.dumps(rows)
    assert await conn.fetchval("SELECT count(*) FROM pending_actions WHERE status = 'applied'") == 0


async def test_roles_are_guarded_the_same_way_as_in_the_terminal(stand, conn):
    world = World(ME)
    world.authorized = False
    s = await stand(world=world)
    await s.page.login(conn)
    await s.page.put("/tg/keys", KEYS)
    # помощник — только когда сервис знает владельца
    early = await s.page.post("/tg/login", {"role": "assistant"})
    assert early.status_code == 409 and early.json()["code"] == "owner_unknown" and "шаг 2" in early.json()["error"]
    assert (await s.page.post("/tg/login", {"role": "admin"})).status_code == 400
    # основной аккаунт — только с явным согласием
    assert (await s.page.post("/tg/login", {"role": "owner"})).status_code == 400
    assert world.clients == []
    await save_bot(s)
    await bind_owner(s)
    # аккаунт владельца помощником не становится
    started = (await s.page.post("/tg/login", {"role": "assistant"})).json()
    world.last.scan.set_result(ME)
    await wait_for(lambda: s.manager.flows[started["login_id"]].done)
    failed = (await s.page.get(f"/tg/login/{started['login_id']}")).json()
    assert failed["status"] == "failed" and "основной аккаунт владельца" in failed["error"]
    assert (await s.page.get("/state")).json()["tg"]["accounts"] == []
    # как основной — подключается, и отправлять не может при любом выключателе
    account_id = await connect(s, "owner", ME, confirm_owner=True)
    account = (await s.page.get("/state")).json()["tg"]["accounts"][0]
    assert (account["role"], account["can_send"], account["account_id"]) == ("owner", False, account_id)


async def test_pause_resume_cancel_and_logout(stand, conn):
    world = World(HELPER)
    world.authorized = False
    s = await ready(stand, conn, world=world)
    started = (await s.page.post("/tg/login", {"role": "assistant"})).json()
    cancelled = await s.page.post(f"/tg/login/{started['login_id']}/cancel")
    assert cancelled.json()["status"] == "cancelled" and "link" not in cancelled.json()
    assert (await s.page.get("/tg/login/нет-такого")).status_code == 404
    account_id = await connect(s)
    assert (await s.page.post(f"/tg/accounts/{account_id}/pause")).status_code == 200
    assert (await s.page.get("/state")).json()["tg"]["accounts"][0]["status"] == "paused"
    # снятие с паузы через внутренний API ждёт владельца, со страницы — применяется сразу
    assert (await s.api.post(f"/api/tg/accounts/{account_id}/resume")).status_code == 202
    world.authorized = True          # сессия на диске — уже вошедшая
    assert (await s.page.post(f"/tg/accounts/{account_id}/resume")).status_code == 200
    await wait_for(lambda: s.manager.runtimes["assistant"].status == "running")
    out = await s.page.post(f"/tg/accounts/{account_id}/logout")
    assert out.json() == {"ok": True, "terminated": True}
    assert (await s.page.get("/state")).json()["tg"]["accounts"] == []
    assert (await s.page.post("/tg/accounts/999/pause")).status_code == 404
    actions = [a for a, _, _ in await audit_rows(conn)]
    for action in ("tg.login_cancel", "tg.pause", "tg.resume", "tg.logout"):
        assert action in actions


# --- 4. чаты ---

def crowded() -> World:
    world = World(HELPER)
    world.authorized = False
    world.dialogs = [U_IVAN, U_MARIA, U_BOT, U_TELEGRAM, G_FAMILY, C_SUPER, C_NEWS]
    world.dialogs += [user(20_000 + i, "Клиент", f"Номер {i}") for i in range(130)]
    world.add(*(tg_msg(i, ("user", IVAN), f"сообщение {i}", sender=IVAN) for i in range(1, 6)))
    return world


async def test_dialogs_are_paged_searched_and_filtered(stand, conn):
    s = await ready(stand, conn, world=crowded())
    account_id = await connect(s)
    base = f"/tg/accounts/{account_id}/dialogs"
    first = (await s.page.get(base)).json()
    assert first["total"] == 137 and len(first["items"]) == 50 and first["enabled_total"] == 0
    assert set(first["items"][0]) >= {"peer_class", "tg_id", "title", "type", "kind", "enabled", "excluded", "locked"}
    last = (await s.page.get(base + "?offset=100&limit=50")).json()
    assert len(last["items"]) == 37
    assert (await s.page.get(base + "?limit=9999")).json()["items"].__len__() == 137      # предел — 200 на страницу
    found = (await s.page.get(base, params={"q": "иван"})).json()
    assert [i["title"] for i in found["items"]] == ["Иван Петров"] and found["total"] == 1
    assert (await s.page.get(base, params={"q": "@STROYKA"})).json()["items"][0]["title"] == "Стройка: новости"
    assert (await s.page.get(base, params={"q": "а/б%_"})).json()["total"] == 0           # знаки поиска — просто знаки
    kinds = {k: (await s.page.get(base, params={"kind": k})).json() for k in ("personal", "group", "channel")}
    assert (kinds["personal"]["total"], kinds["group"]["total"], kinds["channel"]["total"]) == (134, 2, 1)
    assert {i["kind"] for i in kinds["group"]["items"]} == {"group"}
    assert (await s.page.get(base, params={"kind": "прочее"})).status_code == 400
    telegram = next(i for i in kinds["personal"]["items"] if i["title"] == "Telegram")
    assert telegram["locked"] is True and telegram["excluded"] is True


async def test_chats_are_enabled_at_once_and_counted(stand, conn):
    s = await ready(stand, conn, world=crowded())
    account_id = await connect(s)
    ivan = {"peer_class": "user", "tg_id": IVAN}
    # через внутренний API включение чата ждёт владельца…
    assert (await s.api.post(f"/api/tg/accounts/{account_id}/sync", json={"enabled": True, "chats": [ivan]})).status_code == 202
    assert await conn.fetchval("SELECT count(*) FROM tg_sync_chats WHERE enabled") == 0
    # …со страницы — сразу
    on = await s.page.post(f"/tg/accounts/{account_id}/sync", {"enabled": True, "chats": [ivan]})
    assert on.status_code == 200 and on.json() == {"chats": [{**ivan, "enabled": True}], "enabled": 1}
    await until(lambda: conn.fetchval("SELECT count(*) = 5 FROM messages"))               # история догружена
    listed = (await s.page.get(f"/tg/accounts/{account_id}/dialogs", params={"only": "enabled"})).json()
    assert [i["title"] for i in listed["items"]] == ["Иван Петров"] and listed["enabled_total"] == 1
    account = (await s.page.get("/state")).json()["tg"]["accounts"][0]
    assert account["chats_enabled"] == 1
    # все чаты одного вида разом; служебный чат Telegram при этом не включается
    bulk = (await s.page.post(f"/tg/accounts/{account_id}/sync", {"enabled": True, "kind": "group"})).json()
    assert bulk["enabled"] == 2
    everyone = (await s.page.post(f"/tg/accounts/{account_id}/sync", {"enabled": True, "kind": "personal"})).json()
    blocked = [c for c in everyone["chats"] if c["tg_id"] == 777000][0]
    assert blocked["enabled"] is False and everyone["enabled"] == 133
    off = await s.page.post(f"/tg/accounts/{account_id}/sync", {"enabled": False, "chats": [ivan]})
    assert off.json()["chats"] == [{**ivan, "enabled": False}]
    for body in ({}, {"enabled": True}, {"enabled": "да", "chats": [ivan]}, {"enabled": True, "chats": [{"peer_class": "x", "tg_id": 1}]},
                 {"enabled": True, "kind": "все"}):
        assert (await s.page.post(f"/tg/accounts/{account_id}/sync", body)).status_code == 400
    details = [d for a, _, d in await audit_rows(conn) if a in ("tg.sync_on", "tg.sync_off")]
    assert details[0] == "чатов: 1 из 1" and "Иван" not in json.dumps(details, ensure_ascii=False)
    assert await conn.fetchval("SELECT count(*) FROM pending_actions WHERE status = 'applied'") == 0


async def test_options_depth_and_auto_take_effect_at_once(stand, conn):
    s = await ready(stand, conn, world=crowded())
    account_id = await connect(s)
    url = f"/tg/accounts/{account_id}/options"
    assert (await s.api.put(f"/api{url}", json={"auto_personal": True})).status_code == 202   # через API — ждёт
    assert (await s.page.put(url, {"auto_personal": True, "backfill_months": 36})).status_code == 200
    account = (await s.page.get("/state")).json()["tg"]["accounts"][0]
    assert (account["auto_personal"], account["auto_groups"], account["backfill_months"]) == (True, False, 36)
    assert (await s.page.put(url, {"backfill_months": None, "auto_personal": False})).status_code == 200
    account = (await s.page.get("/state")).json()["tg"]["accounts"][0]
    assert (account["auto_personal"], account["backfill_months"]) == (False, None)
    for body in ({"backfill_months": 0}, {"backfill_months": "12"}, {"auto_groups": "да"}, {"backfill_months": True}):
        assert (await s.page.put(url, body)).status_code == 400
    assert "глубина истории: 36 мес." in [d for a, _, d in await audit_rows(conn) if a == "tg.options"][0]


async def test_chat_is_excluded_purged_and_returned_from_the_page(stand, conn):
    s = await ready(stand, conn, world=crowded())
    account_id = await connect(s)
    ivan = {"peer_class": "user", "tg_id": IVAN}
    await s.page.post(f"/tg/accounts/{account_id}/sync", {"enabled": True, "chats": [ivan]})
    await until(lambda: conn.fetchval("SELECT count(*) = 5 FROM messages"))
    url = f"/tg/accounts/{account_id}/exclude"
    out = await s.page.post(url, {**ivan, "excluded": True})
    assert out.json() == {"excluded": True, "purged": 0}
    assert await conn.fetchval("SELECT excluded FROM chats") is True
    await until(lambda: conn.fetchval("SELECT NOT enabled FROM tg_sync_chats WHERE tg_id = $1", IVAN))   # чтение выключено
    assert await conn.fetchval("SELECT count(*) FROM messages") == 5                       # сохранённое пока на месте
    row = (await s.page.get(f"/tg/accounts/{account_id}/dialogs", params={"q": "Иван"})).json()["items"][0]
    assert row["excluded"] is True and row["enabled"] is False
    assert (await s.page.post(f"/tg/accounts/{account_id}/sync", {"enabled": True, "chats": [ivan]})).json()["enabled"] == 0
    # стирание необратимо: через внутренний API оно ждёт владельца, со страницы — сразу
    purged = await s.page.post(url, {**ivan, "excluded": True, "purge": True})
    assert purged.json() == {"excluded": True, "purged": 5} and await conn.fetchval("SELECT count(*) FROM messages") == 0
    back = await s.page.post(url, {**ivan, "excluded": False})
    assert back.json() == {"excluded": False, "purged": 0} and await conn.fetchval("SELECT excluded FROM chats") is False
    # служебный чат Telegram вернуть нельзя; незнакомый чат — не найден; стирать без исключения нельзя
    assert (await s.page.post(url, {"peer_class": "user", "tg_id": 777000, "excluded": False})).json()["code"] == "locked"
    assert (await s.page.post(url, {"peer_class": "user", "tg_id": 123456, "excluded": True})).status_code == 404
    assert (await s.page.post(url, {**ivan, "excluded": False, "purge": True})).status_code == 400
    rows = await audit_rows(conn)
    assert ("chat.exclude", "ok", f"чат user:{IVAN}") in rows and ("chat.purge", "ok", f"чат user:{IVAN}; сообщений: 5") in rows
    assert ("chat.include", "ok", f"чат user:{IVAN}") in rows


# --- 5. выгрузка ---

def export_bytes() -> bytes:
    t = 1789200000
    chats = [
        {"name": "Иван Петров", "type": "personal_chat", "id": IVAN, "messages": [
            msg(1, t, IVAN, "Иван Петров", "Пришлю смету к пятнице."), msg(2, t + 60, OWNER, "Евгений Тестов", "Жду.")]},
        {"name": "Семья", "type": "private_group", "id": 3001, "messages": [msg(10, t + 10, 2002, "Мария", "Купи хлеба")]},
        {"name": "Telegram", "type": "personal_chat", "id": 777000, "messages": [msg(20, t + 20, 777000, "Telegram", "Login code: 12345")]},
    ]
    return as_file(full_export(chats)).getvalue()


async def upload(s, body: bytes):
    return await s.page.http.post(API + "/imports", content=body,
                                  headers={**s.page.headers(), "Content-Type": "application/json"})


async def test_export_is_uploaded_scanned_and_imported_without_a_card(stand, conn):
    s = await ready(stand, conn)
    assert (await upload(s, b"")).status_code == 400
    multipart = await s.page.http.post(API + "/imports", files={"file": ("result.json", b"{}")}, headers=s.page.headers())
    assert multipart.status_code == 415
    created = await upload(s, export_bytes())
    assert created.status_code == 201
    import_id = created.json()["import_id"]
    assert [i["import_id"] for i in (await s.page.get("/state")).json()["imports"]["items"]] == [import_id]
    assert (await s.page.get("/state")).json()["imports"]["max_bytes"] == 2 * 1024**3    # тот же предел, что у /api/imports

    scan = (await s.page.get(f"/imports/{import_id}/scan")).json()
    assert scan["owner"] == {"tg_user_id": OWNER, "name": "Евгений Тестов"} and scan["total_messages"] == 4
    by_name = {c["name"]: c for c in scan["chats"]}
    assert by_name["Telegram"]["locked"] is True and by_name["Семья"]["key"] == "chat:3001"

    # через внутренний API импорт ждал бы карточки; со страницы идёт сразу
    assert (await s.api.post(f"/api/imports/{import_id}/run", json={})).status_code == 202
    assert await conn.fetchval("SELECT count(*) FROM messages") == 0
    run = await s.page.post(f"/imports/{import_id}/run", {"exclude": ["chat:3001"]})
    assert run.status_code == 202 and run.json()["state"] == "running"
    await until(lambda: _state(s, import_id, "done"))
    view = (await s.page.get(f"/imports/{import_id}")).json()
    assert view["stats"]["messages_new"] == 2 and view["file_kept"] is False
    assert await conn.fetchval("SELECT count(*) FROM messages") == 2
    assert await conn.fetchval("SELECT excluded FROM chats c JOIN peers p ON p.id = c.peer_id WHERE p.tg_id = 3001")
    assert not list(s.config.data_dir.joinpath("uploads").glob("export-*"))
    assert (await s.page.post(f"/imports/{import_id}/run", {})).status_code == 409        # файл уже импортирован
    assert (await s.page.send("DELETE", f"/imports/{import_id}")).json()["deleted"] is True
    assert (await s.page.get(f"/imports/{import_id}")).status_code == 404
    actions = [a for a, _, _ in await audit_rows(conn)]
    assert actions.count("import.upload") == 1 and "import.run" in actions and "import.delete" in actions
    assert "Иван" not in json.dumps(await audit_rows(conn), ensure_ascii=False)


async def _state(s, import_id: str, wanted: str) -> bool:
    return (await s.page.get(f"/imports/{import_id}")).json()["state"] == wanted


async def test_upload_keeps_the_size_limit_and_refuses_what_is_not_an_export(stand, conn):
    s = await stand()
    await s.page.login(conn)
    s.state.extras["imports"].max_bytes = 100
    assert (await upload(s, b"x" * 500)).status_code == 413
    s.state.extras["imports"].max_bytes = 10_000
    bad = await upload(s, b'{"not": "an export"}')
    assert bad.status_code == 201
    scan = await s.page.get(f"/imports/{bad.json()['import_id']}/scan")
    assert scan.status_code == 422 and scan.json()["code"] == "scan_failed"
    guest = s.browser()
    assert (await guest.http.post(API + "/imports", content=export_bytes(), headers=guest.headers())).status_code == 401


# --- 6. бизнес-режим ---

async def test_business_mode_state_is_reported_and_refreshed(stand, conn):
    s = await stand()
    await s.page.login(conn)
    assert (await s.page.post("/bot/refresh")).json()["code"] == "no_bot"
    s.telegram.me["can_connect_to_business"] = False
    await save_bot(s)
    await bind_owner(s)
    state = (await s.page.get("/state")).json()["bot"]
    assert state["business_capable"] is False and state["business_connections"] == 0
    s.telegram.me["can_connect_to_business"] = True                    # владелец включил режим у @BotFather
    assert (await s.page.post("/bot/refresh")).json() == {"business_capable": True}
    # подключение принимается только от привязанного владельца
    link = {"id": "bc-1", "user_chat_id": STRANGER_USER["id"], "date": int(time.time()), "is_enabled": True,
            "rights": {"can_reply": True}}
    s.telegram.push(business_connection={**link, "user": STRANGER_USER})
    s.telegram.push(business_connection={**link, "id": "bc-2", "user": OWNER_USER, "user_chat_id": OWNER})
    s.telegram.push(business_message={"message_id": 5, "date": int(time.time()), "business_connection_id": "bc-2",
                                      "from": IVAN_USER, "chat": private(IVAN_USER), "text": "Добрый день"})
    await until(lambda: conn.fetchval("SELECT count(*) = 1 FROM messages"))
    state = (await s.page.get("/state")).json()["bot"]
    assert state["business_connections"] == 1 and state["business_can_reply"] is True
    assert (await s.api.get("/api/status")).json()["setup"]["business_connected"] is True
    s.telegram.script["getMe"] = [OSError("сеть")]
    assert (await s.page.post("/bot/refresh")).status_code == 502


# --- 7. своя модель ---

async def test_model_key_is_checked_by_a_live_request_and_applied(stand, conn):
    s = await stand()
    await s.page.login(conn)
    assert (await s.page.put("/llm", {"model": "gpt-test"})).json()["code"] == "empty"
    for body, code in (({"api_key": LLM_KEY, "model": "две части"}, "bad_model"), ({"api_key": LLM_KEY, "model": ""}, "bad_model"),
                       ({"api_key": LLM_KEY, "model": "m", "base_url": "ftp://llm.example"}, "bad_base_url"),
                       ({"api_key": LLM_KEY, "model": "m", "base_url": "http://llm.example/v1"}, "bad_base_url"),
                       ({"api_key": LLM_KEY, "model": "m", "base_url": "https://127.0.0.1:9119/v1"}, "blocked_base_url"),
                       ({"api_key": LLM_KEY, "model": "m", "base_url": "https://u:p@llm.example/v1"}, "bad_base_url")):
        got = await s.page.put("/llm", body)
        assert got.status_code == 422 and got.json()["code"] == code
    assert s.llm.requests == []
    import httpx
    for status, code, words in ((401, "http_401", "не принял ключ"), (404, "http_404", "не знает такой модели"),
                                (429, "http_429", "ограничил запросы")):
        s.llm.script = [httpx.Response(status, json={"error": {"message": "nope"}})] * 3
        got = await s.page.put("/llm", {"api_key": LLM_KEY, "model": "gpt-test"})
        assert got.status_code == 422 and got.json()["code"] == code and words in got.json()["error"]
    s.llm.script = []
    assert bridge.LLM_TEXT not in bridge.builtin_kinds() and s.state.config.own_llm is False

    saved = await s.page.put("/llm", {"api_key": LLM_KEY, "model": "gpt-test", "base_url": "https://llm.example/v1/"})
    assert saved.status_code == 200 and saved.json() == {"ok": True, "model": "gpt-test"}
    assert s.llm.headers[-1]["authorization"] == f"Bearer {LLM_KEY}" and s.llm.requests[-1]["model"] == "gpt-test"
    assert bridge.LLM_TEXT in bridge.builtin_kinds() and s.state.config.llm_base_url == "https://llm.example/v1"
    state = (await s.page.get("/state")).json()["llm"]
    assert state["configured"] is True and state["model"]["value"] == "gpt-test" and LLM_KEY not in json.dumps(state)
    # сервис сам выполняет задания модели — без перезапуска
    job = await bridge.request_text(conn, handler="x", messages=[{"role": "user", "content": "привет"}])
    await until(lambda: conn.fetchval("SELECT status = 'done' FROM jobs WHERE id = $1", job))
    # сменить только модель можно без повторного ввода ключа — пока адрес прежний
    again = await s.page.put("/llm", {"model": "gpt-other", "base_url": "https://llm.example/v1"})
    assert again.status_code == 200 and s.state.config.llm_model == "gpt-other" and s.state.config.llm_api_key == LLM_KEY
    assert s.state.config.llm_base_url == "https://llm.example/v1"
    # а смена адреса (в том числе «не назван» — значит обычный) без ключа не проходит
    moved = await s.page.put("/llm", {"model": "gpt-other"})
    assert moved.status_code == 422 and moved.json()["code"] == "key_required"
    assert s.state.config.llm_base_url == "https://llm.example/v1"
    assert (await s.page.delete("/llm")).status_code == 200
    assert s.state.config.own_llm is False and bridge.LLM_TEXT not in bridge.builtin_kinds()
    assert ss.SecretStore(s.config.data_dir).load() == {}
    rows = await audit_rows(conn)
    assert ("llm.save", "refused", "проверка не прошла: http_401") in rows and ("llm.removed", "ok", "") in rows


# --- 8. что собрано и журнал ---

async def test_overview_shows_counts_and_recent_actions(stand, conn):
    s = await ready(stand, conn)
    created = await upload(s, export_bytes())
    import_id = created.json()["import_id"]
    await s.page.post(f"/imports/{import_id}/run", {})
    await until(lambda: _state(s, import_id, "done"))
    out = (await s.page.get("/overview")).json()
    archive = out["archive"]
    assert archive["messages"] == 3 and archive["chats"] == 2 and archive["chats_excluded"] == 1
    assert archive["sending"] is False and archive["embeddings_enabled"] is False and archive["guard_enabled"] is False
    assert "setup" not in archive and "owner_known" not in archive
    titles = [row["title"] for row in out["audit"]]
    assert titles[0] == "Запущен импорт выгрузки" and "Вход по ссылке" in titles
    await audit.write(s.state.pool, "bot.token", audit.REFUSED, "ботом уже пользуется другая программа")
    assert (await s.page.get("/overview")).json()["audit"][0]["title"] == "Токен бота согласований не сохранён"
    assert set(out["audit"][0]) == {"at", "action", "title", "outcome", "detail"}
    # входы, ключи и аккаунты — отдельным списком
    key_titles = [row["title"] for row in out["audit_key"]]
    assert "Вход по ссылке" in key_titles and "Ключи приложения Telegram сохранены" in key_titles
    assert "Запущен импорт выгрузки" not in key_titles
    assert (await s.browser().get("/overview")).status_code == 401


async def test_every_audit_action_has_a_title_and_unknown_actions_are_refused(stand, conn):
    import re
    from importlib import resources

    source = (resources.files("shturman.setup_page") / "service.py").read_text(encoding="utf-8")
    used = set(re.findall(r'_log\(request, "([a-z_.]+)"', source)) | set(re.findall(r'"(tg\.sync_o(?:n|ff))"', source)) \
        | {"login.failed"}
    assert used <= set(audit.ACTIONS), used - set(audit.ACTIONS)
    assert len(used) >= 25
    s = await stand()
    try:
        await audit.write(s.state.pool, "что-то.новое")
        raise AssertionError("незнакомое действие не должно попадать в журнал")
    except KeyError:
        pass
    await audit.write(s.state.pool, "logout", detail="я" * 1000)
    assert await conn.fetchval("SELECT length(detail) FROM setup_audit") == 300
    for i in range(5):
        await audit.write(s.state.pool, "logout", detail=str(i))
    await conn.execute("DELETE FROM setup_audit WHERE id <= (SELECT max(id) FROM setup_audit) - 3")
    assert [r["detail"] for r in await audit.recent(conn)] == ["4", "3", "2"]


async def test_flood_of_small_actions_cannot_push_out_logins_keys_and_accounts(stand, conn, monkeypatch):
    """Журнал обрезается, но записи о входах, смене ключей и входе в аккаунты Telegram хранятся
    отдельно от обычных: потоком мелких действий их не вытеснить ни из базы, ни из вида."""
    s = await ready(stand, conn)
    account_id = await connect(s, "owner", ME, confirm_owner=True)
    monkeypatch.setattr(audit, "KEEP", 50)
    monkeypatch.setattr(audit, "KEEP_IMPORTANT", 40)
    important = {r["action"] for r in await conn.fetch("SELECT DISTINCT action FROM setup_audit")} & audit.IMPORTANT
    assert {"login.link", "bot.token", "bot.bind_link", "tg.keys", "tg.login", "tg.login_done"} <= important
    # тот, кто получил доступ, заметает следы: сотни действий, каждое из которых оставляет обычную запись
    for i in range(4):
        await s.page.put(f"/tg/accounts/{account_id}/options", {"backfill_months": i + 1})
    await conn.execute(
        """INSERT INTO setup_audit (action, outcome, detail)
           SELECT (ARRAY['tg.options', 'tg.sync_on', 'tg.sync_off', 'login.failed', 'login.code_sent', 'logout'])[1 + i % 6],
                  'ok', '' FROM generate_series(1, 3000) i""")
    await audit.trim(conn)
    left = {r["action"]: r["n"] for r in await conn.fetch("SELECT action, count(*) AS n FROM setup_audit GROUP BY action")}
    assert sum(n for action, n in left.items() if action not in audit.IMPORTANT) == 50
    assert important <= set(left)                                   # все важные записи на месте
    out = (await s.page.get("/overview")).json()
    assert {row["action"] for row in out["audit"]} <= {"tg.options", "tg.sync_on", "tg.sync_off", "login.failed",
                                                       "login.code_sent", "logout"}       # общий список залит
    assert {"login.link", "tg.keys", "tg.login_done"} <= {row["action"] for row in out["audit_key"]}
    # у важных свой предел: поток важных записей обычные не трогает и сам обрезается
    await conn.execute(
        "INSERT INTO setup_audit (action, outcome, detail) SELECT 'tg.keys', 'ok', '' FROM generate_series(1, 500)")
    await audit.trim(conn)
    counts = await conn.fetchrow(
        "SELECT count(*) FILTER (WHERE action = ANY($1::text[])) AS important, count(*) AS total FROM setup_audit",
        sorted(audit.IMPORTANT))
    assert (counts["important"], counts["total"]) == (40, 90)
    assert audit.IMPORTANT <= set(audit.ACTIONS)


async def test_changing_actions_need_a_session(stand, conn):
    s = await stand()
    guest = s.browser()
    for method, path in (("POST", "/bot/token"), ("DELETE", "/bot/token"), ("POST", "/bot/bind"), ("POST", "/bot/refresh"),
                         ("PUT", "/tg/keys"), ("DELETE", "/tg/keys"), ("POST", "/tg/login"), ("POST", "/tg/login/x/password"),
                         ("POST", "/tg/login/x/cancel"), ("POST", "/tg/accounts/1/pause"), ("POST", "/tg/accounts/1/resume"),
                         ("POST", "/tg/accounts/1/logout"), ("PUT", "/tg/accounts/1/options"), ("POST", "/tg/accounts/1/sync"),
                         ("POST", "/tg/accounts/1/exclude"), ("POST", "/imports"), ("POST", "/imports/x/run"),
                         ("DELETE", "/imports/x"), ("PUT", "/llm"), ("DELETE", "/llm"), ("POST", "/logout"),
                         ("POST", "/logout-all")):
        got = await guest.send(method, path)
        assert got.status_code == 401 and got.json()["code"] == "unauthenticated", (method, path)
    for path in ("/state", "/overview", "/tg/login/x", "/tg/accounts/1/dialogs", "/imports", "/imports/x", "/imports/x/scan"):
        assert (await guest.get(path)).status_code == 401, path
    assert await conn.fetchval("SELECT count(*) FROM setup_audit") == 0
