"""Разрешённые владельцем групповые ответы и проверяемое прямое обращение.

Разрешение на ответ не разрешает читать посторонние источники. Методы изменения
вызываются только в проверенном контексте владельца; агент предлагает изменения
через confirm. В этом модуле нет включения главного выключателя отправки.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import asyncpg

from . import direct_address, policy
from .policy import Decision, Target

SETTINGS_KEY = "outbox.addressing"
GROUP_TYPES = frozenset({"private_group", "private_supergroup", "public_supergroup"})
_REASONS = {
    "group_not_supported": "Автоответ доступен только в группе аккаунта помощника.",
    "group_disabled": "Автоответ для этого аккаунта выключен.",
    "group_not_addressed": "Нет прямого обращения или разрешённой владельцем области ответа.",
    "group_trigger_invalid": "Нет достоверного входящего сообщения Telegram для ответа.",
}


def _no(code: str) -> Decision:
    return Decision(False, code, _REASONS[code])


def is_group(tgt: Target) -> bool:
    return tgt.peer_class in ("chat", "channel") and tgt.chat_type in GROUP_TYPES


def _positive(value: Any, field: str, *, optional=False) -> int | None:
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field}: нужен положительный числовой идентификатор")
    return value


def validate_scope(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) - {"chat_id", "topic_tg_id", "enabled"}:
        raise ValueError("scope: допустимы chat_id, topic_tg_id, enabled")
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("enabled: нужно true или false")
    return {"chat_id": _positive(data.get("chat_id"), "chat_id"),
            "topic_tg_id": _positive(data.get("topic_tg_id"), "topic_tg_id", optional=True),
            "enabled": enabled}


def _owner() -> None:
    from .. import authority
    if not authority.is_owner():
        raise PermissionError("Решение об областях ответа принимает только владелец в своём боте.")


async def put(conn: asyncpg.Connection, chat_id: int, topic_tg_id: int | None = None,
              enabled: bool = True) -> dict[str, Any]:
    _owner()
    data = validate_scope({"chat_id": chat_id, "topic_tg_id": topic_tg_id, "enabled": enabled})
    tgt = await policy.target(conn, chat_id)
    if tgt is None or tgt.account_role != "assistant" or not is_group(tgt) or tgt.excluded:
        raise ValueError("Нужна доступная группа аккаунта помощника; аккаунт владельца только читает.")
    row = await conn.fetchrow(
        """INSERT INTO outbox_reply_scopes (account_id, peer_id, topic_tg_id, enabled)
           VALUES ($1,$2,$3,$4) ON CONFLICT (account_id,peer_id,topic_tg_id)
           DO UPDATE SET enabled=EXCLUDED.enabled, updated_at=now() RETURNING *""",
        tgt.account_id, tgt.peer_id, data["topic_tg_id"] or 0, data["enabled"])
    return dict(row)


async def remove(conn: asyncpg.Connection, scope_id: int) -> bool:
    _owner()
    _positive(scope_id, "scope_id")
    return await conn.fetchval("DELETE FROM outbox_reply_scopes WHERE id=$1 RETURNING true", scope_id) is True


async def list_scopes(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """SELECT s.*, c.id AS chat_id, c.title, a.label AS account_label, p.class AS peer_class,
                  p.tg_id FROM outbox_reply_scopes s
           JOIN accounts a ON a.id=s.account_id JOIN peers p ON p.id=s.peer_id
           LEFT JOIN chats c ON c.account_id=s.account_id AND c.peer_id=s.peer_id ORDER BY s.id""")
    return [dict(r) for r in rows]


def validate_addressing(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) != {"aliases"}:
        raise ValueError("addressing: допустимо только поле aliases")
    return {"aliases": direct_address.validate_aliases(data["aliases"])}


async def addressing(conn: asyncpg.Connection) -> dict[str, Any]:
    raw = policy._loads(await conn.fetchval("SELECT value FROM settings WHERE key=$1", SETTINGS_KEY))
    try:
        return {"aliases": direct_address.validate_aliases(raw.get("aliases", []))}
    except ValueError:
        return {"aliases": []}


async def update_addressing(conn: asyncpg.Connection, changes: dict[str, Any]) -> dict[str, Any]:
    _owner()
    value = validate_addressing(changes)
    await policy.save_setting(conn, SETTINGS_KEY, value)
    return value


async def matches(conn: asyncpg.Connection, tgt: Target, topic_tg_id: int | None) -> bool:
    return await conn.fetchval(
        """SELECT EXISTS(SELECT 1 FROM outbox_reply_scopes WHERE account_id=$1 AND peer_id=$2
                          AND enabled AND (topic_tg_id=0 OR topic_tg_id=$3))""",
        tgt.account_id, tgt.peer_id, topic_tg_id or 0)


async def topic_for_message(conn: asyncpg.Connection, message_id: int, chat_id: int | None = None) -> int | None:
    return await conn.fetchval("SELECT topic_tg_id FROM messages WHERE id=$1 AND ($2::bigint IS NULL OR chat_id=$2)",
                               message_id, chat_id)


async def group_candidate(conn: asyncpg.Connection, tgt: Target, message_id: int | None) -> Decision:
    """Кандидат на ответ; ограничения отправки и источников обязательны отдельно."""
    if tgt.account_role != "assistant" or not is_group(tgt):
        return _no("group_not_supported")
    if tgt.excluded:
        return policy.deny("chat_excluded")
    if not await conn.fetchval("SELECT autoreply_enabled FROM outbox_accounts WHERE account_id=$1", tgt.account_id):
        return _no("group_disabled")
    msg = await conn.fetchrow(
        """SELECT m.*, p.class AS sender_class, p.tg_id AS sender_tg_id FROM messages m
           LEFT JOIN peers p ON p.id=m.sender_peer_id
           WHERE m.id=$1 AND m.chat_id=$2 AND m.kind='message' AND m.deleted_at IS NULL
             AND m.agent_visible AND m.is_outgoing IS FALSE AND m.edited_at IS NULL
             AND 'session'=ANY(m.sources) AND m.telegram_entities IS NOT NULL
             AND m.telegram_via_bot IS FALSE AND m.telegram_sender_bot IS FALSE
             AND m.is_forwarded IS FALSE AND p.class='user'""", message_id, tgt.chat_id)
    if msg is None:
        return _no("group_trigger_invalid")
    ids = [r["tg_user_id"] for r in await conn.fetch("SELECT tg_user_id FROM accounts WHERE role IN ('owner','assistant')")]
    if msg["sender_tg_id"] in ids:
        return _no("group_trigger_invalid")  # собственные сообщения не запускают беседу аккаунтов между собой
    if await matches(conn, tgt, msg["topic_tg_id"]):
        return policy.ALLOW
    reply_sender = None
    if msg["reply_to_tg_id"] is not None:
        reply_sender = await conn.fetchval(
            """SELECT p.tg_id FROM messages m JOIN peers p ON p.id=m.sender_peer_id
               WHERE m.chat_id=$1 AND m.tg_message_id=$2 AND m.kind='message'
                 AND m.deleted_at IS NULL AND m.agent_visible AND m.is_forwarded IS FALSE
                 AND 'session'=ANY(m.sources) AND m.telegram_entities IS NOT NULL
                 AND p.class='user' AND m.topic_tg_id IS NOT DISTINCT FROM $3::bigint""",
            tgt.chat_id, msg["reply_to_tg_id"], msg["topic_tg_id"])
    config = await addressing(conn)
    return policy.ALLOW if direct_address.is_direct_address(
        dict(msg), ids, config["aliases"], reply_sender_tg_id=reply_sender) else _no("group_not_addressed")


async def policy_revision(conn: asyncpg.Connection, tgt: Target, message_id: int | None = None) -> str:
    """Отпечаток решения владельца: отзыв и повторное включение не воскрешают старое задание."""
    rows = await conn.fetch("SELECT id,topic_tg_id,enabled,updated_at::text AS revision FROM outbox_reply_scopes "
                            "WHERE account_id=$1 AND peer_id=$2 ORDER BY id", tgt.account_id, tgt.peer_id)
    config = await conn.fetchrow("SELECT value,updated_at::text AS revision FROM settings WHERE key=$1", SETTINGS_KEY)
    enabled = await conn.fetchrow("SELECT autoreply_enabled,autoreply_revision AS revision FROM outbox_accounts WHERE account_id=$1",
                                   tgt.account_id)
    ids = await conn.fetch("SELECT id,tg_user_id,role FROM accounts ORDER BY id")
    payload = {"scopes": [dict(r) for r in rows], "addressing": dict(config) if config else None,
               "enabled": dict(enabled) if enabled else None, "ids": [dict(r) for r in ids],
               "target": [tgt.account_id,tgt.peer_id,tgt.chat_type,tgt.excluded]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


current_revision = policy_revision
