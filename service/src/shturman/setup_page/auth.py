"""Вход на страницу настройки: одноразовая ссылка, код от бота согласований, сессии.

Здесь только правила и записи в базу. Отправку кода в Telegram передаёт снаружи вызывающий
(`send`), поэтому модуль проверяется без сети.

Ссылка входа
  * случайная, 256 бит; в базе — только её SHA-256;
  * живёт 30 минут и срабатывает один раз; новая отменяет прежнюю;
  * создаётся командой `shturman setup-link` прямой записью в базу. Маршрута для неё нет и быть
    не должно: токен внутреннего API есть у ассистента в Hermes, строка подключения к базе —
    только в контейнере сервиса;
  * вход по ссылке завершает все прежние сессии и снимает блокировку входа по коду: владелец
    доказал, что сервер его.
  Перебирать 256 бит бессмысленно, поэтому неверные ссылки вход не закрывают — иначе любой
  посторонний мог бы не пускать владельца. Ограничено только число одновременно «думающих»
  отказов (см. `service.py`).

Код от бота согласований — когда владелец к боту привязан
  * 8 цифр, 5 минут; в базе — HMAC кода с ключом из каталога данных (ключа в базе нет);
  * действующий код новым не заменяется: посторонний, запрашивая коды, не обесценит тот, что
    владелец сейчас вводит, и владельцу приходит не больше одного сообщения в 5 минут;
  * после неудачной отправки — новая попытка не раньше чем через минуту;
  * 5 неверных кодов подряд — вход по коду закрывается на 1, 5, 15, 60 минут; счётчик новым
    кодом не обнуляется. Счётчик один на всех, а не «на адрес»: адресу клиента за прокси
    верить нельзя. Цена — посторонний может на время закрыть вход по коду; выход — новая ссылка.
  Правила те же, что у входа в дашборд по коду от бота Hermes (plugins/shturman, auth.py).

Сессия
  * случайный идентификатор, 256 бит; в базе — его SHA-256, сам он — только в cookie;
  * 12 часов без обращений — и вход заново; не дольше 7 дней от входа в любом случае;
  * «выйти» завершает эту сессию, «выйти везде» и команда `shturman setup-logout-all` — все.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

import asyncpg

LINK_TTL = 30 * 60
SESSION_IDLE = 12 * 3600
SESSION_MAX = 7 * 24 * 3600
CODE_TTL = 5 * 60
CODE_DIGITS = 8
CODE_RETRY = 60
CODE_MAX_ATTEMPTS = 5
LOCK_STEPS = (60, 5 * 60, 15 * 60, 60 * 60)
COOKIE = "shturman_setup"
CODE_STATE = "login_code"

_TOKEN = re.compile(r"^[A-Za-z0-9_-]{40,64}$")

# Часы для состояния кода входа. Тесты подменяют, чтобы не ждать.
now: Callable[[], float] = time.time


def new_token() -> str:
    return secrets.token_urlsafe(32)          # 256 бит, 43 знака


def well_formed(value: Any) -> bool:
    return isinstance(value, str) and _TOKEN.match(value) is not None


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def csrf_token(session_token: str) -> str:
    """Значение для заголовка X-Shturman-Csrf. Выводится из идентификатора сессии, поэтому
    отдельно не хранится; сам идентификатор по нему не восстановить."""
    return hashlib.sha256(b"shturman-setup-csrf:" + session_token.encode("utf-8")).hexdigest()


# --- ссылка входа ---

async def create_link(conn: asyncpg.Connection, *, ttl: int = LINK_TTL) -> tuple[str, datetime]:
    """Создаёт значение одноразовой ссылки и отменяет все прежние."""
    token = new_token()
    async with conn.transaction():
        await conn.execute("DELETE FROM setup_links")
        expires_at = await conn.fetchval(
            """INSERT INTO setup_links (token_hash, expires_at)
               VALUES ($1, now() + make_interval(secs => $2)) RETURNING expires_at""",
            token_hash(token), float(ttl))
    return token, expires_at


async def redeem_link(conn: asyncpg.Connection, token: Any) -> bool:
    """Гасит ссылку. Одна запись в базе: ссылка срабатывает ровно один раз."""
    if not well_formed(token):
        return False
    async with conn.transaction():
        used = await conn.fetchval(
            """UPDATE setup_links SET used_at = now()
               WHERE token_hash = $1 AND used_at IS NULL AND expires_at > now() RETURNING id""",
            token_hash(token))
        if used is None:
            return False
        # Прежние сессии гаснут: если одну из них увели, новая ссылка с сервера её отзывает.
        await conn.execute("UPDATE setup_sessions SET revoked_at = now() WHERE revoked_at IS NULL")
        await conn.execute("DELETE FROM setup_state WHERE key = $1", CODE_STATE)
    return True


# --- сессии ---

@dataclass(frozen=True)
class Session:
    id: int
    via: str
    csrf: str
    created_at: datetime
    expires_at: datetime


async def create_session(conn: asyncpg.Connection, via: str) -> tuple[str, Session]:
    token = new_token()
    row = await conn.fetchrow(
        """INSERT INTO setup_sessions (token_hash, via, expires_at)
           VALUES ($1, $2, now() + make_interval(secs => $3)) RETURNING id, via, created_at, expires_at""",
        token_hash(token), via, float(SESSION_MAX))
    return token, Session(row["id"], row["via"], csrf_token(token), row["created_at"], row["expires_at"])


async def find_session(conn: asyncpg.Connection, token: Any) -> Session | None:
    """Действующая сессия по значению из cookie; заодно отмечает обращение."""
    if not well_formed(token):
        return None
    row = await conn.fetchrow(
        """UPDATE setup_sessions SET last_seen_at = now()
           WHERE token_hash = $1 AND revoked_at IS NULL AND expires_at > now()
             AND last_seen_at > now() - make_interval(secs => $2)
           RETURNING id, via, created_at, expires_at""",
        token_hash(token), float(SESSION_IDLE))
    if row is None:
        return None
    return Session(row["id"], row["via"], csrf_token(token), row["created_at"], row["expires_at"])


async def revoke_session(conn: asyncpg.Connection, session_id: int) -> None:
    await conn.execute("UPDATE setup_sessions SET revoked_at = now() WHERE id = $1 AND revoked_at IS NULL",
                       session_id)


async def revoke_all(conn: asyncpg.Connection) -> int:
    """Завершает все сессии и отменяет невостребованную ссылку входа. Возвращает число сессий."""
    async with conn.transaction():
        done = await conn.execute("UPDATE setup_sessions SET revoked_at = now() WHERE revoked_at IS NULL")
        await conn.execute("DELETE FROM setup_links WHERE used_at IS NULL")
    return int(done.split()[-1])


async def cleanup(conn: asyncpg.Connection) -> None:
    """Убирает то, что уже ничего не значит: отработавшие ссылки и давно закрытые сессии."""
    await conn.execute("DELETE FROM setup_links WHERE expires_at < now() - interval '1 day'")
    await conn.execute(
        """DELETE FROM setup_sessions
           WHERE expires_at < now() - interval '1 day' OR revoked_at < now() - interval '1 day'
              OR last_seen_at < now() - make_interval(secs => $1) - interval '1 day'""", float(SESSION_IDLE))


# --- код от бота согласований ---

def _digest(code: str, key: bytes) -> str:
    return hmac.new(key, code.encode("ascii"), hashlib.sha256).hexdigest()


async def _locked_state(conn: asyncpg.Connection) -> dict[str, Any]:
    """Состояние входа по коду под блокировкой строки. Вызывать внутри транзакции."""
    await conn.execute(
        "INSERT INTO setup_state (key, value) VALUES ($1, '{}'::jsonb) ON CONFLICT (key) DO NOTHING", CODE_STATE)
    raw = await conn.fetchval("SELECT value FROM setup_state WHERE key = $1 FOR UPDATE", CODE_STATE)
    value = json.loads(raw) if isinstance(raw, str) else raw
    return dict(value) if isinstance(value, dict) else {}


async def _save_state(conn: asyncpg.Connection, data: dict[str, Any]) -> None:
    await conn.execute("UPDATE setup_state SET value = $2::jsonb, updated_at = now() WHERE key = $1",
                       CODE_STATE, json.dumps(data))


Send = Callable[[str], Awaitable[None]]


async def request_code(conn: asyncpg.Connection, key: bytes, send: Send | None) -> str:
    """Готовит код входа и отправляет его владельцу через `send(код)`.

    Возвращает: sent — отправлен новый код; reused — прежний ещё действует, новый не создаётся;
    no_owner — отправить некому (бота нет или владелец к нему не привязан); locked — вход по
    коду временно закрыт; wait — недавняя отправка не удалась, повторить можно через минуту;
    send_failed — Telegram сообщение не принял.
    """
    if send is None:
        return "no_owner"
    moment = int(now())
    async with conn.transaction():
        data = await _locked_state(conn)
        if int(data.get("lock_until", 0)) > moment:
            return "locked"
        if data.get("digest") and int(data.get("expires_at", 0)) > moment:
            return "reused"
        if moment - int(data.get("attempt_at", 0)) < CODE_RETRY and not data.get("digest"):
            return "wait"
        data["attempt_at"] = moment
        data.pop("digest", None)
        code = "".join(secrets.choice("0123456789") for _ in range(CODE_DIGITS))
        try:
            await send(code)
        except Exception:  # noqa: BLE001 — причина не важна и в журнал не идёт: в ней может быть код
            await _save_state(conn, data)
            return "send_failed"
        data["digest"] = _digest(code, key)
        data["expires_at"] = moment + CODE_TTL
        # Счётчик неверных попыток новым кодом не обнуляется: иначе перебор шёл бы по четыре
        # попытки на каждый свежий код без единой блокировки.
        await _save_state(conn, data)
    return "sent"


async def verify_code(conn: asyncpg.Connection, key: bytes, code: Any) -> str:
    """ok | wrong | expired | locked | locked_now | none (код не запрашивали).

    locked_now — эта попытка стала пятой неверной: вход закрыт, владельцу стоит сообщить."""
    digits = "".join(ch for ch in str(code) if ch in "0123456789")[:CODE_DIGITS * 2]
    moment = int(now())
    async with conn.transaction():
        data = await _locked_state(conn)
        if int(data.get("lock_until", 0)) > moment:
            return "locked"
        if not data.get("digest"):
            return "none"
        if int(data.get("expires_at", 0)) <= moment:
            data.pop("digest", None)
            await _save_state(conn, data)
            return "expired"
        if len(digits) == CODE_DIGITS and hmac.compare_digest(_digest(digits, key), str(data["digest"])):
            await _save_state(conn, {})          # код одноразовый; счётчики сброшены
            return "ok"
        attempts = int(data.get("attempts", 0)) + 1
        data["attempts"] = attempts
        if attempts < CODE_MAX_ATTEMPTS:
            await _save_state(conn, data)
            return "wrong"
        level = int(data.get("lock_level", 0))
        data["lock_until"] = moment + LOCK_STEPS[min(level, len(LOCK_STEPS) - 1)]
        data["lock_level"] = level + 1
        data["attempts"] = 0
        data.pop("digest", None)
        await _save_state(conn, data)
    return "locked_now"
