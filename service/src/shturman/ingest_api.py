"""Приём данных от плагина «Штурмана» в Hermes: бизнес-сообщения, исключение чатов, загрузка экспорта.

Три группы маршрутов (все под /api/, токен SHTURMAN_API_TOKEN):

  /api/ingest/business/*  — подключение бота в бизнес-режиме, новые, изменённые и удалённые
                            сообщения. Плагин пересылает объекты Bot API как есть; здесь они
                            приводятся к словарю архива (`botapi_normalize`) и пишутся через `store`.
  /api/chats              — список чатов без текста сообщений и переключатель «не принимать
                            в архив», в том числе с удалением уже сохранённого.
  /api/imports            — загрузка экспорта Telegram Desktop для мастера настройки: файл
                            пишется на диск потоком, затем просмотр состава и фоновый импорт.

Состояние загрузок живёт в памяти процесса, а итоги импорта — в таблице `imports`, которую
ведёт `importer`. Отдельной таблицы нет намеренно: загруженный файл — временная вещь, после
перезапуска сервиса он удаляется (в нём вся переписка открытым текстом), а значит и помнить
о нём нечего; повторная загрузка и повторный импорт безопасны.

Обновления бизнес-режима приходят двумя путями: от плагина по HTTP (`via="plugin"`) и от своего
бота сервиса прямо в процессе (`via="service"`, см. `executor/bot.py`). Оба пути зовут одни и те же
функции `accept_business_*`. Подключение принадлежит тому пути, которым пришло первым: другой
путь по нему ничего записать не может (код `foreign_connection`).

Подтверждение владельцем (см. `confirm.py`). Когда у сервиса свой бот согласований:
исключить чат можно сразу (это ужесточение), а вернуть чат в архив, стереть сообщения
исключённого чата и запустить импорт выгрузки — только после нажатия владельца в боте; маршрут
тогда отвечает 202 с `status: pending_confirmation`. Запрос «исключить и стереть» исключает чат
сразу, а стирание ждёт. Удаление загруженного файла подтверждения не требует: в архив оно ничего
не добавляет и из архива ничего не убирает.

Текст сообщений и имена собеседников в журнал не пишутся.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import inspect
import json
import logging
import os
import re
import secrets
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

import asyncpg
import ijson
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from . import bridge, confirm, db, events, guard, store
from .api_core import BadRequest, error_response, need_str, only_without_own_bot, settle
from .app import AppState, state_of
from .botapi_normalize import (
    MAX_ID, NormalizeError, SkipMessage, chat_record, normalize_message, seen_by, user_name,
)
from .config import ConfigError
from .importer import ImportStats, import_export, scan
from .records import MessageRecord
from .sanitize import clean_line
from .telegram_export import ExportFormatError

logger = logging.getLogger("shturman.ingest")

# Действия, которые при своём боте согласований ждут нажатия владельца (см. confirm.py).
# Исключение чата — ужесточение и применяется сразу; возврат чата, стирание его сообщений
# и импорт выгрузки расширяют то, что видит ассистент, либо необратимо стирают данные.
CHAT_EXCLUDE = "archive.chat_exclude"
CHAT_INCLUDE = "archive.chat_include"
CHAT_PURGE = "archive.chat_purge"
IMPORT_RUN = "archive.import_run"

# Состояние работающего сервиса: функции применения действий состояния не получают.
_state: AppState | None = None

MAX_JSON_BYTES = 512 * 1024           # объект Bot API с запасом; больше — не сообщение
UPLOAD_ENV = "SHTURMAN_UPLOAD_MAX_BYTES"
DEFAULT_UPLOAD_MAX = 2 * 1024**3      # 2 ГиБ
MAX_KEPT_FILES = 3                    # столько загруженных файлов лежит на диске одновременно
MAX_RECORDS = 20                      # столько записей о загрузках помнится (с итогами)
UPLOAD_TTL = timedelta(hours=24)      # неиспользованная загрузка удаляется сама
JANITOR_EVERY = 600                   # секунд
SCAN_WAIT_DEFAULT = 25.0              # сколько запрос scan ждёт результата, прежде чем ответить 202
SCAN_WAIT_MAX = 60.0
DEADLOCK_RETRIES = 3

_IMPORT_ID = re.compile(r"^[0-9a-f]{32}$")
_EXCLUDE = re.compile(r"^(user|chat|channel):(\d{1,19})$")
_CYRILLIC = re.compile(r"[а-яА-ЯёЁ]")
_UPLOAD_GLOB = "export-*"


class ApiError(BadRequest):
    """Ошибка с машинным кодом: по нему плагин решает, что делать дальше."""

    def __init__(self, message: str, status: int, code: str) -> None:
        super().__init__(message, status, code)


def _handler(fn):
    """Как `api_core.handler`, плюс код ошибки и обрыв соединения посреди тела запроса."""
    async def wrapped(request: Request) -> JSONResponse:
        try:
            return await fn(request)
        except BadRequest as exc:
            return error_response(exc)
        except ClientDisconnect:
            return JSONResponse({"error": "соединение оборвалось до конца запроса"}, status_code=400)
    wrapped.__name__ = fn.__name__
    return wrapped


async def _json(request: Request, *, limit: int = MAX_JSON_BYTES, allow_empty: bool = False) -> dict[str, Any]:
    """Тело запроса как JSON-объект. В отличие от `api_core.body`, с пределом размера."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise BadRequest("тело запроса слишком большое", 413)
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > limit:
            raise BadRequest("тело запроса слишком большое", 413)
    if not buf.strip() and allow_empty:
        return {}
    try:
        data = json.loads(bytes(buf))
    except (ValueError, RecursionError):
        raise BadRequest("тело запроса должно быть JSON-объектом") from None
    if not isinstance(data, dict):
        raise BadRequest("тело запроса должно быть JSON-объектом")
    return data


def _need_dict(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise BadRequest(f"поле {key}: нужен объект")
    return value


def _flag(data: dict[str, Any], key: str, *, default: bool | None = None) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise BadRequest(f"поле {key}: нужно true или false")
    return value


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


# ---------------------------------------------------------------------------
# Бизнес-режим
# ---------------------------------------------------------------------------

async def _owner_ids(conn: asyncpg.Connection) -> set[int]:
    """Кого сервис считает владельцем: привязанного в управляющем чате, а пока привязки нет —
    владельца уже загруженного экспорта."""
    owner = await bridge.get_owner(conn)
    if owner and isinstance(owner.get("user_id"), int):
        return {int(owner["user_id"])}
    rows = await conn.fetch("SELECT tg_user_id FROM accounts WHERE role = 'owner'")
    return {r["tg_user_id"] for r in rows}


async def _connection(conn: asyncpg.Connection, connection_id: str, via: str) -> asyncpg.Record:
    """Подключение, по которому пришло обновление. Чужой путь доставки отклоняется."""
    link = await conn.fetchrow(
        """SELECT b.account_id, b.enabled, b.via, a.tg_user_id
           FROM business_connections b JOIN accounts a ON a.id = b.account_id
           WHERE b.id = $1""",
        connection_id,
    )
    if link is None:
        raise _unknown_connection()
    if link["via"] != via:
        raise _foreign_connection()
    return link


def _unknown_connection() -> ApiError:
    return ApiError(
        "неизвестное бизнес-подключение — сначала пришлите его на /api/ingest/business/connection",
        409, "unknown_connection",
    )


def _foreign_connection() -> ApiError:
    return ApiError(
        "это бизнес-подключение принадлежит другому боту — обновления по нему принимаются только от него",
        409, "foreign_connection",
    )


OwnerIds = Callable[[asyncpg.Connection], Awaitable[set[int]]]


async def accept_business_connection(
    pool: asyncpg.Pool, link: Any, *, via: str = "plugin", owner_ids: OwnerIds = _owner_ids,
) -> dict[str, Any]:
    """Подключение бота к аккаунту в бизнес-режиме: создано, изменено или отключено.

    Подключить бота к себе может любой пользователь Telegram, поэтому принимается только
    подключение от аккаунта владельца. Пока владелец сервису не известен, подключение
    не принимается вовсе.

    via — каким путём пришло обновление: "plugin" (бот в Hermes) или "service" (свой бот сервиса).
    Через того же бота потом идёт отправка, поэтому путь у подключения один и не меняется.
    """
    if not isinstance(link, dict):
        raise BadRequest("поле connection: нужен объект")
    connection_id = need_str(link, "id", limit=256)
    user = _need_dict(link, "user")
    user_id = user.get("id")
    if isinstance(user_id, bool) or not isinstance(user_id, int) or not 0 < user_id <= MAX_ID:
        raise BadRequest("поле user.id: нужен положительный идентификатор")
    if user.get("is_bot") is True:
        raise BadRequest("поле user: бизнес-подключение создаёт человек, а не бот")
    enabled = _flag(link, "is_enabled")
    # Bot API 9.0+: права лежат в rights; до того было отдельное поле can_reply.
    rights = link.get("rights")
    can_reply = rights.get("can_reply") is True if isinstance(rights, dict) else link.get("can_reply") is True

    async with pool.acquire() as conn:
        async with conn.transaction():
            owners = await owner_ids(conn)
            if not owners:
                raise ApiError(
                    "владелец ещё не привязан — подключение бизнес-режима принимается только после привязки",
                    409, "owner_unknown",
                )
            if user_id not in owners:
                logger.warning("отклонено бизнес-подключение от постороннего аккаунта")
                raise ApiError("бизнес-подключение создано не аккаунтом владельца", 403, "not_owner")
            account_id = await store.ensure_account(conn, user_id, user_name(user) or f"Аккаунт {user_id}")
            role = await conn.fetchval("SELECT role FROM accounts WHERE id = $1", account_id)
            if role != "owner":
                raise ApiError("этот аккаунт записан как помощник, а не как владелец", 409, "not_owner_account")
            saved = await conn.fetchval(
                """INSERT INTO business_connections (id, account_id, can_reply, enabled, via)
                   VALUES ($1, $2, $3, $4, $5)
                   ON CONFLICT (id) DO UPDATE
                   SET account_id = EXCLUDED.account_id, can_reply = EXCLUDED.can_reply,
                       enabled = EXCLUDED.enabled, updated_at = now()
                   WHERE business_connections.via = EXCLUDED.via
                   RETURNING id""",
                connection_id, account_id, can_reply, enabled, via,
            )
            if saved is None:
                # Подключение уже записано другим путём: первый записавший остаётся хозяином.
                raise _foreign_connection()
    return {"ok": True, "account_id": account_id, "enabled": enabled, "can_reply": can_reply}


@_handler
async def business_connection(request: Request) -> JSONResponse:
    # Со своим ботом сервиса бизнес-режим идёт только через него: по HTTP держатель токена API
    # мог бы записать подключение и подложить в архив сообщения от имени чужих людей.
    only_without_own_bot()
    data = await _json(request)
    return JSONResponse(await accept_business_connection(
        state_of(request).pool, _need_dict(data, "connection")))


def _as_newer_edit(record: MessageRecord, stored_edit: datetime | None) -> MessageRecord:
    """Живая правка с другим текстом должна стать текущим текстом, а «новее» архив определяет
    по времени правки. Два случая, когда времени для этого не хватает:
      * время правки не пришло — ставится время приёма;
      * оно совпало с сохранённым до секунды (две правки подряд) — сдвигается на микросекунду.
    Правка со временем раньше сохранённого остаётся как есть: это запоздавшая копия.
    """
    step = timedelta(microseconds=1)
    if record.edited_at is None:
        now = datetime.now(timezone.utc)
        return replace(record, edited_at=max(now, stored_edit + step) if stored_edit else now)
    if stored_edit is not None and record.edited_at == stored_edit:
        return replace(record, edited_at=stored_edit + step)
    return record


async def _store_business_message(
    conn: asyncpg.Connection, link: asyncpg.Record, norm, *, edited: bool
) -> dict[str, Any] | None:
    """Пишет сообщение в архив. Возвращает сведения для ответа и события либо None — чат исключён."""
    owner_tg_id = link["tg_user_id"]
    record = norm.record
    outgoing = record.sender_tg_id == owner_tg_id if record.sender_tg_id is not None else None
    async with conn.transaction():
        chat_id, excluded = await store.ensure_chat(
            conn, link["account_id"], seen_by(norm.chat, owner_tg_id), refresh=True)
        if excluded:
            return None
        # Одно и то же сообщение может прийти дважды одновременно — второе ждёт первого,
        # иначе оба сочтут себя новыми.
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            f"shturman.ingest:{chat_id}:{record.tg_message_id}",
        )
        before = await conn.fetchrow(
            "SELECT text, edited_at FROM messages WHERE chat_id = $1 AND tg_message_id = $2",
            chat_id, record.tg_message_id,
        )
        if edited and before is not None and before["text"] != record.text:
            record = _as_newer_edit(record, before["edited_at"])
        result = await store.upsert_messages(
            conn, [(chat_id, record, outgoing)], source="business", owner_tg_id=owner_tg_id,
            hold=guard.holding())
    if result.new + result.known == 0:
        return None
    # «Изменилось» — появилась новая строка либо пришла правка новее сохранённой. Повторная
    # доставка того же самого архив не меняет и событием не становится.
    newer_edit = record.edited_at is not None and (
        before is None or before["edited_at"] is None or record.edited_at > before["edited_at"])
    return {
        "chat_id": chat_id,
        "message_id": (result.new_ids or result.known_ids)[0],
        "new": result.new == 1,
        "changed": result.new == 1 or newer_edit,
        "outgoing": bool(outgoing),
        "edited": edited,
    }


async def accept_business_message(
    state: AppState, message: Any, *, edited: bool = False, via: str = "plugin",
) -> dict[str, Any]:
    """Новое или изменённое сообщение из личного чата владельца."""
    if not isinstance(message, dict):
        raise BadRequest("поле message: нужен объект")
    connection_id = need_str(message, "business_connection_id", limit=256)
    try:
        norm = normalize_message(message)
    except SkipMessage:
        return {"stored": False, "message_id": None, "reason": "no_message_id"}
    except NormalizeError as exc:
        raise BadRequest(str(exc)) from None

    async with state.pool.acquire() as conn:
        link = await _connection(conn, connection_id, via)
        if not link["enabled"]:
            return {"stored": False, "message_id": None, "reason": "connection_disabled"}
        for attempt in range(DEADLOCK_RETRIES):
            try:
                saved = await _store_business_message(conn, link, norm, edited=edited)
                break
            except asyncpg.DeadlockDetectedError:
                # Запись может встретиться с идущим импортом на одних и тех же строках.
                if attempt == DEADLOCK_RETRIES - 1:
                    raise BadRequest("архив занят, повторите запрос", 503) from None
    if saved is None:
        return {"stored": False, "message_id": None, "reason": "excluded"}
    # Новый входящий текст записан скрытым от ассистента; проверка решает, открыть ли его.
    hidden = await guard.screen([saved["message_id"]])
    if saved["changed"] and not hidden:
        state.events.publish(events.MESSAGE_LIVE, {
            "account_id": link["account_id"], "chat_id": saved["chat_id"],
            "message_id": saved["message_id"], "source": "business",
            "outgoing": saved["outgoing"], "edited": edited, "via_bot": norm.via_bot,
        })
    return {"stored": True, "message_id": saved["message_id"], "new": saved["new"], "changed": saved["changed"]}


@_handler
async def business_message(request: Request) -> JSONResponse:
    only_without_own_bot()
    data = await _json(request)
    message = _need_dict(data, "message")
    edited = _flag(data, "edited", default=False)
    return JSONResponse(await accept_business_message(state_of(request), message, edited=edited))


async def accept_business_deleted(state: AppState, data: Any, *, via: str = "plugin") -> dict[str, Any]:
    """Сообщения удалены в личном чате владельца: в архиве они помечаются, а не стираются."""
    if not isinstance(data, dict):
        raise BadRequest("тело запроса должно быть JSON-объектом")
    connection_id = need_str(data, "business_connection_id", limit=256)
    try:
        chat = chat_record(data.get("chat"))
    except NormalizeError as exc:
        raise BadRequest(str(exc)) from None
    ids = data.get("message_ids")
    if not isinstance(ids, list) or len(ids) > 1000 or any(
            isinstance(i, bool) or not isinstance(i, int) or not 0 < i <= MAX_ID for i in ids):
        raise BadRequest("поле message_ids: нужен список идентификаторов сообщений (не больше 1000)")

    async with state.pool.acquire() as conn:
        link = await _connection(conn, connection_id, via)
        if not link["enabled"]:
            return {"deleted": 0, "reason": "connection_disabled"}
        # Чат ищется, а не создаётся: удалять в незнакомом чате нечего.
        chat_id = await conn.fetchval(
            """SELECT c.id FROM chats c JOIN peers p ON p.id = c.peer_id
               WHERE c.account_id = $1 AND p.class = 'user' AND p.tg_id = $2""",
            link["account_id"], chat.tg_id,
        )
        deleted = await store.mark_deleted(conn, chat_id, ids) if chat_id is not None else []
    if deleted:
        state.events.publish(events.MESSAGES_DELETED, {"message_ids": deleted})
    return {"deleted": len(deleted)}


@_handler
async def business_deleted(request: Request) -> JSONResponse:
    only_without_own_bot()
    return JSONResponse(await accept_business_deleted(state_of(request), await _json(request)))


# ---------------------------------------------------------------------------
# Чаты и исключения
# ---------------------------------------------------------------------------

_CHAT_FILTER = """
    ($1::text IS NULL OR c.title ILIKE $1 OR p.name ILIKE $1 OR p.username ILIKE $1)
    AND ($2::boolean IS NULL OR c.excluded = $2)
"""


def _query_int(request: Request, key: str, default: int, *, low: int, high: int) -> int:
    raw = request.query_params.get(key)
    if raw is None or raw == "":
        return default
    if not raw.isdigit() or len(raw) > 9 or not low <= int(raw) <= high:
        raise BadRequest(f"параметр {key}: нужно целое число от {low} до {high}")
    return int(raw)


@_handler
async def list_chats(request: Request) -> JSONResponse:
    """Чаты архива для экрана исключений. Текста сообщений в ответе нет — только счётчики."""
    params = request.query_params
    query = (params.get("query") or "").strip().lstrip("@")[:200]
    pattern = None
    if query:
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    raw_excluded = (params.get("excluded") or "").lower()
    if raw_excluded not in ("", "true", "false", "1", "0"):
        raise BadRequest("параметр excluded: нужно true или false")
    excluded = None if raw_excluded == "" else raw_excluded in ("true", "1")
    limit = _query_int(request, "limit", 50, low=1, high=200)
    offset = _query_int(request, "offset", 0, low=0, high=10**9)

    async with state_of(request).ro_pool.acquire() as conn:
        total = await conn.fetchval(
            f"SELECT count(*) FROM chats c JOIN peers p ON p.id = c.peer_id WHERE {_CHAT_FILTER}",
            pattern, excluded,
        )
        rows = await conn.fetch(
            f"""SELECT c.id, c.account_id, a.label AS account_label, a.role AS account_role,
                       p.class AS kind, p.tg_id, p.username, p.is_bot, c.type,
                       COALESCE(c.title, p.name) AS title, c.excluded,
                       s.messages, s.last_message_at
                FROM chats c
                JOIN accounts a ON a.id = c.account_id
                JOIN peers p ON p.id = c.peer_id
                LEFT JOIN LATERAL (
                    SELECT count(*) AS messages, max(m.sent_at) AS last_message_at
                    FROM messages m WHERE m.chat_id = c.id
                ) s ON true
                WHERE {_CHAT_FILTER}
                ORDER BY s.last_message_at DESC NULLS LAST, c.id
                LIMIT $3 OFFSET $4""",
            pattern, excluded, limit, offset,
        )
    chats = [{
        "id": r["id"],
        "account_id": r["account_id"], "account_label": r["account_label"], "account_role": r["account_role"],
        "kind": r["kind"], "type": r["type"], "peer": f"{r['kind']}:{r['tg_id']}",
        "title": r["title"], "username": r["username"], "is_bot": r["is_bot"],
        "messages": r["messages"], "last_message_at": _iso(r["last_message_at"]),
        "excluded": r["excluded"],
        # служебные чаты Telegram исключены всегда: снять запрет нельзя
        "locked": store.is_blocked_peer(r["kind"], r["tg_id"], r["username"]),
    } for r in rows]
    return JSONResponse({"chats": chats, "total": total, "limit": limit, "offset": offset})


@_handler
async def put_chat_excluded(request: Request) -> JSONResponse:
    """Исключить чат из архива (по желанию — стерев уже сохранённое) или вернуть его.

    Вернуть чат можно только здесь: остальные пути записи запрет лишь ставят.
    """
    data = await _json(request)
    excluded = _flag(data, "excluded")
    purge = _flag(data, "purge", default=False)
    if purge and not excluded:
        raise BadRequest("поле purge: стереть сообщения можно только вместе с исключением чата")
    chat_id = request.path_params["chat_id"]
    if chat_id > MAX_ID:
        raise BadRequest("чат не найден", 404)

    state = state_of(request)
    _no_running_import(state, BadRequest)
    async with state.pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT p.class, p.tg_id, p.username, c.excluded, COALESCE(c.title, p.name) AS title,
                      (SELECT count(*) FROM messages m WHERE m.chat_id = c.id) AS messages
               FROM chats c JOIN peers p ON p.id = c.peer_id WHERE c.id = $1""",
            chat_id,
        )
        if row is None:
            raise BadRequest("чат не найден", 404)
        name = clean_line(row["title"], 64) or f"№ {chat_id}"
        if not excluded:
            if store.is_blocked_peer(row["class"], row["tg_id"], row["username"]):
                raise ApiError(_LOCKED, 409, "locked")
            # Возврат чата расширяет то, что сохраняется и что видит ассистент, — ждёт владельца.
            summary = (f"Вернуть чат «{name}» в архив: сервис снова будет сохранять его сообщения, "
                       "и ассистент сможет их читать.") if row["excluded"] else None
            answer, out = await settle(conn, CHAT_INCLUDE, {"chat_id": chat_id}, summary=summary)
            return answer or JSONResponse(out)
        # Исключение — ужесточение: применяется сразу, чтобы чат можно было закрыть без ожидания.
        await settle(conn, CHAT_EXCLUDE, {"chat_id": chat_id}, summary=None)
        purged = 0
        if purge:
            # Стирание необратимо, поэтому ждёт владельца. Чат к этому моменту уже исключён.
            summary = (f"Стереть из архива все сохранённые сообщения чата «{name}» (сообщений: {row['messages']}) "
                       "и всё, что из них выведено: обязательства, строки страниц памяти, черновики. "
                       "Вернуть стёртое нельзя. Сам чат уже исключён: новые сообщения из него не сохраняются."
                       ) if row["messages"] else None
            answer, purged = await settle(conn, CHAT_PURGE, {"chat_id": chat_id}, summary=summary,
                                          applied_now={"id": chat_id, "excluded": True})
            if answer is not None:
                return answer
    return JSONResponse({"id": chat_id, "excluded": True, "purged": purged or 0})


_LOCKED = "служебный чат Telegram исключён всегда: в нём коды входа и токены"


def _no_running_import(state: AppState | None, error: type[Exception]) -> None:
    registry: Registry | None = state.extras.get("imports") if state is not None else None
    if registry is not None and registry.running() is not None:
        # Импорт пишет пачками и проверяет запрет в начале пачки: смена запрета посреди
        # импорта могла бы пропустить в архив сообщения только что исключённого чата.
        raise error("идёт импорт экспорта — измените исключения после его окончания", 409, "import_running")


def _publish_excluded(chat_id: int, *, purged: bool) -> confirm.After:
    async def publish() -> None:
        if _state is not None:
            _state.events.publish(events.CHAT_EXCLUDED, {"chat_id": chat_id, "purged": purged})
    return publish


async def _chat_for_update(conn: asyncpg.Connection, chat_id: int) -> asyncpg.Record:
    row = await conn.fetchrow(
        """SELECT p.class, p.tg_id, p.username, c.excluded FROM chats c JOIN peers p ON p.id = c.peer_id
           WHERE c.id = $1 FOR UPDATE OF c""", chat_id)
    if row is None:
        raise confirm.Refused("чат не найден", 404)
    return row


@confirm.applier(CHAT_EXCLUDE)
async def _apply_chat_exclude(conn: asyncpg.Connection, payload: dict[str, Any]) -> confirm.Done:
    chat_id = int(payload["chat_id"])
    _no_running_import(_state, confirm.Refused)
    await _chat_for_update(conn, chat_id)
    await conn.execute("UPDATE chats SET excluded = true WHERE id = $1", chat_id)
    logger.info("чат %s: исключён", chat_id)
    return confirm.Done(result={"id": chat_id, "excluded": True, "purged": 0},
                        after=_publish_excluded(chat_id, purged=False))


@confirm.applier(CHAT_INCLUDE)
async def _apply_chat_include(conn: asyncpg.Connection, payload: dict[str, Any]) -> confirm.Done:
    chat_id = int(payload["chat_id"])
    _no_running_import(_state, confirm.Refused)
    row = await _chat_for_update(conn, chat_id)
    if store.is_blocked_peer(row["class"], row["tg_id"], row["username"]):
        raise confirm.Refused(_LOCKED, 409, "locked")
    confirm.must_not_widen(row["excluded"])     # строка чата заблокирована: исключён ли он сейчас
    await conn.execute("UPDATE chats SET excluded = false WHERE id = $1", chat_id)
    logger.info("чат %s: возвращён в архив", chat_id)
    return confirm.Done(result={"id": chat_id, "excluded": False, "purged": 0})


@confirm.applier(CHAT_PURGE)
async def _apply_chat_purge(conn: asyncpg.Connection, payload: dict[str, Any]) -> confirm.Done:
    chat_id = int(payload["chat_id"])
    _no_running_import(_state, confirm.Refused)
    row = await _chat_for_update(conn, chat_id)
    if not row["excluded"]:
        # За время ожидания чат вернули в архив: стирать сообщения действующего чата нельзя.
        raise confirm.Refused("чат больше не исключён: стереть сообщения можно только у исключённого чата")
    # Без владельца стирать можно только «ничего»: исключённый чат новых сообщений не принимает.
    confirm.must_not_widen(await conn.fetchval("SELECT EXISTS (SELECT 1 FROM messages WHERE chat_id = $1)", chat_id))
    status = await conn.execute("DELETE FROM messages WHERE chat_id = $1", chat_id)
    purged = int(status.split()[-1])
    logger.info("чат %s: стёрто сообщений=%s", chat_id, purged)
    return confirm.Done(note=f"Стёрто сообщений: {purged}.", result=purged,
                        after=_publish_excluded(chat_id, purged=True))


# ---------------------------------------------------------------------------
# Загрузка и импорт экспорта
# ---------------------------------------------------------------------------

class _Stopped(Exception):
    """Чтение файла остановлено: загрузку удалили или сервис останавливается."""


class _ProgressFile:
    """Файл, который считает прочитанные байты и умеет остановиться по просьбе."""

    def __init__(self, fp) -> None:
        self._fp = fp
        self.done = 0
        self.stop = False

    def read(self, size: int = -1) -> bytes:
        if self.stop:
            raise _Stopped()
        chunk = self._fp.read(size)
        self.done += len(chunk)
        return chunk


@dataclass
class Upload:
    id: str
    path: Path
    size: int
    uploaded_at: datetime
    state: str = "uploaded"            # uploaded | scanning | running | done | failed
    error: str | None = None
    scan: dict[str, Any] | None = None  # готовый ответ просмотра
    stats: dict[str, Any] | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    reader: _ProgressFile | None = None
    live: ImportStats | None = None     # счётчики идущего импорта, если importer их отдаёт
    scan_task: asyncio.Task | None = None
    run_task: asyncio.Task | None = None

    def has_file(self) -> bool:
        return self.path.exists()

    def drop_file(self) -> None:
        self.path.unlink(missing_ok=True)

    def view(self) -> dict[str, Any]:
        read = self.reader.done if self.reader is not None else (self.size if self.state == "done" else 0)
        progress: dict[str, Any] = {
            "bytes_read": min(read, self.size), "bytes_total": self.size,
            "percent": 100 if self.state == "done" else min(99, read * 100 // max(self.size, 1)),
        }
        if self.live is not None and self.state == "running":
            progress.update(chats=self.live.chats, messages_read=self.live.messages_read,
                            messages_new=self.live.messages_new)
        return {
            "import_id": self.id, "state": self.state, "size_bytes": self.size,
            "uploaded_at": _iso(self.uploaded_at), "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "scanned": self.scan is not None, "file_kept": self.has_file(),
            "progress": progress, "stats": self.stats, "error": self.error,
        }


@dataclass
class Registry:
    directory: Path
    max_bytes: int
    items: dict[str, Upload] = field(default_factory=dict)
    uploading: int = 0

    def running(self) -> Upload | None:
        return next((u for u in self.items.values() if u.state == "running"), None)

    def files(self) -> int:
        return self.uploading + sum(1 for u in self.items.values() if u.has_file())

    def forget(self, upload: Upload) -> None:
        if upload.reader is not None:
            upload.reader.stop = True
        upload.drop_file()
        self.items.pop(upload.id, None)

    def trim(self) -> None:
        """Старые записи без файла (итоги давних импортов) вытесняются новыми."""
        spare = sorted((u for u in self.items.values() if u.state in ("done", "failed") and not u.has_file()),
                       key=lambda u: u.uploaded_at)
        while len(self.items) >= MAX_RECORDS and spare:
            self.items.pop(spare.pop(0).id, None)

    def expire(self, now: datetime) -> int:
        """Удаляет загрузки, которыми давно не пользуются. Возвращает число удалённых."""
        stale = [u for u in self.items.values()
                 if u.state not in ("running", "scanning") and now - (u.finished_at or u.uploaded_at) > UPLOAD_TTL]
        for upload in stale:
            self.forget(upload)
        return len(stale)


def _upload_limit() -> int:
    raw = os.environ.get(UPLOAD_ENV, "").strip()
    if not raw:
        return DEFAULT_UPLOAD_MAX
    if not raw.isdigit() or int(raw) <= 0:
        raise ConfigError(f"{UPLOAD_ENV}: нужно положительное целое число байтов")
    return int(raw)


def _sweep(directory: Path) -> int:
    """Удаляет файлы загрузок, оставшиеся от прошлого запуска: сведений о них уже нет."""
    removed = 0
    for path in directory.glob(_UPLOAD_GLOB):
        if path.is_file() and path.suffix in (".json", ".part"):
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def _registry(request: Request) -> Registry:
    return state_of(request).extras["imports"]


def _upload(request: Request) -> Upload:
    import_id = request.path_params["import_id"]
    upload = _registry(request).items.get(import_id) if _IMPORT_ID.match(import_id) else None
    if upload is None:
        raise BadRequest("загрузка не найдена — возможно, сервис перезапускался; загрузите файл заново", 404)
    return upload


def _owner_error(exc: BaseException) -> str:
    """Текст ошибки для владельца. Свои сообщения (они на русском) отдаются как есть, чужие —
    нет: в них могут оказаться куски содержимого файла."""
    if _is_bad_file(exc):
        text = str(exc)
        if _CYRILLIC.search(text):
            return text[:500]
        return "файл не похож на экспорт Telegram Desktop: нужен result.json из экспорта в формате JSON"
    return "внутренняя ошибка — подробности в журнале сервиса"


def _is_bad_file(exc: BaseException) -> bool:
    """Ошибка в самом файле или в параметрах импорта, а не в сервисе или базе.
    (asyncpg.DataError тоже наследует ValueError, но это ошибка записи, не файла.)"""
    if isinstance(exc, (asyncpg.PostgresError, asyncpg.InterfaceError)):
        return False
    return isinstance(exc, (ValueError, ijson.JSONError))


@_handler
async def upload_export(request: Request) -> JSONResponse:
    """Принимает result.json телом запроса и пишет его на диск по мере поступления."""
    registry = _registry(request)
    if "multipart/" in request.headers.get("content-type", "").lower():
        raise BadRequest("файл нужно прислать телом запроса как есть, а не формой", 415)
    too_big = BadRequest(
        f"файл больше допустимого размера ({registry.max_bytes} байт)", 413)
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > registry.max_bytes:
        raise too_big
    if registry.files() >= MAX_KEPT_FILES:
        raise ApiError("слишком много загруженных файлов — удалите ненужные загрузки", 409, "too_many_uploads")

    import_id = secrets.token_hex(16)
    path = registry.directory / f"export-{import_id}.json"
    part = path.with_suffix(".part")
    size, complete = 0, False
    registry.uploading += 1
    try:
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fp:
            async for chunk in request.stream():
                if not chunk:
                    continue
                size += len(chunk)
                if size > registry.max_bytes:
                    raise too_big
                await asyncio.to_thread(fp.write, chunk)
        if size == 0:
            raise BadRequest("пустой файл: пришлите result.json телом запроса")
        os.replace(part, path)
        complete = True
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            raise BadRequest("на диске сервера не хватает места для файла", 507) from None
        logger.error("не удалось записать загружаемый файл: %s", type(exc).__name__)
        raise BadRequest("не удалось сохранить файл на сервере", 500) from None
    finally:
        registry.uploading -= 1
        if not complete:
            part.unlink(missing_ok=True)

    registry.trim()
    registry.items[import_id] = Upload(import_id, path, size, datetime.now(timezone.utc))
    logger.info("загружен экспорт %s: %s байт", import_id, size)
    return JSONResponse({"import_id": import_id, "size_bytes": size, "state": "uploaded"}, status_code=201)


@_handler
async def list_imports(request: Request) -> JSONResponse:
    items = sorted(_registry(request).items.values(), key=lambda u: u.uploaded_at, reverse=True)
    return JSONResponse({"imports": [u.view() for u in items]})


@_handler
async def import_status(request: Request) -> JSONResponse:
    return JSONResponse(_upload(request).view())


def _scan_file(upload: Upload):
    with open(upload.path, "rb") as fp:
        upload.reader = _ProgressFile(fp)
        return scan(upload.reader)


async def _scan(state: AppState, upload: Upload) -> None:
    """Просмотр состава файла в отдельном потоке: разбор JSON занимает процессор надолго."""
    try:
        owner, chats = await asyncio.to_thread(_scan_file, upload)
        banned: set[tuple[str, int]] = set()
        if owner is not None:
            async with state.ro_pool.acquire() as conn:
                banned = {(r["class"], r["tg_id"]) for r in await conn.fetch(
                    """SELECT p.class, p.tg_id FROM chats c
                       JOIN peers p ON p.id = c.peer_id JOIN accounts a ON a.id = c.account_id
                       WHERE a.tg_user_id = $1 AND c.excluded""",
                    owner.tg_user_id)}
        upload.scan = {
            "import_id": upload.id,
            "owner": {"tg_user_id": owner.tg_user_id, "name": owner.name} if owner else None,
            "total_messages": sum(c.messages for c in chats),
            "chats": [{
                "key": f"{c.peer_class}:{c.tg_id}", "kind": c.peer_class, "tg_id": c.tg_id,
                "type": c.type, "name": c.name, "messages": c.messages,
                "first_at": c.first_at, "last_at": c.last_at,
                # служебный чат Telegram: в архив не попадёт при любом выборе
                "locked": store.is_blocked_peer(c.peer_class, c.tg_id),
                # уже исключён раньше: импорт его пропустит
                "excluded": (c.peer_class, c.tg_id) in banned,
            } for c in chats],
        }
        upload.state = "uploaded"
    except asyncio.CancelledError:
        if upload.reader is not None:
            upload.reader.stop = True
        raise
    except _Stopped:
        pass
    except Exception as exc:  # noqa: BLE001 — любая ошибка разбора становится состоянием загрузки
        upload.state, upload.error = "failed", _owner_error(exc)
        upload.finished_at = datetime.now(timezone.utc)
        if _is_bad_file(exc):
            upload.drop_file()   # это не экспорт: хранить файл незачем
        else:
            logger.error("просмотр экспорта %s завершился с ошибкой: %s", upload.id, type(exc).__name__)
    finally:
        upload.scan_task = None


@_handler
async def scan_import(request: Request) -> JSONResponse:
    """Владелец экспорта и список чатов с объёмом — для выбора, что не принимать в архив.

    Большой файл разбирается десятки секунд: если результат не успел, ответ 202 с состоянием
    «scanning», и запрос повторяют.
    """
    upload = _upload(request)
    raw_wait = request.query_params.get("wait", "")
    try:
        wait = min(max(float(raw_wait), 0.0), SCAN_WAIT_MAX) if raw_wait else SCAN_WAIT_DEFAULT
    except ValueError:
        raise BadRequest("параметр wait: нужно число секунд") from None
    if wait != wait:
        raise BadRequest("параметр wait: нужно число секунд")

    if upload.scan is None:
        if upload.state == "running":
            raise ApiError("идёт импорт этого файла — список чатов сейчас недоступен", 409, "import_running")
        if not upload.has_file():
            if upload.state == "failed":
                raise ApiError(upload.error or "файл не удалось разобрать", 422, "scan_failed")
            raise ApiError("файл уже импортирован — состав смотрите в итогах импорта", 409, "no_file")
        if upload.scan_task is None:
            upload.state = "scanning"
            upload.scan_task = state_of(request).spawn(_scan(state_of(request), upload), name=f"scan-{upload.id}")
        await asyncio.wait({upload.scan_task}, timeout=wait)
    if upload.scan is not None:
        return JSONResponse(upload.scan)
    if upload.state == "failed":
        raise ApiError(upload.error or "файл не удалось разобрать", 422, "scan_failed")
    return JSONResponse(upload.view(), status_code=202)


async def _close(conn: asyncpg.Connection) -> None:
    try:
        await asyncio.shield(conn.close(timeout=5))
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        conn.terminate()


async def _import_once(state: AppState, upload: Upload, exclude: set[tuple[str, int]], owner_id: int | None) -> ImportStats:
    conn = await db.connect(state.config.dsn)
    try:
        with open(upload.path, "rb") as fp:
            upload.reader = _ProgressFile(fp)
            extra: dict[str, Any] = {}
            if "stats" in inspect.signature(import_export).parameters:
                # importer умеет отдавать счётчики по ходу работы — показываем их в прогрессе
                upload.live = extra["stats"] = ImportStats()
            return await import_export(
                conn, upload.reader, owner_tg_user_id=owner_id, exclude=exclude,
                source_name="result.json", **extra)
    finally:
        await _close(conn)


async def _run(state: AppState, upload: Upload, exclude: set[tuple[str, int]], owner_id: int | None) -> None:
    """Фоновый импорт на собственном соединении. Наружу исключений не выпускает: итог — в состоянии."""
    try:
        for attempt in range(DEADLOCK_RETRIES):
            try:
                stats = await _import_once(state, upload, exclude, owner_id)
                break
            except asyncpg.DeadlockDetectedError:
                # Встретились с записью живого сообщения; импорт повторяем — он не создаёт дублей.
                if attempt == DEADLOCK_RETRIES - 1:
                    raise
        upload.state, upload.stats, upload.error = "done", stats.as_dict(), None
        upload.drop_file()
        logger.info("импорт %s завершён: новых сообщений %s", upload.id, stats.messages_new)
    except asyncio.CancelledError:
        upload.state, upload.error = "failed", "импорт остановлен"
        raise
    except _Stopped:
        upload.state, upload.error = "failed", "импорт остановлен"
    except Exception as exc:  # noqa: BLE001
        upload.state, upload.error = "failed", _owner_error(exc)
        if isinstance(exc, (ExportFormatError, ijson.JSONError)):
            upload.drop_file()   # это не экспорт; при прочих ошибках файл остаётся для повтора
        elif not _is_bad_file(exc):
            # Только вид ошибки: в её тексте могут быть значения из переписки.
            logger.error("импорт %s завершился с ошибкой: %s", upload.id, type(exc).__name__)
    finally:
        upload.finished_at = datetime.now(timezone.utc)
        upload.run_task = None


def _parse_exclude(value: Any) -> set[tuple[str, int]]:
    if value is None:
        return set()
    if not isinstance(value, list) or len(value) > 100_000:
        raise BadRequest("поле exclude: нужен список вида [\"user:123\", \"chat:456\"]")
    out: set[tuple[str, int]] = set()
    for item in value:
        match = _EXCLUDE.match(item) if isinstance(item, str) else None
        if match is None or int(match.group(2)) > MAX_ID:
            raise BadRequest("поле exclude: каждый элемент — user:123, chat:123 или channel:123")
        out.add((match.group(1), int(match.group(2))))
    return out


@_handler
async def run_import(request: Request) -> JSONResponse:
    """Запускает импорт загруженного файла в фоне. Одновременно идёт только один импорт."""
    upload = _upload(request)
    data = await _json(request, limit=8 * 1024 * 1024, allow_empty=True)
    exclude = _parse_exclude(data.get("exclude"))
    owner_id = data.get("owner_id")
    if owner_id is not None and (isinstance(owner_id, bool) or not isinstance(owner_id, int)
                                 or not 0 < owner_id <= MAX_ID):
        raise BadRequest("поле owner_id: нужен положительный идентификатор")

    state = state_of(request)
    _check_runnable(_registry(request), upload, ApiError)
    # Импорт добавляет в архив сообщения, подлинность которых сервис проверить не может: среди
    # них могут быть и «ваши собственные», после которых ассистенту разрешено готовить черновики
    # в этот чат. Поэтому при своём боте согласований импорт ждёт владельца.
    size = max(1, round(upload.size / 1024 / 1024))
    summary = (f"Импортировать в архив загруженную выгрузку Telegram (файл около {size} МБ, загружен "
               f"{upload.uploaded_at:%d.%m.%Y в %H:%M} UTC). ")
    if upload.scan is not None:
        owner = upload.scan.get("owner") or {}
        summary += (f"В выгрузке чатов: {len(upload.scan['chats'])}, сообщений: {upload.scan['total_messages']}"
                    + (f"; владелец выгрузки — {clean_line(owner.get('name'), 60) or 'без имени'} "
                       f"(идентификатор Telegram {owner.get('tg_user_id')})" if owner else "") + ". ")
    else:
        summary += "Состав файла перед импортом не просматривался. "
    if exclude:
        summary += f"Не принимать чатов: {len(exclude)}. "
    summary += ("Сообщения из выгрузки попадут в архив, и ассистент сможет их читать. Проверить, что "
                "выгрузка настоящая, сервис не может: подтверждайте, только если загружали её сами.")
    payload = {"import_id": upload.id, "exclude": sorted(f"{kind}:{tg_id}" for kind, tg_id in exclude),
               "owner_id": owner_id}
    async with state.pool.acquire() as conn:
        answer, view = await settle(conn, IMPORT_RUN, payload, summary=summary)
    return answer or JSONResponse(view, status_code=202)


def _check_runnable(registry: Registry, upload: Upload, error: type[Exception]) -> None:
    if registry.running() is not None:
        raise error("импорт уже идёт — дождитесь его окончания", 409, "import_running")
    if upload.scan_task is not None:
        raise error("файл ещё просматривается — дождитесь списка чатов", 409, "scanning")
    if upload.state == "done" or not upload.has_file():
        raise error("файл уже импортирован или удалён — загрузите его заново", 409, "no_file")


@confirm.applier(IMPORT_RUN)
async def _apply_import_run(conn: asyncpg.Connection, payload: dict[str, Any]) -> confirm.Done:
    confirm.must_not_widen(True)      # импорт всегда добавляет в архив: без владельца не выполняется
    state = _state
    registry: Registry | None = state.extras.get("imports") if state is not None else None
    upload = registry.items.get(str(payload.get("import_id"))) if registry is not None else None
    if state is None or upload is None:
        raise confirm.Refused(
            "загрузка не найдена — возможно, сервис перезапускался; загрузите файл заново", 404)
    _check_runnable(registry, upload, confirm.Refused)
    try:
        exclude = _parse_exclude(payload.get("exclude"))
    except BadRequest as exc:
        raise confirm.Refused(exc.message, 400) from None
    owner_id = payload.get("owner_id")
    upload.state, upload.error, upload.stats, upload.live = "running", None, None, None
    upload.started_at, upload.finished_at, upload.reader = datetime.now(timezone.utc), None, None
    upload.run_task = state.spawn(_run(state, upload, exclude, owner_id), name=f"import-{upload.id}")
    return confirm.Done(note="Импорт запущен.", result=upload.view())


@_handler
async def delete_import(request: Request) -> JSONResponse:
    """Удаляет загрузку и её файл. Идущий импорт останавливается; уже записанное остаётся."""
    upload = _upload(request)
    was_running = upload.run_task is not None
    for task in (upload.run_task, upload.scan_task):
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    _registry(request).forget(upload)
    return JSONResponse({"deleted": True, "was_running": was_running})


def routes() -> list[BaseRoute]:
    return [
        Route("/api/ingest/business/connection", business_connection, methods=["POST"]),
        Route("/api/ingest/business/message", business_message, methods=["POST"]),
        Route("/api/ingest/business/deleted", business_deleted, methods=["POST"]),
        Route("/api/chats", list_chats, methods=["GET"]),
        Route("/api/chats/{chat_id:int}/excluded", put_chat_excluded, methods=["PUT"]),
        Route("/api/imports", upload_export, methods=["POST"]),
        Route("/api/imports", list_imports, methods=["GET"]),
        Route("/api/imports/{import_id}", import_status, methods=["GET"]),
        Route("/api/imports/{import_id}", delete_import, methods=["DELETE"]),
        Route("/api/imports/{import_id}/scan", scan_import, methods=["GET"]),
        Route("/api/imports/{import_id}/run", run_import, methods=["POST"]),
    ]


@contextlib.asynccontextmanager
async def lifespan(state: AppState) -> AsyncIterator[None]:
    directory = state.config.uploads_dir
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    registry = Registry(directory=directory, max_bytes=_upload_limit())
    removed = _sweep(directory)
    if removed:
        logger.info("удалены файлы загрузок прошлого запуска: %s", removed)
    state.extras["imports"] = registry
    global _state
    _state = state

    async def janitor() -> None:
        while True:
            await asyncio.sleep(JANITOR_EVERY)
            registry.expire(datetime.now(timezone.utc))

    state.spawn(janitor(), name="imports-janitor")
    try:
        yield
    finally:
        if _state is state:
            _state = None
        # Фоновые работы к этому моменту остановлены. Файлы без записей о них не нужны.
        for upload in list(registry.items.values()):
            if upload.reader is not None:
                upload.reader.stop = True
        _sweep(directory)
