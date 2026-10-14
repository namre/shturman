"""Список диалогов аккаунта: загрузка из Telegram страницами и хранение в базе (решение Р-81).

Раньше список читал `iter_dialogs` Telethon, пока ждал запрос страницы настройки: у аккаунта с
тысячами чатов это десятки запросов `messages.GetDialogs` с паузой 1,5–2,7 с между ними — минута
и больше, а прокси перед страницей (Cloudflare) рвёт запрос через 100 с. Если Telegram на
середине просил подождать (FLOOD_WAIT), всё загруженное выбрасывалось, и следующая попытка
начинала с нуля.

Теперь:
  * загрузка идёт в фоне (`TgManager.start_dialogs`), запрос страницы её только запускает и сразу
    отвечает; состояние — в `DialogsLoad` («загружается N из ~M», «Telegram просит подождать»);
  * каждая страница сразу пишется в таблицу `tg_dialogs`; экран читает список отсюда, и после
    перезапуска сервиса он есть без запроса к Telegram;
  * при FLOOD_WAIT загрузка ждёт названное время (прерывается остановкой аккаунта) и повторяет ту
    же страницу — с теми же смещениями, ничего не теряя и не перечитывая уже полученное;
  * диалоги, которых в полной загрузке не оказалось, убираются из списка (`finish`); архив
    сообщений это не трогает.

Свой цикл по `messages.GetDialogs` повторяет `_DialogsIter` Telethon 1.45.0 (client/dialogs.py):
смещения по последнему диалогу страницы, у которого есть сообщение; закреплённые — только на первой
странице; конец — неполная страница или ответ не «срез». И то, что делал `iter_dialogs` попутно:
ключи доступа собеседников в память Telethon (`_mb_entity_cache.extend`; в файл сессии их пишет
сам `_call` Telethon) и состояние каналов для потока обновлений
(`_message_box.try_set_channel_state`, см. Р-79). На ключи доступа опираются загрузка истории и
отправка (`get_input_entity`).

Паузы. Между страницами — прежние 1,5–2,7 с (`Spacer` в `GuardedClient`). Загрузка идёт через
общий `Pacer` аккаунта: первая страница ждёт своей очереди, как запрос истории; каждая страница
отодвигает следующий запрос истории (`Pacer.note_request`), так что список и история не бьют
Telegram вместе; FLOOD_WAIT, полученный одним, — ожидание и для другого.

# Основано на LonamiWebs/Telethon (MIT), telethon/client/dialogs.py@v1.45.0 (`_DialogsIter`:
#   смещения, признак конца, пропуск уже виденных и диалогов без сущности)
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Iterable

import asyncpg
from telethon import utils
from telethon.tl import functions, types

from ..records import ChatRecord
from . import normalize, sync
from .normalize import PeerKey

logger = logging.getLogger("shturman.tg")

PAGE = 100                    # предел Telegram на страницу списка диалогов
TRANSIENT_RETRIES = 3         # столько раз подряд повторяется страница при сбое связи
TRANSIENT_WAIT = 10.0         # пауза перед таким повтором, с

# Состояния загрузки для экрана.
IDLE, LOADING, WAITING, DONE, FAILED = "idle", "loading", "waiting", "done", "failed"


@dataclass
class DialogRow:
    """Диалог, каким его вернул Telegram."""

    chat: ChatRecord
    top_message: int
    last_message_at: datetime | None = None
    folder_id: int = 0
    pinned: bool = False

    @property
    def key(self) -> PeerKey:
        return self.chat.peer_class, self.chat.tg_id


@dataclass
class DialogsLoad:
    """Загрузка списка одного аккаунта — то, что видит экран. Живёт в памяти сервиса; когда
    список последний раз загружен целиком, хранится в базе (`tg_dialog_lists.completed_at`)."""

    state: str = IDLE
    loaded: int = 0                      # сколько диалогов получено в этой загрузке
    expected: int | None = None          # сколько назвал Telegram (count первой страницы)
    pages: int = 0                       # сколько страниц получено (запросов, на которые пришёл список)
    waits: int = 0                       # сколько раз Telegram просил подождать
    started: float = 0.0                 # когда начата (time.monotonic); 0 — не начиналась
    error: str | None = None
    mark_seen: bool = False              # по окончании — отметить все диалоги как уже существующие
    task: asyncio.Task | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()


# --- разбор ответа -------------------------------------------------------------------------------

def _message_key(peer: Any, message_id: int) -> tuple[int | None, int]:
    """Как `_dialog_message_key` Telethon: номера сообщений своих у каждого канала, общие у остальных."""
    return (peer.channel_id if isinstance(peer, types.PeerChannel) else None), message_id


@dataclass
class _Cursor:
    """Смещения следующей страницы и всё, что нужно, чтобы продолжить с того же места."""

    request: Any
    seen: set[int] = field(default_factory=set)


def first_request() -> Any:
    return functions.messages.GetDialogsRequest(
        offset_date=None, offset_id=0, offset_peer=types.InputPeerEmpty(), limit=PAGE, hash=0,
        exclude_pinned=False, folder_id=None)


def parse_page(client: Any, cursor: _Cursor, response: Any, *, self_id: int) -> tuple[list[DialogRow], bool]:
    """Диалоги страницы и признак конца. Двигает смещения `cursor` к следующей странице и, как
    `iter_dialogs`, передаёт Telethon ключи доступа и состояние каналов."""
    request = cursor.request
    users, chats = list(getattr(response, "users", None) or ()), list(getattr(response, "chats", None) or ())
    entities = {utils.get_peer_id(x): x for x in itertools.chain(users, chats)
                if not isinstance(x, (types.UserEmpty, types.ChatEmpty))}
    cache = getattr(client, "_mb_entity_cache", None)
    if cache is not None:
        cache.extend(users, chats)
    box = getattr(client, "_message_box", None)
    messages = {_message_key(m.peer_id, m.id): m for m in getattr(response, "messages", None) or ()
                if getattr(m, "peer_id", None) is not None}
    dialogs = list(getattr(response, "dialogs", None) or ())
    rows: list[DialogRow] = []
    added = 0
    last_entity: Any = None
    for d in dialogs:
        message = messages.get(_message_key(d.peer, d.top_message))
        if request.offset_date:
            date = getattr(message, "date", None)
            if not date or date.timestamp() > request.offset_date.timestamp():
                continue      # Telegram мог не учесть смещение по дате
        peer_id = utils.get_peer_id(d.peer)
        if peer_id in cursor.seen:
            continue
        cursor.seen.add(peer_id)
        entity = entities.get(peer_id)
        if entity is None:
            continue          # так бывает изредка; Telethon такие диалоги тоже пропускает
        added += 1
        last_entity = entity
        if isinstance(d, types.DialogFolder):
            continue          # папка (архив) в списке, а не чат
        if getattr(d, "pts", None) and box is not None:
            box.try_set_channel_state(utils.get_peer_id(d.peer, add_mark=False), d.pts)
        chat = normalize.chat_record(entity, self_id=self_id)
        if chat is None:
            continue
        rows.append(DialogRow(chat=chat, top_message=int(d.top_message or 0),
                              last_message_at=getattr(message, "date", None),
                              folder_id=int(getattr(d, "folder_id", None) or 0), pinned=bool(getattr(d, "pinned", False))))
    done = not added or len(dialogs) < request.limit or not isinstance(response, types.messages.DialogsSlice)
    if not done:
        # Не `messages[-1]`: закреплённые диалоги ломают порядок. Берётся последний диалог страницы,
        # у которого есть сообщение.
        last_message = next(filter(None, (messages.get(_message_key(d.peer, d.top_message))
                                          for d in reversed(dialogs))), None)
        request.exclude_pinned = True
        request.offset_id = last_message.id if last_message else 0
        request.offset_date = last_message.date if last_message else None
        request.offset_peer = utils.get_input_peer(last_entity)
    return rows, done


# --- загрузка ------------------------------------------------------------------------------------

PageSink = Callable[[list[DialogRow], int | None, bool], Awaitable[None]]


async def fetch_all(
    client: Any, *, self_id: int, pacer: sync.Pacer, stop: asyncio.Event,
    on_page: PageSink, on_wait: Callable[[int], Awaitable[None]],
    retries: int = TRANSIENT_RETRIES, retry_wait: float = TRANSIENT_WAIT,
) -> None:
    """Весь список диалогов, страница за страницей. Каждую полученную страницу отдаёт `on_page`
    (строки, сколько всего назвал Telegram, последняя ли) — до запроса следующей.

    FLOOD_WAIT: `pacer.flood`, `on_wait(секунды)`, ожидание и повтор той же страницы. Сбой связи —
    до `retries` повторов той же страницы с паузой. Остановка аккаунта — `sync.Stopped`."""
    cursor = _Cursor(first_request())
    first, failures = True, 0
    while True:
        if first:
            await pacer.wait(stop)         # первая страница — в общую очередь с историей
        else:
            await pacer.wait_flood(stop)
        try:
            response = await client(cursor.request)
        except sync.FLOOD as exc:
            pacer.flood(exc.seconds)
            await on_wait(int(exc.seconds))
            continue
        except (sync.Stopped, asyncio.CancelledError):
            raise
        except sync.FATAL:
            raise
        except sync.TRANSIENT:
            failures += 1
            if failures > retries:
                raise
            if await sync.interruptible_sleep(retry_wait, stop):
                raise sync.Stopped() from None
            continue
        finally:
            pacer.note_request()
        first, failures = False, 0
        rows, done = parse_page(client, cursor, response, self_id=self_id)
        expected = getattr(response, "count", None)
        await on_page(rows, int(expected) if expected is not None else len(cursor.seen), done)
        if done:
            return


# --- база ----------------------------------------------------------------------------------------

def search_text(chat: ChatRecord) -> str:
    return "\n".join(part.casefold() for part in (chat.name or "", chat.username or "") if part)


async def begin(conn: asyncpg.Connection, account_id: int) -> int:
    """Номер новой загрузки аккаунта."""
    return int(await conn.fetchval(
        """INSERT INTO tg_dialog_lists AS l (account_id, load_id, started_at) VALUES ($1, 1, now())
           ON CONFLICT (account_id) DO UPDATE SET load_id = l.load_id + 1, started_at = now()
           RETURNING load_id""", account_id))


async def save_page(conn: asyncpg.Connection, account_id: int, load_id: int, rows: list[DialogRow],
                    *, position: int, expected: int | None) -> None:
    """Страница загрузки — в список аккаунта: новые диалоги добавляются, известные обновляются."""
    if rows:
        await conn.execute(
            """INSERT INTO tg_dialogs AS t (account_id, peer_class, tg_id, chat_type, title, username, is_bot, search,
                                            top_message, last_message_at, folder_id, pinned, position, load_id)
               SELECT $1, r.peer_class, r.tg_id, r.chat_type, r.title, r.username, r.is_bot, r.search,
                      r.top_message, r.last_message_at, r.folder_id, r.pinned, r.position, $2
               FROM unnest($3::text[], $4::bigint[], $5::text[], $6::text[], $7::text[], $8::boolean[], $9::text[],
                           $10::bigint[], $11::timestamptz[], $12::int[], $13::boolean[], $14::int[])
                    AS r (peer_class, tg_id, chat_type, title, username, is_bot, search, top_message, last_message_at,
                          folder_id, pinned, position)
               ON CONFLICT (account_id, peer_class, tg_id) DO UPDATE
               SET chat_type = EXCLUDED.chat_type, title = EXCLUDED.title, username = EXCLUDED.username,
                   is_bot = EXCLUDED.is_bot, search = EXCLUDED.search, top_message = EXCLUDED.top_message,
                   last_message_at = EXCLUDED.last_message_at, folder_id = EXCLUDED.folder_id,
                   pinned = EXCLUDED.pinned, position = EXCLUDED.position, load_id = EXCLUDED.load_id,
                   updated_at = now()""",
            account_id, load_id,
            [r.chat.peer_class for r in rows], [r.chat.tg_id for r in rows], [r.chat.type for r in rows],
            [_clean(r.chat.name) for r in rows], [_clean(r.chat.username) for r in rows],
            [r.chat.is_bot for r in rows], [_clean(search_text(r.chat)) or "" for r in rows],
            [r.top_message for r in rows], [r.last_message_at for r in rows], [r.folder_id for r in rows],
            [r.pinned for r in rows], list(range(position, position + len(rows))))
    if expected is not None:
        await conn.execute("UPDATE tg_dialog_lists SET expected = $2 WHERE account_id = $1", account_id, expected)


def _clean(value: str | None) -> str | None:
    """Нулевой символ Postgres в тексте не принимает."""
    return value.replace("\x00", "") if isinstance(value, str) else value


async def finish(conn: asyncpg.Connection, account_id: int, load_id: int, *, mark_seen: bool) -> int:
    """Загрузка дошла до конца: диалоги, которых в ней не было, убираются из списка (не из архива).
    Возвращает, сколько убрано."""
    async with conn.transaction():
        removed = await conn.execute(
            "DELETE FROM tg_dialogs WHERE account_id = $1 AND load_id <> $2", account_id, load_id)
        await conn.execute("UPDATE tg_dialog_lists SET completed_at = now() WHERE account_id = $1", account_id)
        # Список наполнил Telethon ключами доступа — чаты, которые не удавалось опросить из-за их
        # отсутствия, можно пробовать снова.
        await conn.execute(
            "UPDATE tg_sync_chats SET last_error = NULL WHERE account_id = $1 AND last_error = 'entity_unknown'",
            account_id)
        if mark_seen:
            await conn.execute(
                """INSERT INTO tg_sync_chats (account_id, peer_class, tg_id)
                   SELECT account_id, peer_class, tg_id FROM tg_dialogs WHERE account_id = $1
                   ON CONFLICT DO NOTHING""", account_id)
    return int(removed.split()[-1])


async def forget(conn: asyncpg.Connection, account_id: int) -> None:
    """Список диалогов аккаунта больше не нужен (выход из аккаунта)."""
    await conn.execute("DELETE FROM tg_dialogs WHERE account_id = $1", account_id)
    await conn.execute("DELETE FROM tg_dialog_lists WHERE account_id = $1", account_id)


async def info(conn: asyncpg.Connection, account_id: int) -> dict[str, Any]:
    """Есть ли список, когда загружен целиком и сколько в нём диалогов."""
    row = await conn.fetchrow(
        """SELECT l.completed_at, l.expected,
                  (now() - l.completed_at) AS age,
                  (SELECT count(*) FROM tg_dialogs d WHERE d.account_id = $1) AS stored
           FROM (SELECT 1) one LEFT JOIN tg_dialog_lists l ON l.account_id = $1""", account_id)
    age = row["age"].total_seconds() if row["age"] is not None else None
    return {"completed_at": row["completed_at"], "expected": row["expected"], "stored": int(row["stored"]),
            "age": max(0, int(age)) if age is not None else None}


def _row_chat(row: asyncpg.Record) -> ChatRecord:
    return ChatRecord(row["peer_class"], int(row["tg_id"]), row["chat_type"], row["title"],
                      username=row["username"], is_bot=row["is_bot"])


def row_dialog(row: asyncpg.Record) -> DialogRow:
    return DialogRow(chat=_row_chat(row), top_message=int(row["top_message"]),
                     last_message_at=row["last_message_at"], folder_id=int(row["folder_id"]),
                     pinned=bool(row["pinned"]))


_COLUMNS = "peer_class, tg_id, chat_type, title, username, is_bot, top_message, last_message_at, folder_id, pinned"


async def rows(conn: asyncpg.Connection, account_id: int, *, keys: Iterable[PeerKey] | None = None,
               chat_types: Iterable[str] | None = None) -> list[DialogRow]:
    """Диалоги аккаунта из базы, в порядке списка Telegram. `keys`, `chat_types` — отбор."""
    where, args = ["account_id = $1"], [account_id]
    if keys is not None:
        keys = list(keys)
        args += [[k[0] for k in keys], [k[1] for k in keys]]
        where.append(f"(peer_class, tg_id) IN (SELECT * FROM unnest(${len(args) - 1}::text[], ${len(args)}::bigint[]))")
    if chat_types is not None:
        args.append(list(chat_types))
        where.append(f"chat_type = ANY(${len(args)}::text[])")
    found = await conn.fetch(
        f"SELECT {_COLUMNS} FROM tg_dialogs WHERE {' AND '.join(where)} ORDER BY position, peer_class, tg_id", *args)
    return [row_dialog(r) for r in found]


async def page(conn: asyncpg.Connection, account_id: int, *, offset: int, limit: int,
               chat_types: Iterable[str] | None = None, needle: str = "",
               only_enabled: bool = False) -> tuple[int, list[DialogRow]]:
    """Страница списка для экрана выбора: (сколько прошло отбор, строки страницы)."""
    where, args = ["d.account_id = $1"], [account_id]
    if chat_types is not None:
        args.append(list(chat_types))
        where.append(f"d.chat_type = ANY(${len(args)}::text[])")
    if needle:
        args.append(needle)
        where.append(f"strpos(d.search, ${len(args)}) > 0")
    if only_enabled:
        where.append("""EXISTS (SELECT 1 FROM tg_sync_chats s WHERE s.account_id = d.account_id
                        AND s.peer_class = d.peer_class AND s.tg_id = d.tg_id AND s.enabled)""")
    cond = " AND ".join(where)
    total = int(await conn.fetchval(f"SELECT count(*) FROM tg_dialogs d WHERE {cond}", *args))
    args += [limit, offset]
    found = await conn.fetch(
        f"""SELECT {', '.join('d.' + c.strip() for c in _COLUMNS.split(','))} FROM tg_dialogs d WHERE {cond}
            ORDER BY d.position, d.peer_class, d.tg_id LIMIT ${len(args) - 1} OFFSET ${len(args)}""", *args)
    return total, [row_dialog(r) for r in found]
