"""Поиск по архиву. Пока только полнотекстовый, с русской морфологией."""

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
