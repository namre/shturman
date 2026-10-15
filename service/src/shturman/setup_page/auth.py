"""Вход на страницу настройки: одноразовая ссылка, код из бота согласований, сессии.

Здесь только правила и записи в базу. Отправку кода в Telegram передаёт снаружи вызывающий
(`send`), поэтому модуль проверяется без сети.

Ссылка входа
  * случайная, 256 бит; в базе — только её SHA-256;
  * живёт 30 минут и срабатывает один раз; новая отменяет прежнюю;
  * создаётся командой `shturman setup-link` (на сервере — `./ops/setup-link.sh`) прямой записью в базу. Маршрута для неё нет и быть
    не должно: токен внутреннего API есть у ассистента в Hermes, строка подключения к базе —
    только в контейнере сервиса;
  * вход по ссылке завершает все прежние сессии и снимает блокировку входа по коду: владелец
    доказал, что сервер его.
  Перебирать 256 бит бессмысленно, поэтому неверные ссылки вход не закрывают — иначе любой
  посторонний мог бы не пускать владельца. Ограничено только число одновременно «думающих»
  отказов (см. `service.py`).

Код из бота согласований (Р-88) — когда бот настроен, владелец привязан и вход по коду включён
  * 6 цифр, 5 минут, срабатывает один раз; в базе — HMAC кода с ключом из каталога данных (ключа в
    базе нет); сравнение — за постоянное время;
  * код привязан к браузеру, который его запросил: случайный идентификатор попытки (256 бит) лежит у
    браузера в cookie `__Secure-shturman-code` (`HttpOnly; Secure; SameSite=Strict`, путь — только
    `/shturman-setup/api/login/code`), в базе — его SHA-256. Тот же код в другом браузере не работает.
    Это единственная cookie страницы: сессию по-прежнему держит заголовок (ниже), а `shield.py`
    пропускает только эту cookie и только на этом пути. Две cookie с этим именем — отказ: соседний
    порт того же имени мог подложить свою;
  * не больше `REQUEST_LIMIT` (3) запросов кода за 15 минут — на всех, а не на браузер: адресу
    клиента за прокси верить нельзя. Новый запрос из того же браузера гасит его прежнюю попытку;
  * 5 неверных попыток на код — код погашен, и вход по коду на паузе: 1, 5, 15 минут, час, 6 часов,
    сутки; удачный вход начинает паузы заново. О паузе владельцу уходит одно сообщение за период;
  * запрос без привязанного владельца или при выключенном входе по коду заводит такую же попытку,
    но код никуда не уходит и не сработает: ответ снаружи тот же («одинаковый ответ»);
  * «Это не я» в сообщении с кодом гасит все ждущие попытки (`cancel_codes`), «Выйти на всех
    устройствах» там же — все сессии (`revoke_all`);
  * вход по ссылке и «выйти везде» тоже гасят ждущие попытки. Цена паузы — посторонний может на
    время закрыть вход по коду; выход — ссылка с сервера.

Сессия — без cookie
  * ключ сессии: случайный, 256 бит; в базе — его SHA-256. Сервер отдаёт ключ один раз, в ответе
    на вход; страница хранит его в `localStorage` своего origin и присылает в заголовке
    `X-Shturman-Session` с каждым запросом, в том числе с загрузкой выгрузки;
  * почему не cookie. Страница стоит на том же имени узла, что и дашборд Hermes, но на другом
    порту. Cookie по портам не разделяются: поставленная страницей cookie уходила бы и на порт
    дашборда — в Hermes, которым может управлять противник, — а скрипт дашборда мог бы подложить
    свою cookie на то же имя. `localStorage` разделяется по origin вместе с портом: дашборд его
    не видит. Заголовок браузер сам не приставляет ни к какому запросу — его ставит только
    скрипт страницы, поэтому подделка запроса с чужого сайта (CSRF) невозможна по построению, и
    отдельного значения против неё больше нет;
  * cookie страница не ставит и не читает: с любыми cookie, но без заголовка запрос — гость;
  * ключ не попадает ни в адрес, ни в журналы, ни в ответы (кроме единственного ответа на вход);
  * сессия живёт 7 дней от входа; «выйти» завершает её, «выйти везде» и команда
    `shturman setup-logout-all` (на сервере — `./ops/logout-all.sh`) — все;
  * вход по новой ссылке завершает все прежние сессии.
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
SESSION_MAX = 7 * 24 * 3600
SESSION_HEADER = "x-shturman-session"
CODE_TTL = 5 * 60
CODE_DIGITS = 6
CODE_MAX_ATTEMPTS = 5
REQUEST_WINDOW = 15 * 60        # секунд: окно, в котором считаются запросы кода
REQUEST_LIMIT = 3               # запросов кода за окно — на всех
LOCK_STEPS = (60, 5 * 60, 15 * 60, 60 * 60, 6 * 3600, 24 * 3600)
CODE_COOKIE = "__Secure-shturman-code"
CODE_PATH = "/shturman-setup/api/login/code"     # путь cookie: запрос кода и ввод кода, и только они
LOCK_NOTICE_EVERY = 15 * 60     # и не чаще раза в 15 минут, пока блокировки ещё короткие
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
        await cancel_codes(conn, "link")
    return True


# --- сессии ---

@dataclass(frozen=True)
class Session:
    id: int
    via: str
    created_at: datetime
    expires_at: datetime


async def create_session(conn: asyncpg.Connection, via: str) -> tuple[str, Session]:
    """Новая сессия: (ключ сессии, запись). Ключ существует только здесь и у страницы."""
    token = new_token()
    row = await conn.fetchrow(
        """INSERT INTO setup_sessions (token_hash, via, expires_at)
           VALUES ($1, $2, now() + make_interval(secs => $3)) RETURNING id, via, created_at, expires_at""",
        token_hash(token), via, float(SESSION_MAX))
    return token, Session(row["id"], row["via"], row["created_at"], row["expires_at"])


async def find_session(conn: asyncpg.Connection, token: Any) -> Session | None:
    """Действующая сессия по ключу из заголовка запроса; заодно отмечает обращение."""
    if not well_formed(token):
        return None
    row = await conn.fetchrow(
        """UPDATE setup_sessions SET last_seen_at = now()
           WHERE token_hash = $1 AND revoked_at IS NULL AND expires_at > now()
           RETURNING id, via, created_at, expires_at""",
        token_hash(token))
    if row is None:
        return None
    return Session(row["id"], row["via"], row["created_at"], row["expires_at"])


async def revoke_session(conn: asyncpg.Connection, session_id: int) -> None:
    await conn.execute("UPDATE setup_sessions SET revoked_at = now() WHERE id = $1 AND revoked_at IS NULL",
                       session_id)


async def revoke_all(conn: asyncpg.Connection) -> int:
    """Завершает все сессии и отменяет невостребованную ссылку входа. Возвращает число сессий
    страницы. Сессии Mini App бота согласований (`miniapp.py`) завершаются тоже: «выйти везде» —
    это все входы владельца мимо Hermes."""
    async with conn.transaction():
        done = await conn.execute("UPDATE setup_sessions SET revoked_at = now() WHERE revoked_at IS NULL")
        await conn.execute("DELETE FROM setup_links WHERE used_at IS NULL")
        await conn.execute("UPDATE miniapp_sessions SET revoked_at = now() WHERE revoked_at IS NULL")
        await cancel_codes(conn, "logout_all")
    return int(done.split()[-1])


async def cleanup(conn: asyncpg.Connection) -> None:
    """Убирает то, что уже ничего не значит: отработавшие ссылки и давно закрытые сессии."""
    await conn.execute("DELETE FROM setup_links WHERE expires_at < now() - interval '1 day'")
    await conn.execute(
        """DELETE FROM setup_sessions
           WHERE expires_at < now() - interval '1 day' OR revoked_at < now() - interval '1 day'""")
    # Попытки входа по коду хранятся неделю и сутки: столько показывает `./ops/doctor.sh`.
    await conn.execute("DELETE FROM setup_codes WHERE created_at < now() - interval '8 days'")


# --- код из бота согласований (Р-88) ---

def _digest(code: str, key: bytes) -> str:
    return hmac.new(key, code.encode("ascii"), hashlib.sha256).hexdigest()


def new_code() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(CODE_DIGITS))


async def _locked_state(conn: asyncpg.Connection) -> dict[str, Any]:
    """Общее состояние входа по коду (пауза после неверных попыток) под блокировкой строки.
    Вызывать внутри транзакции."""
    await conn.execute(
        "INSERT INTO setup_state (key, value) VALUES ($1, '{}'::jsonb) ON CONFLICT (key) DO NOTHING", CODE_STATE)
    raw = await conn.fetchval("SELECT value FROM setup_state WHERE key = $1 FOR UPDATE", CODE_STATE)
    value = json.loads(raw) if isinstance(raw, str) else raw
    return dict(value) if isinstance(value, dict) else {}


async def _save_state(conn: asyncpg.Connection, data: dict[str, Any]) -> None:
    await conn.execute("UPDATE setup_state SET value = $2::jsonb, updated_at = now() WHERE key = $1",
                       CODE_STATE, json.dumps(data))


@dataclass(frozen=True)
class CodeAttempt:
    """Итог запроса кода. `result`: sent — попытка заведена (код ушёл владельцу либо, если отправить
    некому, не ушёл — снаружи одно и то же); limited — запросов за 15 минут уже `REQUEST_LIMIT`;
    locked — вход по коду на паузе после неверных попыток. `attempt` — значение для cookie браузера."""
    result: str
    attempt: str | None = None
    code: str | None = None
    code_id: int | None = None
    nonce: str | None = None
    retry_after: int | None = None
    left: int | None = None


async def start_code(conn: asyncpg.Connection, key: bytes, *, previous: str | None = None) -> CodeAttempt:
    """Заводит попытку входа по коду: код и случайный идентификатор попытки для cookie браузера.

    Отправку делает вызывающий (`deliver_code` после неё): попытка без доставки не срабатывает.
    previous — идентификатор прежней попытки этого браузера из его cookie: она снимается."""
    moment = int(now())
    async with conn.transaction():
        data = await _locked_state(conn)
        if int(data.get("lock_until", 0)) > moment:
            return CodeAttempt("locked", retry_after=int(data["lock_until"]) - moment)
        recent = await conn.fetch(
            """SELECT created_at FROM setup_codes WHERE created_at > now() - make_interval(secs => $1)
               ORDER BY created_at""", float(REQUEST_WINDOW))
        if len(recent) >= REQUEST_LIMIT:
            wait = await conn.fetchval(
                "SELECT ceil(extract(epoch FROM $1::timestamptz + make_interval(secs => $2) - now()))::int",
                recent[0]["created_at"], float(REQUEST_WINDOW))
            return CodeAttempt("limited", retry_after=max(1, int(wait or 1)), left=0)
        if well_formed(previous):
            await conn.execute(
                """UPDATE setup_codes SET cancelled_at = now(), cancel_reason = 'superseded'
                   WHERE attempt_hash = $1 AND used_at IS NULL AND cancelled_at IS NULL""", token_hash(previous))
        attempt, code, nonce = new_token(), new_code(), secrets.token_urlsafe(9)
        code_id = await conn.fetchval(
            """INSERT INTO setup_codes (attempt_hash, code_digest, nonce, expires_at)
               VALUES ($1, $2, $3, now() + make_interval(secs => $4)) RETURNING id""",
            token_hash(attempt), _digest(code, key), nonce, float(CODE_TTL))
    return CodeAttempt("sent", attempt=attempt, code=code, code_id=code_id, nonce=nonce,
                       left=REQUEST_LIMIT - len(recent) - 1)


async def mark_delivered(conn: asyncpg.Connection, code_id: int, message_id: int | None) -> None:
    """Код ушёл владельцу в бот: только теперь попытка может сработать."""
    await conn.execute(
        """UPDATE setup_codes SET delivered = true, message_id = $2
           WHERE id = $1 AND used_at IS NULL AND cancelled_at IS NULL""", code_id, message_id)


async def verify_code(conn: asyncpg.Connection, key: bytes, attempt: Any, code: Any) -> tuple[str, int | None]:
    """Проверяет код в браузере, который его запросил. Возвращает (итог, номер попытки):

    ok — вход; wrong — код не подошёл (ещё можно); none — попытки нет, она погашена, использована или
    заведена в другом браузере; expired — срок вышел; locked — вход по коду на паузе; locked_now —
    эта неверная попытка стала пятой: код погашен, вход по коду на паузе, владельцу стоит сообщить;
    locked_again — то же, но о паузе владельцу сообщали только что."""
    digits = "".join(ch for ch in str(code) if ch in "0123456789")[:CODE_DIGITS * 2]
    moment = int(now())
    async with conn.transaction():
        data = await _locked_state(conn)
        if int(data.get("lock_until", 0)) > moment:
            return "locked", None
        if not well_formed(attempt):
            return "none", None
        row = await conn.fetchrow("SELECT * FROM setup_codes WHERE attempt_hash = $1 FOR UPDATE", token_hash(attempt))
        if row is None or row["used_at"] is not None or row["cancelled_at"] is not None:
            return "none", None
        if await conn.fetchval("SELECT $1::timestamptz <= now()", row["expires_at"]):
            await conn.execute("UPDATE setup_codes SET cancelled_at = now(), cancel_reason = 'expired' WHERE id = $1",
                               row["id"])
            return "expired", row["id"]
        # Сравнение — за постоянное время; недоставленный код не срабатывает никогда.
        matches = hmac.compare_digest(_digest(digits if len(digits) == CODE_DIGITS else "x", key),
                                      str(row["code_digest"]))
        if matches and row["delivered"] and len(digits) == CODE_DIGITS:
            await conn.execute("UPDATE setup_codes SET used_at = now(), tries = tries + 1 WHERE id = $1", row["id"])
            data.pop("lock_level", None)        # удачный вход: следующие паузы — снова с минуты
            await _save_state(conn, data)
            return "ok", row["id"]
        tries = int(row["tries"]) + 1
        if tries < CODE_MAX_ATTEMPTS:
            await conn.execute("UPDATE setup_codes SET tries = $2 WHERE id = $1", row["id"], tries)
            return "wrong", row["id"]
        await conn.execute(
            """UPDATE setup_codes SET tries = $2, cancelled_at = now(), cancel_reason = 'tries' WHERE id = $1""",
            row["id"], tries)
        level = int(data.get("lock_level", 0))
        period = LOCK_STEPS[min(level, len(LOCK_STEPS) - 1)]
        data["lock_until"] = moment + period
        data["lock_level"] = level + 1
        # Сообщение владельцу — одно на период: следующее не раньше, чем через длину этой паузы.
        notify = moment >= int(data.get("lock_notice_until", 0))
        if notify:
            data["lock_notice_until"] = moment + max(period, LOCK_NOTICE_EVERY)
        await _save_state(conn, data)
    return ("locked_now" if notify else "locked_again"), row["id"]


async def tries_left(conn: asyncpg.Connection, code_id: int | None) -> int | None:
    if code_id is None:
        return None
    tries = await conn.fetchval("SELECT tries FROM setup_codes WHERE id = $1", code_id)
    return None if tries is None else max(0, CODE_MAX_ATTEMPTS - int(tries))


async def lock_left(conn: asyncpg.Connection) -> int:
    """Сколько секунд ещё длится пауза входа по коду (0 — паузы нет)."""
    raw = await conn.fetchval("SELECT value FROM setup_state WHERE key = $1", CODE_STATE)
    value = json.loads(raw) if isinstance(raw, str) else (raw or {})
    return max(0, int(value.get("lock_until", 0)) - int(now())) if isinstance(value, dict) else 0


async def cancel_codes(conn: asyncpg.Connection, reason: str) -> int:
    """Гасит все ждущие попытки входа по коду («Это не я», выход везде, вход по ссылке, выключение)."""
    done = await conn.execute(
        """UPDATE setup_codes SET cancelled_at = now(), cancel_reason = $1
           WHERE used_at IS NULL AND cancelled_at IS NULL""", reason)
    return int(done.split()[-1])


async def logins_by_code(conn: asyncpg.Connection, days: int = 7) -> int:
    """Сколько входов по коду за `days` суток — для `./ops/doctor.sh` (строка setup-code-login)."""
    return int(await conn.fetchval(
        "SELECT count(*) FROM setup_codes WHERE used_at > now() - make_interval(days => $1)", days))
