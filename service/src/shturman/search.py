"""Поиск по архиву: полнотекстовый с русской морфологией и гибридный (слова + смысл).

`search` и `thread` — полнотекстовый поиск и контекст вокруг сообщения. `hybrid` добавляет
смысловую ветку по эмбеддингам и сливает ранги; вызывать его стоит через `retrieval.find`.
"""

# Слияние рангов в `_HYBRID`:
# Основано на pgvector/pgvector-python (MIT), examples/hybrid_search/rrf.py@474522b

from __future__ import annotations

from datetime import datetime
from typing import Any

import asyncpg

_SEARCH = """
WITH q AS (SELECT websearch_to_tsquery('russian', $1) AS query)
SELECT m.id, m.tg_message_id, m.sent_at, m.sender_name, m.is_outgoing, m.text,
       m.chat_id, c.title AS chat_title, c.type AS chat_type,
       ts_rank_cd(m.fts, q.query) AS rank,
       ts_headline('russian', m.text, q.query,
                   'StartSel=«, StopSel=», MaxWords=40, MinWords=12') AS snippet
FROM messages m
JOIN chats c ON c.id = m.chat_id
CROSS JOIN q
WHERE m.fts @@ q.query
  AND m.deleted_at IS NULL
  AND NOT c.excluded
  AND ($2::bigint IS NULL OR c.account_id = $2)
  AND ($3::bigint IS NULL OR m.chat_id = $3)
  AND ($4::bigint IS NULL OR m.sender_peer_id = $4)
  AND ($5::timestamptz IS NULL OR m.sent_at >= $5)
  AND ($6::timestamptz IS NULL OR m.sent_at < $6)
ORDER BY rank DESC, m.sent_at DESC
LIMIT $7
"""

_THREAD = """
WITH anchor AS (SELECT chat_id, sent_at, id FROM messages WHERE id = $1),
before AS (
    SELECT m.* FROM messages m, anchor a
    WHERE m.chat_id = a.chat_id AND (m.sent_at, m.id) < (a.sent_at, a.id) AND m.deleted_at IS NULL
    ORDER BY m.sent_at DESC, m.id DESC LIMIT $2
),
after AS (
    SELECT m.* FROM messages m, anchor a
    WHERE m.chat_id = a.chat_id AND (m.sent_at, m.id) >= (a.sent_at, a.id) AND m.deleted_at IS NULL
    ORDER BY m.sent_at, m.id LIMIT $3 + 1
)
SELECT id, tg_message_id, sent_at, sender_name, is_outgoing, text, kind
FROM (SELECT * FROM before UNION ALL SELECT * FROM after) t
ORDER BY sent_at, id
"""


async def search(
    conn: asyncpg.Connection,
    query: str,
    *,
    account_id: int | None = None,
    chat_id: int | None = None,
    sender_peer_id: int | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Ищет сообщения по словам с учётом русских словоформ. Исключённые чаты не участвуют."""
    rows = await conn.fetch(
        _SEARCH, query, account_id, chat_id, sender_peer_id, since, until, max(1, min(limit, 200))
    )
    return [dict(r) for r in rows]


async def thread(
    conn: asyncpg.Connection, message_id: int, *, before: int = 5, after: int = 5
) -> list[dict[str, Any]]:
    """Возвращает сообщение с соседями по чату — контекст вокруг найденного."""
    rows = await conn.fetch(_THREAD, message_id, before, after)
    return [dict(r) for r in rows]


# Общие условия обеих веток: удалённое и исключённое не участвует, фильтры вызывающего — тоже.
# Параметры: $3 аккаунт, $4 чат, $5 отправитель, $6 «не раньше», $7 «раньше».
_FILTERS = """m.deleted_at IS NULL
      AND NOT c.excluded
      AND ($3::bigint IS NULL OR c.account_id = $3)
      AND ($4::bigint IS NULL OR m.chat_id = $4)
      AND ($5::bigint IS NULL OR m.sender_peer_id = $5)
      AND ($6::timestamptz IS NULL OR m.sent_at >= $6)
      AND ($7::timestamptz IS NULL OR m.sent_at < $7)"""

# Две ветки по $9 кандидатов каждая, затем слияние рангов (Reciprocal Rank Fusion): место
# сообщения в ветке даёт 1 / ($10 + место), вклады веток складываются. Найденное обеими ветками
# оказывается выше найденного одной.
#   * смысловая ветка берёт только векторы текущей модели ($8): после смены модели старые
#     векторы с новым запросом несравнимы;
#   * внутренний запрос смысловой ветки — «ORDER BY расстояние LIMIT n» без оконных функций:
#     в таком виде Postgres может идти по индексу HNSW;
#   * место считается row_number, а не rank: у коротких сообщений ts_rank_cd часто совпадает,
#     и равные места дали бы десяткам строк одинаковый вес; при равенстве раньше идёт свежее.
_HYBRID = f"""
WITH q AS (SELECT websearch_to_tsquery('russian', $1) AS query),
semantic AS (
    SELECT s.id, row_number() OVER (ORDER BY s.distance, s.id) AS rank
    FROM (
        SELECT m.id, m.embedding <=> $2::text::halfvec AS distance
        FROM messages m
        JOIN chats c ON c.id = m.chat_id
        WHERE m.embedding IS NOT NULL
          AND m.embedding_model = $8
          AND {_FILTERS}
        ORDER BY distance
        LIMIT $9
    ) s
),
keyword AS (
    SELECT k.id, row_number() OVER (ORDER BY k.rank DESC, k.sent_at DESC, k.id DESC) AS rank
    FROM (
        SELECT m.id, m.sent_at, ts_rank_cd(m.fts, q.query) AS rank
        FROM messages m
        JOIN chats c ON c.id = m.chat_id
        CROSS JOIN q
        WHERE m.fts @@ q.query
          AND {_FILTERS}
        ORDER BY rank DESC, m.sent_at DESC, m.id DESC
        LIMIT $9
    ) k
),
fused AS (
    SELECT COALESCE(s.id, k.id) AS id,
           COALESCE(1.0 / ($10 + s.rank), 0.0) + COALESCE(1.0 / ($10 + k.rank), 0.0) AS score,
           k.rank IS NOT NULL AS by_words
    FROM semantic s
    FULL OUTER JOIN keyword k ON s.id = k.id
)
SELECT m.id, m.tg_message_id, m.sent_at, m.sender_name, m.is_outgoing, m.text,
       m.chat_id, c.title AS chat_title, c.type AS chat_type,
       CASE WHEN f.by_words
            THEN ts_headline('russian', m.text, q.query,
                             'StartSel=«, StopSel=», MaxWords=40, MinWords=12')
            ELSE left(m.text, 300) END AS snippet,
       f.score::float8 AS score
FROM (SELECT * FROM fused ORDER BY score DESC, id DESC LIMIT $11) f
JOIN messages m ON m.id = f.id
JOIN chats c ON c.id = m.chat_id
CROSS JOIN q
ORDER BY f.score DESC, m.sent_at DESC, m.id DESC
"""

RRF_K = 60


async def hybrid(
    conn: asyncpg.Connection,
    query: str,
    vector: str,
    model: str,
    *,
    account_id: int | None = None,
    chat_id: int | None = None,
    sender_peer_id: int | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Гибридный поиск: слова и смысл, слияние рангов.

    `vector` — эмбеддинг запроса в текстовой записи pgvector, `model` — модель, которой он
    посчитан. Строка результата: id, tg_message_id, sent_at, sender_name, is_outgoing, text,
    chat_id, chat_title, chat_type, snippet, score. Сниппет — выдержка с выделением, если
    сообщение нашлось по словам, иначе первые 300 знаков.
    """
    limit = max(1, min(limit, 200))
    candidates = min(max(4 * limit, 60), 400)
    # Настройки pgvector действуют до конца транзакции:
    #   iterative_scan — с фильтрами обход индекса продолжается, пока не наберётся нужное число
    #   строк (иначе при узком фильтре смысловая ветка вернула бы почти ничего);
    #   ef_search — не меньше числа кандидатов, по умолчанию он 40.
    async with conn.transaction():
        await conn.execute("SET LOCAL hnsw.iterative_scan = strict_order")
        await conn.execute(f"SET LOCAL hnsw.ef_search = {int(candidates)}")
        rows = await conn.fetch(
            _HYBRID, query, vector, account_id, chat_id, sender_peer_id, since, until,
            model, candidates, RRF_K, limit,
        )
    return [dict(r) for r in rows]
