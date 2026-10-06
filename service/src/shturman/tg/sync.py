"""Чтение истории чатов: загрузка вглубь, дозагрузка вперёд, сверка удалений.

Ничего из аккаунта не берётся, пока владелец не выбрал чаты: работа идёт только по строкам
`tg_sync_chats` с признаком enabled, и чат, исключённый в архиве, пропускается.

Три вида работы, все — одной задачей на аккаунт, запросы идут строго по одному:

  * загрузка вглубь — от новых сообщений к старым, страницами по 100. Курсор (наименьший
    сохранённый номер) пишется в одной транзакции со страницей, поэтому после обрыва работа
    продолжается ровно с места. Страница, на которой курсор не сдвинулся, отвергается;
  * дозагрузка вперёд — от своего курсора `forward_id` к новым сообщениям. Обязательна после
    каждого запуска и переподключения: `catch_up()` Telethon только ставит запрос разницы в
    очередь, а слишком длинную разницу Telegram молча не отдаёт. Курсор двигает только чтение
    истории, не живые события, поэтому повторный проход закрывает и потерянные события;
  * сверка удалений — по недавним сообщениям чата: в личных чатах и обычных группах событие
    удаления приходит без чата и может не найти сообщение, а за время простоя теряется.

Ограничения Telegram: запрос истории — примерно 10 за 30 секунд, поэтому между запросами
пауза; при FLOOD_WAIT сервис ждёт названное время (и секунду сверху) и не повторяет раньше.
Все ожидания прерываются остановкой сервиса.

Порядок запросов и вид курсоров — по образцу:
# Основано на j2h4u/mcp-telegram (MIT; форк sparfenyuk/mcp-telegram, MIT),
#   src/mcp_telegram/sync_worker.py, delta_sync.py, flood.py, event_handlers.py,
#   message_history/telegram_adapter.py@1acce79
# (хранилище и планировщик запросов оттуда не переносились)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Iterable

import asyncpg
from telethon import errors, utils
from telethon.tl import functions, types

from .. import events as ev
from .. import store
from ..records import ChatRecord
from . import normalize
from .normalize import PeerKey

logger = logging.getLogger("shturman.tg")

PAGE = 100                    # предел Telegram на страницу истории
GAP_PAGES_PER_CHAT = 50       # за один проход на чат; остальное — следующим проходом
SWEEP_EVERY = 6 * 3600        # повторная дозагрузка вперёд по всем выбранным чатам
RECONCILE_EVERY = 6 * 3600    # сверка удалений
RECONCILE_WINDOW = 7 * 86400  # «недавно активный» чат и глубина сверки
RECONCILE_LIMIT = 300         # сообщений на чат за одну сверку
IDLE_WAIT = 60.0
ERROR_WAIT = 60.0


def _errors(*names: str) -> tuple[type[BaseException], ...]:
    return tuple(cls for cls in (getattr(errors, n, None) for n in names) if cls is not None)


# Доступ к чату потерян: повторять бессмысленно, пока владелец не включит чат заново.
ACCESS_LOST = _errors(
    "ChannelPrivateError", "ChannelBannedError", "ChannelInvalidError", "ChatForbiddenError",
    "UserBannedInChannelError", "UserKickedError", "PeerIdInvalidError", "ChatIdInvalidError",
    "InputUserDeactivatedError",
)
# Сессия мертва: отозвана, аккаунт удалён или ключ использован вторым подключением.
# AUTH_KEY_DUPLICATED повторять нельзя — ключ после него недействителен.
FATAL = (errors.UnauthorizedError, errors.AuthKeyError)
FLOOD = _errors("FloodWaitError", "FloodPremiumWaitError")
TRANSIENT = (errors.RPCError, ConnectionError, OSError, asyncio.TimeoutError)


class Stopped(Exception):
    """Работа прервана остановкой сервиса или аккаунта."""


class CursorStuck(Exception):
    """Telegram вернул страницу, которая не продвигает курсор."""


async def interruptible_sleep(seconds: float, stop: asyncio.Event) -> bool:
    """Ждёт `seconds` или до остановки. True — остановка пришла раньше."""
    if seconds <= 0:
        return stop.is_set()
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


class Pacer:
    """Пауза между запросами к Telegram и соблюдение FLOOD_WAIT — одна на аккаунт."""

    def __init__(
        self, interval: float = 3.0, *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float, asyncio.Event], Awaitable[bool]] = interruptible_sleep,
    ) -> None:
        self.interval = interval
        self._clock, self._sleep = clock, sleep
        self._next_at = 0.0
        self._flood_at = 0.0
        self._lock = asyncio.Lock()
        self.flood_until: datetime | None = None  # для экрана состояния

    def flood(self, seconds: int) -> None:
        """Telegram велел ждать: раньше названного времени запросов не будет."""
        wait = max(1, int(seconds)) + 1
        self._flood_at = max(self._flood_at, self._clock() + wait)
        self.flood_until = datetime.now(timezone.utc) + timedelta(seconds=wait)

    def flood_remaining(self) -> float:
        """Сколько секунд ещё ждать по требованию Telegram (0 — ожидания нет)."""
        return max(0.0, self._flood_at - self._clock())

    async def wait(self, stop: asyncio.Event) -> None:
        """Дожидается своей очереди. Бросает Stopped, если пришла остановка."""
        async with self._lock:
            while True:
                if stop.is_set():
                    raise Stopped()
                delay = max(self._next_at, self._flood_at) - self._clock()
                if delay <= 0:
                    break
                if await self._sleep(delay, stop):
                    raise Stopped()
            self._next_at = self._clock() + self.interval
            if self.flood_until is not None and self._flood_at <= self._clock():
                self.flood_until = None


# --- строки синхронизации ---

@dataclass
class ChatState:
    chat_id: int | None
    enabled: bool


async def load_index(conn: asyncpg.Connection, account_id: int) -> dict[PeerKey, ChatState]:
    rows = await conn.fetch(
        "SELECT peer_class, tg_id, chat_id, enabled FROM tg_sync_chats WHERE account_id = $1", account_id)
    return {(r["peer_class"], r["tg_id"]): ChatState(r["chat_id"], r["enabled"]) for r in rows}


class _AccountDefault:
    """Глубина истории не названа: берётся настройка аккаунта."""


ACCOUNT_DEFAULT: Any = _AccountDefault()


async def enable_chat(
    conn: asyncpg.Connection, account_id: int, chat: ChatRecord, *, auto: bool = False,
    since: datetime | None | _AccountDefault = ACCOUNT_DEFAULT,
) -> tuple[int, bool]:
    """Включает синхронизацию чата. Возвращает (идентификатор чата в архиве, включён ли).

    Чат, исключённый в архиве (в том числе служебные чаты Telegram), не включается.
    Повторное включение снимает отметку о потерянном доступе и продолжает с прежних курсоров.

    `since` — граница загрузки истории вглубь: сообщения старше неё не загружаются, None — вся
    история. Не названа — у чата, который включают впервые, берётся глубина по умолчанию из
    настроек аккаунта, а у уже включавшегося остаётся прежняя. Названная граница глубже прежней
    возобновляет загрузку с места, где она остановилась.
    """
    explicit = not isinstance(since, _AccountDefault)
    async with conn.transaction():
        chat_id, excluded = await store.ensure_chat(conn, account_id, chat, refresh=True)
        if not explicit:
            since = await conn.fetchval(
                """SELECT now() - make_interval(months => backfill_months) FROM tg_sessions
                   WHERE account_id = $1""", account_id)
        await conn.execute(
            """INSERT INTO tg_sync_chats AS t (account_id, peer_class, tg_id, chat_id, enabled, auto_enabled,
                                               enabled_at, backfill_since)
               VALUES ($1, $2, $3, $4, $5, $6, CASE WHEN $5 THEN now() END, $7)
               ON CONFLICT (account_id, peer_class, tg_id) DO UPDATE
               SET chat_id = EXCLUDED.chat_id, enabled = EXCLUDED.enabled,
                   auto_enabled = EXCLUDED.auto_enabled,
                   enabled_at = COALESCE(t.enabled_at, EXCLUDED.enabled_at),
                   backfill_since = CASE WHEN $8 OR t.enabled_at IS NULL
                                         THEN EXCLUDED.backfill_since ELSE t.backfill_since END,
                   backfill_done = CASE
                       WHEN ($8 OR t.enabled_at IS NULL) AND t.backfill_before IS NOT NULL
                            AND ((EXCLUDED.backfill_since IS NULL AND t.backfill_since IS NOT NULL)
                                 OR EXCLUDED.backfill_since < t.backfill_since)
                       THEN false ELSE t.backfill_done END,
                   access_lost_at = NULL, access_lost_reason = NULL, last_error = NULL,
                   updated_at = now()""",
            account_id, chat.peer_class, chat.tg_id, chat_id, not excluded, auto and not excluded,
            since, explicit,
        )
    return chat_id, not excluded


async def chat_excluded(conn: asyncpg.Connection, chat_id: int, *, purged: bool) -> tuple[int, PeerKey] | None:
    """Владелец исключил чат из архива: синхронизация чата выключается сразу, у всех аккаунтов.

    Если сообщения стёрты, сбрасываются и курсоры: иначе после возврата чата сервис считал бы
    его историю уже загруженной. Возвращает (аккаунт, собеседник) затронутой строки.
    """
    row = await conn.fetchrow(
        """UPDATE tg_sync_chats
           SET enabled = false, updated_at = now(),
               backfill_before = CASE WHEN $2 THEN NULL ELSE backfill_before END,
               backfill_done = CASE WHEN $2 THEN false ELSE backfill_done END,
               forward_id = CASE WHEN $2 THEN NULL ELSE forward_id END
           WHERE chat_id = $1 RETURNING account_id, peer_class, tg_id""",
        chat_id, purged)
    if row is None:
        return None
    return row["account_id"], (row["peer_class"], row["tg_id"])


async def promote_hidden_edits(
    conn: asyncpg.Connection, rows: Iterable[tuple], hidden: dict[tuple[int, int], datetime]
) -> set[tuple[int, int]]:
    """Разбирает «скрытые правки» (см. `normalize.hidden_edit_at`) перед записью.

    Обычно скрытая правка — это реакция: текст тот же, и запись в архиве не меняется, отметка
    «изменено» не появляется. Но если текст сообщения отличается от сохранённого, сервис
    пропустил настоящую правку; тогда время скрытой правки становится временем правки, чтобы
    общий слой записи принял новый текст, а прежний убрал в историю. Возвращает ключи
    (чат, номер сообщения), для которых так и вышло.
    """
    if not hidden:
        return set()
    by_chat: dict[int, list[int]] = {}
    for chat_id, tg_message_id in hidden:
        by_chat.setdefault(chat_id, []).append(tg_message_id)
    stored: dict[tuple[int, int], asyncpg.Record] = {}
    for chat_id, ids in by_chat.items():
        for r in await conn.fetch(
                "SELECT tg_message_id, text, edited_at FROM messages WHERE chat_id = $1 AND tg_message_id = ANY($2::bigint[])",
                chat_id, ids):
            stored[(chat_id, r["tg_message_id"])] = r
    promoted: set[tuple[int, int]] = set()
    for item in rows:
        chat_id, record = item[0], item[1]
        key = (chat_id, record.tg_message_id)
        at, known = hidden.get(key), stored.get(key)
        if at is None or known is None:
            continue
        if known["text"] != record.text.replace("\x00", "") and \
                (known["edited_at"] is None or at > known["edited_at"]):
            record.edited_at = at
            promoted.add(key)
    return promoted


async def disable_chat(conn: asyncpg.Connection, account_id: int, key: PeerKey) -> None:
    """Выключает синхронизацию. Уже сохранённое остаётся в архиве; курсоры сохраняются.
    Строка создаётся и для чата, которого сервис ещё не видел: явный отказ владельца
    не должна перекрыть настройка «брать новые чаты»."""
    await conn.execute(
        """INSERT INTO tg_sync_chats (account_id, peer_class, tg_id, enabled) VALUES ($1, $2, $3, false)
           ON CONFLICT (account_id, peer_class, tg_id) DO UPDATE SET enabled = false, updated_at = now()""",
        account_id, key[0], key[1],
    )


async def mark_seen(conn: asyncpg.Connection, account_id: int, keys: Iterable[PeerKey]) -> None:
    """Запоминает, что чаты уже существовали: настройка «брать новые чаты» их не тронет.
    Хранится только класс и идентификатор — ни названия, ни сообщений."""
    keys = list(keys)
    if not keys:
        return
    await conn.execute(
        """INSERT INTO tg_sync_chats (account_id, peer_class, tg_id)
           SELECT $1, k.peer_class, k.tg_id FROM unnest($2::text[], $3::bigint[]) AS k (peer_class, tg_id)
           ON CONFLICT DO NOTHING""",
        account_id, [k[0] for k in keys], [k[1] for k in keys],
    )


_WORK = """
SELECT s.peer_class, s.tg_id, s.chat_id, s.backfill_before, s.backfill_done, s.forward_id,
       s.backfill_since
FROM tg_sync_chats s JOIN chats c ON c.id = s.chat_id
WHERE s.account_id = $1 AND s.enabled AND NOT c.excluded AND s.access_lost_at IS NULL
"""
# Сначала личные чаты, затем группы, затем супергруппы и каналы.
_ORDER = " ORDER BY CASE s.peer_class WHEN 'user' THEN 0 WHEN 'chat' THEN 1 ELSE 2 END, s.tg_id"


class HistorySync:
    """Работа с историей одного аккаунта. Клиент — любой объект, который умеет
    `await client(request)`, `get_input_entity(peer)` и `catch_up()`."""

    def __init__(
        self, *, client: Any, pool: asyncpg.Pool, events: ev.Events, account_id: int, self_id: int,
        pacer: Pacer, stop: asyncio.Event,
    ) -> None:
        self.client, self.pool, self.events = client, pool, events
        self.account_id, self.self_id = account_id, self_id
        self.pacer, self.stop = pacer, stop
        self.busy: str | None = None  # что делает сейчас — для экрана состояния
        self.idle = False             # постоянная работа всё доделала и ждёт

    # --- запросы ---

    async def call(self, request: Any) -> Any:
        """Запрос с паузой. При FLOOD_WAIT ждёт названное время и повторяет — не раньше."""
        while True:
            await self.pacer.wait(self.stop)
            try:
                return await self.client(request)
            except FLOOD as exc:
                logger.warning("аккаунт %s: Telegram просит подождать %s с", self.account_id, exc.seconds)
                self.pacer.flood(exc.seconds)

    async def _history(self, key: PeerKey, *, offset_id: int, add_offset: int) -> Any:
        peer = await self.client.get_input_entity(normalize.to_peer(key))
        return await self.call(functions.messages.GetHistoryRequest(
            peer=peer, offset_id=offset_id, offset_date=None, add_offset=add_offset,
            limit=PAGE, max_id=0, min_id=0, hash=0,
        ))

    def _rows(
        self, key: PeerKey, chat_id: int, response: Any, *, newer_than: int = 0,
        since: datetime | None = None,
    ) -> list[tuple]:
        """Строки для записи: (чат, запись, исходящее ли, время скрытой правки или None)."""
        entities = normalize.index_entities(getattr(response, "users", None), getattr(response, "chats", None))
        rows = []
        for message in getattr(response, "messages", None) or ():
            if normalize.peer_key(getattr(message, "peer_id", None)) != key:
                continue  # пустое сообщение или чужой чат — в этот чат не пишем
            record = normalize.message_record(message, entities, self_id=self.self_id)
            if record is None or record.tg_message_id <= newer_than:
                continue
            if since is not None and record.sent_at < since:
                continue  # старше границы загрузки
            rows.append((chat_id, record, normalize.is_outgoing(message, self_id=self.self_id),
                         normalize.hidden_edit_at(message)))
        return rows

    async def _store_page(self, key: PeerKey, rows: list[tuple], update_sql: str, *args: Any) -> bool:
        """Страница и курсор — одной транзакцией. False — чат успели выключить или исключить."""
        async with self.pool.acquire() as conn, conn.transaction():
            live = await conn.fetchval(
                """SELECT s.enabled AND NOT c.excluded FROM tg_sync_chats s JOIN chats c ON c.id = s.chat_id
                   WHERE s.account_id = $1 AND s.peer_class = $2 AND s.tg_id = $3 FOR UPDATE OF s""",
                self.account_id, *key)
            if not live:
                return False
            if rows:
                hidden = {(r[0], r[1].tg_message_id): r[3] for r in rows if r[3] is not None}
                await promote_hidden_edits(conn, rows, hidden)
                await store.upsert_messages(conn, [r[:3] for r in rows], source="session",
                                            owner_tg_id=self.self_id)
            await conn.execute(
                f"UPDATE tg_sync_chats SET {update_sql}, last_error = NULL, updated_at = now() "
                "WHERE account_id = $1 AND peer_class = $2 AND tg_id = $3",
                self.account_id, *key, *args)
        return True

    async def _note(self, key: PeerKey, *, error: str | None = None, lost: str | None = None) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """UPDATE tg_sync_chats
                   SET last_error = $4, updated_at = now(),
                       access_lost_at = CASE WHEN $5::text IS NULL THEN access_lost_at ELSE now() END,
                       access_lost_reason = COALESCE($5, access_lost_reason)
                   WHERE account_id = $1 AND peer_class = $2 AND tg_id = $3""",
                self.account_id, *key, error, lost)

    async def _guarded(self, key: PeerKey, work: Callable[[], Awaitable[Any]], default: Any) -> Any:
        """Выполняет работу по чату. Потеря доступа помечает чат; временная ошибка откладывает его."""
        try:
            return await work()
        except (Stopped, asyncio.CancelledError):
            raise
        except FATAL:
            raise
        except ACCESS_LOST as exc:
            logger.info("аккаунт %s: доступ к чату %s%s потерян (%s)",
                        self.account_id, key[0], key[1], type(exc).__name__)
            await self._note(key, lost=type(exc).__name__)
        except CursorStuck:
            logger.warning("аккаунт %s: курсор истории чата %s%s не сдвинулся", self.account_id, *key)
            await self._note(key, error="cursor_stuck")
        except ValueError:
            # Telethon не знает ключа доступа к собеседнику: он появится после списка диалогов.
            await self._note(key, error="entity_unknown")
        except TRANSIENT as exc:
            logger.warning("аккаунт %s: чат %s%s, запрос не удался (%s)",
                           self.account_id, key[0], key[1], type(exc).__name__)
            await self._note(key, error=type(exc).__name__)
        return default

    # --- загрузка вглубь ---

    async def backfill_page(self, row: asyncpg.Record) -> bool:
        """Одна страница истории чата. True — страница сохранена и работа по чату не закончена."""
        key: PeerKey = (row["peer_class"], row["tg_id"])
        cursor = row["backfill_before"] or 0
        response = await self._history(key, offset_id=cursor, add_offset=0)
        raw = [m for m in getattr(response, "messages", None) or () if not isinstance(m, types.MessageEmpty)]
        ids = [int(m.id) for m in raw]
        if not ids:
            await self._store_page(key, [], "backfill_done = true, forward_id = COALESCE(forward_id, 0)")
            return False
        lowest = min(ids)
        if cursor and lowest >= cursor:
            raise CursorStuck()
        # Полный список (не «срез») означает, что старше ничего нет.
        done = isinstance(response, types.messages.Messages) or lowest <= 1
        since = row["backfill_since"]
        rows = self._rows(key, row["chat_id"], response, since=since)
        if since is not None:
            # Граница глубины: что старше — не берём, и дальше вглубь не идём. Курсор встаёт на
            # самое старое из взятого, чтобы при сдвиге границы продолжить ровно отсюда.
            dates = [m.date for m in raw if getattr(m, "date", None) is not None]
            if dates and min(dates) < since:
                done = True
                kept = [r[1].tg_message_id for r in rows]
                lowest = min(kept) if kept else (cursor or max(ids) + 1)
        stored = await self._store_page(
            key, rows,
            "backfill_before = $4, backfill_done = $5, forward_id = COALESCE(forward_id, $6)",
            lowest, done, max(ids))
        return stored and not done

    async def backfill_round(self) -> int:
        """По одной странице на каждый чат с незаконченной историей. Возвращает число чатов,
        по которым работа продолжается."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                _WORK + """ AND NOT s.backfill_done
                    AND (s.last_error IS NULL OR s.last_error NOT IN ('cursor_stuck', 'entity_unknown'))""" + _ORDER,
                self.account_id)
        more = 0
        for row in rows:
            self.busy = "backfill"
            key = (row["peer_class"], row["tg_id"])
            if await self._guarded(key, lambda r=row: self.backfill_page(r), False):
                more += 1
        self.busy = None
        return more

    async def backfill_all(self) -> None:
        """Грузит историю, пока есть что грузить (для тестов и ручного запуска)."""
        while await self.backfill_round():
            pass

    # --- дозагрузка вперёд ---

    async def gap_fill_chat(self, row: asyncpg.Record) -> bool:
        """Дочитывает чат от своего курсора к новым сообщениям. True — осталось ещё."""
        key: PeerKey = (row["peer_class"], row["tg_id"])
        cursor = int(row["forward_id"])
        for _ in range(GAP_PAGES_PER_CHAT):
            # Страница из 100 сообщений сразу после курсора.
            response = await self._history(key, offset_id=cursor + 1, add_offset=-PAGE)
            rows = self._rows(key, row["chat_id"], response, newer_than=cursor)
            if not rows:
                await self._store_page(key, [], "gap_checked_at = now()")
                return False
            highest = max(r[1].tg_message_id for r in rows)
            if not await self._store_page(key, rows, "forward_id = $4, gap_checked_at = now()", highest):
                return False
            cursor = highest
            if len(rows) < PAGE:
                return False
        return True

    async def gap_fill_pass(
        self, tops: Callable[[], Awaitable[dict[PeerKey, int] | None]] | None = None
    ) -> bool:
        """Проход дозагрузки по всем выбранным чатам. True — у какого-то чата осталось ещё.

        `tops` — необязательный способ узнать номер последнего сообщения каждого диалога одним
        списком: чаты, где нового нет, тогда не опрашиваются по одному.
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(_WORK + " AND s.forward_id IS NOT NULL" + _ORDER, self.account_id)
        if not rows:
            return False
        known: dict[PeerKey, int] = {}
        if tops is not None and len(rows) > 3:
            try:
                known = await tops() or {}
            except (Stopped, asyncio.CancelledError):
                raise
            except FATAL:
                raise
            except Exception as exc:  # список диалогов — только оптимизация
                logger.info("аккаунт %s: список диалогов недоступен (%s), опрашиваю чаты по одному",
                            self.account_id, type(exc).__name__)
        more = False
        for row in rows:
            key = (row["peer_class"], row["tg_id"])
            top = known.get(key)
            if top is not None and top <= row["forward_id"]:
                continue
            self.busy = "gap_fill"
            more = await self._guarded(key, lambda r=row: self.gap_fill_chat(r), False) or more
        self.busy = None
        return more

    # --- сверка удалений ---

    async def reconcile_chat(self, key: PeerKey, chat_id: int) -> int:
        async with self.pool.acquire() as conn:
            ids = [r["tg_message_id"] for r in await conn.fetch(
                """SELECT tg_message_id FROM messages
                   WHERE chat_id = $1 AND deleted_at IS NULL AND sent_at > now() - make_interval(secs => $2)
                   ORDER BY tg_message_id DESC LIMIT $3""",
                chat_id, float(RECONCILE_WINDOW), RECONCILE_LIMIT)]
        gone: list[int] = []
        for start in range(0, len(ids), PAGE):
            batch = ids[start:start + PAGE]
            wanted = [types.InputMessageID(i) for i in batch]
            if key[0] == "channel":
                peer = await self.client.get_input_entity(normalize.to_peer(key))
                request: Any = functions.channels.GetMessagesRequest(utils.get_input_channel(peer), wanted)
            else:
                request = functions.messages.GetMessagesRequest(wanted)
            response = await self.call(request)
            asked = set(batch)
            # Удалённым считается только то, на что Telegram явно ответил «пусто».
            gone += [int(m.id) for m in getattr(response, "messages", None) or ()
                     if isinstance(m, types.MessageEmpty) and m.id in asked]
        deleted: list[int] = []
        async with self.pool.acquire() as conn:
            if gone:
                deleted = await store.mark_deleted(conn, chat_id, gone)
            await conn.execute(
                "UPDATE tg_sync_chats SET reconciled_at = now() WHERE account_id = $1 AND peer_class = $2 AND tg_id = $3",
                self.account_id, *key)
        if deleted:
            self.events.publish(ev.MESSAGES_DELETED, {"message_ids": deleted})
        return len(deleted)

    async def reconcile_pass(self) -> int:
        """Сверяет недавние сообщения недавно активных чатов. Возвращает число найденных удалений."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                _WORK + """
                AND (s.reconciled_at IS NULL OR s.reconciled_at < now() - make_interval(secs => $2))
                AND EXISTS (SELECT 1 FROM messages m WHERE m.chat_id = s.chat_id AND m.deleted_at IS NULL
                            AND m.sent_at > now() - make_interval(secs => $3))""" + _ORDER,
                self.account_id, float(RECONCILE_EVERY) / 2, float(RECONCILE_WINDOW))
        found = 0
        for row in rows:
            self.busy = "reconcile"
            key = (row["peer_class"], row["tg_id"])
            found += await self._guarded(key, lambda r=row, k=key: self.reconcile_chat(k, r["chat_id"]), 0)
        self.busy = None
        return found

    # --- постоянная работа ---

    async def run(
        self, wake: asyncio.Event, reconnected: asyncio.Event, *,
        tops: Callable[[], Awaitable[dict[PeerKey, int] | None]] | None = None,
    ) -> None:
        """Крутится, пока аккаунт подключён. `wake` — появилась работа (включили чат);
        `reconnected` — соединение восстановлено, нужно догнать пропущенное."""
        first = True
        next_sweep = next_reconcile = 0.0
        while not self.stop.is_set():
            progressed = 0
            self.idle = False
            try:
                now = time.monotonic()
                if first or reconnected.is_set() or now >= next_sweep:
                    reconnected.clear()
                    # Telethon после переподключения сам пропущенное не запрашивает.
                    await self.client.catch_up()
                    more = await self.gap_fill_pass(tops)
                    first = False
                    next_sweep = time.monotonic() + (0 if more else SWEEP_EVERY)
                if time.monotonic() >= next_reconcile:
                    await self.reconcile_pass()
                    next_reconcile = time.monotonic() + RECONCILE_EVERY
                wake.clear()
                progressed = await self.backfill_round()
            except Stopped:
                break
            except FATAL:
                raise
            except TRANSIENT as exc:
                logger.warning("аккаунт %s: работа с историей отложена (%s)", self.account_id, type(exc).__name__)
                if await interruptible_sleep(ERROR_WAIT, self.stop):
                    break
                continue
            if progressed or wake.is_set() or reconnected.is_set():
                continue
            pause = max(1.0, min(IDLE_WAIT, next_sweep - time.monotonic(), next_reconcile - time.monotonic()))
            self.idle = True
            try:
                await asyncio.wait_for(wake.wait(), timeout=pause)
            except asyncio.TimeoutError:
                pass
        self.busy = None
