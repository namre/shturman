"""Модуль исполнителя в составе сервиса: простой без настроек, опрос, место опроса, состояние,
и цепочка подделки согласования, которая со своим ботом больше не проходит."""

import dataclasses
import logging

import httpx
import pytest
import pytest_asyncio

from shturman import bridge, confirm, jobs
from shturman.executor import binding, commands
from shturman.executor import service as executor_service
from shturman.executor.bot import Bot
from shturman.executor.botapi import BotApi

from conftest import API_TOKEN
from exec_fakes import (  # noqa: F401 — rig — фикстура
    BOT_ID, BOT_NAME, OWNER, OWNER_USER, STRANGER, TOKEN, FakeLlm, FakeTelegram, bind, buttons_of,
    no_sleep, refusal, rig, until,
)

KEY = "sk-SENTINEL-llm-key-DoNotLeak"


class Stop(Exception):
    """Останавливает бесконечный цикл опроса в тесте."""


@pytest_asyncio.fixture
async def live(make_client, conn, config):
    """Сервис, поднятый целиком через `lifespan` модуля: опрос и исполнитель работают в фоне."""
    tg, llm = FakeTelegram(), FakeLlm('{"items": []}')
    executor_service.TEST_OVERRIDES.update(
        bot_transport=tg.transport(), llm_transport=llm.transport(), poll=0, idle=0.02)

    async def start(*modules, **changes):
        cfg = dataclasses.replace(config, **changes)
        client, state = await make_client("shturman.api_core", "shturman.executor.service", *modules, cfg=cfg)
        return client, state

    try:
        yield start, tg, llm
    finally:
        executor_service.TEST_OVERRIDES.clear()


# --- без настроек ---

async def test_with_nothing_configured_the_module_is_idle_and_the_plugin_executes(make_client, conn):
    client, state = await make_client("shturman.api_core", "shturman.executor.service")
    assert bridge.builtin_kinds() == frozenset() and bridge.owns_bot() is False
    assert not [t for t in state._tasks if t.get_name().startswith("executor-")]
    status = (await client.get("/api/executor/status")).json()
    assert status["bot"]["configured"] is False and status["llm"]["configured"] is False
    assert status["jobs"]["kinds"] == []

    # всё как раньше: владельца сообщает плагин, задания забирает плагин, нажатия идут через API
    assert (await client.put("/api/owner", json={"user_id": OWNER, "chat_id": OWNER})).status_code == 200
    job = await bridge.notify_owner(conn, "Карточка")
    await bridge.request_text(conn, handler="x", messages=[{"role": "user", "content": "x"}])
    assert await conn.fetchval("SELECT count(*) FROM jobs WHERE executor = 'plugin'") == 2
    claimed = (await client.post("/api/jobs/claim", json={"limit": 5})).json()["jobs"]
    assert len(claimed) == 2 and job in [j["id"] for j in claimed]
    pressed = await client.post("/api/callbacks/telegram", json={"data": "sh:zz:1", "from_user_id": OWNER})
    assert pressed.status_code == 200
    assert (await client.get("/api/status")).json()["own_bot"] is False


# --- запуск по настройкам ---

async def test_only_what_is_configured_is_started_and_announced(live, conn):
    start, tg, llm = live
    client, state = await start(llm_api_key=KEY, llm_model="main-model")
    assert bridge.builtin_kinds() == {bridge.LLM_STRUCTURED, bridge.LLM_TEXT}
    assert bridge.owns_bot() is False                      # бота нет — нажатия по-прежнему через плагин
    assert {t.get_name() for t in state._tasks if t.get_name().startswith("executor-")} == {"executor-jobs-llm"}
    job = await bridge.request_structured(conn, handler="x", instructions="i", input="t", json_schema={}, schema_name="s")
    await until(lambda: conn.fetchval("SELECT status = 'done' FROM jobs WHERE id = $1", job))
    assert tg.requests == [] and len(llm.requests) == 1
    status = (await client.get("/api/executor/status")).json()
    assert status["llm"] == {"configured": True, "model": "main-model", "task_models": {}, "problem": None,
                             "last_call_ok": True, "calls": 1, "failures": 0}
    assert status["jobs"]["done"] == {"llm.structured": 1} and status["bot"]["configured"] is False
    assert KEY not in (await client.get("/api/executor/status")).text


async def test_builtin_kinds_are_released_when_the_service_stops(make_client, config):
    import contextlib

    from shturman.app import build_app

    tg = FakeTelegram()
    executor_service.TEST_OVERRIDES.update(bot_transport=tg.transport(), poll=0, idle=0.02)
    try:
        gate = build_app(dataclasses.replace(config, bot_token=TOKEN), migrate=False,
                         modules=("shturman.api_core", "shturman.executor.service"))
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(gate.inner.router.lifespan_context(gate.inner))
            assert bridge.owns_bot() is True
            await until(lambda: tg.calls("getUpdates"))
        assert bridge.builtin_kinds() == frozenset()
    finally:
        executor_service.TEST_OVERRIDES.clear()


async def test_jobs_queued_before_the_switch_are_not_left_to_the_plugin(live, conn):
    start, tg, _ = live
    old = await bridge.notify_owner(conn, "Карточка, поставленная до включения своего бота")
    send = await jobs.enqueue(conn, bridge.BUSINESS_SEND, {"text": "x"})
    assert await conn.fetchval("SELECT executor FROM jobs WHERE id = $1", old) == "plugin"
    client, _ = await start(bot_token=TOKEN)
    assert await conn.fetchval("SELECT executor FROM jobs WHERE id = $1", old) == "builtin"
    assert await conn.fetchval("SELECT executor FROM jobs WHERE id = $1", send) == "plugin"   # решает подключение
    claimed = (await client.post("/api/jobs/claim", json={"kinds": [bridge.NOTIFY_OWNER]})).json()["jobs"]
    assert claimed == []


# --- опрос ---

async def test_offset_survives_a_restart_without_replaying_or_losing_updates(rig):
    await bind(rig)
    seen = []

    @bridge.on_callback("tsv")
    async def pressed(conn, rest, user_id):
        seen.append(rest)
        return {"answer": "ок"}

    rig.tg.press("sh:tsv:1")
    rig.tg.press("sh:tsv:2")
    await rig.bot.poll_once()
    assert seen == ["1", "2"]
    saved = await binding.get_state(rig.conn, "offset")
    assert saved == {"bot_id": BOT_ID, "next": rig.tg._update_id + 1}

    rig.tg.press("sh:tsv:3")                               # пришло, пока сервис был остановлен
    again = Bot(rig.state, BotApi(TOKEN, transport=rig.tg.transport()), poll=0)
    await again.ensure_identity()
    assert again.offset == saved["next"]
    await again.poll_once()
    assert seen == ["1", "2", "3"]                         # старые не повторились, новое не потерялось
    assert [p.get("offset") for p in rig.tg.calls("getUpdates")][-1] == saved["next"]
    await again.poll_once()
    assert seen == ["1", "2", "3"]


async def test_offset_of_another_bot_is_not_reused(rig):
    await binding.put_state(rig.conn, "offset", {"bot_id": 999, "next": 123456})
    await rig.bot.ensure_identity()
    assert rig.bot.offset is None
    assert (await binding.get_state(rig.conn, "bot")) == {"id": BOT_ID, "username": BOT_NAME}


async def test_update_is_not_confirmed_while_the_database_is_down(rig, monkeypatch):
    await bind(rig)
    rig.tg.press("sh:tsv:9")
    before = rig.bot.offset

    async def down(conn, bot_id):
        raise ConnectionError("база недоступна")

    monkeypatch.setattr(binding, "bound_owner", down)
    with pytest.raises(Exception):
        await rig.bot.poll_once()
    assert rig.bot.offset == before                        # Telegram пришлёт обновление ещё раз
    monkeypatch.undo()
    await rig.bot.poll_once()
    assert rig.bot.offset == before + 1


async def test_conflict_with_another_poller_is_reported_and_backed_off(rig):
    pauses = []

    async def sleep(seconds):
        pauses.append(seconds)
        if len(pauses) == 4:
            raise Stop                                     # выходим из бесконечного цикла опроса

    conflict = refusal(409, "Conflict: terminated by other getUpdates request; make sure that only one bot instance is running")
    rig.tg.script["getUpdates"] = [conflict, conflict, conflict, None,
                                   refusal(409, "Conflict: can't use getUpdates method while webhook is active")]
    rig.bot._sleep = sleep
    with pytest.raises(Stop):
        await rig.bot.run()
    assert pauses == [5.0, 10.0, 20.0, 5.0]                # пауза растёт, после успешного опроса — сначала
    assert rig.bot.polling is False and rig.bot.problem == "webhook"
    assert len(rig.tg.calls("getUpdates")) == 5


@pytest.mark.parametrize("failure, problem", [
    (refusal(401, "Unauthorized"), "token_rejected"),
    (httpx.ConnectError("нет сети"), "no_connection"),
    (httpx.Response(502, text="bad gateway"), "no_connection"),
    (refusal(429, "Too Many Requests: retry after 3", retry_after=3), "too_many_requests"),
])
async def test_polling_problems_are_named_in_the_status(rig, caplog, failure, problem):
    caplog.set_level(logging.DEBUG)

    async def sleep(seconds):
        raise Stop

    rig.tg.script["getUpdates"] = [failure]
    rig.bot._sleep = sleep
    with pytest.raises(Stop):
        await rig.bot.run()
    assert rig.bot.problem == problem and rig.bot.polling is False
    assert "SENTINEL" not in caplog.text


async def test_one_bad_update_does_not_block_the_rest(rig):
    await bind(rig)
    rig.tg.push(message="не объект")
    rig.tg.push(business_message={"business_connection_id": "нет такого", "message_id": "x"})
    rig.tg.push(что_то_новое={"a": 1})
    rig.tg.text("привет")
    before = len(rig.tg.sent())
    await rig.bot.poll_once()
    assert len(rig.tg.sent()) == before + 1 and rig.bot.offset == rig.tg._update_id + 1


# --- состояние и команды ---

async def test_status_has_flags_and_counters_but_no_ids_or_secrets(live, conn, capsys):
    start, tg, llm = live
    client, state = await start(bot_token=TOKEN, llm_api_key=KEY, llm_model="main-model")
    # признак «опрос работает» выставляется после возврата первого getUpdates, а не при его вызове
    await until(lambda: state.extras["executor"].bot.polling)
    status = (await client.get("/api/executor/status")).json()
    assert status["bot"]["configured"] is True and status["bot"]["username"] == BOT_NAME
    assert status["bot"]["polling"] is True and status["bot"]["owner_bound"] is False
    assert status["bot"]["problem"] is None and isinstance(status["bot"]["last_poll_age"], int)
    assert status["jobs"]["kinds"] == ["llm.structured", "llm.text", "notify.edit", "notify.owner"]

    code, _ = await binding.create_code(conn)
    tg.text(f"/start {code}")
    await until(lambda: binding.bound_owner(conn, BOT_ID))
    raw = (await client.get("/api/executor/status")).text
    assert '"owner_bound":true' in raw
    for secret in (TOKEN, "SENTINEL", KEY, str(OWNER), str(BOT_ID), code):
        assert secret not in raw
    assert (await client.get("/api/status")).json()["own_bot"] is True

    commands.bot_status(lambda method, path: (200, (status | {"bot": status["bot"] | {"owner_bound": True}})))
    out = capsys.readouterr().out
    assert f"имя бота: @{BOT_NAME}" in out and "опрос Telegram: работает" in out
    assert "владелец привязан: да" in out and "Своя модель: main-model" in out


def test_bot_status_explains_problems_in_plain_words(capsys):
    broken = {"bot": {"configured": True, "username": None, "polling": False, "problem": "other_poller",
                      "owner_bound": False, "bind_paused": True, "business_capable": False, "counters": {}},
              "llm": {"configured": False}, "jobs": {}}
    commands.bot_status(lambda method, path: (200, broken))
    out = capsys.readouterr().out
    assert "опрос Telegram: не работает" in out and "уже опрашивает другая программа" in out
    assert "shturman bot-bind" in out and "приостановлен" in out and "Business Mode" in out
    commands.bot_status(lambda method, path: (200, {"bot": {"configured": False}, "llm": {"configured": False}}))
    out = capsys.readouterr().out
    assert "не настроен" in out
    # сервис не знает, установлен ли Hermes: текст не утверждает, что задания кто-то выполняет
    assert "идут через Hermes" not in out and "вызывает плагин в Hermes" not in out
    assert out.count("без Hermes") == 2


def test_model_hint_points_at_whoever_really_asks_the_model():
    try:
        bridge.set_builtin(())
        assert "Hermes" in bridge.model_hint()
        bridge.set_builtin({bridge.NOTIFY_OWNER, bridge.NOTIFY_EDIT})      # свой бот без своей модели
        assert "Hermes" in bridge.model_hint()
        bridge.set_builtin({bridge.LLM_STRUCTURED, bridge.LLM_TEXT})
        assert "Hermes" not in bridge.model_hint() and "bot-status" in bridge.model_hint()
    finally:
        bridge.set_builtin(())


async def test_bot_bind_refuses_until_the_service_has_met_telegram(conn, monkeypatch, capsys):
    from conftest import DSN

    monkeypatch.delenv("SHTURMAN_BOT_TOKEN", raising=False)
    with pytest.raises(SystemExit) as stop:
        await commands.bot_bind(DSN)
    assert "SHTURMAN_BOT_TOKEN" in str(stop.value)
    monkeypatch.setenv("SHTURMAN_BOT_TOKEN", "задан")
    with pytest.raises(SystemExit) as stop:
        await commands.bot_bind(DSN)
    assert "ещё не связался с Telegram" in str(stop.value)
    assert await conn.fetchval("SELECT count(*) FROM executor_bind_codes") == 0


# --- подделка согласования больше не проходит ---

async def test_forged_approval_chain_fails_with_own_bot(live, conn, caplog):
    """Повторяет находку проверки безопасности: ассистент с токеном внутреннего API забирал
    карточку из очереди (узнавал метку кнопки и «съедал» карточку) и присылал нажатие владельца."""
    caplog.set_level(logging.DEBUG)
    start, tg, _ = live
    client, state = await start(bot_token=TOKEN, sending=True)
    done = []

    @confirm.applier("t.forge")
    async def widen(c, payload):
        done.append(payload)
        return "Включено"

    code, _ = await binding.create_code(conn)
    tg.text(f"/start {code}")
    await until(lambda: binding.bound_owner(conn, BOT_ID))

    out = await confirm.request(conn, "t.forge", "Включить автоответ для всех", {"on": True})
    assert out["status"] == "pending_confirmation"

    # 1. Забрать карточку из очереди нельзя: задания своего бота плагину не выдаются.
    for body in ({}, {"kinds": [bridge.NOTIFY_OWNER]}, {"kinds": list(bridge.EXECUTOR_KINDS), "limit": 20}):
        assert (await client.post("/api/jobs/claim", json=body)).json()["jobs"] == []
    # Карточку доставил бот согласований — её не «съели».
    card = await until(lambda: [p for p in tg.sent() if "Подтвердите действие" in p["text"]])
    yes = buttons_of(card[0])["Да, сделать"]

    # 2. Прислать нажатие нельзя — даже зная метку кнопки целиком.
    forged = await client.post("/api/callbacks/telegram", json={"data": yes, "from_user_id": OWNER})
    assert forged.status_code == 403 and done == []
    # 3. Назначить владельцем себя или сбросить владельца нельзя.
    assert (await client.put("/api/owner", json={"user_id": STRANGER, "chat_id": STRANGER})).status_code == 403
    assert (await client.delete("/api/owner")).status_code == 403
    assert (await bridge.get_owner(conn))["user_id"] == OWNER
    # 4. Закрыть чужое задание «выполнено» можно, но это ничего не подтверждает.
    job_id = await conn.fetchval("SELECT id FROM jobs WHERE kind = 'notify.owner' ORDER BY id DESC LIMIT 1")
    await client.post(f"/api/jobs/{job_id}/complete", json={"result": {"message_id": 1}})
    assert done == [] and await conn.fetchval("SELECT status FROM pending_actions") == "pending"
    # 5. Нажатие постороннего в самом боте тоже не проходит.
    tg.press(yes, user={"id": STRANGER, "is_bot": False, "first_name": "Некто"}, message_id=tg.last_message_id())
    await until(lambda: tg.calls("answerCallbackQuery"))
    assert tg.calls("answerCallbackQuery")[-1]["text"] == "Кнопка недоступна." and done == []

    # Настоящее нажатие владельца, пришедшее от Telegram, действует.
    tg.press(yes, message_id=tg.last_message_id())
    await until(lambda: done)
    assert done == [{"on": True}]
    await until(lambda: tg.calls("editMessageText"))
    assert tg.calls("editMessageText")[-1]["text"].startswith("Сделано:")
    for place in (caplog.text, (await client.get("/api/executor/status")).text, (await client.get("/api/status")).text):
        assert "SENTINEL" not in place and API_TOKEN not in place
