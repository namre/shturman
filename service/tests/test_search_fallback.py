"""Поиск без смысловой ветки: вопрос обычной фразой не должен возвращать пустоту."""

from datetime import datetime, timezone

from shturman import retrieval, store
from shturman.records import ChatRecord, MessageRecord

T0 = datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)


def rec(mid, text):
    return MessageRecord(mid, T0, "message", "user", 2001, "Иван", text, None, None, None, None, None, None, None)


async def fill(conn):
    account_id = await store.ensure_account(conn, 1000, "Владелец")
    chat_id, _ = await store.ensure_chat(conn, account_id, ChatRecord("user", 2001, "personal_chat", "Иван"))
    await store.upsert_messages(conn, [
        (chat_id, rec(1, "Пришлю смету по фасадам к пятнице")),
        (chat_id, rec(2, "Смета готова, фасады посчитаны отдельно, подрядчик согласен")),
        (chat_id, rec(3, "Купи хлеба")),
        (chat_id, rec(4, "It's a 'quoted' смета")),
    ], source="import", owner_tg_id=1000)


async def test_natural_question_finds_partial_matches_strict_hits_first(conn):
    await fill(conn)
    rows = await retrieval.find(None, conn, "когда подрядчик пришлёт смету по фасадам")
    ids = [r["id"] for r in rows]
    texts = [r["text"] for r in rows]
    assert len(ids) == 3 and "Купи хлеба" not in texts
    assert [r["score"] for r in rows] == sorted((r["score"] for r in rows), reverse=True)
    strict = await retrieval.find(None, conn, "смета фасады подрядчик")
    assert strict[0]["text"].startswith("Смета готова")   # все слова сразу — первым


async def test_operators_are_not_relaxed(conn):
    await fill(conn)
    assert await retrieval.find(None, conn, '"смета по окнам"') == []
    assert await retrieval.find(None, conn, "смета -фасадам -фасады -quoted") == []


async def test_odd_characters_and_stop_words_do_not_break_the_query(conn):
    await fill(conn)
    assert await retrieval.find(None, conn, "и в на") == []
    rows = await retrieval.find(None, conn, "it's 'quoted' & | ! ( ) : * хлеб")
    assert {r["text"] for r in rows} >= {"Купи хлеба"}
