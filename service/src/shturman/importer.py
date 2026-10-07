"""Запись экспорта Telegram Desktop в архив.

Повторный импорт того же или более свежего экспорта безопасен: сообщения
сопоставляются по (чат, идентификатор сообщения) и не дублируются.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import BinaryIO, Iterable

import asyncpg

from . import store
from .records import ChatRecord
from .store import ensure_account
from .telegram_export import ExportOwner, iter_export

BATCH = 5000


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


async def _flush(
    conn: asyncpg.Connection, rows: dict[tuple[int, int], tuple], owner_tg_id: int, stats: ImportStats
) -> None:
    if not rows:
        return
    result = await store.upsert_messages(conn, rows.values(), source="import", owner_tg_id=owner_tg_id)
    stats.messages_new += result.new
    stats.messages_known += result.known
    stats.versions_added += result.versions
    rows.clear()


async def import_export(
    conn: asyncpg.Connection,
    fp: BinaryIO,
    *,
    owner_tg_user_id: int | None = None,
    owner_label: str | None = None,
    exclude: Iterable[tuple[str, int]] = (),
    source_name: str = "result.json",
    stats: ImportStats | None = None,
) -> ImportStats:
    """Импортирует экспорт в архив.

    owner_tg_user_id обязателен для экспорта одного чата: в нём нет сведений о владельце.
    exclude — чаты (класс, идентификатор), которые не должны попасть в архив; запрет
    запоминается и действует на все будущие источники.
    """
    excluded = set(exclude)
    # Счётчики можно передать снаружи, чтобы показывать ход импорта, пока он идёт.
    stats = stats if stats is not None else ImportStats()
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
            await _flush(conn, rows, owner_id, stats)
            current = (a.peer_class, a.tg_id)
            chat_id, skip = await store.ensure_chat(
                conn, account_id, ChatRecord(a.peer_class, a.tg_id, a.type, a.name),
                exclude=current in excluded,
            )
            if skip:
                stats.chats_excluded += 1
                stats.excluded_names.append(a.name or f"{a.type} {a.tg_id}")
            else:
                stats.chats += 1
            continue

        stats.messages_read += 1
        if skip:
            continue
        rows[(chat_id, b.tg_message_id)] = (chat_id, b)
        if len(rows) >= BATCH:
            await _flush(conn, rows, owner_id, stats)

    await _flush(conn, rows, owner_id, stats)
    if import_id is not None:
        await conn.execute(
            "UPDATE imports SET finished_at = now(), stats = $2::jsonb WHERE id = $1",
            import_id, json.dumps(stats.as_dict(), ensure_ascii=False),
        )
    return stats
