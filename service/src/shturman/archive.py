"""Запросы чтения архива — то, из чего собираются ответы агенту.

Здесь только SELECT. Все функции принимают соединение (в сервисе — из `state.ro_pool`) и
возвращают словари с «сырыми» значениями: чистка текста и перевод времени в пояс владельца —
дело вызывающего (`mcp_server.py`).

Три правила видимости зашиты в каждый запрос и проверены тестами:
  * чат с признаком `chats.excluded` не виден: ни он сам, ни его сообщения, ни счётчики;
  * сообщение с отметкой `deleted_at` не видно;
  * служебные собеседники Telegram (`store.is_blocked_peer`: уведомления 777000, @BotFather,
    @SpamBot) не видны ни как чат, ни как отправитель, ни как найденный человек.
Третье правило проверяется дважды: условием в SQL (собранным из списков `store.py`) и повторно
в коде функцией `store.is_blocked_peer` — для каждой возвращаемой строки: чата, сообщения,
человека. Счётчики и время последнего сообщения считает база, для них проверка одна — в SQL.

Поиск по словам здесь не живёт: он идёт только через `retrieval.find`. Но его результат
перед выдачей проходит `visible_messages` — те же три правила.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

import asyncpg

from . import store

CHAT_KINDS = ("user", "bot", "group", "channel")

# Порог похожести для «возможно, вы имели в виду». Ниже стандартных 0.6: опечатка в коротком
# имени иначе не находится. Такие совпадения никогда не выбираются молча — только как кандидаты.
_SIMILARITY = "0.45"

_BLOCKED_IDS = ", ".join(str(int(i)) for i in sorted(store.BLOCKED_USER_IDS))
for _name in store.BLOCKED_USERNAMES:
    if not re.fullmatch(r"[a-z0-9_]+", _name):  # значения подставляются в текст запроса
        raise RuntimeError("store.BLOCKED_USERNAMES: допустимы только латиница, цифры и подчёркивание")
_BLOCKED_NAMES = ", ".join(f"'{n}'" for n in sorted(store.BLOCKED_USERNAMES))


def _peer_ok(alias: str, *, optional: bool = False) -> str:
    """Условие «собеседник не служебный». optional — для LEFT JOIN, где строки может не быть."""
    blocked = (
        f"({alias}.class = 'user' AND ({alias}.tg_id IN ({_BLOCKED_IDS}) "
        f"OR lower(ltrim(COALESCE({alias}.username, ''), '@')) IN ({_BLOCKED_NAMES})))"
    )
    return f"({alias}.id IS NULL OR NOT {blocked})" if optional else f"NOT {blocked}"


# Вид чата для агента: user (личный), bot, group, channel.
_KIND = """CASE WHEN cp.class = 'user'
                THEN CASE WHEN c.type = 'bot_chat' OR cp.is_bot IS TRUE THEN 'bot' ELSE 'user' END
                WHEN cp.class = 'chat' THEN 'group'
                WHEN c.type LIKE '%channel' THEN 'channel'
                ELSE 'group' END"""

_CHAT_NAME = """COALESCE(NULLIF(c.title, ''), NULLIF(cp.name, ''),
                         CASE WHEN c.type = 'saved_messages' THEN 'Saved Messages' END)"""

# Видимые чаты. Псевдонимы c (чат) и cp (собеседник чата) используются и в других запросах.
# a — аккаунт, с точки зрения которого виден чат (владелец или помощник).
_VISIBLE_CHATS = f"""
SELECT c.id, {_KIND} AS kind, {_CHAT_NAME} AS name, cp.username,
       a.label AS account_label, a.role AS account_role,
       cp.class AS peer_class, cp.tg_id AS peer_tg_id
FROM chats c
JOIN peers cp ON cp.id = c.peer_id
JOIN accounts a ON a.id = c.account_id
WHERE NOT c.excluded AND {_peer_ok('cp')}
"""

# Видимые сообщения одного чата — для времени последнего сообщения и счётчика. Служебные
# отправители здесь отсекаются по списку их идентификаторов: он вычисляется один раз на запрос,
# и соединять каждое сообщение с таблицей собеседников не приходится.
_BLOCKED_PEER_IDS = f"ARRAY(SELECT bp.id FROM peers bp WHERE NOT ({_peer_ok('bp')}))"
_VISIBLE_MESSAGE_OF_CHAT = f"""
        FROM messages m
        WHERE m.chat_id = {{chat}} AND m.deleted_at IS NULL
          AND (m.sender_peer_id IS NULL OR m.sender_peer_id <> ALL ({_BLOCKED_PEER_IDS}))"""

# Сначала выбирается страница чатов по времени последнего сообщения (один шаг по индексу на чат),
# и только для неё считаются сообщения: подсчёт по всему архиву на каждый вызов не нужен.
_LIST_CHATS = f"""
WITH visible AS ({_VISIBLE_CHATS}), page AS (
    SELECT v.*, last.sent_at AS last_message_at
    FROM visible v
    LEFT JOIN LATERAL (
        SELECT m.sent_at {_VISIBLE_MESSAGE_OF_CHAT.format(chat='v.id')}
        ORDER BY m.sent_at DESC LIMIT 1
    ) last ON true
    WHERE ($1::text[] IS NULL OR v.kind = ANY($1))
      AND ($2::text[] IS NULL OR v.kind <> ALL($2))
      AND ($3::text IS NULL OR v.name ILIKE $4 OR v.username ILIKE $4 OR $3 <% v.name)
      AND ($6::text IS NULL OR v.account_role = $6 OR lower(v.account_label) = lower($6))
    ORDER BY last.sent_at DESC NULLS LAST, v.id DESC
    LIMIT $5
)
SELECT page.*, (SELECT count(*) {_VISIBLE_MESSAGE_OF_CHAT.format(chat='page.id')}) AS message_count
FROM page
ORDER BY page.last_message_at DESC NULLS LAST, page.id DESC
"""

_ACCOUNTS = "SELECT label, role FROM accounts ORDER BY (role = 'owner') DESC, id"

_CHAT_BY_ID = f"SELECT v.* FROM ({_VISIBLE_CHATS}) v WHERE v.id = $1"

# Кандидаты по имени. Ступень: 0 — имя или адрес совпали целиком, 1 — все слова запроса есть
# в названии, 2 — только похоже (опечатка, другая форма).
_CHAT_CANDIDATES = f"""
WITH visible AS ({_VISIBLE_CHATS}), cand AS (
    SELECT v.*,
           CASE WHEN lower(v.name) = lower($1) OR lower(v.username) = lower($2) THEN 0
                WHEN v.name ILIKE ALL($3::text[]) THEN 1
                ELSE 2 END AS tier,
           word_similarity($1, COALESCE(v.name, '')) AS score
    FROM visible v
    WHERE v.name ILIKE ALL($3::text[]) OR $1 <% v.name OR lower(v.username) = lower($2)
)
SELECT cand.*,
       (SELECT max(m.sent_at) FROM messages m
        WHERE m.chat_id = cand.id AND m.deleted_at IS NULL) AS last_message_at
FROM cand
ORDER BY tier, last_message_at DESC NULLS LAST, score DESC, id
LIMIT $4
"""

# Человек показывается, только если у него есть видимый след: неисключённый личный чат или
# хотя бы одно неудалённое сообщение в неисключённом чате. Иначе по имени можно было бы
# узнать о существовании исключённого чата.
_PEOPLE = f"""
WITH cand AS (
    SELECT p.id, p.class, p.tg_id, p.name, p.username, p.is_bot,
           CASE WHEN lower(p.name) = lower($1) OR lower(p.username) = lower($2) THEN 0
                WHEN p.name ILIKE ALL($3::text[]) OR (length($2) >= 3 AND p.username ILIKE $5) THEN 1
                ELSE 2 END AS tier,
           GREATEST(word_similarity($1, COALESCE(p.name, '')),
                    similarity(COALESCE(p.username, ''), $2)) AS score
    FROM peers p
    WHERE p.class = ANY($4::text[]) AND {_peer_ok('p')}
      AND (p.name ILIKE ALL($3::text[]) OR $1 <% p.name OR lower(p.username) = lower($2)
           OR (length($2) >= 3 AND p.username ILIKE $5))
)
SELECT cand.*, dc.id AS direct_chat_id, GREATEST(dl.at, sl.at) AS last_interaction_at,
       EXISTS (SELECT 1 FROM accounts a
               WHERE cand.class = 'user' AND a.tg_user_id = cand.tg_id) AS is_self
FROM cand
LEFT JOIN LATERAL (
    SELECT c.id FROM chats c JOIN accounts a ON a.id = c.account_id
    WHERE c.peer_id = cand.id AND cand.class = 'user' AND NOT c.excluded
    ORDER BY (a.role = 'owner') DESC, c.id LIMIT 1
) dc ON true
LEFT JOIN LATERAL (
    SELECT max(m.sent_at) AS at FROM messages m
    WHERE m.chat_id = dc.id AND m.deleted_at IS NULL
) dl ON true
LEFT JOIN LATERAL (
    SELECT max(m.sent_at) AS at FROM messages m JOIN chats c ON c.id = m.chat_id
    WHERE m.sender_peer_id = cand.id AND m.deleted_at IS NULL AND NOT c.excluded
) sl ON true
WHERE dc.id IS NOT NULL OR sl.at IS NOT NULL
ORDER BY cand.tier, last_interaction_at DESC NULLS LAST, cand.score DESC, cand.id
LIMIT $6
"""

_PERSON_BY_ID = f"""
SELECT p.id, p.class, p.tg_id, p.name, p.username, p.is_bot
FROM peers p
WHERE p.id = $1 AND {_peer_ok('p')}
  AND (EXISTS (SELECT 1 FROM chats c WHERE c.peer_id = p.id AND NOT c.excluded)
       OR EXISTS (SELECT 1 FROM messages m JOIN chats c ON c.id = m.chat_id
                  WHERE m.sender_peer_id = p.id AND m.deleted_at IS NULL AND NOT c.excluded))
"""

# Сообщение со всем, что нужно для ответа. r — сообщение, на которое отвечают: его идентификатор
# отдаётся, только если оно само видимо.
_MESSAGES = f"""
SELECT m.id, m.chat_id, m.tg_message_id, m.sent_at, m.kind, m.sender_peer_id,
       COALESCE(NULLIF(m.sender_name, ''), sp.name) AS sender_name, m.is_outgoing, m.text,
       m.forwarded_from, m.edited_at, m.media_type, m.service_action,
       CASE WHEN r.id IS NOT NULL AND {_peer_ok('rp', optional=True)} THEN r.id END AS reply_to_id,
       {_CHAT_NAME} AS chat_title, {_KIND} AS chat_kind, cp.username AS chat_username,
       a.label AS account_label, a.role AS account_role,
       cp.class AS peer_class, cp.tg_id AS peer_tg_id,
       sp.class AS sender_class, sp.tg_id AS sender_tg_id, sp.username AS sender_username
FROM messages m
JOIN chats c ON c.id = m.chat_id
JOIN peers cp ON cp.id = c.peer_id
JOIN accounts a ON a.id = c.account_id
LEFT JOIN peers sp ON sp.id = m.sender_peer_id
LEFT JOIN messages r ON r.chat_id = m.chat_id AND r.tg_message_id = m.reply_to_tg_id
                    AND r.deleted_at IS NULL
LEFT JOIN peers rp ON rp.id = r.sender_peer_id
WHERE m.deleted_at IS NULL AND NOT c.excluded
  AND {_peer_ok('cp')} AND {_peer_ok('sp', optional=True)}
"""

_HISTORY_FILTERS = """
  AND ($1::bigint IS NULL OR m.chat_id = $1)
  AND ($2::bigint IS NULL OR m.sender_peer_id = $2)
  AND ($3::timestamptz IS NULL OR m.sent_at >= $3)
  AND ($4::timestamptz IS NULL OR m.sent_at < $4)
  AND ($5::boolean IS NULL OR ($5 AND m.is_outgoing IS TRUE) OR (NOT $5 AND m.is_outgoing IS NOT TRUE))
"""
# Продолжение со следующей строки после (время, идентификатор) — устойчиво к новым сообщениям.
_HISTORY = {
    "desc": _MESSAGES + _HISTORY_FILTERS + """
  AND ($6::timestamptz IS NULL OR (m.sent_at, m.id) < ($6, $7::bigint))
ORDER BY m.sent_at DESC, m.id DESC LIMIT $8""",
    "asc": _MESSAGES + _HISTORY_FILTERS + """
  AND ($6::timestamptz IS NULL OR (m.sent_at, m.id) > ($6, $7::bigint))
ORDER BY m.sent_at, m.id LIMIT $8""",
}

_BEFORE = _MESSAGES + """
  AND m.chat_id = $1 AND (m.sent_at, m.id) < ($2::timestamptz, $3::bigint)
ORDER BY m.sent_at DESC, m.id DESC LIMIT $4"""
_AFTER = _MESSAGES + """
  AND m.chat_id = $1 AND (m.sent_at, m.id) > ($2::timestamptz, $3::bigint)
ORDER BY m.sent_at, m.id LIMIT $4"""

_INTERNAL = ("peer_class", "peer_tg_id", "sender_class", "sender_tg_id", "sender_username")


@dataclass
class Resolved:
    """Итог разбора ссылки на чат или человека.

    status: ok — найден ровно один (`row`); ambiguous — подходят несколько либо есть только
    похожие (`candidates`); not_found — ничего.
    """

    status: str
    row: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Window:
    """Сообщение с соседями по чату."""

    target: dict[str, Any]
    before: list[dict[str, Any]]
    after: list[dict[str, Any]]
    more_before: bool
    more_after: bool
    # сообщение, на которое отвечает найденное, если оно не попало в окно
    reply: dict[str, Any] | None


def _like(text: str) -> str:
    """Шаблон ILIKE «содержит» с экранированием служебных знаков."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _tokens(text: str) -> list[str]:
    return [_like(t) for t in text.split()[:8]]


def _chat_rows(rows: Sequence[asyncpg.Record]) -> list[dict[str, Any]]:
    """Повторная проверка служебных собеседников в коде и отбрасывание внутренних полей."""
    out = []
    for record in rows:
        row = dict(record)
        if store.is_blocked_peer(row.pop("peer_class"), row.pop("peer_tg_id"), row.get("username")):
            continue
        out.append(row)
    return out


def _person_rows(rows: Sequence[asyncpg.Record]) -> list[dict[str, Any]]:
    out = []
    for record in rows:
        row = dict(record)
        if store.is_blocked_peer(row["class"], row.pop("tg_id"), row.get("username")):
            continue
        out.append(row)
    return out


def _message_rows(rows: Sequence[asyncpg.Record]) -> list[dict[str, Any]]:
    out = []
    for record in rows:
        row = dict(record)
        if store.is_blocked_peer(row["peer_class"], row["peer_tg_id"], row["chat_username"]):
            continue
        if row["sender_class"] and store.is_blocked_peer(
                row["sender_class"], row["sender_tg_id"], row["sender_username"]):
            continue
        for key in _INTERNAL:
            del row[key]
        out.append(row)
    return out


def _as_id(ref: int | str) -> int | None:
    if isinstance(ref, bool):
        return None
    if isinstance(ref, int):
        return ref if 0 < ref < 2**63 else None
    text = ref.strip()
    if text.isascii() and text.isdigit() and len(text) <= 18:
        return int(text)
    return None


def _decide(rows: list[dict[str, Any]]) -> Resolved:
    """Одно полное совпадение — берём; иначе единственное частичное; иначе — кандидаты.
    Совпадение «только похоже» молча не выбирается никогда."""
    if not rows:
        return Resolved("not_found")
    for tier in (0, 1):
        same = [r for r in rows if r["tier"] == tier]
        if len(same) == 1:
            return Resolved("ok", row=same[0])
        if same:
            return Resolved("ambiguous", candidates=same)
    return Resolved("ambiguous", candidates=rows)


async def _similar(conn: asyncpg.Connection, sql: str, *args: Any) -> list[asyncpg.Record]:
    """Запрос с оператором похожести `<%` и своим порогом. Порог действует до конца транзакции."""
    async with conn.transaction():
        await conn.execute(
            f"SELECT set_config('pg_trgm.word_similarity_threshold', '{_SIMILARITY}', true)")
        return await conn.fetch(sql, *args)


# --- чаты ---

async def accounts(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """Аккаунты архива: название и роль (owner — владелец, assistant — помощник)."""
    return [dict(r) for r in await conn.fetch(_ACCOUNTS)]


async def list_chats(
    conn: asyncpg.Connection, *, limit: int = 50, kinds: Sequence[str] | None = None,
    exclude_kinds: Sequence[str] | None = None, query: str | None = None,
    account: str | None = None,
) -> list[dict[str, Any]]:
    """Видимые чаты, сначала с самой свежей перепиской. Строка: id, kind, name, username,
    account_label, account_role, last_message_at, message_count. Счётчик и время — только по
    видимым сообщениям. `account` — роль (owner, assistant) или название аккаунта."""
    query = (query or "").strip() or None
    rows = await _similar(
        conn, _LIST_CHATS, list(kinds) if kinds else None,
        list(exclude_kinds) if exclude_kinds else None, query, _like(query) if query else None,
        limit, (account or "").strip() or None,
    )
    return _chat_rows(rows)


async def chat_by_id(conn: asyncpg.Connection, chat_id: int) -> dict[str, Any] | None:
    """Чат по идентификатору архива; None, если его нет или он не виден."""
    rows = _chat_rows(await conn.fetch(_CHAT_BY_ID, chat_id))
    return rows[0] if rows else None


async def find_chats(conn: asyncpg.Connection, name: str, *, limit: int = 8) -> list[dict[str, Any]]:
    """Чаты, подходящие по названию или адресу. Строка — как у `list_chats` без счётчика,
    плюс tier (0 — полное совпадение, 1 — частичное, 2 — похоже) и score."""
    name = name.strip()
    if not name:
        return []
    rows = await _similar(conn, _CHAT_CANDIDATES, name, name.lstrip("@"), _tokens(name), limit)
    return _chat_rows(rows)


async def resolve_chat(conn: asyncpg.Connection, ref: int | str) -> Resolved:
    """Разбирает ссылку на чат: идентификатор архива, @адрес или название."""
    chat_id = _as_id(ref)
    if chat_id is not None:
        row = await chat_by_id(conn, chat_id)
        if row is not None:
            return Resolved("ok", row=row)
        if isinstance(ref, int):
            return Resolved("not_found")
    return _decide(await find_chats(conn, str(ref)))


# --- люди ---

async def find_people(
    conn: asyncpg.Connection, name: str, *, limit: int = 10,
    classes: Sequence[str] = ("user",),
) -> list[dict[str, Any]]:
    """Собеседники, подходящие по имени или адресу. Строка: id, class, name, username, is_bot,
    tier, score, direct_chat_id (неисключённый личный чат, если есть), last_interaction_at,
    is_self (это сам владелец аккаунта)."""
    name = name.strip()
    if not name:
        return []
    username = name.lstrip("@")
    rows = await _similar(conn, _PEOPLE, name, username, _tokens(name), list(classes),
                          _like(username), limit)
    return _person_rows(rows)


async def resolve_person(conn: asyncpg.Connection, ref: int | str) -> Resolved:
    """Разбирает ссылку на отправителя: идентификатор собеседника в архиве, @адрес или имя."""
    person_id = _as_id(ref)
    if person_id is not None:
        rows = _person_rows(await conn.fetch(_PERSON_BY_ID, person_id))
        if rows:
            return Resolved("ok", row=rows[0])
        if isinstance(ref, int):
            return Resolved("not_found")
    return _decide(await find_people(conn, str(ref), classes=("user", "chat", "channel")))


# --- сообщения ---

async def visible_messages(
    conn: asyncpg.Connection, message_ids: Sequence[int]
) -> dict[int, dict[str, Any]]:
    """Сообщения по идентификаторам архива — только видимые. Ключ — идентификатор."""
    if not message_ids:
        return {}
    rows = await conn.fetch(_MESSAGES + " AND m.id = ANY($1::bigint[])", list(message_ids))
    return {row["id"]: row for row in _message_rows(rows)}


async def get_message(conn: asyncpg.Connection, message_id: int) -> dict[str, Any] | None:
    return (await visible_messages(conn, [message_id])).get(message_id)


async def context(
    conn: asyncpg.Connection, message_id: int, *, before: int = 10, after: int = 10
) -> Window | None:
    """Сообщение с соседями по чату. None — сообщения нет или оно не видно
    (ответ одинаковый, чтобы по нему нельзя было отличить одно от другого)."""
    target = await get_message(conn, message_id)
    if target is None:
        return None
    key = (target["chat_id"], target["sent_at"], target["id"])
    earlier = _message_rows(await conn.fetch(_BEFORE, *key, before + 1)) if before >= 0 else []
    later = _message_rows(await conn.fetch(_AFTER, *key, after + 1)) if after >= 0 else []
    more_before, more_after = len(earlier) > before, len(later) > after
    earlier, later = earlier[:before][::-1], later[:after]
    reply = None
    reply_id = target["reply_to_id"]
    if reply_id is not None and all(m["id"] != reply_id for m in earlier + later):
        reply = await get_message(conn, reply_id)
    return Window(target, earlier, later, more_before, more_after, reply)


async def history(
    conn: asyncpg.Connection, *, chat_id: int | None = None, sender_peer_id: int | None = None,
    after: datetime | None = None, before: datetime | None = None, from_me: bool | None = None,
    limit: int = 50, order: str = "desc", cursor: tuple[datetime, int] | None = None,
) -> list[dict[str, Any]]:
    """Сообщения по фильтрам. `after` включительно, `before` — не включая. `cursor` — пара
    (время, идентификатор) последней строки предыдущей страницы; порядок должен быть тем же."""
    if order not in _HISTORY:
        raise ValueError("order: нужно asc или desc")
    cursor_at, cursor_id = cursor if cursor else (None, None)
    rows = await conn.fetch(_HISTORY[order], chat_id, sender_peer_id, after, before, from_me,
                            cursor_at, cursor_id, limit)
    return _message_rows(rows)
