"""Уведомления владельцу о скрытых сообщениях и его решения.

Правила, которые здесь зашиты (и проверены тестами):
  * одно уведомление на текст: тот же текст в другом чате или пришедший повторно новой карточки
    не создаёт, решение действует на все такие сообщения сразу и запоминается;
  * не больше `per_hour` карточек в час. Остальное ждёт: карточки уходят по мере того, как
    освобождается место, а владелец раз в час получает сводное «ещё K скрыто» с кнопкой
    «Показать следующие»;
  * в карточке — кто, какой чат, когда, почему скрыто и короткая цитата. Цитата обезврежена:
    одна строка, без невидимых символов, обрезана, ссылки, адреса и команды не нажимаются;
  * цитата есть, только когда карточку отправляет свой бот сервиса. Если уведомления идут через
    плагин в Hermes, цитаты нет: задание с её текстом ассистент мог бы забрать из очереди сам,
    а смысл проверки — чтобы скрытый текст до него не дошёл;
  * текст сообщения здесь не хранится: в таблице уведомлений только его md5 и шапка карточки.

Решение принимает только нажатие владельца. Со своим ботом сервиса нажатие приходит от Telegram
напрямую и подделать его из Hermes нельзя; без своего бота оно идёт через плагин — с тем же
ограничением, что и остальные подтверждения (см. confirm.py).
"""

from __future__ import annotations

import hmac
import re
import secrets
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg

from .. import bridge, sanitize
from . import rules

CALLBACK_MODULE = "gd"
CARD_HANDLER = "guard.card"
QUOTE_LIMIT = 280
MORE = 5                 # сколько карточек приходит по кнопке «Показать следующие»
DEFAULT_PER_HOUR = 5

_SCHEME = re.compile(r"://")
_DOMAIN_DOT = re.compile(r"(?<=[\w-])\.(?=[^\W\d_]{2,})")
_COMMAND = re.compile(r"(?<![\w/])/(?=[A-Za-z])")


def one_line(text: str | None, limit: int) -> str:
    """Чужая строка для показа владельцу: одна строка, без невидимых и управляющих символов."""
    value = sanitize.clean_line(text, 20_000)
    return value if len(value) <= limit else value[: max(1, limit - 1)].rstrip() + "…"


def defang(text: str) -> str:
    """Делает ссылки, адреса, упоминания и команды ненажимаемыми в сообщении Telegram."""
    value = _SCHEME.sub("[://]", text)
    value = _DOMAIN_DOT.sub("[.]", value)
    value = _COMMAND.sub("/ ", value)
    return value.replace("@", "(@)")


def quote(text: str | None, limit: int = QUOTE_LIMIT) -> str:
    """Короткая обезвреженная цитата чужого текста для показа владельцу."""
    return defang(one_line(text, limit))


def _name(value: str | None, fallback: str) -> str:
    return defang(one_line(value, 60)) or fallback


def _score(value: float | None) -> str:
    return f"{value:.2f}".replace(".", ",") if value is not None else "—"


def why(row: Any) -> str:
    """Почему сообщение скрыто: оценка модели и сработавшие правила."""
    parts: list[str] = []
    model = row["guard_model"] or ""
    if model and model != rules.NAME:   # решила модель (одна или вместе с правилами)
        parts.append(f"оценка классификатора {_score(row['guard_score'])} из 1")
    fired = rules.explain(row["text"] or "")
    if fired and rules.score_one(row["text"] or "") >= rules.THRESHOLD:
        parts.append("признаки: " + "; ".join(rule.reason for rule in fired[:3]))
    return "; ".join(parts) or "сработала проверка"


def summary(row: Any, *, same: int, chats: int, tz: str) -> str:
    """Шапка карточки без цитаты: её же владелец видит после решения."""
    try:
        zone = ZoneInfo(tz)
    except Exception:
        zone = timezone.utc
    when = row["sent_at"].astimezone(zone).strftime("%d.%m.%Y %H:%M")
    kind = "личный чат" if row["chat_type"] in ("personal_chat", "bot_chat") else "группа или канал"
    lines = [
        f"От: {_name(row['sender'], 'неизвестный отправитель')}",
        f"Чат: {_name(row['chat'], 'без названия')} ({kind})",
        f"Когда: {when}",
        f"Почему: {why(row)}",
    ]
    if same > 1:
        lines.append(f"Таких сообщений: {same}, чатов: {chats}")
    return "\n".join(lines)


def card(head: str, text: str | None) -> str:
    """Текст карточки. text=None — без цитаты (уведомление идёт не через своего бота)."""
    shown = (["Начало текста (ссылки обезврежены):", f"«{quote(text)}»"] if text is not None else
             ["Текст сюда не включён: уведомление идёт через Hermes, где его мог бы прочитать "
              "ассистент. Посмотрите сообщение в Telegram — чат и время указаны выше."])
    return "\n".join([
        "Скрыл от ассистента входящее сообщение: оно похоже на попытку им управлять.",
        "",
        head,
        "",
        *shown,
        "",
        "Ассистент этого сообщения не видит: его нет в поиске, истории и сводках. В Telegram оно "
        "у вас на месте. Проверка ошибается в обе стороны — если это обычное сообщение, нажмите "
        "«Показать ассистенту».",
    ])


_WAITING = """
WITH hidden AS (
    SELECT md5(m.text) AS text_hash, min(m.id) AS first_id, count(*) AS same,
           count(DISTINCT m.chat_id) AS chats
    FROM messages m JOIN chats c ON c.id = m.chat_id
    WHERE NOT m.agent_visible AND m.guard_label = 'suspect' AND m.deleted_at IS NULL AND NOT c.excluded
    GROUP BY 1
)
SELECT h.* FROM hidden h
WHERE NOT EXISTS (SELECT 1 FROM guard_alerts a WHERE a.text_hash = h.text_hash)
ORDER BY h.first_id
"""

_FIRST = """
SELECT m.id, m.text, m.sent_at, m.guard_score, m.guard_model, c.type AS chat_type,
       COALESCE(NULLIF(m.sender_name, ''), sp.name) AS sender,
       COALESCE(NULLIF(c.title, ''), cp.name) AS chat
FROM messages m
JOIN chats c ON c.id = m.chat_id
JOIN peers cp ON cp.id = c.peer_id
LEFT JOIN peers sp ON sp.id = m.sender_peer_id
WHERE m.id = $1
"""


async def waiting(conn: asyncpg.Connection) -> int:
    """Сколько скрытых текстов ещё ждут своей карточки."""
    return len(await conn.fetch(_WAITING))


async def pump(
    conn: asyncpg.Connection, *, per_hour: int = DEFAULT_PER_HOUR, tz: str = "UTC", extra: int = 0,
) -> dict[str, int]:
    """Отправляет владельцу карточки о скрытых сообщениях, сколько позволяет предел в час.

    extra — сверх предела: владелец сам нажал «Показать следующие». Возвращает счётчики:
    sent — отправлено карточек, waiting — осталось ждать, digest — ушла ли сводка.
    """
    out = {"sent": 0, "waiting": 0, "digest": 0}
    async with conn.transaction():
        # Двум одновременным обходам нельзя отправить одну карточку дважды и превысить предел.
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('shturman.guard.alerts'))")
        if await bridge.get_owner(conn) is None:
            # Уведомлять некого: сообщения остаются скрытыми, карточки уйдут, когда владелец привяжется.
            out["waiting"] = await waiting(conn)
            return out
        recent = await conn.fetchval(
            "SELECT count(*) FROM guard_alerts WHERE notified_at > now() - interval '1 hour'")
        room = max(0, per_hour - recent) + max(0, extra)
        groups = await conn.fetch(_WAITING)
        for group in groups[:room]:
            row = await conn.fetchrow(_FIRST, group["first_id"])
            if row is None:
                continue
            head = summary(row, same=group["same"], chats=group["chats"], tz=tz)
            nonce = secrets.token_urlsafe(9)
            alert_id = await conn.fetchval(
                """INSERT INTO guard_alerts (text_hash, message_id, summary, nonce)
                   VALUES ($1, $2, $3, $4) ON CONFLICT (text_hash) DO NOTHING RETURNING id""",
                group["text_hash"], row["id"], head, nonce)
            if alert_id is None:
                continue
            await bridge.notify_owner(
                conn, card(head, row["text"] if bridge.owns_bot() else None),
                buttons=[[bridge.button("Показать ассистенту", CALLBACK_MODULE, f"r:{alert_id}:{nonce}"),
                          bridge.button("Оставить скрытым", CALLBACK_MODULE, f"c:{alert_id}:{nonce}")]],
                handler=CARD_HANDLER, context={"alert_id": alert_id})
            out["sent"] += 1
        out["waiting"] = max(0, len(groups) - out["sent"])
        if out["waiting"] and not extra:
            hour = datetime.now(timezone.utc).strftime("%Y%m%d%H")
            job = await bridge.notify_owner(
                conn,
                f"Скрыто от ассистента ещё сообщений с подозрительным текстом: {out['waiting']}.\n"
                f"Карточки о них придут позже: не больше {per_hour} в час, чтобы не засыпать вас "
                "уведомлениями. До вашего решения ассистент этих сообщений не видит.",
                buttons=[[bridge.button(f"Показать следующие {MORE}", CALLBACK_MODULE, "more")]],
                silent=True, dedup_key=f"guard-digest:{hour}")
            out["digest"] = int(job is not None)
    return out


@bridge.on_result(CARD_HANDLER)
async def _card_sent(conn: asyncpg.Connection, job: dict[str, Any], result: dict[str, Any]) -> None:
    message_id = result.get("message_id")
    if isinstance(message_id, int):
        await conn.execute("UPDATE guard_alerts SET card_message_id = $2 WHERE id = $1",
                           job["context"].get("alert_id"), message_id)


@bridge.on_failure(CARD_HANDLER)
async def _card_failed(conn: asyncpg.Connection, job: dict[str, Any], error: str) -> None:
    # Карточка не дошла — решить по ней нельзя. Запись снимается, и следующий обход отправит новую.
    await conn.execute("DELETE FROM guard_alerts WHERE id = $1 AND status = 'sent'",
                       job["context"].get("alert_id"))


def _settings() -> tuple[int, str]:
    from . import current

    guard = current()
    if guard is None:
        return DEFAULT_PER_HOUR, "UTC"
    return guard.settings.notify_per_hour, guard.tz


@bridge.on_callback(CALLBACK_MODULE)
async def _pressed(conn: asyncpg.Connection, rest: str, user_id: int) -> dict[str, Any]:
    gone = {"answer": "Кнопка уже недоступна.", "edit_text": None, "remove_buttons": True}
    if rest == "more":
        per_hour, tz = _settings()
        done = await pump(conn, per_hour=per_hour, tz=tz, extra=MORE)
        answer = f"Отправляю: {done['sent']}." if done["sent"] else "Скрытых сообщений без карточки нет."
        return {"answer": answer, "edit_text": None, "remove_buttons": True}
    choice, _, tail = rest.partition(":")
    raw_id, _, nonce = tail.partition(":")
    if choice not in ("r", "c") or not raw_id.isdigit():
        return gone
    row = await conn.fetchrow("SELECT * FROM guard_alerts WHERE id = $1 FOR UPDATE", int(raw_id))
    if row is None or row["status"] != "sent" or not hmac.compare_digest(row["nonce"], nonce):
        return gone
    if choice == "r":
        done = await conn.execute(
            """UPDATE messages SET agent_visible = true, guard_label = 'released', guard_checked_at = now()
               WHERE NOT agent_visible AND guard_label = 'suspect' AND md5(text) = $1""", row["text_hash"])
        status, answer, note = "released", "Показано ассистенту.", "Решение: показано ассистенту"
    else:
        done = await conn.execute(
            """UPDATE messages SET guard_label = 'confirmed', guard_checked_at = now()
               WHERE NOT agent_visible AND guard_label = 'suspect' AND md5(text) = $1""", row["text_hash"])
        status, answer, note = "confirmed", "Оставлено скрытым.", "Решение: оставлено скрытым от ассистента"
    await conn.execute("UPDATE guard_alerts SET status = $2, decided_at = now() WHERE id = $1", row["id"], status)
    count = int(done.split()[-1])
    # Цитата из карточки убирается: решение принято, чужой текст в управляющем чате больше не нужен.
    text = f"{row['summary']}\n\n{note}" + (f" (сообщений: {count})." if count != 1 else ".")
    return {"answer": answer, "edit_text": text, "remove_buttons": True}


async def remembered(conn: asyncpg.Connection, hashes: list[str]) -> dict[str, str]:
    """Решения владельца по уже встречавшимся текстам: {md5: released | confirmed}."""
    if not hashes:
        return {}
    rows = await conn.fetch(
        "SELECT text_hash, status FROM guard_alerts WHERE text_hash = ANY($1::text[]) AND status <> 'sent'",
        hashes)
    return {r["text_hash"]: r["status"] for r in rows}
