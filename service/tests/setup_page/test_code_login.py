"""Вход на страницу настройки по коду из бота согласований (П4, Р-88).

Код — 6 цифр, 5 минут, один раз и только в браузере, который его запросил (cookie попытки). Не больше
трёх запросов за 15 минут на всех, пять попыток на код и пауза после них. Сообщение в боте — с
временем, адресом и браузером словами и кнопками «Это не я» и «Выйти на всех устройствах». У бота от
посредника вход по коду по умолчанию выключен; включить — только нажатием в боте.
"""

import asyncio
import json
import logging

from shturman import authority, bridge, confirm
from shturman.setup_page import auth, code_login, service as setup_service

from setup_fakes import API, ORIGIN, bind_owner, save_bot, stand  # noqa: F401, I001 — stand — фикстура; первым: добавляет пути
from exec_fakes import OWNER, STRANGER_USER, until


async def owner_bound(stand, conn, **changes):
    s = await stand(**changes)
    await s.page.login(conn)
    assert (await save_bot(s)).status_code == 200
    await until(lambda: s.state.extras["executor"].bot and s.state.extras["executor"].bot.identity)
    await bind_owner(s)
    return s


def code_messages(s) -> list[dict]:
    return [p for p in s.telegram.calls("sendMessage") if p["text"].startswith("🔐 Код входа")]


def codes(s) -> list[str]:
    return ["".join(ch for ch in p["text"].split("\n")[0] if ch.isdigit()) for p in code_messages(s)]


def buttons(params: dict) -> dict[str, str]:
    rows = (params.get("reply_markup") or {}).get("inline_keyboard") or []
    return {b["text"]: b["callback_data"] for row in rows for b in row}


async def press(s, data: str) -> dict:
    """Владелец нажимает кнопку под сообщением с кодом — тем же путём, что настоящий бот."""
    with authority.owner_context(OWNER, chat_id=OWNER, action="telegram.callback"):
        return await bridge.dispatch_callback(s.conn, data, OWNER)


def lock_notices(s) -> list[dict]:
    return [p for p in s.telegram.calls("sendMessage") if "неверный код" in p["text"]]


async def test_code_from_the_bot_logs_in_once_in_the_browser_that_asked(stand, conn, caplog):
    caplog.set_level(logging.DEBUG)
    s = await owner_bound(stand, conn)
    guest = s.browser()
    assert (await guest.get("/session")).json() == {"authenticated": False, "code_login": True, "code_login_off": None}
    asked = await guest.http.post(API + "/login/code/request", json={},
                                  headers={**guest.headers(), "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36",
                                           "X-Forwarded-For": "203.0.113.7"})
    assert asked.status_code == 200 and asked.json() == {"result": "sent", "expires_in": 300, "left": 2}
    # единственная cookie страницы: попытка входа по коду, узкий путь, HttpOnly, Secure, SameSite=Strict
    (cookie,) = asked.headers.get_list("set-cookie")
    assert cookie.startswith(auth.CODE_COOKIE + "=")
    for part in ("Path=/shturman-setup/api/login/code", "HttpOnly", "Secure", "SameSite=Strict", "Max-Age=300"):
        assert part in cookie
    assert "Domain" not in cookie
    guest.code_cookie = cookie.split(";", 1)[0].split("=", 1)[1]

    (sent,) = code_messages(s)
    (code,) = codes(s)
    assert len(code) == 6 and sent["chat_id"] == OWNER
    text = sent["text"]
    assert "Никому не сообщайте код, даже ассистенту" in text and "5 минут" in text
    assert "с адреса 203.0.113.7" in text and "Chrome на Windows" in text and "Запрошен сегодня в" in text
    assert set(buttons(sent)) == {"Это не я", "Выйти на всех устройствах"}
    # в базе — ни кода, ни идентификатора попытки: только HMAC кода и SHA-256 попытки; адреса нет
    dump = await conn.fetchval("SELECT string_agg(t::text, ' ') FROM setup_codes t")
    assert code not in dump and guest.code_cookie not in dump and "203.0.113.7" not in dump
    assert auth.token_hash(guest.code_cookie) in dump
    assert code not in caplog.text and guest.code_cookie not in caplog.text

    # тот же код в другом браузере (без этой cookie) не действует — и попытку этого браузера не тратит
    other = s.browser()
    assert (await other.post("/login/code", {"code": code})).json()["code"] == "none"
    done = await guest.post("/login/code", {"code": f"{code[:3]} {code[3:]}"})
    assert done.status_code == 200
    assert any("Max-Age=0" in v for v in done.headers.get_list("set-cookie"))     # cookie стёрта
    guest.key = done.json()["key"]
    assert (await guest.get("/session")).json()["via"] == "code"
    # вход по коду чужих сессий не завершает; сессия — такая же, как по ссылке (7 дней)
    assert (await s.page.get("/state")).status_code == 200
    ttl = await conn.fetchval("SELECT extract(epoch FROM expires_at - created_at) FROM setup_sessions WHERE via = 'code'")
    assert ttl == auth.SESSION_MAX
    # одноразовый: второй раз тем же кодом и той же cookie — нет
    guest.key = ""
    assert (await guest.post("/login/code", {"code": code})).json()["code"] == "none"
    log = await conn.fetch("SELECT action, detail FROM setup_audit ORDER BY id")
    assert ("login.code", "по коду из бота") in [(r["action"], r["detail"]) for r in log]
    assert any(r["action"] == "login.link" and "по ссылке" in r["detail"] for r in log)
    assert code not in json.dumps([dict(r) for r in log], ensure_ascii=False)
    await until(lambda: any("по коду из этого чата" in p["text"] for p in s.telegram.calls("sendMessage")))


async def test_expired_code_does_not_work(stand, conn):
    s = await owner_bound(stand, conn)
    guest = s.browser()
    await guest.post("/login/code/request")
    code = codes(s)[-1]
    await conn.execute("UPDATE setup_codes SET expires_at = now() - interval '1 second'")
    assert (await guest.post("/login/code", {"code": code})).json()["code"] == "expired"
    assert (await guest.post("/login/code", {"code": code})).json()["code"] == "none"     # погашен


async def test_two_cookies_with_the_same_name_mean_no_attempt(stand, conn):
    """Соседний порт того же имени мог подложить свою cookie: две — это не вход, а отказ."""
    s = await owner_bound(stand, conn)
    guest = s.browser()
    await guest.post("/login/code/request")
    code = codes(s)[-1]
    forged = auth.new_token()
    response = await guest.http.post(API + "/login/code", json={"code": code}, headers={
        **guest.headers(), "Cookie": f"{auth.CODE_COOKIE}={forged}; {auth.CODE_COOKIE}={guest.code_cookie}"})
    assert response.json()["code"] == "none"
    assert (await guest.post("/login/code", {"code": code})).status_code == 200    # своя — по-прежнему годна


async def test_new_request_from_the_same_browser_cancels_its_previous_code(stand, conn):
    s = await owner_bound(stand, conn)
    guest = s.browser()
    await guest.post("/login/code/request")
    first_cookie, first = guest.code_cookie, codes(s)[-1]
    await guest.post("/login/code/request")
    assert guest.code_cookie != first_cookie
    stale = s.browser()
    stale.code_cookie = first_cookie
    assert (await stale.post("/login/code", {"code": first})).json()["code"] == "none"


async def test_five_wrong_codes_cancel_the_code_and_pause_code_login(stand, conn, monkeypatch):
    s = await owner_bound(stand, conn)
    clock = [1_800_000_000.0]
    monkeypatch.setattr(auth, "now", lambda: clock[0])
    guest = s.browser()
    await guest.post("/login/code/request")
    good = codes(s)[-1]
    wrong = "000000" if good != "000000" else "111111"
    for left in (4, 3, 2, 1):
        answer = (await guest.post("/login/code", {"code": wrong})).json()
        assert answer["code"] == "wrong" and answer["left"] == left
    fifth = await guest.post("/login/code", {"code": wrong})
    assert fifth.status_code == 401 and fifth.json()["code"] == "locked" and fifth.json()["retry_after"] == 60
    await until(lambda: lock_notices(s))
    assert (await guest.post("/login/code", {"code": good})).json()["code"] == "locked"     # пауза
    locked = await guest.post("/login/code/request")
    assert locked.status_code == 429 and locked.json()["result"] == "locked"
    clock[0] += 61
    assert (await guest.post("/login/code", {"code": good})).json()["code"] == "none"      # код погашен
    # вторая пауза длиннее первой; удачный вход по ссылке её снимает
    await conn.execute("DELETE FROM setup_codes")      # предел запросов здесь не проверяем
    await guest.post("/login/code/request")
    for _ in range(5):
        await guest.post("/login/code", {"code": wrong})
    clock[0] += 61
    assert (await guest.post("/login/code/request")).json()["result"] == "locked"
    await s.browser().login(conn)
    await conn.execute("DELETE FROM setup_codes")
    assert (await guest.post("/login/code/request")).json()["result"] == "sent"
    await asyncio.sleep(0.05)
    assert len(lock_notices(s)) == 1        # о паузе — одно сообщение за период


async def test_brute_force_is_bounded_by_requests_tries_and_growing_pauses(conn, monkeypatch):
    """Правило перебора на уровне базы: 5 попыток на код, паузы 1 мин → 5 → 15 → час → 6 ч → сутки."""
    clock = [1_800_000_000.0]
    monkeypatch.setattr(auth, "now", lambda: clock[0])
    key = b"k" * 32
    pauses = []
    for _ in range(7):
        await conn.execute("DELETE FROM setup_codes")
        attempt = await auth.start_code(conn, key)
        assert attempt.result == "sent"
        await auth.mark_delivered(conn, attempt.code_id, 1)
        wrong = "000000" if attempt.code != "000000" else "111111"
        results = [(await auth.verify_code(conn, key, attempt.attempt, wrong))[0] for _ in range(5)]
        assert results[:4] == ["wrong"] * 4 and results[4] in ("locked_now", "locked_again")
        left = await auth.lock_left(conn)
        pauses.append(left)
        assert (await auth.verify_code(conn, key, attempt.attempt, attempt.code))[0] == "locked"
        clock[0] += left + 1
    assert pauses == [60, 300, 900, 3600, 6 * 3600, 24 * 3600, 24 * 3600]


async def test_three_requests_per_15_minutes_for_everyone(stand, conn):
    s = await owner_bound(stand, conn)
    first, second = s.browser(), s.browser()
    assert (await first.post("/login/code/request")).json()["left"] == 2
    assert (await second.post("/login/code/request")).json()["left"] == 1
    assert (await first.post("/login/code/request")).json()["left"] == 0
    limited = await s.browser().post("/login/code/request")
    assert limited.status_code == 429 and limited.json()["result"] == "limited"
    assert 0 < limited.json()["retry_after"] <= 15 * 60
    assert "set-cookie" not in limited.headers and len(code_messages(s)) == 3
    await conn.execute("UPDATE setup_codes SET created_at = now() - interval '16 minutes'")
    assert (await s.browser().post("/login/code/request")).json()["result"] == "sent"


async def test_not_me_cancels_the_code_and_every_waiting_attempt(stand, conn):
    s = await owner_bound(stand, conn)
    guest, other = s.browser(), s.browser()
    await guest.post("/login/code/request")
    await other.post("/login/code/request")
    first, second = codes(s)
    message = code_messages(s)[0]
    done = await press(s, buttons(message)["Это не я"])
    assert done["answer"] == "Код погашен." and "Выйти на всех устройствах" in done["edit_text"]
    assert (await guest.post("/login/code", {"code": first})).json()["code"] == "none"
    assert (await other.post("/login/code", {"code": second})).json()["code"] == "none"
    assert await conn.fetchval("SELECT count(*) FROM setup_audit WHERE action = 'login.code_denied'") == 1
    # «Выйти на всех устройствах» под тем же сообщением — все сессии страницы и приложения
    assert (await s.page.get("/state")).status_code == 200
    out = await press(s, buttons(message)["Выйти на всех устройствах"])
    assert out["answer"] == "Все входы завершены."
    assert (await s.page.get("/state")).status_code == 401
    assert await conn.fetchval("SELECT count(*) FROM setup_audit WHERE action = 'logout.all'") == 1
    # чужая метка кнопки ничего не делает
    assert (await press(s, "sh:lc:x:1:wrong-nonce-0"))["answer"] in ("Это сообщение устарело.", "Кнопка недоступна.")


async def test_without_a_bound_owner_the_answer_is_the_same_and_nothing_is_sent(stand, conn):
    s = await stand()
    guest = s.browser()
    assert (await guest.get("/session")).json() == {"authenticated": False, "code_login": False, "code_login_off": None}
    nobody = await guest.post("/login/code/request")
    # тот же вид ответа, что при отправленном коде — ничего не раскрывает
    assert nobody.status_code == 200 and nobody.json() == {"result": "sent", "expires_in": 300, "left": 2}
    for guess in ("000000", "123456", "999999"):
        assert (await guest.post("/login/code", {"code": guess})).json()["code"] == "wrong"   # недоставленный не срабатывает
    # бот есть, а владелец не привязан — тоже некому
    await s.page.login(conn)
    await save_bot(s)
    await guest.post("/login/code/request")
    assert code_messages(s) == []
    assert await conn.fetchval("SELECT count(*) FROM setup_codes WHERE delivered") == 0


async def test_only_the_owner_bound_to_this_bot_gets_the_code(stand, conn):
    """Запись о владельце из общей таблицы (её мог оставить плагин) кодом не пользуется."""
    s = await stand()
    await s.page.login(conn)
    await save_bot(s)
    await conn.execute("INSERT INTO settings (key, value) VALUES ('owner', $1::jsonb)",
                       json.dumps({"user_id": STRANGER_USER["id"], "chat_id": STRANGER_USER["id"]}))
    assert (await s.browser().post("/login/code/request")).json()["result"] == "sent"
    assert code_messages(s) == []
    assert ORIGIN


async def test_dashboard_origin_and_service_port_cannot_ask_or_enter_a_code(stand, conn):
    """Маршруты кода — только на адресе страницы. С Hermes на сервере — только через сокет прокси:
    через порт сервиса (туда достаёт процесс ассистента в сети хоста) — отказ."""
    s = await owner_bound(stand, conn, dashboard_origin="https://assistant.example")
    # с адреса дашборда (соседний порт того же имени) — браузер ставит Origin и Sec-Fetch-Site
    dashboard = s.browser("https://assistant.example")
    refused = await dashboard.http.post(API + "/login/code/request", json={}, headers={
        "Origin": "https://assistant.example", "Sec-Fetch-Site": "same-site", "X-Shturman-Setup": "1"})
    assert refused.status_code in (403, 421)
    # через порт сервиса (так ходит и процесс на хосте) — отказ, даже с правильными заголовками
    port = await s.browser().post("/login/code/request")
    assert port.status_code == 403 and port.json()["code"] == "port"
    assert (await s.browser().post("/login/code", {"code": "123456"})).json()["code"] == "port"
    assert (await s.browser().get("/session")).json()["code_login_off"] == "port"
    assert code_messages(s) == [] and await conn.fetchval("SELECT count(*) FROM setup_codes") == 0


async def test_through_the_proxy_socket_code_login_works_with_hermes(stand, conn, monkeypatch):
    s = await owner_bound(stand, conn, dashboard_origin="https://assistant.example")
    monkeypatch.setattr(setup_service, "_via_proxy", lambda request, state: True)     # как запрос через сокет
    guest = s.browser()
    assert (await guest.post("/login/code/request")).json()["result"] == "sent"
    assert (await guest.post("/login/code", {"code": codes(s)[-1]})).status_code == 200


async def test_managed_bot_has_code_login_off_until_the_owner_presses_in_the_bot(stand, conn, approvals):
    s = await owner_bound(stand, conn)
    bot_id = s.state.extras["executor"].bot.bot_id
    # бот «в одно нажатие» через бот-менеджер посредника (Р-77): так запись делает bot_setup
    await conn.execute(
        """INSERT INTO setup_state (key, value) VALUES ('approvals_bot', $1::jsonb)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
        json.dumps({"bot_id": bot_id, "created_via": "managed", "creator_user_id": OWNER}))
    guest = s.browser()
    info = (await guest.get("/session")).json()
    assert info == {"authenticated": False, "code_login": False, "code_login_off": "managed"}
    assert (await guest.post("/login/code/request")).json()["result"] == "sent"      # тот же ответ
    assert code_messages(s) == []                                                    # но кода нет
    # включение со страницы — только карточка в бот
    asked = await s.page.put("/code-login", {"enabled": True})
    assert asked.status_code == 202
    action = asked.json()["action_id"]
    assert (await guest.get("/session")).json()["code_login"] is False
    # нажатие из приложения «Штурман» не принимается — только в самом боте
    nonce = await conn.fetchval("SELECT nonce FROM pending_actions WHERE id = $1", action)
    with authority.miniapp_context(OWNER, chat_id=OWNER):
        out = await bridge.dispatch_callback(conn, f"sh:cf:y:{action}:{nonce}", OWNER)
    assert "только в боте" in out["answer"]
    assert (await guest.get("/session")).json()["code_login"] is False
    assert (await approvals.press(action))["answer"] == "Сделано."
    assert (await guest.get("/session")).json() == {"authenticated": False, "code_login": True, "code_login_off": None}


async def test_owner_turns_code_login_off_at_once_and_waiting_codes_die(stand, conn):
    s = await owner_bound(stand, conn)
    guest = s.browser()
    await guest.post("/login/code/request")
    code = codes(s)[-1]
    off = await s.page.put("/code-login", {"enabled": False})
    assert off.status_code == 200 and off.json()["enabled"] is False
    assert (await guest.get("/session")).json() == {"authenticated": False, "code_login": False, "code_login_off": "off"}
    assert (await guest.post("/login/code", {"code": code})).json()["code"] == "none"
    await guest.post("/login/code/request")
    assert len(code_messages(s)) == 1                    # выключен — код больше не уходит
    on = await s.page.put("/code-login", {"enabled": True})     # свой бот: включение со страницы — сразу
    assert on.status_code == 200 and on.json()["enabled"] is True
    view = (await s.page.get("/code-login")).json()
    assert view["logins_7d"] == 0 and view["origin"] in (None, "manual")
    assert await conn.fetchval("SELECT count(*) FROM setup_audit WHERE action = 'login.code_setting'") == 2


async def test_internal_api_has_no_code_routes_and_status_shows_only_numbers(stand, conn):
    s = await owner_bound(stand, conn)
    for path in ("/api/login/code/request", "/api/setup/code", "/api/code-login"):
        assert (await s.api.post(path, json={})).status_code == 404
    guest = s.browser()
    await guest.post("/login/code/request")
    await guest.post("/login/code", {"code": codes(s)[-1]})
    status = (await s.api.get("/api/status")).json()["setup"]["code_login"]
    assert status == {"enabled": True, "reason": None, "logins_7d": 1}
    assert codes(s)[-1] not in json.dumps(status)


def test_browser_words_and_address():
    assert code_login.browser_words("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
                                    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1") == "Safari на iPhone"
    assert code_login.browser_words("Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0") \
        == "Firefox на Linux"
    assert code_login.browser_words("python-httpx/0.27") == "неизвестный браузер"
    assert confirm.BOT_ONLY >= {"setup.code_login", "halt.resume"}
