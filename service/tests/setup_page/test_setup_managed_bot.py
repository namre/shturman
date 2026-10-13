"""Бот согласований в одно нажатие через бот-менеджер посредника (Р-77, setup_page/bot_setup.py).

Посредник здесь подставной (httpx.MockTransport с API relayd 0.4.0: /api/botpair/start и
/api/botpair/<pair_id>/token); Telegram сервиса — подставной из tests/executor. Стенд с настоящей
службой посредника — test_setup_managed_bot_bench.py.
"""

from __future__ import annotations

import json
import logging
import re

import httpx

from setup_fakes import BUSY_TOKEN, TOKEN, bind_owner, save_bot, stand  # noqa: F401, I001 — stand — фикстура; первым: добавляет пути
from exec_fakes import BOT_ID, BOT_NAME, IVAN, IVAN_USER, OWNER, OWNER_USER, until

from shturman.executor import binding
from shturman.executor import service as executor_service
from shturman.executor.bot import TEXT_NOT_CREATOR
from shturman.setup_page import bot_setup
from shturman.setup_page import secrets_store as ss

RELAY = "https://relay.test"
SECRET = "S" * 20 + "ecret-of-the-pair_" + "x" * 10          # 48 знаков, как token_urlsafe(32) с запасом
PAIR = "abcdefgh23"
USERNAME = f"shturman_{PAIR}_bot"


class FakeRelay:
    """Подставной посредник: пара, ожидание и одна выдача токена — как у relayd 0.4.0."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []
        self.created: dict | None = None
        self.start_answer: httpx.Response | None = None
        self.token_answers: list[httpx.Response] = []
        self.delivered = False
        self.ready = True
        self.health_asked = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def create(self, *, token: str = TOKEN, bot_id: int = BOT_ID, username: str = BOT_NAME,
               creator: dict = OWNER_USER, restricted: bool = True) -> None:
        """Владелец подтвердил создание в Telegram: посредник получил managed_bot."""
        self.created = {"status": "ready", "token": token,
                        "bot": {"id": bot_id, "username": username, "name": "Согласования"},
                        "creator": {"id": creator["id"], "name": creator.get("first_name", "")},
                        "restricted": restricted}

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/health":
            self.health_asked += 1
            return httpx.Response(200, json={"ok": True, "version": "0.4.0", "botpair": self.ready})
        body = json.loads(request.content or b"{}")
        self.requests.append((request.url.path, body))
        assert str(request.url).startswith(RELAY + "/api/botpair/")
        if request.url.path == "/api/botpair/start":
            if self.start_answer is not None:
                return self.start_answer
            return httpx.Response(200, json={"pair_id": PAIR, "secret": SECRET, "username": USERNAME,
                                             "name": body.get("name"), "manager": "shturman_manager_bot",
                                             "link": "https://evil.example/not-used", "expires_at": 0, "ttl": 900})
        assert request.url.path == f"/api/botpair/{PAIR}/token"
        if body.get("secret") != SECRET:
            return httpx.Response(404, json={"error": "not_found"})
        if self.token_answers:
            return self.token_answers.pop(0)
        if self.delivered:
            return httpx.Response(404, json={"error": "not_found"})
        if self.created is None:
            return httpx.Response(202, json={"status": "waiting"})
        self.delivered = True
        return httpx.Response(200, json=self.created)


async def managed_stand(stand, conn, **changes):
    relay = FakeRelay()
    s = await stand(bot_manager_api=RELAY, **changes)
    executor_service.TEST_OVERRIDES.update(botpair_transport=relay.transport(), botpair_poll=0.01)
    bot_setup._health.clear()
    await s.page.login(conn)
    s.relay = relay
    return s


async def audit_rows(conn) -> list[tuple[str, str, str]]:
    rows = await conn.fetch("SELECT action, outcome, detail FROM setup_audit ORDER BY id")
    return [(r["action"], r["outcome"], r["detail"]) for r in rows]


async def attempt(s) -> dict | None:
    return (await s.page.get("/state")).json()["bot"]["managed"]["attempt"]


async def test_one_tap_bot_is_applied_by_the_same_path_as_a_pasted_token(stand, conn, caplog):
    caplog.set_level(logging.DEBUG)
    s = await managed_stand(stand, conn)
    state = (await s.page.get("/state")).json()["bot"]
    assert state["managed"] == {"available": True, "host": "relay.test", "ready": None, "attempt": None}
    await until(lambda: s.relay.health_asked == 1)                    # готовность посредника — в фоне
    await until(lambda: bot_setup._health.get(RELAY, (0, None))[1] is True)
    assert (await s.page.get("/state")).json()["bot"]["managed"]["ready"] is True
    assert s.relay.health_asked == 1                                  # не чаще раза в 10 минут
    started = await s.page.post("/bot/managed", {"name": "Мой пульт"})
    assert started.status_code == 200, started.text
    view = started.json()["attempt"]
    # Ссылку сервис собирает сам из имён — ссылке посредника не верит.
    assert view["link"] == f"https://t.me/newbot/shturman_manager_bot/{USERNAME}?name=%D0%9C%D0%BE%D0%B9%20%D0%BF%D1%83%D0%BB%D1%8C%D1%82"
    assert view["status"] == "waiting" and view["seconds_left"] > 800
    assert s.relay.requests[0] == ("/api/botpair/start", {"name": "Мой пульт"})
    await until(lambda: len(s.relay.requests) >= 3)                   # сервис сам спрашивает посредника
    assert (await attempt(s))["status"] == "waiting"
    s.relay.create()
    await until(lambda: s.state.config.bot_token == TOKEN)
    await until(lambda: s.state.extras["executor"].bot is not None and s.state.extras["executor"].bot.polling)
    # Тот же путь, что у вставленного токена: getMe, затем пробный getUpdates («не опрашивает ли бота
    # другая программа») — до того, как бот начал работать.
    assert [m for m, _ in s.telegram.requests[:3]] == ["getMe", "getUpdates", "getMe"]
    assert ss.SecretStore(s.config.data_dir).load()[ss.BOT_TOKEN] == TOKEN
    state = (await s.page.get("/state")).json()["bot"]
    assert state["configured"] and state["created_via"] == "managed" and state["creator_name"] == "Евгений"
    assert state["restricted"] is True and state["manager"] == "relay.test"
    assert state["managed"]["attempt"]["status"] == "done"
    origin = await binding.bot_origin(conn, BOT_ID)
    assert origin["created_via"] == "managed" and origin["creator_user_id"] == OWNER and origin["manager"] == "shturman_manager_bot"
    rows = await audit_rows(conn)
    assert ("bot.managed_start", "ok", "бот-менеджер @shturman_manager_bot посредника relay.test") in rows
    assert any(a == "bot.token" and o == "ok" and "в одно нажатие" in d for a, o, d in rows)
    status = (await s.api.get("/api/executor/status")).json()["bot"]
    assert status["created_via"] == "managed"
    # Ни секрета пары, ни токена — ни в ответах страницы, ни в журнале действий, ни в журнале сервиса.
    everything = json.dumps(rows, ensure_ascii=False) + caplog.text + started.text + json.dumps(state)
    assert SECRET not in everything and TOKEN not in everything
    assert SECRET not in json.dumps((await s.page.get("/overview")).json(), ensure_ascii=False)


async def test_only_the_creator_can_become_the_owner(stand, conn):
    s = await managed_stand(stand, conn)
    await s.page.post("/bot/managed")
    s.relay.create(creator=IVAN_USER)
    await until(lambda: s.state.extras["executor"].bot is not None and s.state.extras["executor"].bot.identity)
    state = (await s.page.get("/state")).json()["bot"]
    assert state["creator_name"] == "Иван"
    link = (await s.page.post("/bot/bind")).json()["link"]
    code = link.split("start=")[1]
    s.telegram.text("/start " + code, user=OWNER_USER)
    await until(lambda: any(p["text"] == TEXT_NOT_CREATOR for p in s.telegram.calls("sendMessage")))
    assert (await s.page.get("/state")).json()["bot"]["owner_bound"] is False
    assert await binding.code_valid(conn, code)                       # код не погашен
    assert s.state.extras["executor"].bot.counters["bind_not_creator"] == 1
    assert not s.state.extras["executor"].bot.flood.locked()
    # Создатель привязывается той же ссылкой.
    s.telegram.text("/start " + code, user=IVAN_USER)
    await until(lambda: _owner_is(s, IVAN))


async def _owner_is(s, user_id: int) -> bool:
    return (await s.page.get("/state")).json()["bot"]["owner_bound"] and \
        (await binding.bound_owner(s.conn, BOT_ID) or {}).get("user_id") == user_id


async def test_creator_rule_ends_when_token_is_pasted_by_hand(stand, conn):
    """Переход на своего бота: токен вставлен вручную — записано «вручную», правило создателя снято."""
    s = await managed_stand(stand, conn)
    await s.page.post("/bot/managed")
    s.relay.create(creator=IVAN_USER)
    await until(lambda: s.state.config.bot_token == TOKEN)
    assert await binding.required_creator(conn, BOT_ID) == IVAN
    assert (await save_bot(s)).json()["status"] == "saved"
    assert await binding.required_creator(conn, BOT_ID) is None
    assert (await binding.bot_origin(conn, BOT_ID))["created_via"] == "manual"
    await bind_owner(s)                                                 # владелец — любой, кто открыл ссылку
    assert (await s.page.get("/state")).json()["bot"]["created_via"] == "manual"


async def test_token_of_another_bot_is_refused(stand, conn):
    s = await managed_stand(stand, conn)
    await s.page.post("/bot/managed")
    s.relay.create(bot_id=BOT_ID + 5)                                  # посредник назвал другой номер бота
    await until(lambda: (_ := s.state.extras["setup_page"].managed) is not None and _.status == "failed")
    a = await attempt(s)
    assert a["code"] == "wrong_bot" and s.state.config.bot_token == ""
    assert await binding.bot_origin(conn, BOT_ID) is None
    assert ("bot.token", "refused", "токен не того бота, о котором сообщил посредник") in await audit_rows(conn)


async def test_bot_already_polled_by_another_program_is_refused_like_a_pasted_one(stand, conn):
    s = await managed_stand(stand, conn)
    await s.page.post("/bot/managed")
    s.relay.create(token=BUSY_TOKEN, bot_id=7000000002, username="ivan_assistant_bot")
    await until(lambda: s.state.extras["setup_page"].managed.status == "failed")
    a = await attempt(s)
    assert a["code"] == "other_poller" and "другая программа" in a["message"]
    assert s.state.config.bot_token == ""
    assert ("bot.token", "refused", "ботом уже пользуется другая программа") in await audit_rows(conn)


async def test_logging_out_everywhere_cancels_the_attempt(stand, conn):
    s = await managed_stand(stand, conn)
    await s.page.post("/bot/managed")
    await until(lambda: len(s.relay.requests) >= 2)
    assert (await s.page.post("/logout-all")).status_code == 200
    assert s.state.extras["setup_page"].managed is None
    seen = len(s.relay.requests)
    s.relay.create()
    await s.page.http.get("/shturman-setup/")                          # время на то, чтобы опрос (если бы шёл) спросил
    assert len(s.relay.requests) == seen and s.state.config.bot_token == ""


async def test_token_is_not_applied_after_the_session_ended(stand, conn):
    s = await managed_stand(stand, conn)
    await s.page.post("/bot/managed")
    await conn.execute("UPDATE setup_sessions SET revoked_at = now()")   # сессия кончилась мимо страницы
    s.relay.create()
    await until(lambda: s.state.extras["setup_page"].managed.status == "failed")
    assert s.state.extras["setup_page"].managed.code == "session_ended"
    assert s.state.config.bot_token == "" and not s.telegram.calls("getMe")
    assert ("bot.managed", "refused", "вход на страницу завершился до создания бота") in await audit_rows(conn)


async def test_cancel_and_restart(stand, conn):
    s = await managed_stand(stand, conn)
    await s.page.post("/bot/managed")
    assert (await s.page.delete("/bot/managed")).status_code == 200
    assert await attempt(s) is None
    assert [a for a, _, _ in await audit_rows(conn)].count("bot.managed_cancel") == 1
    assert (await s.page.post("/bot/managed")).status_code == 200
    assert (await attempt(s))["status"] == "waiting"


async def test_relay_refusals_are_explained(stand, conn):
    s = await managed_stand(stand, conn)
    cases = [
        (httpx.Response(503, json={"error": "manager_unavailable", "message": "x"}), "manager_unavailable", "не работает"),
        (httpx.Response(403, json={"error": "invite_required", "message": "x"}), "invite_required", "приглашения"),
        (httpx.Response(429, json={"error": "rate_limited"}), "rate_limited", "подождать"),
        (httpx.Response(500, text="oops"), "bad_answer", "непонятно"),
        # Имя бота, в котором нет номера пары, или менеджер с чужими знаками — ответ не принимается.
        (httpx.Response(200, json={"pair_id": PAIR, "secret": SECRET, "username": "evil_bot", "manager": "m_bot", "ttl": 900}),
         "bad_answer", "непонятно"),
        (httpx.Response(200, json={"pair_id": PAIR, "secret": SECRET, "username": USERNAME, "manager": "bad/name", "ttl": 900}),
         "bad_answer", "непонятно"),
    ]
    for answer, code, words in cases:
        s.relay.start_answer = answer
        got = await s.page.post("/bot/managed", {"code": "ABCD-EFGH"})
        assert got.status_code == 422 and got.json()["code"] == code and words in got.json()["error"], (code, got.text)
    assert s.relay.requests[1][1] == {"name": "Согласования", "code": "ABCD-EFGH"}
    assert await attempt(s) is None
    s.relay.start_answer = None
    executor_service.TEST_OVERRIDES["botpair_transport"] = httpx.MockTransport(_unreachable)
    got = await s.page.post("/bot/managed")
    assert got.json()["code"] == "no_connection"


def _unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("нет связи", request=request)


async def test_expired_pair_and_temporary_errors(stand, conn):
    s = await managed_stand(stand, conn)
    s.relay.token_answers = [httpx.Response(502, json={"error": "telegram"}), httpx.Response(429, json={}),
                             httpx.Response(404, json={"error": "not_found"})]
    await s.page.post("/bot/managed")
    await until(lambda: s.state.extras["setup_page"].managed.status == "failed")
    a = await attempt(s)
    assert a["code"] == "expired" and "не меняя адрес" in a["message"] and a["link"] is None
    assert s.state.extras["setup_page"].managed.secret == ""           # секрет пары больше не держится


async def test_one_tap_can_be_switched_off_by_the_operator(stand, conn):
    s = await stand(bot_manager_api="")
    await s.page.login(conn)
    assert (await s.page.get("/state")).json()["bot"]["managed"]["available"] is False
    got = await s.page.post("/bot/managed")
    assert got.status_code == 422 and got.json()["code"] == "off"


async def test_internal_api_cannot_start_or_see_the_pairing(stand, conn):
    s = await managed_stand(stand, conn)
    await s.page.post("/bot/managed")
    for method in ("POST", "DELETE", "GET"):
        got = await s.api.request(method, "/shturman-setup/api/bot/managed")
        assert got.status_code in (401, 403, 405), (method, got.status_code)
    status = json.dumps((await s.api.get("/api/executor/status")).json())
    assert SECRET not in status and "t.me/newbot" not in status
    assert not re.search(r"/api/(bot/managed|botpair)", json.dumps([str(r.path) for r in s.api._transport.app.inner.routes
                                                                     if hasattr(r, "path") and r.path.startswith("/api/")]))


def test_bot_manager_address_comes_only_from_the_environment(monkeypatch):
    from shturman.config import ConfigError, bot_manager_api

    assert bot_manager_api("") == "https://relay.sh.namre.app"
    assert bot_manager_api("off") == ""
    assert bot_manager_api("https://Relay.Example.org/") == "https://relay.example.org"
    assert bot_manager_api("http://127.0.0.1:7401") == "http://127.0.0.1:7401"
    for bad in ("http://relay.example.org", "relay.example.org", "https://relay.example.org/api"):
        try:
            bot_manager_api(bad)
        except ConfigError:
            continue
        raise AssertionError(bad)


async def test_relay_without_manager_hides_the_button(stand, conn):
    s = await managed_stand(stand, conn)
    s.relay.ready = False
    await s.page.get("/state")
    await until(lambda: bot_setup._health.get(RELAY, (0, None))[1] is False)
    assert (await s.page.get("/state")).json()["bot"]["managed"]["ready"] is False
    # Кнопка, нажатая до того, как страница узнала, — понятный отказ, и готовность запоминается.
    s.relay.start_answer = httpx.Response(503, json={"error": "manager_unavailable"})
    bot_setup._health.clear()
    assert (await s.page.post("/bot/managed")).json()["code"] == "manager_unavailable"
    assert bot_setup._health[RELAY][1] is False
