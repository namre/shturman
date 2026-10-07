"""Маршруты и фоновая работа шлюза отправки.

Агент через эти маршруты может только СОЗДАТЬ черновик. Отправка происходит после нажатия
владельца под карточкой либо по правилу автоответа, которое включил владелец. О каждом
включении автоответа, изменении списка доверенных и правил отправки владелец получает сообщение.

Изменения настроек идут через `confirm` (см. его шапку). Когда у сервиса свой бот согласований,
всё, что расширяет возможности ассистента, ждёт нажатия владельца в этом боте, и маршрут
отвечает 202:

  ждёт владельца                                   применяется сразу
  -----------------------------------------------  ------------------------------------------
  ослабление общих правил (policy.LOOSER)          ужесточение общих правил
  черновики: «нельзя» → «как в общих» → «можно»    то же в обратную сторону
  включение автоответа; ослабление его настроек;   выключение автоответа; ужесточение настроек
    любая правка текста представления
  добавление в доверенные                          удаление из доверенных
  новое правило наблюдателя; правка, которая        выключение правила, сокращение его чатов,
    включает правило, добавляет чаты, слова или      слов и пределов
    выражения, меняет название или описание,
    поднимает пределы; удаление правила с журналом
  —                                                создание и отмена черновика

Если в одном запросе правил или автоответа есть и то и другое, ужесточения применяются сразу,
а ослабления ждут (в ответе 202 — поле applied_now). Правка правила наблюдателя не делится:
правило проверяется целиком.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, AsyncIterator

import asyncpg
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from .. import bridge, confirm, store
from ..api_core import BadRequest, body, handler, need_int, settle
from ..app import AppState, state_of
from ..events import CHAT_EXCLUDED, MESSAGE_LIVE
from . import autoreply, drafts, policy, runtime, watcher
from . import text as textlib

logger = logging.getLogger("shturman.outbox")

SWEEP_EVERY = 30        # секунд между обходами просроченного и зависшего
MAX_TEXT = 20_000       # знаков в тексте черновика; длиннее — отказ, а не обрезание
CHANNELS = (None, "auto", "business", "session")


def _pool(request: Request) -> asyncpg.Pool:
    return state_of(request).pool


def _query_int(request: Request, key: str) -> int | None:
    raw = request.query_params.get(key)
    if raw is None or raw == "":
        return None
    if not raw.isascii() or not raw.lstrip("-").isdigit() or len(raw) > 19:
        raise BadRequest(f"параметр {key}: нужно целое число")
    return int(raw)


def _limit(request: Request, default: int = 50) -> int:
    return max(1, min(_query_int(request, "limit") or default, 200))


def _checked(fn, data: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    try:
        return fn(data, **kwargs)
    except ValueError as exc:
        raise BadRequest(str(exc)) from None


def _changes(before: dict[str, Any], after: dict[str, Any]) -> str:
    return "; ".join(f"{k}: {before.get(k)} → {v}" for k, v in after.items() if before.get(k) != v)


# --- черновики ---

@handler
async def create_draft(request: Request) -> JSONResponse:
    data = await body(request)
    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        raise BadRequest("поле text: нужна непустая строка")
    if len(text) > MAX_TEXT:
        raise BadRequest(f"поле text: длиннее {MAX_TEXT} знаков — сократите", status=422)
    channel = data.get("channel")
    if channel not in CHANNELS:
        raise BadRequest("поле channel: допустимо business, session или auto")
    reply_to = data.get("reply_to_message_id")
    if reply_to is not None:
        reply_to = need_int(data, "reply_to_message_id")
    key = data.get("idempotency_key")
    if key is not None and (not isinstance(key, str) or not 8 <= len(key) <= 200):
        raise BadRequest("поле idempotency_key: нужна строка от 8 до 200 знаков")
    state = state_of(request)
    async with state.pool.acquire() as conn:
        try:
            out = await drafts.create(
                conn, state, chat_id=need_int(data, "chat_id"), text=text,
                channel=None if channel == "auto" else channel,
                reply_to_message_id=reply_to, idempotency_key=key)
        except drafts.Refused as exc:
            return JSONResponse(exc.decision.as_dict(), status_code=exc.status)
    return JSONResponse(out)


@handler
async def list_drafts(request: Request) -> JSONResponse:
    status, origin = request.query_params.get("status"), request.query_params.get("origin")
    if status is not None and status not in drafts.STATUS_WORDS:
        raise BadRequest("параметр status: такого состояния нет")
    if origin is not None and origin not in ("agent", "autoreply"):
        raise BadRequest("параметр origin: допустимо agent или autoreply")
    async with state_of(request).ro_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM outbox_drafts
               WHERE ($1::text IS NULL OR status = $1) AND ($2::bigint IS NULL OR chat_id = $2)
                 AND ($3::text IS NULL OR origin = $3)
               ORDER BY id DESC LIMIT $4""",
            status, _query_int(request, "chat_id"), origin, _limit(request))
    return JSONResponse({"drafts": [drafts.public(r) for r in rows]})


@handler
async def cancel_draft(request: Request) -> JSONResponse:
    async with _pool(request).acquire() as conn:
        out = await drafts.cancel(conn, int(request.path_params["draft_id"]))
    if out is None:
        raise BadRequest("такого черновика нет", status=404)
    if not out["cancelled"]:
        return JSONResponse({**out, "error": "черновик уже не ждёт решения — отменить нельзя"}, status_code=409)
    return JSONResponse(out)


# --- правила отправки ---

async def _policy_view(conn: asyncpg.Connection, config: Any) -> dict[str, Any]:
    accounts = await conn.fetch(
        """SELECT a.id AS account_id, a.label, a.role, o.drafting_default
           FROM accounts a LEFT JOIN outbox_accounts o ON o.account_id = a.id ORDER BY a.id""")
    chats = await conn.fetch("SELECT chat_id, drafting FROM outbox_chats ORDER BY chat_id")
    rules = await policy.load(conn, config)
    return {
        # Эти два значения задаются только окружением сервиса и через API не меняются.
        "sending": rules.pop("sending"),
        "hard_daily_cap": rules.pop("hard_daily_cap"),
        # daily_cap — действующий предел (не выше потолка сервера); daily_cap_stored — сохранённая настройка
        "policy": rules,
        "limits": {k: {"default": v[0], "min": v[1], "max": v[2]} for k, v in policy.NUMBERS.items()},
        "accounts": [dict(r) for r in accounts],
        "chats": [dict(r) for r in chats],
    }


@handler
async def get_policy(request: Request) -> JSONResponse:
    async with _pool(request).acquire() as conn:
        return JSONResponse(await _policy_view(conn, state_of(request).config))


async def _lock(conn: asyncpg.Connection, what: str) -> None:
    """Блокировка до конца транзакции: решение «расширяет или нет» и запись идут без разрыва."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"shturman.settings:{what}")


POLICY = "outbox.policy"
ACCOUNT_DRAFTING = "outbox.account_drafting"
CHAT_DRAFTING = "outbox.chat_drafting"
AUTOREPLY = "outbox.autoreply"
TRUSTED_ADD = "outbox.trusted_add"
RULE_CREATE = "watch.rule_create"
RULE_UPDATE = "watch.rule_update"
RULE_DELETE = "watch.rule_delete"
_STILL_BY_HAND = "Отправка по-прежнему только после вашего нажатия под каждым черновиком."


@confirm.applier(POLICY)
async def _apply_policy(conn: asyncpg.Connection, payload: dict[str, Any]) -> None:
    try:
        changes = policy.validate_update(dict(payload.get("changes") or {}))
    except ValueError as exc:
        raise confirm.Refused(str(exc), 400) from None
    await _lock(conn, policy.SETTINGS_KEY)
    before = await policy.stored(conn)
    confirm.must_not_widen(bool(policy.split_changes(before, changes, policy.LOOSER)[1]))
    await policy.update(conn, changes)
    note = _changes(before, changes)
    if note:
        await confirm.tell(conn, f"Изменены правила отправки сообщений. {note}", silent=True)


@confirm.applier(ACCOUNT_DRAFTING)
async def _apply_account_drafting(conn: asyncpg.Connection, payload: dict[str, Any]) -> None:
    account_id, value = int(payload["account_id"]), payload.get("value")
    if value not in ("allow", "deny", None):
        raise confirm.Refused("для аккаунта задаётся только drafting_default: allow, deny или null", 400)
    label = await conn.fetchval("SELECT label FROM accounts WHERE id = $1", account_id)
    if label is None:
        raise confirm.Refused("такого аккаунта нет", 404)
    await _lock(conn, f"outbox.account:{account_id}")
    current = await conn.fetchval("SELECT drafting_default FROM outbox_accounts WHERE account_id = $1", account_id)
    confirm.must_not_widen(policy.loosens("drafting_default", current, value, policy.LOOSER))
    await conn.execute(
        """INSERT INTO outbox_accounts (account_id, drafting_default) VALUES ($1, $2)
           ON CONFLICT (account_id) DO UPDATE SET drafting_default = EXCLUDED.drafting_default,
                                                  updated_at = now()""", account_id, value)
    await confirm.tell(
        conn, f"Изменены правила отправки сообщений. аккаунт «{textlib.one_line(label, 40)}»: "
              f"черновики по умолчанию — {value or 'как в общих правилах'}", silent=True)


@confirm.applier(CHAT_DRAFTING)
async def _apply_chat_drafting(conn: asyncpg.Connection, payload: dict[str, Any]) -> None:
    chat_id, value = int(payload["chat_id"]), payload.get("value")
    if value not in ("allow", "deny", "default"):
        raise confirm.Refused("поле drafting: допустимо allow, deny или default", 400)
    tgt = await policy.target(conn, chat_id)
    if tgt is None:
        raise confirm.Refused("такого чата нет", 404)
    await _lock(conn, f"outbox.chat:{chat_id}")
    current = await conn.fetchval("SELECT drafting FROM outbox_chats WHERE chat_id = $1", chat_id)
    confirm.must_not_widen(policy.loosens("drafting_default", current, value, policy.LOOSER))
    if value == "default":
        await conn.execute("DELETE FROM outbox_chats WHERE chat_id = $1", chat_id)
    else:
        await conn.execute(
            """INSERT INTO outbox_chats (chat_id, drafting) VALUES ($1, $2)
               ON CONFLICT (chat_id) DO UPDATE SET drafting = EXCLUDED.drafting, updated_at = now()""",
            chat_id, value)
    words = {"allow": "разрешены", "deny": "запрещены", "default": "как в общих правилах"}
    await confirm.tell(conn, f"Черновики сообщений в чат «{tgt.display_name}»: {words[value]}.", silent=True)


@handler
async def put_policy(request: Request) -> JSONResponse:
    """Меняет общие правила. С полем account_id — умолчание «можно ли готовить черновики» для аккаунта."""
    data = await body(request)
    async with _pool(request).acquire() as conn:
        if "account_id" in data:
            account_id = need_int(data, "account_id")
            value = data.get("drafting_default")
            if value not in ("allow", "deny", None) or set(data) - {"account_id", "drafting_default"}:
                raise BadRequest("для аккаунта задаётся только drafting_default: allow, deny или null")
            row = await conn.fetchrow(
                """SELECT a.label, o.drafting_default FROM accounts a
                   LEFT JOIN outbox_accounts o ON o.account_id = a.id WHERE a.id = $1""", account_id)
            if row is None:
                raise BadRequest("такого аккаунта нет", status=404)
            summary = None
            if policy.loosens("drafting_default", row["drafting_default"], value, policy.LOOSER):
                name = textlib.one_line(row["label"], 40)
                summary = (
                    f"Разрешить ассистенту готовить черновики сообщений во всех чатах аккаунта «{name}»."
                    if value == "allow" else
                    f"Снять запрет на черновики сообщений для аккаунта «{name}»: будут действовать общие правила."
                ) + " " + _STILL_BY_HAND
            answer, _ = await settle(
                conn, ACCOUNT_DRAFTING, {"account_id": account_id, "value": value}, summary=summary)
            if answer is not None:
                return answer
        else:
            changes = _checked(policy.validate_update, data)
            before = await policy.stored(conn)
            now, later = policy.split_changes(before, changes, policy.LOOSER)
            if now:
                await settle(conn, POLICY, {"changes": now}, summary=None)
            if later:
                answer, _ = await settle(
                    conn, POLICY, {"changes": later}, applied_now=now or None,
                    summary="Ослабить правила отправки сообщений:\n"
                            + policy.describe_changes(before, later, policy.LABELS)
                            + "\nАссистент сможет готовить и отправлять сообщения свободнее, чем сейчас.")
                if answer is not None:
                    return answer
        return JSONResponse(await _policy_view(conn, state_of(request).config))


@handler
async def put_chat(request: Request) -> JSONResponse:
    data = await body(request)
    chat_id = int(request.path_params["chat_id"])
    value = data.get("drafting")
    if value not in ("allow", "deny", "default"):
        raise BadRequest("поле drafting: допустимо allow, deny или default")
    async with _pool(request).acquire() as conn:
        tgt = await policy.target(conn, chat_id)
        if tgt is None:
            raise BadRequest("такого чата нет", status=404)
        own = await conn.fetchval("SELECT drafting FROM outbox_chats WHERE chat_id = $1", chat_id)
        summary = None
        if policy.loosens("drafting_default", own, value, policy.LOOSER):
            summary = (
                f"Разрешить ассистенту готовить черновики сообщений в чат «{tgt.display_name}»."
                if value == "allow" else
                f"Снять запрет на черновики сообщений в чат «{tgt.display_name}»: будут действовать общие правила."
            ) + " " + _STILL_BY_HAND
        answer, _ = await settle(conn, CHAT_DRAFTING, {"chat_id": chat_id, "value": value}, summary=summary)
        if answer is not None:
            return answer
        decision = await policy.check_target(conn, await policy.load(conn, state_of(request).config), tgt)
    return JSONResponse({"chat_id": chat_id, "drafting": value, "can_draft": decision.ok,
                         "reason": None if decision.ok else decision.code})


# --- автоответ и доверенные ---

async def _autoreply_view(conn: asyncpg.Connection, config: Any) -> dict[str, Any]:
    accounts = await conn.fetch(
        """SELECT a.id AS account_id, a.label, a.role, COALESCE(o.autoreply_enabled, false) AS enabled
           FROM accounts a LEFT JOIN outbox_accounts o ON o.account_id = a.id ORDER BY a.id""")
    return {
        # Главный выключатель отправки: только из окружения сервиса. Пока он выключен, автоответ не работает.
        "sending": getattr(config, "sending", False) is True,
        "outcomes_24h": await autoreply.outcomes(conn),
        "settings": await autoreply.load(conn),
        "limits": {k: {"default": v[0], "min": v[1], "max": v[2]} for k, v in autoreply.NUMBERS.items()},
        "accounts": [dict(r) for r in accounts],
        "trusted_count": await conn.fetchval("SELECT count(*) FROM outbox_trusted"),
    }


@handler
async def get_autoreply(request: Request) -> JSONResponse:
    async with _pool(request).acquire() as conn:
        return JSONResponse(await _autoreply_view(conn, state_of(request).config))


def _sending_on() -> bool:
    """Главный выключатель отправки — из настроек работающего сервиса."""
    mod = runtime.current()
    return mod is not None and getattr(mod.state.config, "sending", False) is True


@confirm.applier(AUTOREPLY)
async def _apply_autoreply(conn: asyncpg.Connection, payload: dict[str, Any]) -> None:
    """payload: {account_id, enabled}? + {changes}? — включение или выключение для аккаунта и общие настройки."""
    notes: list[str] = []
    if "enabled" in payload:
        account_id, enabled = int(payload["account_id"]), payload["enabled"] is True
        if enabled and not _sending_on():
            # Иначе автоответ, включённый заранее, заработал бы сам в момент включения отправки.
            raise confirm.Refused(policy.REASONS["sending_disabled"], 409, extra={"reason": "sending_disabled"})
        label = await conn.fetchval("SELECT label FROM accounts WHERE id = $1", account_id)
        if label is None:
            raise confirm.Refused("такого аккаунта нет", 404)
        await _lock(conn, f"outbox.account:{account_id}")
        was = await conn.fetchval(
            "SELECT autoreply_enabled FROM outbox_accounts WHERE account_id = $1", account_id)
        confirm.must_not_widen(enabled and not was)
        await conn.execute(
            """INSERT INTO outbox_accounts (account_id, autoreply_enabled) VALUES ($1, $2)
               ON CONFLICT (account_id) DO UPDATE SET autoreply_enabled = EXCLUDED.autoreply_enabled,
                                                      updated_at = now()""", account_id, enabled)
        if bool(was) != enabled:
            count = await conn.fetchval("SELECT count(*) FROM outbox_trusted")
            name = textlib.one_line(label, 40)
            notes.append(
                f"Автоответ доверенным ВКЛЮЧЁН для аккаунта «{name}». Доверенных в списке: {count}. "
                "Им сервис будет отвечать сам, без вашего подтверждения. Остальным не отвечает."
                if enabled else f"Автоответ доверенным выключен для аккаунта «{name}».")
    if payload.get("changes"):
        try:
            changes = autoreply.validate_update(dict(payload["changes"]))
        except ValueError as exc:
            raise confirm.Refused(str(exc), 400) from None
        await _lock(conn, autoreply.SETTINGS_KEY)
        before = await autoreply.load(conn)
        confirm.must_not_widen(bool(policy.split_changes(before, changes, autoreply.LOOSER)[1]))
        await autoreply.update(conn, changes)
        diff = _changes(before, changes)
        if diff:
            notes.append(f"Изменены настройки автоответа. {diff}")
    for note in notes:
        await confirm.tell(conn, note)


@handler
async def put_autoreply(request: Request) -> JSONResponse:
    """Включает или выключает автоответ для аккаунта (account_id + enabled) и меняет общие настройки."""
    data = await body(request)
    config = state_of(request).config
    async with _pool(request).acquire() as conn:
        now: dict[str, Any] = {}       # ужесточения: применяются сразу
        later: dict[str, Any] = {}     # ослабления: ждут владельца, если у сервиса свой бот
        asks: list[str] = []
        if "enabled" in data or "account_id" in data:
            account_id = need_int(data, "account_id")
            if not isinstance(data.get("enabled"), bool):
                raise BadRequest("поле enabled: нужно true или false")
            if data["enabled"] and config.sending is not True:
                return JSONResponse(policy.deny("sending_disabled").as_dict(), status_code=409)
            row = await conn.fetchrow(
                """SELECT a.label, COALESCE(o.autoreply_enabled, false) AS enabled FROM accounts a
                   LEFT JOIN outbox_accounts o ON o.account_id = a.id WHERE a.id = $1""", account_id)
            if row is None:
                raise BadRequest("такого аккаунта нет", status=404)
            switch = {"account_id": account_id, "enabled": data["enabled"]}
            if data["enabled"] and not row["enabled"]:
                later.update(switch)
                count = await conn.fetchval("SELECT count(*) FROM outbox_trusted")
                asks.append(
                    f"Включить автоответ доверенным для аккаунта «{textlib.one_line(row['label'], 40)}». "
                    f"Доверенных в списке: {count}. Этим людям ассистент будет отвечать сам, "
                    "без вашего согласования каждого ответа. Остальным он не отвечает.")
            else:
                now.update(switch)
        rest = {k: v for k, v in data.items() if k not in ("account_id", "enabled")}
        if rest:
            changes = _checked(autoreply.validate_update, rest)
            before = await autoreply.load(conn)
            tighter, looser = policy.split_changes(before, changes, autoreply.LOOSER)
            if tighter:
                now["changes"] = tighter
            if looser:
                later["changes"] = looser
                asks.append("Ослабить настройки автоответа доверенным:\n"
                            + policy.describe_changes(before, looser, autoreply.LABELS))
        if now:
            await settle(conn, AUTOREPLY, now, summary=None)
        if later:
            answer, _ = await settle(conn, AUTOREPLY, later, summary="\n\n".join(asks), applied_now=now or None)
            if answer is not None:
                return answer
        return JSONResponse(await _autoreply_view(conn, config))


async def _trusted_view(conn: asyncpg.Connection) -> dict[str, Any]:
    rows = await conn.fetch(
        """SELECT t.tg_user_id, t.note, t.created_at, p.name
           FROM outbox_trusted t LEFT JOIN peers p ON p.class = 'user' AND p.tg_id = t.tg_user_id
           ORDER BY t.created_at, t.tg_user_id""")
    return {"trusted": [{"tg_user_id": r["tg_user_id"], "note": r["note"], "name": r["name"],
                         "created_at": r["created_at"].isoformat()} for r in rows]}


def _trusted_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BadRequest("поле tg_user_id: доверенный задаётся только числовым идентификатором Telegram, "
                         "не именем пользователя")
    if value <= 0:
        raise BadRequest("поле tg_user_id: нужен идентификатор человека — положительное число")
    return value


@handler
async def get_trusted(request: Request) -> JSONResponse:
    async with _pool(request).acquire() as conn:
        return JSONResponse(await _trusted_view(conn))


@handler
async def add_trusted(request: Request) -> JSONResponse:
    data = await body(request)
    tg_user_id = _trusted_id(data.get("tg_user_id"))
    note = data.get("note")
    if note is not None and not isinstance(note, str):
        raise BadRequest("поле note: нужна строка")
    note = textlib.one_line(note, 200) if note else None
    async with _pool(request).acquire() as conn:
        problem, peer = await _trusted_problem(conn, tg_user_id)
        if problem:
            raise BadRequest(problem)
        if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM outbox_trusted WHERE tg_user_id = $1)", tg_user_id):
            return JSONResponse({**await _trusted_view(conn), "added": False})   # уже в списке: менять нечего
        name = textlib.one_line(peer["name"], 60) if peer is not None and peer["name"] else None
        who = (f"{name} (идентификатор Telegram {tg_user_id})" if name
               else f"человека с идентификатором Telegram {tg_user_id} — в архиве такого собеседника пока нет")
        summary = (f"Добавить в доверенные: {who}. Если автоответ включён, ассистент будет отвечать "
                   "этому человеку сам, без вашего согласования.")
        if note:
            summary += f"\nПометка к записи: {note}"
        answer, added = await settle(conn, TRUSTED_ADD, {"tg_user_id": tg_user_id, "note": note}, summary=summary)
        if answer is not None:
            return answer
        return JSONResponse({**await _trusted_view(conn), "added": bool(added)})


async def _trusted_problem(conn: asyncpg.Connection, tg_user_id: int) -> tuple[str | None, asyncpg.Record | None]:
    """Почему этого человека нельзя сделать доверенным (или None) и его запись в архиве."""
    if store.is_blocked_peer("user", tg_user_id):
        return "это служебный адрес Telegram: ему сервис не отвечает никогда", None
    if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM accounts WHERE tg_user_id = $1)", tg_user_id):
        return "это ваш собственный аккаунт: добавлять его в доверенные не нужно", None
    peer = await conn.fetchrow("SELECT name, is_bot FROM peers WHERE class = 'user' AND tg_id = $1", tg_user_id)
    if peer is not None and peer["is_bot"]:
        return "это бот: ботам сервис не отвечает", peer
    return None, peer


@confirm.applier(TRUSTED_ADD)
async def _apply_trusted_add(conn: asyncpg.Connection, payload: dict[str, Any]) -> confirm.Done:
    tg_user_id, note = _trusted_id(payload.get("tg_user_id")), payload.get("note")
    problem, peer = await _trusted_problem(conn, tg_user_id)
    if problem:
        raise confirm.Refused(problem, 400)
    confirm.must_not_widen(True)      # новый доверенный — всегда расширение
    added = await conn.fetchval(
        """INSERT INTO outbox_trusted (tg_user_id, note) VALUES ($1, $2)
           ON CONFLICT (tg_user_id) DO NOTHING RETURNING tg_user_id""",
        tg_user_id, textlib.one_line(note, 200) if isinstance(note, str) and note else None)
    count = await conn.fetchval("SELECT count(*) FROM outbox_trusted")
    if added is not None:
        known = f" (в архиве: {textlib.one_line(peer['name'], 60)})" if peer is not None and peer["name"] else \
            " (в архиве такого собеседника пока нет)"
        await confirm.tell(
            conn, f"В список доверенных добавлен идентификатор {tg_user_id}{known}. "
                  f"Всего доверенных: {count}. Если автоответ включён, этому человеку сервис отвечает сам.")
    return confirm.Done(note=f"Всего доверенных: {count}.", result=added is not None)


@handler
async def remove_trusted(request: Request) -> JSONResponse:
    raw = _query_int(request, "tg_user_id")
    if raw is None:
        raw = (await body(request)).get("tg_user_id")
    tg_user_id = _trusted_id(raw)
    async with _pool(request).acquire() as conn, conn.transaction():
        removed = await conn.fetchval(
            "DELETE FROM outbox_trusted WHERE tg_user_id = $1 RETURNING tg_user_id", tg_user_id)
        if removed is not None:
            count = await conn.fetchval("SELECT count(*) FROM outbox_trusted")
            await bridge.notify_owner(
                conn, f"Из списка доверенных убран идентификатор {tg_user_id}. Осталось: {count}.")
        return JSONResponse({**await _trusted_view(conn), "removed": removed is not None})


# --- наблюдатель ---

_RULE_COLUMNS = ("name", "enabled", "chat_ids", "keywords", "regexes", "use_lemmas", "description",
                 *watcher.LIMITS)


@handler
async def list_rules(request: Request) -> JSONResponse:
    async with _pool(request).acquire() as conn:
        rows = await conn.fetch("SELECT * FROM watch_rules ORDER BY id")
    return JSONResponse({"rules": [watcher.rule_public(r) for r in rows]})


_RULE_LABELS = {
    "max_checks_per_hour": "проверок моделью в час",
    "max_checks_per_day": "проверок моделью в сутки",
    "max_notifications_per_hour": "уведомлений вам в час",
    "max_notifications_per_day": "уведомлений вам в сутки",
}


def _listed(items: list[str], limit: int, width: int) -> str:
    shown = ", ".join(f"«{textlib.one_line(i, width)}»" for i in items[:limit])
    return shown + (f" и ещё {len(items) - limit}" if len(items) > limit else "")


async def _chat_names(conn: asyncpg.Connection, chat_ids: list[int]) -> str:
    rows = await conn.fetch(
        """SELECT c.id, COALESCE(c.title, p.name) AS title FROM chats c JOIN peers p ON p.id = c.peer_id
           WHERE c.id = ANY($1::bigint[])""", chat_ids)
    titles = {r["id"]: r["title"] or f"чат {r['id']}" for r in rows}
    return _listed([titles.get(i, f"чат {i}") for i in chat_ids], 8, 40)


async def _rule_lines(conn: asyncpg.Connection, fields: dict[str, Any], before: Any = None) -> str:
    """Поля правила строками для карточки владельцу. before — прежнее правило (для правки)."""
    lines: list[str] = []
    if before is not None and "name" in fields:
        lines.append(f"• новое название: «{fields['name']}»")
    if fields.get("enabled") is True and before is not None:
        lines.append("• включить правило")
    if fields.get("enabled") is False:
        lines.append("• правило выключено" if before is None else "• выключить правило")
    if "chat_ids" in fields:
        lines.append(f"• чаты ({len(fields['chat_ids'])}): {await _chat_names(conn, fields['chat_ids'])}")
    if fields.get("keywords"):
        lines.append(f"• слова: {_listed(fields['keywords'], 12, 40)}")
    if fields.get("regexes"):
        lines.append(f"• выражения: {_listed(fields['regexes'], 5, 80)}")
    if "use_lemmas" in fields and before is not None:
        lines.append(f"• учитывать словоформы: {policy.show(fields['use_lemmas'])}")
    if "description" in fields:
        lines.append(f"• что считать важным (это прочитает модель): {textlib.one_line(fields['description'], 400)}")
    for key, label in _RULE_LABELS.items():
        if key in fields and before is not None:
            lines.append(f"• {label}: было {policy.show(before[key])}, станет {policy.show(fields[key])}")
    return "\n".join(lines)


def _rule_widens(current: Any, changes: dict[str, Any]) -> bool:
    """Расширяет ли правка правило: больше чатов и совпадений, новый текст для модели и для
    уведомлений, выше пределы. Сужение (выключить, убрать чаты и слова, снизить пределы) — нет."""
    for key, value in changes.items():
        old = current[key]
        if key in ("chat_ids", "keywords", "regexes"):
            if set(value) - set(old):
                return True
        elif key in ("enabled", "use_lemmas"):
            if value and not old:
                return True
        elif key in watcher.LIMITS:
            if value > old:
                return True
        elif value != old:   # название и описание
            return True
    return False


async def _checked_chats(conn: asyncpg.Connection, chat_ids: list[int], error: type[Exception]) -> None:
    try:
        await watcher.check_chats(conn, chat_ids)
    except ValueError as exc:
        raise error(str(exc), 400) from None


@confirm.applier(RULE_CREATE)
async def _apply_rule_create(conn: asyncpg.Connection, payload: dict[str, Any]) -> confirm.Done:
    confirm.must_not_widen(True)      # новое правило — всегда расширение
    try:
        rule = watcher.validate_rule(dict(payload))
    except ValueError as exc:
        raise confirm.Refused(str(exc), 400) from None
    await _checked_chats(conn, rule["chat_ids"], confirm.Refused)
    columns = [c for c in _RULE_COLUMNS if c in rule]
    row = await conn.fetchrow(
        f"""INSERT INTO watch_rules ({', '.join(columns)})
            VALUES ({', '.join(f'${i}' for i in range(1, len(columns) + 1))}) RETURNING *""",
        *[rule[c] for c in columns])
    await confirm.tell(
        conn, f"Добавлено правило наблюдателя «{row['name']}»: чатов — {len(row['chat_ids'])}, "
              f"слов — {len(row['keywords'])}, выражений — {len(row['regexes'])}.", silent=True)
    return confirm.Done(result=watcher.rule_public(row))


@confirm.applier(RULE_UPDATE)
async def _apply_rule_update(conn: asyncpg.Connection, payload: dict[str, Any]) -> confirm.Done:
    rule_id = int(payload["rule_id"])
    try:
        changes = watcher.validate_rule(dict(payload.get("changes") or {}), partial=True)
    except ValueError as exc:
        raise confirm.Refused(str(exc), 400) from None
    current = await conn.fetchrow("SELECT * FROM watch_rules WHERE id = $1 FOR UPDATE", rule_id)
    if current is None:
        raise confirm.Refused("такого правила нет", 404)
    if not changes:
        raise confirm.Refused("нечего менять", 400)
    confirm.must_not_widen(_rule_widens(current, changes))    # строка правила уже заблокирована
    merged = {**dict(current), **changes}
    if not merged["keywords"] and not merged["regexes"]:
        raise confirm.Refused("нужно хотя бы одно слово в keywords или выражение в regexes", 400)
    if "chat_ids" in changes:
        await _checked_chats(conn, changes["chat_ids"], confirm.Refused)
    columns = [c for c in _RULE_COLUMNS if c in changes]
    row = await conn.fetchrow(
        f"""UPDATE watch_rules SET {', '.join(f'{c} = ${i}' for i, c in enumerate(columns, 2))},
                   updated_at = now() WHERE id = $1 RETURNING *""",
        rule_id, *[changes[c] for c in columns])
    return confirm.Done(result=watcher.rule_public(row))


@confirm.applier(RULE_DELETE)
async def _apply_rule_delete(conn: asyncpg.Connection, payload: dict[str, Any]) -> confirm.Done:
    confirm.must_not_widen(True)      # удаление необратимо: без владельца не выполняется
    gone = await conn.fetchval("DELETE FROM watch_rules WHERE id = $1 RETURNING id", int(payload["rule_id"]))
    if gone is None:
        raise confirm.Refused("такого правила нет", 404)
    return confirm.Done(result={"ok": True})


@handler
async def create_rule(request: Request) -> JSONResponse:
    rule = _checked(watcher.validate_rule, await body(request))
    async with _pool(request).acquire() as conn:
        await _checked_chats(conn, rule["chat_ids"], BadRequest)
        summary = (f"Добавить правило наблюдателя «{rule['name']}». Сервис будет просматривать новые "
                   "сообщения в этих чатах, показывать совпадения модели и писать вам о важном.\n"
                   + await _rule_lines(conn, rule))
        answer, row = await settle(conn, RULE_CREATE, rule, summary=summary)
    return answer or JSONResponse(row)


@handler
async def update_rule(request: Request) -> JSONResponse:
    changes = _checked(watcher.validate_rule, await body(request), partial=True)
    if not changes:
        raise BadRequest("нечего менять")
    rule_id = int(request.path_params["rule_id"])
    async with _pool(request).acquire() as conn:
        current = await conn.fetchrow("SELECT * FROM watch_rules WHERE id = $1", rule_id)
        if current is None:
            raise BadRequest("такого правила нет", status=404)
        merged = {**dict(current), **changes}
        if not merged["keywords"] and not merged["regexes"]:
            raise BadRequest("нужно хотя бы одно слово в keywords или выражение в regexes")
        if "chat_ids" in changes:
            await _checked_chats(conn, changes["chat_ids"], BadRequest)
        summary = None
        if _rule_widens(current, changes):
            changed = {k: v for k, v in changes.items()
                       if (set(v) != set(current[k]) if isinstance(v, list) else v != current[k])}
            summary = (f"Изменить правило наблюдателя «{textlib.one_line(current['name'], 100)}»:\n"
                       + await _rule_lines(conn, changed, before=current))
        answer, row = await settle(conn, RULE_UPDATE, {"rule_id": rule_id, "changes": changes}, summary=summary)
    return answer or JSONResponse(row)


@handler
async def delete_rule(request: Request) -> JSONResponse:
    rule_id = int(request.path_params["rule_id"])
    async with _pool(request).acquire() as conn:
        name = await conn.fetchval("SELECT name FROM watch_rules WHERE id = $1", rule_id)
        if name is None:
            raise BadRequest("такого правила нет", status=404)
        hits = await conn.fetchval("SELECT count(*) FROM watch_hits WHERE rule_id = $1", rule_id)
        answer, out = await settle(
            conn, RULE_DELETE, {"rule_id": rule_id},
            summary=f"Удалить правило наблюдателя «{textlib.one_line(name, 100)}» вместе с журналом его "
                    f"срабатываний (записей: {hits}). Вернуть удалённое нельзя. Если нужно только "
                    "остановить правило, его можно выключить — это подтверждения не требует.")
    return answer or JSONResponse(out)


@handler
async def list_hits(request: Request) -> JSONResponse:
    status = request.query_params.get("status")
    async with state_of(request).ro_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, rule_id, message_id, chat_id, matched, status, reason, notified, created_at, decided_at
               FROM watch_hits
               WHERE ($1::bigint IS NULL OR rule_id = $1) AND ($2::text IS NULL OR status = $2)
               ORDER BY id DESC LIMIT $3""",
            _query_int(request, "rule_id"), status, _limit(request))
    hits = []
    for r in rows:
        item = dict(r)
        item["matched"] = list(item["matched"])
        for key in ("created_at", "decided_at"):
            item[key] = item[key].isoformat() if item[key] else None
        hits.append(item)
    return JSONResponse({"hits": hits})


@bridge.on_owner_change
async def _owner_changed(conn, new_user_id: int) -> None:
    """Новый владелец не должен унаследовать чужие решения: черновики, ждавшие прежнего владельца,
    отклоняются, автоответ выключается, список доверенных очищается."""
    await conn.execute(
        """UPDATE outbox_drafts SET status = 'rejected', error_code = 'owner_changed', finished_at = now()
           WHERE status = 'pending'""")
    await conn.execute("UPDATE outbox_accounts SET autoreply_enabled = false WHERE autoreply_enabled")
    await conn.execute("DELETE FROM outbox_trusted")


def routes() -> list[BaseRoute]:
    return [
        Route("/api/outbox/drafts", create_draft, methods=["POST"]),
        Route("/api/outbox/drafts", list_drafts, methods=["GET"]),
        Route("/api/outbox/drafts/{draft_id:int}/cancel", cancel_draft, methods=["POST"]),
        Route("/api/outbox/policy", get_policy, methods=["GET"]),
        Route("/api/outbox/policy", put_policy, methods=["PUT"]),
        Route("/api/outbox/chats/{chat_id:int}", put_chat, methods=["PUT"]),
        Route("/api/outbox/autoreply", get_autoreply, methods=["GET"]),
        Route("/api/outbox/autoreply", put_autoreply, methods=["PUT"]),
        Route("/api/outbox/trusted", get_trusted, methods=["GET"]),
        Route("/api/outbox/trusted", add_trusted, methods=["POST"]),
        Route("/api/outbox/trusted", remove_trusted, methods=["DELETE"]),
        Route("/api/watch/rules", list_rules, methods=["GET"]),
        Route("/api/watch/rules", create_rule, methods=["POST"]),
        Route("/api/watch/rules/{rule_id:int}", update_rule, methods=["PUT"]),
        Route("/api/watch/rules/{rule_id:int}", delete_rule, methods=["DELETE"]),
        Route("/api/watch/hits", list_hits, methods=["GET"]),
    ]


@contextlib.asynccontextmanager
async def lifespan(state: AppState) -> AsyncIterator[None]:
    mod = runtime.Outbox(state)
    runtime.set_current(mod)
    state.extras["outbox"] = mod

    async def to_autoreply(payload: dict[str, Any]) -> None:
        await autoreply.on_message(mod, payload)

    async def to_watcher(payload: dict[str, Any]) -> None:
        await watcher.on_message(mod, payload)

    async def sweeper() -> None:
        # Первый обход — сразу: после перезапуска надо закрыть то, что оборвалось на полпути.
        while True:
            try:
                await drafts.sweep(mod)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("уборка черновиков завершилась с ошибкой")
            await asyncio.sleep(SWEEP_EVERY)

    async def chat_excluded(payload: dict[str, Any]) -> None:
        chat_id = payload.get("chat_id")
        if isinstance(chat_id, int):
            async with state.pool.acquire() as conn:
                await drafts.drop_excluded(conn, chat_id)

    state.events.subscribe(CHAT_EXCLUDED, chat_excluded)
    state.events.subscribe(MESSAGE_LIVE, to_autoreply)
    state.events.subscribe(MESSAGE_LIVE, to_watcher)
    state.spawn(drafts.run_sender(mod), name="outbox-sender")
    state.spawn(sweeper(), name="outbox-sweeper")
    try:
        yield
    finally:
        mod.close()
        if runtime.current() is mod:
            runtime.set_current(None)
