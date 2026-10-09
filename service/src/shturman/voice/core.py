"""Очередь расшифровки голосовых и обработка одного сообщения.

Очередь — сами строки архива (`messages.transcript_state = 'pending'`), как у защиты и
эмбеддингов: отдельной таблицы нет. В очередь попадают голосовые и «кружки» не старше
`asr_days` дней из невыключенных чатов, сначала свежие. Пока сообщение ждёт, в
`transcript_at` лежит время следующей попытки.

Откуда берётся файл:
  * файл из загруженного архива выгрузки Telegram Desktop (`messages.media_file`, его кладёт
    импорт — media/from_export.py). Закончив с сообщением, очередь файл удаляет (`media.files.drop`).
    Файла нет или он не читается — дальше как без него;
  * сессия аккаунта, которому принадлежит чат, — по номеру сообщения (так же, как Telegram на
    компьютере открывает вложение). Подходит и для сообщений из выгрузки и бизнес-режима этого
    аккаунта: номера у них те же;
  * file_id бизнес-режима через своего бота сервиса — если сессии нет.
Ни того ни другого — сообщение ждёт сутки (аккаунт мог быть на паузе) и снимается с очереди.

Ничего здесь не отправляет сообщений и не отмечает прочитанным.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import asyncpg

from .. import events, guard
from ..media import files as media_files
from . import VOICE_TYPES
from .asr import AsrClient, AsrRejected, AsrUnavailable

logger = logging.getLogger("shturman.voice")

MAX_ATTEMPTS = 5                    # сбоев при скачивании или распознавании подряд — и хватит
RETRY = timedelta(minutes=10)       # пауза после сбоя
NO_SOURCE_WAIT = timedelta(hours=1)  # источника нет (сессия на паузе) — проверить позже
NO_SOURCE_GIVE_UP = timedelta(days=1)
ENQUEUE_BATCH = 500
FRESH = timedelta(hours=1)          # расшифровку сообщения не старше этого разбирают как живое

# Скачивание файла: (аккаунт, вид собеседника, его id, номер сообщения, предел байт) → (байты, секунды).
SessionFetch = Callable[[int, str, int, int, int], Awaitable[tuple[bytes, int | None]]]
# Скачивание по file_id бизнес-режима: (file_id, предел байт) → байты.
BotFetch = Callable[[str, int], Awaitable[bytes]]


class NoSource(Exception):
    """Скачать файл сейчас нечем: нет ни сессии аккаунта, ни своего бота с file_id."""


class Gone(Exception):
    """Файла больше нет или он не подходит: повторять бессмысленно."""


class Later(Exception):
    """Попробовать позже: Telegram просит подождать, сессия переподключается."""

    def __init__(self, seconds: float) -> None:
        super().__init__(f"повторить через {int(seconds)} с")
        self.seconds = seconds


@dataclass
class Settings:
    days: int = 30
    max_seconds: int = 600
    max_bytes: int = 20 * 1024 * 1024


_ENQUEUE = f"""
WITH todo AS (
    SELECT m.id FROM messages m JOIN chats c ON c.id = m.chat_id
    WHERE m.transcript_state IS NULL AND m.media_type IN ({", ".join(f"'{t}'" for t in VOICE_TYPES)})
      AND m.kind = 'message' AND m.deleted_at IS NULL AND NOT c.excluded
      AND m.sent_at >= now() - make_interval(days => $1)
    ORDER BY m.id DESC LIMIT $3
)
UPDATE messages m
SET transcript_state = CASE WHEN m.media_duration > $2 THEN 'skipped' ELSE 'pending' END,
    transcript_error = CASE WHEN m.media_duration > $2 THEN 'too_long' ELSE NULL END,
    transcript_at = CASE WHEN m.media_duration > $2 THEN now() ELSE NULL END
FROM todo WHERE m.id = todo.id
"""

_NEXT = """
SELECT m.id, m.tg_message_id, m.media_type, m.media_duration, m.media_ref, m.media_file, m.sent_at,
       m.transcript_attempts, m.first_seen_at, c.account_id, p.class AS peer_class, p.tg_id AS peer_tg_id
FROM messages m JOIN chats c ON c.id = m.chat_id JOIN peers p ON p.id = c.peer_id
WHERE m.transcript_state = 'pending' AND (m.transcript_at IS NULL OR m.transcript_at <= now())
  AND m.deleted_at IS NULL AND NOT c.excluded
ORDER BY m.sent_at DESC
LIMIT $1
"""

# Расшифровка готова. Решение защиты о прежнем тексте (подписи), если оно «скрыть», остаётся;
# иначе новый текст проверяется заново, а чужое сообщение при включённой защите до проверки
# скрыто от ассистента — как новое сообщение живого потока.
_DONE = """
UPDATE messages SET
    transcript = $2, transcript_state = 'done', transcript_error = NULL, transcript_at = now(),
    late_content = true,
    media_duration = COALESCE(media_duration, $3),
    text = voice_text(text, $2, media_type, COALESCE(media_duration, $3)),
    agent_visible = CASE WHEN $4 AND is_outgoing IS NOT TRUE THEN false ELSE agent_visible END,
    guard_label = CASE WHEN guard_label IN ('suspect', 'confirmed') THEN guard_label END,
    guard_score = CASE WHEN guard_label IN ('suspect', 'confirmed') THEN guard_score END,
    guard_model = CASE WHEN guard_label IN ('suspect', 'confirmed') THEN guard_model END,
    guard_checked_at = CASE WHEN guard_label IN ('suspect', 'confirmed') THEN guard_checked_at END
WHERE id = $1 AND transcript_state = 'pending' AND transcript IS NULL AND deleted_at IS NULL
RETURNING id, is_outgoing, chat_id, sent_at
"""


async def enqueue(conn: asyncpg.Connection, settings: Settings) -> int:
    done = await conn.execute(_ENQUEUE, settings.days, settings.max_seconds, ENQUEUE_BATCH)
    return int(done.split()[-1])


async def counters(conn: asyncpg.Connection) -> dict[str, int]:
    rows = await conn.fetch(
        "SELECT transcript_state AS s, count(*) AS n FROM messages WHERE transcript_state IS NOT NULL GROUP BY 1")
    out = {"voice_pending": 0, "voice_done": 0, "voice_failed": 0, "voice_skipped": 0}
    for row in rows:
        out[f"voice_{row['s']}"] = int(row["n"])
    return out


class Transcriber:
    """Работающая расшифровка. Одна на процесс сервиса."""

    def __init__(self, pool: asyncpg.Pool, asr: AsrClient, settings: Settings, *,
                 session_fetch: Callable[[], SessionFetch | None],
                 bot_fetch: Callable[[], BotFetch | None],
                 publish: Callable[[str, dict[str, Any]], None] | None = None,
                 data_dir: Path | None = None) -> None:
        self.pool, self.asr, self.settings = pool, asr, settings
        self.data_dir = data_dir     # каталог данных сервиса: там файлы из архива выгрузки
        self.publish = publish
        self._session_fetch, self._bot_fetch = session_fetch, bot_fetch
        self.problem: str | None = None       # None — в порядке; unreachable — контейнер не отвечает
        self.wake = asyncio.Event()

    # --- скачать ---

    async def _fetch(self, row: asyncpg.Record) -> tuple[bytes, int | None]:
        from ..executor.botapi import BotApiError, Refused
        from ..tg import gateway

        if row["media_file"] and self.data_dir is not None:
            try:
                return await asyncio.to_thread(media_files.read, self.data_dir, row["media_file"],
                                               max_bytes=self.settings.max_bytes), None
            except (OSError, ValueError, media_files.TooBig) as exc:
                # Файла нет или он негоден — берём из Telegram, как без него.
                logger.info("голосовое %s: файл из выгрузки не прочитан (%s)", row["id"], type(exc).__name__)
        session = self._session_fetch()
        if session is not None:
            try:
                return await session(row["account_id"], row["peer_class"], row["peer_tg_id"],
                                     row["tg_message_id"], self.settings.max_bytes)
            except gateway.MediaUnavailable as exc:
                raise Gone(str(exc)[:200]) from None
            except gateway.FloodWait as exc:
                raise Later(exc.seconds + 1) from None
            except gateway.AccountUnavailable:
                pass   # сессии этого аккаунта нет — попробуем бота
        bot = self._bot_fetch()
        if bot is not None and row["media_ref"]:
            try:
                return await bot(row["media_ref"], self.settings.max_bytes), None
            except Refused as exc:
                if exc.code == 429:
                    raise Later(exc.retry_after or 60) from None
                raise Gone(f"bot:{exc.reason}") from None
            except BotApiError as exc:
                raise Later(RETRY.total_seconds()) from exc
        raise NoSource()

    # --- одно сообщение ---

    async def process(self, row: asyncpg.Record) -> str:
        """Скачивает, распознаёт и записывает одно сообщение. Возвращает итог: done, skipped,
        failed, later — для журнала и тестов. Закончив с сообщением, удаляет файл из выгрузки."""
        outcome = await self._process(row)
        if outcome != "later" and row["media_file"] and self.data_dir is not None:
            try:
                async with self.pool.acquire() as conn:
                    await media_files.drop(conn, self.data_dir, row["id"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — файл уберёт уборка (media.files.sweep)
                logger.warning("голосовое %s: файл из выгрузки не удалён (%s)", row["id"], type(exc).__name__)
        return outcome

    async def _process(self, row: asyncpg.Record) -> str:
        mid = row["id"]
        try:
            audio, seconds = await self._fetch(row)
            if row["media_duration"] is None and seconds is not None and seconds > self.settings.max_seconds:
                raise Gone("too_long")
            result = await self.asr.transcribe(audio)
            self.problem = None
        except NoSource:
            age = datetime.now(timezone.utc) - row["first_seen_at"]
            if age > NO_SOURCE_GIVE_UP:
                await self._finish(mid, "skipped", "no_source")
                return "skipped"
            await self._later(mid, NO_SOURCE_WAIT, count=False)
            return "later"
        except Gone as exc:
            await self._finish(mid, "skipped", str(exc) or "gone")
            return "skipped"
        except Later as exc:
            await self._later(mid, timedelta(seconds=max(1.0, exc.seconds)), count=False)
            return "later"
        except AsrUnavailable as exc:
            # Контейнер не отвечает: сообщение не виновато — попыток не тратим.
            self.problem = "unreachable"
            logger.warning("распознавание речи недоступно (%s): голосовые ждут", exc)
            await self._later(mid, RETRY, count=False)
            return "later"
        except AsrRejected as exc:
            await self._finish(mid, "skipped", f"asr:{exc}")
            return "skipped"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — сбой одного сообщения не останавливает очередь
            logger.warning("голосовое %s: не расшифровано (%s)", mid, type(exc).__name__)
            if row["transcript_attempts"] + 1 >= MAX_ATTEMPTS:
                await self._finish(mid, "failed", type(exc).__name__)
                return "failed"
            await self._later(mid, RETRY, count=True)
            return "later"

        if row["media_duration"] is None and result.seconds is not None and result.seconds > self.settings.max_seconds:
            await self._finish(mid, "skipped", "too_long")
            return "skipped"
        text = " ".join(result.text.split())[:20000]
        duration = seconds if seconds is not None else (int(round(result.seconds)) if result.seconds is not None else None)
        holding = guard.holding()
        async with self.pool.acquire() as conn:
            updated = await conn.fetchrow(_DONE, mid, text, duration, holding)
        if updated is not None and holding and updated["is_outgoing"] is not True:
            await guard.screen([mid])
        if updated is not None:
            await self._announce(mid)
        return "done"

    async def _announce(self, mid: int) -> None:
        """Свежее голосовое стало текстом — сказать модулям, которым важен текст живых сообщений."""
        if self.publish is None:
            return
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT m.chat_id, m.is_outgoing, c.account_id FROM messages m JOIN chats c ON c.id = m.chat_id
                   WHERE m.id = $1 AND m.agent_visible AND m.deleted_at IS NULL
                     AND m.sent_at > now() - make_interval(secs => $2)""", mid, FRESH.total_seconds())
        if row is not None:
            self.publish(events.MESSAGE_CONTENT, {"account_id": row["account_id"], "chat_id": row["chat_id"],
                                                  "message_id": mid, "outgoing": bool(row["is_outgoing"])})

    async def _finish(self, mid: int, state: str, error: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """UPDATE messages SET transcript_state = $2, transcript_error = $3, transcript_at = now()
                   WHERE id = $1 AND transcript_state = 'pending'""", mid, state, error[:200])

    async def _later(self, mid: int, wait: timedelta, *, count: bool) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """UPDATE messages SET transcript_at = now() + $2::interval,
                       transcript_attempts = transcript_attempts + $3::int
                   WHERE id = $1 AND transcript_state = 'pending'""", mid, wait, 1 if count else 0)

    # --- обход ---

    async def _reachable(self) -> bool:
        """Контейнер распознавания отвечает? Пока он не отвечал, перед каждым обходом спрашиваем
        его заново: после запуска сервиса он может ещё загружать модель. Пока не ответил, файлы
        из Telegram не скачиваются — запросы впустую не тратятся."""
        if self.problem is None:
            return True
        try:
            await self.asr.health()
        except AsrUnavailable:
            return False
        self.problem = None
        logger.info("контейнер распознавания речи отвечает")
        return True

    async def step(self, batch: int = 5) -> int:
        """Ставит новое в очередь и обрабатывает до `batch` сообщений. Возвращает, сколько взято."""
        async with self.pool.acquire() as conn:
            await enqueue(conn, self.settings)
            if not await self._reachable():
                return 0
            rows = await conn.fetch(_NEXT, batch)
        for row in rows:
            await self.process(row)
        return len(rows)

    async def run(self, idle: float = 30.0) -> None:
        while True:
            try:
                taken = await self.step()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error("очередь голосовых: сбой обхода (%s)", type(exc).__name__)
                taken = 0
            if taken:
                continue
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=idle)
            except asyncio.TimeoutError:
                pass


def status_fields(transcriber: Any) -> dict[str, Any]:
    return {"voice_enabled": transcriber is not None,
            "voice_problem": getattr(transcriber, "problem", None),
            "voice_model": getattr(getattr(transcriber, "asr", None), "model", None)}
