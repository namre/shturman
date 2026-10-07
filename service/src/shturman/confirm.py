"""Подтверждение владельцем действий, которые расширяют права ассистента или стирают данные.

Зачем. Внутренний API сервиса доступен плагину в Hermes, а значит и ассистенту, у которого там
терминал. Чтобы внедрённая в чужое сообщение инструкция не могла, например, включить автоответ
или стереть чат, такие действия не применяются сразу: сервис показывает владельцу карточку
в своём боте согласований и ждёт нажатия. Нажатие приходит сервису напрямую от Telegram,
мимо Hermes, поэтому подделать его ассистент не может.

Если своего бота у сервиса нет (всё идёт через плагин), подтверждение было бы видимостью:
нажатие шло бы через тот же Hermes. В этом режиме действие применяется сразу, а отправка
сообщений остаётся выключенной (см. config.sending).

Модуль регистрирует, как применить действие:

    @confirm.applier("outbox.trusted_add")
    async def _apply(conn, payload) -> str | None:      # текст-итог для владельца или None
        ...

и запрашивает подтверждение из обработчика маршрута:

    out = await confirm.request(conn, "outbox.trusted_add",
                                "Добавить Анну Ким (id 2044) в доверенные для автоответа",
                                {"tg_user_id": 2044})
    # {"status": "applied", ...} либо {"status": "pending_confirmation", "action_id": 7, ...}

Текст `summary` показывается владельцу как есть: вызывающий отвечает за то, что он короткий,
однозначный и что имена из переписки прошли чистку (sanitize.clean_line).
"""

from __future__ import annotations

import hmac
import json
import secrets
from typing import Any, Awaitable, Callable

import asyncpg

from . import bridge

TTL = 3600                 # секунд на решение; потом действие снимается
CALLBACK_MODULE = "cf"
CARD_HANDLER = "confirm.card"
MAX_PENDING = 20           # больше одновременно ждущих карточек не создаём: защита от засыпания владельца

Applier = Callable[[asyncpg.Connection, dict[str, Any]], Awaitable[str | None]]
_appliers: dict[str, Applier] = {}


class TooManyPending(Exception):
    """Слишком много действий уже ждут решения владельца."""


def applier(kind: str) -> Callable[[Applier], Applier]:
    def deco(fn: Applier) -> Applier:
        _appliers[kind] = fn
        return fn
    return deco


def required() -> bool:
    """Нужно ли ждать нажатия владельца: только когда у сервиса свой бот."""
    return bridge.owns_bot()


def _card(summary: str) -> str:
    return ("Подтвердите действие\n\n" + summary.strip() + "\n\n"
            "Запрос пришёл из кабинета или от ассистента. Если вы этого не просили — нажмите «Нет».")


async def request(conn: asyncpg.Connection, kind: str, summary: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Применяет действие сразу либо ставит его ждать нажатия владельца."""
    fn = _appliers.get(kind)
    if fn is None:
        raise KeyError(f"нет обработчика для действия {kind}")
    if not required():
        async with conn.transaction():
            note = await fn(conn, payload)
        return {"status": "applied", "note": note}
    async with conn.transaction():
        waiting = await conn.fetchval("SELECT count(*) FROM pending_actions WHERE status = 'pending'")
        if waiting >= MAX_PENDING:
            raise TooManyPending()
        nonce = secrets.token_urlsafe(9)
        row = await conn.fetchrow(
            """INSERT INTO pending_actions (kind, summary, payload, nonce, expires_at)
               VALUES ($1, $2, $3::jsonb, $4, now() + make_interval(secs => $5))
               RETURNING id, expires_at""",
            kind, summary[:1500], json.dumps(payload, ensure_ascii=False), nonce, float(TTL),
        )
        await bridge.notify_owner(
            conn, _card(summary[:1500]),
            buttons=[[bridge.button("Да, сделать", CALLBACK_MODULE, f"y:{row['id']}:{nonce}"),
                      bridge.button("Нет", CALLBACK_MODULE, f"n:{row['id']}:{nonce}")]],
            handler=CARD_HANDLER, context={"action_id": row["id"]},
        )
    return {"status": "pending_confirmation", "action_id": row["id"],
            "expires_at": row["expires_at"].isoformat(),
            "note": "Ждёт вашего подтверждения в боте согласований."}


@bridge.on_result(CARD_HANDLER)
async def _card_sent(conn: asyncpg.Connection, job: dict[str, Any], result: dict[str, Any]) -> None:
    message_id = result.get("message_id")
    if isinstance(message_id, int):
        await conn.execute("UPDATE pending_actions SET card_message_id = $2 WHERE id = $1",
                           job["context"].get("action_id"), message_id)


@bridge.on_failure(CARD_HANDLER)
async def _card_failed(conn: asyncpg.Connection, job: dict[str, Any], error: str) -> None:
    # Карточка не дошла — действие не может быть подтверждено; снимаем, чтобы оно не висело.
    await conn.execute(
        """UPDATE pending_actions SET status = 'failed', error = 'карточка не доставлена', decided_at = now()
           WHERE id = $1 AND status = 'pending'""", job["context"].get("action_id"))


@bridge.on_callback(CALLBACK_MODULE)
async def _pressed(conn: asyncpg.Connection, rest: str, user_id: int) -> dict[str, Any]:
    gone = {"answer": "Действие уже недоступно.", "edit_text": None, "remove_buttons": True}
    choice, _, tail = rest.partition(":")
    raw_id, _, nonce = tail.partition(":")
    if choice not in ("y", "n") or not raw_id.isdigit():
        return gone
    row = await conn.fetchrow("SELECT * FROM pending_actions WHERE id = $1 FOR UPDATE", int(raw_id))
    if row is None or row["status"] != "pending" or not hmac.compare_digest(row["nonce"], nonce):
        return gone
    expired = await conn.fetchval("SELECT $1::timestamptz <= now()", row["expires_at"])
    if expired:
        await conn.execute("UPDATE pending_actions SET status = 'expired', decided_at = now() WHERE id = $1", row["id"])
        return {"answer": "Срок вышел.", "edit_text": "Срок подтверждения вышел:\n" + row["summary"],
                "remove_buttons": True}
    if choice == "n":
        await conn.execute("UPDATE pending_actions SET status = 'rejected', decided_at = now() WHERE id = $1", row["id"])
        return {"answer": "Отклонено.", "edit_text": "Отклонено:\n" + row["summary"], "remove_buttons": True}
    fn = _appliers.get(row["kind"])
    payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
    try:
        if fn is None:
            raise RuntimeError("обработчик действия не найден")
        async with conn.transaction():
            note = await fn(conn, payload)
    except Exception as exc:  # действие не применилось — владелец должен это увидеть
        await conn.execute(
            "UPDATE pending_actions SET status = 'failed', error = $2, decided_at = now() WHERE id = $1",
            row["id"], type(exc).__name__)
        return {"answer": "Не получилось.", "edit_text": "Не получилось выполнить:\n" + row["summary"],
                "remove_buttons": True}
    await conn.execute("UPDATE pending_actions SET status = 'applied', decided_at = now() WHERE id = $1", row["id"])
    text = "Сделано:\n" + row["summary"] + (f"\n\n{note}" if note else "")
    return {"answer": "Сделано.", "edit_text": text, "remove_buttons": True}


async def cancel(conn: asyncpg.Connection, action_id: int) -> bool:
    """Снимает ждущее действие (отказаться можно и без бота: это ничего не расширяет)."""
    row = await conn.fetchrow(
        """UPDATE pending_actions SET status = 'rejected', decided_at = now()
           WHERE id = $1 AND status = 'pending' RETURNING summary, card_message_id""", action_id)
    if row is None:
        return False
    if row["card_message_id"]:
        await bridge.edit_owner_message(conn, row["card_message_id"], "Отменено:\n" + row["summary"])
    return True


async def expire(conn: asyncpg.Connection) -> int:
    rows = await conn.fetch(
        """UPDATE pending_actions SET status = 'expired', decided_at = now()
           WHERE status = 'pending' AND expires_at <= now() RETURNING summary, card_message_id""")
    for row in rows:
        if row["card_message_id"]:
            await bridge.edit_owner_message(
                conn, row["card_message_id"], "Срок подтверждения вышел:\n" + row["summary"])
    return len(rows)


async def list_pending(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """SELECT id, kind, summary, created_at, expires_at FROM pending_actions
           WHERE status = 'pending' ORDER BY id""")
    return [{"id": r["id"], "kind": r["kind"], "summary": r["summary"],
             "created_at": r["created_at"].isoformat(), "expires_at": r["expires_at"].isoformat()} for r in rows]
