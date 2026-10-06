"""Правила шлюза отправки: кому, каким каналом и как часто можно писать.

Каждая проверка возвращает решение с кодом причины и объяснением по-русски. Запрет — значение
по умолчанию: писать можно только туда, где ни одна проверка не возразила.

Замысел (не код) взят из двух проектов:
  Prgebish/mcp-telegram (MIT), internal/acl/acl.go — права по чатам с запретом по умолчанию;
  tolboy/telegram-mcp-tdlib (Apache-2.0), AntiSpamGuardService.kt и AntiSpamProperties.kt —
  скользящее окно на чат, поиск повторов «чат + текст», дневной предел, потолки настроек.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import asyncpg

from .. import store
from ..tg.gateway import TgGateway
from . import text as textlib

SETTINGS_KEY = "outbox.policy"
BUSINESS_WINDOW_SECONDS = 24 * 3600  # правило Telegram: ответ через бизнес-бота — в течение суток

# настройка: (значение по умолчанию, наименьшее, наибольшее). Значение вне границ прижимается к границе.
NUMBERS: dict[str, tuple[float, float, float]] = {
    # не больше стольких отправок в один чат за окно
    "chat_window_seconds": (60, 10, 3600),
    "chat_window_max": (5, 1, 30),
    # тот же текст в тот же чат повторно не уходит, пока не пройдёт это время
    "duplicate_window_seconds": (300, 30, 86400),
    # отправок на аккаунт за сутки, всего (черновики и автоответы вместе)
    "daily_cap": (400, 1, 1000),
    # пауза между отправками одного аккаунта
    "min_pause_seconds": (3, 0, 600),
    # пауза между частями длинного сообщения
    "part_pause_seconds": (1.5, 0, 30),
    # на сколько частей можно разрезать длинный текст
    "max_parts": (4, 1, 10),
    # сколько живёт черновик без решения владельца
    "draft_ttl_seconds": (86400, 300, 3 * 86400),
    # сколько черновиков в час можно создать на аккаунт (защита владельца от потока карточек)
    "drafts_per_hour": (30, 1, 200),
    # сколько ждать ответа Telegram или бизнес-бота, прежде чем признать исход неизвестным
    "send_timeout_seconds": (60, 5, 300),
    # согласованный черновик, который не удалось начать отправлять за это время, не уходит
    "approval_max_age_seconds": (120, 10, 900),
    # запас до конца суточного окна бизнес-бота
    "business_window_margin_seconds": (300, 0, 3600),
    # через сколько дней у завершённого черновика стирается текст (остаются состояние и отпечаток)
    "text_retention_days": (30, 1, 365),
}
INTEGER_KEYS = frozenset(NUMBERS) - {"min_pause_seconds", "part_pause_seconds"}
CHOICES = {"drafting_default": ("allow", "deny")}
DEFAULT_CHOICES = {"drafting_default": "allow"}

REASONS = {
    "sending_disabled": "Отправка сообщений выключена в настройках сервера. Включить её можно только на "
                        "самом сервере (SHTURMAN_SENDING в файле .env), через ассистента — нельзя.",
    "first_contact": "Ассистент не пишет первым: в этом чате ещё нет ни одного вашего сообщения. "
                     "Напишите человеку сами — после этого можно будет готовить черновики.",
    "card_not_delivered": "Карточка с черновиком не дошла до владельца: согласовать его было некому.",
    "chat_excluded_cleanup": "Чат исключён владельцем: черновик и его текст удалены.",
    "chat_not_found": "Такого чата нет в архиве.",
    "chat_excluded": "Этот чат исключён владельцем: писать в него нельзя.",
    "chat_blocked": "Это служебный чат Telegram: писать в него нельзя никогда.",
    "drafting_forbidden": "Владелец запретил готовить сообщения в этот чат.",
    "owner_read_only": "Основной аккаунт владельца работает только на чтение. От имени владельца "
                       "пишет только бизнес-бот.",
    "business_not_for_assistant": "Бизнес-бот пишет только от имени владельца, а этот чат — аккаунта-помощника.",
    "session_not_configured": "Аккаунт-помощник не подключён.",
    "session_unavailable": "Аккаунт-помощник сейчас не на связи.",
    "business_unavailable": "Бизнес-бот не подключён или ему не разрешено отвечать.",
    "business_private_only": "Через бизнес-бота можно писать только людям в личных чатах.",
    "business_window_closed": "Через бизнес-бота можно ответить только в течение суток после сообщения "
                              "собеседника. Последнее входящее старше — напишите сами.",
    "text_empty": "Текст пуст.",
    "text_too_long_business": "Текст длиннее 4096 знаков: через бизнес-бота он не уйдёт одним "
                              "сообщением. Сократите.",
    "text_too_long": "Текст слишком длинный: пришлось бы разрезать больше чем на {parts} частей. Сократите.",
    "limit_chat_window": "В этот чат уже отправлено {count} сообщений за последние {seconds} с. Подождите.",
    "duplicate_text": "Такой же текст в этот чат уже отправлялся недавно.",
    "limit_daily": "Достигнут дневной предел отправок с этого аккаунта ({cap}).",
    "limit_autoreply_daily": "Достигнут дневной предел автоответов с этого аккаунта ({cap}).",
    "limit_drafts": "За последний час создано слишком много черновиков ({cap}). Подождите.",
    "flood_wait": "Telegram просит подождать ещё {seconds} с. Раньше отправлять нельзя.",
    "owner_unknown": "Владелец ещё не привязан: согласовать отправку некому.",
    "idempotency_conflict": "Этот ключ запроса уже использован для другого сообщения.",
    "reply_not_found": "Сообщение, на которое нужно ответить, не найдено в этом чате.",
    "stale": "Сообщение не удалось отправить вовремя, а с опозданием оно не уходит.",
    "send_forbidden": "Этому аккаунту отправка запрещена.",
    "partial": "Отправлена только часть сообщения: {sent} из {total}.",
    "executor_absent": "Hermes не забрал задание на отправку: сообщение не ушло.",
    "business_rejected": "Бизнес-бот не смог отправить сообщение.",
    "outcome_unknown": "Ответ об отправке не пришёл: сообщение могло дойти, а могло и нет.",
    "cancelled": "Черновик отменён.",
    "service_stopped": "Сервис отправки не запущен.",
}


@dataclass(frozen=True)
class Decision:
    ok: bool
    code: str = "ok"
    message: str = ""
    # временный отказ: позже то же самое может стать возможным
    temporary: bool = False
    retry_after: int | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"error": self.message, "reason": self.code}
        if self.retry_after is not None:
            out["retry_after"] = self.retry_after
        return out


ALLOW = Decision(True)


def deny(code: str, *, temporary: bool = False, retry_after: int | None = None, **fmt: Any) -> Decision:
    return Decision(False, code, REASONS[code].format(**fmt), temporary, retry_after)


@dataclass(frozen=True)
class Target:
    """Чат-адресат со всем, что нужно для решения."""

    chat_id: int
    account_id: int
    account_role: str
    account_label: str
    chat_type: str
    title: str | None
    excluded: bool
    peer_id: int
    peer_class: str
    tg_id: int
    name: str | None
    username: str | None
    is_bot: bool | None

    @property
    def display_name(self) -> str:
        return textlib.one_line(self.title or self.name or f"чат {self.tg_id}", 64)

    @property
    def is_private(self) -> bool:
        """Личный чат с человеком: не группа, не канал, не бот, не «Избранное»."""
        return (self.peer_class == "user" and self.chat_type not in ("bot_chat", "saved_messages")
                and self.is_bot is not True)


_TARGET = """
SELECT c.id AS chat_id, c.account_id, a.role AS account_role, a.label AS account_label,
       c.type AS chat_type, c.title, c.excluded, p.id AS peer_id, p.class AS peer_class,
       p.tg_id, p.name, p.username, p.is_bot
FROM chats c
JOIN accounts a ON a.id = c.account_id
JOIN peers p ON p.id = c.peer_id
WHERE c.id = $1
"""


async def target(conn: asyncpg.Connection, chat_id: int) -> Target | None:
    row = await conn.fetchrow(_TARGET, chat_id)
    return Target(**dict(row)) if row is not None else None


# --- настройки ---

def _loads(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    value = json.loads(raw) if isinstance(raw, str) else raw
    return value if isinstance(value, dict) else {}


def clamp(key: str, value: Any, table: dict[str, tuple[float, float, float]], integers: frozenset[str]) -> float:
    """Прижимает число к допустимым границам; не число — значение по умолчанию."""
    default, low, high = table[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        value = default
    value = max(low, min(high, value))
    return int(value) if key in integers else float(value)


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {k: clamp(k, raw.get(k, v[0]), NUMBERS, INTEGER_KEYS) for k, v in NUMBERS.items()}
    for key, allowed in CHOICES.items():
        out[key] = raw.get(key) if raw.get(key) in allowed else DEFAULT_CHOICES[key]
    return out


async def stored(conn: asyncpg.Connection) -> dict[str, Any]:
    """Сохранённые правила поверх значений по умолчанию, всё в допустимых границах."""
    return normalize(_loads(await conn.fetchval("SELECT value FROM settings WHERE key = $1", SETTINGS_KEY)))


async def load(conn: asyncpg.Connection, config: Any = None) -> dict[str, Any]:
    """Действующие правила: сохранённые настройки плюс то, что задаётся ТОЛЬКО окружением сервиса.

    `sending` — главный выключатель отправки, `hard_daily_cap` — потолок отправок на аккаунт в
    сутки. Оба берутся из `config`, в базе не хранятся и через API не меняются: тот, кто завладел
    токеном API, не может ни включить отправку, ни поднять потолок. Без `config` отправка
    считается выключенной.
    """
    rules = await stored(conn)
    rules["sending"] = getattr(config, "sending", False) is True
    rules["hard_daily_cap"] = max(0, int(getattr(config, "send_daily_hard_cap", 0) or 0))
    rules["daily_cap_stored"] = rules["daily_cap"]
    rules["daily_cap"] = min(rules["daily_cap"], rules["hard_daily_cap"])
    return rules


def validate_update(data: dict[str, Any]) -> dict[str, Any]:
    """Проверяет изменение настроек. Неизвестный ключ или значение не того вида — ошибка (ValueError)."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key in NUMBERS:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"поле {key}: нужно число")
            out[key] = clamp(key, value, NUMBERS, INTEGER_KEYS)
        elif key in CHOICES:
            if value not in CHOICES[key]:
                raise ValueError(f"поле {key}: допустимо " + " или ".join(CHOICES[key]))
            out[key] = value
        else:
            raise ValueError(f"поле {key}: такой настройки нет")
    return out


async def save_setting(conn: asyncpg.Connection, key: str, value: dict[str, Any]) -> None:
    await conn.execute(
        """INSERT INTO settings (key, value) VALUES ($1, $2::jsonb)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
        key, json.dumps(value, ensure_ascii=False),
    )


async def update(conn: asyncpg.Connection, changes: dict[str, Any]) -> dict[str, Any]:
    current = await stored(conn)
    current.update(changes)
    await save_setting(conn, SETTINGS_KEY, current)
    return current


# --- проверки ---

def check_switch(rules: dict[str, Any]) -> Decision:
    """Главный выключатель. Пока он выключен, сервис не отправляет ничего, никому и ни одним каналом."""
    return ALLOW if rules.get("sending") is True else deny("sending_disabled")


async def has_fresh_incoming(conn: asyncpg.Connection, rules: dict[str, Any], chat_id: int) -> bool:
    """Есть ли входящее сообщение собеседника в пределах суточного окна бизнес-бота.
    Сообщение с неизвестным направлением входящим не считается."""
    return await conn.fetchval(
        """SELECT EXISTS (
               SELECT 1 FROM messages
               WHERE chat_id = $1 AND kind = 'message' AND is_outgoing IS FALSE
                 AND sent_at > now() - make_interval(secs => $2) AND sent_at <= now() + interval '5 minutes')""",
        chat_id, float(BUSINESS_WINDOW_SECONDS - rules["business_window_margin_seconds"]),
    )


async def check_first_contact(conn: asyncpg.Connection, rules: dict[str, Any], tgt: Target, channel: str) -> Decision:
    """Агент не пишет первым. Черновик возможен, если в чате уже есть исходящее сообщение владельца
    или помощника, либо это ответ через бизнес-бота человеку, который только что написал сам.
    Через API это правило не выключается."""
    if await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM messages WHERE chat_id = $1 AND kind = 'message' AND is_outgoing IS TRUE)",
            tgt.chat_id):
        return ALLOW
    if channel == "business" and await has_fresh_incoming(conn, rules, tgt.chat_id):
        return ALLOW
    return deny("first_contact")

async def drafting_allowed(conn: asyncpg.Connection, rules: dict[str, Any], tgt: Target) -> bool:
    own = await conn.fetchval("SELECT drafting FROM outbox_chats WHERE chat_id = $1", tgt.chat_id)
    if own is None:
        own = await conn.fetchval(
            "SELECT drafting_default FROM outbox_accounts WHERE account_id = $1", tgt.account_id)
    return (own or rules["drafting_default"]) == "allow"


async def check_target(conn: asyncpg.Connection, rules: dict[str, Any], tgt: Target) -> Decision:
    """Можно ли вообще писать в этот чат."""
    if store.is_blocked_peer(tgt.peer_class, tgt.tg_id, tgt.username):
        return deny("chat_blocked")
    if tgt.excluded:
        return deny("chat_excluded")
    if not await drafting_allowed(conn, rules, tgt):
        return deny("drafting_forbidden")
    return ALLOW


def pick_channel(tgt: Target, requested: str | None) -> tuple[str | None, Decision]:
    """Канал определяется ролью аккаунта: владелец — только бизнес-бот, помощник — только сессия."""
    if tgt.account_role == "owner":
        if requested == "session":
            return None, deny("owner_read_only")
        return "business", ALLOW
    if requested == "business":
        return None, deny("business_not_for_assistant")
    return "session", ALLOW


async def business_connection(conn: asyncpg.Connection, account_id: int) -> str | None:
    return await conn.fetchval(
        """SELECT id FROM business_connections
           WHERE account_id = $1 AND enabled AND can_reply
           ORDER BY updated_at DESC, id LIMIT 1""",
        account_id,
    )


async def check_channel(
    conn: asyncpg.Connection, tg: TgGateway | None, rules: dict[str, Any], tgt: Target, channel: str,
) -> Decision:
    """Доступен ли канал прямо сейчас."""
    if channel == "session":
        if tgt.account_role != "assistant":
            return deny("owner_read_only")
        if tg is None:
            return deny("session_not_configured", temporary=True)
        if not tg.can_send(tgt.account_id):
            return deny("session_unavailable", temporary=True)
        return ALLOW
    if tgt.account_role != "owner":
        return deny("business_not_for_assistant")
    if not tgt.is_private:
        return deny("business_private_only")
    if await business_connection(conn, tgt.account_id) is None:
        return deny("business_unavailable", temporary=True)
    # Суточное окно считается по архиву: последнее входящее сообщение собеседника.
    return ALLOW if await has_fresh_incoming(conn, rules, tgt.chat_id) else deny("business_window_closed")


def parts_of(channel: str, text: str) -> list[str]:
    """На сколько сообщений разойдётся текст: бизнес-бот — всегда одно, сессия — по абзацам."""
    if channel == "business" or textlib.utf16_len(text) <= textlib.SPLIT_LIMIT:
        return [text]
    return textlib.split_text(text, textlib.SPLIT_LIMIT)


def check_text(rules: dict[str, Any], channel: str, text: str) -> Decision:
    if not text:
        return deny("text_empty")
    if channel == "business":
        if textlib.utf16_len(text) > textlib.TELEGRAM_LIMIT:
            return deny("text_too_long_business")
        return ALLOW
    if len(parts_of(channel, text)) > rules["max_parts"]:
        return deny("text_too_long", parts=rules["max_parts"])
    return ALLOW


async def is_duplicate(
    conn: asyncpg.Connection, rules: dict[str, Any], tgt: Target, text_hash: str, *, draft_id: int | None = None,
) -> bool:
    """Уходил ли такой же текст в этот чат недавно. Отправка с неизвестным исходом тоже считается:
    она могла дойти."""
    return await conn.fetchval(
        """SELECT EXISTS (
               SELECT 1 FROM outbox_drafts
               WHERE chat_id = $1 AND text_hash = $2 AND id <> $4
                 AND (status IN ('sending', 'sent', 'outcome_unknown') OR parts_sent > 0)
                 AND claimed_at > now() - make_interval(secs => $3))""",
        tgt.chat_id, text_hash, float(rules["duplicate_window_seconds"]), draft_id or 0,
    )


async def check_limits(
    conn: asyncpg.Connection, rules: dict[str, Any], tgt: Target, text_hash: str, *,
    origin: str = "agent", autoreply_daily_cap: int | None = None, draft_id: int | None = None,
) -> Decision:
    """Частота и повторы. Считаются отправки, запрос на которые уже ушёл (`claimed_at`)."""
    wait = await conn.fetchval(
        """SELECT ceil(extract(epoch FROM blocked_until - now()))::int FROM outbox_accounts
           WHERE account_id = $1 AND blocked_until > now()""",
        tgt.account_id,
    )
    if wait:
        return deny("flood_wait", temporary=True, retry_after=int(wait), seconds=int(wait))
    me = draft_id or 0
    in_chat = await conn.fetchval(
        """SELECT count(*) FROM outbox_drafts
           WHERE chat_id = $1 AND id <> $3 AND claimed_at > now() - make_interval(secs => $2)""",
        tgt.chat_id, float(rules["chat_window_seconds"]), me,
    )
    if in_chat >= rules["chat_window_max"]:
        return deny("limit_chat_window", temporary=True, retry_after=int(rules["chat_window_seconds"]),
                    count=in_chat, seconds=int(rules["chat_window_seconds"]))
    if await is_duplicate(conn, rules, tgt, text_hash, draft_id=draft_id):
        return deny("duplicate_text", temporary=True, retry_after=int(rules["duplicate_window_seconds"]))
    row = await conn.fetchrow(
        """SELECT count(*) AS total, count(*) FILTER (WHERE origin = 'autoreply') AS auto
           FROM outbox_drafts
           WHERE account_id = $1 AND id <> $2 AND claimed_at > now() - interval '24 hours'""",
        tgt.account_id, me,
    )
    # Потолок из окружения сервиса сильнее любых сохранённых настроек.
    cap = min(rules["daily_cap"], rules.get("hard_daily_cap", 0))
    if row["total"] >= cap:
        return deny("limit_daily", temporary=True, cap=cap)
    if origin == "autoreply" and autoreply_daily_cap is not None:
        auto_cap = min(autoreply_daily_cap, cap)
        if row["auto"] >= auto_cap:
            return deny("limit_autoreply_daily", temporary=True, cap=auto_cap)
    return ALLOW


async def check_send(
    conn: asyncpg.Connection, tg: TgGateway | None, rules: dict[str, Any], tgt: Target, *,
    channel: str, text: str, text_hash: str, origin: str = "agent",
    autoreply_daily_cap: int | None = None, draft_id: int | None = None,
) -> Decision:
    """Полная проверка перед отправкой: выключатель, чат, канал, текст, частота. Первая причина
    отказа — ответ."""
    decision = check_switch(rules)
    if not decision.ok:
        return decision
    for decision in (
        await check_target(conn, rules, tgt),
        await check_channel(conn, tg, rules, tgt, channel),
        check_text(rules, channel, text),
    ):
        if not decision.ok:
            return decision
    if origin == "agent":
        decision = await check_first_contact(conn, rules, tgt, channel)
        if not decision.ok:
            return decision
    return await check_limits(conn, rules, tgt, text_hash, origin=origin,
                              autoreply_daily_cap=autoreply_daily_cap, draft_id=draft_id)


async def pause_remaining(conn: asyncpg.Connection, account_id: int, pause: float) -> float:
    """Сколько секунд осталось выждать после предыдущей отправки аккаунта."""
    left = await conn.fetchval(
        """SELECT extract(epoch FROM last_send_at + make_interval(secs => $2) - now())
           FROM outbox_accounts WHERE account_id = $1""",
        account_id, float(pause),
    )
    return max(0.0, float(left or 0.0))


async def lock_account(conn: asyncpg.Connection, account_id: int) -> None:
    """Блокировка на время транзакции: проверка лимитов и отметка отправки — неделимо для аккаунта."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext('shturman.outbox.account'), $1::int)",
                       account_id % 2_000_000_000)


async def lock_chat(conn: asyncpg.Connection, chat_id: int) -> None:
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext('shturman.outbox.chat'), $1::int)",
                       chat_id % 2_000_000_000)


async def touch_account(conn: asyncpg.Connection, account_id: int, *, blocked_for: int | None = None) -> None:
    """Отмечает отправку (для паузы) или просьбу Telegram подождать."""
    if blocked_for is None:
        await conn.execute(
            """INSERT INTO outbox_accounts (account_id, last_send_at) VALUES ($1, now())
               ON CONFLICT (account_id) DO UPDATE SET last_send_at = now(), updated_at = now()""",
            account_id,
        )
    else:
        await conn.execute(
            """INSERT INTO outbox_accounts (account_id, blocked_until)
               VALUES ($1, now() + make_interval(secs => $2))
               ON CONFLICT (account_id) DO UPDATE
               SET blocked_until = GREATEST(COALESCE(outbox_accounts.blocked_until, now()),
                                            EXCLUDED.blocked_until), updated_at = now()""",
            account_id, float(blocked_for),
        )
