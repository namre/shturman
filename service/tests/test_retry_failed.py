"""Повтор того, что не получилось из-за сбоя модели (retry.py, команда `shturman retry-failed`, Р-84).

Возвращается только сбой модели: вложение с причиной model:…, lost_job, bad_answer — не старше
SHTURMAN_MEDIA_DAYS, не удалённое, из неисключённого чата, при включённом разборе; запрос обработки
переписки, от которого прогоны отказались после повторов. Вид файла, размер, нет файла — не повторяются.
"""

import json
from datetime import datetime, timedelta, timezone

from shturman import bridge, jobs, retry, store
from shturman.processing import pipeline
from shturman.records import ChatRecord, MessageRecord

OWNER = 1000
TZ = "Europe/Moscow"


async def _setup(conn, *, media=True):
    acc = await store.ensure_account(conn, OWNER, "Владелец", "owner")
    chat, _ = await store.ensure_chat(conn, acc, ChatRecord("user", 2001, "personal_chat", "Иван"))
    if media:
        await conn.execute("INSERT INTO setup_state (key, value) VALUES ('media', '{\"enabled\": true}'::jsonb)")
    return acc, chat


async def _msg(conn, chat, n, text="", *, age_days=0.0):
    at = datetime.now(timezone.utc) - timedelta(days=age_days)
    rec = MessageRecord(tg_message_id=n, sent_at=at, kind="message", sender_class="user", sender_tg_id=2001,
                        sender_name="Иван", text=text, entities=None, reply_to_tg_id=None, forwarded_from=None,
                        edited_at=None, media_type=None, media_path=None, service_action=None)
    return (await store.upsert_messages(conn, [(chat, rec, False)], source="session", owner_tg_id=OWNER)).new_ids[0]


async def _failed(conn, chat, n, error, *, state="failed", age_days=0.0, marked_days_ago=0.0):
    mid = await _msg(conn, chat, n, age_days=age_days)
    await conn.execute(
        """UPDATE messages SET media_type = 'photo', media_state = $2, media_error = $3, media_attempts = 4,
                  media_at = now() - make_interval(secs => $4) WHERE id = $1""",
        mid, state, error, marked_days_ago * 86400)
    return mid


async def _state(conn, mid):
    row = await conn.fetchrow("SELECT media_state, media_error, media_attempts FROM messages WHERE id = $1", mid)
    return tuple(row)


async def test_only_model_failures_within_the_media_window_come_back(conn):
    _, chat = await _setup(conn)
    back = [await _failed(conn, chat, 1, "model:исполнитель не ответил"),
            await _failed(conn, chat, 2, "lost_job"),
            await _failed(conn, chat, 3, "bad_answer")]
    stay = [await _failed(conn, chat, 4, "TimeoutError"),                       # не модель — файл или сеть
            await _failed(conn, chat, 5, "unsupported", state="skipped"),       # вид файла
            await _failed(conn, chat, 6, "too_big", state="skipped"),           # размер
            await _failed(conn, chat, 7, "no_source", state="skipped"),         # скачать нечем
            await _failed(conn, chat, 8, "lost_job", age_days=40)]              # старше SHTURMAN_MEDIA_DAYS
    deleted = await _failed(conn, chat, 9, "lost_job")
    await conn.execute("UPDATE messages SET deleted_at = now() WHERE id = $1", deleted)
    found = await retry.candidates(conn, media_days=30)
    assert (found["media"], found["media_not_model"], found["media_enabled"]) == (3, 1, True)
    out = await retry.run(conn, media_days=30, by="cli")
    assert (out["media"], out["requests"], out["by"]) == (3, 0, "cli")
    for mid in back:
        assert await _state(conn, mid) == ("pending", None, 0)
    for mid in stay + [deleted]:
        assert (await _state(conn, mid))[0] in ("failed", "skipped")
    assert (await retry.last(conn))["media"] == 3
    assert (await retry.candidates(conn, media_days=30))["media"] == 0     # второй раз — нечего


async def test_since_counts_from_when_it_failed_and_disabled_media_is_left_alone(conn):
    _, chat = await _setup(conn, media=False)
    early = await _failed(conn, chat, 1, "lost_job", marked_days_ago=10)
    late = await _failed(conn, chat, 2, "lost_job", marked_days_ago=1)
    since = datetime.now(timezone.utc) - timedelta(days=3)
    assert (await retry.candidates(conn, media_days=30, since=since))["media"] == 1
    out = await retry.run(conn, media_days=30, since=since)
    assert out["media"] == 0 and out["media_enabled"] is False                # разбор выключен владельцем
    await conn.execute("INSERT INTO setup_state (key, value) VALUES ('media', '{\"enabled\": true}'::jsonb)")
    out = await retry.run(conn, media_days=30, since=since)
    assert out["media"] == 1 and (await _state(conn, late))[0] == "pending" and (await _state(conn, early))[0] == "failed"


async def _fail_all(conn):
    claimed = await jobs.claim(conn, [bridge.LLM_STRUCTURED], worker="test", limit=20)
    for job in claimed:
        await bridge.deliver_failure(conn, job["id"], "UnscopedSecretError", retry_in=None)
    return len(claimed)


async def test_processing_request_given_up_during_the_outage_is_planned_again(conn):
    _, chat = await _setup(conn)
    await _msg(conn, chat, 1, "Пришлю смету по фасаду к пятнице, договорились", age_days=1)
    await pipeline.plan_run(conn, tz=TZ)
    assert await _fail_all(conn) == 1
    for _ in range(pipeline.MAX_REPLANS):                     # прогоны повторяют сами, пока не сдадутся
        await pipeline.plan_run(conn, tz=TZ)
        assert await _fail_all(conn) == 1
    await pipeline.plan_run(conn, tz=TZ)
    assert await conn.fetchval("SELECT count(*) FROM processing_requests WHERE state = 'given_up'") == 1
    assert await _fail_all(conn) == 0                          # сами больше не пробуют

    assert (await retry.candidates(conn, media_days=30))["requests"] == 1
    out = await retry.run(conn, media_days=30)
    assert out["requests"] == 1
    state = json.loads(await conn.fetchval("SELECT value::text FROM settings WHERE key = 'processing.state'"))
    assert state["more"] is True                               # ночной прогон продолжит
    planned = await pipeline.plan_run(conn, tz=TZ)
    assert planned["retried"] == 1
    assert len(await jobs.claim(conn, [bridge.LLM_STRUCTURED], worker="test", limit=20)) == 1


async def test_excluded_chat_requests_are_not_returned(conn):
    _, chat = await _setup(conn)
    await _msg(conn, chat, 1, "Пришлю смету по фасаду к пятнице", age_days=1)
    await pipeline.plan_run(conn, tz=TZ)
    await conn.execute("UPDATE processing_requests SET state = 'given_up'")
    await conn.execute("UPDATE chats SET excluded = true")
    assert (await retry.run(conn, media_days=30))["requests"] == 0


async def test_cli_command_returns_and_logs(conn, monkeypatch, capsys):
    from conftest import DSN
    from shturman import cli

    _, chat = await _setup(conn)
    await _failed(conn, chat, 1, "lost_job")
    monkeypatch.setenv("SHTURMAN_DSN", DSN)
    monkeypatch.setenv("SHTURMAN_MEDIA_DAYS", "30")
    await cli._retry_failed(None, True)
    assert "Можно вернуть: вложений — 1" in capsys.readouterr().out
    assert await conn.fetchval("SELECT media_state FROM messages") == "failed"      # --dry-run ничего не меняет
    await cli._retry_failed("2020-01-01", False)
    out = capsys.readouterr().out
    assert "Возвращено в работу: вложений — 1" in out
    assert await conn.fetchval("SELECT media_state FROM messages") == "pending"
    row = await conn.fetchrow("SELECT action, outcome, detail FROM setup_audit")
    assert tuple(row) == ("retry.failed", "ok", "из терминала; вложений: 1; запросов обработки: 0; с 2020-01-01")
