"""Привязка владельца через бота согласований.

Со своим ботом сервис не принимает владельца от плагина (`/api/owner` отвечает 403): иначе
ассистент в Hermes мог бы назначить владельцем кого угодно. Владельцем становится тот, кто
открыл одноразовую ссылку из команды оператора `shturman bot-bind` и нажал «Запустить» в боте.
И первая привязка, и смена владельца идут только так.

Код привязки:
  * случайный, 192 бита; в базе лежит только его SHA-256;
  * живёт 15 минут и срабатывает один раз;
  * новый код отменяет прежний;
  * создаётся прямой записью в базу, а не через внутренний API: токен API есть у ассистента
    в Hermes, строка подключения к базе — только в контейнере сервиса.

Отметка «владелец привязан через бота» (`executor_state.owner`). Запись о владельце в общей
таблице настроек могла остаться с того времени, когда владельца сообщал плагин, и тогда её мог
подменить ассистент. Поэтому бот согласований считает владельцем только того, кто прошёл
привязку в этом самом боте: до неё карточки не отправляются и кнопки не действуют.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from collections import deque
from datetime import datetime
from typing import Any, Callable

import asyncpg

from .. import bridge

CODE_TTL = 15 * 60            # секунд живёт код привязки
WRONG_LIMIT = 5               # столько неверных кодов за окно — и коды перестают приниматься
WRONG_WINDOW = 10 * 60        # секунд
LOCKOUT = 10 * 60             # секунд бот не принимает коды после череды неверных
_CODE = re.compile(r"^[A-Za-z0-9_-]{24,64}$")


def new_code() -> str:
    return secrets.token_urlsafe(24)      # 192 бита, 32 знака из набора, разрешённого в ссылке t.me


def code_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def well_formed(code: Any) -> bool:
    return isinstance(code, str) and _CODE.match(code) is not None


def deep_link(username: str, code: str) -> str:
    return f"https://t.me/{username}?start={code}"


# --- состояние исполнителя ---

async def get_state(conn: asyncpg.Connection, key: str) -> dict[str, Any] | None:
    raw = await conn.fetchval("SELECT value FROM executor_state WHERE key = $1", key)
    if raw is None:
        return None
    value = json.loads(raw) if isinstance(raw, str) else raw
    return value if isinstance(value, dict) else None


async def put_state(conn: asyncpg.Connection, key: str, value: dict[str, Any]) -> None:
    await conn.execute(
        """INSERT INTO executor_state (key, value) VALUES ($1, $2::jsonb)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
        key, json.dumps(value, ensure_ascii=False),
    )


# --- коды ---

async def create_code(conn: asyncpg.Connection, *, ttl: int = CODE_TTL) -> tuple[str, datetime]:
    """Создаёт код привязки и отменяет все прежние. Возвращает код и время, до которого он действует."""
    code = new_code()
    async with conn.transaction():
        await conn.execute("DELETE FROM executor_bind_codes")
        expires_at = await conn.fetchval(
            """INSERT INTO executor_bind_codes (code_hash, expires_at)
               VALUES ($1, now() + make_interval(secs => $2)) RETURNING expires_at""",
            code_hash(code), float(ttl),
        )
    return code, expires_at


async def revoke_code(conn: asyncpg.Connection, code: str) -> bool:
    """Гасит код, который оказался не там, где должен (например, его прислали в группу)."""
    done = await conn.execute(
        "UPDATE executor_bind_codes SET used_at = now() WHERE code_hash = $1 AND used_at IS NULL",
        code_hash(code))
    return done.endswith(" 1")


async def redeem(conn: asyncpg.Connection, code: str, *, user_id: int, chat_id: int, bot_id: int) -> bool:
    """Гасит код и привязывает владельца. Одна транзакция: код срабатывает ровно один раз."""
    async with conn.transaction():
        used = await conn.fetchval(
            """UPDATE executor_bind_codes SET used_at = now()
               WHERE code_hash = $1 AND used_at IS NULL AND expires_at > now() RETURNING id""",
            code_hash(code))
        if used is None:
            return False
        await bridge.set_owner(conn, int(user_id), int(chat_id))
        await put_state(conn, "owner", {"user_id": int(user_id), "bot_id": int(bot_id)})
    return True


async def bound_owner(conn: asyncpg.Connection, bot_id: int | None) -> dict[str, int] | None:
    """Владелец, привязанный через этого бота: {user_id, chat_id}. Иначе None."""
    if bot_id is None:
        return None
    owner = await bridge.get_owner(conn)
    if not owner:
        return None
    mark = await get_state(conn, "owner")
    try:
        same = (mark is not None and int(mark["user_id"]) == int(owner["user_id"])
                and int(mark["bot_id"]) == int(bot_id))
        return {"user_id": int(owner["user_id"]), "chat_id": int(owner["chat_id"])} if same else None
    except (KeyError, TypeError, ValueError):
        return None


# --- защита от перебора ---

class Flood:
    """Счётчик неверных кодов. Общий на всех отправителей: перебирать могут с разных аккаунтов.

    Подобрать код в 192 бита перебором нельзя и без этого; пауза нужна, чтобы бот не тратил
    на чужие попытки ни запросов к базе, ни места в журнале.
    """

    def __init__(self, *, limit: int = WRONG_LIMIT, window: float = WRONG_WINDOW, lockout: float = LOCKOUT,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.limit, self.window, self.lockout, self._clock = limit, window, lockout, clock
        self._wrong: deque[float] = deque()
        self._until = 0.0

    def locked(self) -> bool:
        return self._clock() < self._until

    def wrong(self) -> None:
        now = self._clock()
        self._wrong.append(now)
        while self._wrong and now - self._wrong[0] > self.window:
            self._wrong.popleft()
        if len(self._wrong) >= self.limit:
            self._until = now + self.lockout
            self._wrong.clear()
