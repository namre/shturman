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
    async def _apply(conn, payload) -> str | Done | None:   # текст-итог для владельца, Done или None
        ...

и запрашивает подтверждение из обработчика маршрута:

    out = await confirm.request(conn, "outbox.trusted_add",
                                "Добавить Анну Ким (id 2044) в доверенные для автоответа",
                                {"tg_user_id": 2044})
    # {"status": "applied", ...} либо {"status": "pending_confirmation", "action_id": 7, ...}

В маршрутах удобнее обёртка `api_core.settle`: она же превращает ожидание в ответ 202.

Текст `summary` показывается владельцу как есть: вызывающий отвечает за то, что он короткий,
однозначный и что имена из переписки прошли чистку (sanitize.clean_line).

Правила для модулей, которые переводят свои маршруты на подтверждение:

  * ждёт подтверждения только то, что расширяет возможности ассистента (отправлять, видеть),
    ослабляет ограничение или необратимо стирает данные. Ужесточение (выключить, убрать,
    понизить предел, исключить чат) применяется сразу — `apply`: иначе владелец не смог бы
    быстро «закрыть кран». Что из двух перед нами, решается по значению, а не по имени маршрута;
  * функция применения заново проверяет всё, что могло измениться за время ожидания, и при
    отказе бросает `Refused` с текстом для владельца;
  * решение «это ужесточение» маршрут принимает по прочитанному заранее состоянию, поэтому
    функция применения повторяет его под блокировкой: `must_not_widen(расширяет ли на самом деле)`.
    Без этого запрос, повторяемый в цикле, мог бы отменить ужесточение, которое владелец сделал
    в тот же миг;
  * функция может вернуть `Done`: строку для карточки, результат для ответа маршрута и работу
    «после фиксации транзакции» (события, запуск фоновой задачи);
  * повторный запрос того же вида с тем же содержимым новую карточку не создаёт, пока прежняя ждёт.
"""

from __future__ import annotations

import contextvars
import hmac
import json
import logging
import secrets
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import asyncpg

from . import bridge

logger = logging.getLogger("shturman.confirm")

PENDING = "pending_confirmation"
APPLIED = "applied"
TTL = 3600                 # секунд на решение; потом действие снимается
CALLBACK_MODULE = "cf"
CARD_HANDLER = "confirm.card"
MAX_PENDING = 20           # больше одновременно ждущих карточек не создаём: защита от засыпания владельца
SCRUB_AFTER = 600          # секунд после решения: дальше содержимое действия (payload) стирается
SUMMARY_LIMIT = 3000      # знаков в описании действия на карточке (сообщение Telegram — до 4096)

After = Callable[[], Awaitable[None]]


@dataclass
class Done:
    """Итог применения действия."""

    note: str | None = None     # строка для владельца: дописывается в карточку после «Сделано»
    result: Any = None          # что маршрут вернёт вызывающему, если действие применилось сразу
    after: After | None = None  # выполнить после фиксации транзакции (события, фоновые задачи)


Applier = Callable[[asyncpg.Connection, dict[str, Any]], Awaitable["str | Done | None"]]
_appliers: dict[str, Applier] = {}
# Истина, пока действие применяется по нажатию под карточкой: отдельное сообщение владельцу
# об изменении тогда не нужно — итог уже написан в самой карточке.
_from_card: contextvars.ContextVar[bool] = contextvars.ContextVar("shturman_confirm_from_card", default=False)


# Истина, пока действие применяется без нажатия владельца, хотя у сервиса свой бот (маршрут счёл
# его ужесточением). Функция применения в этом случае обязана сама, под своей блокировкой,
# убедиться, что ничего не расширяет: см. `must_not_widen`.
_unconfirmed: contextvars.ContextVar[bool] = contextvars.ContextVar("shturman_confirm_unconfirmed", default=False)


class TooManyPending(Exception):
    """Слишком много действий уже ждут решения владельца."""


class NoOwner(Exception):
    """Свой бот есть, а владелец к нему не привязан: показать карточку некому."""


class Refused(Exception):
    """Действие применить нельзя. Текст — для владельца (в карточке) и для ответа маршрута."""

    def __init__(self, message: str, status: int = 409, code: str | None = None,
                 extra: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message, self.status, self.code, self.extra = message, status, code, extra or {}


def applier(kind: str) -> Callable[[Applier], Applier]:
    def deco(fn: Applier) -> Applier:
        _appliers[kind] = fn
        return fn
    return deco


def kinds() -> frozenset[str]:
    """Все виды действий, для которых есть функция применения."""
    return frozenset(_appliers)


def required() -> bool:
    """Нужно ли ждать нажатия владельца: только когда у сервиса свой бот."""
    return bridge.owns_bot()


def _card(summary: str) -> str:
    return ("Подтвердите действие\n\n" + summary.strip() + "\n\n"
            "Запрос пришёл из кабинета или от ассистента. Если вы этого не просили — нажмите «Нет».")


def _done(value: "str | Done | None") -> Done:
    return value if isinstance(value, Done) else Done(note=value)


async def _run_after(after: After | None) -> None:
    if after is None:
        return
    try:
        await after()
    except Exception as exc:  # действие уже применено; сбой «после» не должен выглядеть как отказ
        logger.error("действие применено, но работа после него не выполнена (%s)", type(exc).__name__)


async def tell(conn: asyncpg.Connection, text: str, *, silent: bool = False) -> None:
    """Сообщение владельцу об изменении настроек. По нажатию под карточкой не отправляется:
    итог владелец уже видит в самой карточке."""
    if not _from_card.get():
        await bridge.notify_owner(conn, text, silent=silent)


def unconfirmed() -> bool:
    """Действие применяется без нажатия владельца, хотя у сервиса свой бот: можно только ужесточать."""
    return _unconfirmed.get()


def must_not_widen(widens: bool) -> None:
    """Вызывается функцией применения там, где она уже держит блокировку и видит нынешнее состояние.

    Маршрут решает «ужесточение это или ослабление» по состоянию, которое прочитал до применения.
    Между чтением и записью состояние могло измениться (владелец как раз ужесточил настройку), и
    тогда «ужесточение» оказалось бы ослаблением, применённым без владельца. Поэтому при своём
    боте действие, применяемое без нажатия, проверяется ещё раз здесь — и отклоняется."""
    if widens and _unconfirmed.get():
        raise Refused("Настройки изменились, пока запрос обрабатывался. Повторите запрос.", 409, "changed_meanwhile")


async def apply(conn: asyncpg.Connection, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Применяет действие сразу, без подтверждения: для ужесточений и когда своего бота нет.

    Вызывать вне транзакции: работа «после фиксации» выполняется сразу за транзакцией действия."""
    fn = _appliers.get(kind)
    if fn is None:
        raise KeyError(f"нет обработчика для действия {kind}")
    token = _unconfirmed.set(required())
    try:
        async with conn.transaction():
            done = _done(await fn(conn, payload))
    finally:
        _unconfirmed.reset(token)
    await _run_after(done.after)
    return {"status": APPLIED, "note": done.note, "result": done.result}


async def apply_owner(conn: asyncpg.Connection, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Применяет действие, которое совершил сам владелец каналом, недоступным ассистенту, —
    на странице настройки сервиса (`setup_page/`), куда входят по одноразовой ссылке с сервера.

    Карточка в боте для такого действия не нужна: подтверждение защищает от держателя токена
    внутреннего API, а здесь вход свой. Поэтому действие применяется сразу и считается
    подтверждённым (`unconfirmed()` — ложь), а отдельное сообщение владельцу об изменении
    не отправляется: он сам его только что сделал, запись остаётся в журнале страницы.

    Вызывать только из маршрутов страницы настройки, после проверки её сессии. Вне транзакции.
    """
    fn = _appliers.get(kind)
    if fn is None:
        raise KeyError(f"нет обработчика для действия {kind}")
    confirmed, quiet = _unconfirmed.set(False), _from_card.set(True)
    try:
        async with conn.transaction():
            done = _done(await fn(conn, payload))
    finally:
        _from_card.reset(quiet)
        _unconfirmed.reset(confirmed)
    await _run_after(done.after)
    return {"status": APPLIED, "note": done.note, "result": done.result}


def _waiting(row: Any, *, duplicate: bool) -> dict[str, Any]:
    return {"status": PENDING, "action_id": row["id"], "summary": row["summary"],
            "expires_at": row["expires_at"].isoformat(), "duplicate": duplicate,
            "note": "Ждёт вашего подтверждения в боте согласований."}


async def request(conn: asyncpg.Connection, kind: str, summary: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Применяет действие сразу либо ставит его ждать нажатия владельца."""
    if kind not in _appliers:
        raise KeyError(f"нет обработчика для действия {kind}")
    if not required():
        return await apply(conn, kind, payload)
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    summary = summary.strip()
    if not summary:
        raise ValueError(f"действие {kind}: владельцу нечего показать — нужно описание простыми словами")
    if len(summary) > SUMMARY_LIMIT:
        summary = summary[:SUMMARY_LIMIT - 1] + "…"
    async with conn.transaction():
        # Один и тот же запрос, пришедший дважды (в том числе одновременно), — одна карточка.
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"shturman.confirm:{kind}:{body}")
        same = await conn.fetchrow(
            """SELECT id, summary, expires_at FROM pending_actions
               WHERE status = 'pending' AND kind = $1 AND payload = $2::jsonb AND expires_at > now()
               ORDER BY id LIMIT 1""", kind, body)
        if same is not None:
            return _waiting(same, duplicate=True)
        if await bridge.get_owner(conn) is None:
            raise NoOwner()
        waiting = await conn.fetchval(
            "SELECT count(*) FROM pending_actions WHERE status = 'pending' AND expires_at > now()")
        if waiting >= MAX_PENDING:
            raise TooManyPending()
        nonce = secrets.token_urlsafe(9)
        row = await conn.fetchrow(
            """INSERT INTO pending_actions (kind, summary, payload, nonce, expires_at)
               VALUES ($1, $2, $3::jsonb, $4, now() + make_interval(secs => $5))
               RETURNING id, summary, expires_at""",
            kind, summary, body, nonce, float(TTL),
        )
        await bridge.notify_owner(
            conn, _card(summary),
            buttons=[[bridge.button("Да, сделать", CALLBACK_MODULE, f"y:{row['id']}:{nonce}"),
                      bridge.button("Нет", CALLBACK_MODULE, f"n:{row['id']}:{nonce}")]],
            handler=CARD_HANDLER, context={"action_id": row["id"]},
        )
    return _waiting(row, duplicate=False)


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
    token = _from_card.set(True)
    try:
        if fn is None:
            raise RuntimeError("обработчик действия не найден")
        async with conn.transaction():
            done = _done(await fn(conn, payload))
    except Exception as exc:  # действие не применилось — владелец должен это увидеть
        # Причина показывается, только если её написал сам модуль: в тексте посторонней ошибки
        # может оказаться что угодно, в том числе кусок переписки.
        reason = exc.message if isinstance(exc, Refused) else None
        if reason is None:
            logger.warning("подтверждённое действие %s не применилось (%s)", row["kind"], type(exc).__name__)
        await conn.execute(
            "UPDATE pending_actions SET status = 'failed', error = $2, decided_at = now() WHERE id = $1",
            row["id"], (reason or type(exc).__name__)[:500])
        return {"answer": "Не получилось.", "remove_buttons": True,
                "edit_text": "Не получилось выполнить:\n" + row["summary"]
                             + (f"\n\nПричина: {reason}" if reason else "")}
    finally:
        _from_card.reset(token)
    await conn.execute("UPDATE pending_actions SET status = 'applied', decided_at = now() WHERE id = $1", row["id"])
    text = "Сделано:\n" + row["summary"] + (f"\n\n{done.note}" if done.note else "")
    return {"answer": "Сделано.", "edit_text": text, "remove_buttons": True, "after_commit": done.after}


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
    # Содержимое решённых действий больше не нужно: остаются вид, описание и исход.
    await conn.execute(
        """UPDATE pending_actions SET payload = '{}'::jsonb
           WHERE status <> 'pending' AND payload <> '{}'::jsonb
             AND COALESCE(decided_at, created_at) < now() - make_interval(secs => $1)""", float(SCRUB_AFTER))
    return len(rows)


async def get(conn: asyncpg.Connection, action_id: int) -> dict[str, Any] | None:
    """Что стало с действием: pending, applied, rejected, expired или failed. Содержимого нет."""
    r = await conn.fetchrow(
        """SELECT id, kind, summary, error, created_at, expires_at, decided_at,
                  CASE WHEN status = 'pending' AND expires_at <= now() THEN 'expired' ELSE status END AS status
           FROM pending_actions WHERE id = $1""", action_id)
    if r is None:
        return None
    return {"id": r["id"], "kind": r["kind"], "status": r["status"], "summary": r["summary"],
            "error": r["error"] if r["status"] == "failed" else None,
            "created_at": r["created_at"].isoformat(), "expires_at": r["expires_at"].isoformat(),
            "decided_at": r["decided_at"].isoformat() if r["decided_at"] else None}


async def list_pending(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """SELECT id, kind, summary, created_at, expires_at FROM pending_actions
           WHERE status = 'pending' ORDER BY id""")
    return [{"id": r["id"], "kind": r["kind"], "summary": r["summary"],
             "created_at": r["created_at"].isoformat(), "expires_at": r["expires_at"].isoformat()} for r in rows]
