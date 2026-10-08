"""Файлы вложений из архива выгрузки Telegram Desktop — на разбор.

Импорт архива (export_archive.py) по ходу записи сообщений собирает кандидатов:
  * голосовые и «кружки» — если включена расшифровка (SHTURMAN_ASR=on) и сообщение не старше
    `asr_days` дней;
  * фото и документы — если владелец включил разбор вложений (setup_state 'media') и
    сообщение не старше `media_days` дней.
После записи сообщений файл каждого кандидата копируется из архива в каталог media-files
(`files.store`, случайное имя, права 600), а путь к нему пишется в messages.media_file. Очередь
голосовых или вложений берёт файл оттуда, а не из Telegram, и, закончив, удаляет его.

Файл не берётся, если его нет в архиве, путь в result.json негоден (абсолютный, с `..`), член
архива зашифрован, сжат неподдерживаемым способом или подозрительно сильно, больше предела
(`asr_max_bytes` для голосовых, `media_max_bytes` для фото и документов), или сообщению файл
уже не нужен (расшифровано, разобрано, удалено). Кончилось место на диске — копирование
останавливается, а записанные сообщения остаются.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import zipfile
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable

import asyncpg

from . import MEDIA_TYPES, enabled
from . import files
from ..export_archive import ExportArchive, readable, suspicious
from ..records import MessageRecord
from ..voice import VOICE_TYPES

logger = logging.getLogger("shturman.media")

MAX_CANDIDATES = 100_000      # больше файлов за один импорт не берём: остальные — через Telegram

# Путь к файлу пишется, только если сообщению файл ещё нужен. Голосовое, снятое с очереди из-за
# того, что скачать было нечем (no_source), возвращается в очередь: теперь файл есть. Ожидающее
# голосовое берётся сразу, без паузы до следующей попытки.
_ATTACH = """
UPDATE messages SET
    media_file = $3,
    transcript_state = CASE WHEN transcript_state = 'skipped' THEN 'pending' ELSE transcript_state END,
    transcript_error = CASE WHEN transcript_state = 'skipped' THEN NULL ELSE transcript_error END,
    transcript_attempts = CASE WHEN transcript_state = 'skipped' THEN 0 ELSE transcript_attempts END,
    transcript_at = CASE WHEN transcript_state IN ('pending', 'skipped') THEN NULL ELSE transcript_at END
WHERE chat_id = $1 AND tg_message_id = $2 AND media_file IS NULL AND deleted_at IS NULL
  AND (transcript_state IS NULL OR transcript_state = 'pending'
       OR (transcript_state = 'skipped' AND transcript_error = 'no_source' AND media_type = ANY($4::text[])))
  AND (media_state IS NULL OR media_state IN ('pending'))
"""


@dataclass
class Rules:
    """Что брать из архива: с какого времени (None — этот вид не берётся) и до какого размера."""

    voice_since: datetime | None = None
    voice_max: int = 20 * 1024 * 1024
    files_since: datetime | None = None
    files_max: int = 20 * 1024 * 1024

    @property
    def any(self) -> bool:
        return self.voice_since is not None or self.files_since is not None


async def rules(conn: asyncpg.Connection, config: Any, now: datetime | None = None) -> Rules:
    now = now or datetime.now(timezone.utc)
    voice = bool(getattr(config, "asr", False))
    media_on = await enabled(conn)
    return Rules(
        voice_since=now - timedelta(days=config.asr_days) if voice else None,
        voice_max=config.asr_max_bytes,
        files_since=now - timedelta(days=config.media_days) if media_on else None,
        files_max=config.media_max_bytes,
    )


@dataclass
class Candidate:
    chat_id: int
    tg_message_id: int
    media_type: str
    media_path: str
    sent_at: datetime
    media_size: int | None


class _Checked:
    """Поток члена архива, который останавливается по просьбе (удаление загрузки, остановка сервиса)."""

    def __init__(self, fp: BinaryIO, check: Callable[[], None]) -> None:
        self._fp, self._check = fp, check

    def read(self, size: int = -1) -> bytes:
        self._check()
        return self._fp.read(size)


def _suffix(name: str) -> str:
    return os.path.splitext(name.replace("\\", "/").rsplit("/", 1)[-1])[1].lstrip(".")


class Attachments:
    """Сборщик кандидатов и копирование файлов из архива. Передаётся импорту (`importer`)."""

    def __init__(self, archive: ExportArchive, data_dir: Path, rules: Rules, *,
                 check: Callable[[], None] = lambda: None) -> None:
        self.archive, self.data_dir, self.rules = archive, Path(data_dir), rules
        self.check = check
        self.items: dict[tuple[int, int], Candidate] = {}

    def _limit(self, media_type: str) -> int:
        return self.rules.voice_max if media_type in VOICE_TYPES else self.rules.files_max

    def add(self, chat_id: int, record: MessageRecord) -> None:
        """Сообщение записывается в архив: если у него нужный файл — запомнить."""
        mt, path = record.media_type, record.media_path
        if not path or record.kind != "message" or len(self.items) >= MAX_CANDIDATES:
            return
        if mt in VOICE_TYPES:
            since = self.rules.voice_since
        elif mt in MEDIA_TYPES:
            since = self.rules.files_since
        else:
            return
        if since is None or record.sent_at < since:
            return
        if record.media_size is not None and record.media_size > self._limit(mt):
            return
        self.items[(chat_id, record.tg_message_id)] = Candidate(
            chat_id, record.tg_message_id, mt, path, record.sent_at, record.media_size)

    def _copy(self, cand: Candidate) -> str | None:
        """Копирует файл кандидата из архива. None — файл не берётся. В отдельном потоке."""
        info = self.archive.media(cand.media_path)
        limit = self._limit(cand.media_type)
        if info is None or not readable(info) or suspicious(info) or info.file_size > limit:
            return None
        try:
            with self.archive.open(info) as src:
                return files.store(self.data_dir, _Checked(src, self.check), max_bytes=limit,
                                   suffix=_suffix(info.filename))
        except files.TooBig:
            return None
        except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError):
            return None    # член архива повреждён (контрольная сумма) или не читается

    async def finish(self, conn: asyncpg.Connection, stats: Any) -> None:
        """Сообщения записаны — копирует файлы и пишет пути. Счётчики — в stats."""
        todo = sorted(self.items.values(), key=lambda c: c.sent_at, reverse=True)
        self.items = {}
        for cand in todo:
            self.check()
            try:
                rel = await asyncio.to_thread(self._copy, cand)
            except OSError as exc:
                if exc.errno == errno.ENOSPC:
                    stats.media_no_space = True
                    stats.media_skipped += 1
                    logger.warning("импорт: на диске кончилось место — файлы из архива больше не копируются")
                    break
                logger.warning("импорт: файл вложения не скопирован (%s)", type(exc).__name__)
                stats.media_skipped += 1
                continue
            if rel is None:
                stats.media_skipped += 1
                continue
            try:
                done = await conn.execute(_ATTACH, cand.chat_id, cand.tg_message_id, rel, list(VOICE_TYPES))
            except BaseException:
                files.remove(self.data_dir, rel)
                raise
            if done.split()[-1] == "0":
                files.remove(self.data_dir, rel)      # сообщению файл не нужен
                stats.media_skipped += 1
            else:
                stats.media_files += 1
