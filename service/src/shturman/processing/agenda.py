"""Договорённости владельца по срокам: «Я обещал» и «Мне обещали» — экран «Договорённости» приложения
«Штурман», команда /today бота согласований и счётчики «Сейчас» (Р-94).

Что сюда попадает. Только принятые владельцем договорённости (статус `open`) и только те, где он
сторона: «Я обещал» (`owner_owes`) и «Мне обещали» (`owed_to_owner`); обещания между другими людьми —
нет. Видимость та же, что у всех выборок договорённостей (`commitments.VISIBLE`): исключённые чаты,
удалённые сообщения и сообщения, скрытые защитой от внедрённых инструкций, не показываются — как и
ассистенту. Неподтверждённые («Верно / Не так») остаются в «Согласованиях» — здесь только их число.

Чего здесь нет: текста сообщений. Формулировка — поле `what` (короткая, записанная при извлечении),
стороны — имена из реестра людей или Telegram, откуда — название чата и его вид, сфера — названия сфер
владельца, срок — дата. Ни цитаты-источника, ни слов о сроке из сообщения.

Группы по сроку (по часам владельца): «Просрочено» — срок раньше сегодня; «Сегодня»; «На неделе» —
следующие 7 дней; «Позже»; «Без срока» — дата не названа или не определена.

«В команде» (Р-96) — третий список: обещания между другими людьми из чатов владельца (статус `team`),
только для сведения — без «Выполнено», «Перенести» и «Не так», с одной кнопкой «Скрыть». Свежие сверху.

Дата обещания (Р-96): у каждой договорённости — «обещано 23.09» (день сообщения-источника). Срок,
названный событием («к уикли», «как прилечу»), показывается словами из сообщения, коротко: даты у него
нет, а «без срока» было бы неправдой. Других слов из сообщений здесь по-прежнему нет.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg

from . import commitments, dates
from .extract import clean_text

DIRECTIONS = ("owner_owes", "owed_to_owner")
DIRECTION_TITLES = {"owner_owes": "Я обещал", "owed_to_owner": "Мне обещали"}
GROUPS = ("overdue", "today", "week", "later", "no_date")
GROUP_TITLES = {"overdue": "Просрочено", "today": "Сегодня", "week": "На неделе", "later": "Позже",
                "no_date": "Без срока"}
WEEK_DAYS = 7
TEAM_LIMIT = 200            # «В команде» — столько свежих
EVENT_WORDS = 40            # срок-событие словами — не длиннее
LIMIT = 500                 # столько принятых договорённостей читается за раз (предел `list_commitments`)
WHAT_LIMIT = 200
NAME_LIMIT = 80
TITLE_LIMIT = 80
SHOWN = 24                  # знаков отпечатка, которые приложение присылает обратно с действием

KIND_WORDS = (("personal", "личный чат"), ("saved", "избранное"), ("bot", "бот"),
              ("supergroup", "группа"), ("group", "группа"), ("channel", "канал"))


def group_of(due_date: date | None, today: date) -> str:
    if due_date is None:
        return "no_date"
    if due_date < today:
        return "overdue"
    if due_date == today:
        return "today"
    if due_date <= today + timedelta(days=WEEK_DAYS):
        return "week"
    return "later"


def chat_kind(chat_type: str | None) -> str:
    return next((word for mark, word in KIND_WORDS if mark in (chat_type or "")), "чат")


def _short(text: str | None, limit: int) -> str | None:
    if not text:
        return None
    text = " ".join(clean_text(text, 2000).replace("⏎", " ").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def promised(item: dict[str, Any], tz: str | None) -> str:
    """День обещания по часам владельца, ГГГГ-ММ-ДД."""
    sent = datetime.fromisoformat(item["source"]["sent_at"])
    if tz:
        sent = sent.astimezone(ZoneInfo(tz))
    return sent.date().isoformat()


def view(item: dict[str, Any], today: date, spheres: list[str], tz: str | None = None) -> dict[str, Any]:
    """Договорённость для владельца: кто, что, срок, сфера, откуда, когда обещано. Без цитаты; слова
    о сроке — только у срока-события."""
    side = "creditor" if item["direction"] == "owner_owes" else "debtor"
    due = date.fromisoformat(item["due_date"]) if item["due_date"] else None
    at = time.fromisoformat(item["due_time"]) if item["due_time"] else None
    event = item["due_reason"] == dates.Reason.EVENT.value and bool(item["due_expression"])
    return {
        "id": item["id"],
        "direction": item["direction"],
        "who": _short((item[side] or {}).get("name"), NAME_LIMIT),
        "promised": promised(item, tz),
        "due_event": _short(item["due_expression"], EVENT_WORDS) if event else None,
        "what": _short(item["what"], WHAT_LIMIT) or "без формулировки",
        "due_date": item["due_date"],
        "due_time": item["due_time"],
        "due_words": dates.format_due(due, at, today=today) if due else None,
        "days": (due - today).days if due else None,
        # дата названа, но не определена («на днях», «после праздников») — слов не показываем, только признак
        "due_vague": due is None and bool(item["due_expression"]) and not event,
        "group": group_of(due, today),
        "chat": {"title": _short(item["chat"]["title"], TITLE_LIMIT), "kind": chat_kind(item["chat"]["type"])},
        "spheres": spheres,
        "v": item["approval_fingerprint"][:SHOWN],
    }


def team_view(item: dict[str, Any], today: date, spheres: list[str], tz: str | None = None) -> dict[str, Any]:
    """Обещание «В команде»: кто кому (имена), что, срок, когда обещано, откуда. Отпечаток — для «Скрыть»."""
    shown = view(item, today, spheres, tz)
    shown["who"] = _short((item["debtor"] or {}).get("name"), NAME_LIMIT)
    shown["to"] = _short((item["creditor"] or {}).get("name"), NAME_LIMIT)
    return shown


async def board(conn: asyncpg.Connection, *, today: date, tz: str | None = None) -> dict[str, Any]:
    """Оба списка по группам срока, «В команде» и сколько ждут подтверждения."""
    from .. import spheres as spheres_mod

    found = [i for i in await commitments.list_commitments(conn, view="open", today=today, limit=LIMIT)
             if i["direction"] in DIRECTIONS]
    team = await commitments.list_commitments(conn, view="team", today=today, limit=LIMIT)
    team.sort(key=lambda i: (i["source"]["sent_at"], i["id"]), reverse=True)
    team = team[:TEAM_LIMIT]
    marks = await spheres_mod.titles_by_chat(conn, [i["chat"]["id"] for i in [*found, *team]])
    lists: dict[str, dict[str, list[dict[str, Any]]]] = {d: {g: [] for g in GROUPS} for d in DIRECTIONS}
    for item in found:
        shown = view(item, today, marks.get(item["chat"]["id"], []), tz)
        lists[item["direction"]][shown["group"]].append(shown)
    # просроченное — сначала самое давнее (так и идёт выборка: по дате срока)
    return {"today": today.isoformat(), "lists": lists, "waiting": await waiting(conn),
            "team": [team_view(i, today, marks.get(i["chat"]["id"], []), tz) for i in team],
            "truncated": len(found) >= LIMIT, "total": len(found)}


async def waiting(conn: asyncpg.Connection) -> int:
    """Сколько договорённостей ждут подтверждения владельца («Верно / Не так») — видимых."""
    return int(await conn.fetchval(
        f"""SELECT count(*) FROM commitments c JOIN chats ch ON ch.id = c.chat_id
            JOIN messages m ON m.id = c.source_message_id
            WHERE c.status = 'proposed' AND c.direction <> 'others' AND {commitments.NOT_HELD}
              AND {commitments.VISIBLE}"""))


async def counts(conn: asyncpg.Connection, *, today: date) -> dict[str, Any]:
    """Счётчики для «Сейчас»: на сегодня и просрочено — по направлению и всего; ждут подтверждения."""
    rows = await conn.fetch(
        f"""SELECT c.direction, count(*) FILTER (WHERE c.due_date < $1) AS overdue,
                   count(*) FILTER (WHERE c.due_date = $1) AS today
            FROM commitments c JOIN chats ch ON ch.id = c.chat_id JOIN messages m ON m.id = c.source_message_id
            WHERE c.status = 'open' AND c.direction = ANY($2::text[]) AND {commitments.VISIBLE}
            GROUP BY c.direction""", today, list(DIRECTIONS))
    out: dict[str, Any] = {d: {"overdue": 0, "today": 0} for d in DIRECTIONS}
    for r in rows:
        out[r["direction"]] = {"overdue": int(r["overdue"]), "today": int(r["today"])}
    out["overdue"] = sum(out[d]["overdue"] for d in DIRECTIONS)
    out["today"] = sum(out[d]["today"] for d in DIRECTIONS)
    out["waiting"] = await waiting(conn)
    return out
