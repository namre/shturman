"""Файлы вложений из загруженной выгрузки Telegram Desktop.

Выгрузка (архив папки) содержит сами файлы: голосовые, фото, документы. Сессии аккаунта у
владельца может не быть, тогда скачать их больше неоткуда. Импорт кладёт нужные файлы сюда —
каталог `media-files` в каталоге данных, права 600 — и записывает путь в messages.media_file.
Файл лежит только до разбора: очередь голосовых или вложений, закончив с сообщением
(готово, пропущено, не получилось), удаляет его функцией `drop`. Что осталось — ничьи файлы,
файлы удалённых сообщений и исключённых чатов, файлы старше 45 дней — убирает `sweep`.

Имена файлов — случайные: из выгрузки не берётся ничего, кроме содержимого.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO

import asyncpg

DIRNAME = "media-files"
CHUNK = 1024 * 1024


class TooBig(Exception):
    """Файл больше допустимого."""


def _root(data_dir: Path) -> Path:
    return Path(data_dir) / DIRNAME


def _resolve(data_dir: Path, rel: str) -> Path:
    """Путь из messages.media_file — только внутри своего каталога и только своего вида."""
    if not isinstance(rel, str) or "/" not in rel:
        raise ValueError("неверный путь файла вложения")
    head, name = rel.split("/", 1)
    if head != DIRNAME or not name or "/" in name or name.startswith("."):
        raise ValueError("неверный путь файла вложения")
    return _root(data_dir) / name


def store(data_dir: Path, source: BinaryIO, *, max_bytes: int, suffix: str = "") -> str:
    """Копирует поток в новый файл и возвращает путь для messages.media_file.
    Больше max_bytes — TooBig, файл не остаётся."""
    root = _root(data_dir)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    clean = "".join(ch for ch in suffix.lower() if ch.isalnum())[:8]
    name = secrets.token_hex(16) + (f".{clean}" if clean else "")
    path = root / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    done = False
    try:
        size = 0
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = source.read(CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise TooBig()
                out.write(chunk)
        done = True
    finally:
        if not done:
            path.unlink(missing_ok=True)
    return f"{DIRNAME}/{name}"


def read(data_dir: Path, rel: str, *, max_bytes: int) -> bytes:
    path = _resolve(data_dir, rel)
    with open(path, "rb") as fp:
        data = fp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise TooBig()
    return data


def remove(data_dir: Path, rel: str | None) -> None:
    if not rel:
        return
    try:
        _resolve(data_dir, rel).unlink(missing_ok=True)
    except ValueError:
        pass


async def drop(conn: asyncpg.Connection, data_dir: Path, message_id: int) -> bool:
    """Удаляет файл вложения сообщения, если он больше не нужен ни голосовым, ни вложениям."""
    rel = await conn.fetchval(
        """UPDATE messages m SET media_file = NULL
           FROM (SELECT id, media_file FROM messages WHERE id = $1 FOR UPDATE) old
           WHERE m.id = old.id AND old.media_file IS NOT NULL
             AND COALESCE(m.transcript_state, 'done') <> 'pending'
             AND COALESCE(m.media_state, 'done') NOT IN ('pending', 'asking')
           RETURNING old.media_file""", message_id)
    remove(data_dir, rel)
    return rel is not None


# Уборка (её зовёт janitor импорта, ingest_api.lifespan):
ORPHAN_AGE = timedelta(hours=1)     # файл без ссылки из архива: копирование оборвалось или не понадобился
SETTLED_AGE = timedelta(days=1)     # сообщение уже не ждёт ни расшифровки, ни разбора
HARD_AGE = timedelta(days=45)       # дольше файл не лежит ни при каком состоянии очереди


async def sweep(conn: asyncpg.Connection, data_dir: Path, now: datetime | None = None) -> dict[str, int]:
    """Удаляет файлы вложений, которые больше не нужны. Возвращает счётчики для журнала.

    * Файл, на который не ссылается ни одно сообщение, старше часа — удаляется (час — запас на
      время между копированием файла и записью пути).
    * Ссылка сообщения, которое уже не ждёт ни расшифровки, ни разбора, на файл старше суток —
      снимается вместе с файлом (`drop`). Сутки — запас, пока очередь ещё не поставила сообщение.
    * Сообщение удалено или его чат исключён — файл удаляется сразу.
    * Файл старше 45 дней удаляется при любом состоянии очереди.
    * Ссылка на файл, которого нет, снимается.
    """
    now = now or datetime.now(timezone.utc)
    root = _root(data_dir)
    out = {"orphans": 0, "settled": 0, "gone": 0, "expired": 0, "missing": 0}
    # Сначала ссылки, потом каталог: файл, записанный между ними, попадёт в каталог без ссылки
    # и как «ничей» удаляется только через час — к тому времени ссылка на него уже видна.
    rows = await conn.fetch(
        """SELECT m.id, m.media_file, m.deleted_at IS NOT NULL OR c.excluded AS gone,
                  COALESCE(m.transcript_state, 'done') <> 'pending'
                  AND COALESCE(m.media_state, 'done') NOT IN ('pending', 'asking') AS settled
           FROM messages m JOIN chats c ON c.id = m.chat_id
           WHERE m.media_file IS NOT NULL""")
    referenced = {r["media_file"] for r in rows}
    on_disk: dict[str, datetime] = {}
    if root.is_dir():
        for path in root.iterdir():
            if path.name.startswith(".") or not path.is_file():
                continue
            try:
                on_disk[f"{DIRNAME}/{path.name}"] = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            except FileNotFoundError:
                continue

    async def unlink_ref(message_id: int, rel: str) -> None:
        await conn.execute("UPDATE messages SET media_file = NULL WHERE id = $1 AND media_file = $2",
                           message_id, rel)
        remove(data_dir, rel)

    for r in rows:
        rel, born = r["media_file"], on_disk.get(r["media_file"])
        if born is None:
            await unlink_ref(r["id"], rel)
            out["missing"] += 1
        elif r["gone"]:
            await unlink_ref(r["id"], rel)
            out["gone"] += 1
        elif now - born > HARD_AGE:
            await unlink_ref(r["id"], rel)
            out["expired"] += 1
        elif r["settled"] and now - born > SETTLED_AGE:
            if await drop(conn, data_dir, r["id"]):
                out["settled"] += 1
    for rel, born in on_disk.items():
        if rel not in referenced and now - born > ORPHAN_AGE:
            # Ссылка могла появиться после выборки: перепроверяем перед удалением.
            if await conn.fetchval("SELECT 1 FROM messages WHERE media_file = $1 LIMIT 1", rel) is None:
                remove(data_dir, rel)
                out["orphans"] += 1
    return out
