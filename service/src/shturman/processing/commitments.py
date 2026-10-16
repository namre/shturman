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
  rejected  — владелец отклонил предложение (или скрыл из «В команде»);
  expired   — снято без владельца: осталось без ответа, сделано или отменено ещё до показа или до
              решения, заменено перепроверкой — причина в `quiet_reason` (Р-96);
  team      — «В команде»: обещание между другими людьми, только для сведения (Р-96).

Тихо — без владельца — снимается только то, что он ещё не принял: proposed и team (`quiet_close`).
Принятое (open) меняется только его нажатием: модель лишь предлагает изменение (`propose_change`).

Всё, что возвращается наружу, — обычные словари, готовые к JSON. Каждое изменение статуса
пишется в журнал `commitment_events` (кто: owner, auto, model); текста сообщений в журнале нет.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any, Iterable, Sequence

import asyncpg

from .. import authority, bridge, words
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

DIGEST_BUDGET = bridge.MESSAGE_LIMIT - 200   # единиц UTF-16 на сообщение-сводку; запас — на шапку и хвост
DIGEST_MAX_SENDS = 3       # сколько раз пункт уходит владельцу, если сводка не доставляется

VIEWS = ("open", "overdue", "today", "week", "next_week", "proposed", "closed", "team", "all")

# Тихо, без владельца, снимается только ещё не принятое им (Р-96). Принятое — только его нажатием.
QUIET_CLOSABLE = ("proposed", "team")
TEAM_TTL_DAYS = 14         # «В команде» обещание держится столько дней после того, как дано
IMMEDIATE_HOLD = timedelta(hours=24)   # быстрое обещание показывается, только если не сделано за сутки

# «Не так» с причиной (Р-96): код -> подпись кнопки. Причина пишется в журнал (`commitment_events.details`).
WRONG_REASONS: dict[str, str] = {
    "not_promise": "не обещание", "wrong_person": "не тот человек", "wrong_what": "неверно что",
    "wrong_due": "неверный срок", "duplicate": "дубль", "already_done": "уже сделано",
}

# Поля словаря обязательства, взятые из чужого текста (сообщения, имена, названия чатов).
# Тот, кто показывает словарь агенту, обязан подавать их как данные, а не как указания.
UNTRUSTED_FIELDS = ["what", "source_quote", "due_expression", "debtor.name", "creditor.name", "chat.title",
                    "request_quote"]

# Видимость. Обязательство не показывается никому — ни владельцу в сводке, ни агенту, ни
# маршрутам, — если его чат исключён из архива или удалено сообщение, из которого оно выведено
# (с обещанием или со сроком). Строки при этом могут ещё лежать в базе: их убирает подписчик
# события и обход `purge_orphans`, но показывать их нельзя уже сейчас.
# То же — если сообщение-источник скрыто защитой от внедрённых инструкций (`agent_visible`):
# выведенное из текста, который сочли попыткой управлять ассистентом, не показывается, пока
# владелец не откроет сообщение. Такие строки не стираются: решение владельца их вернёт.
# Псевдонимы: c — commitments, ch — chats, m — сообщение-источник.
VISIBLE = """NOT ch.excluded AND m.deleted_at IS NULL AND m.agent_visible
             AND NOT EXISTS (SELECT 1 FROM messages dm
                             WHERE dm.id IN (c.due_message_id, c.request_message_id)
                               AND (dm.deleted_at IS NOT NULL OR NOT dm.agent_visible))"""
# Показано или будет показано владельцу: быстрое обещание до конца суток ожидания — нет (Р-96).
NOT_HELD = "(c.hold_until IS NULL OR c.hold_until <= now())"

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
WHERE """ + VISIBLE + "\n"


async def is_visible(conn: asyncpg.Connection, commitment_id: int) -> bool:
    return bool(await conn.fetchval(
        f"""SELECT 1 FROM commitments c JOIN chats ch ON ch.id = c.chat_id
            JOIN messages m ON m.id = c.source_message_id WHERE c.id = $1 AND {VISIBLE}""", commitment_id))


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
        "owner_approved": approved(row),
        "legacy_unverified": row["legacy_unverified"],
        "approval_fingerprint": fingerprint(row),
        "approved_at": row["approved_at"].isoformat() if row["approved_at"] else None,
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
        "request_quote": row["request_quote"],
        "quiet_reason": row["quiet_reason"],
        "hold_until": row["hold_until"].isoformat() if row["hold_until"] else None,
        "source": {"message_id": row["source_message_id"], "tg_message_id": row["source_tg_message_id"],
                   "sent_at": row["source_sent_at"].isoformat(), "ref": f"msg:{row['source_message_id']}"},
        "chat": {"id": row["chat_id"], "title": row["chat_title"], "type": row["chat_type"]},
        "project_id": row["project_id"],
        "created_at": row["created_at"].isoformat(),
        "decided_at": row["decided_at"].isoformat() if row["decided_at"] else None,
        "closed_at": row["closed_at"].isoformat() if row["closed_at"] else None,
        "untrusted_fields": list(UNTRUSTED_FIELDS),
    }


async def get_commitment(
    conn: asyncpg.Connection, commitment_id: int, *, today: date | None = None, with_events: bool = False,
) -> dict[str, Any] | None:
    """Одно обязательство; None — если его нет или оно не видно (чат исключён, источник удалён)."""
    row = await conn.fetchrow(_SELECT + " AND c.id = $1", commitment_id)
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
    peer_id: int | None = None, chat_id: int | None = None, direction: str | None = None, limit: int = 100,
    project_id: int | None = None, chat_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Выборка обязательств. `chat_ids` — только из этих чатов (сужение по сферам); None — из всех.

    view: open — принятые и не закрытые; overdue — просроченные; today — срок сегодня;
    week — срок с сегодня до конца недели; next_week — срок с понедельника по воскресенье
    следующей недели; proposed — ждут решения владельца; closed — выполненные и отменённые;
    team — «В команде», обещания между другими людьми (Р-96); all — все.
    `person_id` — обязательства, где человек должен или ему должны (по всем его учётным записям);
    `peer_id` — то же по одной учётной записи Telegram (peers.id); `project_id` — обязательства проекта.
    Обязательства исключённых чатов и удалённых сообщений не возвращаются.
    """
    if view not in VIEWS:
        raise ValueError(f"неизвестная выборка: {view}")
    where, args = [], []

    def arg(value: Any) -> str:
        args.append(value)
        return f"${len(args)}"

    if view in ("open", "overdue", "today", "week", "next_week"):
        where.append("c.status = 'open'")
    elif view == "proposed":
        # быстрое обещание, которое ещё ждёт суток проверки, владельцу не показано и здесь не видно
        where.append(f"c.status = 'proposed' AND {NOT_HELD}")
    elif view == "team":
        where.append(f"c.status = 'team' AND {NOT_HELD}")
    elif view == "closed":
        where.append("c.status IN ('done', 'cancelled')")
    if view == "overdue":
        where.append(f"c.due_date < {arg(today)}")
    elif view == "today":
        where.append(f"c.due_date = {arg(today)}")
    elif view == "week":
        sunday = today + timedelta(days=6 - today.weekday())
        where.append(f"c.due_date BETWEEN {arg(today)} AND {arg(sunday)}")
    elif view == "next_week":
        monday = today + timedelta(days=7 - today.weekday())
        where.append(f"c.due_date BETWEEN {arg(monday)} AND {arg(monday + timedelta(days=6))}")
    if peer_id is not None:
        p = arg(peer_id)
        where.append(f"(c.debtor_peer_id = {p} OR c.creditor_peer_id = {p})")
    if person_id is not None:
        p = arg(person_id)
        where.append(
            f"""(c.debtor_peer_id IN (SELECT peer_id FROM person_peers WHERE person_id = {p})
                 OR c.creditor_peer_id IN (SELECT peer_id FROM person_peers WHERE person_id = {p})
                 OR EXISTS (SELECT 1 FROM people o WHERE o.id = {p} AND o.is_owner
                            AND c.direction IN ('owner_owes', 'owed_to_owner')))""")
    if chat_id is not None:
        where.append(f"c.chat_id = {arg(chat_id)}")
    if chat_ids is not None:
        where.append(f"c.chat_id = ANY({arg(list(chat_ids))}::bigint[])")
    if direction is not None:
        where.append(f"c.direction = {arg(direction)}")
    if project_id is not None:
        where.append(f"c.project_id = {arg(project_id)}")
    sql = _SELECT + "".join(f" AND {condition}" for condition in where)
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
        if row["status"] not in ("proposed", "open", "team") or row["debtor_peer_id"] != debtor_peer_id:
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


QUIET_BEFORE_SHOWN = {"fulfilled": "done_before_shown", "cancelled": "cancelled_before_shown"}
QUIET_BEFORE_DECISION = {"fulfilled": "done_before_decision", "cancelled": "cancelled_before_decision"}
QUIET_TEAM = {"fulfilled": "team_done", "cancelled": "team_cancelled"}


async def propose(
    conn: asyncpg.Connection, *, chat_id: int, candidate: Candidate, debtor_peer_id: int | None,
    creditor_peer_id: int | None, direction: str, tz: tzinfo | str | None,
    run_id: int | None = None, model: str | None = None, project_id: int | None = None,
    now: datetime | None = None, checked: Any = None, prompt_version: str | None = None,
) -> tuple[int, str]:
    """Записывает обязательство из ответа модели. project_id — проект, к которому оно относится
    (projects.for_commitment), или None. Возвращает номер и что с ним стало:

      proposed  — предложение владельцу (статус proposed);
      held      — быстрое обещание: предложение, но показ отложен до суток после обещания (`hold_until`);
      team      — «В команде»: между другими людьми, только для сведения;
      done_before_shown / cancelled_before_shown (и team_done / team_cancelled) — по сообщениям после
                  обещания уже сделано или отменено: записано тихо (expired с причиной), владельцу не
                  показывается; основание — номер сообщения в журнале (Р-96).

    `checked` — последнее сообщение чата, по которое модель уже видела переписку (Msg): с него
    продолжит проверка закрытия."""
    now = now or datetime.now(timezone.utc)
    due = candidate_due(candidate, tz)
    status, quiet, hold = ("team" if direction == "others" else "proposed"), None, None
    if candidate.status in ("fulfilled", "cancelled") and candidate.evidence is not None:
        quiet = (QUIET_TEAM if status == "team" else QUIET_BEFORE_SHOWN)[candidate.status]
        status = "expired"
    elif due.is_immediate:
        until = candidate.message.sent_at + IMMEDIATE_HOLD
        if until > now:
            hold = until
    commitment_id = await conn.fetchval(
        """INSERT INTO commitments (chat_id, source_message_id, due_message_id, debtor_peer_id, creditor_peer_id,
                                    direction, what, source_quote, due_expression, due_date, due_time, due_part,
                                    due_reason, status, run_id, model, project_id, quiet_reason, hold_until,
                                    checked_until_at, checked_until_id, request_message_id, request_quote,
                                    prompt_version, closed_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20,
                   $21, $22, $23, $24, CASE WHEN $14 = 'expired' THEN now() END)
           RETURNING id""",
        chat_id, candidate.message.id,
        candidate.due_message.id if candidate.due_expression and candidate.due_message else None,
        debtor_peer_id, creditor_peer_id, direction, candidate.what, candidate.quote,
        candidate.due_expression, due.due_date, due.due_time, due.part_of_day, due.reason.value,
        status, run_id, (model or "")[:120] or None, project_id, quiet, hold,
        checked.sent_at if checked is not None else None, checked.id if checked is not None else None,
        candidate.request.id if candidate.request is not None else None, candidate.request_quote,
        prompt_version,
    )
    details: dict[str, Any] = {"due_reason": due.reason.value}
    if hold is not None:
        details["held_until"] = hold.isoformat()
    await log_event(conn, commitment_id, actor="model", action="proposed", from_status=None,
                    to_status="team" if quiet and direction == "others" else ("proposed" if quiet else status),
                    details=details)
    if quiet:
        await log_event(conn, commitment_id, actor="model", action="quiet_closed",
                        from_status="team" if direction == "others" else "proposed", to_status="expired",
                        details={"reason": quiet, "evidence_message_id": candidate.evidence.id})
        return commitment_id, quiet
    return commitment_id, "held" if hold is not None else status


async def quiet_close(
    conn: asyncpg.Connection, commitment_id: int, *, kind: str, evidence_message_id: int | None,
    reason: str | None = None, actor: str = "model",
) -> str | None:
    """Тихо снимает ещё не принятое владельцем (proposed, team): сделано или отменено до его решения,
    заменено перепроверкой. Принятое (open) не трогается никогда: его меняет только нажатие владельца.
    Показанный в сводке пункт снимается с показа (кнопка ответит «Уже решено»). Возвращает причину
    или None, если снимать нечего."""
    row = await conn.fetchrow("SELECT status FROM commitments WHERE id = $1 FOR UPDATE", commitment_id)
    if row is None or row["status"] not in QUIET_CLOSABLE:
        return None
    if reason is None:
        reason = (QUIET_TEAM if row["status"] == "team" else QUIET_BEFORE_DECISION).get(kind)
    if reason is None:
        return None
    done = await conn.execute(
        """UPDATE commitments SET status = 'expired', quiet_reason = $2, updated_at = now(), closed_at = now()
           WHERE id = $1 AND status = ANY($3::text[])""", commitment_id, reason, list(QUIET_CLOSABLE))
    if done.split()[-1] == "0":
        return None
    await log_event(conn, commitment_id, actor=actor, action="quiet_closed", from_status=row["status"],
                    to_status="expired", details={"reason": reason, "evidence_message_id": evidence_message_id})
    return reason


async def quiet_reschedule(
    conn: asyncpg.Connection, commitment_id: int, *, expression: str, due: dates.Resolution,
    evidence_message_id: int,
) -> bool:
    """Срок ещё не принятого владельцем перенесён в переписке: новый срок записывается сразу. Если пункт
    уже ушёл в сводке, он уйдёт ещё раз — с новым сроком (кнопка под старой карточкой устарела)."""
    row = await conn.fetchrow("SELECT status, due_date FROM commitments WHERE id = $1 FOR UPDATE", commitment_id)
    if row is None or row["status"] not in QUIET_CLOSABLE or not due.resolved:
        return False
    await _set_due(conn, commitment_id, expression=expression, due=due, due_message_id=evidence_message_id)
    await conn.execute(
        """UPDATE commitments SET digest_batch = NULL, digest_pos = NULL, notified_at = NULL, hold_until = NULL,
                  digest_fingerprint = NULL
           WHERE id = $1 AND status = 'proposed'""", commitment_id)
    await log_event(conn, commitment_id, actor="model", action="rescheduled", from_status=row["status"],
                    to_status=row["status"],
                    details={"from": row["due_date"].isoformat() if row["due_date"] else None,
                             "to": due.due_date.isoformat(), "evidence_message_id": evidence_message_id})
    return True


# --- изменения статуса --------------------------------------------------------------------------

_APPROVAL_FIELDS = ("chat_id", "source_message_id", "due_message_id", "debtor_peer_id", "creditor_peer_id",
                    "direction", "what", "source_quote", "due_expression", "due_date", "due_time", "due_part")


def fingerprint(row: Any) -> str:
    """Согласие относится к содержимому, не к меняющемуся рабочему статусу."""
    body = {key: row[key] for key in _APPROVAL_FIELDS}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def approved(row: Any) -> bool:
    return bool(row["approved_at"] and row["approved_by"] and row["approved_via"]
                and row["approval_fingerprint"] == fingerprint(row))


def _actor(requested: str) -> str:
    # Строка actor не является доказательством: API-клиент не может создать owner context.
    if authority.is_owner():
        return "owner"
    return requested if requested in ("auto", "model") else "agent"


async def _record_approval(conn: asyncpg.Connection, row: Any) -> None:
    principal = authority.get_owner_principal()
    if principal is None:
        raise PermissionError("нет независимого подтверждения владельца")
    await conn.execute(
        """UPDATE commitments SET approved_at = now(), approved_by = $2, approved_via = $3,
                  approval_fingerprint = $4, legacy_unverified = false WHERE id = $1""",
        row["id"], str(authority.current_owner_id()), principal.source, fingerprint(row))

def _result(ok: bool, **extra: Any) -> dict[str, Any]:
    return {"ok": ok, **extra}


async def _locked(conn: asyncpg.Connection, commitment_id: int) -> asyncpg.Record | None:
    return await conn.fetchrow("SELECT * FROM commitments WHERE id = $1 FOR UPDATE", commitment_id)


async def _move(
    conn: asyncpg.Connection, commitment_id: int, *, allowed: Iterable[str], to: str, action: str,
    actor: str, details: dict[str, Any] | None = None, today: date | None = None,
    expected_fingerprint: str | None = None,
) -> dict[str, Any]:
    async with conn.transaction():
        row = await _locked(conn, commitment_id)
        if row is None or not await is_visible(conn, commitment_id):
            return _result(False, error="Такой договорённости нет.", code="not_found")
        if expected_fingerprint is not None and fingerprint(row) != expected_fingerprint:
            return _result(False, error="Содержание изменилось. Подтвердите новое предложение.", code="changed_meanwhile")
        needs_approval = to in ("open", "done") and not approved(row)
        if needs_approval and not authority.is_owner():
            return _result(False, error="Эта договорённость ещё не подтверждена владельцем.", code="approval_required")
        if row["status"] == to:
            if needs_approval:
                await _record_approval(conn, row)
                await log_event(conn, commitment_id, actor="owner", action="approved", from_status=to, to_status=to)
            return _result(True, changed=False, commitment=await get_commitment(conn, commitment_id, today=today))
        if row["status"] not in allowed:
            return _result(False, error=f"Нельзя: договорённость в статусе «{STATUS_TEXT[row['status']]}».",
                           code="bad_status", commitment=await get_commitment(conn, commitment_id, today=today))
        if needs_approval:
            await _record_approval(conn, row)
        closing = to in ("done", "cancelled")
        deciding = row["status"] == "proposed"
        await conn.execute(
            """UPDATE commitments SET status = $2, updated_at = now(),
                      decided_at = CASE WHEN $3 THEN now() ELSE decided_at END,
                      closed_at = CASE WHEN $4 THEN now() WHEN $2 = 'open' THEN NULL ELSE closed_at END
               WHERE id = $1""",
            commitment_id, to, deciding, closing,
        )
        await log_event(conn, commitment_id, actor=_actor(actor), action=action, from_status=row["status"],
                        to_status=to, details=details)
        if to != "open":
            # закрытому обязательству чужие предложения изменений уже не нужны
            await conn.execute(
                """UPDATE commitment_changes SET status = 'expired', decided_at = now()
                   WHERE commitment_id = $1 AND status = 'proposed'""", commitment_id)
    return _result(True, changed=True, commitment=await get_commitment(conn, commitment_id, today=today))


STATUS_TEXT = {
    "proposed": "ждёт решения", "open": "открыто", "done": "выполнено", "cancelled": "отменено",
    "rejected": "отклонено", "expired": "не подтверждено", "team": "в команде",
}


async def accept(conn: asyncpg.Connection, commitment_id: int, *, actor: str = "agent", **kw: Any) -> dict[str, Any]:
    """Владелец принял предложение: обязательство становится открытым."""
    return await _move(conn, commitment_id, allowed=("proposed",), to="open", action="accepted", actor=actor, **kw)


async def reject(conn: asyncpg.Connection, commitment_id: int, *, actor: str = "agent", **kw: Any) -> dict[str, Any]:
    return await _move(conn, commitment_id, allowed=("proposed",), to="rejected", action="rejected", actor=actor, **kw)


async def close(conn: asyncpg.Connection, commitment_id: int, *, actor: str = "agent",
                details: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
    """Отмечает обязательство выполненным."""
    return await _move(conn, commitment_id, allowed=("open", "proposed"), to="done", action="closed",
                       actor=actor, details=details, **kw)


async def cancel(conn: asyncpg.Connection, commitment_id: int, *, actor: str = "agent",
                 details: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
    return await _move(conn, commitment_id, allowed=("open", "proposed"), to="cancelled", action="cancelled",
                       actor=actor, details=details, **kw)


async def reopen(conn: asyncpg.Connection, commitment_id: int, *, actor: str = "agent", **kw: Any) -> dict[str, Any]:
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
    now: datetime | None = None, actor: str = "agent", today: date | None = None,
    expected_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Переносит срок по формулировке («к пятнице», «до 20.10», «2026-11-01»). Дату считает
    dates.py от момента команды; если формулировка неоднозначна, срок не меняется."""
    now = now or datetime.now(timezone.utc)
    due = dates.resolve_due_expression(wording, now, tz)
    if not due.resolved:
        return _result(False, error=dates.REASON_TEXT[due.reason], code=due.reason.value)
    return await _new_due(conn, commitment_id, expression=clean_text(wording, 80), due=due,
                          allowed=("open", "proposed"), actor=actor, today=today,
                          expected_fingerprint=expected_fingerprint)


async def _new_due(
    conn: asyncpg.Connection, commitment_id: int, *, expression: str, due: dates.Resolution,
    allowed: Iterable[str], actor: str, today: date | None, expected_fingerprint: str | None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    async with conn.transaction():
        row = await _locked(conn, commitment_id)
        if row is None or not await is_visible(conn, commitment_id):
            return _result(False, error="Такой договорённости нет.", code="not_found")
        if expected_fingerprint is not None and fingerprint(row) != expected_fingerprint:
            return _result(False, error="Содержание изменилось. Подтвердите новый срок.", code="changed_meanwhile")
        if row["status"] not in allowed:
            return _result(False, error=f"Нельзя: договорённость в статусе «{STATUS_TEXT[row['status']]}».",
                           code="bad_status")
        if row["status"] != "proposed" and not authority.is_owner():
            return _result(False, error="Изменение срока требует подтверждения владельца.", code="approval_required")
        await _set_due(conn, commitment_id, expression=expression, due=due, due_message_id=None)
        if row["status"] == "open":
            await _record_approval(conn, await _locked(conn, commitment_id))
        await log_event(
            conn, commitment_id, actor=_actor(actor), action="rescheduled", from_status=row["status"],
            to_status=row["status"],
            details={**(details or {}), "from": row["due_date"].isoformat() if row["due_date"] else None,
                     "to": due.due_date.isoformat()})
    return _result(True, changed=True, commitment=await get_commitment(conn, commitment_id, today=today))


async def reschedule_to(
    conn: asyncpg.Connection, commitment_id: int, new_date: date, *, actor: str = "agent",
    today: date | None = None, expected_fingerprint: str | None = None, details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Новый срок датой — так переносит владелец из приложения «Штурман» (Р-94): «на завтра», «через
    неделю», выбранная дата. Только принятой договорённости и только владельцем; время срока снимается.
    Формулировка срока — сама дата: слов из переписки у нового срока нет."""
    return await _new_due(conn, commitment_id, expression=f"{new_date:%d.%m.%Y}", due=dates.Resolution.ok(new_date),
                          allowed=("open",), actor=actor, today=today, expected_fingerprint=expected_fingerprint,
                          details=details)


async def mark_wrong(
    conn: asyncpg.Connection, commitment_id: int, *, actor: str = "agent", details: dict[str, Any] | None = None,
    **kw: Any,
) -> dict[str, Any]:
    """«Не так» у уже принятой договорённости (Р-94): извлечено неверно. Становится отклонённой — как
    предложение, которое владелец не подтвердил: в сводках и выборках её больше нет, а то же обещание
    из того же сообщения заново не предлагается (`find_duplicate`). Только владелец: убрать
    договорённость из вида ассистент сам не может."""
    if not authority.is_owner():
        return _result(False, error="Это решает владелец.", code="approval_required")
    return await _move(conn, commitment_id, allowed=("open",), to="rejected", action="marked_wrong", actor=actor,
                       details=details, **kw)


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


async def apply_change(conn: asyncpg.Connection, change_id: int, *, actor: str = "agent",
                       expected_fingerprint: str | None = None) -> dict[str, Any]:
    """Владелец согласился с предложенным изменением: оно применяется."""
    async with conn.transaction():
        if not authority.is_owner():
            return _result(False, error="Изменение требует подтверждения владельца.", code="approval_required")
        change = await conn.fetchrow("SELECT * FROM commitment_changes WHERE id = $1 FOR UPDATE", change_id)
        if change is None or not await is_visible(conn, change["commitment_id"]):
            return _result(False, error="Предложение уже неактуально.", code="not_found")
        if change["status"] != "proposed":
            return _result(True, changed=False, status=change["status"])
        row = await _locked(conn, change["commitment_id"])
        if expected_fingerprint is not None and fingerprint(row) != expected_fingerprint:
            return _result(False, error="Договорённость изменилась. Нужна новая карточка.", code="changed_meanwhile")
        if row["status"] != "open":
            await conn.execute(
                "UPDATE commitment_changes SET status = 'expired', decided_at = now() WHERE id = $1", change_id)
            return _result(False, error="Договорённость уже не открыта.", code="bad_status")
        await conn.execute(
            "UPDATE commitment_changes SET status = 'accepted', decided_at = now() WHERE id = $1", change_id)
        details = {"proposed_by": "model", "change_id": change_id}
        if change["kind"] == "rescheduled":
            due = dates.Resolution.ok(change["new_due_date"], change["new_due_time"], change["new_due_part"])
            await _set_due(conn, row["id"], expression=change["new_due_expression"], due=due,
                           due_message_id=change["evidence_message_id"])
            await _record_approval(conn, await _locked(conn, row["id"]))
            await log_event(
                conn, row["id"], actor=_actor(actor), action="rescheduled", from_status="open", to_status="open",
                details={**details, "from": row["due_date"].isoformat() if row["due_date"] else None,
                         "to": change["new_due_date"].isoformat()})
            return _result(True, changed=True, kind="rescheduled")
        if change["kind"] == "fulfilled":
            out = await close(conn, change["commitment_id"], actor=actor, details=details)
        else:
            out = await cancel(conn, change["commitment_id"], actor=actor, details=details)
        return _result(out["ok"], changed=out.get("changed", False), kind=change["kind"])


async def reject_change(conn: asyncpg.Connection, change_id: int, *, actor: str = "agent") -> dict[str, Any]:
    async with conn.transaction():
        change = await conn.fetchrow("SELECT * FROM commitment_changes WHERE id = $1 FOR UPDATE", change_id)
        if change is None or not await is_visible(conn, change["commitment_id"]):
            return _result(False, error="Предложение уже неактуально.", code="not_found")
        if change["status"] != "proposed":
            return _result(True, changed=False, status=change["status"])
        await conn.execute(
            "UPDATE commitment_changes SET status = 'rejected', decided_at = now() WHERE id = $1", change_id)
        await log_event(conn, change["commitment_id"], actor=_actor(actor), action=f"{change['kind']}_declined",
                        from_status="open", to_status="open", details={"change_id": change_id})
    return _result(True, changed=True, status="rejected")


# --- устаревание и удаление ------------------------------------------------------------------------

async def expire_stale(conn: asyncpg.Connection, *, days: int = PROPOSAL_TTL_DAYS) -> dict[str, int]:
    """Тихо снимает показанные владельцу предложения, оставшиеся без ответа."""
    rows = await conn.fetch(
        """UPDATE commitments SET status = 'expired', quiet_reason = 'unanswered', updated_at = now()
           WHERE status = 'proposed' AND notified_at < now() - make_interval(days => $1)
           RETURNING id""", days)
    for r in rows:
        await log_event(conn, r["id"], actor="auto", action="expired", from_status="proposed", to_status="expired",
                        details={"reason": "unanswered"})
    # «В команде» — только для сведения: давнее уходит из списка само (Р-96)
    team = await conn.fetch(
        """UPDATE commitments c SET status = 'expired', quiet_reason = 'team_stale', updated_at = now()
           FROM messages m
           WHERE m.id = c.source_message_id AND c.status = 'team'
             AND m.sent_at < now() - make_interval(days => $1)
           RETURNING c.id""", TEAM_TTL_DAYS)
    for r in team:
        await log_event(conn, r["id"], actor="auto", action="expired", from_status="team", to_status="expired",
                        details={"reason": "team_stale"})
    changes = await conn.execute(
        """UPDATE commitment_changes SET status = 'expired', decided_at = now()
           WHERE status = 'proposed' AND notified_at < now() - make_interval(days => $1)""", days)
    return {"commitments": len(rows), "team": len(team), "changes": int(changes.split()[-1])}


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
        """DELETE FROM commitments WHERE source_message_id = ANY($1::bigint[]) OR due_message_id = ANY($1::bigint[])
                                      OR request_message_id = ANY($1::bigint[])""", ids)
    return {"commitments": int(gone.split()[-1]), "changes": int(changes.split()[-1])}


async def purge_orphans(conn: asyncpg.Connection) -> dict[str, int]:
    """Обход по базе на случай пропущенного события: производное от удалённых сообщений
    и от чатов, которые владелец исключил."""
    gone = await conn.execute(
        """DELETE FROM commitments c
           WHERE EXISTS (SELECT 1 FROM messages m
                         WHERE m.id IN (c.source_message_id, c.due_message_id, c.request_message_id)
                           AND m.deleted_at IS NOT NULL)
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


def promised_on(item: dict[str, Any], tz: tzinfo | str | None = None) -> str:
    """«обещано 23.09» — день сообщения-источника по часам владельца (Р-96)."""
    sent = datetime.fromisoformat(item["source"]["sent_at"])
    local = dates.to_local(sent, tz) if tz is not None else sent
    return f"обещано {local:%d.%m}"


def due_line(item: dict[str, Any], today: date | None = None, tz: tzinfo | str | None = None) -> str:
    promised = f" · {promised_on(item, tz)}"
    if item["due_reason"] == dates.Reason.IMMEDIATE.value:
        return f"Быстрое обещание («{_short(item['due_expression'], 40)}»){promised} — похоже, так и не сделано"
    if item["due_date"]:
        when = dates.format_due(date.fromisoformat(item["due_date"]),
                                time.fromisoformat(item["due_time"]) if item["due_time"] else None, today=today)
        return f"Срок: {when} («{_short(item['due_expression'], 60)}»){promised}"
    if item["due_expression"]:
        note = dates.describe(item["due_reason"]) or "дата не определена"
        return f"Срок: «{_short(item['due_expression'], 60)}» — {note}{promised}"
    return f"Срок не назван{promised}"


_CHANGE_TEXT = {"fulfilled": "Похоже, выполнено", "cancelled": "Похоже, отменено", "rescheduled": "Похоже, срок перенесён"}
_MARK = {"open": "✓ принято", "rejected": "✗ отклонено", "expired": "— без ответа", "done": "✓ выполнено",
         "cancelled": "✓ отменено", "accepted": "✓ применено", "proposed": "… ждёт"}
# Снятое без владельца, пока карточка ждала его решения (Р-96).
_QUIET_MARK = {"done_before_decision": "— уже сделано", "cancelled_before_decision": "— уже отменено",
               "recheck": "— заменено перепроверкой"}


def _render_commitment(item: dict[str, Any], today: date | None, tz: tzinfo | str | None = None) -> str:
    return (f"{who_line(item)}: {_short(item['what'], 200)}\n"
            f"{due_line(item, today, tz)}\n«{_short(item['source_quote'], 160)}»")


def _render_change(change: asyncpg.Record, item: dict[str, Any], today: date | None) -> str:
    head = _CHANGE_TEXT[change["kind"]]
    if change["kind"] == "rescheduled":
        head += f" на {dates.format_due(change['new_due_date'], change['new_due_time'], today=today)}"
    return (f"{head}: {who_line(item)} — {_short(item['what'], 200)}\n"
            f"«{_short(change['evidence_quote'], 160)}»")


async def build_digests(
    conn: asyncpg.Connection, *, run_id: int, today: date, max_items: int = DIGEST_MAX_ITEMS,
    per_message: int = DIGEST_PER_MESSAGE, budget: int = DIGEST_BUDGET, tz: tzinfo | str | None = None,
) -> list[dict[str, Any]]:
    """Собирает сводку для владельца: несколько пронумерованных пунктов на сообщение, под каждым
    пунктом кнопки ✓ и ✗. Помечает показанное. Возвращает [{"text", "buttons", "batch"}].

    Длина считается так, как её считает Telegram, — в единицах UTF-16 (эмодзи занимает две):
    сообщение, не влезающее в предел, делится на несколько, а пункт, который не влезает даже
    один, обрезается. Пункт, уже уходивший `DIGEST_MAX_SENDS` раз, больше не отправляется.
    """
    # В сводку с кнопками — только то, где владелец сторона; «В команде» (team) сюда не попадает,
    # быстрое обещание — только после суток ожидания (Р-96).
    new = await conn.fetch(
        _SELECT + f""" AND c.status = 'proposed' AND c.direction <> 'others' AND {NOT_HELD}
                      AND c.digest_batch IS NULL AND c.digest_attempts < $2
                      ORDER BY (c.direction = 'owner_owes') DESC, c.due_date NULLS LAST, c.id LIMIT $1""",
        max_items, DIGEST_MAX_SENDS)
    waiting_changes = f"""FROM commitment_changes x JOIN commitments c ON c.id = x.commitment_id
           JOIN chats ch ON ch.id = c.chat_id JOIN messages m ON m.id = c.source_message_id
           WHERE x.status = 'proposed' AND x.digest_batch IS NULL AND x.digest_attempts < {DIGEST_MAX_SENDS}
             AND c.status = 'open' AND {VISIBLE}"""
    changes = await conn.fetch(
        f"SELECT x.* {waiting_changes} ORDER BY x.id LIMIT $1", max(0, max_items - len(new)))
    entries: list[tuple[str, int, str]] = [("c", row["id"], _render_commitment(to_dict(row, today), today, tz))
                                           for row in new]
    prints = {("c", row["id"]): fingerprint(row) for row in new}
    for row in changes:
        item = await get_commitment(conn, row["commitment_id"], today=today)
        if item is not None:
            entries.append(("x", row["id"], _render_change(row, item, today)))
            prints[("x", row["id"])] = item["approval_fingerprint"]
    if not entries:
        return []
    waiting = await conn.fetchval(
        f"""SELECT (SELECT count(*) FROM commitments c JOIN chats ch ON ch.id = c.chat_id
                    JOIN messages m ON m.id = c.source_message_id
                    WHERE c.status = 'proposed' AND c.direction <> 'others' AND {NOT_HELD}
                      AND c.digest_batch IS NULL
                      AND c.digest_attempts < {DIGEST_MAX_SENDS} AND {VISIBLE})
                 + (SELECT count(*) {waiting_changes})""") - len(entries)

    # раскладка по сообщениям: не больше per_message пунктов и не больше budget единиц UTF-16
    numbering = 8   # «N. » и пустая строка между пунктами
    chunks: list[list[tuple[str, int, str]]] = [[]]
    size = 0
    for kind, target, body in entries:
        body = bridge.fit_message(body, budget - numbering)
        cost = bridge.utf16_len(body) + numbering
        if chunks[-1] and (len(chunks[-1]) >= per_message or size + cost > budget):
            chunks.append([])
            size = 0
        chunks[-1].append((kind, target, body))
        size += cost

    out = []
    for n, chunk in enumerate(chunks, start=1):
        batch = f"{run_id}.{n}"
        lines, buttons = [], []
        for pos, (kind, target, body) in enumerate(chunk, start=1):
            lines.append(f"{pos}. {body}")
            accept_data, reject_data = (f"a:{target}", f"r:{target}") if kind == "c" else (f"ca:{target}", f"cr:{target}")
            accept_data += ":" + prints[(kind, target)][:24]
            # Подписи словами (Б6), данные кнопок прежние: старые сводки работают как раньше.
            yes, no = ("верно", "не так") if kind == "c" else ("применить", "оставить")
            buttons.append([bridge.button(f"{pos} — {yes}", CALLBACK_MODULE, accept_data),
                            bridge.button(f"{pos} — {no}", CALLBACK_MODULE, reject_data)])
            table = "commitments" if kind == "c" else "commitment_changes"
            await conn.execute(
                f"""UPDATE {table} SET digest_batch = $2, digest_pos = $3, notified_at = now(),
                           digest_attempts = digest_attempts + 1, digest_fingerprint = $4 WHERE id = $1""",
                target, batch, pos, prints[(kind, target)])
        head = "Договорённости из переписки: верно ли записано?"
        if len(chunks) > 1:
            head += f" ({n} из {len(chunks)})"
        text = head + "\n\n" + "\n\n".join(lines)
        if n == len(chunks) and waiting > 0:
            text += f"\n\nЕщё ждут решения: {waiting}. Придут в следующей сводке."
        out.append({"text": bridge.fit_message(text), "buttons": buttons, "batch": batch})
    return out


async def unmark_batch(conn: asyncpg.Connection, batch: str) -> dict[str, int]:
    """Сообщение-сводка не доставлено. Его нерешённые пункты снова ждут показа — но не бесконечно:
    пункт, уходивший уже `DIGEST_MAX_SENDS` раз, остаётся помеченным и больше не отправляется
    (он виден в выборке «ждут решения» и тихо устареет). Возвращает, сколько пунктов вернулось
    в очередь показа и сколько снято с отправки."""
    counts = {"requeued": 0, "dropped": 0}
    for table in ("commitments", "commitment_changes"):
        back = await conn.execute(
            f"""UPDATE {table} SET digest_batch = NULL, digest_pos = NULL, notified_at = NULL
                WHERE digest_batch = $1 AND status = 'proposed' AND digest_attempts < $2""",
            batch, DIGEST_MAX_SENDS)
        left = await conn.fetchval(
            f"SELECT count(*) FROM {table} WHERE digest_batch = $1 AND status = 'proposed'", batch)
        counts["requeued"] += int(back.split()[-1])
        counts["dropped"] += left
    return counts


async def batch_summary(conn: asyncpg.Connection, batch: str) -> tuple[bool, str]:
    """Состояние одного сообщения-сводки: все ли пункты решены и итоговый текст."""
    rows = await conn.fetch(
        f"""SELECT c.digest_pos AS pos, c.status, c.what, NULL::text AS kind, c.quiet_reason
            FROM commitments c JOIN chats ch ON ch.id = c.chat_id JOIN messages m ON m.id = c.source_message_id
            WHERE c.digest_batch = $1 AND {VISIBLE}
            UNION ALL
            SELECT x.digest_pos, x.status, c.what, x.kind, NULL FROM commitment_changes x
            JOIN commitments c ON c.id = x.commitment_id
            JOIN chats ch ON ch.id = c.chat_id JOIN messages m ON m.id = c.source_message_id
            WHERE x.digest_batch = $1 AND {VISIBLE}
            ORDER BY pos""", batch)
    lines = []
    for r in rows:
        prefix = f"{_CHANGE_TEXT[r['kind']].lower()}: " if r["kind"] else ""
        mark = _MARK.get(r["status"], r["status"])
        if r["kind"] is None and r["status"] in ("done", "cancelled", "open"):
            mark = "✓ принято"
        elif r["kind"] is None and r["quiet_reason"] in _QUIET_MARK:
            mark = _QUIET_MARK[r["quiet_reason"]]
        lines.append(f"{r['pos']}. {prefix}{_short(r['what'], 120)} — {mark}")
    done = bool(rows) and all(r["status"] != "proposed" for r in rows)
    return done, bridge.fit_message("Договорённости из переписки — решено:\n" + "\n".join(lines))


def why_buttons(target: int, *, pos: int | None = None, reject: bool = False) -> list[list[dict[str, str]]]:
    """Кнопки причины «Не так» (Р-96): по три в ряд. `reject` — нажатие сразу отклоняет с причиной
    (приложение, `rw:`); иначе — дописывает причину к уже отклонённому (бот, `w:`)."""
    code = "rw" if reject else "w"
    lead = f"{pos}: " if pos is not None else ""
    buttons = [bridge.button(lead + label, CALLBACK_MODULE, f"{code}:{target}:{key}")
               for key, label in WRONG_REASONS.items()]
    return [buttons[:3], buttons[3:]]


async def awaiting_owner(conn: asyncpg.Connection, *, today: date | None, limit: int = 100,
                         tz: tzinfo | str | None = None) -> dict[str, Any]:
    """Пункты сводок, которые уже ушли владельцу и ждут его решения, — для Mini App бота
    согласований (`setup_page/approvals.py`). Текст пункта — как в сводке, кнопки — те же данные,
    что под сводкой (с отпечатком того, что было показано), подписи словами вместо «N ✓».
    Решение приходит обратно в `handle_callback` — тем же путём, что нажатие в боте. У «Не так» —
    причины (`why`): приложение показывает их после нажатия «Не так» (Р-96).

    Новое предложение видно здесь сразу, ещё до сводки в боте (Р-96: сводка с кнопками уходит только
    в свои часы): отпечаток кнопки «Верно» — того, что показано сейчас. «В команде» и быстрые
    обещания, ждущие суток проверки, сюда не попадают. `later` — 0: ждать сводки больше нечему."""
    items: list[dict[str, Any]] = []
    rows = await conn.fetch(
        _SELECT + f""" AND c.status = 'proposed' AND c.direction <> 'others' AND {NOT_HELD}
                      ORDER BY c.notified_at NULLS LAST, c.id LIMIT $1""", limit)
    for row in rows:
        target = row["id"]
        shown = row["digest_fingerprint"] or fingerprint(row)
        items.append({
            "id": target, "title": "Верно ли записана договорённость?",
            "text": _render_commitment(to_dict(row, today), today, tz),
            "buttons": [[bridge.button(words.COMMITMENT_YES, CALLBACK_MODULE, f"a:{target}:{shown[:24]}"),
                         bridge.button(words.COMMITMENT_NO, CALLBACK_MODULE, f"r:{target}")]],
            "why": [b for line in why_buttons(target, reject=True) for b in line],
            "card_message_id": None})
    changes = await conn.fetch(
        f"""SELECT x.* FROM commitment_changes x JOIN commitments c ON c.id = x.commitment_id
            JOIN chats ch ON ch.id = c.chat_id JOIN messages m ON m.id = c.source_message_id
            WHERE x.status = 'proposed' AND x.digest_batch IS NOT NULL AND x.digest_fingerprint IS NOT NULL
              AND c.status = 'open' AND {VISIBLE} ORDER BY x.id LIMIT $1""", limit)
    for change in changes:
        item = await get_commitment(conn, change["commitment_id"], today=today)
        if item is None:
            continue
        target = change["id"]
        items.append({
            "id": target, "title": "Применить изменение?", "text": _render_change(change, item, today),
            "buttons": [[bridge.button("Применить", CALLBACK_MODULE, f"ca:{target}:{change['digest_fingerprint'][:24]}"),
                         bridge.button("Оставить как есть", CALLBACK_MODULE, f"cr:{target}")]],
            "card_message_id": None})
    return {"items": items, "later": 0}


async def batch_keyboard(conn: asyncpg.Connection, batch: str, *, asking: int | None = None
                         ) -> list[list[dict[str, str]]]:
    """Кнопки под сообщением-сводкой после нажатия: у нерешённых пунктов — прежние, у только что
    отклонённого (`asking`) — причины «Не так» (необязательно), у решённых — ничего."""
    rows = await conn.fetch(
        f"""SELECT 'c' AS kind, c.id, c.digest_pos AS pos, c.status, c.digest_fingerprint
            FROM commitments c JOIN chats ch ON ch.id = c.chat_id JOIN messages m ON m.id = c.source_message_id
            WHERE c.digest_batch = $1 AND {VISIBLE}
            UNION ALL
            SELECT 'x', x.id, x.digest_pos, x.status, x.digest_fingerprint FROM commitment_changes x
            JOIN commitments c ON c.id = x.commitment_id
            JOIN chats ch ON ch.id = c.chat_id JOIN messages m ON m.id = c.source_message_id
            WHERE x.digest_batch = $1 AND {VISIBLE}
            ORDER BY pos""", batch)
    keyboard: list[list[dict[str, str]]] = []
    for r in rows:
        pos, target = r["pos"], r["id"]
        if r["kind"] == "c" and target == asking and r["status"] == "rejected":
            keyboard += why_buttons(target, pos=pos)
        elif r["status"] == "proposed" and r["digest_fingerprint"]:
            if r["kind"] == "c":
                keyboard.append([bridge.button(f"{pos} — верно", CALLBACK_MODULE, f"a:{target}:{r['digest_fingerprint'][:24]}"),
                                 bridge.button(f"{pos} — не так", CALLBACK_MODULE, f"r:{target}")])
            else:
                keyboard.append([bridge.button(f"{pos} — применить", CALLBACK_MODULE, f"ca:{target}:{r['digest_fingerprint'][:24]}"),
                                 bridge.button(f"{pos} — оставить", CALLBACK_MODULE, f"cr:{target}")])
    return keyboard


async def add_reason(conn: asyncpg.Connection, commitment_id: int, reason: str, *, via: str) -> bool:
    """Причина «Не так» к договорённости, которую владелец только что отклонил или отметил неверной.
    Пишется в журнал один раз; ничего не меняет в самой договорённости. Только владелец."""
    if reason not in WRONG_REASONS or not authority.is_owner():
        return False
    last = await conn.fetchrow(
        """SELECT action FROM commitment_events WHERE commitment_id = $1 ORDER BY at DESC, id DESC LIMIT 1""",
        commitment_id)
    status = await conn.fetchval("SELECT status FROM commitments WHERE id = $1", commitment_id)
    if last is None or status != "rejected" or last["action"] not in ("rejected", "marked_wrong"):
        return False
    await log_event(conn, commitment_id, actor="owner", action="wrong_reason", from_status=status,
                    to_status=status, details={"reason": reason, "via": via})
    return True


async def hide(conn: asyncpg.Connection, commitment_id: int, *, details: dict[str, Any] | None = None,
               expected_fingerprint: str | None = None) -> dict[str, Any]:
    """«Скрыть» в списке «В команде» (Р-96): обещание между другими людьми уходит из списка и больше не
    предлагается (то же сообщение — `find_duplicate`). Только владелец; меняет только статус team."""
    if not authority.is_owner():
        return _result(False, error="Скрыть может только владелец.", code="approval_required")
    return await _move(conn, commitment_id, allowed=("team",), to="rejected", action="hidden", actor="owner",
                       details=details, expected_fingerprint=expected_fingerprint)


async def unhide(conn: asyncpg.Connection, commitment_id: int, *, today: date | None = None) -> dict[str, Any]:
    """«Вернуть» сразу после «Скрыть»: обещание снова в «В команде». Только владелец."""
    if not authority.is_owner():
        return _result(False, error="Вернуть может только владелец.", code="approval_required")
    return await _move(conn, commitment_id, allowed=("rejected",), to="team", action="unhidden", actor="owner",
                       details={"via": "app"}, today=today)


def _done_reply(answer: str, done: bool, text: str | None, keyboard: list[list[dict[str, str]]] | None
                ) -> dict[str, Any]:
    if done and not keyboard:
        return {"answer": answer, "edit_text": text, "remove_buttons": True}
    out: dict[str, Any] = {"answer": answer, "edit_text": None, "remove_buttons": False}
    if keyboard is not None:
        out["buttons"] = keyboard
    return out


async def handle_callback(conn: asyncpg.Connection, rest: str) -> dict[str, Any]:
    """Нажатие кнопки под сводкой. Данные: a:<id> принять, r:<id> отклонить, rw:<id>:<причина> отклонить
    с причиной (приложение), w:<id>:<причина> — причина к только что отклонённому (бот), ca:<id> применить
    изменение, cr:<id> оставить как есть."""
    action, _, raw = rest.partition(":")
    raw, _, shown_fingerprint = raw.partition(":")
    if action not in ("a", "r", "rw", "w", "ca", "cr") or not raw.isdigit():
        return {"answer": "Кнопка недоступна.", "edit_text": None, "remove_buttons": False}
    target = int(raw)
    asking = None
    if action == "w":
        batch = await conn.fetchval("SELECT digest_batch FROM commitments WHERE id = $1", target)
        if not await add_reason(conn, target, shown_fingerprint, via="bot"):
            return {"answer": "Причина уже записана или договорённость не отклонена.", "edit_text": None,
                    "remove_buttons": False}
        answer = "Спасибо, причина записана."
    elif action in ("a", "r", "rw"):
        row = await conn.fetchrow("SELECT * FROM commitments WHERE id = $1", target)
        if row is None or not await is_visible(conn, target):
            return {"answer": "Эта договорённость уже удалена.", "edit_text": None, "remove_buttons": False}
        if row["status"] != "proposed":
            answer = f"Уже решено: {STATUS_TEXT[row['status']]}."
        elif action == "a":
            # показанное — из сводки или (до сводки) из приложения: то, что было на карточке
            shown = row["digest_fingerprint"] or fingerprint(row)
            if not shown_fingerprint or shown_fingerprint != shown[:24]:
                return {"answer": "Карточка устарела. Запросите новое подтверждение.", "edit_text": None, "remove_buttons": True}
            outcome = await accept(conn, target, expected_fingerprint=shown)
            answer = "Принято." if outcome["ok"] else outcome["error"]
        elif action == "rw":
            if shown_fingerprint not in WRONG_REASONS:
                return {"answer": "Кнопка недоступна.", "edit_text": None, "remove_buttons": False}
            await reject(conn, target, details={"reason": shown_fingerprint})
            answer = f"Отклонено: {WRONG_REASONS[shown_fingerprint]}."
        else:
            await reject(conn, target)
            answer = "Отклонено. Почему — по желанию, кнопкой ниже."
            asking = target
        batch = row["digest_batch"]
    else:
        row = await conn.fetchrow("SELECT status, kind, digest_batch, digest_fingerprint FROM commitment_changes WHERE id = $1", target)
        if row is None:
            return {"answer": "Это предложение уже неактуально.", "edit_text": None, "remove_buttons": False}
        if row["status"] != "proposed":
            answer = "Уже решено."
        elif action == "ca":
            if not row["digest_fingerprint"] or shown_fingerprint != row["digest_fingerprint"][:24]:
                return {"answer": "Карточка устарела. Запросите новое подтверждение.", "edit_text": None, "remove_buttons": True}
            out = await apply_change(conn, target, expected_fingerprint=row["digest_fingerprint"])
            answer = {"fulfilled": "Закрыто.", "cancelled": "Отменено.", "rescheduled": "Срок перенесён."}.get(
                out.get("kind"), "Готово.") if out["ok"] else out["error"]
        else:
            await reject_change(conn, target)
            answer = "Оставлено как есть."
        batch = row["digest_batch"]
    if batch:
        done, text = await batch_summary(conn, batch)
        keyboard = await batch_keyboard(conn, batch, asking=asking)
        if asking is None and not done and action not in ("w", "r"):
            keyboard = None      # остальные кнопки под сводкой прежние — клавиатуру не трогаем
        return _done_reply(answer, done, text, keyboard)
    return {"answer": answer, "edit_text": None, "remove_buttons": False}
