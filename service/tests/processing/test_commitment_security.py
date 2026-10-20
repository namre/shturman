"""Регрессии: статус/actor не заменяют независимого согласия, в том числе под row lock."""
import asyncio
from datetime import date, datetime, timezone

import asyncpg
import pytest

from shturman import authority
from shturman.processing import commitments
from conftest import DSN
from proc_helpers import OWNER, account, chat, say


def content():
    return dict(chat_id=1, source_message_id=2, due_message_id=None, debtor_peer_id=3,
                creditor_peer_id=None, direction="owed_to_owner", what="Прислать смету",
                source_quote="Пришлю смету", due_expression=None, due_date=None,
                due_time=None, due_part=None, status="proposed", approved_at=None,
                approved_by=None, approved_via=None, approval_fingerprint=None)


def test_status_timestamp_and_actor_are_not_approval():
    row = content()
    row.update(status="open", decided_at=datetime.now(timezone.utc), actor="owner")
    assert not commitments.approved(row)
    assert commitments._actor("owner") == "agent"
    with authority.owner_context(OWNER):
        assert commitments._actor("agent") == "owner"
    assert commitments._actor("owner") == "agent"


def test_approval_fingerprint_ignores_status_but_binds_material_content():
    row = content()
    row.update(approved_at=datetime.now(timezone.utc), approved_by=str(OWNER), approved_via="telegram")
    row["approval_fingerprint"] = commitments.fingerprint(row)
    row["status"] = "cancelled"
    assert commitments.approved(row)
    row["due_date"] = date(2026, 11, 1)
    assert not commitments.approved(row)


async def proposed(conn):
    a = await account(conn)
    c = await chat(conn, a, 2001, "Тестовый собеседник")
    m, = await say(conn, c, [(2001, "Собеседник", "Пришлю смету")])
    return await conn.fetchval(
        """INSERT INTO commitments (chat_id, source_message_id, direction, what, source_quote)
           VALUES ($1, $2, 'owed_to_owner', 'Прислать смету', 'Пришлю смету') RETURNING id""", c, m)


async def test_cancel_reopen_does_not_manufacture_consent(conn):
    item = await proposed(conn)
    assert (await commitments.cancel(conn, item, actor="owner"))["ok"]
    out = await commitments.reopen(conn, item, actor="owner")
    assert not out["ok"] and out["code"] == "approval_required"
    row = await conn.fetchrow("SELECT * FROM commitments WHERE id = $1", item)
    assert row["status"] == "cancelled" and row["approved_at"] is None
    assert row["decided_at"] is not None  # историческое поле не даёт полномочий
    assert await conn.fetchval("SELECT actor FROM commitment_events WHERE commitment_id = $1", item) == "agent"
    with authority.owner_context(OWNER):
        assert (await commitments.reopen(conn, item))["ok"]
    row = await conn.fetchrow("SELECT * FROM commitments WHERE id = $1", item)
    assert commitments.approved(row) and row["approved_by"] == str(OWNER)
    assert row["approved_via"] == "telegram"
    assert (await commitments.cancel(conn, item))["ok"]
    assert (await commitments.reopen(conn, item))["ok"]
    assert await conn.fetchval(
        "SELECT actor FROM commitment_events WHERE commitment_id = $1 ORDER BY id DESC LIMIT 1", item) == "agent"


async def test_legacy_open_noop_still_requires_real_owner(conn):
    item = await proposed(conn)
    await conn.execute("UPDATE commitments SET status = 'open', decided_at = now() WHERE id = $1", item)
    for command in (commitments.accept, commitments.reopen, commitments.close):
        assert (await command(conn, item, actor="owner"))["code"] == "approval_required"
    with authority.setup_context("synthetic-session"):
        assert (await commitments.accept(conn, item))["ok"]
    assert (await commitments.close(conn, item))["ok"]


async def test_proposed_dates_can_change_but_approved_dates_require_owner(conn):
    item = await proposed(conn)
    assert (await commitments.reschedule(conn, item, "2026-11-01", tz="UTC"))["ok"]
    assert await conn.fetchval("SELECT approved_at FROM commitments WHERE id = $1", item) is None
    with authority.owner_context(OWNER):
        assert (await commitments.accept(conn, item))["ok"]
    assert (await commitments.reschedule(conn, item, "2026-12-01", tz="UTC"))["code"] == "approval_required"
    assert await conn.fetchval("SELECT due_date FROM commitments WHERE id = $1", item) == date(2026, 11, 1)


async def test_changed_content_is_rechecked_after_waiting_for_row_lock(conn):
    item = await proposed(conn)
    before = commitments.fingerprint(await conn.fetchrow("SELECT * FROM commitments WHERE id = $1", item))
    other = await asyncpg.connect(DSN)
    started = asyncio.Event()
    async def delayed_owner_accept():
        with authority.owner_context(OWNER):
            started.set()
            return await commitments.accept(other, item, expected_fingerprint=before)
    try:
        async with conn.transaction():
            await conn.fetchrow("SELECT id FROM commitments WHERE id = $1 FOR UPDATE", item)
            task = asyncio.create_task(delayed_owner_accept())
            await started.wait()
            await conn.execute("UPDATE commitments SET what = 'Другое обещание' WHERE id = $1", item)
        out = await task
        assert not out["ok"] and out["code"] == "changed_meanwhile"
        assert await conn.fetchval("SELECT approved_at FROM commitments WHERE id = $1", item) is None
    finally:
        await other.close()
