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

История без поштучных карточек (Р-99). Отдельная карточка — только живому потоку и свежему
(`messages.guard_card`, правило — в core._WRITE). Скрытое из импорта и догрузки старых сообщений
владелец видит одной сводкой: сколько, почему (модель, правила, сильные правила), откуда (каналы,
группы, личные) — и решает разом: «Показать ассистенту все скрытые», «Все, кроме сильных правил»,
«Оставить все скрытыми» — через подтверждение вторым нажатием (`confirm`, виды `guard.release_all`
и `guard.keep_all`), — или «Разбирать по одной» (следующие карточки, в том числе из истории).
Решение разом пишется так же, как поштучное: на каждый текст, с памятью «по тексту», с отметкой
`guard_alerts.via = 'bulk'` — чтобы статистика (`stats.py`) не смешивала его с поштучным.
Сводка приходит в бот не чаще раза в сутки и по команде /guard; в приложении — всегда сверху
раздела «Защита».

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

from .. import bridge, confirm, sanitize
from . import rules

CALLBACK_MODULE = "gd"
CARD_HANDLER = "guard.card"
QUOTE_LIMIT = 280
MORE = 5                 # сколько карточек приходит по кнопке «Показать следующие»
DEFAULT_PER_HOUR = 5
# Сколько часов от отправки (правки) до проверки сообщение считается свежим и получает свою карточку.
# Сутки: карточка нужна, пока сообщение ещё «в разговоре» (автоответ, разбор входящих, сводка дня);
# сообщение из импорта или догрузки старше суток — память, а не срочность: ему хватает сводки.
DEFAULT_CARD_HOURS = 24
# Сколько часов сводка ждёт конца фоновой проверки истории, прежде чем прийти с промежуточными числами.
SUMMARY_WAIT = 6
RELEASE_ALL = "guard.release_all"
KEEP_ALL = "guard.keep_all"
SUMMARY_HANDLER = "guard.summary"

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


def certainty(value: float | None) -> str:
    """Оценка модели словами (Б12): владельцу не нужно знать, что такое «0,97 из 1»."""
    if value is None:
        return "проверка сочла это попыткой управлять ассистентом"
    if value >= 0.95:
        return "почти наверняка попытка управлять ассистентом"
    if value >= 0.8:
        return "очень похоже на попытку управлять ассистентом"
    return "похоже на попытку управлять ассистентом, но проверка не уверена"


def why(row: Any) -> str:
    """Почему сообщение скрыто: насколько модель уверена (словами) и сработавшие правила."""
    parts: list[str] = []
    model = row["guard_model"] or ""
    if model and model != rules.NAME:   # решила модель (одна или вместе с правилами)
        parts.append(certainty(row["guard_score"]))
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
             ["Текст сюда не включён: уведомление идёт через бота ассистента, где его мог бы прочитать "
              "сам ассистент. Посмотрите сообщение в Telegram — чат и время указаны выше."])
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


# Нужна ли сообщению своя карточка: отметка проверки (с 0040) либо, у проверенного раньше, то же
# правило по времени отправки и проверки. $1 — часы свежести.
CARD_WORTHY = """COALESCE(m.guard_card,
    greatest(m.sent_at, COALESCE(m.edited_at, m.sent_at)) >= m.guard_checked_at - make_interval(hours => $1), true)"""

# Скрытые сообщения, которые ждут решения владельца (общая часть всех выборок ниже).
HIDDEN = """NOT m.agent_visible AND m.guard_label = 'suspect' AND m.deleted_at IS NULL AND NOT c.excluded"""

_WAITING = f"""
WITH hidden AS (
    SELECT md5(m.text) AS text_hash, min(m.id) AS first_id, count(*) AS same,
           count(DISTINCT m.chat_id) AS chats, bool_or({CARD_WORTHY}) AS card
    FROM messages m JOIN chats c ON c.id = m.chat_id
    WHERE {HIDDEN}
    GROUP BY 1
)
SELECT h.* FROM hidden h
WHERE ($2 OR h.card) AND NOT EXISTS (SELECT 1 FROM guard_alerts a WHERE a.text_hash = h.text_hash)
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


async def waiting(conn: asyncpg.Connection, card_hours: int = DEFAULT_CARD_HOURS) -> int:
    """Сколько скрытых текстов из живого потока ещё ждут своей карточки (историю решают сводкой)."""
    return len(await conn.fetch(_WAITING, card_hours, False))


async def pump(
    conn: asyncpg.Connection, *, per_hour: int = DEFAULT_PER_HOUR, tz: str = "UTC", extra: int = 0,
    card_hours: int = DEFAULT_CARD_HOURS,
) -> dict[str, int]:
    """Отправляет владельцу карточки о скрытых сообщениях живого потока, сколько позволяет предел
    в час, и раз в сутки — сводку обо всём, что ждёт решения без отдельной карточки (Р-99).

    extra — сверх предела: владелец сам нажал «Разбирать по одной» («Показать следующие»); тогда
    карточки идут и по истории. Возвращает счётчики: sent — отправлено карточек, waiting — осталось
    ждать карточки, digest — ушла ли сводка.
    """
    out = {"sent": 0, "waiting": 0, "digest": 0}
    async with conn.transaction():
        # Двум одновременным обходам нельзя отправить одну карточку дважды и превысить предел.
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('shturman.guard.alerts'))")
        if await bridge.get_owner(conn) is None:
            # Уведомлять некого: сообщения остаются скрытыми, карточки уйдут, когда владелец привяжется.
            out["waiting"] = await waiting(conn, card_hours)
            return out
        recent = await conn.fetchval(
            "SELECT count(*) FROM guard_alerts WHERE via = 'card' AND notified_at > now() - interval '1 hour'")
        room = max(0, per_hour - recent) + max(0, extra)
        groups = await conn.fetch(_WAITING, card_hours, extra > 0)
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
                buttons=_card_buttons(alert_id, nonce), handler=CARD_HANDLER, context={"alert_id": alert_id})
            out["sent"] += 1
        out["waiting"] = max(0, len(groups) - out["sent"])
        if not extra:
            found = await overview(conn, card_hours=card_hours)
            # Сводка — когда фоновая проверка истории дошла до конца (числа в ней итоговые) либо, если
            # очередь большая, не позже SUMMARY_WAIT часов после первого скрытого без карточки.
            busy = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM messages m WHERE m.guard_label IS NULL AND m.is_outgoing IS NOT TRUE "
                "AND m.kind = 'message' AND m.text <> '' AND m.deleted_at IS NULL)")
            if found["uncarded"] and (not busy or found["waited_hours"] >= SUMMARY_WAIT):
                # Не чаще раза в сутки (по местной дате): история скрывается сотнями, а решение одно.
                day = datetime.now(_zone(tz)).strftime("%Y%m%d")
                job = await bridge.notify_owner(
                    conn, summary_text(found), buttons=summary_buttons(found), silent=True,
                    dedup_key=f"guard-summary:{day}")
                out["digest"] = int(job is not None)
    return out


def _zone(tz: str) -> Any:
    try:
        return ZoneInfo(tz)
    except Exception:
        return timezone.utc


# --- сводка и решение разом (Р-99) ---

_OVERVIEW = f"""
SELECT count(*) AS total, count(DISTINCT md5(m.text)) AS texts, COALESCE(max(m.id), 0) AS upto,
       count(*) FILTER (WHERE m.guard_model LIKE 'rules-%') AS by_rules,
       count(*) FILTER (WHERE m.guard_model LIKE '%+rules-%') AS by_both,
       count(*) FILTER (WHERE m.guard_model NOT LIKE 'rules-%' AND m.guard_model NOT LIKE '%+rules-%') AS by_model,
       count(*) FILTER (WHERE c.type LIKE '%channel') AS channels,
       count(*) FILTER (WHERE c.type IN ('personal_chat', 'bot_chat')) AS personal,
       count(*) FILTER (WHERE NOT {CARD_WORTHY}) AS history,
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM guard_alerts a
                                      WHERE a.text_hash = md5(m.text) AND a.status = 'sent')) AS carded,
       COALESCE(extract(epoch FROM now() - min(m.guard_checked_at)) / 3600, 0)::int AS waited_hours
FROM messages m JOIN chats c ON c.id = m.chat_id
WHERE {HIDDEN}
"""


async def overview(conn: asyncpg.Connection, *, card_hours: int = DEFAULT_CARD_HOURS,
                   upto: int | None = None) -> dict[str, int]:
    """Что ждёт решения владельца — только числа: всего, разных текстов, по оценщику, откуда, из
    истории, с карточкой в боте, по сильным правилам (`rules.HIGH_WEIGHT`, нынешние правила)."""
    row = dict(await conn.fetchrow(_OVERVIEW, card_hours))
    out = {k: int(v or 0) for k, v in row.items()}
    out["groups"] = out["total"] - out["channels"] - out["personal"]
    out["uncarded"] = out["total"] - out["carded"]
    limit = upto if upto is not None else out["upto"]
    strong = 0
    for r in await conn.fetch(
            f"""SELECT m.text, count(*) AS n FROM messages m JOIN chats c ON c.id = m.chat_id
                WHERE {HIDDEN} AND m.id <= $1 GROUP BY m.text""", limit):
        if rules.high_weight(r["text"] or ""):
            strong += int(r["n"])
    out["strong"] = strong
    return out


def _n(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def summary_text(found: dict[str, int]) -> str:
    """Сводка скрытого, которое ждёт решения, — для бота, приложения и /guard. Только числа."""
    if not found["total"]:
        return "Скрытых сообщений, которые ждут вашего решения, нет."
    lines = [
        f"Скрыто от ассистента и ждут вашего решения: {_n(found['total'])} сообщ. "
        f"(разных текстов: {_n(found['texts'])}).",
        "",
        f"Почему: оценка модели — {_n(found['by_model'])}, правила — {_n(found['by_rules'])}, "
        f"модель и правила — {_n(found['by_both'])}. Сильные правила (просьба отменить или раскрыть "
        f"инструкции) — {_n(found['strong'])}.",
        f"Откуда: каналы — {_n(found['channels'])}, группы — {_n(found['groups'])}, "
        f"личные чаты — {_n(found['personal'])}.",
        f"Из истории, без отдельных карточек — {_n(found['history'])}; карточки уже в боте — {_n(found['carded'])}.",
        "",
        "Проверка ошибается в обе стороны. В разговорах про ИИ, новостях из каналов и рабочих фразах про "
        "доступы и права большая часть скрытого — обычные сообщения. Числа по причинам — ./ops/guard.sh stats "
        "(можно попросить оператора: команда текстов не показывает).",
        "",
        "Решите разом — каждое нажатие ещё раз спросит подтверждение с итоговым числом — или по одному.",
    ]
    return "\n".join(lines)


def summary_buttons(found: dict[str, int]) -> list[list[dict[str, str]]]:
    if not found["total"]:
        return []
    rows = [[bridge.button(f"Показать ассистенту все ({_n(found['total'])})", CALLBACK_MODULE, "b:a")]]
    if 0 < found["strong"] < found["total"]:
        rows.append([bridge.button(f"Все, кроме сильных правил ({_n(found['total'] - found['strong'])})",
                                   CALLBACK_MODULE, "b:x")])
    rows.append([bridge.button("Оставить все скрытыми", CALLBACK_MODULE, "b:k"),
                 bridge.button("Разбирать по одной", CALLBACK_MODULE, "b:o")])
    return rows


async def summary_item(conn: asyncpg.Connection) -> dict[str, Any] | None:
    """Сводка для приложения «Штурман» (раздел «Защита»): тот же текст и те же кнопки, что в боте."""
    found = await overview(conn, card_hours=_settings()[2])
    if not found["total"]:
        return None
    return {"id": 0, "title": f"Скрыто и ждёт решения: {_n(found['total'])}", "text": summary_text(found),
            "buttons": summary_buttons(found), "card_message_id": None}


def _bulk_request(found: dict[str, int], choice: str) -> tuple[str, str, dict[str, Any], int]:
    """Вид действия, текст карточки подтверждения, содержимое и сколько сообщений затронет."""
    total, strong = found["total"], found["strong"]
    tail = (" Решение запоминается по тексту: такие же сообщения потом не скрываются. Сообщения, скрытые "
            "после этого запроса, не затрагиваются.")
    if choice == "a":
        return (RELEASE_ALL,
                f"Показать ассистенту все скрытые сообщения, которые ждут решения: {_n(total)} "
                f"(разных текстов: {_n(found['texts'])}). Ассистент начнёт их видеть: поиск, история "
                "чатов, сводки." + tail,
                {"upto": found["upto"], "except_strong": False}, total)
    if choice == "x":
        return (RELEASE_ALL,
                f"Показать ассистенту скрытые сообщения, кроме сработавших по сильным правилам: "
                f"{_n(total - strong)} из {_n(total)}. Остальные {_n(strong)} (просьба отменить или раскрыть "
                "инструкции) останутся скрытыми и будут ждать решения по одному." + tail,
                {"upto": found["upto"], "except_strong": True}, total - strong)
    return (KEEP_ALL,
            f"Оставить скрытыми от ассистента все {_n(total)} сообщений, которые ждут решения (разных текстов: "
            f"{_n(found['texts'])}). Такие же тексты потом будут скрываться молча, без карточки. Отменить "
            "это решение нельзя.",
            {"upto": found["upto"]}, total)


async def _bulk(conn: asyncpg.Connection, payload: dict[str, Any], status: str) -> str:
    """Решение разом: на каждый текст — так же, как кнопка под карточкой (`_pressed`)."""
    upto = int(payload.get("upto") or 0)
    rows = await conn.fetch(
        f"""SELECT md5(m.text) AS text_hash, min(m.id) AS first_id, min(m.text) AS text
            FROM messages m JOIN chats c ON c.id = m.chat_id
            WHERE {HIDDEN} AND m.id <= $1 GROUP BY 1""", upto)
    kept = 0
    if payload.get("except_strong"):
        strong = {r["text_hash"] for r in rows if rules.high_weight(r["text"] or "")}
        kept = len(strong)
        rows = [r for r in rows if r["text_hash"] not in strong]
    hashes = [r["text_hash"] for r in rows]
    if not hashes:
        return "Скрытых сообщений, которые ждут решения, уже нет."
    if status == "released":
        done = await conn.execute(
            """UPDATE messages SET agent_visible = true, guard_label = 'released', guard_checked_at = now()
               WHERE NOT agent_visible AND guard_label = 'suspect' AND md5(text) = ANY($1::text[])""", hashes)
    else:
        done = await conn.execute(
            """UPDATE messages SET guard_label = 'confirmed', guard_checked_at = now()
               WHERE NOT agent_visible AND guard_label = 'suspect' AND md5(text) = ANY($1::text[])""", hashes)
    count = int(done.split()[-1])
    # Карточки, которые уже в боте: решение то же, кнопки убираются.
    note = "Решение: показано ассистенту (сводкой)" if status == "released" else \
        "Решение: оставлено скрытым от ассистента (сводкой)"
    for card_row in await conn.fetch(
            """UPDATE guard_alerts SET status = $2, decided_at = now(), via = 'bulk'
               WHERE text_hash = ANY($1::text[]) AND status = 'sent'
               RETURNING summary, card_message_id""", hashes, status):
        if card_row["card_message_id"]:
            await bridge.edit_owner_message(conn, card_row["card_message_id"], f"{card_row['summary']}\n\n{note}.")
    # Тексты без карточки: запись решения — та же память «по тексту», с отметкой «сводкой».
    await conn.execute(
        """INSERT INTO guard_alerts (text_hash, message_id, summary, nonce, status, via, decided_at)
           SELECT h, f, 'Решено сводкой, без отдельной карточки.', n, $4, 'bulk', now()
           FROM unnest($1::text[], $2::bigint[], $3::text[]) AS v (h, f, n)
           ON CONFLICT (text_hash) DO NOTHING""",
        hashes, [r["first_id"] for r in rows], [secrets.token_urlsafe(9) for _ in rows], status)
    word = "Показано ассистенту" if status == "released" else "Оставлено скрытым"
    out = f"{word}: {_n(count)} сообщ. (разных текстов: {_n(len(hashes))})."
    if kept:
        out += f" Ждут решения по одному (сильные правила): текстов — {_n(kept)}; «Разбирать по одной» — /guard."
    return out


@confirm.applier(RELEASE_ALL)
async def _apply_release(conn: asyncpg.Connection, payload: dict[str, Any]) -> str:
    # Расширяет то, что видит ассистент: только нажатием владельца (BOT_ONLY в confirm.py).
    confirm.must_not_widen(True)
    return await _bulk(conn, payload, "released")


@confirm.applier(KEEP_ALL)
async def _apply_keep(conn: asyncpg.Connection, payload: dict[str, Any]) -> str:
    return await _bulk(conn, payload, "confirmed")


async def _summary_press(conn: asyncpg.Connection, choice: str) -> dict[str, Any]:
    per_hour, tz, card_hours = _settings()
    if choice == "o":
        done = await pump(conn, per_hour=per_hour, tz=tz, extra=MORE, card_hours=card_hours)
        answer = f"Отправляю карточек: {done['sent']}." if done["sent"] else "Скрытых сообщений без карточки нет."
        return {"answer": answer, "edit_text": None, "remove_buttons": False}
    found = await overview(conn, card_hours=card_hours)
    if not found["total"]:
        return {"answer": "Скрытых сообщений, которые ждут решения, нет.", "edit_text": None, "remove_buttons": True}
    kind, text, payload, affected = _bulk_request(found, choice)
    if not affected:
        return {"answer": "Таких сообщений нет.", "edit_text": None, "remove_buttons": False}
    try:
        await confirm.request_card(conn, kind, text, payload)
    except confirm.TooManyPending:
        return {"answer": "Слишком много карточек ждут решения — сначала ответьте на них.", "edit_text": None,
                "remove_buttons": False}
    except confirm.NoOwner:
        return {"answer": "Нужен свой бот согласований.", "edit_text": None, "remove_buttons": False}
    return {"answer": f"Подтвердите в боте: {_n(affected)} сообщ.", "edit_text": None, "remove_buttons": False}


def _card_buttons(alert_id: int, nonce: str) -> list[list[dict[str, str]]]:
    return [[bridge.button("Показать ассистенту", CALLBACK_MODULE, f"r:{alert_id}:{nonce}"),
             bridge.button("Оставить скрытым", CALLBACK_MODULE, f"c:{alert_id}:{nonce}")]]


async def awaiting_owner(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """Карточки о скрытых сообщениях, по которым владелец ещё не решил, — для Mini App бота
    согласований (`setup_page/approvals.py`). Текст — как в карточке своего бота, с обезвреженной
    цитатой (её владелец видит и в боте: это его собственная переписка, Hermes сюда не дотягивается),
    кнопки — те же, с той же случайной меткой. Решение приходит обратно в `_pressed`."""
    rows = await conn.fetch(
        """SELECT a.id, a.summary, a.nonce, a.card_message_id, m.text, m.deleted_at
           FROM guard_alerts a JOIN messages m ON m.id = a.message_id
           WHERE a.status = 'sent' ORDER BY a.id""")
    out = []
    for r in rows:
        text = card(r["summary"], r["text"] or "") if r["deleted_at"] is None else (
            "Скрыл от ассистента входящее сообщение: оно похоже на попытку им управлять.\n\n"
            + r["summary"] + "\n\nСообщение уже удалено из архива.")
        out.append({"id": r["id"], "title": "Показать это сообщение ассистенту?", "text": text,
                    "buttons": _card_buttons(r["id"], r["nonce"]), "card_message_id": r["card_message_id"]})
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


def _settings() -> tuple[int, str, int]:
    from . import current

    guard = current()
    if guard is None:
        return DEFAULT_PER_HOUR, "UTC", DEFAULT_CARD_HOURS
    return guard.settings.notify_per_hour, guard.tz, guard.settings.card_hours


@bridge.on_callback(CALLBACK_MODULE)
async def _pressed(conn: asyncpg.Connection, rest: str, user_id: int) -> dict[str, Any]:
    gone = {"answer": "Кнопка уже недоступна.", "edit_text": None, "remove_buttons": True}
    if rest == "more":   # кнопка прежних сводок «Показать следующие»
        per_hour, tz, card_hours = _settings()
        done = await pump(conn, per_hour=per_hour, tz=tz, extra=MORE, card_hours=card_hours)
        answer = f"Отправляю: {done['sent']}." if done["sent"] else "Скрытых сообщений без карточки нет."
        return {"answer": answer, "edit_text": None, "remove_buttons": True}
    if rest in ("b:a", "b:x", "b:k", "b:o"):
        return await _summary_press(conn, rest[2])
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
