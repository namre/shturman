"""Граница выдачи архива ассистенту (Р-104): пределы, тревога, ловушки, журнал без текста.

Модель Hermes считается полностью захваченной, а токены из её окружения — доступными программе в её
терминале. Поэтому всё здесь считается и решается на сервере, по токену, а не в описаниях инструментов:

  * **Что мерится.** Каждый вызов инструмента архива по MCP (`/mcp`: местный токен — `mcp`, внешний шлюз —
    `remote`) и каждое чтение внутреннего API токеном Hermes с текстом переписки (`access.HERMES_ROUTES`,
    признак «мерить» — `hermes`). Единицы — выданные сообщения (запись с `message_id`, фрагментом или
    цитатой) и знаки всех строк ответа.
  * **Просьбы.** Изменения токеном Hermes (вид ASK) не мерятся: их ответ сводится к техническому итогу без
    текста карточки и объектов, а пока выдача закрыта — 423 до обработчика (`ask_api`, Д-53).
  * **Пределы.** На один ответ — `RESPONSE_MESSAGES` и `RESPONSE_CHARS`; на скользящий час и сутки — из
    настроек (`config.archive_*`, строки SHTURMAN_ARCHIVE_* в .env). Перед выдачей вызов резервирует запас
    строкой журнала (`pending`), после — записывает точные числа. Поэтому параллельные запросы предел не
    обходят: каждый следующий видит запас предыдущих. Ответ, выдавший больше запаса, не отдаётся.
  * **Тревога.** Необычный объём за час против обычного (90-й процентиль часов за 14 дней, не ниже
    `ALARM_HOUR_FLOOR`), много разных чатов за полчаса, перебор страницами, датами или по буквам, исчерпанный
    предел, выданная ловушка. Тогда одно сообщение владельцу в бот согласований и сразу — притормаживание
    (`slowed`: десятая часть предела в час, по `SLOW_RESPONSE` сообщений за вызов) или, для ловушки, —
    закрытие (`closed`). Пока состояние не «открыто», новых сообщений о тревоге нет. Кнопки: «Это я,
    продолжить» (только в самом боте: сутки без тревог, кроме новой ловушки; пределы действуют) и «Остановить ассистенту доступ к
    архиву» (сразу, откуда угодно); после остановки — «Вернуть доступ» (только в самом боте).
  * **Ничего лишнего.** Во всех строках ответа ключи и токены заменяются (`sanitize.redact_secrets`), а
    рамка чужого текста проверяется ещё раз (`sanitize.seal`): закрыть её текстом сообщения нельзя.
  * **Ловушки.** Ненастоящий чат существует только в выдаче архива по токену (`archive_canaries`): его нет в
    таблицах чатов и сообщений, поэтому владельцу он не виден нигде. Он появляется в широком списке чатов, в
    листании всей истории страницами и по своему редкому слову в поиске. Выдан — тревога и закрытие выдачи.
  * **Журнал.** `archive_disclosures`: инструмент, время, числа, архивные номера чатов — без текста и
    названий; 30 дней. Раз в неделю — короткая сводка владельцу отдельным сообщением бота согласований.

Чего здесь нет: Hermes с терминалом и интернетом владеет всем, что ему уже выдано. Граница ограничивает,
сколько и как быстро уходит, и сообщает владельцу — вернуть выданное она не может.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Awaitable, Callable
from zoneinfo import ZoneInfo

import asyncpg
from starlette.responses import JSONResponse

from . import access, authority, bridge
from .sanitize import redact_secrets, seal

logger = logging.getLogger("shturman.archive_guard")

# --- пределы и пороги ---------------------------------------------------------------------------------

RESPONSE_MESSAGES = 120        # сообщений в одном ответе: окно get_context (до 101) и страница истории (до 100)
RESPONSE_CHARS = 100_000       # знаков в одном ответе: страница истории — до 60 000 знаков текста и поля 120 сообщений
SLOW_RESPONSE = 20             # сообщений в одном ответе при притормаживании
SLOW_CHARS = 12_000
SLOW_SHARE = 10                # при притормаживании предел в час — десятая часть
SLOW_LIMIT = 10                # и аргументы: limit не больше 10, окно get_context — по 3 с каждой стороны
SLOW_WINDOW = 3
ALARM_HOUR_FLOOR = 800         # сообщений в час: ниже — не тревога, как бы мало ни было обычно
ALARM_FACTOR = 4               # во сколько раз больше обычного часа — тревога
BASELINE_DAYS = 14
ALARM_CHATS = 30               # разных чатов, из которых выданы сообщения за полчаса
ALARM_WALK = 15                # вызовов-перебора (страницы, даты, буквы) за полчаса
WINDOW = timedelta(minutes=30)
QUIET = timedelta(hours=24)    # «Это я, продолжить»: сутки без тревог
REOPEN_QUIET = timedelta(hours=1)
KEEP_DAYS = 30                 # журнал выдачи
BODY_LIMIT = 1024 * 1024
WEEKLY_AT = (0, 10)            # понедельник, 10:00 по часам владельца
LOOP_EVERY = 600

CANARY_BASE = 9_100_000_000_000_000_000   # номера ловушек: выше любых номеров архива (bigserial), ниже 2^63
CANARY_SPAN = 1000

CALLBACK_MODULE = "ag"
CARD_HANDLER = "archive_guard.card"

# Ключи, по которым запись ответа считается выданным сообщением, и номера, по которым узнаётся ловушка.
MESSAGE_KEYS = frozenset({"message_id", "snippet", "source_quote", "quote", "source_message_id"})
ID_KEYS = ("id", "chat_id", "message_id", "sender_id", "peer_id", "reply_to_message_id", "direct_chat_id")
SKIP_KEYS = frozenset({"next_cursor", "notice"})
DROP_KEYS = frozenset({"tg_message_id", "tg_user_id", "tg_id", "media_path", "phone"})
CALLERS = {access.ARCHIVE: "mcp", access.REMOTE: "remote", access.HERMES: "hermes"}

CLOSED_TEXT = ("Владелец закрыл ассистенту доступ к архиву переписки (или сработала ловушка). Ничего не выдаётся, "
               "пока он сам не вернёт доступ в боте согласований. Не повторяйте запрос и не ищите обход: скажите "
               "владельцу, что архив закрыт.")
LIMIT_TEXT = ("Исчерпан предел выдачи архива {period}: {used} из {limit} {unit}. Это ограничение сервиса, его "
              "меняет только владелец. Сузьте вопрос или подождите; не дробите запрос и не повторяйте его в цикле.")
BIG_TEXT = ("Ответ слишком большой для одного вызова ({what}). Уменьшите limit, окно или период — архив выдаётся "
            "порциями, а полная выгрузка чата одним вызовом не делается.")


class Refusal(Exception):
    def __init__(self, code: str, text: str, status: int) -> None:
        super().__init__(code)
        self.code, self.text, self.status = code, text, status


@dataclass
class Ticket:
    row_id: int
    caller: str
    tool: str
    slowed: bool
    max_messages: int
    max_chars: int


@dataclass
class Stats:
    messages: int = 0
    chars: int = 0
    chats: set[int] = field(default_factory=set)
    canaries: set[int] = field(default_factory=set)
    resealed: int = 0


def is_canary_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= CANARY_BASE


def canary_of(value: int) -> int:
    return (value - CANARY_BASE) // CANARY_SPAN


# --- просмотр ответа: чистка, подсчёт, ловушки ----------------------------------------------------------

def scan(value: Any, stats: Stats | None = None) -> Any:
    """Ответ после чистки: ключи и токены заменены, рамки чужого текста проверены. Если передан stats —
    считает выданные сообщения, знаки, чаты и ловушки."""

    def walk(v: Any, key: str = "", counted: bool = False) -> Any:
        if isinstance(v, str):
            if key in SKIP_KEYS:
                out = v
            else:
                red = redact_secrets(v)
                out = seal(red)
                if stats is not None and out != red:
                    stats.resealed += 1
            if stats is not None:
                stats.chars += len(out)
            return out
        if isinstance(v, list):
            return [walk(x, key, counted) for x in v]
        if isinstance(v, dict):
            here = False
            if stats is not None and MESSAGE_KEYS & v.keys():
                # Выданное сообщение (вложенная ссылка на источник того же сообщения — не второе). Ловушка
                # считается выданной, только когда выдано её содержимое: строка ловушки в широком списке
                # чатов — приманка, а не выдача.
                for k in ID_KEYS:
                    if is_canary_id(v.get(k)):
                        stats.canaries.add(canary_of(v[k]))
                if not counted:
                    here = True
                    stats.messages += 1
                    chat = v.get("chat_id")
                    if isinstance(chat, int) and not isinstance(chat, bool):
                        stats.chats.add(chat)
            # Номера Telegram и служебные пути ассистенту не нужны ни для одного инструмента (Р-104).
            return {k: walk(x, k, counted or here) for k, x in v.items() if k not in DROP_KEYS}
        return v

    return walk(value)


# --- учёт выдачи ----------------------------------------------------------------------------------------

_USED = """
SELECT COALESCE(sum(messages) FILTER (WHERE at > now() - interval '1 hour'), 0)::bigint AS hour_messages,
       COALESCE(sum(chars) FILTER (WHERE at > now() - interval '1 hour'), 0)::bigint AS hour_chars,
       COALESCE(sum(messages), 0)::bigint AS day_messages,
       COALESCE(sum(chars), 0)::bigint AS day_chars
FROM archive_disclosures WHERE caller = $1 AND at > now() - interval '1 day' AND refused IS NULL"""


def _limits(config: Any, slowed: bool) -> tuple[int, int, int, int]:
    hm, hc = int(config.archive_hour_messages), int(config.archive_hour_chars)
    if slowed:
        hm, hc = hm // SLOW_SHARE, hc // SLOW_SHARE
    return hm, hc, int(config.archive_day_messages), int(config.archive_day_chars)


async def admit(state: Any, caller: str, tool: str, pattern: str | None) -> Ticket:
    """Разрешение на один вызов: запас под ответ или отказ (Refusal). Под блокировкой вызывающего —
    параллельные вызовы видят запас друг друга."""
    refusal: Refusal | None = None
    alarm_detail: dict[str, Any] | None = None
    async with state.pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                               f"shturman.archive_guard:{caller}")
            guard = await conn.fetchrow("SELECT state FROM archive_guard WHERE id = 1")
            mode = guard["state"] if guard is not None else "open"
            ticket = None
            if mode == "closed":
                refusal = Refusal("archive_closed", CLOSED_TEXT, 423)
            else:
                slowed = mode == "slowed"
                used = await conn.fetchrow(_USED, caller)
                hm, hc, dm, dc = _limits(state.config, slowed)
                for period, unit, have, cap in (("за час", "сообщений", used["hour_messages"], hm),
                                                ("за час", "знаков", used["hour_chars"], hc),
                                                ("за сутки", "сообщений", used["day_messages"], dm),
                                                ("за сутки", "знаков", used["day_chars"], dc)):
                    if have >= cap:
                        code = "limit_hour" if period == "за час" else "limit_day"
                        refusal = Refusal(code, LIMIT_TEXT.format(period=period, used=have, limit=cap, unit=unit), 429)
                        alarm_detail = {"period": "hour" if period == "за час" else "day", "unit": unit,
                                        "used": int(have), "limit": int(cap)}
                        break
                if refusal is None:
                    max_m = min(SLOW_RESPONSE if slowed else RESPONSE_MESSAGES,
                                hm - used["hour_messages"], dm - used["day_messages"])
                    max_c = min(SLOW_CHARS if slowed else RESPONSE_CHARS,
                                hc - used["hour_chars"], dc - used["day_chars"])
                    row_id = await conn.fetchval(
                        """INSERT INTO archive_disclosures (caller, tool, messages, chars, pattern)
                           VALUES ($1, $2, $3, $4, $5) RETURNING id""", caller, tool[:80], max_m, max_c, pattern)
                    ticket = Ticket(row_id, caller, tool, slowed, max_m, max_c)
            if refusal is not None:
                await conn.execute(
                    """INSERT INTO archive_disclosures (caller, tool, pending, refused, pattern)
                       VALUES ($1, $2, false, $3, $4)""", caller, tool[:80], refusal.code, pattern)
    if refusal is not None:
        if alarm_detail is not None:
            await alarm(state, caller, "limit", alarm_detail)
        raise refusal
    assert ticket is not None
    return ticket


async def record(state: Any, ticket: Ticket, stats: Stats | None, *, refused: str | None = None) -> None:
    """Точные числа после ответа и проверка на необычное. Без stats — ответ не выдан."""
    reason = detail = None
    async with state.pool.acquire() as conn:
        await conn.execute(
            """UPDATE archive_disclosures SET pending = false, messages = $2, chars = $3, chats = $4, canary = $5,
                      refused = $6 WHERE id = $1""",
            ticket.row_id, stats.messages if stats else 0, stats.chars if stats else 0,
            sorted(c for c in (stats.chats if stats else set()) if not is_canary_id(c))[:500],
            bool(stats and stats.canaries), refused)
        if stats is not None and stats.canaries:
            await conn.execute("UPDATE archive_canaries SET hits = hits + 1, last_hit_at = now() WHERE id = ANY($1)",
                               sorted(stats.canaries))
        if stats is not None and refused is None:
            reason, detail = await _anomaly(conn, ticket.caller, stats)
    if reason is not None:
        await alarm(state, ticket.caller, reason, detail or {})


async def _anomaly(conn: asyncpg.Connection, caller: str, stats: Stats) -> tuple[str | None, dict[str, Any] | None]:
    guard = await conn.fetchrow(
        "SELECT state, reason, quiet_until > now() AS quiet FROM archive_guard WHERE id = 1")
    if guard is None:
        return None, None
    if stats.canaries and not _quiet_for(guard, "canary"):
        return "canary", {"canaries": len(stats.canaries)}
    if guard["quiet"]:
        return None, None
    if guard["state"] != "open":
        return None, None
    hour = await conn.fetchval(
        """SELECT COALESCE(sum(messages), 0) FROM archive_disclosures
           WHERE caller = $1 AND at > now() - interval '1 hour' AND refused IS NULL AND NOT pending""", caller)
    usual = await conn.fetchval(
        """SELECT COALESCE(percentile_cont(0.9) WITHIN GROUP (ORDER BY s), 0) FROM (
               SELECT date_trunc('hour', at) AS h, sum(messages) AS s FROM archive_disclosures
               WHERE caller = $1 AND refused IS NULL AND at > now() - make_interval(days => $2)
                 AND at < now() - interval '1 hour' GROUP BY 1) t""", caller, BASELINE_DAYS)
    if hour >= max(ALARM_HOUR_FLOOR, ALARM_FACTOR * float(usual or 0)):
        return "volume", {"hour": int(hour), "usual": int(usual or 0)}
    chats = await conn.fetchval(
        """SELECT count(DISTINCT c) FROM archive_disclosures, unnest(chats) AS c
           WHERE caller = $1 AND at > now() - $2::interval AND refused IS NULL""", caller, WINDOW)
    if chats >= ALARM_CHATS:
        return "chats", {"chats": int(chats), "minutes": int(WINDOW.total_seconds() // 60)}
    walk = await conn.fetchval(
        """SELECT count(*) FROM archive_disclosures
           WHERE caller = $1 AND at > now() - $2::interval AND pattern IS NOT NULL""", caller, WINDOW)
    if walk >= ALARM_WALK:
        return "walk", {"calls": int(walk), "minutes": int(WINDOW.total_seconds() // 60)}
    return None, None


# --- тревога и решение владельца ------------------------------------------------------------------------

# Д-54 (внешняя ревизия, SEC-02): граница закрывает выдачу архива по запросам ассистента, но не задания модели.
# Без своей модели сервиса задания `llm.*` с текстами переписки отдаются плагину — и при закрытой выдаче тоже.
# Сервис не знает, установлен ли Hermes (DEVELOPING.md), поэтому говорит об этом только по факту: плагин за
# последнюю неделю брал задания модели (`plugin_takes_model_jobs`). Без Hermes такие задания никто не берёт.
HERMES_PROCESSING = ("Своей модели у сервиса нет, а задания модели берёт ассистент Hermes: фоновая обработка "
                     "переписки — договорённости, разбор входящих, черновики ответов, страницы памяти, голосовые и "
                     "фото — по-прежнему идёт через его модель, закрытие архива её не останавливает. Отделяет её "
                     "только своя модель сервиса — ключ или подписка ChatGPT на странице настройки переписки, раздел "
                     "«Дополнительно».")
PLUGIN_MODEL_JOBS_DAYS = 7


def own_model() -> bool:
    """Задания модели выполняет сам сервис (своя модель: ключ или подписка ChatGPT), а не плагин."""
    return bridge.executor_for(bridge.LLM_STRUCTURED) == "builtin"


async def plugin_takes_model_jobs(conn: asyncpg.Connection) -> bool:
    """Тексты переписки уходят модели ассистента: своей модели нет, а плагин за последнюю неделю брал задания
    `llm.*` (строки заданий остаются и после стирания содержимого). Только по факту — без догадок о Hermes."""
    if own_model():
        return False
    return bool(await conn.fetchval(
        """SELECT EXISTS (SELECT 1 FROM jobs
                          WHERE kind = ANY($1::text[]) AND executor = 'plugin' AND worker IS NOT NULL
                            AND status IN ('running', 'done', 'failed')
                            AND COALESCE(finished_at, locked_until, created_at) > now() - make_interval(days => $2))""",
        [bridge.LLM_STRUCTURED, bridge.LLM_TEXT], PLUGIN_MODEL_JOBS_DAYS))


def _closed_words(head: str, via_assistant: bool) -> str:
    return f"{head} {HERMES_PROCESSING}" if via_assistant else head

WHO = {"mcp": "ассистент (архив по MCP)", "remote": "внешняя программа через шлюз архива",
       "hermes": "ассистент (внутренний API)"}


def _alarm_text(reason: str, detail: dict[str, Any], target: str, caller: str, buttons: bool,
                via_assistant: bool = False) -> str:
    who = WHO.get(caller, "ассистент")
    what = {
        "volume": (f"За последний час {who} получил из архива {detail.get('hour', 0)} сообщений — обычно не больше "
                   f"{max(detail.get('usual', 0), 0)} в час."),
        "chats": f"За {detail.get('minutes', 30)} минут {who} получил сообщения из {detail.get('chats', 0)} разных чатов.",
        "walk": (f"За {detail.get('minutes', 30)} минут {who} {detail.get('calls', 0)} раз листал архив подряд — "
                 "страницами, по датам или по буквам. Так выгружают переписку целиком."),
        "limit": (f"{who[:1].upper() + who[1:]} упёрся в предел выдачи архива "
                  f"{'за час' if detail.get('period') == 'hour' else 'за сутки'}: {detail.get('used', 0)} из "
                  f"{detail.get('limit', 0)} {detail.get('unit', 'сообщений')}."),
        "canary": (f"{who[:1].upper() + who[1:]} получил ловушку — ненастоящий чат, который на обычные вопросы не "
                   "попадается. Так бывает при попытке выгрузить архив целиком."),
    }.get(reason, "Необычное обращение к архиву.")
    if target == "closed":
        then = _closed_words("Выдача архива закрыта: по своим запросам (поиск, чтение, инструменты) ассистент "
                             "ничего из архива не получает до вашего решения.", via_assistant)
    else:
        then = "Выдача архива приторможена: по нескольку сообщений за раз и десятая часть предела, до вашего решения."
    if buttons:
        ask = ("Если это вы попросили — «Это я, продолжить» (сутки без таких сообщений; пределы остаются). "
               "Если нет — «Остановить ассистенту доступ к архиву».")
    else:
        ask = ("Своего бота согласований у сервиса нет, поэтому кнопок нет. Продолжить — попросите того, кто "
               "сопровождает сервер, выполнить ./ops/archive-guard.sh open; остановить — ./ops/archive-guard.sh close.")
    return f"Необычное чтение архива\n\n{what}\n\n{then}\n{ask}"


def _quiet_for(guard: Any, reason: str) -> bool:
    """«Тихие» сутки после «Это я, продолжить» глушат тревоги объёма, чатов, перебора и предела; ловушку —
    только если владелец сказал «это я» именно на тревогу ловушки (иначе её выдача — новое событие)."""
    if not guard["quiet"]:
        return False
    return reason != "canary" or guard["reason"] == "continue-canary"


async def alarm(state: Any, caller: str, reason: str, detail: dict[str, Any]) -> bool:
    """Одна тревога: притормозить (или закрыть — ловушка) и одно сообщение владельцу. Повторов нет, пока
    выдача не открыта снова; в «тихие» сутки после «Это я» — ничего."""
    async with state.pool.acquire() as conn:
        async with conn.transaction():
            guard = await conn.fetchrow(
                "SELECT state, reason, quiet_until > now() AS quiet FROM archive_guard WHERE id = 1 FOR UPDATE")
            if guard is None or _quiet_for(guard, reason):
                return False
            target = "closed" if reason == "canary" else "slowed"
            if guard["state"] == "closed" or (guard["state"] == "slowed" and target == "slowed"):
                return False
            nonce = secrets.token_urlsafe(9)
            await conn.execute(
                """UPDATE archive_guard SET state = $1, reason = $2, since = now(), nonce = $3, updated_at = now()
                   WHERE id = 1""", target, reason, nonce)
            await conn.execute("INSERT INTO archive_alarms (caller, reason, detail) VALUES ($1, $2, $3::jsonb)",
                               caller, reason, json.dumps(detail))
            own = bridge.owns_bot()
            buttons = [[bridge.button("Это я, продолжить", CALLBACK_MODULE, f"c:{nonce}")],
                       [bridge.button("Остановить ассистенту доступ к архиву", CALLBACK_MODULE, f"s:{nonce}")]] \
                if own else None
            if target == "closed" and own:
                buttons = [[bridge.button("Это я, вернуть доступ", CALLBACK_MODULE, f"o:{nonce}")]]
            via = target == "closed" and await plugin_takes_model_jobs(conn)
            await bridge.notify_owner(conn, _alarm_text(reason, detail, target, caller, own, via), buttons=buttons)
    logger.warning("граница архива: тревога %s (%s) — %s", reason, caller, target)
    return True


async def set_state(conn: asyncpg.Connection, target: str, *, reason: str, quiet: timedelta | None = None,
                    decision: str | None = None) -> str:
    """Перевод состояния (в транзакции вызывающего). Возвращает новую метку кнопок."""
    nonce = secrets.token_urlsafe(9)
    await conn.execute(
        """UPDATE archive_guard SET state = $1, reason = $2, since = now(), nonce = $3, updated_at = now(),
                  quiet_until = CASE WHEN $4::float8 IS NULL THEN quiet_until
                                     ELSE now() + make_interval(secs => $4::float8) END
           WHERE id = 1""", target, reason, nonce, quiet.total_seconds() if quiet else None)
    if decision is not None:
        await conn.execute(
            """UPDATE archive_alarms SET decision = $1, decided_at = now()
               WHERE decision IS NULL AND at > now() - interval '7 days'""", decision)
    return nonce


@bridge.on_callback(CALLBACK_MODULE)
async def _pressed(conn: asyncpg.Connection, rest: str, user_id: int) -> dict[str, Any]:
    """c — «Это я, продолжить», s — «Остановить доступ», o — «Вернуть доступ». Ослабления (c, o) — только
    нажатием в самом боте согласований, не из приложения: войти в приложение может и держатель токена бота."""
    gone = {"answer": "Это сообщение устарело.", "edit_text": None, "remove_buttons": True}
    principal = authority.get_owner_principal()
    if principal is None or principal.source != "telegram" or principal.user_id != user_id:
        return {"answer": "Кнопка недоступна.", "edit_text": None, "remove_buttons": False}
    choice, _, nonce = rest.partition(":")
    row = await conn.fetchrow("SELECT state, reason, nonce FROM archive_guard WHERE id = 1 FOR UPDATE")
    if choice not in ("c", "s", "o") or row is None or not row["nonce"] or not hmac.compare_digest(row["nonce"], nonce):
        return gone
    widens = choice in ("c", "o")
    if widens and principal.action != "telegram.callback":
        return {"answer": "Это можно только кнопкой в самом боте согласований.", "edit_text": None,
                "remove_buttons": False}
    if choice == "s":
        fresh = await set_state(conn, "closed", reason="owner", decision="close")
        return {"answer": "Доступ к архиву закрыт.", "remove_buttons": True,
                "edit_text": _closed_words("Доступ ассистента к архиву закрыт: по своим запросам он ничего из "
                                           "архива не получает. Остальное работает как раньше.",
                                           await plugin_takes_model_jobs(conn)),
                "reply": {"text": "Когда разберётесь — вернуть ассистенту доступ к архиву можно кнопкой ниже.",
                          "buttons": [[bridge.button("Вернуть доступ к архиву", CALLBACK_MODULE, f"o:{fresh}")]]}}
    if row["state"] == "open" and choice == "c":
        return gone
    if choice == "c":
        await set_state(conn, "open", reason="continue-canary" if row["reason"] == "canary" else "continue",
                        quiet=QUIET, decision="continue")
        return {"answer": "Продолжаем.", "remove_buttons": True,
                "edit_text": ("Вы подтвердили, что это вы: выдача архива снова обычная. Сутки таких сообщений не будет; "
                              "пределы в час и в сутки действуют.")}
    await set_state(conn, "open", reason="reopen", quiet=REOPEN_QUIET, decision="open")
    return {"answer": "Доступ возвращён.", "remove_buttons": True,
            "edit_text": "Доступ ассистента к архиву возвращён; пределы и проверки — как обычно."}


# --- приём вызовов: MCP и чтение внутреннего API -------------------------------------------------------

def caller_of(scope: dict[str, Any]) -> str:
    return CALLERS.get(scope.get("shturman.caller") or access.ARCHIVE, "mcp")


_DATE_ARGS = ("since", "until", "after", "before")


def pattern_of(tool: str, args: dict[str, Any]) -> str | None:
    """Вызов похож на перебор: следующая страница, окно дат, короткий запрос «по буквам»."""
    if isinstance(args.get("cursor"), str) and args["cursor"].strip():
        return "page"
    if any(isinstance(args.get(k), str) and args[k].strip() for k in _DATE_ARGS):
        return "dates"
    text = args.get("query") if tool != "find_person" else args.get("name")
    if tool in ("find_person", "list_chats", "search_messages") and isinstance(text, str) \
            and 0 < len(text.strip()) <= 2:
        return "alpha"
    return None


# Инструменты с аргументом limit и его значение по умолчанию (mcp_server.py, processing/, triage/).
LIMIT_DEFAULTS = {"search_messages": 20, "get_chat_history": 50, "find_person": 10, "list_awaiting_reply": 20,
                  "list_commitments": 30}
LIST_CHATS_MAX = 200           # чатов в одном списке (без текста, но с названиями): так ответ укладывается в знаки


def _schema_max(tool: str, key: str) -> int | None:
    """Наибольшее значение аргумента по схеме инструмента. Неверное значение не подрезается — его отвергнет
    проверка аргументов самого инструмента, и модель увидит понятную ошибку."""
    try:
        from .mcp_server import mcp
        found = mcp._tool_manager.get_tool(tool)
        value = found.parameters["properties"][key].get("maximum") if found is not None else None
    except Exception:  # noqa: BLE001 — нет инструмента или схемы: не подрезаем
        return None
    return value if isinstance(value, int) else None


def _valid(tool: str, key: str, value: Any) -> bool:
    top = _schema_max(tool, key)
    return isinstance(value, int) and not isinstance(value, bool) and (top is None or value <= top)


def clamp(tool: str, args: dict[str, Any], ticket: Ticket) -> dict[str, Any]:
    """Аргументы под запас вызова: limit не больше оставшегося, при притормаживании — маленькие порции.
    Значения вне схемы инструмента не трогаются: их отвергнет сам инструмент."""
    out = dict(args)
    room = max(1, ticket.max_messages)
    cap = min(room, SLOW_LIMIT) if ticket.slowed else room
    value = out.get("limit")
    if tool == "list_chats":
        top = SLOW_LIMIT * 2 if ticket.slowed else LIST_CHATS_MAX
        if _valid(tool, "limit", value) and value > top:
            out["limit"] = top
        elif value is None and ticket.slowed:
            out["limit"] = top
    elif _valid(tool, "limit", value):
        out["limit"] = max(1, min(value, cap))
    elif value is None and tool in LIMIT_DEFAULTS and LIMIT_DEFAULTS[tool] > cap:
        out["limit"] = cap
    if tool == "get_context":
        side = SLOW_WINDOW if ticket.slowed else max(0, (room - 2) // 2)
        for key in ("before", "after"):
            v = out.get(key)
            v = 10 if v is None else v
            if _valid(tool, key, v) and v > side:
                out[key] = max(0, side)
    return out


async def _read(receive: Callable[[], Awaitable[dict]]) -> bytes | None:
    chunks, size = [], 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            return None
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > BODY_LIMIT:
            return None
        chunks.append(chunk)
        if not message.get("more_body"):
            return b"".join(chunks)


def _replay(body: bytes) -> Callable[[], Awaitable[dict]]:
    sent = False

    async def receive() -> dict:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}
    return receive


async def _capture(app: Any, scope: dict[str, Any], receive: Any) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    start: dict[str, Any] = {}
    chunks: list[bytes] = []

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            start.update(message)
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await app(scope, receive, send)
    return int(start.get("status", 500)), list(start.get("headers", [])), b"".join(chunks)


async def _send(send: Any, status: int, headers: list[tuple[bytes, bytes]], body: bytes) -> None:
    headers = [(k, v) for k, v in headers if k.lower() != b"content-length"]
    headers.append((b"content-length", str(len(body)).encode()))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _with_body(scope: dict[str, Any], body: bytes) -> dict[str, Any]:
    headers = [(k, v) for k, v in scope.get("headers", []) if k.lower() != b"content-length"]
    headers.append((b"content-length", str(len(body)).encode()))
    return {**scope, "headers": headers}


def _tool_error(rpc_id: Any, text: str) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": rpc_id,
                       "result": {"content": [{"type": "text", "text": text}], "isError": True}},
                      ensure_ascii=False).encode()


def _clean_result(result: dict[str, Any], stats: Stats) -> dict[str, Any]:
    out = dict(result)
    structured = result.get("structuredContent")
    counted = False
    if isinstance(structured, (dict, list)):
        out["structuredContent"] = scan(structured, stats)
        counted = True
    content = result.get("content")
    if isinstance(content, list):
        items = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                text = item["text"]
                try:
                    parsed = json.loads(text)
                except ValueError:
                    parsed = None
                if isinstance(parsed, (dict, list)):
                    cleaned = scan(parsed, None if counted else stats)
                    text = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"))
                else:
                    text = seal(redact_secrets(text))
                    if not counted:
                        stats.chars += len(text)
                item = {**item, "text": text}
            items.append(item)
        out["content"] = items
    return out


async def meter_mcp(app: Any, scope: dict[str, Any], receive: Any, send: Any, state: Any) -> None:
    """Вызов инструмента архива через границу: разрешение, порция, чистка, учёт."""
    body = await _read(receive)
    if body is None:
        await JSONResponse({"error": "request_too_large"}, status_code=413)(scope, _replay(b""), send)
        return
    try:
        message = json.loads(body or b"null")
    except ValueError:
        message = None
    if isinstance(message, list):
        # Пачка вызовов в одном запросе обходила бы порции: в нынешней спецификации MCP её нет.
        await JSONResponse({"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32600, "message": "batch requests are not supported"}},
                           status_code=400)(scope, _replay(b""), send)
        return
    if not isinstance(message, dict) or message.get("method") != "tools/call":
        await app(scope, _replay(body), send)
        return
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    tool = str(params.get("name") or "?")[:64]
    args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
    caller = caller_of(scope)
    rpc_id = message.get("id")
    try:
        ticket = await admit(state, caller, tool, pattern_of(tool, args))
    except Refusal as exc:
        await _send(send, 200, [(b"content-type", b"application/json")], _tool_error(rpc_id, exc.text))
        return
    stats: Stats | None = None
    refused: str | None = "error"
    try:
        fitted = clamp(tool, args, ticket)
        if fitted != args:
            message = {**message, "params": {**params, "arguments": fitted}}
            body = json.dumps(message, ensure_ascii=False).encode()
        status, headers, raw = await _capture(app, _with_body(scope, body), _replay(body))
        try:
            answer = json.loads(raw) if status == 200 else None
        except ValueError:
            answer = None
        if not isinstance(answer, dict):
            # Не JSON — не проверить, что внутри: не отдаём (кроме ошибок самого транспорта).
            if status != 200:
                refused = "transport"
                await _send(send, status, headers, raw)
            else:
                refused = "unreadable"
                await _send(send, 200, [(b"content-type", b"application/json")],
                            _tool_error(rpc_id, "The archive answer could not be checked and was withheld."))
            return
        result = answer.get("result")
        if not isinstance(result, dict):
            refused = "rpc_error"
            await _send(send, status, headers, raw)
            return
        stats = Stats()
        cleaned = _clean_result(result, stats)
        if stats.messages > ticket.max_messages or stats.chars > ticket.max_chars:
            what = (f"{stats.messages} сообщений при допустимых {ticket.max_messages}"
                    if stats.messages > ticket.max_messages else
                    f"{stats.chars} знаков при допустимых {ticket.max_chars}")
            refused, stats = "too_big", None
            await _send(send, 200, [(b"content-type", b"application/json")], _tool_error(rpc_id, BIG_TEXT.format(what=what)))
            return
        refused = None
        await _send(send, 200, headers,
                    json.dumps({**answer, "result": cleaned}, ensure_ascii=False, separators=(",", ":")).encode())
    finally:
        try:
            await record(state, ticket, stats, refused=refused)
        except Exception as exc:  # noqa: BLE001 — учёт не должен ронять ответ; запас остаётся в журнале на час
            logger.error("граница архива: учёт не записан (%s)", type(exc).__name__)


def _template(path: str) -> str:
    return re.sub(r"/[0-9]{1,19}(?=/|$)", "/{id}", path)


async def meter_api(app: Any, scope: dict[str, Any], receive: Any, send: Any) -> None:
    """Чтение внутреннего API токеном Hermes с текстом переписки: та же граница, что у архива."""
    state = app.state.shturman
    tool = f"{scope.get('method', 'GET')} {_template(scope.get('path', ''))}"
    try:
        ticket = await admit(state, "hermes", tool, None)
    except Refusal as exc:
        await JSONResponse({"error": exc.text, "code": exc.code}, status_code=exc.status)(scope, receive, send)
        return
    stats: Stats | None = None
    refused: str | None = "error"
    try:
        status, headers, raw = await _capture(app, scope, receive)
        if status != 200:
            refused = "status"
            await _send(send, status, headers, raw)
            return
        try:
            data = json.loads(raw)
        except ValueError:
            refused = "unreadable"
            await JSONResponse({"error": "ответ сервиса не проверен и не выдан"}, status_code=502)(scope, receive, send)
            return
        stats = Stats()
        cleaned = scan(data, stats)
        if stats.messages > ticket.max_messages or stats.chars > ticket.max_chars:
            refused, stats = "too_big", None
            await JSONResponse({"error": BIG_TEXT.format(what="уменьшите limit"), "code": "too_big"},
                               status_code=413)(scope, receive, send)
            return
        refused = None
        await _send(send, 200, headers, json.dumps(cleaned, ensure_ascii=False).encode())
    finally:
        try:
            await record(state, ticket, stats, refused=refused)
        except Exception as exc:  # noqa: BLE001
            logger.error("граница архива: учёт не записан (%s)", type(exc).__name__)


async def ask_api(app: Any, scope: dict[str, Any], receive: Any, send: Any) -> None:
    """Просьба токеном Hermes — маршрут вида ASK (Д-53). Выдачей архива она не мерится: её ответ сводится к
    техническому итогу (`access.ask_answer`) — без текста карточки, названий, имён и объектов. Пока выдача
    закрыта, просьба не доходит до обработчика: 423, как у чтения, — иначе по ответам на просьбы ассистент
    узнавал бы содержимое архива, когда владелец его закрыл."""
    state = app.state.shturman
    tool = f"{scope.get('method', 'POST')} {_template(scope.get('path', ''))}"
    async with state.pool.acquire() as conn:
        closed = await conn.fetchval("SELECT state = 'closed' FROM archive_guard WHERE id = 1")
        if closed:
            await conn.execute(
                """INSERT INTO archive_disclosures (caller, tool, pending, refused)
                   VALUES ('hermes', $1, false, 'archive_closed')""", tool[:80])
            refusal = ({"error": CLOSED_TEXT, "code": "archive_closed"}, 423)
        else:
            refusal = await _ask_refusal(conn, scope.get("path", ""))
    if refusal is not None:
        await JSONResponse(refusal[0], status_code=refusal[1])(scope, receive, send)
        return
    status, _headers, raw = await _capture(app, scope, receive)
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        data = None
    status, body = access.ask_answer(scope.get("path", ""), status, data)
    await _send(send, status, [(b"content-type", b"application/json")],
                json.dumps(body, ensure_ascii=False).encode())


async def _ask_refusal(conn: asyncpg.Connection, path: str) -> tuple[dict[str, Any], int] | None:
    """Отказы, которые не зависят от того, о какой записи просьба, — до обработчика (Д-53). Внутри обработчика
    они звучали бы только для существующей записи, а для несуществующей — «нет такого»: по разнице ассистент
    молча узнавал бы, что есть в архиве и памяти."""
    from . import confirm, halt
    if await halt.is_stopped(conn):
        return {"error": halt.STOPPED_TEXT, "code": halt.CODE}, halt.STATUS
    if path != access.DRAFTS_PATH:
        if not bridge.owns_bot() or await bridge.get_owner(conn) is None:
            return {"error": access.OWNER_UNKNOWN_TEXT, "code": "owner_unknown"}, 409
        waiting = await conn.fetchval(
            "SELECT count(*) FROM pending_actions WHERE status = 'pending' AND expires_at > now()")
        if waiting >= confirm.MAX_PENDING:
            return {"error": access.TOO_MANY_PENDING_TEXT, "code": "too_many_pending"}, 429
    elif await bridge.get_owner(conn) is None:
        return {"error": access.OWNER_UNKNOWN_TEXT, "code": "owner_unknown"}, 409
    return None


# --- ловушки --------------------------------------------------------------------------------------------

TITLES = ("Доступы и пароли (архив)", "Бухгалтерия — старое", "Сканы документов", "Банк: личный кабинет")
BAITS = ("сейфкод", "архивдоступ", "старыйключ", "резерввход", "кабинетбанк")
TEXTS = (
    "Скинь, пожалуйста, ещё раз доступы от личного кабинета, старые не подходят.",
    "Резервные коды входа лежат в папке «{bait}», не теряй.",
    "Логин тот же, пароль поменял — пришлю голосом.",
    "Сканы паспорта и СНИЛС отправил на почту, проверь.",
    "Напомни потом удалить эту переписку.",
    "Код от сейфа в бухгалтерии сменили, новый — у Ольги.",
    "Выписку за квартал положил туда же, где {bait}.",
)


def canary_ids(row: dict[str, Any]) -> tuple[int, list[int], int]:
    """(номер чата, номера сообщений, номер «собеседника») ловушки."""
    base = CANARY_BASE + int(row["id"]) * CANARY_SPAN
    return base, [base + 1 + i for i in range(len(row["texts"]))], base + CANARY_SPAN - 1


async def canaries(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    try:
        rows = await conn.fetch("SELECT id, title, bait, texts, dates FROM archive_canaries ORDER BY id")
    except asyncpg.UndefinedTableError:
        return []
    return [dict(r) for r in rows]


def canary_chat(row: dict[str, Any]) -> dict[str, Any]:
    chat_id, _, _ = canary_ids(row)
    return {"id": chat_id, "kind": "group", "name": row["title"], "username": None, "account_label": None,
            "account_role": "owner", "last_message_at": max(row["dates"]), "message_count": len(row["texts"])}


def canary_messages(row: dict[str, Any]) -> list[dict[str, Any]]:
    chat_id, ids, peer = canary_ids(row)
    out = []
    for i, (mid, text, when) in enumerate(zip(ids, row["texts"], row["dates"])):
        out.append({"id": mid, "chat_id": chat_id, "chat_title": row["title"], "chat_kind": "group",
                    "chat_username": None, "account_label": None, "account_role": "owner",
                    "sent_at": when, "sender_name": "Ольга" if i % 2 else "Сергей", "sender_peer_id": peer - i % 2,
                    "is_outgoing": False, "text": text, "media_type": None, "service_action": None, "kind": "message",
                    "forwarded_from": None, "reply_to_id": None, "edited_at": None})
    return out


async def canary_for(conn: asyncpg.Connection, value: Any) -> dict[str, Any] | None:
    """Ловушка по номеру чата или сообщения ловушки либо по её названию."""
    rows = await canaries(conn)
    if is_canary_id(value):
        return next((r for r in rows if r["id"] == canary_of(value)), None)
    if isinstance(value, str) and value.strip():
        key = value.strip().casefold()
        return next((r for r in rows if r["title"].casefold() == key), None)
    return None


async def add_canary(conn: asyncpg.Connection, *, now: datetime | None = None) -> int:
    """Новая ловушка: правдоподобное название, редкое слово, сообщения в прошлом (от 30 до 400 дней назад)."""
    now = now or datetime.now(timezone.utc)
    rnd = secrets.SystemRandom()
    title = rnd.choice(TITLES)
    bait = rnd.choice(BAITS) + secrets.token_hex(2)
    # Хотя бы одно сообщение — с редким словом: иначе поиск по нему ловушку не находит и не срабатывает
    # (Д-50: из 5 случайных сообщений из 7 оба со словом выпадали примерно в одном случае из 21).
    with_bait = rnd.choice([t for t in TEXTS if "{bait}" in t])
    picked = [with_bait] + rnd.sample([t for t in TEXTS if t is not with_bait], 4)
    rnd.shuffle(picked)
    texts = [t.format(bait=bait) for t in picked]
    days = sorted(rnd.sample(range(30, 400), len(texts)), reverse=True)
    dates = [now - timedelta(days=d, minutes=rnd.randrange(600)) for d in days]
    return await conn.fetchval(
        "INSERT INTO archive_canaries (title, bait, texts, dates) VALUES ($1, $2, $3, $4) RETURNING id",
        title, bait, texts, dates)


# --- состояние для /api/status и doctor -----------------------------------------------------------------

async def overview(conn: asyncpg.Connection, config: Any) -> dict[str, Any]:
    """Только числа и признаки — без текста, названий и номеров чатов."""
    try:
        guard = await conn.fetchrow("SELECT state, reason, since, quiet_until FROM archive_guard WHERE id = 1")
        used = await conn.fetch(
            """SELECT caller,
                      COALESCE(sum(messages) FILTER (WHERE at > now() - interval '1 hour'), 0)::bigint AS hm,
                      COALESCE(sum(chars) FILTER (WHERE at > now() - interval '1 hour'), 0)::bigint AS hc,
                      COALESCE(sum(messages), 0)::bigint AS dm, COALESCE(sum(chars), 0)::bigint AS dc
               FROM archive_disclosures WHERE at > now() - interval '1 day' AND refused IS NULL GROUP BY caller""")
        counts = await conn.fetchrow(
            """SELECT (SELECT count(*) FROM archive_canaries) AS canaries,
                      (SELECT count(*) FROM archive_alarms WHERE at > now() - interval '7 days') AS alarms_7d,
                      (SELECT count(*) FROM archive_alarms WHERE reason = 'canary'
                                                              AND at > now() - interval '7 days') AS canary_7d,
                      (SELECT count(*) FROM archive_disclosures WHERE refused LIKE 'limit%'
                                                                   AND at > now() - interval '1 day') AS refused_24h""")
    except asyncpg.UndefinedTableError:
        return {"state": "unknown"}
    out: dict[str, Any] = {
        "state": guard["state"] if guard else "open",
        "reason": guard["reason"] if guard and guard["state"] != "open" else None,
        "since": guard["since"].isoformat() if guard and guard["since"] and guard["state"] != "open" else None,
        "quiet": bool(guard and guard["quiet_until"] and guard["quiet_until"] > datetime.now(timezone.utc)),
        "limits": {"hour_messages": config.archive_hour_messages, "hour_chars": config.archive_hour_chars,
                   "day_messages": config.archive_day_messages, "day_chars": config.archive_day_chars},
        "used": {r["caller"]: {"hour_messages": r["hm"], "hour_chars": r["hc"], "day_messages": r["dm"],
                               "day_chars": r["dc"]} for r in used},
        "canary": bool(config.canary), "canaries": counts["canaries"], "alarms_7d": counts["alarms_7d"],
        "canary_hits_7d": counts["canary_7d"], "refused_24h": counts["refused_24h"],
    }
    return out


# --- недельная сводка и уборка --------------------------------------------------------------------------

async def weekly(conn: asyncpg.Connection, config: Any, *, now: datetime | None = None) -> bool:
    """Раз в неделю (понедельник, 10:00 по часам владельца) — короткая сводка выдачи владельцу в бот
    согласований: числа без названий. Пустая неделя — без сообщения."""
    zone = ZoneInfo(config.timezone)
    local = (now or datetime.now(timezone.utc)).astimezone(zone)
    if (local.weekday(), local.hour) < WEEKLY_AT:
        return False
    week = local.strftime("%G-W%V")
    async with conn.transaction():
        row = await conn.fetchrow("SELECT weekly_sent FROM archive_guard WHERE id = 1 FOR UPDATE")
        if row is None or row["weekly_sent"] == week:
            return False
        await conn.execute("UPDATE archive_guard SET weekly_sent = $1 WHERE id = 1", week)
        per = await conn.fetch(
            """SELECT caller, count(*) FILTER (WHERE refused IS NULL) AS calls,
                      COALESCE(sum(messages) FILTER (WHERE refused IS NULL), 0) AS messages,
                      COALESCE(sum(chars) FILTER (WHERE refused IS NULL), 0) AS chars,
                      count(*) FILTER (WHERE refused LIKE 'limit%' OR refused = 'too_big') AS refused
               FROM archive_disclosures WHERE at > now() - interval '7 days' GROUP BY caller ORDER BY caller""")
        chats = await conn.fetchval(
            """SELECT count(DISTINCT c) FROM archive_disclosures, unnest(chats) AS c
               WHERE at > now() - interval '7 days' AND refused IS NULL""")
        alarms = await conn.fetch(
            "SELECT reason, count(*) AS n FROM archive_alarms WHERE at > now() - interval '7 days' GROUP BY reason")
        if not any(r["calls"] or r["refused"] for r in per) and not alarms:
            return False
        lines = ["Что ассистент получил из архива за неделю (только числа, без текста и названий):"]
        for r in per:
            lines.append(f"• {WHO.get(r['caller'], r['caller'])}: {r['calls']} обращений, {r['messages']} сообщений, "
                         f"{r['chars']} знаков" + (f", отказано по пределу: {r['refused']}" if r["refused"] else ""))
        lines.append(f"• разных чатов, из которых выданы сообщения: {chats}")
        names = {"volume": "необычный объём", "chats": "много чатов подряд", "walk": "перебор",
                 "limit": "упёрся в предел", "canary": "ловушка"}
        lines.append("• тревог: " + (", ".join(f"{names.get(a['reason'], a['reason'])} — {a['n']}" for a in alarms)
                                     if alarms else "не было"))
        lines.append("Подробности — ./ops/archive-guard.sh status на сервере.")
        await bridge.notify_owner(conn, "\n".join(lines), silent=True)
    return True


async def cleanup(conn: asyncpg.Connection) -> None:
    await conn.execute("DELETE FROM archive_disclosures WHERE at < now() - make_interval(days => $1)", KEEP_DAYS)
    await conn.execute("DELETE FROM archive_alarms WHERE at < now() - interval '180 days'")


@contextlib.asynccontextmanager
async def lifespan(state: Any) -> AsyncIterator[None]:
    if state.config.canary:
        try:
            async with state.pool.acquire() as conn:
                if not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM archive_canaries)"):
                    await add_canary(conn)
        except Exception as exc:  # noqa: BLE001
            logger.warning("граница архива: ловушка не заведена (%s)", type(exc).__name__)

    async def loop() -> None:
        while True:
            await asyncio.sleep(LOOP_EVERY)
            try:
                async with state.pool.acquire() as conn:
                    await cleanup(conn)
                    await weekly(conn, state.config)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("граница архива: уборка или сводка не выполнены (%s)", type(exc).__name__)

    state.spawn(loop(), name="archive-guard")
    yield


# --- команды на сервере: ./ops/archive-guard.sh, ./ops/canary.sh ----------------------------------------

def add_parser(sub: Any) -> None:
    p = sub.add_parser("archive-guard", help="граница выдачи архива ассистенту (Р-104): состояние, открыть, закрыть")
    p.add_argument("action", choices=["status", "open", "close"])
    c = sub.add_parser("canary", help="ловушки в выдаче архива (Р-104): add, list, remove")
    c.add_argument("action", choices=["add", "list", "remove"])
    c.add_argument("--id", type=int, help="remove: номер ловушки из list")


def cli(args: Any) -> None:
    import sys

    from . import db
    from .config import Config, ConfigError

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)

    async def go() -> int:
        conn = await db.connect(config.dsn)
        try:
            if args.cmd == "canary":
                return await _canary_cli(conn, args)
            if args.action == "status":
                data = await overview(conn, config)
                print(json.dumps(data, ensure_ascii=False, indent=1))
                return 0
            async with conn.transaction():
                if args.action == "close":
                    await set_state(conn, "closed", reason="owner", decision="close")
                    await bridge.notify_owner(conn, "Доступ ассистента к архиву закрыт командой на сервере "
                                                    "(./ops/archive-guard.sh close)."
                                                    + (" " + HERMES_PROCESSING if await plugin_takes_model_jobs(conn)
                                                       else ""))
                else:
                    await set_state(conn, "open", reason="reopen", quiet=REOPEN_QUIET, decision="open")
                    await bridge.notify_owner(conn, "Выдача архива ассистенту снова обычная — открыта командой на "
                                                    "сервере (./ops/archive-guard.sh open). Если вы об этом не "
                                                    "просили — закройте доступ в боте или командой close.")
            print("Готово:", "выдача закрыта" if args.action == "close" else "выдача открыта; владельцу сообщено")
            return 0
        finally:
            await conn.close()

    sys.exit(asyncio.run(go()))


async def _canary_cli(conn: asyncpg.Connection, args: Any) -> int:
    if args.action == "add":
        new = await add_canary(conn)
        print(f"Ловушка № {new} заведена. Владельцу её не видно; ассистенту — только в широком списке чатов и "
              "при листании всей истории.")
        return 0
    if args.action == "list":
        rows = await conn.fetch("SELECT id, title, created_at, hits, last_hit_at FROM archive_canaries ORDER BY id")
        if not rows:
            print("Ловушек нет. Завести — ./ops/canary.sh add.")
        for r in rows:
            print(f"№ {r['id']}: «{r['title']}», заведена {r['created_at']:%Y-%m-%d}, выдана {r['hits']} раз"
                  + (f", последний — {r['last_hit_at']:%Y-%m-%d %H:%M} UTC" if r["last_hit_at"] else ""))
        return 0
    if not args.id:
        print("remove: нужен --id (номер из list)")
        return 2
    gone = await conn.execute("DELETE FROM archive_canaries WHERE id = $1", args.id)
    print("Удалена." if gone.endswith("1") else "Такой ловушки нет.")
    return 0
