"""Бот согласований в одно нажатие через бот-менеджер посредника (Р-77, setup_page/bot_setup.py).

Посредник здесь подставной (httpx.MockTransport с API relayd 0.4.0: /api/health, /api/botpair/start и
/api/botpair/<pair_id>/token); Telegram сервиса — подставной из tests/executor. Стенд с настоящей
службой посредника — test_setup_managed_bot_bench.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

import httpx

from setup_fakes import TOKEN, bind_owner, save_bot, stand  # noqa: F401, I001 — stand — фикстура; первым: добавляет пути
from exec_fakes import BOT_ID, IVAN, IVAN_USER, OWNER, OWNER_USER, refusal, until

from shturman.executor import binding
from shturman.executor import service as executor_service
from shturman.executor.bot import TEXT_NOT_CREATOR
from shturman.setup_page import apply, bot_setup
from shturman.setup_page import secrets_store as ss

RELAY = "https://relay.test"
SECRET = "S" * 20 + "ecret-of-the-pair_" + "x" * 10          # 48 знаков, как token_urlsafe(32) с запасом
PAIR = "abcdefgh23"
USERNAME = f"shturman_{PAIR}_bot"
CLAIM = "k7m2p9q4r8s3"
OWNER_TG = {**OWNER_USER, "username": "evgeny_t"}


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

    def create(self, *, token: str = TOKEN, bot_id: int = BOT_ID, username: str = USERNAME,
               creator: dict = OWNER_TG, restricted: bool = True, claimed: bool = False) -> None:
        """Аккаунт `creator` подтвердил создание в Telegram: посредник получил managed_bot."""
        self.created = {"status": "ready", "token": token,
                        "bot": {"id": bot_id, "username": username, "name": "Согласования"},
                        "creator": {"id": creator["id"], "username": creator.get("username"),
                                    "name": creator.get("first_name", "")},
                        "restricted": restricted, "claimed": claimed}

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
                                             "link": "https://evil.example/not-used", "expires_at": 0, "ttl": 900,
                                             "claim": CLAIM})
        if request.url.path == f"/api/botpair/{PAIR}/cancel":
            return httpx.Response(200 if body.get("secret") == SECRET else 404, json={})
        assert request.url.path == f"/api/botpair/{PAIR}/token"
        if body.get("secret") != SECRET:
            return httpx.Response(404, json={"error": "not_found"})
        if self.token_answers:
            return self.token_answers.pop(0)
        if self.delivered:
            return httpx.Response(404, json={"error": "not_found"})
        if self.created is None:
            return httpx.Response(202, json={"status": "waiting", "expires_at": int(time.time()) + 900})
        self.delivered = True
        return httpx.Response(200, json=self.created)


async def managed_stand(stand, conn, **changes):
    relay = FakeRelay()
    s = await stand(bot_manager_api=RELAY, **changes)
    executor_service.TEST_OVERRIDES.update(botpair_transport=relay.transport(), botpair_poll=0.01)
    bot_setup._health.clear()
    s.telegram.me["username"] = USERNAME            # бот, созданный по ссылке, — с именем из ссылки
    await s.page.login(conn)
    s.relay = relay
    return s


async def audit_rows(conn) -> list[tuple[str, str, str]]:
    rows = await conn.fetch("SELECT action, outcome, detail FROM setup_audit ORDER BY id")
    return [(r["action"], r["outcome"], r["detail"]) for r in rows]


async def attempt(s) -> dict | None:
    return (await s.page.get("/state")).json()["bot"]["managed"]["attempt"]


def pairing(s):
    return s.state.extras["setup_page"].managed


async def until_status(s, status: str) -> None:
    await until(lambda: pairing(s) is not None and pairing(s).status == status)


async def test_one_tap_bot_waits_for_the_owner_and_then_takes_the_manual_path(stand, conn, caplog):
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
    s.relay.create()
    await until_status(s, "confirm")
    # Токен получен, проверен getMe — но не сохранён, пока владелец не узнает свой аккаунт.
    assert s.state.config.bot_token == "" and not ss.SecretStore(s.config.data_dir).path.exists()
    a = await attempt(s)
    assert a["creator"] == {"id": OWNER, "username": "evgeny_t", "name": "Евгений"}
    assert a["bot_username"] == USERNAME and 500 < a["confirm_seconds_left"] <= 600 and a["link"] is None
    assert [m for m, _ in s.telegram.requests] == ["getMe"]
    done = await s.page.post("/bot/managed/confirm", {"accept": True})
    assert done.status_code == 200 and done.json()["attempt"]["status"] == "done", done.text
    await until(lambda: s.state.extras["executor"].bot is not None and s.state.extras["executor"].bot.polling)
    # Тот же путь, что у вставленного токена: getMe, затем пробный getUpdates («не опрашивает ли бота
    # другая программа») — до того, как бот начал работать.
    assert [m for m, _ in s.telegram.requests[:4]] == ["getMe", "getMe", "getUpdates", "getMe"]
    assert ss.SecretStore(s.config.data_dir).load()[ss.BOT_TOKEN] == TOKEN
    state = (await s.page.get("/state")).json()["bot"]
    assert state["configured"] and state["created_via"] == "managed" and state["creator_name"] == "Евгений"
    assert state["creator_username"] == "evgeny_t" and state["restricted"] is True and state["manager"] == "relay.test"
    origin = await binding.bot_origin(conn, BOT_ID)
    assert origin["created_via"] == "managed" and origin["creator_user_id"] == OWNER and origin["manager"] == "shturman_manager_bot"
    rows = await audit_rows(conn)
    assert ("bot.managed_start", "ok", "бот-менеджер @shturman_manager_bot посредника relay.test") in rows
    assert ("bot.managed", "ok", "бот создан; ждёт подтверждения, что создан вашим аккаунтом") in rows
    assert any(a == "bot.token" and o == "ok" and "подтверждён владельцем" in d and "@evgeny_t" in d for a, o, d in rows)
    status = (await s.api.get("/api/executor/status")).json()["bot"]
    assert status["created_via"] == "managed"
    # Ни секрета пары, ни токена — ни в ответах страницы, ни в журнале действий, ни в журнале сервиса.
    everything = json.dumps(rows, ensure_ascii=False) + caplog.text + started.text + done.text + json.dumps(state)
    assert SECRET not in everything and TOKEN not in everything
    assert SECRET not in json.dumps((await s.page.get("/overview")).json(), ensure_ascii=False)


async def test_bot_created_by_someone_else_is_never_accepted_silently(stand, conn):
    """Ссылку подсмотрели и бота создал посторонний: токен ждёт ответа владельца, «Нет, не мой» —
    токен выбрасывается, бот не используется."""
    s = await managed_stand(stand, conn)
    await s.page.post("/bot/managed")
    s.relay.create(creator={"id": 6666, "first_name": "Некто", "username": "thief"})
    await until_status(s, "confirm")
    await asyncio.sleep(0.2)
    assert s.state.config.bot_token == ""                              # без ответа владельца — не сохранён
    assert (await attempt(s))["creator"]["username"] == "thief"
    got = await s.page.post("/bot/managed/confirm", {"accept": False})
    assert got.status_code == 200
    a = got.json()["attempt"]
    assert a["status"] == "intercepted" and "Бота создал кто-то другой" in a["message"] and "он не ваш" in a["message"]
    assert pairing(s).token == "" and s.state.config.bot_token == ""
    assert await binding.bot_origin(conn, BOT_ID) is None
    assert any(a == "bot.managed" and o == "refused" and "не узнал" in d for a, o, d in await audit_rows(conn))
    assert (await s.page.post("/bot/managed/confirm", {"accept": True})).status_code == 422   # поздно


async def test_unconfirmed_token_expires(stand, conn):
    s = await managed_stand(stand, conn)
    await s.page.post("/bot/managed")
    s.relay.create()
    await until_status(s, "confirm")
    pairing(s).confirm_until = time.time() - 1
    a = await attempt(s)
    assert a["status"] == "failed" and a["code"] == "confirm_expired" and pairing(s).token == ""
    got = await s.page.post("/bot/managed/confirm", {"accept": True})
    assert got.status_code == 422 and got.json()["code"] == "confirm_expired"
    assert s.state.config.bot_token == ""


async def test_only_the_creator_can_become_the_owner(stand, conn):
    s = await managed_stand(stand, conn)
    await s.page.post("/bot/managed")
    s.relay.create(creator=IVAN_USER)                                  # второй аккаунт самого владельца
    await until_status(s, "confirm")
    assert (await s.page.post("/bot/managed/confirm", {"accept": True})).status_code == 200
    await until(lambda: s.state.extras["executor"].bot is not None and s.state.extras["executor"].bot.identity)
    assert (await s.page.get("/state")).json()["bot"]["creator_name"] == "Иван"
    code = (await s.page.post("/bot/bind")).json()["link"].split("start=")[1]
    s.telegram.text("/start " + code, user=OWNER_USER)
    await until(lambda: any(p["text"] == TEXT_NOT_CREATOR for p in s.telegram.calls("sendMessage")))
    state = (await s.page.get("/state")).json()["bot"]
    assert state["owner_bound"] is False and state["bind_refused_at"] is not None
    assert not await binding.code_valid(conn, code)                    # ссылка отменена
    assert s.state.extras["executor"].bot.counters["bind_not_creator"] == 1
    assert not s.state.extras["executor"].bot.flood.locked()
    assert ("bot.bind_refused", "refused", "ссылку привязки открыл не создатель бота") in await audit_rows(conn)
    s.telegram.text("/start " + code, user=IVAN_USER)                  # отменённая ссылка не действует и для создателя
    # Новая ссылка, открытая создателем, — привязка; отметка об отказе исчезает.
    code = (await s.page.post("/bot/bind")).json()["link"].split("start=")[1]
    s.telegram.text("/start " + code, user=IVAN_USER)
    await until(lambda: _owner_is(s, IVAN))
    assert (await s.page.get("/state")).json()["bot"]["bind_refused_at"] is None


async def _owner_is(s, user_id: int) -> bool:
    return (await s.page.get("/state")).json()["bot"]["owner_bound"] and \
        (await binding.bound_owner(s.conn, BOT_ID) or {}).get("user_id") == user_id


async def test_creator_rule_ends_when_token_is_pasted_by_hand(stand, conn):
    """Переход на своего бота: токен вставлен вручную — записано «вручную», правило создателя снято."""
    s = await managed_stand(stand, conn)
    await s.page.post("/bot/managed")
    s.relay.create(creator=IVAN_USER)
    await until_status(s, "confirm")
    await s.page.post("/bot/managed/confirm", {"accept": True})
    assert await binding.required_creator(conn, BOT_ID) == IVAN
    assert (await save_bot(s)).json()["status"] == "saved"
    assert await binding.required_creator(conn, BOT_ID) is None
    assert (await binding.bot_origin(conn, BOT_ID))["created_via"] == "manual"
    await bind_owner(s)                                                 # владелец — любой, кто открыл ссылку
    assert (await s.page.get("/state")).json()["bot"]["created_via"] == "manual"


async def test_token_must_be_of_the_bot_named_in_our_link(stand, conn):
    # Посредник сообщил о боте с другим именем, чем сервис вписал в ссылку.
    s = await managed_stand(stand, conn)
    await s.page.post("/bot/managed")
    s.relay.create(username="other_name_bot")
    await until_status(s, "failed")
    assert pairing(s).code == "wrong_bot" and not s.telegram.calls("getMe")
    # Имя то же, но getMe говорит о боте с другим номером, чем сообщил посредник.
    s.relay = FakeRelay()
    executor_service.TEST_OVERRIDES["botpair_transport"] = s.relay.transport()
    await s.page.post("/bot/managed")
    s.relay.create(bot_id=BOT_ID + 5)
    await until_status(s, "failed")
    # И токен бота с номером из сообщения, но с другим именем (getMe): тоже нет.
    s.relay = FakeRelay()
    executor_service.TEST_OVERRIDES["botpair_transport"] = s.relay.transport()
    s.telegram.me["username"] = "renamed_bot"
    await s.page.post("/bot/managed")
    s.relay.create()
    await until_status(s, "failed")
    assert pairing(s).code == "wrong_bot" and s.state.config.bot_token == ""
    assert await binding.bot_origin(conn, BOT_ID) is None
    assert [d for a, o, d in await audit_rows(conn) if a == "bot.managed" and o == "refused"] == [
        "посредник сообщил о боте с другим именем", "токен не того бота, который создавался по ссылке",
        "токен не того бота, который создавался по ссылке"]


async def test_token_of_another_bot_is_refused(stand, conn):
    s = await managed_stand(stand, conn)
    await s.page.post("/bot/managed")
    s.relay.create(bot_id=BOT_ID + 5)                                  # посредник назвал другой номер бота
    await until_status(s, "failed")
    a = await attempt(s)
    assert a["code"] == "wrong_bot" and s.state.config.bot_token == ""
    assert await binding.bot_origin(conn, BOT_ID) is None


async def test_bot_already_polled_by_another_program_is_refused_like_a_pasted_one(stand, conn):
    s = await managed_stand(stand, conn)
    await s.page.post("/bot/managed")
    s.relay.create()
    await until_status(s, "confirm")
    s.telegram.script["getUpdates"] = [refusal(409, "Conflict: terminated by other getUpdates request")]
    got = await s.page.post("/bot/managed/confirm", {"accept": True})
    assert got.status_code == 422 and got.json()["code"] == "other_poller"
    assert (await attempt(s))["status"] == "failed" and s.state.config.bot_token == ""
    assert ("bot.token", "refused", "ботом уже пользуется другая программа") in await audit_rows(conn)


async def test_logging_out_everywhere_stops_the_attempt_in_any_state(stand, conn):
    s = await managed_stand(stand, conn)
    await s.page.post("/bot/managed")
    await until(lambda: len(s.relay.requests) >= 2)
    keep = pairing(s)
    assert (await s.page.post("/logout-all")).status_code == 200
    assert pairing(s) is None and keep.status == "cancelled" and keep.secret == ""
    seen = len(s.relay.requests)
    s.relay.create()
    await asyncio.sleep(0.1)
    assert len(s.relay.requests) == seen and s.state.config.bot_token == ""
    # То же, когда токен уже получен и ждёт подтверждения.
    await s.page.login(conn)
    s.relay = FakeRelay()
    executor_service.TEST_OVERRIDES["botpair_transport"] = s.relay.transport()
    await s.page.post("/bot/managed")
    s.relay.create()
    await until_status(s, "confirm")
    keep = pairing(s)
    assert (await s.page.post("/logout-all")).status_code == 200
    assert keep.status == "cancelled" and keep.token == "" and s.state.config.bot_token == ""


async def test_token_is_not_applied_after_the_session_ended(stand, conn):
    s = await managed_stand(stand, conn)
    await s.page.post("/bot/managed")
    await conn.execute("UPDATE setup_sessions SET revoked_at = now()")   # сессия кончилась мимо страницы
    s.relay.create()
    await until_status(s, "failed")
    assert pairing(s).code == "session_ended" and pairing(s).token == ""
    assert s.state.config.bot_token == ""
    assert ("bot.managed", "refused", "вход на страницу завершился до создания бота") in await audit_rows(conn)


async def test_session_and_manual_token_are_rechecked_right_before_saving(stand, conn, monkeypatch):
    s = await managed_stand(stand, conn)
    probe = apply.probe_other_poller

    async def logout_during_probe(config, token):
        await conn.execute("UPDATE setup_sessions SET revoked_at = now()")
        await probe(config, token)

    await s.page.post("/bot/managed")
    s.relay.create()
    await until_status(s, "confirm")
    monkeypatch.setattr(apply, "probe_other_poller", logout_during_probe)
    got = await s.page.post("/bot/managed/confirm", {"accept": True})
    assert got.status_code == 422 and got.json()["code"] == "session_ended"
    assert s.state.config.bot_token == ""
    # Токен поменяли вручную, пока шла проверка: созданный бот не подключается поверх.
    await s.page.login(conn)
    s.relay = FakeRelay()
    executor_service.TEST_OVERRIDES["botpair_transport"] = s.relay.transport()
    await s.page.post("/bot/managed")
    s.relay.create()
    await until_status(s, "confirm")
    page = s.state.extras["setup_page"]

    async def manual_change_during_probe(config, token):
        await bot_setup.cancel(page)
        await probe(config, token)

    monkeypatch.setattr(apply, "probe_other_poller", manual_change_during_probe)
    got = await s.page.post("/bot/managed/confirm", {"accept": True})
    assert got.status_code == 422 and got.json()["code"] == "replaced"
    assert s.state.config.bot_token == ""
    # Ручной токен, введённый во время ожидания подтверждения, снимает попытку.
    monkeypatch.setattr(apply, "probe_other_poller", probe)
    s.relay = FakeRelay()
    executor_service.TEST_OVERRIDES["botpair_transport"] = s.relay.transport()
    await s.page.post("/bot/managed")
    s.relay.create()
    await until_status(s, "confirm")
    assert (await save_bot(s)).json()["status"] == "saved"
    assert pairing(s) is None
    assert (await s.page.post("/bot/managed/confirm", {"accept": True})).json()["code"] == "not_pending"


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
        # Ответ длиннее 64 КБ не читается дальше.
        (httpx.Response(200, content=b"{" + b" " * (70 * 1024) + b"}"), "no_connection", "связаться"),
    ]
    for answer, code, words in cases:
        s.relay.start_answer = answer
        got = await s.page.post("/bot/managed", {"code": "ABCD-EFGH"})
        assert got.status_code == 422 and got.json()["code"] == code and words in got.json()["error"], (code, got.text)
    assert s.relay.requests[1][1] == {"name": "Согласования", "code": "ABCD-EFGH"}
    assert await attempt(s) is None
    executor_service.TEST_OVERRIDES["botpair_transport"] = httpx.MockTransport(_unreachable)
    assert (await s.page.post("/bot/managed")).json()["code"] == "no_connection"


async def test_relay_call_has_an_overall_deadline(stand, conn, monkeypatch):
    s = await managed_stand(stand, conn)
    monkeypatch.setattr(bot_setup, "CALL_DEADLINE", 0.2)

    async def slow(request):
        await asyncio.sleep(5)
        return httpx.Response(200, json={})

    executor_service.TEST_OVERRIDES["botpair_transport"] = httpx.MockTransport(slow)
    began = time.monotonic()
    assert (await s.page.post("/bot/managed")).json()["code"] == "no_connection"
    assert time.monotonic() - began < 2


def _unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("нет связи", request=request)


async def test_polling_follows_the_relay_not_the_first_deadline(stand, conn):
    """Сервис спрашивает, пока посредник не скажет «пары нет» (404): у посредника срок продлевается."""
    s = await managed_stand(stand, conn)
    s.relay.token_answers = [httpx.Response(502, json={"error": "telegram"}), httpx.Response(429, json={})]
    await s.page.post("/bot/managed")
    pairing(s).expires_at = time.time() - 100                          # первый срок давно прошёл
    await until(lambda: len(s.relay.requests) >= 6)
    assert pairing(s).status == "waiting" and pairing(s).expires_at > time.time() + 800   # срок — от посредника
    s.relay.token_answers = [httpx.Response(404, json={"error": "not_found"})]
    await until_status(s, "failed")
    a = await attempt(s)
    assert a["code"] == "expired" and "не меняя адрес" in a["message"] and a["link"] is None
    assert pairing(s).secret == ""                                     # секрет пары больше не держится


async def test_one_tap_can_be_switched_off_by_the_operator(stand, conn):
    s = await stand(bot_manager_api="")
    await s.page.login(conn)
    assert (await s.page.get("/state")).json()["bot"]["managed"]["available"] is False
    got = await s.page.post("/bot/managed")
    assert got.status_code == 422 and got.json()["code"] == "off"


async def test_internal_api_cannot_start_or_see_the_pairing(stand, conn):
    s = await managed_stand(stand, conn)
    await s.page.post("/bot/managed")
    for method, path in (("POST", "/bot/managed"), ("DELETE", "/bot/managed"), ("GET", "/bot/managed"),
                         ("POST", "/bot/managed/confirm")):
        got = await s.api.request(method, "/shturman-setup/api" + path)
        assert got.status_code in (401, 403, 405), (method, path, got.status_code)
    status = json.dumps((await s.api.get("/api/executor/status")).json())
    assert SECRET not in status and "t.me/newbot" not in status
    assert not re.search(r"/api/(bot/managed|botpair)", json.dumps([str(r.path) for r in s.api._transport.app.inner.routes
                                                                     if hasattr(r, "path") and r.path.startswith("/api/")]))


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
