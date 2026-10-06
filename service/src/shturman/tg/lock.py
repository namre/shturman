"""Блокировка «одна сессия — один процесс».

Два подключения с одним ключом авторизации Telegram считает кражей ключа и отзывает его
(AUTH_KEY_DUPLICATED) — после этого сессия мертва, повтор не поможет. Поэтому до подключения
берутся две блокировки, и отказ любой из них означает «не подключаться»:

  * `flock` на файле рядом с файлом сессии — от второго процесса на том же сервере;
  * рекомендательная блокировка Postgres (`pg_advisory_lock`) — от второго экземпляра сервиса
    с той же базой, но другим каталогом данных.

Обе снимаются сами, если процесс умер.

# Основано на chigwell/telegram-mcp (Apache-2.0), telegram_mcp/singleton.py@c4f9b23
# (блокировка файла без ожидания; повтор подключения после AuthKeyDuplicatedError оттуда не взят)
"""

from __future__ import annotations

import fcntl
import hashlib
import os
from pathlib import Path
from typing import Callable

import asyncpg


class SessionLocked(RuntimeError):
    """Сессию уже держит другой процесс или экземпляр сервиса."""


def advisory_key(name: str) -> int:
    """Ключ блокировки Postgres (знаковое 64-битное число) по имени."""
    digest = hashlib.sha256(f"shturman.tg:{name}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


class SessionLock:
    """Блокировка одной сессии. Держится, пока сессия подключена."""

    def __init__(self, session_file: Path, dsn: str, *, on_lost: Callable[[], None] | None = None) -> None:
        self._lock_file = Path(str(session_file) + ".lock")
        self._dsn = dsn
        self._on_lost = on_lost
        self._fd: int | None = None
        self._conn: asyncpg.Connection | None = None
        self._names: list[str] = []
        self._releasing = False

    @property
    def held(self) -> bool:
        return self._fd is not None

    async def acquire(self, name: str) -> None:
        """Берёт блокировку файла и блокировку базы с именем `name`. Не ждёт: занято — отказ."""
        if self._fd is not None:
            raise RuntimeError("блокировка уже взята")
        self._lock_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self._lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise SessionLocked("файл сессии уже занят другим процессом") from None
        self._fd = fd
        try:
            self._conn = await asyncpg.connect(self._dsn)
            self._conn.add_termination_listener(self._terminated)
            await self.add(name)
        except BaseException:
            await self.release()
            raise

    async def add(self, name: str) -> None:
        """Берёт ещё одну блокировку базы — например, по аккаунту, когда он стал известен."""
        if self._conn is None:
            raise RuntimeError("блокировка не взята")
        if name in self._names:
            return
        if not await self._conn.fetchval("SELECT pg_try_advisory_lock($1)", advisory_key(name)):
            raise SessionLocked("сессию этого аккаунта уже держит другой экземпляр сервиса")
        self._names.append(name)

    def _terminated(self, _conn: asyncpg.Connection) -> None:
        # Соединение с базой оборвалось — блокировка базы потеряна вместе с ним.
        if not self._releasing and self._on_lost is not None:
            self._on_lost()

    async def release(self) -> None:
        self._releasing = True
        conn, self._conn = self._conn, None
        self._names.clear()
        if conn is not None:
            try:
                await conn.close(timeout=5)
            except Exception:
                conn.terminate()
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        self._releasing = False
