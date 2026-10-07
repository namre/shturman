"""Гибридный поиск: слова + смысл, слияние рангов, фильтры в обеих ветках, деградация."""

import logging
from datetime import timedelta
from types import SimpleNamespace

import asyncpg
import pytest

from shturman import retrieval, store

from conftest import DSN
from test_embeddings import E5, FakeTEI, T0, add, add_chat, fake_vector, rec

ROW_KEYS = {"id", "tg_message_id", "sent_at", "sender_name", "is_outgoing", "text",
            "chat_id", "chat_title", "chat_type", "snippet", "score"}

IVAN, MARIA = 2001, 2002


def literal(vector):
    return "[" + ",".join(repr(x) for x in vector) + "]"


async def embed(conn, *ids, model=E5):
    """Пишет подставные векторы так, как их записал бы счётчик (с приставкой сообщения)."""
    for row in await conn.fetch("SELECT id, text FROM messages WHERE id = ANY($1::bigint[])", list(ids)):
        await conn.execute(
            "UPDATE messages SET embedding = $2::text::halfvec, embedding_model = $3 WHERE id = $1",
            row["id"], literal(fake_vector("passage: " + row["text"], salt=E5)), model)


def service(tei=None):
    """Состояние сервиса, каким его видит retrieval.find."""
    tei = tei or FakeTEI()
    return SimpleNamespace(extras={"embedder": tei.embedder()}), tei


def mids(rows):
    return [r["tg_message_id"] for r in rows]


async def corpus(conn):
    """Четыре сообщения в одном чате; возвращает идентификаторы строк по номерам сообщений."""
    _, chat_id = await add_chat(conn)
    ids = await add(
        conn, chat_id,
        rec(1, "Пришлю смету по фасадам к пятнице, там всё подробно расписано"),   # слова + смысл
        rec(2, "Бюджет на отделку согласовали вчера вечером"),                     # только смысл
        rec(3, "Смету жду до вечера, пожалуйста не тяните с этим"),                # только слова
        rec(4, "Договор отправили на подпись сегодня утром"),                      # мимо
    )
    by_mid = dict(zip((1, 2, 3, 4), ids))
    await embed(conn, by_mid[1], by_mid[2], by_mid[4])    # у третьего вектора нет: ещё в очереди
    return chat_id, by_mid


async def test_without_embedder_search_is_plain_fulltext(conn):
    await corpus(conn)
    rows = await retrieval.find(SimpleNamespace(extras={}), conn, "смета")
    assert sorted(mids(rows)) == [1, 3]
    assert all(set(r) == ROW_KEYS for r in rows)
    assert all("«" in r["snippet"] for r in rows)


async def test_hit_in_both_branches_ranks_first_and_single_branch_hits_surface(conn):
    await corpus(conn)
    state, tei = service()
    rows = await retrieval.find(state, conn, "смета")
    assert tei.inputs == ["query: смета"]
    assert all(set(r) == ROW_KEYS for r in rows)
    order = mids(rows)
    # первое — найденное и по словам, и по смыслу
    assert order[0] == 1
    # найденное только по словам (вектора ещё нет) и только по смыслу (слова другие) — оба в выдаче
    assert {2, 3} <= set(order)
    # сообщение не по теме — ниже всех остальных
    assert order[-1] == 4
    by = {r["tg_message_id"]: r for r in rows}
    assert by[1]["score"] > max(by[2]["score"], by[3]["score"]) > by[4]["score"] > 0
    assert "«смету»" in by[1]["snippet"] and "«Смету»" in by[3]["snippet"]
    assert by[2]["snippet"] == "Бюджет на отделку согласовали вчера вечером"   # без выделения: слов запроса нет
    assert isinstance(by[1]["score"], float)


async def test_semantic_only_snippet_is_cut_to_300_characters(conn):
    _, chat_id = await add_chat(conn)
    (mid,) = await add(conn, chat_id, rec(1, "Бюджет согласован. " + "Подробности ниже. " * 40))
    await embed(conn, mid)
    state, _ = service()
    (row,) = await retrieval.find(state, conn, "смета")
    assert len(row["snippet"]) == 300 and row["text"].startswith(row["snippet"])


async def test_limit_is_honoured(conn):
    await corpus(conn)
    state, _ = service()
    assert mids(await retrieval.find(state, conn, "смета", limit=1)) == [1]
    assert len(await retrieval.find(state, conn, "смета", limit=2)) == 2


async def test_deleted_and_excluded_are_never_returned_by_either_branch(conn):
    chat_id, by_mid = await corpus(conn)
    _, secret = await add_chat(conn, tg_id=2777, name="Тайный чат")
    hidden = await add(conn, secret, rec(50, "Смета по тайному объекту, никому не показывать"),
                       rec(51, "Бюджет тайного объекта огромный, держим в секрете"))
    await embed(conn, *hidden)
    await conn.execute("UPDATE chats SET excluded = true WHERE id = $1", secret)
    # Удалённые: одно нашлось бы по словам и смыслу, другое только по смыслу. Удаление снимает
    # вектор само (см. миграцию), поэтому возвращаем его вручную — проверяем именно фильтр поиска.
    await store.mark_deleted(conn, chat_id, [1, 2])
    await embed(conn, by_mid[1], by_mid[2])
    state, _ = service()
    assert sorted(mids(await retrieval.find(state, conn, "смета"))) == [3, 4]
    assert mids(await retrieval.find(SimpleNamespace(extras={}), conn, "смета")) == [3]


async def test_vectors_of_another_model_are_ignored(conn):
    _, by_mid = await corpus(conn)
    await conn.execute("UPDATE messages SET embedding_model = 'acme/old-model' WHERE id = $1", by_mid[2])
    state, _ = service()
    order = mids(await retrieval.find(state, conn, "смета"))
    assert 2 not in order and order[0] == 1 and 3 in order


async def filter_corpus(conn):
    """Пары «по словам / по смыслу» по обе стороны каждого фильтра.

    Номера сообщений: десятки — «своё» (должно находиться), сотни — «чужое» (должно отсекаться).
    Нечётное находится только по словам (без вектора), чётное — только по смыслу.
    """
    owner, ivan = await add_chat(conn)
    _, maria = await add_chat(conn, tg_id=MARIA, name="Мария")
    assistant, shared = await add_chat(conn, tg_id=3001, name="Стройка", cls="chat", type_="private_group",
                                       owner=5000, label="Помощник", role="assistant")
    late = T0 + timedelta(days=30)
    words, meaning = "Смету жду, пожалуйста не тяните с ней", "Бюджет на отделку согласовали вчера вечером"
    ids = []
    ids += await add(conn, ivan, rec(11, words), rec(12, meaning))
    ids += await add(conn, ivan, rec(101, words + " (поздно)", at=late), rec(102, meaning + " (поздно)", at=late))
    ids += await add(conn, maria, rec(201, words + " (Мария)", sender=MARIA, name="Мария"),
                     rec(202, meaning + " (Мария)", sender=MARIA, name="Мария"))
    ids += await add(conn, shared, rec(301, words + " (помощник)", sender=4001, name="Прораб"),
                     rec(302, meaning + " (помощник)", sender=4001, name="Прораб"))
    even = await conn.fetch("SELECT id FROM messages WHERE tg_message_id % 2 = 0")
    await embed(conn, *[r["id"] for r in even])
    peers = {r["tg_id"]: r["id"] for r in await conn.fetch("SELECT id, tg_id FROM peers")}
    return SimpleNamespace(owner=owner, assistant=assistant, ivan=ivan, maria=maria, shared=shared,
                           peers=peers, late=late)


@pytest.mark.parametrize("name,expected", [
    ("none", {11, 12, 101, 102, 201, 202, 301, 302}),
    ("account", {11, 12, 101, 102, 201, 202}),
    ("other_account", {301, 302}),
    ("chat", {201, 202}),
    ("sender", {11, 12, 101, 102}),
    ("until", {11, 12, 201, 202, 301, 302}),
    ("since", {101, 102}),
    ("window_and_chat", {11, 12}),
])
async def test_every_filter_applies_to_both_branches(conn, name, expected):
    c = await filter_corpus(conn)
    filters = {
        "none": {},
        "account": {"account_id": c.owner},
        "other_account": {"account_id": c.assistant},
        "chat": {"chat_id": c.maria},
        "sender": {"sender_peer_id": c.peers[IVAN]},
        "until": {"until": c.late},
        "since": {"since": c.late},
        "window_and_chat": {"chat_id": c.ivan, "since": T0, "until": T0 + timedelta(days=1)},
    }[name]
    state, _ = service()
    rows = await retrieval.find(state, conn, "смета", **filters)
    # в выдаче есть и найденное по словам (нечётные), и найденное по смыслу (чётные) — и ничего лишнего
    assert set(mids(rows)) == expected
    # полнотекстовый режим с теми же фильтрами отдаёт ровно «словесную» половину
    plain = await retrieval.find(SimpleNamespace(extras={}), conn, "смета", **filters)
    assert set(mids(plain)) == {m for m in expected if m % 2}


async def test_embedder_failure_degrades_to_fulltext_without_logging_query(conn, caplog):
    await corpus(conn)
    state, tei = service()
    tei.down = True
    with caplog.at_level(logging.DEBUG, logger="shturman"):
        rows = await retrieval.find(state, conn, "смета фасадов")
    # строгое совпадение первым; следом — добор по части слов
    assert mids(rows)[0] == 1 and "«смету»" in rows[0]["snippet"] and set(rows[0]) == ROW_KEYS
    assert "поиск только по словам" in caplog.text
    assert "смета" not in caplog.text and "фасад" not in caplog.text   # запрос в журнал не попал

    # сервер ожил, но идёт пауза после сбоя: поиск не ждёт и к серверу не ходит
    tei.down = False
    assert sorted(mids(await retrieval.find(state, conn, "смета"))) == [1, 3]
    assert tei.requests == []


async def test_slow_embedder_degrades_within_timeout(conn, monkeypatch):
    await corpus(conn)
    state, tei = service()
    tei.delay = 1.0
    embedder = state.extras["embedder"]
    original = embedder.embed_query
    monkeypatch.setattr(embedder, "embed_query", lambda text: original(text, timeout=0.05))
    assert sorted(mids(await retrieval.find(state, conn, "смета"))) == [1, 3]


async def test_bad_answer_from_embedder_degrades_to_fulltext(conn):
    await corpus(conn)
    state, _ = service(FakeTEI(dim=768))
    assert sorted(mids(await retrieval.find(state, conn, "смета"))) == [1, 3]
    state, _ = service(FakeTEI(model="acme/not-what-was-configured"))
    assert sorted(mids(await retrieval.find(state, conn, "смета"))) == [1, 3]


async def test_query_without_searchable_words_still_gets_semantic_results(conn):
    await corpus(conn)
    state, _ = service()
    rows = await retrieval.find(state, conn, "бюджет?")       # слова «бюджет» нет в сообщении 1
    assert mids(rows)[0] == 2
    assert await retrieval.find(state, conn, "   ") == []


async def test_hybrid_works_on_read_only_connection(conn):
    await corpus(conn)
    ro = await asyncpg.connect(DSN, server_settings={"default_transaction_read_only": "on"})
    try:
        state, _ = service()
        assert mids(await retrieval.find(state, ro, "смета"))[0] == 1
        # настройки pgvector действовали только внутри транзакции поиска
        assert await ro.fetchval("SHOW hnsw.iterative_scan") == "off"
    finally:
        await ro.close()
