"""Единая точка поиска по архиву для всего, что отвечает агенту.

Если подключён сервер эмбеддингов — гибридный поиск: смысловая ветка и полнотекстовая, слияние
рангов. Если не подключён или не ответил за отведённое время — только полнотекстовый.
Вызывающим разница не видна: форма строки результата одна.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import asyncpg

from . import search as fts
from .embeddings import EmbedderError, vector_literal

logger = logging.getLogger("shturman.retrieval")


async def _query_vector(state: Any, query: str) -> tuple[str, str] | None:
    """Эмбеддинг запроса и имя модели либо None, если смысловая ветка сейчас недоступна."""
    embedder = (getattr(state, "extras", None) or {}).get("embedder")
    if embedder is None or not query.strip():
        return None
    try:
        vector = await embedder.embed_query(query)
    except EmbedderError as exc:
        # Текст запроса в журнал не пишем — только вид сбоя. Во время паузы после сбоя
        # предупреждение не повторяем: о самом сбое уже сказано.
        logger.log(logging.DEBUG if exc.cooldown else logging.WARNING,
                   "эмбеддинг запроса не получен (%s): поиск только по словам", exc.reason)
        return None
    return vector_literal(vector), embedder.model


async def find(
    state: Any, conn: asyncpg.Connection, query: str, *,
    account_id: int | None = None, chat_id: int | None = None, sender_peer_id: int | None = None,
    since: datetime | None = None, until: datetime | None = None, limit: int = 20,
) -> list[dict[str, Any]]:
    """Ищет сообщения. Строка результата: id, tg_message_id, sent_at, sender_name, is_outgoing,
    text, chat_id, chat_title, chat_type, snippet, score. Исключённые чаты и удалённые
    сообщения не участвуют. `state` — состояние сервиса (AppState).

    `score` годится только для порядка внутри одного ответа: в гибридном поиске это сумма
    вкладов двух веток (слияние рангов), в полнотекстовом — ранг Postgres.
    """
    filters = dict(account_id=account_id, chat_id=chat_id, sender_peer_id=sender_peer_id,
                   since=since, until=until, limit=limit)
    semantic = await _query_vector(state, query)
    if semantic is not None:
        vector, model = semantic
        return await fts.hybrid(conn, query, vector, model, **filters)
    rows = await fts.search(conn, query, **filters)
    if len(rows) < limit and _plain_phrase(query):
        # Без смысловой ветки строгий поиск «все слова сразу» на обычный вопрос чаще всего пуст.
        # Добираем сообщениями, где совпала часть слов; строгие совпадения остаются первыми.
        seen = {row["id"] for row in rows}
        relaxed = await fts.search(conn, query, **filters, any_word=True)
        rows += [row for row in relaxed if row["id"] not in seen][: limit - len(rows)]
    for place, row in enumerate(rows):
        row.pop("rank")
        row["score"] = 1.0 / (1 + place)
    return rows


def _plain_phrase(query: str) -> bool:
    """Обычная фраза из нескольких слов, без операторов поиска: её можно ослабить до «любое слово»."""
    words = query.split()
    return len(words) > 1 and '"' not in query and not any(w == "OR" or w.startswith("-") for w in words)
