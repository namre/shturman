"""Обновление на 0045 (Р-105): включённый при «подготовке без доставки» автоответ не начинает отвечать сам.

До 0045 автоответ доверенным можно было включить при выключенной отправке, пока шла подготовка без доставки
(`/prepare on` — настройка `outbox.prepare_only`, или `SHTURMAN_PREPARE_ONLY` в `.env`): он готовил только
черновики для просмотра. Режима больше нет, поэтому миграция выключает автоответ у всех аккаунтов, а сервис
один раз сообщает об этом владельцу. Включить снова — только при включённой отправке.
"""

import dataclasses
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from outbox_helpers import IVAN, OWNER, FakeTg, add_chat, add_message  # noqa: E402

from shturman import authority, bridge, db, store  # noqa: E402
from shturman.outbox import autoreply, policy  # noqa: E402
from shturman.outbox import service as outbox_service  # noqa: E402


async def _migrate_until(conn, stop: str) -> None:
    """Схема до миграции `stop` (не включая её) — как у экземпляра прежней версии."""
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await conn.execute(db._MIGRATIONS_TABLE)
    for name, sql in db._migration_files():
        if name >= stop:
            break
        async with conn.transaction():
            await conn.execute(sql)
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)


@pytest.mark.asyncio
async def test_0045_turns_autoreply_off_tells_the_owner_once_and_nothing_answers(conn, make_client, config):
    await _migrate_until(conn, "0045")
    owner_acc = await store.ensure_account(conn, OWNER, "Владелец", "owner")
    helper_acc = await store.ensure_account(conn, 1500, "Помощник", "assistant")
    await conn.execute("INSERT INTO outbox_accounts (account_id, autoreply_enabled) VALUES ($1, true), ($2, true)",
                       owner_acc, helper_acc)
    await conn.execute("""INSERT INTO settings (key, value) VALUES ('outbox.prepare_only', '{"enabled": true}')""")
    await conn.execute("INSERT INTO outbox_trusted (tg_user_id) VALUES ($1)", IVAN)

    applied = await db.migrate(conn)
    assert applied[0] == "0045_reply_drafts.sql"
    assert await conn.fetchval("SELECT bool_or(autoreply_enabled) FROM outbox_accounts") is False
    assert await conn.fetchval("SELECT count(*) FROM settings WHERE key = 'outbox.prepare_only'") == 0
    stored = await conn.fetchval("SELECT value FROM settings WHERE key = $1", outbox_service.UPGRADE_AUTOREPLY_KEY)
    assert json.loads(stored) == {"accounts": 2}

    chat_id = await add_chat(conn, helper_acc)
    mid = await add_message(conn, chat_id, 1, "Привет, когда смета?")
    tgt = await policy.target(conn, chat_id)
    assert (await autoreply.eligible(conn, tgt, mid)).ok is False

    # Владелец не привязан — сообщение ждёт; привязан — одно сообщение, и больше не повторяется.
    assert await outbox_service.announce_upgrade_conn(conn) is False
    with authority.owner_context(OWNER, chat_id=OWNER, action="test.fixture.bind"):
        await bridge.set_owner(conn, OWNER, OWNER)
    assert await outbox_service.announce_upgrade_conn(conn) is True
    assert await outbox_service.announce_upgrade_conn(conn) is False
    texts = [json.loads(r["payload"])["text"] for r in await conn.fetch(
        "SELECT payload::text AS payload FROM jobs WHERE kind = $1", bridge.NOTIFY_OWNER)]
    assert len([t for t in texts if t.startswith("Автоответ доверенным выключен при обновлении")]) == 1
    assert "аккаунтов: 2" in texts[-1] and "когда включена отправка" in texts[-1]

    # Сервис с включённой отправкой и сессией помощника на связи: доверенный пишет — никто не отвечает,
    # пока владелец не включит автоответ снова.
    _, state = await make_client("shturman.api_core", "shturman.outbox.service",
                                 cfg=dataclasses.replace(config, sending=True, send_daily_hard_cap=1000))
    tg = FakeTg()
    tg.sendable.update({owner_acc, helper_acc})
    state.extras["tg"] = tg
    with authority.owner_context(OWNER, chat_id=OWNER, action="test.fixture.autoreply"):
        await autoreply.update(conn, {"debounce_seconds": 0, "pause_seconds": 0})
    later = await add_message(conn, chat_id, 2, "Ау, когда смета?")
    await autoreply.on_message(state.extras["outbox"], {
        "account_id": helper_acc, "chat_id": chat_id, "message_id": later, "source": "session",
        "outgoing": False, "edited": False, "via_bot": False})
    await state.events.drain()
    assert await conn.fetchval("SELECT count(*) FROM reply_tasks") == 0
    assert await conn.fetchval("SELECT count(*) FROM outbox_drafts") == 0
    assert await conn.fetchval("SELECT count(*) FROM jobs WHERE kind = $1", bridge.LLM_STRUCTURED) == 0
    assert tg.calls == 0 and tg.sent == []


@pytest.mark.asyncio
async def test_0045_with_autoreply_off_says_nothing(conn):
    await _migrate_until(conn, "0045")
    acc = await store.ensure_account(conn, OWNER, "Владелец", "owner")
    await conn.execute("INSERT INTO outbox_accounts (account_id, autoreply_enabled) VALUES ($1, false)", acc)
    await db.migrate(conn)
    assert await conn.fetchval("SELECT count(*) FROM settings WHERE key = $1",
                               outbox_service.UPGRADE_AUTOREPLY_KEY) == 0


@pytest.mark.asyncio
async def test_doctor_sees_autoreply_and_a_pending_upgrade_message(conn, make_client, config):
    """Д-51: оператор без входа в базу видит, включён ли автоответ доверенным и ушло ли владельцу сообщение
    о выключении при обновлении (`replies.autoreply` в `GET /api/status` → строка `replies` в doctor)."""
    from shturman.replies import delivery

    _, state = await make_client("shturman.api_core", "shturman.outbox.service")
    acc = await store.ensure_account(conn, OWNER, "Владелец", "owner")
    await conn.execute("INSERT INTO outbox_accounts (account_id, autoreply_enabled) VALUES ($1, false)", acc)
    found = await delivery.summary(conn, state)
    assert found["autoreply"] == {"accounts_on": 0, "announce_pending": False}

    await conn.execute("""INSERT INTO settings (key, value) VALUES ($1, '{"accounts": 1}')""",
                       outbox_service.UPGRADE_AUTOREPLY_KEY)
    assert (await delivery.summary(conn, state))["autoreply"]["announce_pending"] is True

    with authority.owner_context(OWNER, chat_id=OWNER, action="test.fixture.bind"):
        await bridge.set_owner(conn, OWNER, OWNER)
    assert await outbox_service.announce_upgrade_conn(conn) is True
    assert (await delivery.summary(conn, state))["autoreply"]["announce_pending"] is False

    await conn.execute("UPDATE outbox_accounts SET autoreply_enabled = true WHERE account_id = $1", acc)
    assert (await delivery.summary(conn, state))["autoreply"]["accounts_on"] == 1
