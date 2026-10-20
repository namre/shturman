"""Отбор, очередь и текст уведомлений владельцу (решение Р-84).

Отбор — только кодом, без модели и без поиска имени по тексту:

  личное сообщение — входящее в личном чате основного аккаунта владельца (не «Избранное», не чат
      с ботом, не служебные чаты Telegram и не управляющий бот, не от бота и не через бота, не правка);
      дальше — настройка «от всех / из выбранных сфер / от выбранных людей» (по числовому ID);
  прямое обращение в группе — то же определение, что у ответов в группах (docs/service.md, «Ответы в
      группах и темах»): разметка упоминания числового ID владельца, ответ на его настоящее сообщение
      в том же чате и теме, заданное владельцем обращение в начале реплики («Иван, …»). Добавлено
      одно: разметка упоминания @имени, если это @имя владельца, как его сообщил Telegram
      (`direct_address.mentions_username`). Пересылка, цитата, код, правка, бот — не обращение.

Очередь — таблица `notify_items`: строка на сообщение, со временем отправки (`due_at`):
  «сразу» — уведомление уходит, когда пачка сообщений закрылась (packs.py, Р-93: сообщения одного
      человека подряд с паузами до `packs.GAP`, альбом, «переслал и прокомментировал»), — одно на
      пачку: «3 сообщения: ↪ пересланное от …: «…» · 🖼 фото (2) · «…»». Следующие в течение
      `gap_minutes` после отправленного копятся и уходят одним уведомлением;
  «пачкой» — всё уходит одним сообщением в начале следующего отрезка в 15, 30 или 60 минут;
  тихие часы — всё, что пришло или созрело в них, уходит одной пачкой в их конце.
Перед отправкой каждое сообщение проверяется заново: не удалено ли, не исключён ли чат, не выключили
ли такие уведомления, не ответил ли владелец в этом чате после него (тогда — молча снимается).
Текст берётся в момент отправки: расшифровка голосового, успевшая к этому времени, попадает
в уведомление; скрытое защитой показывается без текста.

Разбор входящих (Р-92, triage/). Когда владелец его включил, сообщения, которые разбирает модель
(видимые ассистенту личные от людей и прямые обращения в группах), сюда не попадают: о них пишет
разбор — только о срочном, важном и о вопросе без ответа. Разбор не состоялся (сбой модели, «Остановить
всё», разбор выключили посреди) — `Notifier.fallback` ставит их обычным уведомлением. Скрытое защитой
разбор не берёт: о нём — уведомление без текста, как раньше.

Сообщение владельцу — обычное `notify.owner` (bridge.notify_owner): только когда у сервиса свой бот
согласований. Чужой текст — одной строкой, без невидимых и управляющих символов, до 300 знаков, без
разметки (сообщения бота уходят простым текстом).
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, time, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

import asyncpg

from .. import bridge, control_peers, packs, spheres, store
from ..outbox import direct_address
from ..outbox import text as textlib
from . import settings as ns

logger = logging.getLogger("shturman.notify")

MAX_AGE = timedelta(hours=3)        # живое сообщение старше — догонялка после перерыва связи: не уведомляем
STALE = timedelta(hours=36)         # дождалось отправки, но старше — снимается (сервис долго стоял)
KEEP = timedelta(days=7)            # сколько хранить отправленное и снятое (для «не чаще раза на чат» и счётчиков)
EXCERPT = 300
NAME = 60
PART = 120                          # знаков на одно сообщение в строке пачки
PACK_LINE = 600                     # знаков на всю строку пачки
DIGEST_CHATS = 15                   # чатов в одной пачке; остальные — строкой «и ещё N»
GROUP_TYPES = frozenset({"private_group", "private_supergroup", "public_supergroup"})

MEDIA_LABELS = {
    "voice_message": "🎤 голосовое", "video_message": "⏺ кружок", "photo": "🖼 фото", "file": "📎 файл",
    "sticker": "стикер", "animation": "GIF", "video_file": "🎬 видео", "audio_file": "🎵 аудио",
}
HIDDEN_TEXT = "🛡 Сообщение скрыто защитой — решите в карточке защиты."
_LABEL = re.compile(r"^\[[^\]\n]{1,200}\]\s*")

_ROWS = """
SELECT m.id, m.chat_id, m.tg_message_id, m.sent_at, m.kind, m.is_outgoing, m.deleted_at, m.text, m.edited_at,
       m.is_forwarded, m.telegram_entities, m.telegram_via_bot, m.telegram_sender_bot, m.reply_to_tg_id,
       m.topic_tg_id, m.agent_visible, m.sources, m.media_type, m.media_name, m.media_duration,
       m.transcript_state, m.media_state, m.sender_name, m.sender_peer_id, m.grouped_id, m.forwarded_from,
       m.guard_label, c.type AS chat_type, c.excluded, c.account_id, c.peer_id, COALESCE(c.title, p.name) AS chat_title,
       a.role AS account_role, p.class AS peer_class, p.tg_id AS peer_tg_id, p.is_bot AS peer_is_bot,
       p.username AS peer_username, sp.class AS sender_class, sp.tg_id AS sender_tg_id,
       sp.is_bot AS sender_is_bot
FROM messages m
JOIN chats c ON c.id = m.chat_id
JOIN accounts a ON a.id = c.account_id
JOIN peers p ON p.id = c.peer_id
LEFT JOIN peers sp ON sp.id = m.sender_peer_id
WHERE m.id = ANY($1::bigint[])
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


# --- кто владелец ---------------------------------------------------------------------------------

async def owner_ids(conn: asyncpg.Connection) -> set[int]:
    """Числовые ID владельца: основные аккаунты и привязанный к боту согласований."""
    ids = {int(r["tg_user_id"]) for r in await conn.fetch("SELECT tg_user_id FROM accounts WHERE role = 'owner'")}
    owner = await bridge.get_owner(conn)
    if owner is not None:
        ids.add(int(owner["user_id"]))
    return ids


async def owner_usernames(conn: asyncpg.Connection, ids: Iterable[int]) -> list[str]:
    """@имена владельца, как их сообщил Telegram (живой источник пишет их в `peers`)."""
    rows = await conn.fetch(
        "SELECT username FROM peers WHERE class = 'user' AND tg_id = ANY($1::bigint[]) AND username IS NOT NULL",
        list(ids))
    return [r["username"] for r in rows if r["username"]]


async def chat_spheres(conn: asyncpg.Connection, chat_id: int) -> set[int]:
    rows = await conn.fetch(
        f"""SELECT sc.sphere_id FROM chats c JOIN peers p ON p.id = c.peer_id {spheres.CHAT_KEY_JOIN}
            WHERE c.id = $1""", chat_id)
    return {int(r["sphere_id"]) for r in rows}


# --- отбор ------------------------------------------------------------------------------------------

async def personal_candidate(conn: asyncpg.Connection, row: Mapping[str, Any]) -> bool:
    """Входящее сообщение человека в личном чате основного аккаунта — независимо от настроек."""
    if row["chat_type"] != "personal_chat" or row["peer_class"] != "user" or row["account_role"] != "owner":
        return False
    if row["excluded"] or row["kind"] != "message" or row["deleted_at"] is not None:
        return False
    if row["is_outgoing"] is not False or row["edited_at"] is not None:
        return False
    if row["peer_is_bot"] or row["sender_is_bot"] or row["telegram_sender_bot"] or row["telegram_via_bot"]:
        return False
    if row["sender_peer_id"] is not None and row["sender_peer_id"] != row["peer_id"]:
        return False   # в личном чате входящее — только от самого собеседника
    if store.is_blocked_peer(row["peer_class"], row["peer_tg_id"], row["peer_username"]) \
            or await control_peers.is_blocked(conn, row["peer_class"], row["peer_tg_id"]):
        return False   # служебные чаты Telegram и управляющий бот
    return True


async def personal_allowed(conn: asyncpg.Connection, row: Mapping[str, Any], s: Mapping[str, Any]) -> bool:
    """Подходит ли личное сообщение под настройку «от кого»."""
    if row["chat_id"] in set(s["muted_chats"]):
        return False        # «Не сообщать об этом чате»
    if s["personal"] == "all":
        return True
    if s["personal"] == "people":
        return int(row["peer_tg_id"]) in set(s["people"])
    if s["personal"] == "spheres":
        return bool(await chat_spheres(conn, row["chat_id"]) & set(s["spheres"]))
    return False


async def _replied_to_owner(conn: asyncpg.Connection, row: Mapping[str, Any], owners: set[int]) -> bool:
    """Ответ на настоящее сообщение владельца в том же чате и той же теме."""
    if row["reply_to_tg_id"] is None:
        return False
    original = await conn.fetchrow(
        """SELECT m.is_outgoing, a.role, p.class AS sender_class, p.tg_id AS sender_tg_id
           FROM messages m JOIN chats c ON c.id = m.chat_id JOIN accounts a ON a.id = c.account_id
           LEFT JOIN peers p ON p.id = m.sender_peer_id
           WHERE m.chat_id = $1 AND m.tg_message_id = $2 AND m.kind = 'message' AND m.deleted_at IS NULL
             AND m.is_forwarded IS NOT TRUE AND m.topic_tg_id IS NOT DISTINCT FROM $3::bigint""",
        row["chat_id"], row["reply_to_tg_id"], row["topic_tg_id"])
    if original is None:
        return False
    if original["is_outgoing"] is True and original["role"] == "owner":
        return True    # своё сообщение в чате основного аккаунта
    return original["sender_class"] == "user" and original["sender_tg_id"] in owners


async def group_reason(conn: asyncpg.Connection, row: Mapping[str, Any], s: Mapping[str, Any],
                       owners: set[int]) -> str | None:
    """Прямое обращение к владельцу в группе: mention, reply или alias; иначе None."""
    if row["chat_type"] not in GROUP_TYPES or row["peer_class"] not in ("chat", "channel"):
        return None
    if row["excluded"] or row["kind"] != "message" or row["deleted_at"] is not None:
        return None
    if row["chat_id"] in set(s["muted_chats"]):
        return None         # «Не сообщать об этом чате»
    if row["is_outgoing"] is not False or row["edited_at"] is not None or row["is_forwarded"] is not False:
        return None
    if row["telegram_via_bot"] is not False or row["telegram_sender_bot"] is not False:
        return None
    if row["sender_class"] != "user" or row["sender_is_bot"] or row["sender_tg_id"] in owners:
        return None
    # Разметку Telegram сервис знает только у сообщений живой сессии: без неё не угадываем.
    if "session" not in (row["sources"] or ()) or row["telegram_entities"] is None:
        return None
    message = dict(row)
    if await _replied_to_owner(conn, row, owners):
        return "reply"
    if direct_address.is_direct_address(message, owners):
        return "mention"
    if direct_address.mentions_username(message, await owner_usernames(conn, owners)):
        return "mention"
    if s["aliases"] and direct_address.is_direct_address(message, (), s["aliases"]):
        return "alias"
    return None


# --- когда отправлять --------------------------------------------------------------------------------

def _hm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def in_quiet(s: Mapping[str, Any], local: datetime) -> bool:
    if not s["quiet"]:
        return False
    start, end, now = _hm(s["quiet_from"]), _hm(s["quiet_to"]), local.time().replace(second=0, microsecond=0)
    if start == end:
        return False
    return start <= now < end if start < end else (now >= start or now < end)


def quiet_end(s: Mapping[str, Any], local: datetime) -> datetime:
    """Ближайший конец тихих часов после этого момента (в часовом поясе владельца)."""
    end = _hm(s["quiet_to"])
    candidate = local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    return candidate if candidate > local else candidate + timedelta(days=1)


def next_slot(minutes: int, local: datetime) -> datetime:
    """Начало следующего отрезка в `minutes` минут от полуночи по часам владельца."""
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    passed = (local - midnight).total_seconds() // 60
    return midnight + timedelta(minutes=(int(passed) // minutes + 1) * minutes)


async def schedule(conn: asyncpg.Connection, s: Mapping[str, Any], chat_id: int, now: datetime,
                   tz: ZoneInfo) -> tuple[datetime, bool, bool]:
    """Когда отправить новое уведомление этого чата: (время, тихие часы, пачка)."""
    local = now.astimezone(tz)
    if in_quiet(s, local):
        return quiet_end(s, local), True, False
    if s["mode"] == "digest":
        return next_slot(int(s["digest_minutes"]), local), False, True
    waiting = await conn.fetchval(
        """SELECT min(due_at) FROM notify_items
           WHERE chat_id = $1 AND state = 'waiting' AND NOT quiet AND NOT digest AND NOT held_for_rehearsal
             AND NOT reminder""",
        chat_id)
    if waiting is not None:
        return waiting, False, False   # уже копится уведомление этого чата — в него же
    last = await conn.fetchval("SELECT max(sent_at) FROM notify_items WHERE chat_id = $1 AND state = 'sent'",
                               chat_id)
    gap = timedelta(minutes=int(s["gap_minutes"]))
    if last is not None and now - last < gap:
        return last + gap, False, False
    return now, False, False


# --- текст -----------------------------------------------------------------------------------------

def _clock(moment: datetime, tz: ZoneInfo, now: datetime) -> str:
    local, today = moment.astimezone(tz), now.astimezone(tz).date()
    return local.strftime("%H:%M") if local.date() == today else local.strftime("%d.%m %H:%M")


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _duration(seconds: Any) -> str:
    if isinstance(seconds, int) and seconds >= 0:
        return f"{seconds // 60}:{seconds % 60:02d}"
    return ""


def media_label(row: Mapping[str, Any]) -> str:
    kind = row["media_type"]
    label = MEDIA_LABELS.get(kind or "", "📎 вложение" if kind else "")
    if not label:
        return ""
    if kind == "file" and row["media_name"]:
        label += f" «{textlib.one_line(row['media_name'], 60)}»"
    if kind in ("voice_message", "video_message") and _duration(row["media_duration"]):
        label += ", " + _duration(row["media_duration"])
    if kind in ("voice_message", "video_message") and row["transcript_state"] == "pending":
        label += " (расшифровка ещё не готова)"
    return label


def body(row: Mapping[str, Any], s: Mapping[str, Any]) -> str:
    """Что показать о сообщении: метка вложения и начало текста. Скрытое защитой — без текста."""
    if not row["agent_visible"]:
        return HIDDEN_TEXT
    label = media_label(row)
    raw = row["text"] or ""
    if row["media_type"] and (row["transcript_state"] == "done" or row["media_state"] == "done"):
        raw = _LABEL.sub("", raw, count=1)     # метку «[голосовое, 0:42]» заменяет своя
    if row["is_forwarded"]:
        label = (_forwarded_word(row) + (", " + label if label else ""))
    excerpt = textlib.one_line(raw, EXCERPT) if s["preview"] else ""
    if excerpt and label:
        return f"{label}: «{excerpt}»"
    if excerpt:
        return f"«{excerpt}»"
    return label or ("(без текста)" if s["preview"] else "")


def _forwarded_word(row: Mapping[str, Any]) -> str:
    source = textlib.one_line(row.get("forwarded_from"), 40)
    return "↪ пересланное" + (f" от {source}" if source else "")


def _part(row: Mapping[str, Any], s: Mapping[str, Any]) -> tuple[str | None, str]:
    """Одно сообщение в строке пачки: (метка для склейки одинаковых подряд или None, текст)."""
    if not row["agent_visible"]:
        return "🛡 скрыто защитой", "🛡 скрыто защитой"
    label = media_label(row)
    raw = row["text"] or ""
    if row["media_type"] and (row["transcript_state"] == "done" or row["media_state"] == "done"):
        raw = _LABEL.sub("", raw, count=1)
    excerpt = textlib.one_line(raw, PART) if s["preview"] else ""
    if packs.forwarded(row):
        label = _forwarded_word(row) + (", " + label if label else "")
    if excerpt:
        return None, (f"{label}: «{excerpt}»" if label else f"«{excerpt}»")
    label = label or "сообщение"
    return label, label


def pack_parts(rows: Sequence[Mapping[str, Any]], s: Mapping[str, Any]) -> list[str]:
    """Части строки пачки: альбом — одной частью («🖼 фото (3): «подпись»»), одинаковые вложения без
    текста подряд — одной частью со счётом."""
    groups: list[list[Mapping[str, Any]]] = []
    for row in rows:
        if groups and row.get("grouped_id") is not None and groups[-1][-1].get("grouped_id") == row["grouped_id"]:
            groups[-1].append(row)
        else:
            groups.append([row])
    parts: list[tuple[str | None, str, int]] = []
    for group in groups:
        if len(group) == 1:
            key, text = _part(group[0], s)
            if key is not None and parts and parts[-1][0] == key:
                parts[-1] = (key, key, parts[-1][2] + 1)
            else:
                parts.append((key, text, 1))
            continue
        shown = [_part(r, s) for r in group]
        captioned = next((text for key, text in shown if key is None), None)
        key = next((k for k, _ in shown if k is not None), None) or "🖼 фото"
        if captioned is not None and ": «" in captioned:
            head, _, tail = captioned.partition(": «")
            parts.append((None, f"{head} ({len(group)}): «{tail}", 1))
        elif captioned is not None:
            parts.append((None, f"{key} ({len(group)}): {captioned}", 1))
        else:
            parts.append((None, f"{key} ({len(group)})", 1))
    return [text if count == 1 else f"{text} ({count})" for _, text, count in parts]


def pack_line(rows: Sequence[Mapping[str, Any]], s: Mapping[str, Any]) -> str:
    """Несколько сообщений одной строкой: «3 сообщения: ↪ пересланное от Ивана: «…» · 🖼 фото (2) · «…»»."""
    n = len(rows)
    out = f"{n} {_plural(n, 'сообщение', 'сообщения', 'сообщений')}: "
    parts = pack_parts(rows, s)
    for i, part in enumerate(parts):
        piece = (" · " if i else "") + part
        if i and len(out) + len(piece) > PACK_LINE:
            out += f" · …ещё {len(parts) - i}"
            break
        out += piece
    return out


def _link(row: Mapping[str, Any]) -> str | None:
    from ..outbox import watcher
    if row["peer_class"] == "user":
        username = row["peer_username"] or ""
        return f"https://t.me/{username}" if re.match(r"^[A-Za-z][A-Za-z0-9_]{3,31}$", username) else None
    return watcher._link({"username": row["peer_username"], "tg_message_id": row["tg_message_id"],
                          "peer_class": row["peer_class"], "tg_id": row["peer_tg_id"]})


REASON_WORDS = {"mention": "упоминание", "reply": "ответ на ваше сообщение", "alias": "обращение по имени"}


def _who(row: Mapping[str, Any]) -> str:
    return textlib.one_line(row["sender_name"], NAME) or "кто-то"


def _place(rows: Sequence[Mapping[str, Any]], kind: str) -> str:
    first = rows[0]
    if kind == "personal":
        return textlib.one_line(first["chat_title"], NAME) or textlib.one_line(first["sender_name"], NAME) or "собеседник"
    return "«" + (textlib.one_line(first["chat_title"], NAME) or "группа без названия") + "»"


def compose_chat(rows: Sequence[Mapping[str, Any]], kind: str, s: Mapping[str, Any], tz: ZoneInfo,
                 now: datetime, sphere_titles: Sequence[str]) -> str:
    """Уведомление об одном чате: одно сообщение — его начало; пачка — одной строкой (Р-93)."""
    first, last = rows[0], rows[-1]
    sphere = (" · " + ", ".join(textlib.one_line(t, 40) for t in sphere_titles)) if sphere_titles else ""
    remind = ["⏰ Напоминаю: вы ещё не ответили"] if any(r.get("reminder") for r in rows) else []
    when = _clock(first["sent_at"], tz, now)
    if len(rows) > 1 and _clock(last["sent_at"], tz, now) != when:
        when += "–" + _clock(last["sent_at"], tz, now)
    if kind == "personal":
        lines = remind + [f"💬 Вам написали: {_place(rows, kind)}", f"Личный чат{sphere} · {when}"]
    else:
        reason = REASON_WORDS.get(first.get("reason") or "", "обращение")
        lines = remind + [f"📣 Вас упомянули в {_place(rows, kind)}", f"{_who(first)}{sphere} · {when} · {reason}"]
    text = body(first, s) if len(rows) == 1 else pack_line(rows, s)
    if text:
        lines.append(text)
    link = _link(first)
    if link:
        lines.append(f"Открыть: {link}")
    return "\n".join(lines)


def compose_digest(groups: Sequence[tuple[str, list[Mapping[str, Any]], list[str]]], s: Mapping[str, Any],
                   tz: ZoneInfo, now: datetime, *, quiet: bool) -> str:
    """Одно сообщение о нескольких чатах: после тихих часов или раз в N минут."""
    total = sum(len(rows) for _, rows, _ in groups)
    head = ("🔔 Пока были тихие часы" if quiet else f"🔔 За последние {s['digest_minutes']} мин") + \
        f": {total} {_plural(total, 'сообщение', 'сообщения', 'сообщений')}"
    lines = [head]
    for kind, rows, titles in groups[:DIGEST_CHATS]:
        first, last = rows[0], rows[-1]
        sphere = (" · " + ", ".join(textlib.one_line(t, 40) for t in titles)) if titles else ""
        if kind == "personal":
            what = f"💬 {_place(rows, kind)}{sphere}"
        else:
            reason = REASON_WORDS.get(first.get("reason") or "", "обращение")
            what = f"📣 {_place(rows, kind)}{sphere}: {_who(first)} ({reason})"
        count = (f" — {len(rows)} {_plural(len(rows), 'сообщение', 'сообщения', 'сообщений')}, "
                 f"последнее в {_clock(last['sent_at'], tz, now)}") if len(rows) > 1 \
            else f" — в {_clock(first['sent_at'], tz, now)}"
        lines += ["", what + count]
        text = body(first, s)
        if text:
            lines.append(text)
    if len(groups) > DIGEST_CHATS:
        rest = len(groups) - DIGEST_CHATS
        lines += ["", f"…и ещё {rest} {_plural(rest, 'чат', 'чата', 'чатов')}."]
    return bridge.fit_message("\n".join(lines))


# --- работающие уведомления ---------------------------------------------------------------------------

async def queue(conn: asyncpg.Connection, row: Mapping[str, Any], now: datetime,
                tz: ZoneInfo) -> tuple[str | None, datetime | None]:
    """Ставит уведомление о сообщении в очередь по настройкам: (вид, время отправки) или (None, None)."""
    s = await ns.load(conn)
    if s["personal"] == "off" and not s["groups"]:
        return None, None
    message_id, chat_id = row["id"], row["chat_id"]
    kind, reason = None, None
    if s["personal"] != "off" and await personal_candidate(conn, row) \
            and await personal_allowed(conn, row, s):
        kind = "personal"
    elif s["groups"]:
        reason = await group_reason(conn, row, s, await owner_ids(conn))
        kind = "mention" if reason else None
    if kind is None:
        return None, None
    # Репетиция ответов (Р-85): о сообщении, на которое она готовит ответ, владелец узнает из её
    # карточки — уведомление придерживается и уходит, только если репетиция не состоялась.
    from ..rehearsal import core as rehearsal
    hold = await rehearsal.notify_hold(conn, row) if kind == "personal" else None
    # Пачка (Р-93): уведомление — одно на пачку, когда она закрылась (не дольше `packs.GAP` после
    # последнего сообщения). Придержка ради репетиции считается от закрытия пачки и разбора её вложений.
    close = now + timedelta(seconds=await packs.closes_in(conn, message_id, now=now))
    ready = close + timedelta(seconds=packs.MEDIA_WAIT)
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('shturman.notify'), $1::int)",
                           chat_id % 2_000_000_000)
        due, quiet, digest = await schedule(conn, s, chat_id, now, tz)
        if not quiet and not digest:
            due = max(due, close)
        if hold == "hold":
            due = max(due, ready + rehearsal.NOTIFY_HOLD)
        added = await conn.fetchval(
            """INSERT INTO notify_items (message_id, chat_id, kind, reason, quiet, digest, due_at,
                                         held_for_rehearsal, state, dropped_reason)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
               ON CONFLICT (message_id) DO NOTHING RETURNING id""",
            message_id, chat_id, kind, reason, quiet, digest, due, hold == "hold",
            "dropped" if hold == "covered" else "waiting", "rehearsal" if hold == "covered" else None)
        if added is not None and hold != "covered" and not quiet and not digest:
            # Уже ждущие уведомления этого чата уходят вместе с новым — одним сообщением о пачке; если новое
            # ждёт карточку репетиции, то и они: карточка покроет всю пачку (или отпустит её целиком).
            await conn.execute(
                """UPDATE notify_items SET due_at = GREATEST(due_at, $2),
                          held_for_rehearsal = held_for_rehearsal OR $3
                   WHERE chat_id = $1 AND state = 'waiting' AND NOT quiet AND NOT digest AND NOT reminder""",
                chat_id, due, hold == "hold")
    if added is None or hold == "covered":
        return None, None
    return kind, due


class Notifier:
    """Одна на процесс сервиса (`state.extras["notify"]`)."""

    def __init__(self, pool: asyncpg.Pool, *, tz: Callable[[], str]) -> None:
        self.pool, self._tz = pool, tz
        self.clock: Callable[[], datetime] = _now
        self.wake = asyncio.Event()

    @property
    def tz(self) -> ZoneInfo:
        return zone(self._tz())

    async def on_message(self, payload: Mapping[str, Any]) -> str | None:
        """Новое живое сообщение (или скрытое защитой). Возвращает вид поставленного уведомления."""
        if payload.get("outgoing") is not False or payload.get("edited") is not False \
                or payload.get("via_bot") is not False:
            return None
        message_id, chat_id = payload.get("message_id"), payload.get("chat_id")
        if not isinstance(message_id, int) or not isinstance(chat_id, int):
            return None
        if not bridge.owns_bot():
            return None   # писать некуда: без своего бота согласований уведомлений нет
        now = self.clock()
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(_ROWS, [message_id])
            if row is None or row["chat_id"] != chat_id or now - row["sent_at"] > MAX_AGE:
                return None
            # Разбор входящих (Р-92): сообщение, которое разбирает модель, пересылкой «Вам написали» не
            # приходит — о нём владелец узнает карточкой разбора (или не узнает: «остальное — тишина»).
            # Разбор выключен, ассистент остановлен, сообщение скрыто защитой — пересылка как раньше.
            from ..triage import core as triage
            if await triage.takes(conn, row):
                return None
            kind, due = await queue(conn, row, now, self.tz)
        if kind is None or due is None:
            return None
        if due <= now:
            self.wake.set()
        return kind

    async def fallback(self, conn: asyncpg.Connection, message_ids: Sequence[int]) -> int:
        """Разбор входящих этих сообщений не состоялся (сбой модели, остановка, разбор выключили): они
        уходят обычным уведомлением, как без разбора, — владелец не теряет сигнал. Возвращает, сколько
        поставлено. Вызывать можно внутри чужой транзакции: здесь — только вставки в очередь."""
        if not message_ids or not bridge.owns_bot():
            return 0
        now = self.clock()
        rows = sorted(await conn.fetch(_ROWS, list(message_ids)), key=lambda r: (r["sent_at"], r["id"]))
        count, soon = 0, False
        for row in rows:
            if now - row["sent_at"] > STALE:
                continue
            kind, due = await queue(conn, row, now, self.tz)
            if kind is not None and due is not None:
                count += 1
                soon = soon or due <= now
        if soon:
            try:
                # С небольшой задержкой: вызывающая транзакция должна успеть закрыться.
                asyncio.get_running_loop().call_later(0.1, self.wake.set)
            except RuntimeError:
                pass
        return count

    async def _drop(self, conn: asyncpg.Connection, ids: Sequence[int], reason: str) -> None:
        if ids:
            await conn.execute(
                """UPDATE notify_items SET state = 'dropped', dropped_reason = $2, sent_at = NULL
                   WHERE id = ANY($1::bigint[]) AND state = 'waiting'""", list(ids), reason)

    async def flush(self, now: datetime | None = None) -> int:
        """Отправляет созревшее. Возвращает число отправленных сообщений владельцу."""
        now = now or self.clock()
        tz = self.tz
        sent = 0
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('shturman.notify.flush'))")
            s = await ns.load(conn)
            local = now.astimezone(tz)
            if in_quiet(s, local):
                # Тихие часы: созревшее и созревающее в них уходит в их конце одной пачкой.
                end = quiet_end(s, local)
                await conn.execute(
                    """UPDATE notify_items SET due_at = $1, quiet = true
                       WHERE state = 'waiting' AND due_at < $1""", end)
                return 0
            items = await conn.fetch(
                """SELECT id, message_id, chat_id, kind, reason, quiet, digest, reminder FROM notify_items
                   WHERE state = 'waiting' AND due_at <= $1 ORDER BY id FOR UPDATE""", now)
            if not items:
                return 0
            if not bridge.owns_bot():
                await self._drop(conn, [i["id"] for i in items], "no_bot")
                return 0
            rows = {r["id"]: r for r in await conn.fetch(_ROWS, [i["message_id"] for i in items])}
            owners = await owner_ids(conn)
            keep: list[dict[str, Any]] = []
            gone, disabled, stale = [], [], []
            for item in items:
                row = rows.get(item["message_id"])
                if row is None or row["deleted_at"] is not None or row["excluded"]:
                    gone.append(item["id"])
                elif now - row["sent_at"] > STALE:
                    stale.append(item["id"])
                elif (item["kind"] == "personal" and not (s["personal"] != "off"
                                                          and await personal_allowed(conn, row, s))) \
                        or (item["kind"] == "mention" and (not s["groups"] or row["chat_id"] in set(s["muted_chats"]))):
                    disabled.append(item["id"])
                else:
                    keep.append({**dict(row), "item_id": item["id"], "reason": item["reason"],
                                 "item_kind": item["kind"], "batch": item["quiet"] or item["digest"],
                                 "quiet": item["quiet"], "reminder": item["reminder"]})
            await self._drop(conn, gone, "gone")
            await self._drop(conn, stale, "stale")
            await self._drop(conn, disabled, "disabled")
            # Владелец уже ответил в этом чате после сообщения — уведомлять поздно и незачем.
            by_chat: dict[int, list[dict[str, Any]]] = {}
            for row in sorted(keep, key=lambda r: (r["sent_at"], r["id"])):
                by_chat.setdefault(row["chat_id"], []).append(row)
            answered: list[int] = []
            for chat_id, chat_rows in list(by_chat.items()):
                last_own = await conn.fetchval(
                    """SELECT max(m.sent_at) FROM messages m LEFT JOIN peers p ON p.id = m.sender_peer_id
                       WHERE m.chat_id = $1 AND m.kind = 'message' AND m.deleted_at IS NULL
                         AND (m.is_outgoing IS TRUE OR (p.class = 'user' AND p.tg_id = ANY($2::bigint[])))""",
                    chat_id, list(owners))
                if last_own is not None:
                    answered += [r["item_id"] for r in chat_rows if r["sent_at"] <= last_own]
                    chat_rows = [r for r in chat_rows if r["sent_at"] > last_own]
                if chat_rows:
                    by_chat[chat_id] = chat_rows
                else:
                    del by_chat[chat_id]
            await self._drop(conn, answered, "answered")
            if not by_chat:
                return 0
            titles = await spheres.titles_by_chat(conn, list(by_chat))
            single = {c: r for c, r in by_chat.items() if not any(x["batch"] for x in r)}
            batched = {c: r for c, r in by_chat.items() if c not in single}
            for chat_id, chat_rows in single.items():
                if not any(r["reminder"] for r in chat_rows):
                    # Пачка ещё не закрылась (человек дописывает) — подождать её конца (Р-93).
                    pack = await packs.around(conn, chat_rows[-1]["id"], now=now)
                    if not packs.ready(pack, now):
                        await conn.execute(
                            "UPDATE notify_items SET due_at = $2 WHERE id = ANY($1::bigint[]) AND state = 'waiting'",
                            [r["item_id"] for r in chat_rows],
                            min(pack.closes_at, pack.first["sent_at"] + timedelta(seconds=packs.HOLD_MAX)))
                        continue
                text = compose_chat(chat_rows, chat_rows[0]["item_kind"], s, tz, now, titles.get(chat_id, []))
                # Кнопки (Б14): «Подготовить ответ» — к последнему сообщению чата (ответ готовится на всю
                # пачку), «Напомнить» и «Не сообщать» — к нему же (notify/buttons.py). Если ответ на него
                # подготовить нельзя (скрыто защитой, не личный чат, нечем готовить), кнопки нет.
                from . import buttons as notify_buttons
                personal = chat_rows[0]["item_kind"] == "personal"
                prepare = personal and await notify_buttons.can_prepare(conn, chat_rows[-1]["id"]) is None
                keys = await notify_buttons.attach(conn, chat_rows[-1]["item_id"], personal=personal,
                                                   prepare=prepare)
                sent += await self._send(conn, text, [r["item_id"] for r in chat_rows], now, buttons=keys)
            if batched:
                order = sorted(batched.items(), key=lambda kv: kv[1][-1]["sent_at"], reverse=True)
                groups = [(rows_[0]["item_kind"], rows_, titles.get(chat_id, [])) for chat_id, rows_ in order]
                quiet = any(r["quiet"] for _, rows_ in order for r in rows_)
                text = compose_digest(groups, s, tz, now, quiet=quiet)
                sent += await self._send(conn, text, [r["item_id"] for _, rows_ in order for r in rows_], now)
        return sent

    async def _send(self, conn: asyncpg.Connection, text: str, ids: list[int], now: datetime, *,
                    buttons: list[list[dict[str, str]]] | None = None) -> int:
        # Повтор («Напомнить через час») — новое задание: прежний ключ уже использован.
        reminder = await conn.fetchval("SELECT bool_or(reminder) FROM notify_items WHERE id = ANY($1::bigint[])", ids)
        key = f"notify:{min(ids)}" + (f":remind:{int(now.timestamp())}" if reminder else "")
        job_id = await bridge.notify_owner(conn, text, buttons=buttons, dedup_key=key)
        await conn.execute(
            """UPDATE notify_items SET state = 'sent', sent_at = $2, job_id = $3, reminder = false
               WHERE id = ANY($1::bigint[])""", ids, now, job_id)
        return 1

    async def cleanup(self) -> int:
        async with self.pool.acquire() as conn:
            done = await conn.execute(
                "DELETE FROM notify_items WHERE state <> 'waiting' AND created_at < now() - make_interval(secs => $1)",
                KEEP.total_seconds())
        return int(done.split()[-1])

    async def run(self, every: float = 15.0) -> None:
        cleaned = 0.0
        while True:
            try:
                await self.flush()
                if asyncio.get_running_loop().time() - cleaned > 3600:
                    cleaned = asyncio.get_running_loop().time()
                    await self.cleanup()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — сбой одного обхода не останавливает уведомления
                logger.error("уведомления: обход не выполнен (%s)", type(exc).__name__)
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=every)
            except asyncio.TimeoutError:
                pass


async def status(conn: asyncpg.Connection) -> dict[str, Any]:
    """Счётчики для владельца: только числа."""
    row = await conn.fetchrow(
        """SELECT count(*) FILTER (WHERE state = 'waiting') AS waiting,
                  count(*) FILTER (WHERE state = 'sent' AND sent_at > now() - interval '24 hours') AS sent_24h,
                  count(*) FILTER (WHERE state = 'dropped' AND dropped_reason = 'answered'
                                   AND created_at > now() - interval '24 hours') AS answered_24h,
                  max(sent_at) AS last_sent_at
           FROM notify_items""")
    out = dict(row)
    out["last_sent_at"] = out["last_sent_at"].isoformat() if out["last_sent_at"] else None
    return out
