"""Запись в архив — один путь для экспорта, бизнес-бота и сессии.

Правила, которые здесь зашиты (и проверены тестами):
  * сообщение определяется парой (чат, идентификатор сообщения) — повторный приход из любого
    источника дополняет запись, а не создаёт новую;
  * более свежая правка заменяет текст, прежний уходит в историю версий; более старая версия,
    пришедшая позже, текст не перезаписывает;
  * исключённый чат не принимает сообщения ни из одного источника;
  * служебные собеседники Telegram (коды входа, токены ботов) исключены всегда;
  * новый текст (новое сообщение или правка) — это непроверенный текст: итог проверки на
    внедрённые инструкции сбрасывается, а при `hold` входящее сообщение ещё и скрыто от
    ассистента, пока его не проверят (см. guard/).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Sequence

import asyncpg

from .records import ChatRecord, MessageRecord
from . import control_peers

# Чаты, в которых лежат коды входа и токены: 777000 — служебные уведомления Telegram,
# 93372553 — @BotFather, 178220800 — @SpamBot. В архив не принимаются и агенту не отдаются.
BLOCKED_USER_IDS = frozenset({777000, 93372553, 178220800})
BLOCKED_USERNAMES = frozenset({"botfather", "spambot", "telegram"})
# Диалог «Коды подтверждения» (коды входа в сторонние сервисы) исключается по типу чата.
BLOCKED_CHAT_TYPES = frozenset({"verification_codes"})


def _clean_deep(value):
    if isinstance(value, str):
        return _clean(value)
    if isinstance(value, list):
        return [_clean_deep(v) for v in value]
    if isinstance(value, dict):
        return {k: _clean_deep(v) for k, v in value.items()}
    return value


def _clean(value: str | None) -> str | None:
    """Postgres не хранит нулевой байт в тексте; в сообщениях он изредка встречается."""
    return value.replace("\x00", "") if value and "\x00" in value else value

_STAGE = """
CREATE TEMP TABLE IF NOT EXISTS import_stage (
    chat_id bigint, tg_message_id bigint, sent_at timestamptz, kind text,
    sender_class text, sender_tg_id bigint, sender_name text, is_outgoing boolean,
    text text, entities jsonb, reply_to_tg_id bigint, forwarded_from text,
    edited_at timestamptz, media_type text, media_path text, service_action text,
    telegram_entities jsonb, topic_tg_id bigint, is_forwarded boolean,
    telegram_via_bot boolean, telegram_sender_bot boolean
) ON COMMIT DELETE ROWS
"""

_UPSERT_SENDERS = """
INSERT INTO peers (class, tg_id, name)
SELECT DISTINCT ON (sender_class, sender_tg_id) sender_class, sender_tg_id, sender_name
FROM import_stage
WHERE sender_tg_id IS NOT NULL
ORDER BY sender_class, sender_tg_id, sent_at DESC
ON CONFLICT (class, tg_id) DO UPDATE
SET name = COALESCE(peers.name, EXCLUDED.name), updated_at = now()
"""

# «Новее» — та версия, у которой позже отметка правки; отсутствие отметки считается самой старой.
_NEWER = "COALESCE(s.edited_at, '-infinity') > COALESCE(m.edited_at, '-infinity')"

# Пришёл более новый текст: прежний уходит в историю версий.
_KEEP_OLD_VERSION = f"""
INSERT INTO message_versions (message_id, text, edited_at)
SELECT m.id, m.text, m.edited_at
FROM messages m JOIN import_stage s USING (chat_id, tg_message_id)
WHERE m.text IS DISTINCT FROM s.text AND {_NEWER}
"""

# Пришёл более старый текст: в историю уходит он сам, если такого там ещё нет.
_KEEP_INCOMING_AS_VERSION = f"""
INSERT INTO message_versions (message_id, text, edited_at)
SELECT m.id, s.text, s.edited_at
FROM messages m JOIN import_stage s USING (chat_id, tg_message_id)
WHERE m.text IS DISTINCT FROM s.text AND NOT ({_NEWER})
  AND NOT EXISTS (
      SELECT 1 FROM message_versions v WHERE v.message_id = m.id AND v.text = s.text
  )
"""

# «Пришёл другой текст, и он новее» — то же условие, по которому ниже заменяется текст.
_NEW_TEXT = """(COALESCE(EXCLUDED.edited_at, '-infinity') > COALESCE(m.edited_at, '-infinity')
               AND EXCLUDED.text IS DISTINCT FROM m.text)"""

# Сообщение, помеченное удалённым, остаётся удалённым: повторный приход из старого экспорта
# его не «воскрешает» — отметку снимает только явное восстановление.
#
# Защита от внедрённых инструкций ($2 — «придержать»: живой источник при включённой защите):
#   * новое входящее текстовое сообщение при $2 записывается скрытым от ассистента — видимым его
#     сделает проверка (guard.screen) сразу после записи;
#   * правка с новым текстом сбрасывает итог прежней проверки; уже скрытое остаётся скрытым,
#     видимое входящее при $2 скрывается до новой проверки. Столбцы guard_* упомянуты раньше text:
#     в SET все выражения считаются от прежней строки, порядок здесь только для читателя.
_UPSERT_MESSAGES = f"""
INSERT INTO messages AS m (
    chat_id, tg_message_id, sent_at, kind, sender_peer_id, sender_name, is_outgoing,
    text, entities, reply_to_tg_id, forwarded_from, edited_at,
    media_type, media_path, service_action, sources, agent_visible,
    telegram_entities, topic_tg_id, is_forwarded, telegram_via_bot, telegram_sender_bot
)
SELECT s.chat_id, s.tg_message_id, s.sent_at, s.kind, p.id, s.sender_name, s.is_outgoing,
       s.text, s.entities, s.reply_to_tg_id, s.forwarded_from, s.edited_at,
       s.media_type, s.media_path, s.service_action, ARRAY[$1::text],
       NOT ($2::boolean AND s.is_outgoing IS NOT TRUE AND s.kind = 'message' AND s.text <> ''),
       s.telegram_entities, s.topic_tg_id, s.is_forwarded, s.telegram_via_bot, s.telegram_sender_bot
FROM import_stage s
LEFT JOIN peers p ON p.class = s.sender_class AND p.tg_id = s.sender_tg_id
ON CONFLICT (chat_id, tg_message_id) DO UPDATE SET
    telegram_entities = CASE WHEN {_NEW_TEXT} THEN EXCLUDED.telegram_entities
        WHEN $1 = 'session' AND EXCLUDED.text = m.text
             AND COALESCE(EXCLUDED.edited_at, '-infinity') >= COALESCE(m.edited_at, '-infinity')
        THEN EXCLUDED.telegram_entities ELSE m.telegram_entities END,
    topic_tg_id = CASE WHEN {_NEW_TEXT} THEN EXCLUDED.topic_tg_id
        WHEN $1 = 'session' AND EXCLUDED.text = m.text
             AND COALESCE(EXCLUDED.edited_at, '-infinity') >= COALESCE(m.edited_at, '-infinity')
        THEN EXCLUDED.topic_tg_id ELSE m.topic_tg_id END,
    is_forwarded = CASE WHEN {_NEW_TEXT} THEN EXCLUDED.is_forwarded
        WHEN $1 = 'session' AND EXCLUDED.text = m.text
             AND COALESCE(EXCLUDED.edited_at, '-infinity') >= COALESCE(m.edited_at, '-infinity')
        THEN EXCLUDED.is_forwarded ELSE m.is_forwarded END,
    telegram_via_bot = CASE WHEN {_NEW_TEXT} THEN EXCLUDED.telegram_via_bot
        WHEN $1 = 'session' AND EXCLUDED.text = m.text
             AND COALESCE(EXCLUDED.edited_at, '-infinity') >= COALESCE(m.edited_at, '-infinity')
        THEN EXCLUDED.telegram_via_bot ELSE m.telegram_via_bot END,
    telegram_sender_bot = CASE WHEN {_NEW_TEXT} THEN EXCLUDED.telegram_sender_bot
        WHEN $1 = 'session' AND EXCLUDED.text = m.text
             AND COALESCE(EXCLUDED.edited_at, '-infinity') >= COALESCE(m.edited_at, '-infinity')
        THEN EXCLUDED.telegram_sender_bot ELSE m.telegram_sender_bot END,
    agent_visible    = CASE WHEN {_NEW_TEXT} AND $2::boolean AND COALESCE(m.is_outgoing, EXCLUDED.is_outgoing)
                                 IS NOT TRUE AND m.kind = 'message' AND EXCLUDED.text <> ''
                            THEN false ELSE m.agent_visible END,
    guard_label      = CASE WHEN {_NEW_TEXT} THEN NULL ELSE m.guard_label END,
    guard_score      = CASE WHEN {_NEW_TEXT} THEN NULL ELSE m.guard_score END,
    guard_model      = CASE WHEN {_NEW_TEXT} THEN NULL ELSE m.guard_model END,
    guard_checked_at = CASE WHEN {_NEW_TEXT} THEN NULL ELSE m.guard_checked_at END,
    text      = CASE WHEN COALESCE(EXCLUDED.edited_at, '-infinity') > COALESCE(m.edited_at, '-infinity')
                     THEN EXCLUDED.text ELSE m.text END,
    entities  = CASE WHEN COALESCE(EXCLUDED.edited_at, '-infinity') > COALESCE(m.edited_at, '-infinity')
                     THEN EXCLUDED.entities ELSE m.entities END,
    edited_at = GREATEST(m.edited_at, EXCLUDED.edited_at),
    sender_peer_id = COALESCE(m.sender_peer_id, EXCLUDED.sender_peer_id),
    sender_name    = COALESCE(m.sender_name, EXCLUDED.sender_name),
    is_outgoing    = COALESCE(m.is_outgoing, EXCLUDED.is_outgoing),
    reply_to_tg_id = COALESCE(m.reply_to_tg_id, EXCLUDED.reply_to_tg_id),
    forwarded_from = COALESCE(m.forwarded_from, EXCLUDED.forwarded_from),
    media_type     = COALESCE(m.media_type, EXCLUDED.media_type),
    media_path     = COALESCE(m.media_path, EXCLUDED.media_path),
    sources = CASE WHEN $1 = ANY (m.sources) THEN m.sources ELSE m.sources || $1::text END
RETURNING m.id, (xmax = 0) AS inserted
"""


@dataclass
class UpsertResult:
    new: int = 0
    known: int = 0
    versions: int = 0
    # идентификаторы строк архива: новые и уже известные — для индексации и обработки
    new_ids: tuple[int, ...] = ()
    known_ids: tuple[int, ...] = ()


def is_blocked_peer(peer_class: str, tg_id: int, username: str | None = None) -> bool:
    if peer_class != "user":
        return False
    return tg_id in BLOCKED_USER_IDS


async def ensure_account(
    conn: asyncpg.Connection, tg_user_id: int, label: str, role: str = "owner"
) -> int:
    return await conn.fetchval(
        """INSERT INTO accounts (tg_user_id, label, role) VALUES ($1, $2, $3)
           ON CONFLICT (tg_user_id) DO UPDATE SET label = accounts.label
           RETURNING id""",
        tg_user_id, label, role,
    )


async def ensure_peer(
    conn: asyncpg.Connection, peer_class: str, tg_id: int, *,
    name: str | None = None, username: str | None = None, is_bot: bool | None = None,
    refresh: bool = False,
) -> int:
    """Находит или создаёт собеседника. refresh=True — источник знает актуальные имя и адрес
    (живая сессия, бизнес-бот) и перезаписывает прежние; иначе заполняются только пустые поля."""
    keep = "COALESCE(EXCLUDED.{0}, peers.{0})" if refresh else "COALESCE(peers.{0}, EXCLUDED.{0})"
    return await conn.fetchval(
        f"""INSERT INTO peers (class, tg_id, name, username, is_bot) VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (class, tg_id) DO UPDATE
            SET name = {keep.format('name')}, username = {keep.format('username')},
                is_bot = COALESCE(peers.is_bot, EXCLUDED.is_bot), updated_at = now()
            RETURNING id""",
        peer_class, tg_id, _clean(name), (_clean(username) or None) and _clean(username).lstrip("@"), is_bot,
    )


async def ensure_chat(
    conn: asyncpg.Connection, account_id: int, chat: ChatRecord, *,
    exclude: bool = False, refresh: bool = False,
) -> tuple[int, bool]:
    """Находит или создаёт чат аккаунта. Возвращает (идентификатор чата, исключён ли).

    Запрет не снимается: однажды исключённый чат остаётся исключённым для всех источников.
    """
    is_bot = chat.is_bot if chat.is_bot is not None else (True if chat.type == "bot_chat" else None)
    peer_id = await ensure_peer(
        conn, chat.peer_class, chat.tg_id, name=chat.name, username=chat.username,
        is_bot=is_bot, refresh=refresh,
    )
    exclude = (exclude or chat.type in BLOCKED_CHAT_TYPES
               or is_blocked_peer(chat.peer_class, chat.tg_id, chat.username)
               or await control_peers.is_blocked(conn, chat.peer_class, chat.tg_id))
    title = "COALESCE(EXCLUDED.title, chats.title)" if refresh else "COALESCE(chats.title, EXCLUDED.title)"
    row = await conn.fetchrow(
        f"""INSERT INTO chats (account_id, peer_id, type, title, excluded)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (account_id, peer_id) DO UPDATE
            SET title = {title}, excluded = chats.excluded OR EXCLUDED.excluded
            RETURNING id, excluded""",
        account_id, peer_id, chat.type, _clean(chat.name), exclude,
    )
    return row["id"], row["excluded"]


def _row(chat_id: int, m: MessageRecord, owner_tg_id: int, outgoing: bool | None, source: str = "") -> tuple:
    if outgoing is None and m.sender_tg_id is not None:
        outgoing = m.sender_class == "user" and m.sender_tg_id == owner_tg_id
    return (
        chat_id, m.tg_message_id, m.sent_at, m.kind,
        m.sender_class, m.sender_tg_id, _clean(m.sender_name), outgoing,
        _clean(m.text) or "",
        json.dumps(_clean_deep(m.entities), ensure_ascii=False) if m.entities else None,
        m.reply_to_tg_id, _clean(m.forwarded_from), m.edited_at,
        m.media_type, m.media_path, m.service_action,
        json.dumps(_clean_deep(m.telegram_entities), ensure_ascii=False)
        if source == "session" and m.telegram_entities is not None else None,
        m.topic_tg_id if source == "session" else None,
        m.is_forwarded if source == "session" else None,
        m.telegram_via_bot if source == "session" else None,
        m.telegram_sender_bot if source == "session" else None,
    )


async def upsert_messages(
    conn: asyncpg.Connection,
    rows: Iterable[tuple[int, MessageRecord] | tuple[int, MessageRecord, bool | None]],
    *, source: str, owner_tg_id: int, hold: bool = False,
) -> UpsertResult:
    """Записывает пачку сообщений. Элемент — (чат, запись) или (чат, запись, исходящее ли).

    Третий элемент нужен источникам, которые знают направление точнее, чем сравнение
    отправителя с владельцем (флаг `out` у сессии). Сообщения исключённых чатов отбрасываются.
    Повтор внутри пачки: остаётся последний.

    hold — придержать новые входящие тексты: записать скрытыми от ассистента до проверки на
    внедрённые инструкции. Так пишут живые источники при включённой защите; вызывающий обязан
    сразу после записи отдать сообщения на проверку (`guard.screen`). Импорт и догрузка истории
    пишут без hold: их сообщения видны сразу и проверяются фоном.
    """
    staged: dict[tuple[int, int], tuple] = {}
    for item in rows:
        chat_id, record = item[0], item[1]
        outgoing = item[2] if len(item) > 2 else None
        staged[(chat_id, record.tg_message_id)] = _row(chat_id, record, owner_tg_id, outgoing, source)
    if not staged:
        return UpsertResult()
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock_shared(hashtext($1))", control_peers.LOCK)
        banned = {
            r["id"] for r in await conn.fetch(
                # Строки чатов блокируются до конца транзакции: исключение чата с очисткой
                # (оно берёт строку FOR UPDATE) не может вклиниться между проверкой и записью.
                "SELECT id, excluded FROM chats WHERE id = ANY($1::bigint[]) ORDER BY id FOR KEY SHARE",
                sorted({k[0] for k in staged}),
            ) if r["excluded"]
        }
        protected = await control_peers.blocked_ids(conn)
        records = [v for k, v in staged.items() if k[0] not in banned
                   and not (v[4] == "user" and v[5] in protected)]
        if not records:
            return UpsertResult()
        await conn.execute(_STAGE)
        await conn.copy_records_to_table("import_stage", records=records)
        await conn.execute(_UPSERT_SENDERS)
        v1 = await conn.execute(_KEEP_OLD_VERSION)
        v2 = await conn.execute(_KEEP_INCOMING_AS_VERSION)
        result = await conn.fetch(_UPSERT_MESSAGES, source, bool(hold))
    new_ids = tuple(r["id"] for r in result if r["inserted"])
    known_ids = tuple(r["id"] for r in result if not r["inserted"])
    return UpsertResult(
        new=len(new_ids), known=len(known_ids),
        versions=int(v1.split()[-1]) + int(v2.split()[-1]),
        new_ids=new_ids, known_ids=known_ids,
    )


async def mark_deleted(conn: asyncpg.Connection, chat_id: int, tg_message_ids: Sequence[int]) -> list[int]:
    """Помечает сообщения чата удалёнными. Возвращает идентификаторы затронутых строк архива."""
    if not tg_message_ids:
        return []
    rows = await conn.fetch(
        """UPDATE messages SET deleted_at = now()
           WHERE chat_id = $1 AND tg_message_id = ANY($2::bigint[]) AND deleted_at IS NULL
           RETURNING id""",
        chat_id, list(tg_message_ids),
    )
    return [r["id"] for r in rows]


async def mark_deleted_without_chat(
    conn: asyncpg.Connection, account_id: int, tg_message_ids: Sequence[int]
) -> list[int]:
    """Удаление, пришедшее без указания чата (личные чаты и обычные группы).

    У аккаунта идентификаторы сообщений в таких чатах общие, поэтому сообщение ищется среди
    чатов классов user и chat и помечается только при единственном совпадении.
    """
    if not tg_message_ids:
        return []
    rows = await conn.fetch(
        """WITH hit AS (
               SELECT m.tg_message_id, min(m.id) AS id, count(*) AS n
               FROM messages m
               JOIN chats c ON c.id = m.chat_id
               JOIN peers p ON p.id = c.peer_id
               WHERE c.account_id = $1 AND p.class IN ('user', 'chat')
                 AND m.tg_message_id = ANY($2::bigint[]) AND m.deleted_at IS NULL
               GROUP BY m.tg_message_id
           )
           UPDATE messages m SET deleted_at = now()
           FROM hit WHERE hit.n = 1 AND m.id = hit.id
           RETURNING m.id""",
        account_id, list(tg_message_ids),
    )
    return [r["id"] for r in rows]

