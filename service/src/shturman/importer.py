"""Запись экспорта Telegram Desktop в архив.

Повторный импорт того же или более свежего экспорта безопасен: сообщения
сопоставляются по (чат, идентификатор сообщения) и не дублируются.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import BinaryIO, Iterable

import asyncpg

from .telegram_export import ExportChat, ExportMessage, ExportOwner, iter_export

BATCH = 5000

_STAGE = """
CREATE TEMP TABLE IF NOT EXISTS import_stage (
    chat_id bigint, tg_message_id bigint, sent_at timestamptz, kind text,
    sender_class text, sender_tg_id bigint, sender_name text, is_outgoing boolean,
    text text, entities jsonb, reply_to_tg_id bigint, forwarded_from text,
    edited_at timestamptz, media_type text, media_path text, service_action text
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

_UPSERT_MESSAGES = """
INSERT INTO messages AS m (
    chat_id, tg_message_id, sent_at, kind, sender_peer_id, sender_name, is_outgoing,
    text, entities, reply_to_tg_id, forwarded_from, edited_at,
    media_type, media_path, service_action, sources
)
SELECT s.chat_id, s.tg_message_id, s.sent_at, s.kind, p.id, s.sender_name, s.is_outgoing,
       s.text, s.entities, s.reply_to_tg_id, s.forwarded_from, s.edited_at,
       s.media_type, s.media_path, s.service_action, ARRAY[$1::text]
FROM import_stage s
LEFT JOIN peers p ON p.class = s.sender_class AND p.tg_id = s.sender_tg_id
ON CONFLICT (chat_id, tg_message_id) DO UPDATE SET
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
RETURNING (xmax = 0) AS inserted
"""


@dataclass
class ChatSummary:
    """Чат в экспорте — для экрана выбора исключений до импорта."""

    peer_class: str
    tg_id: int
    type: str
    name: str | None
    messages: int = 0
    first_at: str | None = None
    last_at: str | None = None


@dataclass
class ImportStats:
    chats: int = 0
    chats_excluded: int = 0
    messages_read: int = 0
    messages_new: int = 0
    messages_known: int = 0
    versions_added: int = 0
    owner_tg_user_id: int | None = None
    excluded_names: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def scan(fp: BinaryIO) -> tuple[ExportOwner | None, list[ChatSummary]]:
    """Перечисляет чаты экспорта с объёмом и периодом. В базу ничего не пишет."""
    owner: ExportOwner | None = None
    chats: dict[tuple[str, int], ChatSummary] = {}
    for kind, a, b in iter_export(fp):
        if kind == "owner":
            owner = a
        elif kind == "chat":
            chats.setdefault(
                (a.peer_class, a.tg_id), ChatSummary(a.peer_class, a.tg_id, a.type, a.name)
            )
        else:
            s = chats[(a.peer_class, a.tg_id)]
            s.messages += 1
            day = b.sent_at.date().isoformat()
            s.first_at = day if s.first_at is None or day < s.first_at else s.first_at
            s.last_at = day if s.last_at is None or day > s.last_at else s.last_at
    return owner, sorted(chats.values(), key=lambda c: -c.messages)


async def ensure_account(
    conn: asyncpg.Connection, tg_user_id: int, label: str, role: str = "owner"
) -> int:
    return await conn.fetchval(
        """INSERT INTO accounts (tg_user_id, label, role) VALUES ($1, $2, $3)
           ON CONFLICT (tg_user_id) DO UPDATE SET label = accounts.label
           RETURNING id""",
        tg_user_id, label, role,
    )


async def _ensure_chat(
    conn: asyncpg.Connection, account_id: int, chat: ExportChat, exclude: bool
) -> tuple[int, bool]:
    peer_id = await conn.fetchval(
        """INSERT INTO peers (class, tg_id, name, is_bot) VALUES ($1, $2, $3, $4)
           ON CONFLICT (class, tg_id) DO UPDATE
           SET name = COALESCE(peers.name, EXCLUDED.name),
               is_bot = COALESCE(peers.is_bot, EXCLUDED.is_bot)
           RETURNING id""",
        chat.peer_class, chat.tg_id, chat.name, True if chat.type == "bot_chat" else None,
    )
    row = await conn.fetchrow(
        """INSERT INTO chats (account_id, peer_id, type, title, excluded)
           VALUES ($1, $2, $3, $4, $5)
           ON CONFLICT (account_id, peer_id) DO UPDATE
           SET title = COALESCE(chats.title, EXCLUDED.title),
               excluded = chats.excluded OR EXCLUDED.excluded
           RETURNING id, excluded""",
        account_id, peer_id, chat.type, chat.name, exclude,
    )
    return row["id"], row["excluded"]


def _row(chat_id: int, m: ExportMessage, owner_tg_id: int) -> tuple:
    outgoing = None
    if m.sender_tg_id is not None:
        outgoing = m.sender_class == "user" and m.sender_tg_id == owner_tg_id
    return (
        chat_id, m.tg_message_id, m.sent_at, m.kind,
        m.sender_class, m.sender_tg_id, m.sender_name, outgoing,
        m.text, json.dumps(m.entities, ensure_ascii=False) if m.entities else None,
        m.reply_to_tg_id, m.forwarded_from, m.edited_at,
        m.media_type, m.media_path, m.service_action,
    )


async def _flush(conn: asyncpg.Connection, rows: dict[tuple[int, int], tuple], source: str, stats: ImportStats) -> None:
    if not rows:
        return
    async with conn.transaction():
        await conn.execute(_STAGE)
        await conn.copy_records_to_table("import_stage", records=list(rows.values()))
        await conn.execute(_UPSERT_SENDERS)
        v1 = await conn.execute(_KEEP_OLD_VERSION)
        v2 = await conn.execute(_KEEP_INCOMING_AS_VERSION)
        result = await conn.fetch(_UPSERT_MESSAGES, source)
    new = sum(1 for r in result if r["inserted"])
    stats.messages_new += new
    stats.messages_known += len(result) - new
    stats.versions_added += int(v1.split()[-1]) + int(v2.split()[-1])
    rows.clear()


async def import_export(
    conn: asyncpg.Connection,
    fp: BinaryIO,
    *,
    owner_tg_user_id: int | None = None,
    owner_label: str | None = None,
    exclude: Iterable[tuple[str, int]] = (),
    source_name: str = "result.json",
) -> ImportStats:
    """Импортирует экспорт в архив.

    owner_tg_user_id обязателен для экспорта одного чата: в нём нет сведений о владельце.
    exclude — чаты (класс, идентификатор), которые не должны попасть в архив; запрет
    запоминается и действует на все будущие источники.
    """
    excluded = set(exclude)
    stats = ImportStats()
    account_id: int | None = None
    owner_id = owner_tg_user_id
    import_id: int | None = None
    current: tuple[str, int] | None = None
    chat_id: int | None = None
    skip = False
    rows: dict[tuple[int, int], tuple] = {}

    async def start_account(label: str | None) -> None:
        nonlocal account_id, import_id
        if owner_id is None:
            raise ValueError(
                "в файле нет сведений о владельце (экспорт одного чата) — "
                "укажите идентификатор владельца явно"
            )
        account_id = await ensure_account(conn, owner_id, label or owner_label or f"Аккаунт {owner_id}")
        import_id = await conn.fetchval(
            "INSERT INTO imports (account_id, source_name) VALUES ($1, $2) RETURNING id",
            account_id, source_name,
        )
        stats.owner_tg_user_id = owner_id

    for kind, a, b in iter_export(fp):
        if kind == "owner":
            if owner_id is not None and owner_id != a.tg_user_id:
                raise ValueError(
                    f"владелец экспорта ({a.tg_user_id}) не совпадает с указанным ({owner_id})"
                )
            owner_id = a.tg_user_id
            await start_account(a.name)
            continue

        if account_id is None:
            await start_account(None)

        if kind == "chat":
            await _flush(conn, rows, "import", stats)
            current = (a.peer_class, a.tg_id)
            chat_id, skip = await _ensure_chat(conn, account_id, a, current in excluded)
            if skip:
                stats.chats_excluded += 1
                stats.excluded_names.append(a.name or f"{a.type} {a.tg_id}")
            else:
                stats.chats += 1
            continue

        stats.messages_read += 1
        if skip:
            continue
        rows[(chat_id, b.tg_message_id)] = _row(chat_id, b, owner_id)
        if len(rows) >= BATCH:
            await _flush(conn, rows, "import", stats)

    await _flush(conn, rows, "import", stats)
    if import_id is not None:
        await conn.execute(
            "UPDATE imports SET finished_at = now(), stats = $2::jsonb WHERE id = $1",
            import_id, json.dumps(stats.as_dict(), ensure_ascii=False),
        )
    return stats
