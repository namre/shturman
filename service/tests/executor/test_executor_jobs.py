"""Исполнитель заданий бота: карточки владельцу, их правка и отправка от имени владельца."""

import asyncio
import dataclasses
import logging

import httpx
import pytest

from shturman import bridge, jobs, store

from exec_fakes import (  # noqa: F401 — rig — фикстура
    IVAN, OWNER, bind, buttons_of, ok, refusal, rig,
)

BC = "bc-service-1"
TEXT = "Добрый день! Смету пришлю в пятницу."


async def job_row(conn, job_id):
    return await conn.fetchrow("SELECT status, error, result, attempts, run_after > now() AS later FROM jobs WHERE id = $1", job_id)


async def connection(conn, *, via="service", enabled=True, can_reply=True, connection_id=BC) -> str:
    account = await store.ensure_account(conn, OWNER, "Владелец", "owner")
    await conn.execute(
        "INSERT INTO business_connections (id, account_id, can_reply, enabled, via) VALUES ($1, $2, $3, $4, $5)",
        connection_id, account, can_reply, enabled, via)
    return connection_id


async def send_job(rig, *, text=TEXT, connection_id=BC, reply_to=41, executor="builtin") -> int:
    """Задание отправки ставится так же, как его ставит шлюз отправки."""
    seen = {}

    @bridge.on_result("t.send")
    async def sent(conn, job, result):
        seen["result"] = result

    @bridge.on_failure("t.send")
    async def failed(conn, job, error):
        seen["error"] = error

    job_id = await bridge.request_business_send(
        rig.conn, handler="t.send", business_connection_id=connection_id, chat_id=IVAN, text=text,
        reply_to_message_id=reply_to)
    rig.seen = seen
    return job_id


# --- сообщения владельцу ---

async def test_card_goes_to_the_owner_as_plain_text_with_service_buttons(rig):
    await bind(rig)
    job = await bridge.notify_owner(
        rig.conn, "Черновик № 1\n*звёздочки* и <b>теги</b> остаются как есть",
        buttons=[[bridge.button("Отправить", "ob", "s:1:abc"), bridge.button("Отклонить", "ob", "r:1:abc")]],
        silent=True)
    assert await rig.worker.run_once("bot") == 1
    sent = rig.tg.sent()[-1]
    assert sent["chat_id"] == OWNER and "parse_mode" not in sent and sent["disable_notification"] is True
    assert buttons_of(sent) == {"Отправить": "sh:ob:s:1:abc", "Отклонить": "sh:ob:r:1:abc"}
    row = await job_row(rig.conn, job)
    assert row["status"] == "done" and '"message_id"' in row["result"]
    assert rig.worker.done[bridge.NOTIFY_OWNER] == 1


async def test_card_waits_while_no_owner_is_bound_and_is_sent_after_binding(rig):
    job = await bridge.notify_owner(rig.conn, "Ждёт владельца")
    await rig.worker.run_once("bot")
    row = await job_row(rig.conn, job)
    assert row["status"] == "queued" and row["later"] and rig.tg.calls("sendMessage") == []
    await bind(rig)
    await rig.conn.execute("UPDATE jobs SET run_after = now()")
    await rig.worker.run_once("bot")
    assert (await job_row(rig.conn, job))["status"] == "done"


@pytest.mark.parametrize("failure, status, retried", [
    (refusal(400, "Bad Request: chat not found"), "failed", False),          # точный отказ — окончательно
    (refusal(403, "Forbidden: bot was blocked by the user"), "failed", False),
    (refusal(429, "Too Many Requests: retry after 9", retry_after=9), "queued", True),
    (httpx.ConnectError("нет сети"), "queued", True),
    (httpx.ReadTimeout("долго"), "queued", True),
    (httpx.Response(502, text="bad gateway"), "queued", True),
])
async def test_card_delivery_failures(rig, failure, status, retried):
    await bind(rig)
    before = len(rig.tg.calls("sendMessage"))
    rig.tg.script["sendMessage"] = [failure]
    job = await bridge.notify_owner(rig.conn, "Карточка с секретным текстом")
    await rig.worker.run_once("bot")
    row = await job_row(rig.conn, job)
    assert row["status"] == status and bool(row["later"]) is retried
    assert len(rig.tg.calls("sendMessage")) == before + 1
    assert "секретн" not in row["error"] and "SENTINEL" not in row["error"]


async def test_foreign_buttons_and_broken_jobs_are_refused_for_good(rig):
    await bind(rig)
    before = len(rig.tg.calls("sendMessage"))
    foreign = await jobs.enqueue(rig.conn, bridge.NOTIFY_OWNER, executor="builtin", payload={
        "text": "Одобрить команду?", "buttons": [[{"text": "Да", "data": "ea:approve:1"}]], "silent": False})
    empty = await jobs.enqueue(rig.conn, bridge.NOTIFY_OWNER, {"text": "  ", "buttons": None}, executor="builtin")
    odd = await jobs.enqueue(rig.conn, "что-то.новое", {"x": 1}, executor="builtin")
    for _ in range(3):
        await rig.worker.run_once("bot")
    rows = [await job_row(rig.conn, j) for j in (foreign, empty)]
    assert [r["status"] for r in rows] == ["failed", "failed"]
    assert "не принадлежат сервису" in rows[0]["error"]
    assert (await job_row(rig.conn, odd))["status"] == "queued"      # чужой вид исполнитель не берёт
    assert len(rig.tg.calls("sendMessage")) == before


async def test_edit_replaces_text_and_removes_or_keeps_buttons(rig):
    await bind(rig)
    await bridge.notify_owner(rig.conn, "Карточка", buttons=[[bridge.button("Отправить", "ob", "s:1:abc")]])
    await rig.worker.run_once("bot")
    card = rig.tg.last_message_id()

    await bridge.edit_owner_message(rig.conn, card, "Уточнённый текст", remove_buttons=False)
    await rig.worker.run_once("bot")
    await bridge.edit_owner_message(rig.conn, card, "Черновик отправлен")
    await rig.worker.run_once("bot")
    keep, strip = rig.tg.calls("editMessageText")
    assert keep["message_id"] == card and buttons_of(keep) == {"Отправить": "sh:ob:s:1:abc"}
    assert strip["text"] == "Черновик отправлен" and "reply_markup" not in strip and "parse_mode" not in strip

    # кнопки сняты — «оставить» их уже нельзя, и молча снимать ещё раз исполнитель не станет
    lost = await bridge.edit_owner_message(rig.conn, card, "Ещё раз", remove_buttons=False)
    await rig.worker.run_once("bot")
    assert (await job_row(rig.conn, lost))["error"].startswith("keep_buttons_unavailable")

    # «не изменено» и «сообщения нет» — править нечего, задание выполнено
    rig.tg.script["editMessageText"] = [refusal(400, "Bad Request: message is not modified"),
                                        refusal(400, "Bad Request: message to edit not found")]
    same = await bridge.edit_owner_message(rig.conn, card, "Черновик отправлен")
    gone = await bridge.edit_owner_message(rig.conn, 999, "Нет такого")
    await rig.worker.run_once("bot")
    await rig.worker.run_once("bot")
    assert [(await job_row(rig.conn, j))["status"] for j in (same, gone)] == ["done", "done"]


# --- отправка от имени владельца ---

async def test_approved_text_is_sent_through_the_business_connection_exactly_once(rig):
    await connection(rig.conn)
    job = await send_job(rig)
    assert await rig.worker.run_once("bot") == 1
    assert rig.tg.sent(business=True) == [{
        "chat_id": IVAN, "text": TEXT, "business_connection_id": BC, "reply_parameters": {"message_id": 41}}]
    assert rig.seen == {"result": {"message_id": rig.tg.last_message_id()}}
    assert (await job_row(rig.conn, job))["status"] == "done"
    assert await rig.worker.run_once("bot") == 0 and len(rig.tg.sent(business=True)) == 1


NOT_SENT, UNKNOWN = "not_sent", "unknown"


@pytest.mark.parametrize("failure, verdict", [
    (refusal(400, "Bad Request: BUSINESS_PEER_INVALID"), NOT_SENT),
    (refusal(403, "Forbidden: bot was blocked by the user"), NOT_SENT),
    (refusal(401, "Unauthorized"), NOT_SENT),
    (refusal(429, "Too Many Requests: retry after 30", retry_after=30), NOT_SENT),
    (httpx.ConnectError("нет сети"), NOT_SENT),
    (httpx.ConnectTimeout("нет сети"), NOT_SENT),
    (httpx.PoolTimeout("нет соединений"), NOT_SENT),
    (httpx.Response(500, json={"ok": False, "error_code": 500, "description": "Internal Server Error"}), UNKNOWN),
    (httpx.Response(502, text="bad gateway"), UNKNOWN),
    (httpx.ReadTimeout("долго"), UNKNOWN),
    (httpx.WriteTimeout("долго"), UNKNOWN),
    (httpx.ReadError("соединение сброшено"), UNKNOWN),
    (httpx.RemoteProtocolError("оборвалось"), UNKNOWN),
    (ok(True), UNKNOWN),                                   # «выполнено», но без номера сообщения
])
async def test_send_failure_is_classified_and_never_retried(rig, caplog, failure, verdict):
    caplog.set_level(logging.DEBUG)
    await connection(rig.conn)
    rig.tg.script["sendMessage"] = [failure]
    job = await send_job(rig)
    await rig.worker.run_once("bot")
    row = await job_row(rig.conn, job)
    assert row["status"] == "failed" and row["attempts"] == 1
    assert row["error"].startswith(bridge.NOT_SENT_PREFIX) is (verdict == NOT_SENT)
    assert rig.seen["error"] == row["error"]
    for _ in range(3):                                      # сколько ни опрашивай очередь — второй попытки нет
        await rig.worker.run_once("bot")
    assert len(rig.tg.calls("sendMessage")) == 1
    for place in (row["error"], caplog.text):
        assert "SENTINEL" not in place and "Смету" not in place
    if verdict == UNKNOWN:
        assert rig.worker.counters["sends_unknown"] == 1


async def test_send_that_hangs_is_an_unknown_outcome(rig):
    async def hang(params):
        await asyncio.sleep(5)

    await connection(rig.conn)
    rig.worker.bot_timeout = 0.05
    rig.tg.script["sendMessage"] = [hang]
    job = await send_job(rig)
    await rig.worker.run_once("bot")
    row = await job_row(rig.conn, job)
    assert row["status"] == "failed" and not row["error"].startswith(bridge.NOT_SENT_PREFIX)
    assert "исход неизвестен" in row["error"] and len(rig.tg.calls("sendMessage")) == 1


@pytest.mark.parametrize("case", ["too_long", "empty", "bad_reply", "sending_off", "foreign_connection",
                                  "disabled", "no_reply_right", "unknown_connection"])
async def test_send_is_refused_before_any_request_to_telegram(rig, case):
    kwargs = {}
    if case == "foreign_connection":
        await connection(rig.conn, via="plugin")
    elif case == "disabled":
        await connection(rig.conn, enabled=False)
    elif case == "no_reply_right":
        await connection(rig.conn, can_reply=False)
    elif case != "unknown_connection":
        await connection(rig.conn)
    if case == "too_long":
        kwargs["text"] = "🙂" * 2049                       # 2049 знаков, но 4098 единиц UTF-16
    elif case == "empty":
        kwargs["text"] = "   "
    elif case == "bad_reply":
        kwargs["reply_to"] = -5
    elif case == "sending_off":
        rig.state.config = dataclasses.replace(rig.state.config, sending=False)
    job = await send_job(rig, **kwargs)
    # задание по чужому или неизвестному подключению мост отдал бы плагину — здесь проверяется сам исполнитель
    await rig.conn.execute("UPDATE jobs SET executor = 'builtin' WHERE id = $1", job)
    await rig.worker.run_once("bot")
    row = await job_row(rig.conn, job)
    assert row["status"] == "failed" and row["error"].startswith(bridge.NOT_SENT_PREFIX)
    assert rig.tg.calls("sendMessage") == []


async def test_plugin_connection_send_is_left_to_the_plugin(rig):
    await connection(rig.conn, via="plugin", connection_id="bc-plugin")
    job = await send_job(rig, connection_id="bc-plugin")
    assert await rig.worker.run_once("bot") == 0 and rig.tg.calls("sendMessage") == []
    claimed = (await rig.client.post("/api/jobs/claim", json={"kinds": [bridge.BUSINESS_SEND]})).json()["jobs"]
    assert [j["id"] for j in claimed] == [job]


async def test_result_is_recorded_even_if_the_database_hiccups(rig, monkeypatch):
    await connection(rig.conn)
    job = await send_job(rig)
    real, failures = bridge.deliver_result, [ConnectionError("база недоступна"), ConnectionError("ещё раз")]

    async def flaky(conn, job_id, result):
        if failures:
            raise failures.pop(0)
        return await real(conn, job_id, result)

    monkeypatch.setattr(bridge, "deliver_result", flaky)
    await rig.worker.run_once("bot")
    assert (await job_row(rig.conn, job))["status"] == "done"
    assert len(rig.tg.sent(business=True)) == 1          # итог записывался трижды, отправка была одна
