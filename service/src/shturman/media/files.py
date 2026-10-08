"""Файлы вложений из загруженной выгрузки Telegram Desktop.

Выгрузка (архив папки) содержит сами файлы: голосовые, фото, документы. Сессии аккаунта у
владельца может не быть, тогда скачать их больше неоткуда. Импорт кладёт нужные файлы сюда —
каталог `media-files` в каталоге данных, права 600 — и записывает путь в messages.media_file.
Файл лежит только до разбора: очередь голосовых или вложений, закончив с сообщением
(готово, пропущено, не получилось), удаляет его функцией `drop`.

Имена файлов — случайные: из выгрузки не берётся ничего, кроме содержимого.
"""

from __future__ import annotations

import os
import secrets
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


async def drop(conn: asyncpg.Connection, data_dir: Path, message_id: int) -> None:
    """Удаляет файл вложения сообщения, если он больше не нужен ни голосовым, ни вложениям."""
    rel = await conn.fetchval(
        """UPDATE messages m SET media_file = NULL
           FROM (SELECT id, media_file FROM messages WHERE id = $1 FOR UPDATE) old
           WHERE m.id = old.id AND old.media_file IS NOT NULL
             AND COALESCE(m.transcript_state, 'done') <> 'pending'
             AND COALESCE(m.media_state, 'done') NOT IN ('pending', 'asking')
           RETURNING old.media_file""", message_id)
    remove(data_dir, rel)
