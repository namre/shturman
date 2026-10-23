# Датированные факты с периодом действия (идея, не код): getzep/graphiti (Apache-2.0),
# graphiti_core/utils/maintenance/edge_operations.py@689de29 — у утверждения есть valid_at/invalid_at,
# противоречие закрывает старое датой, а не стирает его. Решение о смене здесь принимает код по
# ключу (slot), а не модель.
"""Датированные факты и решения: запись из ответа модели, смена по ключу, одобрение владельца.

Источник истины — таблица `facts` (docs/memory.md). Блоки facts и decisions на страницах и строки
хронологии f<id>, fz<id>, d<id> — производное от неё.

Кто о ком:
  * факт о человеке — пишется сразу (status active), если у человека есть страница (владелец его
    подтвердил); о неподтверждённом человеке факты не пишутся;
  * факт или решение о проекте — сразу active, если проект действует;
  * факт о самом владельце — proposed: профиль меняется только с одобрения владельца (кнопки ✓/✗
    в боте, модуль `of`, или экран «Память» на странице настройки).

Смена по ключу. У сменяемого факта есть slot (должность, телефон, цена…). Все действующие факты
одного субъекта с одним ключом выстраиваются по дате: у каждого valid_to — дата следующего,
superseded_by — следующий; открыт (valid_to IS NULL) только последний. Тот же текст, что у
открытого, — дубль и не пишется. Закрытый факт не стирается: он остаётся в хронологии.

Видимость. Факт не показывается никому, если сообщение-источник удалено, скрыто защитой от
внедрённых инструкций или его чат исключён. Удалённое и исключённое стирается (`purge_*`),
скрытое — нет: решение владельца его вернёт.

Функции для экрана «Память» (страница настройки, вызываются в контексте владельца):

  list_facts(conn, subject_type, subject_id=None, include_closed=False) -> list[dict]
      subject_type: person | project | owner; subject_id — people.id или projects.id (для owner
      не нужен). include_closed=False — только действующие сейчас; True — ещё и закрытые датой
      и отмеченные владельцем как неверные. Предложенные факты владельца здесь не возвращаются:
      они в `projects.pending_approvals`.
  get_fact(conn, fact_id) -> dict | None
  retract_fact(conn, fact_id) -> dict                       только владелец (authority)
  decide_owner_fact(conn, fact_id, accept, *, expected_fingerprint=None) -> dict
      accept=True — только владелец (authority); accept=False — отклонить можно и без него.

Всё, что возвращается, — словари, готовые к JSON. Поля из чужого текста перечислены в
`untrusted_fields` каждого словаря.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone
from typing import Any, Sequence
from zoneinfo import ZoneInfo

import asyncpg

from .. import authority, bridge
from . import extract

CALLBACK_MODULE = "of"
HANDLER_DIGEST = "facts.digest"
DIGEST_MAX_ITEMS = 10      # фактов о владельце в одном сообщении
DIGEST_MAX_SENDS = 3       # сколько раз пункт уходит владельцу, если сообщение не доставляется

SUBJECTS = ("person", "project", "owner")
UNTRUSTED_FIELDS = ["text", "slot", "source_quote", "chat.title", "said_by"]

STATUS_TEXT = {"proposed": "ждёт решения", "active": "действует", "rejected": "отклонён",
               "retracted": "отмечен как неверный"}

# Видимость. Псевдонимы: f — facts, fm — сообщение-источник, fc — его чат.
VISIBLE = "fm.deleted_at IS NULL AND fm.agent_visible AND NOT fc.excluded"
_FROM = """FROM facts f JOIN messages fm ON fm.id = f.source_message_id JOIN chats fc ON fc.id = fm.chat_id"""
_SELECT = f"""SELECT f.*, fm.sent_at AS source_sent_at, fm.chat_id, fm.is_outgoing AS source_outgoing,
                     fm.sender_name AS source_sender, fc.title AS chat_title,
                     EXISTS (SELECT 1 FROM person_peers sp WHERE sp.peer_id = fm.sender_peer_id
                             AND sp.person_id = f.person_id) AS source_by_subject
              {_FROM} WHERE {VISIBLE}"""


class FactsError(ValueError):
    """Действие с фактом невозможно; текст — для владельца, `code` — для ответа API."""

    def __init__(self, message: str, code: str = "bad_request") -> None:
        super().__init__(message)
        self.code = code


def text_norm(text: str | None) -> str:
    return extract.normalize(text or "")


_APPROVAL_FIELDS = ("subject_type", "person_id", "project_id", "kind", "slot", "text", "valid_from",
                    "source_message_id", "source_quote")


def fingerprint(row: Any) -> str:
    """Согласие относится к содержимому факта, а не к его рабочему состоянию."""
    body = {key: row[key] for key in _APPROVAL_FIELDS}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def approved(row: Any) -> bool:
    return bool(row["approved_at"] and row["approved_by"] and row["approved_via"]
                and row["approval_fingerprint"] == fingerprint(row))


def to_dict(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": row["id"], "subject_type": row["subject_type"], "person_id": row["person_id"],
        "project_id": row["project_id"], "kind": row["kind"], "slot": row["slot"], "text": row["text"],
        "valid_from": row["valid_from"].isoformat(),
        "valid_to": row["valid_to"].isoformat() if row["valid_to"] else None,
        "current": row["status"] == "active" and row["valid_to"] is None,
        "superseded_by": row["superseded_by"], "status": row["status"], "origin": row["origin"],
        # основание: owner — сказал владелец; self — сам человек; member — подтверждённый человек из чата
        # проекта; said — со слов другого, ничего не сменяет; approved — принял владелец; None — записано до 0.0.16
        "basis": row["basis"], "change_kind": row["change_kind"], "conflicts_with": row["conflicts_with"],
        "effective_from": row["effective_from"].isoformat() if row["effective_from"] else None,
        "applied_at": row["applied_at"].isoformat() if row["applied_at"] else None,
        "owner_approved": approved(row) if row["subject_type"] == "owner" else None,
        "approval_fingerprint": fingerprint(row),
        "source_quote": row["source_quote"],
        "source": {"message_id": row["source_message_id"], "ref": f"msg:{row['source_message_id']}",
                   "sent_at": row["source_sent_at"].isoformat()},
        "chat": {"id": row["chat_id"], "title": row["chat_title"]},
        # кто это сказал, если не владелец и не сам человек, о котором факт: имя автора (чужой текст)
        "said_by": None if row["source_outgoing"] or row["source_by_subject"] else (row["source_sender"] or "собеседник"),
        "created_at": row["created_at"].isoformat(),
        "decided_at": row["decided_at"].isoformat() if row["decided_at"] else None,
        "untrusted_fields": list(UNTRUSTED_FIELDS),
    }


def _subject_where(subject_type: str, subject_id: int | None, first_arg: int = 1) -> tuple[str, list[Any]]:
    if subject_type == "owner":
        return "f.subject_type = 'owner'", []
    column = "person_id" if subject_type == "person" else "project_id"
    return f"f.subject_type = '{subject_type}' AND f.{column} = ${first_arg}", [subject_id]


async def list_facts(
    conn: asyncpg.Connection, subject_type: str, subject_id: int | None = None, include_closed: bool = False,
) -> list[dict[str, Any]]:
    """Факты и решения одного субъекта, от старых к новым (см. описание модуля)."""
    if subject_type not in SUBJECTS:
        raise FactsError("Субъект факта: person, project или owner.")
    if subject_type != "owner":
        if not isinstance(subject_id, int) or isinstance(subject_id, bool):
            raise FactsError("Нужен идентификатор человека или проекта.")
        if subject_type == "person":
            from . import people
            subject_id = await people.active_id(conn, subject_id)
            if subject_id is None:
                return []
    where, args = _subject_where(subject_type, subject_id)
    status = ("f.status IN ('active', 'retracted')" if include_closed
              else "f.status = 'active' AND f.valid_to IS NULL")
    rows = await conn.fetch(
        f"{_SELECT} AND {where} AND {status} ORDER BY f.valid_from, fm.sent_at, f.id", *args)
    return [to_dict(r) for r in rows]


async def get_fact(conn: asyncpg.Connection, fact_id: int) -> dict[str, Any] | None:
    row = await conn.fetchrow(f"{_SELECT} AND f.id = $1", fact_id)
    return to_dict(row) if row is not None else None


# --- страницы, на которых виден факт ----------------------------------------------------------------

async def touch_pages(conn: asyncpg.Connection, subject_type: str, person_id: int | None,
                      project_id: int | None) -> None:
    """Страница субъекта ждёт перерисовки: блок facts и хронология изменились."""
    if subject_type == "person":
        await conn.execute("UPDATE pages SET dirty = true WHERE person_id = $1", person_id)
    elif subject_type == "project":
        await conn.execute("UPDATE pages SET dirty = true WHERE project_id = $1", project_id)
    else:
        from . import pages_build
        if await conn.fetchval("SELECT 1 FROM facts WHERE subject_type = 'owner' AND status = 'active' LIMIT 1"):
            await pages_build.ensure_owner_page(conn)
        await conn.execute("UPDATE pages SET dirty = true WHERE entity_type = 'owner'")
    from . import pages_build
    pages_build.wake()


# --- смена по ключу ------------------------------------------------------------------------------------

async def rechain(conn: asyncpg.Connection, subject_type: str, person_id: int | None,
                  project_id: int | None, slot: str | None) -> None:
    """Выстраивает действующие факты одного ключа по дате: каждый закрыт датой следующего,
    открыт только последний (см. описание модуля)."""
    if slot is None:
        return
    rows = await conn.fetch(
        """SELECT f.id, f.valid_from, f.valid_to, f.superseded_by, m.sent_at FROM facts f
           JOIN messages m ON m.id = f.source_message_id
           WHERE f.subject_type = $1 AND f.person_id IS NOT DISTINCT FROM $2
             AND f.project_id IS NOT DISTINCT FROM $3 AND f.slot = $4 AND f.status = 'active' AND f.chained
           ORDER BY f.valid_from, m.sent_at, f.id FOR UPDATE OF f""",
        subject_type, person_id, project_id, slot)
    for n, row in enumerate(rows):
        following = rows[n + 1] if n + 1 < len(rows) else None
        valid_to = following["valid_from"] if following else None
        superseded_by = following["id"] if following else None
        if (row["valid_to"], row["superseded_by"]) != (valid_to, superseded_by):
            await conn.execute("UPDATE facts SET valid_to = $2, superseded_by = $3 WHERE id = $1",
                               row["id"], valid_to, superseded_by)


async def _duplicate(conn: asyncpg.Connection, subject_type: str, person_id: int | None,
                     project_id: int | None, kind: str, slot: str | None, norm: str, message_id: int) -> bool:
    """Тот же факт уже есть: тот же текст в действующих сейчас, в ждущих, отклонённых или
    отмеченных неверными, либо тот же текст из того же сообщения."""
    return bool(await conn.fetchval(
        """SELECT 1 FROM facts f
           WHERE f.subject_type = $1 AND f.person_id IS NOT DISTINCT FROM $2
             AND f.project_id IS NOT DISTINCT FROM $3 AND f.kind = $4 AND f.text_norm = $5
             AND (f.source_message_id = $6 OR f.status IN ('proposed', 'rejected', 'retracted')
                  OR (f.status = 'active' AND f.valid_to IS NULL AND f.slot IS NOT DISTINCT FROM $7))
           LIMIT 1""",
        subject_type, person_id, project_id, kind, norm, message_id, slot))


async def _said_by(conn: asyncpg.Connection, sender_peer_id: int | None, person_id: int | None) -> bool:
    """Автор сообщения — сам этот человек (одна из его учётных записей Telegram)."""
    if sender_peer_id is None or person_id is None:
        return False
    return bool(await conn.fetchval(
        "SELECT 1 FROM person_peers WHERE peer_id = $1 AND person_id = $2", sender_peer_id, person_id))


# Правила доверия (записка «держать, подкладывать, сигналить», шаг 1; docs/memory.md, «Кто меняет
# значения проекта»). Значимое значение проекта меняет только владелец; сказанное другими — предложение.
PROTECTED_PROJECT_SLOTS = frozenset({"реквизиты", "бюджет", "цена", "подрядчик", "ответственный", "срок"})
# Тип поля неясен (ключа нет), но по тексту это деньги, счёт, подрядчик или срок — тоже значимое.
_PROTECTED_TEXT_RE = re.compile(
    r"(?:\d[\d\s.,]*\s*(?:₽|руб|р\.|тыс|млн|млрд|k\b|к\b|\$|€|usd|eur|%)"
    r"|р/с|к/с|бик|инн|расч[её]тн|реквизит|сч[её]т\b|сч[её]та\b|оплат|аванс|предоплат"
    r"|подрядчик|субподряд|исполнител|ответственн|срок|дедлайн|перенес|перенос|сдач[аиу]|бюджет|смет|цен[аеуы]|стоимост"
    r"|\d{5,})",
    re.IGNORECASE)
BASES = ("owner", "self", "member", "said", "approved")


def looks_protected(text: str) -> bool:
    """По формулировке — значимое значение проекта, даже если модель не поставила ключ. Цитата здесь не
    смотрится: в одном сообщении бывает и решение о фасаде, и цена, а записывается формулировка."""
    return bool(_PROTECTED_TEXT_RE.search(text or ""))


async def _project_member(conn: asyncpg.Connection, message: extract.Msg, project_id: int | None) -> bool:
    """Автор — подтверждённый владельцем человек, и пишет он в одном из чатов этого проекта."""
    if project_id is None or message.sender_peer_id is None:
        return False
    return bool(await conn.fetchval(
        """SELECT 1 FROM person_peers pp JOIN people p ON p.id = pp.person_id
           WHERE pp.peer_id = $1 AND p.confirmed AND NOT p.is_owner AND p.merged_into IS NULL
             AND EXISTS (SELECT 1 FROM project_chats pc WHERE pc.project_id = $2 AND pc.chat_id = $3)""",
        message.sender_peer_id, project_id, message.chat_id))


async def _current_value(conn: asyncpg.Connection, project_id: int, slot: str | None, norm: str) -> int | None:
    """Действующее значение того же ключа проекта с другим текстом — с ним новое расходится."""
    if slot is None:
        return None
    return await conn.fetchval(
        f"""SELECT f.id {_FROM} WHERE {VISIBLE} AND f.subject_type = 'project' AND f.project_id = $1
              AND f.slot = $2 AND f.status = 'active' AND f.valid_to IS NULL AND f.text_norm <> $3
            ORDER BY f.valid_from DESC, f.id DESC LIMIT 1""",
        project_id, slot, norm)


async def record(
    conn: asyncpg.Connection, candidate: extract.FactCandidate, *, subject_type: str,
    person_id: int | None = None, project_id: int | None = None, tz: str = "UTC",
    run_id: int | None = None, model: str | None = None,
) -> tuple[int | None, str]:
    """Записывает проверенный факт. Возвращает (id, итог): duplicate — уже есть; active — записан
    и действует (или закрыт более новым); proposed — ждёт решения владельца (факт о нём или
    значимое значение проекта со слов другого); not_owner_message, third_party_contact — не записан.

    Кто может что сказать (решает код по автору сообщения, а не модель; основание — в `basis`):
      * факт о владельце — только из его собственного (исходящего) сообщения, и только с его «да»;
      * сменяемый факт о человеке (с ключом) сменяет прежний, только если его сказал сам этот
        человек или владелец. Со слов третьего лица контактные данные (телефон, почта, адрес,
        реквизиты) не пишутся вовсе, остальное пишется как факт без ключа — ничего не сменяет;
      * значимое значение проекта (реквизиты, бюджет, цена, подрядчик, ответственный, срок — по ключу
        или по тексту) действует, только если его сказал владелец; со слов других — предложение
        (proposed), при расхождении с действующим значением — с пометкой противоречия;
      * прочее о проекте действует со слов владельца или подтверждённого человека из чата проекта;
        со слов остальных — пишется без ключа («со слов …»), ничего не сменяет;
      * значения проекта сами по ключу друг друга не сменяют (chained = false): у проекта несколько
        работ, и ключ без уточнения путал бы их. Сменяет только «Принять» владельца."""
    norm = text_norm(candidate.text)
    if not norm:
        return None, "empty"
    message = candidate.message
    kind = candidate.kind if subject_type == "project" else "fact"
    slot = None if kind == "decision" else candidate.slot
    chained = True
    conflicts_with = None
    if subject_type == "owner":
        if not message.is_outgoing:
            return None, "not_owner_message"
        status, basis = "proposed", None
    elif subject_type == "person":
        status = "active"
        if message.is_outgoing:
            basis = "owner"
        elif await _said_by(conn, message.sender_peer_id, person_id):
            basis = "self"
        else:
            if slot in extract.CONTACT_SLOTS:
                return None, "third_party_contact"
            slot, basis = None, "said"
    else:
        chained = False
        protected = slot in PROTECTED_PROJECT_SLOTS or looks_protected(candidate.text)
        if message.is_outgoing:
            status, basis = "active", "owner"
        elif protected:
            status, basis = "proposed", None
        elif await _project_member(conn, message, project_id):
            status, basis = "active", "member"
        else:
            status, basis, slot = "active", "said", None
    if await _duplicate(conn, subject_type, person_id, project_id, kind, slot, norm, message.id):
        return None, "duplicate"
    if subject_type == "project" and status == "proposed":
        conflicts_with = await _current_value(conn, project_id, slot, norm)
    valid_from = message.sent_at.astimezone(ZoneInfo(tz)).date()
    fact_id = await conn.fetchval(
        """INSERT INTO facts (subject_type, person_id, project_id, kind, slot, text, text_norm, valid_from,
                              status, origin, source_message_id, source_quote, run_id, model,
                              basis, chained, conflicts_with, applied_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17,
                   CASE WHEN $9 = 'active' THEN now() END) RETURNING id""",
        subject_type, person_id, project_id, kind, slot, candidate.text, norm, valid_from, status,
        candidate.origin, message.id, candidate.quote[: extract.QUOTE_LIMIT], run_id,
        (model or "")[:120] or None, basis, chained, conflicts_with)
    if status == "active":
        await rechain(conn, subject_type, person_id, project_id, slot)
        await touch_pages(conn, subject_type, person_id, project_id)
    return fact_id, status


# --- решения владельца --------------------------------------------------------------------------------

async def _locked(conn: asyncpg.Connection, fact_id: int) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"""SELECT f.*, fm.is_outgoing AS source_outgoing {_FROM} WHERE f.id = $1 AND {VISIBLE}
            FOR UPDATE OF f""", fact_id)


async def retract_fact(conn: asyncpg.Connection, fact_id: int) -> dict[str, Any]:
    """Владелец отметил факт как неверный. Факт перестаёт действовать; если он сменил прежний,
    прежний снова действует. Только владелец (authority)."""
    authority.requires_owner()
    async with conn.transaction():
        row = await _locked(conn, fact_id)
        if row is None:
            raise FactsError("Такого факта нет.", "not_found")
        if row["status"] == "retracted":
            return {"ok": True, "changed": False, "fact": await get_fact(conn, fact_id)}
        if row["status"] != "active":
            raise FactsError(f"Нельзя: факт {STATUS_TEXT[row['status']]}.", "bad_status")
        await conn.execute(
            "UPDATE facts SET status = 'retracted', decided_at = now(), superseded_by = NULL WHERE id = $1",
            fact_id)
        await _reopen_replaced(conn, fact_id)
        await rechain(conn, row["subject_type"], row["person_id"], row["project_id"], row["slot"])
        await touch_pages(conn, row["subject_type"], row["person_id"], row["project_id"])
    return {"ok": True, "changed": True, "fact": await get_fact(conn, fact_id)}


async def _reopen_replaced(conn: asyncpg.Connection, fact_id: int | None = None) -> None:
    """Значение проекта, которое сменило «Принять» владельца (chained = false), снова действует,
    если сменивший факт отмечен неверным или удалён. Без fact_id — все такие осиротевшие."""
    if fact_id is not None:
        await conn.execute(
            """UPDATE facts SET valid_to = NULL, superseded_by = NULL
               WHERE superseded_by = $1 AND NOT chained AND status = 'active'""", fact_id)
    else:
        await conn.execute(
            """UPDATE facts SET valid_to = NULL
               WHERE NOT chained AND status = 'active' AND valid_to IS NOT NULL AND superseded_by IS NULL""")


async def decide_owner_fact(
    conn: asyncpg.Connection, fact_id: int, accept: bool, *, expected_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Решение владельца о предложенном факте: о нём самом или о значимом значении проекта со слов
    другого. Согласие — только по проверенному владельцу (authority) и только для того содержания,
    которое ему показали. Принятое значение проекта сменяет то, с которым расходилось."""
    principal = authority.requires_owner() if accept else authority.get_owner_principal()
    async with conn.transaction():
        row = await _locked(conn, fact_id)
        if row is None or row["subject_type"] not in ("owner", "project"):
            raise FactsError("Такого предложения нет.", "not_found")
        if row["subject_type"] == "project":
            return await _decide_project(conn, row, accept, principal, expected_fingerprint)
        if expected_fingerprint is not None and fingerprint(row) != expected_fingerprint:
            raise FactsError("Содержание изменилось. Подтвердите новое предложение.", "changed_meanwhile")
        if row["status"] != "proposed":
            return {"ok": True, "changed": False, "status": row["status"], "fact": await get_fact(conn, fact_id)}
        if accept and not row["source_outgoing"]:
            # профиль — только то, что владелец сказал о себе сам (предложения прежних версий)
            raise FactsError("Факт о вас принимается только из ваших собственных сообщений.", "not_owner_message")
        if accept:
            await conn.execute(
                """UPDATE facts SET status = 'active', decided_at = now(), approved_at = now(), applied_at = now(),
                          basis = 'approved', approved_by = $2, approved_via = $3, approval_fingerprint = $4
                   WHERE id = $1""",
                fact_id, str(authority.current_owner_id()), principal.source, fingerprint(row))
            await rechain(conn, "owner", None, None, row["slot"])
            await touch_pages(conn, "owner", None, None)
        else:
            await conn.execute("UPDATE facts SET status = 'rejected', decided_at = now() WHERE id = $1", fact_id)
    status = "active" if accept else "rejected"
    return {"ok": True, "changed": True, "status": status, "fact": await get_fact(conn, fact_id)}


async def _decide_project(conn: asyncpg.Connection, row: asyncpg.Record, accept: bool,
                         principal: Any, expected_fingerprint: str | None) -> dict[str, Any]:
    fact_id = row["id"]
    if expected_fingerprint is not None and fingerprint(row) != expected_fingerprint:
        raise FactsError("Содержание изменилось. Подтвердите новое предложение.", "changed_meanwhile")
    if row["status"] != "proposed":
        return {"ok": True, "changed": False, "status": row["status"], "fact": await get_fact(conn, fact_id)}
    if not accept:
        await conn.execute("UPDATE facts SET status = 'rejected', decided_at = now() WHERE id = $1", fact_id)
        return {"ok": True, "changed": True, "status": "rejected", "fact": await get_fact(conn, fact_id)}
    await conn.execute(
        """UPDATE facts SET status = 'active', decided_at = now(), approved_at = now(), applied_at = now(),
                  basis = 'approved', approved_by = $2, approved_via = $3, approval_fingerprint = $4
           WHERE id = $1""",
        fact_id, str(authority.current_owner_id()), principal.source, fingerprint(row))
    if row["conflicts_with"] is not None:
        # «Принять» — это и есть решение сменить прежнее значение: прежнее закрывается датой нового
        await conn.execute(
            """UPDATE facts SET valid_to = GREATEST(valid_from, $2::date), superseded_by = $3
               WHERE id = $1 AND status = 'active' AND valid_to IS NULL""",
            row["conflicts_with"], row["valid_from"], fact_id)
    await touch_pages(conn, "project", None, row["project_id"])
    return {"ok": True, "changed": True, "status": "active", "fact": await get_fact(conn, fact_id)}


# --- удаление источников --------------------------------------------------------------------------------

async def _release(conn: asyncpg.Connection, doomed: str, *args: Any) -> int:
    """Удаляет факты по условию и заново выстраивает затронутые ключи: факт, который сменил
    удаляемый, снова действует. Страницы ждут перерисовки."""
    rows = await conn.fetch(f"DELETE FROM facts f WHERE {doomed} "
                            "RETURNING subject_type, person_id, project_id, slot", *args)
    if rows:
        await _reopen_replaced(conn)
    seen = set()
    for r in rows:
        key = (r["subject_type"], r["person_id"], r["project_id"], r["slot"])
        if key not in seen:
            seen.add(key)
            await rechain(conn, *key)
            await touch_pages(conn, r["subject_type"], r["person_id"], r["project_id"])
    return len(rows)


async def purge_for_messages(conn: asyncpg.Connection, message_ids: Sequence[int]) -> int:
    """Сообщения удалены: убираем выведенные из них факты и решения (docs/memory.md, «Удаление»)."""
    if not message_ids:
        return 0
    return await _release(conn, "f.source_message_id = ANY($1::bigint[])", list(message_ids))


async def purge_orphans(conn: asyncpg.Connection) -> int:
    """Обход на случай пропущенного события: источник удалён или его чат исключён."""
    gone = await _release(
        conn, """EXISTS (SELECT 1 FROM messages m JOIN chats c ON c.id = m.chat_id
                         WHERE m.id = f.source_message_id AND (m.deleted_at IS NOT NULL OR c.excluded))""")
    await repair_chains(conn)
    return gone


async def repair_chains(conn: asyncpg.Connection) -> int:
    """Чинит цепочки, которые порвало стирание сообщений целиком (стирание чата, удаление аккаунта
    из архива, служебный диалог): факт, сменивший прежний, ушёл каскадом вместе с сообщением,
    а прежний остался закрытым датой без преемника (superseded_by обнулён внешним ключом). Такие
    ключи выстраиваются заново: последний действующий факт снова открыт. Возвращает число ключей."""
    await _reopen_replaced(conn)
    rows = await conn.fetch(
        """SELECT DISTINCT subject_type, person_id, project_id, slot FROM facts
           WHERE status = 'active' AND chained AND slot IS NOT NULL AND valid_to IS NOT NULL
             AND superseded_by IS NULL""")
    for r in rows:
        await rechain(conn, r["subject_type"], r["person_id"], r["project_id"], r["slot"])
        await touch_pages(conn, r["subject_type"], r["person_id"], r["project_id"])
    return len(rows)


# --- сообщение владельцу: факты о нём ---------------------------------------------------------------------

def _short(text: str | None, limit: int) -> str:
    text = " ".join(extract.clean_text(text or "", 2000).replace("⏎", " ").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def said(row: Any) -> str:
    """Кто это сказал — для карточек владельцу: факт о нём берётся только из его сообщений."""
    return "сказали вы" if row["source_outgoing"] else "сказал собеседник"


def _item_text(row: asyncpg.Record) -> str:
    slot = f"{row['slot']}: " if row["slot"] else ""
    return (f"{slot}{_short(row['text'], 200)} (с {row['valid_from']:%d.%m.%Y}; {said(row)})\n"
            f"«{_short(row['source_quote'], 160)}»")


async def send_owner_digest(conn: asyncpg.Connection, *, run_id: int | str,
                            max_items: int = DIGEST_MAX_ITEMS) -> int:
    """Одно сообщение владельцу со списком предложенных фактов о нём и кнопками ✓/✗ под каждым.
    Возвращает число показанных пунктов. Не поместившиеся ждут следующего прогона."""
    rows = await conn.fetch(
        f"""{_SELECT} AND f.subject_type = 'owner' AND f.status = 'proposed' AND f.batch IS NULL
            AND f.digest_attempts < $2 ORDER BY f.id LIMIT $1""", max_items, DIGEST_MAX_SENDS)
    if not rows:
        return 0
    batch = f"of{run_id}"
    lines, buttons = [], []
    budget = bridge.MESSAGE_LIMIT - 400
    for pos, row in enumerate(rows, start=1):
        body = bridge.fit_message(_item_text(row), budget // max(1, len(rows)))
        lines.append(f"{pos}. {body}")
        print_ = fingerprint(row)
        buttons.append([bridge.button(f"{pos} — запомнить", CALLBACK_MODULE, f"a:{row['id']}:{print_[:24]}"),
                        bridge.button(f"{pos} — не запоминать", CALLBACK_MODULE, f"r:{row['id']}")])
        await conn.execute(
            """UPDATE facts SET batch = $2, pos = $3, notified_at = now(), digest_attempts = digest_attempts + 1,
                      digest_fingerprint = $4 WHERE id = $1""",
            row["id"], batch, pos, print_)
    waiting = await conn.fetchval(
        f"""SELECT count(*) {_FROM} WHERE {VISIBLE} AND f.subject_type = 'owner' AND f.status = 'proposed'
            AND f.batch IS NULL AND f.digest_attempts < $1""", DIGEST_MAX_SENDS)
    text = ("Профиль: запомнить это о вас? Под пунктами — кнопки «запомнить» и «не запоминать». "
            "Без вашего «да» в профиль ничего не попадает.\n\n" + "\n\n".join(lines))
    if waiting:
        text += f"\n\nЕщё ждут решения: {waiting}. Придут со следующим прогоном."
    await bridge.notify_owner(conn, bridge.fit_message(text), buttons=buttons, handler=HANDLER_DIGEST,
                              context={"batch": batch}, dedup_key=f"of-digest:{batch}")
    return len(rows)


_PROJECT_SELECT = f"""SELECT f.*, fm.sent_at AS source_sent_at, fm.chat_id, fm.is_outgoing AS source_outgoing,
       fm.sender_name AS source_sender, fc.title AS chat_title, pr.title AS project_title,
       cf.text AS current_text, cf.slot AS current_slot
  {_FROM} JOIN projects pr ON pr.id = f.project_id
  LEFT JOIN facts cf ON cf.id = f.conflicts_with AND cf.status = 'active' AND cf.valid_to IS NULL
  WHERE {VISIBLE} AND f.subject_type = 'project' AND f.status = 'proposed' AND pr.status = 'active'"""


def _project_item_text(row: asyncpg.Record) -> str:
    slot = f"{row['slot']}: " if row["slot"] else ""
    who = _short(row["source_sender"], 60) or "собеседник"
    text = (f"«{_short(row['project_title'], 60)}» — {slot}{_short(row['text'], 200)}\n"
            f"Сказал {who}, {row['valid_from']:%d.%m.%Y}: «{_short(row['source_quote'], 160)}»")
    if row["current_text"]:
        text += f"\nСейчас действует: {_short(row['current_text'], 160)}"
    return text


async def send_project_digest(conn: asyncpg.Connection, *, run_id: int | str,
                              max_items: int = DIGEST_MAX_ITEMS) -> int:
    """Одно сообщение владельцу: значимые значения проектов (реквизиты, бюджет, цена, подрядчик,
    ответственный, срок) со слов других, с кнопками «Принять» и «Оставить прежнее». Без его «да»
    действующее значение не меняется. Возвращает число показанных пунктов."""
    rows = await conn.fetch(
        f"""{_PROJECT_SELECT} AND f.batch IS NULL AND f.digest_attempts < $2
            ORDER BY f.id LIMIT $1""", max_items, DIGEST_MAX_SENDS)
    if not rows:
        return 0
    batch = f"pf{run_id}"
    lines, buttons = [], []
    budget = bridge.MESSAGE_LIMIT - 400
    for pos, row in enumerate(rows, start=1):
        lines.append(f"{pos}. {bridge.fit_message(_project_item_text(row), budget // max(1, len(rows)))}")
        print_ = fingerprint(row)
        buttons.append([bridge.button(f"{pos} — принять", CALLBACK_MODULE, f"a:{row['id']}:{print_[:24]}"),
                        bridge.button(f"{pos} — оставить прежнее", CALLBACK_MODULE, f"r:{row['id']}")])
        await conn.execute(
            """UPDATE facts SET batch = $2, pos = $3, notified_at = now(), digest_attempts = digest_attempts + 1,
                      digest_fingerprint = $4 WHERE id = $1""",
            row["id"], batch, pos, print_)
    text = ("Проекты: это сказали другие, а меняете значения вы. Принять — значит, теперь действует это. "
            "Оставить прежнее — ничего не меняется.\n\n" + "\n\n".join(lines))
    await bridge.notify_owner(conn, bridge.fit_message(text), buttons=buttons, handler=HANDLER_DIGEST,
                              context={"batch": batch}, dedup_key=f"pf-digest:{batch}")
    return len(rows)


async def unmark_batch(conn: asyncpg.Connection, batch: str) -> int:
    """Сообщение не доставлено: нерешённые пункты снова ждут показа (не больше DIGEST_MAX_SENDS раз)."""
    done = await conn.execute(
        """UPDATE facts SET batch = NULL, pos = NULL, notified_at = NULL
           WHERE batch = $1 AND status = 'proposed' AND digest_attempts < $2""", batch, DIGEST_MAX_SENDS)
    return int(done.split()[-1])


_MARK = {"active": "✓ запомнено", "rejected": "✗ не запоминать", "proposed": "… ждёт",
         "retracted": "✗ неверно"}
_PROJECT_MARK = {"active": "✓ принято", "rejected": "✗ оставлено прежнее", "proposed": "… ждёт",
                 "retracted": "✗ неверно"}


async def batch_summary(conn: asyncpg.Connection, batch: str) -> tuple[bool, str]:
    rows = await conn.fetch(f"{_SELECT} AND f.batch = $1 ORDER BY f.pos", batch)
    marks, head = (_PROJECT_MARK, "Проекты — решено:") if batch.startswith("pf") else (_MARK, "Профиль — решено:")
    lines = [f"{r['pos']}. {_short(r['text'], 120)} — {marks.get(r['status'], r['status'])}" for r in rows]
    done = bool(rows) and all(r["status"] != "proposed" for r in rows)
    return done, bridge.fit_message(head + "\n" + "\n".join(lines))


@bridge.on_result(HANDLER_DIGEST)
async def on_digest_sent(conn: asyncpg.Connection, job: dict[str, Any], result: dict[str, Any]) -> None:
    await conn.execute("UPDATE jobs SET payload = '{}'::jsonb, result = NULL WHERE id = $1", job["id"])


@bridge.on_failure(HANDLER_DIGEST)
async def on_digest_failed(conn: asyncpg.Connection, job: dict[str, Any], error: str) -> None:
    await conn.execute("UPDATE jobs SET payload = '{}'::jsonb, result = NULL WHERE id = $1", job["id"])
    batch = (job.get("context") or {}).get("batch")
    if isinstance(batch, str):
        await unmark_batch(conn, batch)


async def awaiting_owner(conn: asyncpg.Connection, *, limit: int = 100) -> dict[str, Any]:
    """Факты о владельце из уже отправленного сообщения, которые ждут его «да» или «нет», — для Mini
    App бота согласований (`setup_page/approvals.py`). Текст — как в сообщении бота, кнопки — те же
    данные (с отпечатком показанного), подписи словами. Решение приходит обратно в `on_button`.
    Ещё не показанные только считаются (`later`): у них нет отпечатка показанного."""
    rows = await conn.fetch(
        f"""{_SELECT} AND f.subject_type = 'owner' AND f.status = 'proposed' AND f.batch IS NOT NULL
            AND f.digest_fingerprint IS NOT NULL ORDER BY f.notified_at NULLS LAST, f.id LIMIT $1""", limit)
    items = [{"id": r["id"], "title": "Запомнить это о вас?", "text": _item_text(r),
              "buttons": [[bridge.button("Запомнить", CALLBACK_MODULE, f"a:{r['id']}:{r['digest_fingerprint'][:24]}"),
                           bridge.button("Не запоминать", CALLBACK_MODULE, f"r:{r['id']}")]],
              "card_message_id": None} for r in rows]
    projects_rows = await conn.fetch(
        f"""{_PROJECT_SELECT} AND f.batch IS NOT NULL AND f.digest_fingerprint IS NOT NULL
            ORDER BY f.notified_at NULLS LAST, f.id LIMIT $1""", limit)
    items += [{"id": r["id"], "title": "Значение проекта со слов другого", "text": _project_item_text(r),
               "buttons": [[bridge.button("Принять", CALLBACK_MODULE, f"a:{r['id']}:{r['digest_fingerprint'][:24]}"),
                            bridge.button("Оставить прежнее", CALLBACK_MODULE, f"r:{r['id']}")]],
               "card_message_id": None} for r in projects_rows]
    later = await conn.fetchval(
        f"""SELECT count(*) {_FROM} WHERE {VISIBLE} AND f.subject_type IN ('owner', 'project')
            AND f.status = 'proposed' AND f.batch IS NULL AND f.digest_attempts < $1""", DIGEST_MAX_SENDS)
    return {"items": items, "later": int(later or 0)}


@bridge.on_callback(CALLBACK_MODULE)
async def on_button(conn: asyncpg.Connection, rest: str, user_id: int) -> dict[str, Any]:
    """Нажатие под сообщением с фактами о владельце: a:<id>:<отпечаток> — запомнить, r:<id> — нет."""
    refused = {"answer": "Кнопка недоступна.", "edit_text": None, "remove_buttons": False}
    action, _, tail = rest.partition(":")
    raw, _, shown = tail.partition(":")
    if action not in ("a", "r") or not raw.isdigit() or len(raw) > 18:
        return refused
    fact_id = int(raw)
    row = await conn.fetchrow(
        f"SELECT f.status, f.batch, f.digest_fingerprint, f.subject_type {_FROM} WHERE f.id = $1 "
        f"AND f.subject_type IN ('owner', 'project') AND {VISIBLE}", fact_id)
    if row is None:
        return {"answer": "Это предложение уже удалено.", "edit_text": None, "remove_buttons": False}
    if row["status"] != "proposed":
        answer = f"Уже решено: {STATUS_TEXT[row['status']]}."
    elif action == "a":
        if not row["digest_fingerprint"] or shown != row["digest_fingerprint"][:24]:
            return {"answer": "Карточка устарела. Дождитесь нового сообщения.", "edit_text": None,
                    "remove_buttons": True}
        project = row["subject_type"] == "project"
        try:
            await decide_owner_fact(conn, fact_id, True, expected_fingerprint=row["digest_fingerprint"])
            answer = "Принято: теперь действует это." if project else "Запомнено."
        except FactsError as exc:
            answer = str(exc)
    else:
        await decide_owner_fact(conn, fact_id, False)
        answer = "Оставлено прежнее." if row["subject_type"] == "project" else "Не запоминаю."
    if row["batch"]:
        done, text = await batch_summary(conn, row["batch"])
        if done:
            return {"answer": answer, "edit_text": text, "remove_buttons": True}
    return {"answer": answer, "edit_text": None, "remove_buttons": False}


async def scrub_closed_jobs(conn: asyncpg.Connection) -> int:
    done = await conn.execute(
        """UPDATE jobs SET payload = '{}'::jsonb, result = NULL
           WHERE handler = $1 AND status IN ('done', 'failed') AND payload <> '{}'::jsonb""", HANDLER_DIGEST)
    return int(done.split()[-1])


def today(tz: str, now: datetime | None = None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz)).date()
