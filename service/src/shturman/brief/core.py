"""Справка при контакте и сигнал о просрочке (записка «держать, подкладывать, сигналить», шаги 2–3).

Справка. Человек, у которого есть открытая договорённость с владельцем (принятая или ещё не
подтверждённая), пишет ему — владелец получает в боте согласований короткое сообщение: кто пишет и
сколько с ним открытых договорённостей. Подробности — по кнопке «Показать», отдельным сообщением,
с кнопками по каждой договорённости. Не чаще раза в BRIEF_GAP на человека; в тихие часы — ничего;
«Отложить» — сутки тишины по этому человеку.

Сигнал о просрочке. В часы сводки (`pipeline.send_digests`) — одно сообщение о принятых владельцем
договорённостях с определённым сроком (`due_reason = 'ok'`), срок которых прошёл. По договорённости —
один раз на срок: перенесли срок — сигнал снова возможен.

Безопасная доставка: только в личный чат владельца с его ботом согласований (`bridge.notify_owner`);
в тексте уведомления нет сумм, реквизитов, цитат и формулировок договорённостей — только имена и числа.
Формулировки — в сообщении «Показать», которое владелец запрашивает сам. Справку собирает код из
базы, без вызова модели. Сообщение, скрытое защитой, справку не вызывает.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

import asyncpg

from .. import bridge
from ..processing import commitments, dates, people
from . import remind as remind_mod

CALLBACK_MODULE = "br"
HANDLER_OVERDUE = "brief.overdue"
BRIEF_GAP = timedelta(hours=4)        # не чаще раза в столько на одного человека (решение владельца 23.10)
SNOOZE = timedelta(hours=24)          # «Отложить»
MAX_AGE = timedelta(hours=3)          # справка — только на свежее сообщение (как «Вам написали»)
SHOW_LIMIT = 8                        # договорённостей в сообщении «Показать»
OVERDUE_LIMIT = 15                    # договорённостей в одном сигнале о просрочке
STATUSES = ("open", "proposed")
COVERAGE = "Видно только то, что есть в читаемых чатах; чего нет в них, ассистент не знает."

_MESSAGE = """
SELECT m.id, m.chat_id, m.sent_at, m.kind, m.is_outgoing, m.deleted_at, m.edited_at, m.agent_visible,
       m.is_forwarded, m.telegram_via_bot, m.telegram_sender_bot, m.sender_peer_id,
       c.type AS chat_type, c.excluded, c.peer_id AS chat_peer_id, a.role AS account_role,
       p.class AS chat_peer_class, p.is_bot AS chat_peer_is_bot, p.tg_id AS chat_peer_tg_id,
       p.username AS chat_peer_username,
       sp.class AS sender_class, sp.is_bot AS sender_is_bot
FROM messages m
JOIN chats c ON c.id = m.chat_id
JOIN accounts a ON a.id = c.account_id
JOIN peers p ON p.id = c.peer_id
LEFT JOIN peers sp ON sp.id = m.sender_peer_id
WHERE m.id = $1
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except Exception:
        return ZoneInfo("UTC")


# --- кто пишет --------------------------------------------------------------------------------------------

def sender_peer(row: Mapping[str, Any]) -> int | None:
    """Собеседник-человек, написавший входящее; None — не человек, не входящее, служебное, бот."""
    if row["kind"] != "message" or row["deleted_at"] is not None or row["excluded"]:
        return None
    if row["account_role"] != "owner":
        return None    # чаты аккаунта помощника — не переписка владельца (как и у «Вам написали»)
    if row["is_outgoing"] is not False or row["edited_at"] is not None or row["is_forwarded"]:
        return None
    if row["telegram_via_bot"] or row["telegram_sender_bot"] or not row["agent_visible"]:
        return None
    if row["chat_type"] == "personal_chat":
        if row["chat_peer_class"] != "user" or row["chat_peer_is_bot"]:
            return None
        if row["sender_peer_id"] is not None and row["sender_peer_id"] != row["chat_peer_id"]:
            return None
        return row["chat_peer_id"]
    if row["sender_peer_id"] is None or row["sender_class"] != "user" or row["sender_is_bot"]:
        return None
    return row["sender_peer_id"]


async def subject_of(conn: asyncpg.Connection, peer_id: int) -> tuple[str, list[int], int | None]:
    """Ключ справки и все учётные записи человека: подтверждённый или нет — неважно, договорённость
    может быть и с ещё не подтверждённым собеседником."""
    person_id = await people.person_for_peer(conn, peer_id)
    if person_id is not None:
        person_id = await people.active_id(conn, person_id)
    if person_id is None:
        return f"peer:{peer_id}", [peer_id], None
    peers = [int(r["peer_id"]) for r in await conn.fetch(
        "SELECT peer_id FROM person_peers WHERE person_id = $1", person_id)]
    return f"person:{person_id}", sorted(set(peers) | {peer_id}), person_id


async def name_of(conn: asyncpg.Connection, person_id: int | None, peer_id: int) -> str:
    name = None
    if person_id is not None:
        name = await conn.fetchval("SELECT display_name FROM people WHERE id = $1", person_id)
    if not name:
        name = await conn.fetchval("SELECT name FROM peers WHERE id = $1", peer_id)
    return commitments._short(name or "Собеседник", 60)


# --- договорённости с человеком -------------------------------------------------------------------------

async def open_with(conn: asyncpg.Connection, peer_ids: Sequence[int], *, today: date | None,
                    limit: int = 50) -> list[dict[str, Any]]:
    """Открытые договорённости с этими учётными записями — принятые и ещё не подтверждённые (показанные
    или готовые к показу), обе стороны: он обещал владельцу и владелец ему. «В команде» — не сюда."""
    rows = await conn.fetch(
        f"""{commitments._SELECT} AND {commitments.NOT_HELD} AND c.status = ANY($2::text[])
            AND c.direction IN ('owner_owes', 'owed_to_owner')
            AND (c.debtor_peer_id = ANY($1::bigint[]) OR c.creditor_peer_id = ANY($1::bigint[]))
            ORDER BY (c.status = 'open') DESC, c.due_date NULLS LAST, c.id LIMIT $3""",
        list(peer_ids), list(STATUSES), limit)
    return [commitments.to_dict(r, today) for r in rows]


def is_overdue(item: Mapping[str, Any], today: date) -> bool:
    """Просрочена: принята владельцем, срок определён по формулировке (не «сейчас», не событие, не
    «на неделе») и прошёл. Срок без времени — после конца дня по часам владельца."""
    return (item["status"] == "open" and item["due_reason"] == dates.Reason.OK.value
            and item["due_date"] is not None and date.fromisoformat(item["due_date"]) < today)


def counts(items: Sequence[Mapping[str, Any]], today: date) -> dict[str, int]:
    return {
        "total": len(items),
        "theirs": sum(1 for i in items if i["direction"] == "owed_to_owner"),
        "mine": sum(1 for i in items if i["direction"] == "owner_owes"),
        "overdue": sum(1 for i in items if is_overdue(i, today)),
        "unconfirmed": sum(1 for i in items if i["status"] == "proposed"),
    }


async def freshness(conn: asyncpg.Connection, tz: tzinfo) -> str:
    """Когда переписка разобрана в последний раз — граница того, что справка может знать."""
    done = await conn.fetchval("SELECT max(finished_at) FROM processing_runs WHERE status = 'done'")
    if done is None:
        return "Переписка ещё ни разу не разобрана."
    local = done.astimezone(tz)
    return f"Договорённости разобраны по {local:%d.%m %H:%M}."


def brief_text(name: str, n: Mapping[str, int], fresh: str) -> str:
    """Текст уведомления: только имя и числа — без формулировок, сумм и цитат."""
    parts = []
    if n["theirs"]:
        parts.append(f"обещано вам: {n['theirs']}")
    if n["mine"]:
        parts.append(f"обещали вы: {n['mine']}")
    lines = [f"Пишет {name}. Открытых договорённостей: {n['total']} ({'; '.join(parts)})."]
    extra = []
    if n["overdue"]:
        extra.append(f"срок прошёл: {n['overdue']}")
    if n["unconfirmed"]:
        extra.append(f"не подтверждено вами: {n['unconfirmed']}")
    if extra:
        lines.append(", ".join(extra).capitalize() + ".")
    lines.append(f"{fresh} {COVERAGE}")
    return "\n".join(lines)


def item_text(pos: int, item: Mapping[str, Any], today: date, tz: tzinfo | str | None) -> str:
    status = "не подтверждено вами" if item["status"] == "proposed" else "принято"
    if is_overdue(item, today):
        status += "; срок прошёл — подтверждение выполнения в читаемых чатах не найдено"
    return (f"{pos}. {commitments.who_line(item)}: {commitments._short(item['what'], 160)}\n"
            f"{commitments.due_line(item, today, tz)}\n{status}")


def item_buttons(pos: int, item: Mapping[str, Any], *, remind: bool = False) -> list[dict[str, str]]:
    if item["status"] == "proposed":
        return [bridge.button(f"{pos} — принять", CALLBACK_MODULE, f"a:{item['id']}:{item['approval_fingerprint'][:24]}"),
                bridge.button(f"{pos} — не так", CALLBACK_MODULE, f"w:{item['id']}")]
    row = [bridge.button(f"{pos} — выполнено", CALLBACK_MODULE, f"d:{item['id']}"),
           bridge.button(f"{pos} — не так", CALLBACK_MODULE, f"w:{item['id']}")]
    if remind:
        row.append(remind_mod.button(pos, int(item["id"])))
    return row


def details(head: str, items: Sequence[Mapping[str, Any]], today: date, tz: tzinfo | str | None,
            *, brief_id: int | None = None, remind: set[int] | frozenset[int] = frozenset()) -> dict[str, Any]:
    shown = list(items)[:SHOW_LIMIT]
    lines = [head, *(item_text(pos, item, today, tz) for pos, item in enumerate(shown, start=1))]
    if len(items) > len(shown):
        lines.append(f"Ещё {len(items) - len(shown)} — в приложении «Штурман», экран «Договорённости».")
    lines.append("Перенести срок — в приложении, экран «Договорённости».")
    buttons = [item_buttons(pos, item, remind=int(item["id"]) in remind) for pos, item in enumerate(shown, start=1)]
    if brief_id is not None:
        buttons.append([bridge.button("Отложить на сутки", CALLBACK_MODULE, f"z:{brief_id}")])
    return {"text": bridge.fit_message("\n\n".join(lines)), "buttons": buttons}


# --- справка при контакте --------------------------------------------------------------------------------

async def _halted(conn: asyncpg.Connection) -> bool:
    return bool(await conn.fetchval("SELECT shturman_halted()"))


async def _silenced(conn: asyncpg.Connection, row: Mapping[str, Any], local: datetime) -> bool:
    """Тихие часы, «Не сообщать об этом чате», служебные чаты Telegram и управляющий бот — как у
    «Вам написали» (`notify/core.py`): справки там нет."""
    from .. import control_peers, store
    from ..notify import core as notify_core
    from ..notify import settings as notify_settings
    s = await notify_settings.load(conn)
    if notify_core.in_quiet(s, local) or row["chat_id"] in set(s.get("muted_chats") or ()):
        return True
    return store.is_blocked_peer(row["chat_peer_class"], row["chat_peer_tg_id"], row["chat_peer_username"]) \
        or await control_peers.is_blocked(conn, row["chat_peer_class"], row["chat_peer_tg_id"])


async def on_message(conn: asyncpg.Connection, message_id: int, *, tz: str,
                     now: datetime | None = None) -> int | None:
    """Новое живое входящее. Возвращает номер справки, если она ушла владельцу; иначе None."""
    if not bridge.owns_bot():
        return None   # писать некуда: без своего бота согласований справок нет
    now = now or _now()
    row = await conn.fetchrow(_MESSAGE, message_id)
    if row is None or now - row["sent_at"] > MAX_AGE:
        return None
    peer_id = sender_peer(row)
    if peer_id is None or await _halted(conn):
        return None
    zone_ = zone(tz)
    local = now.astimezone(zone_)
    if await _silenced(conn, row, local):
        return None
    key, peers, person_id = await subject_of(conn, peer_id)
    today = local.date()
    items = await open_with(conn, peers, today=today)
    if not items:
        return None
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"shturman.brief:{key}")
        last = await conn.fetchrow(
            "SELECT sent_at, snoozed_until FROM briefs WHERE subject_key = $1 AND kind = 'contact' "
            "ORDER BY sent_at DESC LIMIT 1", key)
        if last is not None and (now - last["sent_at"] < BRIEF_GAP
                                 or (last["snoozed_until"] is not None and last["snoozed_until"] > now)):
            return None
        brief_id = await conn.fetchval(
            "INSERT INTO briefs (subject_key, peer_ids, message_id, sent_at) VALUES ($1, $2, $3, $4) RETURNING id",
            key, peers, message_id, now)
        text = brief_text(await name_of(conn, person_id, peer_id), counts(items, today),
                          await freshness(conn, zone_))
        await bridge.notify_owner(
            conn, text, dedup_key=f"brief:{brief_id}",
            buttons=[[bridge.button("Показать", CALLBACK_MODULE, f"s:{brief_id}"),
                      bridge.button("Отложить на сутки", CALLBACK_MODULE, f"z:{brief_id}")]])
    return brief_id


# --- сигнал о просрочке -----------------------------------------------------------------------------------

async def overdue(conn: asyncpg.Connection, *, today: date, only_new: bool = False,
                  limit: int = OVERDUE_LIMIT) -> list[dict[str, Any]]:
    """Принятые договорённости с определённым сроком, срок которых прошёл (см. `is_overdue`)."""
    fresh = "AND c.overdue_signaled_due IS DISTINCT FROM c.due_date" if only_new else ""
    rows = await conn.fetch(
        f"""{commitments._SELECT} AND c.status = 'open' AND c.due_reason = 'ok' AND c.due_date < $1
            AND c.direction IN ('owner_owes', 'owed_to_owner') {fresh}
            ORDER BY c.due_date, c.id LIMIT $2""", today, limit)
    return [item for item in (commitments.to_dict(r, today) for r in rows) if is_overdue(item, today)]


def overdue_text(items: Sequence[Mapping[str, Any]], names: Sequence[str]) -> str:
    """Только число и имена: формулировки и суммы — по кнопке «Показать»."""
    who = ", ".join(names[:5]) + (f" и ещё {len(names) - 5}" if len(names) > 5 else "")
    return (f"Срок прошёл у договорённостей: {len(items)} ({who}). Подтверждение выполнения в читаемых "
            f"чатах не найдено — возможно, сделано вне них.\n{COVERAGE}")


async def send_overdue(conn: asyncpg.Connection, *, today: date, label: str) -> int:
    """Сигнал о новых просрочках — одним сообщением. Возвращает число договорённостей в нём."""
    await remind_mod.prune(conn)
    if not bridge.owns_bot() or await _halted(conn):
        return 0
    items = await overdue(conn, today=today, only_new=True)
    if not items:
        return 0
    names = []
    for item in items:
        party = item["creditor"] if item["direction"] == "owner_owes" else item["debtor"]
        name = (party or {}).get("name") or "собеседник"
        if name not in names:
            names.append(name)
    ids = [item["id"] for item in items]
    await conn.execute("UPDATE commitments SET overdue_signaled_due = due_date WHERE id = ANY($1::bigint[])", ids)
    await bridge.notify_owner(conn, overdue_text(items, names), dedup_key=f"overdue:{today.isoformat()}:{label}",
                              handler=HANDLER_OVERDUE, context={"commitment_ids": ids},
                              buttons=[[bridge.button("Показать", CALLBACK_MODULE, f"o:{today:%Y%m%d}")]])
    return len(items)


@bridge.on_failure(HANDLER_OVERDUE)
async def _overdue_not_delivered(conn: asyncpg.Connection, job: dict[str, Any], error: str) -> None:
    """Сигнал не доставлен: отметка «уже сообщено» снимается — просрочка попадёт в следующий сигнал."""
    ids = [i for i in (job.get("context") or {}).get("commitment_ids") or [] if isinstance(i, int)]
    if ids:
        await conn.execute("UPDATE commitments SET overdue_signaled_due = NULL WHERE id = ANY($1::bigint[])", ids)


# --- нажатия ----------------------------------------------------------------------------------------------

def _refused(text: str = "Кнопка недоступна.") -> dict[str, Any]:
    return {"answer": text, "edit_text": None, "remove_buttons": False}


async def handle(conn: asyncpg.Connection, rest: str, *, tz: str, clock: Callable[[], datetime] = _now
                 ) -> dict[str, Any]:
    """s:<справка> — показать; z:<справка> — отложить на сутки; o:<ГГГГММДД> — показать просроченные;
    d:<договорённость> — выполнено; a:<договорённость>:<отпечаток> — принять; w:<договорённость> — не так;
    n:<договорённость> — напомнить (`remind.py`); p:<напоминание>:<метка> — «В чат» под напоминанием."""
    action, _, tail = rest.partition(":")
    raw, _, shown = tail.partition(":")
    if action not in ("s", "z", "o", "d", "a", "w", "n", "p") or not raw.isascii() or not raw.isdigit() \
            or len(raw) > 18:
        return _refused()
    now = clock()
    zone_ = zone(tz)
    today = now.astimezone(zone_).date()
    if action in ("s", "z"):
        brief = await conn.fetchrow("SELECT * FROM briefs WHERE id = $1", int(raw))
        if brief is None:
            return _refused("Эта справка уже недоступна.")
        if action == "z":
            await conn.execute(
                "UPDATE briefs SET snoozed_until = $2 WHERE subject_key = $1 AND kind = 'contact'",
                brief["subject_key"], now + SNOOZE)
            return {"answer": "Справок по этому человеку не будет сутки.", "edit_text": None, "remove_buttons": False}
        items = await open_with(conn, brief["peer_ids"], today=today)
        if not items:
            return _refused("Открытых договорённостей с ним уже нет.")
        shown_ = details("Договорённости:", items, today, zone_, brief_id=brief["id"],
                         remind=await remind_mod.offered(conn, items))
        return {"answer": "", "edit_text": None, "remove_buttons": False, "reply": shown_}
    if action == "o":
        items = await overdue(conn, today=today)
        if not items:
            return _refused("Просроченных договорённостей сейчас нет.")
        shown_ = details("Срок прошёл:", items, today, zone_, remind=await remind_mod.offered(conn, items))
        return {"answer": "", "edit_text": None, "remove_buttons": False, "reply": shown_}
    target = int(raw)
    if action == "n":
        return await remind_mod.on_remind(conn, target, today=today)
    if action == "p":
        return await remind_mod.on_put(conn, target, shown)
    if action == "d":
        out = await commitments.close(conn, target, actor="owner", details={"via": "brief"})
        return _refused("Отмечено: выполнено." if out["ok"] else out["error"])
    if action == "a":
        row = await conn.fetchrow("SELECT * FROM commitments WHERE id = $1", target)
        if row is None or row["status"] != "proposed":
            return _refused("Уже решено." if row is not None else "Такой договорённости нет.")
        print_ = commitments.fingerprint(row)
        if shown != print_[:24]:
            return _refused("Справка устарела: договорённость изменилась. Нажмите «Показать» ещё раз.")
        out = await commitments.accept(conn, target, actor="owner", expected_fingerprint=print_)
        return _refused("Принято." if out["ok"] else out["error"])
    status = await conn.fetchval("SELECT status FROM commitments WHERE id = $1", target)
    if status == "proposed":
        out = await commitments.reject(conn, target, actor="owner", details={"via": "brief"})
    else:
        out = await commitments.mark_wrong(conn, target, actor="owner", details={"via": "brief"})
    return _refused("Отмечено: не так." if out["ok"] else out["error"])


# Часовой пояс владельца для нажатий: ставит `brief/service.py` при запуске (по умолчанию — Москва, как
# `SHTURMAN_TIMEZONE`). Регистрация здесь, а не в service: кнопки разбираются и без запущенного модуля.
OWNER_TZ = {"name": "Europe/Moscow"}


@bridge.on_callback(CALLBACK_MODULE)
async def _on_button(conn: asyncpg.Connection, rest: str, user_id: int) -> dict[str, Any]:
    return await handle(conn, rest, tz=OWNER_TZ["name"])
