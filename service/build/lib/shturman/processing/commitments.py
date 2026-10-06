# Жизненный цикл (идея, не код) — по nearai/ironclaw (MIT / Apache-2.0),
# skills/commitment-triage/SKILL.md@b0b999d: сигнал -> подтверждённое обязательство -> закрытие,
# устаревание неподтверждённого. Отбор дублей (идея, не код) — по getzep/graphiti (Apache-2.0),
# graphiti_core/utils/maintenance/edge_operations.py@689de29: подсказку модели проверяет и применяет код.
"""Обязательства в базе: предложения, одобрение владельца, статусы, выборки.

Источник истины — таблица `commitments` (docs/memory.md). Обязательство меняется командой
(«закрой», «перенеси срок»), а не правкой текста страницы.

Статусы:
  proposed  — извлечено моделью, ждёт решения владельца (новые обязательства идут через одобрение);
  open      — принято владельцем;
  done / cancelled — закрыто;
  rejected  — владелец отклонил предложение;
  expired   — предложение осталось без ответа.

Всё, что возвращается наружу, — обычные словари, готовые к JSON. Каждое изменение статуса
пишется в журнал `commitment_events` (кто: owner, auto, model); текста сообщений в журнале нет.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any, Iterable, Sequence

import asyncpg

from .. import bridge
from . import dates
from .extract import Candidate, clean_text, normalize

CALLBACK_MODULE = "cm"
PROPOSAL_TTL_DAYS = 7      # сколько показанное предложение ждёт ответа
DIGEST_MAX_ITEMS = 15      # сколько пунктов уходит владельцу за один прогон
DIGEST_PER_MESSAGE = 5
SIMILAR = 90               # похожесть формулировок, с которой это одно и то же обязательство
SIMILAR_QUOTE = 95         # или почти дословно совпали цитаты
SIMILAR_WITH_HINT = 75     # порог ниже, если на дубль указала ещё и модель
SIMILAR_SAME_MESSAGE = 70  # для двух пунктов из одного и того же сообщения

VIEWS = ("open", "overdue", "today", "week", "proposed", "closed", "all")

_SELECT = """
SELECT c.*, ch.title AS chat_title, ch.type AS chat_type,
       m.tg_message_id AS source_tg_message_id, m.sent_at AS source_sent_at,
       dp.name AS debtor_peer_name, dpe.id AS debtor_person_id, dpe.display_name AS debtor_person_name,
       cp.name AS creditor_peer_name, cpe.id AS creditor_person_id, cpe.display_name AS creditor_person_name
FROM commitments c
JOIN chats ch ON ch.id = c.chat_id
JOIN messages m ON m.id = c.source_message_id
LEFT JOIN peers dp ON dp.id = c.debtor_peer_id
LEFT JOIN person_peers dpp ON dpp.peer_id = c.debtor_peer_id
LEFT JOIN people dpe ON dpe.id = dpp.person_id
LEFT JOIN peers cp ON cp.id = c.creditor_peer_id
LEFT JOIN person_peers cpp ON cpp.peer_id = c.creditor_peer_id
LEFT JOIN people cpe ON cpe.id = cpp.person_id
"""


# --- представление ---------------------------------------------------------------------------

def _party(row: asyncpg.Record, side: str, is_owner: bool) -> dict[str, Any] | None:
    peer_id = row[f"{side}_peer_id"]
    if peer_id is None and not is_owner:
        return None
    name = row[f"{side}_person_name"] or row[f"{side}_peer_name"]
    return {"peer_id": peer_id, "person_id": row[f"{side}_person_id"],
            "name": clean_text(name, 80) if name else None, "is_owner": is_owner}


def to_dict(row: asyncpg.Record, today: date | None = None) -> dict[str, Any]:
    """Обязательство как словарь: с цитатой-источником и ссылкой на сообщение."""
    direction = row["direction"]
    return {
        "id": row["id"],
        "status": row["status"],
        "direction": direction,
        "what": row["what"],
        "debtor": _party(row, "debtor", direction == "owner_owes"),
        "creditor": _party(row, "creditor", direction == "owed_to_owner"),
        "due_expression": row["due_expression"],
        "due_date": row["due_date"].isoformat() if row["due_date"] else None,
        "due_time": row["due_time"].strftime("%H:%M") if row["due_time"] else None,
        "due_part": row["due_part"],
        "due_reason": row["due_reason"],
        "overdue": bool(today and row["status"] == "open" and row["due_date"] and row["due_date"] < today),
        "source_quote": row["source_quote"],
        "source": {"message_id": row["source_message_id"], "tg_message_id": row["source_tg_message_id"],
                   "sent_at": row["source_sent_at"].isoformat(), "ref": f"msg:{row['source_message_id']}"},
        "chat": {"id": row["chat_id"], "title": row["chat_title"], "type": row["chat_type"]},
        "created_at": row["created_at"].isoformat(),
        "decided_at": row["decided_at"].isoformat() if row["decided_at"] else None,
        "closed_at": row["closed_at"].isoformat() if row["closed_at"] else None,
    }


async def get_commitment(
    conn: asyncpg.Connection, commitment_id: int, *, today: date | None = None, with_events: bool = False,
) -> dict[str, Any] | None:
    row = await conn.fetchrow(_SELECT + " WHERE c.id = $1", commitment_id)
    if row is None:
        return None
    out = to_dict(row, today)
    if with_events:
        out["events"] = [
            {"at": e["at"].isoformat(), "actor": e["actor"], "action": e["action"],
             "from": e["from_status"], "to": e["to_status"],
             "details": json.loads(e["details"]) if isinstance(e["details"], str) else e["details"]}
            for e in await conn.fetch(
                "SELECT * FROM commitment_events WHERE commitment_id = $1 ORDER BY at, id", commitment_id)
        ]
    return out


async def list_commitments(
    conn: asyncpg.Connection, *, view: str = "open", today: date, person_id: int | None = None,
    chat_id: int | None = None, direction: str | None = None, limit: int = 100,
) -> list[dict[str, Any]]:
    """Выборка обязательств.

    view: open — принятые и не закрытые; overdue — просроченные; today — срок сегодня;
    week — срок с сегодня до конца недели; proposed — ждут решения владельца;
    closed — выполненные и отменённые; all — все.
    `person_id` — обязательства, где человек должен или ему должны (по всем его учётным записям).
    """
    if view not in VIEWS:
        raise ValueError(f"неизвестная выборка: {view}")
    where, args = [], []

    def arg(value: Any) -> str:
        args.append(value)
        return f"${len(args)}"

    if view in ("open", "overdue", "today", "week"):
        where.append("c.status = 'open'")
    elif view == "proposed":
        where.append("c.status = 'proposed'")
    elif view == "closed":
        where.append("c.status IN ('done', 'cancelled')")
    if view == "overdue":
        where.append(f"c.due_date < {arg(today)}")
    elif view == "today":
        where.append(f"c.due_date = {arg(today)}")
    elif view == "week":
        sunday = today + timedelta(days=6 - today.weekday())
        where.append(f"c.due_date BETWEEN {arg(today)} AND {arg(sunday)}")
    if person_id is not None:
        p = arg(person_id)
        where.append(
            f"""(c.debtor_peer_id IN (SELECT peer_id FROM person_peers WHERE person_id = {p})
                 OR c.creditor_peer_id IN (SELECT peer_id FROM person_peers WHERE person_id = {p})
                 OR EXISTS (SELECT 1 FROM people o WHERE o.id = {p} AND o.is_owner
                            AND c.direction IN ('owner_owes', 'owed_to_owner')))""")
    if chat_id is not None:
        where.append(f"c.chat_id = {arg(chat_id)}")
    if direction is not None:
        where.append(f"c.direction = {arg(direction)}")
    sql = _SELECT + (" WHERE " + " AND ".join(where) if where else "")
    sql += f" ORDER BY c.due_date NULLS LAST, c.due_time NULLS LAST, c.id LIMIT {arg(max(1, min(int(limit), 500)))}"
    return [to_dict(r, today) for r in await conn.fetch(sql, *args)]


# --- журнал ----------------------------------------------------------------------------------

async def log_event(
    conn: asyncpg.Connection, commitment_id: int, *, actor: str, action: str,
    from_status: str | None, to_status: str | None, details: dict[str, Any] | None = None,
) -> None:
    await conn.execute(
        """INSERT INTO commitment_events (commitment_id, actor, action, from_status, to_status, details)
           VALUES ($1, $2, $3, $4, $5, $6::jsonb)""",
        commitment_id, actor, action, from_status, to_status, json.dumps(details or {}, ensure_ascii=False),
    )


# --- предложения из ответа модели --------------------------------------------------------------

def _dates_conflict(a: date | None, b: date | None) -> bool:
    return a is not None and b is not None and a != b


def _numbers(text: str) -> tuple[str, ...]:
    return tuple(sorted(re.findall(r"\d+", text)))


def find_duplicate(
    existing: Sequence[asyncpg.Record | dict[str, Any]], *, source_message_id: int,
    debtor_peer_id: int | None, what: str, quote: str, due_date: date | None, hinted_id: int | None = None,
) -> int | None:
    """Есть ли уже такое обязательство в этом чате. Решает код; подсказка модели (`hinted_id`)
    только снижает порог похожести и никогда не действует сама по себе."""
    from rapidfuzz import fuzz

    what_n, quote_n = normalize(what), normalize(quote)
    for row in existing:
        row_what, row_quote = normalize(row["what"]), normalize(row["source_quote"])
        # разные числа — разные обязательства («счёт 15» и «счёт 16»), как бы ни были похожи слова
        same_numbers = _numbers(what_n) == _numbers(row_what)
        if row["source_message_id"] == source_message_id:
            # то же сообщение: тот же фрагмент или та же суть — в любом статусе, включая отклонённые
            if quote_n == row_quote or quote_n in row_quote or row_quote in quote_n \
                    or (same_numbers and fuzz.token_set_ratio(what_n, row_what) >= SIMILAR_SAME_MESSAGE):
                return row["id"]
            continue
        if row["status"] not in ("proposed", "open") or row["debtor_peer_id"] != debtor_peer_id:
            continue
        if _dates_conflict(row["due_date"], due_date) or not same_numbers:
            continue
        ratio = fuzz.token_sort_ratio(what_n, row_what)
        if ratio >= SIMILAR or fuzz.ratio(quote_n, row_quote) >= SIMILAR_QUOTE \
                or (hinted_id == row["id"] and ratio >= SIMILAR_WITH_HINT):
            return row["id"]
    return None


async def existing_in_chat(conn: asyncpg.Connection, chat_id: int, limit: int = 300) -> list[asyncpg.Record]:
    return await conn.fetch(
        """SELECT id, source_message_id, debtor_peer_id, direction, what, source_quote, due_date,
                  due_expression, status
           FROM commitments WHERE chat_id = $1 ORDER BY id DESC LIMIT $2""",
        chat_id, limit,
    )


def candidate_due(candidate: Candidate, tz: tzinfo | str | None) -> dates.Resolution:
    """Срок кандидата: дату считает dates.py от времени того сообщения, в котором срок назван."""
    if candidate.due_expression and candidate.due_message is not None:
        return dates.resolve_due_expression(candidate.due_expression, candidate.due_message.sent_at, tz)
    if candidate.due_dropped:
        return dates.Resolution.unresolved(dates.Reason.NOT_IN_SOURCE)
    return dates.Resolution.unresolved(dates.Reason.NO_DEADLINE)


async def propose(
    conn: asyncpg.Connection, *, chat_id: int, candidate: Candidate, debtor_peer_id: int | None,
    creditor_peer_id: int | None, direction: str, tz: tzinfo | str | None,
    run_id: int | None = None, model: str | None = None,
) -> int:
    """Записывает обязательство как предложение владельцу (статус proposed)."""
    due = candidate_due(candidate, tz)
    commitment_id = await conn.fetchval(
        """INSERT INTO commitments (chat_id, source_message_id, due_message_id, debtor_peer_id, creditor_peer_id,
                                    direction, what, source_quote, due_expression, due_date, due_time, due_part,
                                    due_reason, status, run_id, model)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, 'proposed', $14, $15)
           RETURNING id""",
        chat_id, candidate.message.id,
        candidate.due_message.id if candidate.due_expression and candidate.due_message else None,
        debtor_peer_id, creditor_peer_id, direction, candidate.what, candidate.quote,
        candidate.due_expression, due.due_date, due.due_time, due.part_of_day, due.reason.value,
        run_id, (model or "")[:120] or None,
    )
    await log_event(conn, commitment_id, actor="model", action="proposed", from_status=None,
                    to_status="proposed", details={"due_reason": due.reason.value})
    return commitment_id


# --- изменения статуса --------------------------------------------------------------------------

def _result(ok: bool, **extra: Any) -> dict[str, Any]:
    return {"ok": ok, **extra}


async def _locked(conn: asyncpg.Connection, commitment_id: int) -> asyncpg.Record | None:
    return await conn.fetchrow("SELECT * FROM commitments WHERE id = $1 FOR UPDATE", commitment_id)


async def _move(
    conn: asyncpg.Connection, commitment_id: int, *, allowed: Iterable[str], to: str, action: str,
    actor: str, details: dict[str, Any] | None = None, today: date | None = None,
) -> dict[str, Any]:
    async with conn.transaction():
        row = await _locked(conn, commitment_id)
        if row is None:
            return _result(False, error="Такого обязательства нет.", code="not_found")
        if row["status"] == to:
            return _result(True, changed=False, commitment=await get_commitment(conn, commitment_id, today=today))
        if row["status"] not in allowed:
            return _result(False, error=f"Нельзя: обязательство в статусе «{STATUS_TEXT[row['status']]}».",
                           code="bad_status", commitment=await get_commitment(conn, commitment_id, today=today))
        closing = to in ("done", "cancelled")
        deciding = row["status"] == "proposed"
        await conn.execute(
            """UPDATE commitments SET status = $2, updated_at = now(),
                      decided_at = CASE WHEN $3 THEN now() ELSE decided_at END,
                      closed_at = CASE WHEN $4 THEN now() WHEN $2 = 'open' THEN NULL ELSE closed_at END
               WHERE id = $1""",
            commitment_id, to, deciding, closing,
        )
        await log_event(conn, commitment_id, actor=actor, action=action, from_status=row["status"],
                        to_status=to, details=details)
        if to != "open":
            # закрытому обязательству чужие предложения изменений уже не нужны
            await conn.execute(
                """UPDATE commitment_changes SET status = 'expired', decided_at = now()
                   WHERE commitment_id = $1 AND status = 'proposed'""", commitment_id)
    return _result(True, changed=True, commitment=await get_commitment(conn, commitment_id, today=today))


STATUS_TEXT = {
    "proposed": "ждёт решения", "open": "открыто", "done": "выполнено", "cancelled": "отменено",
    "rejected": "отклонено", "expired": "не подтверждено",
}


async def accept(conn: asyncpg.Connection, commitment_id: int, *, actor: str = "owner", **kw: Any) -> dict[str, Any]:
    """Владелец принял предложение: обязательство становится открытым."""
    return await _move(conn, commitment_id, allowed=("proposed",), to="open", action="accepted", actor=actor, **kw)


async def reject(conn: asyncpg.Connection, commitment_id: int, *, actor: str = "owner", **kw: Any) -> dict[str, Any]:
    return await _move(conn, commitment_id, allowed=("proposed",), to="rejected", action="rejected", actor=actor, **kw)


async def close(conn: asyncpg.Connection, commitment_id: int, *, actor: str = "owner",
                details: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
    """Отмечает обязательство выполненным."""
    return await _move(conn, commitment_id, allowed=("open", "proposed"), to="done", action="closed",
                       actor=actor, details=details, **kw)


async def cancel(conn: asyncpg.Connection, commitment_id: int, *, actor: str = "owner",
                 details: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
    return await _move(conn, commitment_id, allowed=("open", "proposed"), to="cancelled", action="cancelled",
                       actor=actor, details=details, **kw)


async def reopen(conn: asyncpg.Connection, commitment_id: int, *, actor: str = "owner", **kw: Any) -> dict[str, Any]:
    """Возвращает обязательство в открытые: закрытое по ошибке, отклонённое или неподтверждённое."""
    return await _move(conn, commitment_id, allowed=("done", "cancelled", "rejected", "expired", "proposed"),
                       to="open", action="reopened", actor=actor, **kw)


async def _set_due(
    conn: asyncpg.Connection, commitment_id: int, *, expression: str, due: dates.Resolution,
    due_message_id: int | None,
) -> None:
    await conn.execute(
        """UPDATE commitments SET due_expression = $2, due_date = $3, due_time = $4, due_part = $5,
                  due_reason = $6, due_message_id = $7, updated_at = now() WHERE id = $1""",
        commitment_id, expression, due.due_date, due.due_time, due.part_of_day, due.reason.value, due_message_id,
    )


async def reschedule(
    conn: asyncpg.Connection, commitment_id: int, wording: str, *, tz: tzinfo | str | None,
    now: datetime | None = None, actor: str = "owner", today: date | None = None,
) -> dict[str, Any]:
    """Переносит срок по формулировке («к пятнице», «до 20.10», «2026-11-01»). Дату считает
    dates.py от момента команды; если формулировка неоднозначна, срок не меняется."""
    now = now or datetime.now(timezone.utc)
    due = dates.resolve_due_expression(wording, now, tz)
    if not due.resolved:
        return _result(False, error=dates.REASON_TEXT[due.reason], code=due.reason.value)
    async with conn.transaction():
        row = await _locked(conn, commitment_id)
        if row is None:
            return _result(False, error="Такого обязательства нет.", code="not_found")
        if row["status"] not in ("open", "proposed"):
            return _result(False, error=f"Нельзя: обязательство в статусе «{STATUS_TEXT[row['status']]}».",
                           code="bad_status")
        await _set_due(conn, commitment_id, expression=clean_text(wording, 80), due=due, due_message_id=None)
        await log_event(
            conn, commitment_id, actor=actor, action="rescheduled", from_status=row["status"],
            to_status=row["status"],
            details={"from": row["due_date"].isoformat() if row["due_date"] else None,
                     "to": due.due_date.isoformat()})
    return _result(True, changed=True, commitment=await get_commitment(conn, commitment_id, today=today))


# --- изменения, предложенные моделью -------------------------------------------------------------

async def propose_change(
    conn: asyncpg.Connection, *, commitment_id: int, kind: str, evidence_message_id: int, quote: str,
    new_due_expression: str | None = None, due: dates.Resolution | None = None, run_id: int | None = None,
) -> int | None:
    """Записывает предложение закрыть, отменить или перенести. Само ничего не применяет.
    Возвращает None, если такое предложение уже есть или перенос без вычисленной даты."""
    if kind == "rescheduled" and (due is None or not due.resolved):
        return None
    known = await conn.fetchval(
        """SELECT 1 FROM commitment_changes
           WHERE commitment_id = $1 AND kind = $2
             AND (status = 'proposed' OR evidence_message_id = $3)""",
        commitment_id, kind, evidence_message_id)
    if known:
        return None
    change_id = await conn.fetchval(
        """INSERT INTO commitment_changes (commitment_id, kind, evidence_message_id, evidence_quote,
                                           new_due_expression, new_due_date, new_due_time, new_due_part, run_id)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id""",
        commitment_id, kind, evidence_message_id, quote, new_due_expression,
        due.due_date if due else None, due.due_time if due else None, due.part_of_day if due else None, run_id,
    )
    await log_event(conn, commitment_id, actor="model", action=f"{kind}_proposed", from_status="open",
                    to_status="open", details={"change_id": change_id})
    return change_id


async def apply_change(conn: asyncpg.Connection, change_id: int, *, actor: str = "owner") -> dict[str, Any]:
    """Владелец согласился с предложенным изменением: оно применяется."""
    async with conn.transaction():
        change = await conn.fetchrow("SELECT * FROM commitment_changes WHERE id = $1 FOR UPDATE", change_id)
        if change is None:
            return _result(False, error="Предложение уже неактуально.", code="not_found")
        if change["status"] != "proposed":
            return _result(True, changed=False, status=change["status"])
        row = await _locked(conn, change["commitment_id"])
        if row["status"] != "open":
            await conn.execute(
                "UPDATE commitment_changes SET status = 'expired', decided_at = now() WHERE id = $1", change_id)
            return _result(False, error="Обязательство уже не открыто.", code="bad_status")
        await conn.execute(
            "UPDATE commitment_changes SET status = 'accepted', decided_at = now() WHERE id = $1", change_id)
        details = {"proposed_by": "model", "change_id": change_id}
        if change["kind"] == "rescheduled":
            due = dates.Resolution.ok(change["new_due_date"], change["new_due_time"], change["new_due_part"])
            await _set_due(conn, row["id"], expression=change["new_due_expression"], due=due,
                           due_message_id=change["evidence_message_id"])
            await log_event(
                conn, row["id"], actor=actor, action="rescheduled", from_status="open", to_status="open",
                details={**details, "from": row["due_date"].isoformat() if row["due_date"] else None,
                         "to": change["new_due_date"].isoformat()})
            return _result(True, changed=True, kind="rescheduled")
        if change["kind"] == "fulfilled":
            out = await close(conn, change["commitment_id"], actor=actor, details=details)
        else:
            out = await cancel(conn, change["commitment_id"], actor=actor, details=details)
        return _result(out["ok"], changed=out.get("changed", False), kind=change["kind"])


async def reject_change(conn: asyncpg.Connection, change_id: int, *, actor: str = "owner") -> dict[str, Any]:
    async with conn.transaction():
        change = await conn.fetchrow("SELECT * FROM commitment_changes WHERE id = $1 FOR UPDATE", change_id)
        if change is None:
            return _result(False, error="Предложение уже неактуально.", code="not_found")
        if change["status"] != "proposed":
            return _result(True, changed=False, status=change["status"])
        await conn.execute(
            "UPDATE commitment_changes SET status = 'rejected', decided_at = now() WHERE id = $1", change_id)
        await log_event(conn, change["commitment_id"], actor=actor, action=f"{change['kind']}_declined",
                        from_status="open", to_status="open", details={"change_id": change_id})
    return _result(True, changed=True, status="rejected")


# --- устаревание и удаление ------------------------------------------------------------------------

async def expire_stale(conn: asyncpg.Connection, *, days: int = PROPOSAL_TTL_DAYS) -> dict[str, int]:
    """Тихо снимает показанные владельцу предложения, оставшиеся без ответа."""
    rows = await conn.fetch(
        """UPDATE commitments SET status = 'expired', updated_at = now()
           WHERE status = 'proposed' AND notified_at < now() - make_interval(days => $1)
           RETURNING id""", days)
    for r in rows:
        await log_event(conn, r["id"], actor="auto", action="expired", from_status="proposed", to_status="expired")
    changes = await conn.execute(
        """UPDATE commitment_changes SET status = 'expired', decided_at = now()
           WHERE status = 'proposed' AND notified_at < now() - make_interval(days => $1)""", days)
    return {"commitments": len(rows), "changes": int(changes.split()[-1])}


async def purge_for_messages(conn: asyncpg.Connection, message_ids: Sequence[int]) -> dict[str, int]:
    """Сообщения удалены: убираем всё, что из них выведено (docs/memory.md, «Удаление»).

    Уходит обязательство, если удалено сообщение с обещанием или сообщение, из которого взят срок;
    уходит предложенное или применённое изменение, если удалено сообщение-основание.
    """
    if not message_ids:
        return {"commitments": 0, "changes": 0}
    ids = list(message_ids)
    changes = await conn.execute(
        "DELETE FROM commitment_changes WHERE evidence_message_id = ANY($1::bigint[])", ids)
    gone = await conn.execute(
        "DELETE FROM commitments WHERE source_message_id = ANY($1::bigint[]) OR due_message_id = ANY($1::bigint[])", ids)
    return {"commitments": int(gone.split()[-1]), "changes": int(changes.split()[-1])}


async def purge_orphans(conn: asyncpg.Connection) -> dict[str, int]:
    """Обход по базе на случай пропущенного события: производное от удалённых сообщений
    и от чатов, которые владелец исключил."""
    gone = await conn.execute(
        """DELETE FROM commitments c
           WHERE EXISTS (SELECT 1 FROM messages m
                         WHERE m.id IN (c.source_message_id, c.due_message_id) AND m.deleted_at IS NOT NULL)
              OR EXISTS (SELECT 1 FROM chats ch WHERE ch.id = c.chat_id AND ch.excluded)""")
    changes = await conn.execute(
        """DELETE FROM commitment_changes x
           WHERE EXISTS (SELECT 1 FROM messages m WHERE m.id = x.evidence_message_id AND m.deleted_at IS NOT NULL)""")
    return {"commitments": int(gone.split()[-1]), "changes": int(changes.split()[-1])}


# --- сводка владельцу ---------------------------------------------------------------------------------

def _short(text: str | None, limit: int) -> str:
    text = clean_text(text or "", 2000).replace("⏎", " ")
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def who_line(item: dict[str, Any]) -> str:
    """«Иван Петров → вам», «Вы → Иван Петров», «Иван → Пётр»."""
    debtor = (item["debtor"] or {}).get("name") or "кто-то"
    creditor = (item["creditor"] or {}).get("name")
    title = item["chat"]["title"]
    if item["direction"] == "owner_owes":
        return f"Вы → {creditor}" if creditor else (f"Вы (чат «{_short(title, 40)}»)" if title else "Вы")
    if item["direction"] == "owed_to_owner":
        return f"{debtor} → вам"
    return f"{debtor} → {creditor}" if creditor else (f"{debtor} (чат «{_short(title, 40)}»)" if title else debtor)


def due_line(item: dict[str, Any], today: date | None = None) -> str:
    if item["due_date"]:
        when = dates.format_due(date.fromisoformat(item["due_date"]),
                                time.fromisoformat(item["due_time"]) if item["due_time"] else None, today=today)
        return f"Срок: {when} («{_short(item['due_expression'], 60)}»)"
    if item["due_expression"]:
        return f"Срок: «{_short(item['due_expression'], 60)}» — дата не определена"
    return "Срок не назван"


_CHANGE_TEXT = {"fulfilled": "Похоже, выполнено", "cancelled": "Похоже, отменено", "rescheduled": "Похоже, срок перенесён"}
_MARK = {"open": "✓ принято", "rejected": "✗ отклонено", "expired": "— без ответа", "done": "✓ выполнено",
         "cancelled": "✓ отменено", "accepted": "✓ применено", "proposed": "… ждёт"}


def _render_commitment(pos: int, item: dict[str, Any], today: date | None) -> str:
    return (f"{pos}. {who_line(item)}: {_short(item['what'], 200)}\n"
            f"{due_line(item, today)}\n«{_short(item['source_quote'], 160)}»")


def _render_change(pos: int, change: asyncpg.Record, item: dict[str, Any], today: date | None) -> str:
    head = _CHANGE_TEXT[change["kind"]]
    if change["kind"] == "rescheduled":
        head += f" на {dates.format_due(change['new_due_date'], change['new_due_time'], today=today)}"
    return (f"{pos}. {head}: {who_line(item)} — {_short(item['what'], 200)}\n"
            f"«{_short(change['evidence_quote'], 160)}»")


async def build_digests(
    conn: asyncpg.Connection, *, run_id: int, today: date,
    max_items: int = DIGEST_MAX_ITEMS, per_message: int = DIGEST_PER_MESSAGE,
) -> list[dict[str, Any]]:
    """Собирает сводку для владельца: несколько пронумерованных пунктов на сообщение, под каждым
    пунктом кнопки ✓ и ✗. Помечает показанное. Возвращает [{"text", "buttons", "batch"}]."""
    new = await conn.fetch(
        _SELECT + """ WHERE c.status = 'proposed' AND c.digest_batch IS NULL
                      ORDER BY (c.direction = 'owner_owes') DESC, c.due_date NULLS LAST, c.id LIMIT $1""",
        max_items)
    changes = await conn.fetch(
        """SELECT x.* FROM commitment_changes x JOIN commitments c ON c.id = x.commitment_id
           WHERE x.status = 'proposed' AND x.digest_batch IS NULL AND c.status = 'open'
           ORDER BY x.id LIMIT $1""",
        max(0, max_items - len(new)))
    items: list[tuple[str, Any]] = [("c", row) for row in new] + [("x", row) for row in changes]
    if not items:
        return []
    waiting = await conn.fetchval(
        """SELECT (SELECT count(*) FROM commitments WHERE status = 'proposed' AND digest_batch IS NULL)
                + (SELECT count(*) FROM commitment_changes x JOIN commitments c ON c.id = x.commitment_id
                   WHERE x.status = 'proposed' AND x.digest_batch IS NULL AND c.status = 'open')""") - len(items)
    out = []
    chunks = [items[i: i + per_message] for i in range(0, len(items), per_message)]
    for n, chunk in enumerate(chunks, start=1):
        batch = f"{run_id}.{n}"
        lines, buttons = [], []
        for pos, (kind, row) in enumerate(chunk, start=1):
            if kind == "c":
                lines.append(_render_commitment(pos, to_dict(row, today), today))
                buttons.append([bridge.button(f"{pos} ✓", CALLBACK_MODULE, f"a:{row['id']}"),
                                bridge.button(f"{pos} ✗", CALLBACK_MODULE, f"r:{row['id']}")])
                await conn.execute(
                    "UPDATE commitments SET digest_batch = $2, digest_pos = $3, notified_at = now() WHERE id = $1",
                    row["id"], batch, pos)
            else:
                item = await get_commitment(conn, row["commitment_id"], today=today)
                lines.append(_render_change(pos, row, item, today))
                buttons.append([bridge.button(f"{pos} ✓", CALLBACK_MODULE, f"ca:{row['id']}"),
                                bridge.button(f"{pos} ✗", CALLBACK_MODULE, f"cr:{row['id']}")])
                await conn.execute(
                    "UPDATE commitment_changes SET digest_batch = $2, digest_pos = $3, notified_at = now() WHERE id = $1",
                    row["id"], batch, pos)
        head = "Обязательства из переписки. ✓ — верно, ✗ — нет."
        if len(chunks) > 1:
            head += f" ({n} из {len(chunks)})"
        text = head + "\n\n" + "\n\n".join(lines)
        if n == len(chunks) and waiting > 0:
            text += f"\n\nЕщё ждут решения: {waiting}. Придут в следующей сводке."
        out.append({"text": text, "buttons": buttons, "batch": batch})
    return out


async def unmark_batch(conn: asyncpg.Connection, batch: str) -> int:
    """Сообщение-сводка не доставлено: его нерешённые пункты снова ждут показа."""
    a = await conn.execute(
        """UPDATE commitments SET digest_batch = NULL, digest_pos = NULL, notified_at = NULL
           WHERE digest_batch = $1 AND status = 'proposed'""", batch)
    b = await conn.execute(
        """UPDATE commitment_changes SET digest_batch = NULL, digest_pos = NULL, notified_at = NULL
           WHERE digest_batch = $1 AND status = 'proposed'""", batch)
    return int(a.split()[-1]) + int(b.split()[-1])


async def batch_summary(conn: asyncpg.Connection, batch: str) -> tuple[bool, str]:
    """Состояние одного сообщения-сводки: все ли пункты решены и итоговый текст."""
    rows = await conn.fetch(
        """SELECT digest_pos AS pos, status, what, NULL::text AS kind FROM commitments WHERE digest_batch = $1
           UNION ALL
           SELECT x.digest_pos, x.status, c.what, x.kind FROM commitment_changes x
           JOIN commitments c ON c.id = x.commitment_id WHERE x.digest_batch = $1
           ORDER BY pos""", batch)
    lines = []
    for r in rows:
        prefix = f"{_CHANGE_TEXT[r['kind']].lower()}: " if r["kind"] else ""
        mark = _MARK.get(r["status"], r["status"])
        if r["kind"] is None and r["status"] in ("done", "cancelled", "open"):
            mark = "✓ принято"
        lines.append(f"{r['pos']}. {prefix}{_short(r['what'], 120)} — {mark}")
    done = bool(rows) and all(r["status"] != "proposed" for r in rows)
    return done, "Обязательства из переписки — решено:\n" + "\n".join(lines)


async def handle_callback(conn: asyncpg.Connection, rest: str) -> dict[str, Any]:
    """Нажатие кнопки под сводкой. Данные: a:<id> принять, r:<id> отклонить,
    ca:<id> применить изменение, cr:<id> оставить как есть."""
    action, _, raw = rest.partition(":")
    if action not in ("a", "r", "ca", "cr") or not raw.isdigit():
        return {"answer": "Кнопка недоступна.", "edit_text": None, "remove_buttons": False}
    target = int(raw)
    if action in ("a", "r"):
        row = await conn.fetchrow("SELECT status, digest_batch FROM commitments WHERE id = $1", target)
        if row is None:
            return {"answer": "Это обязательство уже удалено.", "edit_text": None, "remove_buttons": False}
        if row["status"] != "proposed":
            answer = f"Уже решено: {STATUS_TEXT[row['status']]}."
        elif action == "a":
            await accept(conn, target)
            answer = "Принято."
        else:
            await reject(conn, target)
            answer = "Отклонено."
        batch = row["digest_batch"]
    else:
        row = await conn.fetchrow("SELECT status, kind, digest_batch FROM commitment_changes WHERE id = $1", target)
        if row is None:
            return {"answer": "Это предложение уже неактуально.", "edit_text": None, "remove_buttons": False}
        if row["status"] != "proposed":
            answer = "Уже решено."
        elif action == "ca":
            out = await apply_change(conn, target)
            answer = {"fulfilled": "Закрыто.", "cancelled": "Отменено.", "rescheduled": "Срок перенесён."}.get(
                out.get("kind"), "Готово.") if out["ok"] else out["error"]
        else:
            await reject_change(conn, target)
            answer = "Оставлено как есть."
        batch = row["digest_batch"]
    if batch:
        done, text = await batch_summary(conn, batch)
        if done:
            return {"answer": answer, "edit_text": text, "remove_buttons": True}
    return {"answer": answer, "edit_text": None, "remove_buttons": False}
