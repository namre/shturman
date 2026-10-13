"""Mini App бота согласований: вход по данным Telegram, своя узкая сессия, «Согласования».

Главное, что здесь проверяется:
  * войти может только тот, кто открыл приложение из Telegram, — данные входа подписаны токеном
    бота согласований, которого у Hermes нет, — и только привязанный владелец; данные входа
    годятся один раз и недолго;
  * сессия приложения — своя и уже сессии страницы: ни одна дверь страницы ею не открывается,
    и наоборот; внутренний API и его токен к приложению не ведут;
  * решение в приложении — нажатие той же кнопки, что в боте, тем же путём: выключатель отправки,
    метки и отпечатки действуют так же; погасшую кнопку нажать нельзя;
  * рамку приложению разрешено открыть только web.telegram.org, остальное под префиксом — нигде;
  * кнопку «Штурман» в чате владельца сервис ставит сам и только при внешнем адресе https.
"""

import dataclasses
import json
import time
from urllib.parse import urlencode

import httpx
import pytest

from setup_fakes import API, ORIGIN, PREFIX, Browser, bind_owner, stand  # noqa: F401, I001 — stand — фикстура; первым: добавляет пути
from conftest import API_AUTH, MCP_AUTH
import exec_fakes  # noqa: E402

from shturman import bridge, confirm, store  # noqa: E402
from shturman.executor import binding, menu_button  # noqa: E402
from shturman.outbox import drafts  # noqa: E402
from shturman.records import ChatRecord, MessageRecord  # noqa: E402
from shturman.setup_page import APP_PATH, approvals, audit, initdata, miniapp, shield  # noqa: E402

TOKEN = exec_fakes.TOKEN
OWNER = exec_fakes.OWNER_USER
STRANGER = exec_fakes.STRANGER_USER
APP_API = API + "/app"
MODULES = ("shturman.api_core", "shturman.executor.service", "shturman.ingest_api", "shturman.tg.service",
           "shturman.outbox.service", "shturman.setup_page.service")
SECRET_WORDS = ("Иван", "Петров", "Евгений", "Смету", "Тестовое действие")


@confirm.applier("t.miniapp")
async def _apply_test_action(conn, payload):
    return "Сделано в тесте."


def signed(user: dict = OWNER, *, age: int = 0, token: str = TOKEN, query_id: str = "AAHdF6IQAAAAAN0XohDhrOrc",
           **extra: str) -> str:
    """Строка initData, какой её подписал бы Telegram для бота с токеном `token`."""
    fields = {"query_id": query_id, "auth_date": str(int(time.time()) - age),
              "user": json.dumps({k: user[k] for k in ("id", "first_name") if k in user}, ensure_ascii=False,
                                 separators=(",", ":")),
              "signature": "c2lnbmF0dXJlLW5vdC1jaGVja2VkLWJ5LXRoZS1zZXJ2aWNl", **extra}
    fields["hash"] = initdata.sign(fields, token)
    return urlencode(fields)


class App(Browser):
    """Приложение внутри Telegram: тот же origin, что у страницы, свой ключ сессии в X-Shturman-App."""

    def headers(self, **extra: str) -> dict[str, str]:
        out = {"Origin": self.origin, "X-Shturman-Setup": "1", "Sec-Fetch-Site": "same-origin"}
        if self.key:
            out["X-Shturman-App"] = self.key
        out.update(extra)
        return out

    async def get(self, path: str, **kw):
        headers = {"Sec-Fetch-Site": "same-origin", **({"X-Shturman-App": self.key} if self.key else {})}
        return await self.http.get(APP_API + path, headers=headers, **kw)

    async def send(self, method: str, path: str, body=None, **kw):
        return await self.http.request(method, APP_API + path, json={} if body is None else body,
                                       headers=self.headers(), **kw)

    async def sign_in(self, init: str) -> httpx.Response:
        response = await self.send("POST", "/login", {"init_data": init})
        if response.status_code == 200:
            self.key = response.json()["key"]
        return response


async def ready(stand, conn, **changes):
    """Сервис со своим ботом согласований и привязанным владельцем; приложение ещё не вошло."""
    s = await stand(modules=MODULES, bot_token=TOKEN, **changes)
    await s.page.login(conn)
    await bind_owner(s)
    app = App(s.api._transport, ORIGIN)
    s.app = app
    return s


async def rebind(s, user: dict) -> None:
    """Владелец сменился: ссылка привязки открыта другим аккаунтом. Ждёт, пока смена состоялась."""
    link = (await s.page.post("/bot/bind")).json()["link"]
    s.telegram.text("/start " + link.split("start=")[1], user=user)
    await exec_fakes.until(lambda: _owner_is(s, user["id"]))


async def _owner_is(s, user_id: int) -> bool:
    owner = await bridge.get_owner(s.conn)
    return bool(owner) and owner["user_id"] == user_id


async def app_audit(conn):
    return [(r["action"], r["outcome"], r["detail"]) for r in
            await conn.fetch("SELECT action, outcome, detail FROM setup_audit WHERE action LIKE 'app.%' ORDER BY id")]


# --- данные входа ------------------------------------------------------------------------------------

def test_init_data_is_checked_exactly_as_telegram_describes():
    now = time.time()
    good = signed()
    data = initdata.check(good, TOKEN, now=now)
    assert data.user_id == OWNER["id"] and data.query_id

    fields = dict(pair.split("=", 1) for pair in good.split("&"))
    # подпись — HMAC-SHA-256 строки «ключ=значение» по алфавиту без hash; ключ — HMAC токена с «WebAppData»
    from urllib.parse import parse_qsl
    import hashlib
    import hmac
    plain = dict(parse_qsl(good))
    check_string = "\n".join(f"{k}={plain[k]}" for k in sorted(plain) if k != "hash")
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    assert hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest() == plain["hash"]
    # поле signature входит в проверяемую строку: подменить или убрать его нельзя
    assert "signature=" in check_string
    without = {k: v for k, v in plain.items() if k != "signature"}
    with pytest.raises(initdata.Invalid) as gone:
        initdata.check(urlencode(without), TOKEN, now=now)
    assert gone.value.code == "bad_signature"

    # переставленные поля — те же данные и та же подпись
    reordered = "&".join(f"{k}={v}" for k, v in reversed(list(fields.items())))
    assert initdata.check(reordered, TOKEN, now=now).signature_hash == data.signature_hash

    tampered = dict(plain, user=json.dumps({"id": STRANGER["id"], "first_name": "Некто"}))
    cases = {
        "bad_signature": [urlencode(tampered), signed(token="7000000009:OTHER-bot-token_0123456789abcdef")],
        "expired": [signed(age=initdata.MAX_AGE + 5)],
        "from_future": [signed(age=-(initdata.CLOCK_SKEW + 30))],
        "malformed": ["", None, 42, "hash=zz", good + "&auth_date=1", "a" * (initdata.MAX_LEN + 1),
                      urlencode({k: v for k, v in plain.items() if k != "hash"}), good.replace("hash=", "HASH=")],
    }
    for code, values in cases.items():
        for value in values:
            with pytest.raises(initdata.Invalid) as refused:
                initdata.check(value, TOKEN, now=now)
            assert refused.value.code == code, (code, value)
    with pytest.raises(initdata.Invalid):
        initdata.check(good, "", now=now)
    # перевод строки в значении — разделитель строки проверки: такое значение не принимается,
    # даже с верной подписью
    for value in ('{"id":1000,"first_name":"Е"}\nauth_date=1', '{"id":1000,"first_name":"Е\r"}'):
        fields = {"auth_date": str(int(now)), "user": value}
        fields["hash"] = initdata.sign(fields, TOKEN)
        with pytest.raises(initdata.Invalid) as split:
            initdata.check(urlencode(fields), TOKEN, now=now)
        assert split.value.code == "malformed"
    # без пользователя или с пользователем без числового id — тоже нет, даже с верной подписью
    for user in ("{}", '{"id": "1000"}', '{"id": true}', '{"id": -5}', "[]", "не json"):
        fields = {"auth_date": str(int(now)), "user": user}
        fields["hash"] = initdata.sign(fields, TOKEN)
        with pytest.raises(initdata.Invalid):
            initdata.check(urlencode(fields), TOKEN, now=now)


# --- вход --------------------------------------------------------------------------------------------

async def test_owner_signs_in_with_data_signed_for_the_approvals_bot(stand, conn):
    s = await ready(stand, conn)
    response = await s.app.sign_in(signed())
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"ok", "key", "expires_at", "idle_seconds"} and body["idle_seconds"] == miniapp.IDLE
    assert "set-cookie" not in response.headers
    assert (await s.app.get("/approvals")).status_code == 200
    # в базе — только отпечатки ключа и подписи, не они сами
    row = await conn.fetchrow("SELECT * FROM miniapp_sessions")
    assert row["token_hash"] != s.app.key and s.app.key not in json.dumps(dict(row), default=str)
    assert row["user_id"] == OWNER["id"] and row["bot_id"] == exec_fakes.BOT_ID
    assert ("app.login", "ok", "") in await app_audit(conn)


async def test_every_refusal_looks_the_same_and_tells_nothing(stand, conn, monkeypatch):
    s = await ready(stand, conn)
    once = signed(query_id="once")
    assert (await s.app.sign_in(once)).status_code == 200
    s.app.key = ""
    reordered = "&".join(reversed(once.split("&")))
    attempts = {
        "stranger": signed(STRANGER), "stale": signed(age=initdata.MAX_AGE + 60),
        "other bot": signed(token="7000000009:OTHER-bot-token_0123456789abcdef"),
        "replayed": once, "replayed reordered": reordered, "garbage": "user=1&hash=00",
        "api token": signed(token=API_AUTH["Authorization"][7:]),
    }
    bodies = set()
    for name, init in attempts.items():
        got = await s.app.sign_in(init)
        assert got.status_code == 401, (name, got.text)
        assert got.json()["code"] == "app_login_failed"
        bodies.add(got.text)
    assert len(bodies) == 1                          # причина наружу не выходит
    assert await conn.fetchval("SELECT count(*) FROM miniapp_sessions") == 1
    # причина — только в журнале, кодом, и не чаще раза в минуту
    failed = [r for r in await app_audit(conn) if r[0] == "app.login_failed"]
    # обычные причины — не чаще раза в минуту; повтор использованных данных входа — всегда
    assert failed == [("app.login_failed", "refused", "причина: not_owner"),
                      ("app.login_failed", "refused", "причина: replayed"),
                      ("app.login_failed", "refused", "причина: replayed")]
    # и владелец узнаёт о повторе в боте — один раз на одни данные входа
    await exec_fakes.until(lambda: [m for m in s.telegram.sent() if m["text"] == miniapp.REPLAY_TEXT])
    assert len([m for m in s.telegram.sent() if m["text"] == miniapp.REPLAY_TEXT]) == 1
    assert "Выйти на всех устройствах" in miniapp.REPLAY_TEXT and "./ops/logout-all.sh" in miniapp.REPLAY_TEXT
    dumped = json.dumps(await app_audit(conn), ensure_ascii=False)
    assert not any(word in dumped for word in SECRET_WORDS) and "Некто" not in dumped


async def test_no_bot_or_no_owner_means_no_way_in(stand, conn):
    s = await stand(modules=MODULES)                 # бота согласований нет
    app = App(s.api._transport, ORIGIN)
    assert (await app.sign_in(signed())).status_code == 401
    s2 = await stand(modules=MODULES, bot_token=TOKEN)   # бот есть, владелец не привязан
    await exec_fakes.until(lambda: getattr(s2.state.extras["executor"].bot, "identity", None))
    app2 = App(s2.api._transport, ORIGIN)
    assert (await app2.sign_in(signed())).status_code == 401
    assert await conn.fetchval("SELECT count(*) FROM miniapp_sessions") == 0


# --- сессия: своя и узкая -----------------------------------------------------------------------------

async def test_app_session_and_setup_session_do_not_open_each_others_doors(stand, conn):
    s = await ready(stand, conn)
    await s.app.sign_in(signed())
    app_key, page_key = s.app.key, s.page.key
    # ключ приложения — ни в заголовке страницы, ни под своим именем — страницу не открывает
    for path in ("/state", "/overview", "/memory/pending", "/tg/accounts/1/dialogs", "/imports"):
        for headers in ({"X-Shturman-Session": app_key}, {"X-Shturman-App": app_key}):
            got = await s.page.http.get(API + path, headers={"Sec-Fetch-Site": "same-origin", **headers})
            assert got.status_code == 401, (path, headers)
    for method, path, body in (("POST", "/bot/token", {"token": TOKEN}), ("PUT", "/tg/keys", {"api_id": "1", "api_hash": "x"}),
                               ("POST", "/tg/login", {"role": "owner"}), ("PUT", "/llm", {"model": "x"}),
                               ("POST", "/bot/bind", {}), ("PUT", "/scenario", {"scenario": "own"}),
                               ("POST", "/memory/pending/pages/1", {"accept": True}), ("POST", "/logout-all", {})):
        got = await s.page.http.request(method, API + path, json=body,
                                        headers={**s.page.headers(), "X-Shturman-Session": "", "X-Shturman-App": app_key})
        assert got.status_code == 401, (method, path, got.text)
    # ключ страницы приложение не открывает
    for got in (await s.page.http.get(APP_API + "/approvals", headers={"Sec-Fetch-Site": "same-origin",
                                                                       "X-Shturman-App": page_key}),
                await s.page.http.post(APP_API + "/approvals/decide", json={"ref": "sh:pg:a:1"},
                                       headers={**s.page.headers(), "X-Shturman-App": ""})):
        assert got.status_code == 401
    # ни токен внутреннего API, ни токен архива
    for headers in (API_AUTH, MCP_AUTH):
        got = await s.api.get(APP_API + "/approvals", headers={**headers, "Sec-Fetch-Site": "same-origin"})
        assert got.status_code == 401
    # и сессия приложения внутренний API не открывает
    assert (await s.app.http.get("/api/status", headers={"X-Shturman-App": app_key})).status_code == 401


def test_app_routes_are_few_and_all_behind_the_app_session():
    from shturman.setup_page import service as setup_service

    routes = [r for r in setup_service.routes() if r.path.startswith(miniapp.APP_API) or r.path == APP_PATH]
    assert sorted((r.path, sorted(r.methods - {"HEAD"})) for r in routes) == sorted([
        (APP_PATH, ["GET"]), (miniapp.APP_API + "/login", ["POST"]), (miniapp.APP_API + "/logout", ["POST"]),
        (miniapp.APP_API + "/approvals", ["GET"]), (miniapp.APP_API + "/approvals/decide", ["POST"])])
    for route in routes:
        if route.path != APP_PATH:
            assert getattr(route.endpoint, "miniapp", False), route.path     # своя обёртка — своя сессия
    # и ни один маршрут страницы не принимает сессию приложения
    for route in setup_service.routes():
        if route.path.startswith(miniapp.APP_API) or route.path == APP_PATH:
            continue
        assert not getattr(route.endpoint, "miniapp", False), route.path


async def test_session_ends_when_idle_when_owner_changes_and_on_logout_all(stand, conn):
    s = await ready(stand, conn)
    await s.app.sign_in(signed(query_id="a"))
    assert (await s.app.get("/approvals")).status_code == 200
    await conn.execute("UPDATE miniapp_sessions SET last_seen_at = now() - make_interval(secs => $1)",
                       float(miniapp.IDLE + 5))
    assert (await s.app.get("/approvals")).status_code == 401
    # новый вход; потом владелец сменился — сессия прежнего владельца больше не действует
    await s.app.sign_in(signed(query_id="b"))
    assert (await s.app.get("/approvals")).status_code == 200
    await rebind(s, exec_fakes.IVAN_USER)
    assert (await s.app.get("/approvals")).status_code == 401
    # новый владелец входит своими данными; «выйти везде» на странице гасит и приложение
    await rebind(s, OWNER)
    await s.app.sign_in(signed(query_id="c"))
    assert (await s.app.get("/approvals")).status_code == 200
    assert (await s.page.post("/logout-all")).status_code == 200
    assert (await s.app.get("/approvals")).status_code == 401


async def test_rebinding_back_to_the_same_owner_does_not_revive_old_sessions(stand, conn):
    s = await ready(stand, conn)
    await s.app.sign_in(signed(query_id="a"))
    old = s.app.key
    assert (await s.app.get("/approvals")).status_code == 200
    await rebind(s, exec_fakes.IVAN_USER)
    await rebind(s, OWNER)                           # A → B → A
    assert (await s.app.get("/approvals")).status_code == 401
    assert await conn.fetchval("SELECT revoked_at IS NOT NULL FROM miniapp_sessions WHERE user_id = $1",
                               OWNER["id"])
    # и привязка того же владельца заново (A → A) тоже завершает сессии
    await s.app.sign_in(signed(query_id="b"))
    assert s.app.key != old and (await s.app.get("/approvals")).status_code == 200
    link = (await s.page.post("/bot/bind")).json()["link"]
    s.telegram.text("/start " + link.split("start=")[1], user=OWNER)
    await exec_fakes.until(lambda: conn.fetchval(
        "SELECT count(*) = 0 FROM miniapp_sessions WHERE revoked_at IS NULL"))
    assert (await s.app.get("/approvals")).status_code == 401


async def test_same_bot_token_entered_again_does_not_revive_old_sessions(stand, conn):
    from setup_fakes import save_bot

    s = await stand(modules=MODULES)                 # токен бота вводится на странице, а не в .env
    await s.page.login(conn)
    assert (await save_bot(s)).status_code == 200
    await bind_owner(s)
    s.app = App(s.api._transport, ORIGIN)
    await s.app.sign_in(signed())
    assert (await s.app.get("/approvals")).status_code == 200
    assert (await s.page.delete("/bot/token")).status_code == 200
    await exec_fakes.until(lambda: s.state.extras["executor"].bot is None)
    assert (await save_bot(s)).status_code == 200    # тот же бот, тот же владелец
    await exec_fakes.until(lambda: getattr(s.state.extras["executor"].bot, "identity", None))
    assert (await s.page.get("/state")).json()["bot"]["owner_bound"] is True
    assert (await s.app.get("/approvals")).status_code == 401
    assert await conn.fetchval("SELECT count(*) FROM miniapp_sessions WHERE revoked_at IS NULL") == 0


async def test_shared_bot_token_closes_the_app_until_resolved(stand, conn):
    """Ботом согласований пользуется ещё кто-то (409 на опрос) — так выглядит и тот же токен у бота
    Hermes. Пока это так, вход закрыт и сессии не действуют; отказ не сдвигает срок сессии."""
    s = await ready(stand, conn)
    await s.app.sign_in(signed(query_id="before"))
    bot = s.state.extras["executor"].bot
    seen = await conn.fetchval("SELECT last_seen_at FROM miniapp_sessions")
    for problem in ("other_poller", "conflict", "webhook"):
        bot.problem = problem
        assert (await s.app.get("/approvals")).status_code == 401
        refused = await App(s.api._transport, ORIGIN).sign_in(signed(query_id=problem))
        assert refused.status_code == 401 and refused.json()["code"] == "app_login_failed"
    assert await conn.fetchval("SELECT last_seen_at FROM miniapp_sessions WHERE revoked_at IS NULL") == seen
    assert ("app.login_failed", "refused", "причина: bot_conflict") in await app_audit(conn)
    bot.problem = None
    assert (await s.app.get("/approvals")).status_code == 200


# --- адрес и рамка ------------------------------------------------------------------------------------

async def test_app_page_is_framable_only_by_telegram_web_and_nothing_else_is(stand, conn):
    s = await ready(stand, conn)
    await s.app.sign_in(signed())
    page = await s.app.http.get(APP_PATH, headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Dest": "iframe"})
    assert page.status_code == 200 and "Согласования" in page.text
    csp = page.headers["content-security-policy"]
    assert "frame-ancestors https://web.telegram.org" in csp and "frame-ancestors 'none'" not in csp
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "*" not in csp
    assert "x-frame-options" not in page.headers
    # ни встроенных скриптов, ни стилей, ни чужих адресов (скрипт Telegram не подключается)
    assert "<script>" not in page.text and " style=" not in page.text and "telegram.org" not in page.text
    for name in ("app.js", "app.css"):
        got = await s.app.http.get(PREFIX + "/static/" + name, headers={"Sec-Fetch-Site": "same-origin"})
        assert got.status_code == 200
        assert "frame-ancestors 'none'" in got.headers["content-security-policy"]
    script = (await s.app.http.get(PREFIX + "/static/app.js")).text
    assert "innerHTML" not in script and "eval(" not in script and "https://web.telegram.org" in script
    # всё остальное под префиксом — как было: рамки нет нигде
    for got in (await s.page.http.get(PREFIX + "/"), await s.app.get("/approvals"),
                await s.app.http.post(APP_PATH, json={}), await s.app.http.get(APP_PATH + "x"),
                await s.app.http.get(PREFIX + "/app")):
        assert "frame-ancestors 'none'" in got.headers["content-security-policy"], got.url
        assert got.headers["x-frame-options"] == "DENY"
    assert shield.APP_CSP.count("frame-ancestors") == 1


async def test_dashboard_address_never_serves_the_app(stand, conn):
    s = await ready(stand, conn, setup_origin="https://assistant.example.com:8443",
                    dashboard_origin="https://assistant.example.com")
    for host in ("assistant.example.com", "assistant.example.com:443", "evil.example"):
        got = await s.app.http.get(APP_PATH, headers={"Host": host})
        assert got.status_code == 421, host
        got = await s.app.http.post(APP_API + "/login", json={"init_data": signed()}, headers={"Host": host})
        assert got.status_code == 421, host
    # запрос с соседнего порта (дашборд) отвергается и у приложения
    foreign = await s.app.http.post(APP_API + "/login", json={"init_data": signed()},
                                    headers={"Origin": "https://assistant.example.com", "X-Shturman-Setup": "1",
                                             "Sec-Fetch-Site": "same-site", "Host": "test"})
    assert foreign.status_code == 403


# --- «Согласования» ------------------------------------------------------------------------------------

async def _helper_chat(s) -> int:
    """Чат с Иваном у аккаунта-помощника, с давним исходящим сообщением (агент не пишет первым)."""
    conn = s.conn
    account = await store.ensure_account(conn, 1500, "Помощник", "assistant")
    chat_id, _ = await store.ensure_chat(conn, account, ChatRecord("user", 2001, "personal_chat", "Иван Петров"))
    from datetime import datetime, timedelta, timezone
    at = datetime.now(timezone.utc) - timedelta(days=30)
    record = MessageRecord(tg_message_id=900001, sent_at=at, kind="message", sender_class="user", sender_tg_id=1000,
                           sender_name="Я", text="Здравствуйте!", entities=None, reply_to_tg_id=None,
                           forwarded_from=None, edited_at=None, media_type=None, media_path=None, service_action=None)
    await store.upsert_messages(conn, [(chat_id, record, True)], source="session", owner_tg_id=1000)
    return chat_id


async def _delivered(s):
    """Ждёт, пока исполнитель бота отправит в чат всё из очереди."""
    await exec_fakes.until(lambda: s.conn.fetchval(
        "SELECT count(*) = 0 FROM jobs WHERE kind IN ('notify.owner','notify.edit') AND status IN ('queued','running')"))


async def test_list_shows_what_the_bot_shows_and_a_decision_goes_the_same_way(stand, conn):
    s = await ready(stand, conn)
    # действие ждёт владельца — так его ставит запрос ассистента через внутренний API
    out = await confirm.request(conn, "t.miniapp", "Тестовое действие для приложения", {"n": 1})
    action = out["action_id"]
    await _delivered(s)
    card_id = await conn.fetchval("SELECT card_message_id FROM pending_actions WHERE id = $1", action)
    assert isinstance(card_id, int)
    sent_buttons = exec_fakes.buttons_of(s.telegram.sent()[-1])
    # предложение завести страницу о человеке, уже ушедшее сводкой
    person = await conn.fetchval("INSERT INTO people (display_name) VALUES ('Мария Сидорова') RETURNING id")
    await conn.execute("""INSERT INTO page_proposals (person_id, reason, batch, pos, notified_at)
                          VALUES ($1, '{"messages": 30}', 'pg1', 1, now())""", person)
    unshown = await conn.fetchval("INSERT INTO people (display_name) VALUES ('Олег') RETURNING id")
    await conn.execute("INSERT INTO page_proposals (person_id, reason) VALUES ($1, '{\"messages\": 25}')", unshown)

    await s.app.sign_in(signed())
    listed = (await s.app.get("/approvals")).json()
    assert listed["later"]["page"] == 1 and listed["sending"] == "off"
    by_kind = {item["kind"]: item for item in listed["items"]}
    assert set(by_kind) == {"action", "page"}
    act = by_kind["action"]
    assert "Тестовое действие для приложения" in act["text"] and act["title"] == f"Действие № {action}"
    # в приложении над карточкой уже есть «Подтверждение действия»: текст — само описание,
    # предупреждение — отдельной строкой (в боте — одна карточка, как раньше)
    assert act["text"] == "Тестовое действие для приложения" and act["note"] == confirm.CARD_WARNING
    assert "Подтвердите действие" in s.telegram.sent()[-1]["text"]
    # те же кнопки, что под карточкой в боте, — с той же меткой
    assert {a["label"]: a["ref"] for a in act["actions"]} == sent_buttons
    assert not any(key.startswith("_") for item in listed["items"] for key in item)
    page_item = by_kind["page"]
    assert "Мария Сидорова" in page_item["text"]
    assert [a["ref"] for a in page_item["actions"]] == [f"sh:pg:a:{person}", f"sh:pg:r:{person}"]

    # «Нет» в приложении — как «Нет» в боте: действие отклонено, карточка в боте погашена
    got = await s.app.post("/approvals/decide", {"ref": sent_buttons["Нет"]})
    assert got.status_code == 200 and got.json()["answer"] == "Отклонено."
    assert await conn.fetchval("SELECT status FROM pending_actions WHERE id = $1", action) == "rejected"
    await _delivered(s)
    edit = s.telegram.calls("editMessageText")[-1]
    assert edit["message_id"] == card_id and edit["text"].startswith("Отклонено:") and "reply_markup" not in edit
    # погасшую кнопку не нажать — ни «Нет» ещё раз, ни «Да»
    for ref in (sent_buttons["Нет"], sent_buttons["Да, сделать"]):
        again = await s.app.post("/approvals/decide", {"ref": ref})
        assert again.status_code == 409 and again.json()["code"] == "gone"
    assert await conn.fetchval("SELECT status FROM pending_actions WHERE id = $1", action) == "rejected"

    # страница о человеке — тем же обработчиком, что кнопка под сводкой
    assert (await s.app.post("/approvals/decide", {"ref": f"sh:pg:a:{person}"})).json()["answer"] == "Страница будет заведена."
    assert await conn.fetchval("SELECT status FROM page_proposals WHERE person_id = $1", person) == "accepted"
    # ещё не показанное предложение кнопкой отсюда не решить: его кнопки ещё нет
    hidden = await s.app.post("/approvals/decide", {"ref": f"sh:pg:a:{unshown}"})
    assert hidden.status_code == 409
    assert await conn.fetchval("SELECT status FROM page_proposals WHERE person_id = $1", unshown) == "pending"
    assert [i["kind"] for i in (await s.app.get("/approvals")).json()["items"]] == []

    rows = await app_audit(conn)
    assert ("app.decide", "ok", f"Подтверждение действия № {action}: «Нет»") in rows
    assert ("app.decide", "ok", f"Страница памяти № {person}: «Завести страницу»") in rows
    assert ("app.decide", "refused", "кнопки уже нет в списке") in rows
    dumped = json.dumps(rows, ensure_ascii=False)
    assert "Мария" not in dumped and "Тестовое действие" not in dumped


async def test_only_known_buttons_of_the_screen_are_accepted(stand, conn):
    s = await ready(stand, conn)
    await s.app.sign_in(signed())
    for ref in ("sh:oc:menu", "sh:gd:more", "sh:oa:y:1:abcdefgh", "sh:cf:y:1", "pg:a:1", "sh:pg:a:1:extra",
                "sh:cm:a:1:" + "0" * 23, 7, None, "", "sh:" + "x" * 70):
        got = await s.app.post("/approvals/decide", {"ref": ref})
        assert got.status_code == 400 and got.json()["code"] == "bad_ref", ref
    # кнопка правильного вида, но её нет в списке — не передаётся вовсе
    action = (await confirm.request(conn, "t.miniapp", "Тестовое действие для приложения", {"n": 2}))["action_id"]
    forged = await s.app.post("/approvals/decide", {"ref": f"sh:cf:y:{action}:forgedNonce12"})
    assert forged.status_code == 409
    assert await conn.fetchval("SELECT status FROM pending_actions WHERE id = $1", action) == "pending"


async def test_send_in_the_app_obeys_the_sending_switch_like_the_bot(stand, conn):
    s = await ready(stand, conn)
    chat_id = await _helper_chat(s)
    draft = await drafts.create(conn, s.state, chat_id=chat_id, text="Смету пришлю в пятницу.", prepare_only=True)
    await _delivered(s)
    await s.app.sign_in(signed())
    listed = (await s.app.get("/approvals")).json()
    (item,) = [i for i in listed["items"] if i["kind"] == "draft"]
    assert item["text"] == "Смету пришлю в пятницу." and listed["sending"] == "off"
    assert any(line.startswith("Кому: Иван Петров") for line in item["head"])
    assert "Изменить текст здесь нельзя" in item["note"]
    labels = {a["label"]: a["ref"] for a in item["actions"]}
    assert set(labels) == {"Отправить", "Отклонить"}            # «Изменить» нет — как и в боте
    # «Отправить» при выключенной отправке — тот же ответ, что в боте; черновик ждёт
    sent = await s.app.post("/approvals/decide", {"ref": labels["Отправить"]})
    assert sent.status_code == 200 and sent.json()["answer"].startswith("Пока нельзя") and "выключен" in sent.json()["answer"]
    assert (await conn.fetchval("SELECT status FROM outbox_drafts WHERE id = $1", draft["draft_id"])) == "pending"
    assert not s.telegram.sent(business=True)
    # «Отклонить» — черновик закрыт, карточка в боте без кнопок
    card_id = await conn.fetchval("SELECT card_message_ids[1] FROM outbox_drafts WHERE id = $1", draft["draft_id"])
    rejected = await s.app.post("/approvals/decide", {"ref": labels["Отклонить"]})
    assert rejected.json()["answer"] == "Отклонено, ничего не отправлено."
    assert (await conn.fetchval("SELECT status FROM outbox_drafts WHERE id = $1", draft["draft_id"])) == "rejected"
    await _delivered(s)
    assert any(e["message_id"] == card_id and "reply_markup" not in e for e in s.telegram.calls("editMessageText"))
    assert [i for i in (await s.app.get("/approvals")).json()["items"] if i["kind"] == "draft"] == []


async def test_hermes_with_the_internal_api_token_cannot_decide(stand, conn):
    """Ассистент в Hermes достаёт до порта сервиса и знает токен внутреннего API. Сессию приложения
    он получить не может: нужны данные входа, подписанные токеном бота согласований."""
    s = await ready(stand, conn)
    action = (await confirm.request(conn, "t.miniapp", "Тестовое действие для приложения", {"n": 3}))["action_id"]
    nonce = await conn.fetchval("SELECT nonce FROM pending_actions WHERE id = $1", action)
    ref = f"sh:cf:y:{action}:{nonce}"
    headers = {**API_AUTH, "Origin": ORIGIN, "X-Shturman-Setup": "1", "Sec-Fetch-Site": "same-origin"}
    for path, body in (("/approvals/decide", {"ref": ref}), ("/approvals", None)):
        got = await s.api.request("POST" if body else "GET", APP_API + path, json=body, headers=headers)
        assert got.status_code == 401
    # данные входа, подписанные тем, что у Hermes есть (токены API и архива), не принимаются
    for token in (API_AUTH["Authorization"][7:], MCP_AUTH["Authorization"][7:]):
        assert (await s.app.sign_in(signed(token=token))).status_code == 401
    # внутренний API не выдаёт ни сессий приложения, ни данных входа
    for path in ("/api/app/login", "/api/miniapp", "/api/executor/status"):
        got = await s.api.post(path, json={"init_data": signed()})
        assert got.status_code in (404, 405)
    status = (await s.api.get("/api/executor/status")).text
    assert TOKEN not in status and "init" not in status
    assert await conn.fetchval("SELECT status FROM pending_actions WHERE id = $1", action) == "pending"


# --- кнопка меню ---------------------------------------------------------------------------------------

async def test_menu_button_is_set_in_the_owners_chat_only_with_a_public_https_address(stand, conn):
    s = await ready(stand, conn, setup_origin="https://assistant.example.com:8443",
                    dashboard_origin="https://assistant.example.com")
    url = "https://assistant.example.com:8443" + APP_PATH
    await exec_fakes.until(lambda: s.telegram.calls("setChatMenuButton"))
    (call,) = s.telegram.calls("setChatMenuButton")
    assert call == {"chat_id": OWNER["id"], "menu_button": {"type": "web_app", "text": "Штурман", "web_app": {"url": url}}}
    bot = s.state.extras["executor"].bot
    assert bot.menu == menu_button.SET
    assert (await s.api.get("/api/executor/status")).json()["bot"]["menu_button"] == "set"
    # повторная сверка ничего не меняет и Telegram не спрашивает
    await bot.sync_menu()
    assert len(s.telegram.calls("setChatMenuButton")) == 1
    # другой владелец: прежнему — обычная кнопка, новому — «Штурман»
    await rebind(s, exec_fakes.IVAN_USER)
    await exec_fakes.until(lambda: len(s.telegram.calls("setChatMenuButton")) == 3)
    assert s.telegram.calls("setChatMenuButton")[1:] == [
        {"chat_id": OWNER["id"], "menu_button": {"type": "default"}},
        {"chat_id": exec_fakes.IVAN, "menu_button": {"type": "web_app", "text": "Штурман", "web_app": {"url": url}}}]
    # адреса больше нет — кнопка возвращается к обычной
    s.state.config = dataclasses.replace(s.state.config, setup_origin="")
    await bot.sync_menu()
    assert s.telegram.calls("setChatMenuButton")[-1] == {"chat_id": exec_fakes.IVAN, "menu_button": {"type": "default"}}
    assert bot.menu == menu_button.NO_ADDRESS
    saved = await binding.get_state(conn, menu_button.STATE_KEY)
    assert saved == {"bot_id": exec_fakes.BOT_ID, "chat_id": None, "url": None}


async def test_no_menu_button_without_a_public_https_address(stand, conn):
    s = await ready(stand, conn)                      # страница — только под локальным именем (туннель SSH)
    bot = s.state.extras["executor"].bot
    for origin in ("", "http://assistant.example.com:8443"):
        s.state.config = dataclasses.replace(s.state.config, setup_origin=origin)
        await bot.sync_menu()
        assert bot.menu == menu_button.NO_ADDRESS
    # адрес страницы совпал с адресом дашборда — страница по нему не отдаётся, кнопки нет тоже
    s.state.config = dataclasses.replace(s.state.config, setup_origin="https://assistant.example.com",
                                         dashboard_origin="https://assistant.example.com")
    await bot.sync_menu()
    assert bot.menu == menu_button.NO_ADDRESS
    assert s.telegram.calls("setChatMenuButton") == []
    assert (await s.api.get("/api/executor/status")).json()["bot"]["menu_button"] == "no_address"


async def test_menu_button_failure_does_not_stop_the_bot(stand, conn):
    s = await ready(stand, conn)
    bot = s.state.extras["executor"].bot
    s.state.config = dataclasses.replace(s.state.config, setup_origin="https://assistant.example.com:8443")
    s.telegram.script["setChatMenuButton"] = [exec_fakes.refusal(400, "Bad Request: invalid url")]
    await bot.sync_menu()
    assert bot.menu == menu_button.FAILED
    assert await binding.get_state(conn, menu_button.STATE_KEY) is None    # не записано — повторим
    await bot.sync_menu()
    assert bot.menu == menu_button.SET
    s.telegram.text("/help")                                               # бот по-прежнему отвечает
    await exec_fakes.until(lambda: s.telegram.sent())


def test_audit_knows_every_app_action():
    import re
    from importlib import resources

    source = (resources.files("shturman.setup_page") / "miniapp.py").read_text(encoding="utf-8")
    used = set(re.findall(r'_log\(request, "([a-z_.]+)"', source))
    assert used == {"app.login", "app.login_failed", "app.logout", "app.decide"}
    assert used <= set(audit.ACTIONS) and {"app.login", "app.decide"} <= audit.IMPORTANT
    assert approvals.REF.fullmatch("sh:ob:s:12:" + "a" * 22)


async def test_digests_and_hidden_messages_are_decided_with_the_buttons_the_bot_sent(stand, conn):
    import sys
    from datetime import date
    from pathlib import Path

    processing = str(Path(__file__).resolve().parents[1] / "processing")
    if processing not in sys.path:
        sys.path.insert(0, processing)
    from pages_helpers import commitment, seed

    from shturman.guard import alerts
    from shturman.processing import commitments, facts, projects

    s = await ready(stand, conn)
    w = await seed(conn)
    signed_up = await commitment(conn, w.maria_chat, w.maria_msgs[0], debtor=w.maria_peer, creditor=w.owner_peer,
                                 direction="owed_to_owner", what="подписать акт сверки", accept=False,
                                 due_expression="в понедельник", quote="Акт сверки подпишу в понедельник.")
    changed = await commitment(conn, w.ivan_chat, w.ivan_msgs[0], debtor=w.ivan_peer, creditor=w.owner_peer,
                               direction="owed_to_owner", what="прислать смету", accept=False, quote="Пришлю смету.")
    assert await commitments.build_digests(conn, run_id=1, today=date.today())        # ушли сводкой
    later = await commitment(conn, w.ivan_chat, w.ivan_msgs[2], debtor=w.ivan_peer, creditor=w.owner_peer,
                             direction="owed_to_owner", what="начать монтаж", accept=False)
    fact = await conn.fetchval(
        """INSERT INTO facts (subject_type, kind, slot, text, text_norm, valid_from, status, origin,
                              source_message_id, source_quote)
           VALUES ('owner', 'fact', 'должность', 'генеральный директор', $1, $2, 'proposed', 'owner', $3,
                   'генеральный директор') RETURNING id""",
        facts.text_norm("генеральный директор"), date(2026, 10, 6), w.ivan_msgs[1])
    assert await facts.send_owner_digest(conn, run_id=1) == 1
    project = (await projects.create_project(conn, "Береговой", [w.ivan_chat], origin="model",
                                             reason={"messages": 25}))["project"]["id"]
    assert await projects.send_digest(conn, run_id=1) == 1
    await conn.execute("UPDATE messages SET agent_visible = false, guard_label = 'suspect' WHERE id = $1",
                       w.maria_msgs[1])
    assert (await alerts.pump(conn))["sent"] == 1
    await _delivered(s)
    # содержание одного обязательства изменилось после сводки: его «Верно» должно отказать
    await conn.execute("UPDATE commitments SET what = 'прислать смету и акт' WHERE id = $1", changed)

    await s.app.sign_in(signed())
    listed = (await s.app.get("/approvals")).json()
    kinds = sorted(i["kind"] for i in listed["items"])
    assert kinds == ["commitment", "commitment", "hidden", "owner_fact", "project"], listed
    assert listed["later"]["commitment"] == 1
    assert later not in [i["id"] for i in listed["items"] if i["kind"] == "commitment"]
    refs = {(i["kind"], i["id"], a["label"]): a["ref"] for i in listed["items"] for a in i["actions"]}
    # что увидит человек: заголовок — вопрос, а не повтор вида карточки; без дат ISO, кодов кнопок,
    # пустых значений и словарей
    import re
    for item in listed["items"]:
        assert item["title"] != item["kind_title"] and not item["title"].startswith(item["kind_title"]), item
        shown = "\n".join([item["title"], item["text"], *item["head"], item["note"] or ""])
        assert not re.search(r"\d{4}-\d{2}-\d{2}|\bsh:|\bNone\b|\bnull\b|[{}]", shown), shown
    hidden = next(i for i in listed["items"] if i["kind"] == "hidden")
    assert "Показать ассистенту" in [a["label"] for a in hidden["actions"]] and "Начало текста" in hidden["text"]

    async def press(kind, target, label):
        got = await s.app.post("/approvals/decide", {"ref": refs[(kind, target, label)]})
        assert got.status_code == 200, got.text
        return got.json()["answer"]

    assert await press("commitment", signed_up, "Верно") == "Принято."
    assert await conn.fetchval("SELECT status FROM commitments WHERE id = $1", signed_up) == "open"
    stale = await press("commitment", changed, "Верно")
    assert stale != "Принято."
    assert await conn.fetchval("SELECT status FROM commitments WHERE id = $1", changed) == "proposed"
    assert await press("owner_fact", fact, "Запомнить") == "Запомнено."
    assert await conn.fetchval("SELECT status FROM facts WHERE id = $1", fact) == "active"
    assert await press("project", project, "Завести проект") == "Проект заведён."
    assert await conn.fetchval("SELECT status FROM projects WHERE id = $1", project) == "active"
    alert = hidden["id"]
    card_id = await conn.fetchval("SELECT card_message_id FROM guard_alerts WHERE id = $1", alert)
    assert await press("hidden", alert, "Оставить скрытым") == "Оставлено скрытым."
    assert await conn.fetchval("SELECT status FROM guard_alerts WHERE id = $1", alert) == "confirmed"
    assert not await conn.fetchval("SELECT agent_visible FROM messages WHERE id = $1", w.maria_msgs[1])
    await _delivered(s)
    assert any(e["message_id"] == card_id for e in s.telegram.calls("editMessageText"))
    left = (await s.app.get("/approvals")).json()["items"]
    assert [(i["kind"], i["id"]) for i in left] == [("commitment", changed)]
    dumped = json.dumps(await app_audit(conn), ensure_ascii=False)
    assert not any(word in dumped for word in ("Мария", "Иван", "смет", "Береговой", "директор"))


# --- что видит человек ------------------------------------------------------------------------------

NAVIGATE = {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document", "Sec-Fetch-Site": "none",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


async def test_a_person_opening_a_service_address_sees_a_page_not_json(stand, conn):
    """Адрес API, устаревшая ссылка, неверный путь, чужое имя узла — человек в браузере или в окне
    Telegram видит короткую страницу по-русски, а не `{"error": …}`. Код ответа и решение те же; запрос
    скрипта получает JSON, как раньше; в странице нет ничего из запроса."""
    s = await ready(stand, conn)
    await s.app.sign_in(signed())
    http = s.app.http
    cases = [
        (APP_API + "/approvals", 403, "приложение «Штурман» получает данные"),
        (APP_API + "/login", 405, "приложение «Штурман» получает данные"),
        (APP_PATH + "nothing-marker", 404, "кнопкой <strong>«Штурман»</strong>"),
        (API + "/state", 403, "страница «Переписка» получает данные"),
        (PREFIX + "/nothing-marker", 404, "Открыть страницу «Переписка»"),
        (PREFIX + "//app/", 404, "Такой страницы нет"),
    ]
    for path, status, words in cases:
        got = await http.get(path, headers=NAVIGATE)
        assert got.status_code == status, path
        assert got.headers["content-type"].startswith("text/html"), path
        assert words in got.text and f"Код ответа: {status}." in got.text, path
        assert "{" not in got.text and "marker" not in got.text, path
        assert got.headers["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in got.headers["content-security-policy"]
        assert "<script" not in got.text and " style=" not in got.text
    # браузер без заголовков Sec-Fetch (старый WebView) — по заголовку Accept
    old = await http.get(APP_API + "/approvals", headers={"Accept": "text/html"})
    assert old.status_code == 401 and old.headers["content-type"].startswith("text/html")
    # чужое имя узла: страница, но о сервисе — ни слова
    other = await http.get(APP_PATH, headers={**NAVIGATE, "Host": "evil.example"})
    assert other.status_code == 421 and other.headers["content-type"].startswith("text/html")
    assert "Штурман" not in other.text and "Переписка" not in other.text and "/shturman-setup" not in other.text
    # запросы скриптов — по-прежнему JSON; сессия приложения работает как раньше
    for headers in ({"Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin", "Accept": "*/*"}, {}):
        got = await http.get(APP_API + "/approvals", headers=headers)
        assert got.status_code == 401 and got.json()["code"] == "unauthenticated"
    assert (await http.get(PREFIX + "/", headers={"Host": "evil.example"})).json() == {"error": "misdirected"}
    assert (await s.app.get("/approvals")).status_code == 200
    # сама страница приложения при переходе — та же страница, а не замена
    page = await http.get(APP_PATH, headers=NAVIGATE)
    assert page.status_code == 200 and "Согласования" in page.text and "Код ответа" not in page.text


async def test_app_address_without_trailing_slash_redirects_relatively(stand, conn):
    """За обратным прокси схема запроса у сервиса — http: абсолютное перенаправление увело бы браузер на
    http:// по порту https. Поэтому — относительный адрес."""
    s = await ready(stand, conn)
    got = await s.app.http.get(PREFIX + "/app", headers=NAVIGATE, follow_redirects=False)
    assert got.status_code == 308 and got.headers["location"] == APP_PATH
    assert got.headers["x-frame-options"] == "DENY"


def test_cards_speak_plain_russian():
    """Даты в карточках — по-человечески, а не записью ISO."""
    from datetime import date, datetime, timezone

    from shturman.processing import facts
    from shturman.replies import workflow

    row = {"slot": "роль", "text": "отвечаю за бюджет фасадов", "valid_from": date(2026, 10, 6),
           "source_outgoing": True, "source_quote": "Я отвечаю за бюджет фасадов."}
    assert facts._item_text(row) == ("роль: отвечаю за бюджет фасадов (с 06.10.2026; сказали вы)\n"
                                     "«Я отвечаю за бюджет фасадов.»")
    moment = workflow._moment(datetime(2026, 10, 1, tzinfo=timezone.utc).isoformat(), timezone.utc)
    assert moment == "01.10.2026 00:00 (UTC)"
