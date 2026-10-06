"""Общий слой: запись в архив из разных источников, очередь заданий, мост, проверка токенов."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from shturman import bridge, jobs, store
from shturman.records import ChatRecord, MessageRecord

from conftest import API_AUTH, MCP_AUTH

T0 = datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)


def rec(mid, text, *, sender=2001, edited=None, at=T0):
    return MessageRecord(
        tg_message_id=mid, sent_at=at, kind="message", sender_class="user", sender_tg_id=sender,
        sender_name="Иван", text=text, entities=None, reply_to_tg_id=None, forwarded_from=None,
        edited_at=edited, media_type=None, media_path=None, service_action=None,
    )


async def setup_chat(conn, *, cls="user", tg_id=2001, type_="personal_chat", name="Иван"):
    account_id = await store.ensure_account(conn, 1000, "Владелец")
    chat_id, excluded = await store.ensure_chat(conn, account_id, ChatRecord(cls, tg_id, type_, name))
    return account_id, chat_id, excluded


async def test_same_message_from_two_sources_is_one_row(conn):
    _, chat_id, _ = await setup_chat(conn)
    first = await store.upsert_messages(conn, [(chat_id, rec(1, "привет"))], source="import", owner_tg_id=1000)
    second = await store.upsert_messages(conn, [(chat_id, rec(1, "привет"))], source="session", owner_tg_id=1000)
    assert (first.new, second.new, second.known) == (1, 0, 1)
    assert first.new_ids == second.known_ids
    row = await conn.fetchrow("SELECT sources FROM messages")
    assert row["sources"] == ["import", "session"]


async def test_live_edit_versions_old_text_and_stale_copy_does_not_overwrite(conn):
    _, chat_id, _ = await setup_chat(conn)
    await store.upsert_messages(conn, [(chat_id, rec(1, "к пятнице"))], source="session", owner_tg_id=1000)
    edit = await store.upsert_messages(
        conn, [(chat_id, rec(1, "к понедельнику", edited=T0 + timedelta(hours=1)))],
        source="session", owner_tg_id=1000)
    assert edit.versions == 1
    await store.upsert_messages(conn, [(chat_id, rec(1, "к пятнице"))], source="import", owner_tg_id=1000)
    assert await conn.fetchval("SELECT text FROM messages") == "к понедельнику"
    assert await conn.fetchval("SELECT count(*) FROM message_versions") == 1


async def test_explicit_outgoing_flag_wins_over_sender_comparison(conn):
    _, chat_id, _ = await setup_chat(conn)
    await store.upsert_messages(conn, [(chat_id, rec(1, "от меня", sender=2001), True)],
                                source="session", owner_tg_id=1000)
    assert await conn.fetchval("SELECT is_outgoing FROM messages") is True


async def test_excluded_chat_takes_nothing_from_any_source(conn):
    account_id, chat_id, _ = await setup_chat(conn)
    await conn.execute("UPDATE chats SET excluded = true WHERE id = $1", chat_id)
    result = await store.upsert_messages(conn, [(chat_id, rec(1, "секрет"))], source="business", owner_tg_id=1000)
    assert (result.new, result.known) == (0, 0)
    assert await conn.fetchval("SELECT count(*) FROM messages") == 0
    # запрет не снимается повторным созданием чата
    _, excluded = await store.ensure_chat(conn, account_id, ChatRecord("user", 2001, "personal_chat", "Иван"))
    assert excluded is True


@pytest.mark.parametrize("tg_id,username", [(777000, None), (93372553, "BotFather"), (5, "@SpamBot")])
async def test_service_chats_with_codes_and_tokens_are_always_excluded(conn, tg_id, username):
    account_id = await store.ensure_account(conn, 1000, "Владелец")
    chat_id, excluded = await store.ensure_chat(
        conn, account_id, ChatRecord("user", tg_id, "personal_chat", "Telegram", username=username))
    assert excluded is True
    result = await store.upsert_messages(conn, [(chat_id, rec(1, "Login code: 12345"))],
                                         source="session", owner_tg_id=1000)
    assert result.new == 0


async def test_refresh_updates_name_only_for_live_sources(conn):
    account_id = await store.ensure_account(conn, 1000, "Владелец")
    await store.ensure_chat(conn, account_id, ChatRecord("user", 2001, "personal_chat", "Иван"))
    await store.ensure_chat(conn, account_id, ChatRecord("user", 2001, "personal_chat", "Иван П."))
    assert await conn.fetchval("SELECT name FROM peers WHERE tg_id = 2001") == "Иван"
    await store.ensure_chat(conn, account_id, ChatRecord("user", 2001, "personal_chat", "Иван Петров",
                                                         username="ivan"), refresh=True)
    row = await conn.fetchrow("SELECT name, username FROM peers WHERE tg_id = 2001")
    assert (row["name"], row["username"]) == ("Иван Петров", "ivan")


async def test_delete_in_known_chat_and_stays_deleted_after_reimport(conn):
    _, chat_id, _ = await setup_chat(conn)
    await store.upsert_messages(conn, [(chat_id, rec(1, "а")), (chat_id, rec(2, "б"))],
                                source="session", owner_tg_id=1000)
    assert len(await store.mark_deleted(conn, chat_id, [1, 99])) == 1
    assert await store.mark_deleted(conn, chat_id, [1]) == []
    await store.upsert_messages(conn, [(chat_id, rec(1, "а"))], source="import", owner_tg_id=1000)
    assert await conn.fetchval("SELECT deleted_at IS NOT NULL FROM messages WHERE tg_message_id = 1")


async def test_delete_without_chat_needs_unique_match_outside_channels(conn):
    account_id, ivan, _ = await setup_chat(conn)
    group, _ = await store.ensure_chat(conn, account_id, ChatRecord("chat", 3001, "private_group", "Семья"))
    channel, _ = await store.ensure_chat(conn, account_id, ChatRecord("channel", 4001, "public_channel", "Новости"))
    await store.upsert_messages(
        conn, [(ivan, rec(10, "личное")), (channel, rec(10, "пост")), (ivan, rec(11, "ещё")), (group, rec(11, "в группе"))],
        source="session", owner_tg_id=1000)
    # 10: в канале свой счёт идентификаторов — совпадение с каналом не мешает и канал не трогается
    # 11: два совпадения среди личных чатов и групп — неоднозначно, не трогаем
    deleted = await store.mark_deleted_without_chat(conn, account_id, [10, 11, 12])
    assert len(deleted) == 1
    rows = await conn.fetch("SELECT chat_id, tg_message_id FROM messages WHERE deleted_at IS NOT NULL")
    assert [(r["chat_id"], r["tg_message_id"]) for r in rows] == [(ivan, 10)]


async def test_null_byte_in_text_and_names_is_dropped_not_fatal(conn):
    account_id = await store.ensure_account(conn, 1000, "Владелец")
    chat_id, _ = await store.ensure_chat(conn, account_id, ChatRecord("user", 2001, "personal_chat", "Ив\x00ан"))
    record = rec(1, "до\x00говор")
    record.sender_name = "Ив\x00ан"
    record.entities = [{"type": "bold", "text": "до\x00говор"}]
    await store.upsert_messages(conn, [(chat_id, record)], source="session", owner_tg_id=1000)
    row = await conn.fetchrow("SELECT text, sender_name, entities FROM messages")
    assert (row["text"], row["sender_name"]) == ("договор", "Иван") and "договор" in row["entities"]


async def test_verification_codes_dialog_is_excluded_by_type(conn):
    account_id = await store.ensure_account(conn, 1000, "Владелец")
    _, excluded = await store.ensure_chat(
        conn, account_id, ChatRecord("user", 424242, "verification_codes", "Verification Codes"))
    assert excluded is True


async def test_lost_job_is_reported_to_its_module(conn):
    seen = []

    @bridge.on_failure("t.lost")
    async def lost(c, job, error):
        seen.append(job["id"])

    job_id = await jobs.enqueue(conn, bridge.LLM_TEXT, {}, handler="t.lost", max_attempts=1)
    await jobs.claim(conn, [bridge.LLM_TEXT], worker="w")
    await conn.execute("UPDATE jobs SET locked_until = now() - interval '1 second' WHERE id = $1", job_id)
    assert await bridge.reap_lost(conn) == 1 and seen == [job_id]


async def test_only_unclaimed_job_can_be_cancelled(conn):
    job_id = await jobs.enqueue(conn, bridge.BUSINESS_SEND, {})
    other = await jobs.enqueue(conn, bridge.BUSINESS_SEND, {})
    await conn.execute("UPDATE jobs SET run_after = now() + interval '1 hour' WHERE id = $1", other)
    await jobs.claim(conn, [bridge.BUSINESS_SEND], worker="w")
    assert await jobs.cancel(conn, job_id, "поздно") is False
    assert await jobs.cancel(conn, other, "снято") is True


# --- очередь заданий ---

async def test_job_is_claimed_once_and_dedup_key_prevents_repeat(conn):
    first = await jobs.enqueue(conn, bridge.NOTIFY_OWNER, {"text": "раз"}, dedup_key="k1")
    assert first is not None
    assert await jobs.enqueue(conn, bridge.NOTIFY_OWNER, {"text": "раз"}, dedup_key="k1") is None
    claimed = await jobs.claim(conn, [bridge.NOTIFY_OWNER], worker="w1")
    assert [j["id"] for j in claimed] == [first] and claimed[0]["payload"] == {"text": "раз"}
    assert await jobs.claim(conn, [bridge.NOTIFY_OWNER], worker="w2") == []


async def test_lost_job_returns_to_queue_until_attempts_run_out(conn):
    job_id = await jobs.enqueue(conn, bridge.LLM_TEXT, {}, max_attempts=2)
    for _ in range(2):
        assert len(await jobs.claim(conn, [bridge.LLM_TEXT], worker="w")) == 1
        await conn.execute("UPDATE jobs SET locked_until = now() - interval '1 second' WHERE id = $1", job_id)
    assert await jobs.claim(conn, [bridge.LLM_TEXT], worker="w") == []
    assert [j["id"] for j in await jobs.reap(conn)] == [job_id]
    assert (await jobs.get(conn, job_id))["status"] == "failed"


async def test_failed_job_retries_then_reports_final_failure(conn):
    seen = []

    @bridge.on_failure("t.fail")
    async def failed(c, job, error):
        seen.append((job["context"], error))

    job_id = await jobs.enqueue(conn, bridge.LLM_TEXT, {}, handler="t.fail", context={"x": 1}, max_attempts=2)
    await jobs.claim(conn, [bridge.LLM_TEXT], worker="w")
    assert await bridge.deliver_failure(conn, job_id, "сеть", retry_in=0) == "queued"
    await jobs.claim(conn, [bridge.LLM_TEXT], worker="w")
    assert await bridge.deliver_failure(conn, job_id, "сеть", retry_in=0) == "failed"
    assert seen == [({"x": 1}, "сеть")]


async def test_result_goes_to_module_handler_with_private_context(conn):
    got = {}

    @bridge.on_result("t.ok")
    async def done(c, job, result):
        got.update(context=job["context"], result=result)

    job_id = await bridge.request_structured(
        conn, handler="t.ok", instructions="i", input="x", json_schema={"type": "object"},
        schema_name="s", context={"message_ids": [1, 2]})
    claimed = await jobs.claim(conn, [bridge.LLM_STRUCTURED], worker="w")
    assert "context" not in claimed[0] and "message_ids" not in str(claimed[0])
    assert await bridge.deliver_result(conn, job_id, {"parsed": {"a": 1}}) is True
    assert got == {"context": {"message_ids": [1, 2]}, "result": {"parsed": {"a": 1}}}
    assert await bridge.deliver_result(conn, job_id, {"parsed": {}}) is False  # второй раз — нет


async def test_button_press_is_accepted_only_from_owner(conn):
    calls = []

    @bridge.on_callback("tt")
    async def pressed(c, rest, user_id):
        calls.append(rest)
        return {"answer": "Готово", "edit_text": "Отправлено", "remove_buttons": True}

    data = bridge.callback_data("tt", "ok:5")
    assert (await bridge.dispatch_callback(conn, data, 1000))["answer"] == "Кнопка недоступна."  # владелец не задан
    await bridge.set_owner(conn, 1000, 1000)
    assert (await bridge.dispatch_callback(conn, data, 6666))["answer"] == "Кнопка недоступна."
    assert (await bridge.dispatch_callback(conn, "ea:once:1", 1000))["answer"] == "Кнопка недоступна."
    assert (await bridge.dispatch_callback(conn, "sh:nope:1", 1000))["answer"] == "Кнопка недоступна."
    assert calls == []
    out = await bridge.dispatch_callback(conn, data, 1000)
    assert out == {"answer": "Готово", "edit_text": "Отправлено", "remove_buttons": True} and calls == ["ok:5"]


def test_button_data_must_fit_telegram_limit():
    with pytest.raises(ValueError):
        bridge.callback_data("tt", "x" * 80)


# --- сервис целиком ---

async def test_tokens_are_separate_per_area(make_client):
    client, _ = await make_client("shturman.api_core")
    assert (await client.get("/health", headers={"Authorization": ""})).json()["ok"] is True
    assert (await client.get("/api/status")).status_code == 200
    assert (await client.get("/api/status", headers={"Authorization": "Bearer wrong"})).status_code == 401
    assert (await client.get("/api/status", headers=MCP_AUTH)).status_code == 401   # токен агента сюда не подходит
    assert (await client.get("/mcp", headers=API_AUTH)).status_code == 401          # и наоборот
    assert (await client.get("/elsewhere")).status_code == 404


async def test_executor_round_trip_over_http(make_client, conn):
    client, _ = await make_client("shturman.api_core")
    assert (await client.put("/api/owner", json={"user_id": 1000, "chat_id": 1000})).json() == {"ok": True}
    await bridge.notify_owner(conn, "Сводка готова", buttons=[[bridge.button("Ок", "tt", "ok:1")]])
    claimed = (await client.post("/api/jobs/claim", json={"worker": "plugin"})).json()["jobs"]
    assert claimed[0]["kind"] == "notify.owner" and claimed[0]["payload"]["buttons"][0][0]["data"] == "sh:tt:ok:1"
    job_id = claimed[0]["id"]
    assert (await client.post(f"/api/jobs/{job_id}/complete", json={"result": {"message_id": 7}})).status_code == 200
    assert (await client.post(f"/api/jobs/{job_id}/complete", json={"result": {}})).status_code == 409
    assert (await client.post("/api/jobs/claim", json={"kinds": ["rm -rf"]})).status_code == 400
    status = (await client.get("/api/status")).json()
    assert status["owner_known"] is True and status["jobs_waiting"] == 0
