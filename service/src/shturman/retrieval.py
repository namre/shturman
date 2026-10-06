"""Единая точка поиска по архиву для всего, что отвечает агенту.

Сейчас — полнотекстовый поиск. Когда подключены эмбеддинги, сюда добавляется смысловая ветка
и слияние рангов; вызывающим это не видно.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import asyncpg

from . import search as fts


async def find(
    state: Any, conn: asyncpg.Connection, query: str, *,
    account_id: int | None = None, chat_id: int | None = None, sender_peer_id: int | None = None,
    since: datetime | None = None, until: datetime | None = None, limit: int = 20,
) -> list[dict[str, Any]]:
    """Ищет сообщения. Строка результата: id, tg_message_id, sent_at, sender_name, is_outgoing,
    text, chat_id, chat_title, chat_type, snippet, score. Исключённые чаты и удалённые
    сообщения не участвуют. `state` — состояние сервиса (AppState)."""
    rows = await fts.search(
        conn, query, account_id=account_id, chat_id=chat_id, sender_peer_id=sender_peer_id,
        since=since, until=until, limit=limit,
    )
    for row in rows:
        row["score"] = float(row.pop("rank"))
    return rows
