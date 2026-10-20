import copy

import pytest

from shturman.importer import import_export
from shturman.search import search, thread

from conftest import IVAN, MARIA, OWNER, as_file, msg


async def test_import_counts_and_outgoing(conn, sample_export):
    stats = await import_export(conn, as_file(sample_export))
    assert stats.owner_tg_user_id == OWNER
    assert (stats.chats, stats.messages_read, stats.messages_new) == (4, 8, 8)
    rows = await conn.fetch(
        "SELECT tg_message_id, is_outgoing, sources FROM messages m JOIN chats c ON c.id = m.chat_id "
        "JOIN peers p ON p.id = c.peer_id WHERE p.tg_id = $1 ORDER BY 1", IVAN)
    assert [r["is_outgoing"] for r in rows] == [False, True, False, False, False, False]
    assert rows[0]["sources"] == ["import"]
    assert await conn.fetchval("SELECT role FROM accounts WHERE tg_user_id = $1", OWNER) == "owner"


async def test_reimport_is_idempotent(conn, sample_export):
    await import_export(conn, as_file(sample_export))
    again = await import_export(conn, as_file(sample_export))
    assert (again.messages_new, again.messages_known, again.versions_added) == (0, 8, 0)
    assert await conn.fetchval("SELECT count(*) FROM messages") == 8
    assert await conn.fetchval("SELECT count(*) FROM accounts") == 1


async def test_newer_edit_replaces_text_and_keeps_history(conn, sample_export):
    await import_export(conn, as_file(sample_export))
    newer = copy.deepcopy(sample_export)
    m = newer["chats"]["list"][0]["messages"][0]
    m["text"] = "Добрый день! Пришлю смету по фасадам к понедельнику."
    m["edited_unixtime"] = str(1789300000)
    stats = await import_export(conn, as_file(newer))
    assert stats.versions_added == 1
    row = await conn.fetchrow(
        "SELECT m.id, m.text, m.edited_at FROM messages m JOIN chats c ON c.id = m.chat_id "
        "JOIN peers p ON p.id = c.peer_id WHERE p.tg_id = $1 AND m.tg_message_id = 1", IVAN)
    assert "понедельнику" in row["text"] and row["edited_at"] is not None
    old = await conn.fetchval("SELECT text FROM message_versions WHERE message_id = $1", row["id"])
    assert "пятнице" in old


async def test_older_export_does_not_overwrite_newer_text(conn, sample_export):
    newer = copy.deepcopy(sample_export)
    m = newer["chats"]["list"][0]["messages"][0]
    m["text"] = "Пришлю смету к понедельнику."
    m["edited_unixtime"] = str(1789300000)
    await import_export(conn, as_file(newer))
    # затем загружают старый экспорт, где правки ещё нет
    stats = await import_export(conn, as_file(sample_export))
    text = await conn.fetchval(
        "SELECT m.text FROM messages m JOIN chats c ON c.id = m.chat_id "
        "JOIN peers p ON p.id = c.peer_id WHERE p.tg_id = $1 AND m.tg_message_id = 1", IVAN)
    assert "понедельнику" in text
    assert stats.versions_added == 1  # старая формулировка сохранена как версия
    # третий заход ничего не добавляет
    assert (await import_export(conn, as_file(sample_export))).versions_added == 0


async def test_excluded_chat_never_enters_archive(conn, sample_export):
    stats = await import_export(conn, as_file(sample_export), exclude={("chat", 3001)})
    assert stats.chats_excluded == 1 and stats.excluded_names == ["Семья"]
    assert await conn.fetchval("SELECT count(*) FROM messages") == 7
    assert await conn.fetchval(
        "SELECT count(*) FROM messages m JOIN peers p ON p.id = m.sender_peer_id WHERE p.tg_id = $1", MARIA) == 0
    # запрет запомнен: повторный импорт без --exclude чат не возвращает
    again = await import_export(conn, as_file(sample_export))
    assert again.chats_excluded == 1
    assert await conn.fetchval("SELECT count(*) FROM messages") == 7


async def test_single_chat_export_needs_owner(conn):
    single = {"name": "Иван Петров", "type": "personal_chat", "id": IVAN,
              "messages": [msg(1, 1789200000, OWNER, "Евгений", "Привет")]}
    with pytest.raises(ValueError):
        await import_export(conn, as_file(single))
    stats = await import_export(conn, as_file(single), owner_tg_user_id=OWNER)
    assert stats.messages_new == 1
    assert await conn.fetchval("SELECT is_outgoing FROM messages") is True


async def test_owner_mismatch_is_rejected(conn, sample_export):
    with pytest.raises(ValueError):
        await import_export(conn, as_file(sample_export), owner_tg_user_id=42)


async def test_search_understands_russian_word_forms(conn, sample_export):
    await import_export(conn, as_file(sample_export))
    hits = await search(conn, "смета фасад")           # в тексте: «смету по фасадам»
    assert len(hits) == 1 and hits[0]["tg_message_id"] == 1
    assert "«" in hits[0]["snippet"]
    assert [h["tg_message_id"] for h in await search(conn, "сроки монтаж")] == [2]
    assert await search(conn, "бетон") == []


async def test_search_filters_and_excluded_chats(conn, sample_export):
    await import_export(conn, as_file(sample_export))
    assert len(await search(conn, "хлеб")) == 1
    await conn.execute(
        "UPDATE chats SET excluded = true WHERE peer_id = (SELECT id FROM peers WHERE class='chat' AND tg_id=3001)")
    assert await search(conn, "хлеб") == []
    ivan_peer = await conn.fetchval("SELECT id FROM peers WHERE class='user' AND tg_id=$1", IVAN)
    assert await search(conn, "сроки", sender_peer_id=ivan_peer) == []


async def test_thread_returns_neighbours_in_order(conn, sample_export):
    await import_export(conn, as_file(sample_export))
    hit = (await search(conn, "сроки монтажа"))[0]
    ctx = await thread(conn, hit["id"], before=1, after=1)
    assert [m["tg_message_id"] for m in ctx] == [1, 2, 3]


async def test_batches_larger_than_one_flush(conn, monkeypatch):
    import shturman.importer as imp
    monkeypatch.setattr(imp, "BATCH", 50)
    chat = {"name": "Большой", "type": "personal_chat", "id": IVAN,
            "messages": [msg(i, 1789200000 + i, IVAN if i % 2 else OWNER, "X", f"сообщение номер {i}")
                         for i in range(1, 231)]}
    from conftest import full_export
    stats = await import_export(conn, as_file(full_export([chat])))
    assert stats.messages_new == 230
    assert await conn.fetchval("SELECT count(*) FROM messages") == 230
