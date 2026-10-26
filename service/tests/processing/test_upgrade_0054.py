"""Обновление на 0054 (Р-111): статус проекта без уточнения больше не сменяет статус другой работы.

На пилоте цепочка «статус» одного проекта связала семь разных работ: каждое новое значение закрывало
прежнее. Миграция открывает такие значения (каждое — положение дел на свою дату), а записанное с
приметами момента («сейчас», «пока», «на 29.09») помечает положением дел. Профиль владельца не трогается."""

from datetime import date

from shturman import db

from proc_helpers import OWNER, account, chat, say
from test_memory_projects import BRIGADE


async def _migrate_until(conn, stop: str) -> None:
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await conn.execute(db._MIGRATIONS_TABLE)
    for name, sql in db._migration_files():
        if name >= stop:
            break
        async with conn.transaction():
            await conn.execute(sql)
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)


async def _fact(conn, mid, subject, text, *, slot=None, project=None, valid_from, chained=True, status="active"):
    return await conn.fetchval(
        """INSERT INTO facts (subject_type, project_id, slot, text, text_norm, valid_from, status, origin,
                              source_message_id, source_quote, chained, applied_at)
           VALUES ($1, $2, $3, $4, lower($4), $5, $6, 'owner', $7, $4, $8, now()) RETURNING id""",
        subject, project, slot, text, valid_from, status, mid, chained)


async def test_0054_reopens_unqualified_status_chains_and_marks_moments(conn):
    await _migrate_until(conn, "0054")
    acc = await account(conn)
    group = await chat(conn, acc, BRIGADE, "Стройка", type_="private_supergroup", cls="channel")
    mids = await say(conn, group, [(OWNER, "Евгений Тестов", f"сообщение {n}") for n in range(5)])
    project = await conn.fetchval(
        "INSERT INTO projects (title, title_norm, status, origin) VALUES ('ЖК', 'жк', 'active', 'owner') RETURNING id")
    old = await _fact(conn, mids[0], "project", "Сервис заработал", slot="статус", project=project,
                      valid_from=date(2026, 10, 1))
    new = await _fact(conn, mids[1], "project", "Торги закрыты", slot="статус", project=project,
                      valid_from=date(2026, 10, 5))
    await conn.execute("UPDATE facts SET valid_to = '2026-10-05', superseded_by = $2 WHERE id = $1", old, new)
    budget = await _fact(conn, mids[2], "project", "Бюджет 9 млн", slot="бюджет", project=project,
                         valid_from=date(2026, 10, 1), chained=False)
    moment = await _fact(conn, mids[3], "project", "На 29.09 лимит 49 млрд", project=project,
                         valid_from=date(2026, 10, 1))
    profile = await _fact(conn, mids[4], "owner", "Сейчас в Екатеринбурге", slot="город",
                          valid_from=date(2026, 10, 1))
    await conn.execute(
        "INSERT INTO pages (entity_type, entity_id, project_id, path, title, dirty) "
        "VALUES ('project', $1, $2, 'projects/zh.md', 'ЖК', false)", f"project:{project}", project)

    applied = await db.migrate(conn)
    assert applied[0] == "0054_fact_nature.sql"
    rows = {r["id"]: r for r in await conn.fetch("SELECT * FROM facts")}
    assert (rows[old]["valid_to"], rows[old]["superseded_by"], rows[old]["chained"]) == (None, None, False)
    assert {rows[old]["nature"], rows[new]["nature"], rows[moment]["nature"]} == {"observation"}
    assert rows[budget]["nature"] == "state"
    assert (rows[profile]["nature"], rows[profile]["chained"]) == ("state", True)    # профиль не трогается
    assert all(r["slot_detail"] is None and r["slot_detail_norm"] == "" for r in rows.values())
    assert await conn.fetchval("SELECT dirty FROM pages WHERE project_id = $1", project) is True
