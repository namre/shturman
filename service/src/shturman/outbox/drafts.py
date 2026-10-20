"""Черновики и отправка: карточка владельцу, нажатие кнопки, сама отправка, уборка зависшего.

Путь черновика:

    pending ──нажатие «Отправить»──► approved ──отправщик──► sending ──► sent
       │                                 │                      ├──────► failed
       ├─► rejected  (нажатие «Отклонить» или отмена)           └──────► outcome_unknown
       ├─► expired   (владелец не ответил в срок)               (ответ не пришёл; повтора нет,
       ├─► superseded (для чата подготовлен новый)               позднее подтверждение → sent)
       └─► failed    (карточка не дошла до владельца)
                                         └─► failed (перед отправкой правила уже не разрешают)

Главный выключатель (`config.sending`, задаётся только окружением сервиса): пока он выключен,
отправка недоступна. После завершения карточка черновика заменяется итогом, кнопки под ней убираются.

Откуда черновик (`origin`):
  * agent    — подготовил ассистент (Hermes) через внутренний API (`create`); карточка «Черновик № N»;
  * suggest  — «Черновик ответа» самого сервиса на входящее в личном чате основного аккаунта (Р-105,
               `create_suggestion`, карточку собирает `rehearsal/core.py`); через API не создаётся;
  * autoreply — автоответ доверенному: рождается согласованным.
Какие кнопки под карточкой — решает одна функция (`replies/delivery.py`), и в боте, и в приложении (Д-46).

Автоответ доверенным рождается сразу в `approved` (правило включил владелец) и дальше идёт
тем же отправщиком, с теми же проверками и в ту же таблицу.

Что здесь принципиально:
  * нажатие сначала занимает черновик (одна строка, один UPDATE под блокировкой), и только
    потом что-то отправляется — два нажатия не дают двух отправок;
  * отправляется текст из строки черновика; из данных кнопки берутся только номер и случайная метка;
  * сеть — вне транзакций; исход «неизвестно» сам не повторяется никогда.
"""

# Жизненный цикл черновика (замена прежнего ожидающего, неделимое закрытие, срок годности)
# основан на NousResearch/hermes-telegram-business (MIT), state.py@98c60af; разбор нажатий и
# клавиатура — manager.py@98c60af. Исправлено относительно оригинала: черновик занимается до
# отправки, а не после; срок годности проверяет фоновая работа; в кнопке есть случайная метка;
# правки текста через «следующее сообщение» нет вовсе.
# Замысел (не код) подтверждения вне хода модели и исхода «отправлено, но ответ потерян» —
# tolboy/telegram-mcp-tdlib (Apache-2.0): DestructiveApprovalService.kt, docs/SEND_IDEMPOTENCY.md.

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import secrets
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import asyncpg

from .. import access, authority, bridge, confirm, jobs, store, words
from ..tg.gateway import AccountUnavailable, FloodWait, SendForbidden
from . import policy, runtime
from . import text as textlib
from .policy import Decision, Target

logger = logging.getLogger("shturman.outbox")

CALLBACK_MODULE = "ob"
CARD_HANDLER = "outbox.card"
BUSINESS_HANDLER = "outbox.sent"
_USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
CARD_LIMIT = 3900          # знаков на одну карточку вместе с шапкой (предел Telegram — 4096)
POLL_SECONDS = 1.0         # как часто отправщик сам проверяет, нет ли согласованных черновиков
# Плагин ставит эту приставку в текст ошибки, когда Telegram ответил отказом и сообщение точно
# не ушло. Любая другая ошибка отправки через бизнес-бота считается исходом «неизвестно».
NOT_SENT_PREFIX = bridge.NOT_SENT_PREFIX

STATUS_WORDS = {
    "pending": "ждёт вашего решения",
    "approved": "принят, отправляется",
    "sending": "отправляется",
    "sent": "отправлен",
    "failed": "не отправлен",
    "outcome_unknown": "неизвестно, дошёл ли — проверьте чат",
    "rejected": "отклонён, не отправлен",
    "expired": "срок истёк, не отправлен",
    "superseded": "заменён новым, не отправлен",
    "placed": "положен в поле ввода чата — отправляете вы",
}
ALREADY = {
    "approved": "Этот черновик уже принят и отправляется.",
    "sending": "Этот черновик уже отправляется.",
    "sent": "Этот черновик уже отправлен.",
    "failed": "Этот черновик не был отправлен. Попросите подготовить новый.",
    "outcome_unknown": "Неизвестно, дошло ли сообщение. Проверьте чат.",
    "rejected": "Этот черновик уже отклонён.",
    "expired": "Срок черновика истёк. Попросите подготовить новый.",
    "superseded": "Этот черновик заменён новым — смотрите следующую карточку.",
    "placed": "Этот текст уже положен в поле ввода чата — отправьте его сами.",
}
CHAT_KINDS = (("personal", "личный чат"), ("bot", "бот"), ("saved", "избранное"),
              ("group", "группа"), ("channel", "канал"))


class Refused(Exception):
    """Отказ создать черновик: решение с причиной и код ответа HTTP."""

    def __init__(self, decision: Decision, status: int = 409) -> None:
        super().__init__(decision.message)
        self.decision, self.status = decision, status


def _status_for(decision: Decision) -> int:
    if decision.code == "chat_not_found":
        return 404
    if decision.code.startswith("text_") or decision.code == "reply_not_found":
        return 422
    if decision.code.startswith("limit_") or decision.code in ("flood_wait", "duplicate_text"):
        return 429
    return 409


def public(row: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
    """Черновик для ответа API. Случайной метки кнопки здесь нет и быть не должно."""
    out: dict[str, Any] = {
        "draft_id": row["id"], "status": row["status"], "account_id": row["account_id"],
        "chat_id": row["chat_id"], "channel": row["channel"], "origin": row["origin"],
        "text": row["text"], "text_purged": row["text_purged_at"] is not None,
        "reply_to_tg_id": row["reply_to_tg_id"],
        # `prepare_only` — прежний признак «только для просмотра» (до Р-105); всегда false, ключ оставлен на
        # один выпуск для тех, кто его читает.
        "task_id": row["task_id"], "prepare_only": False,
        "topic_tg_id": row["topic_tg_id"], "sources": _source_refs(row["sources"]),
        "parts_total": row["parts_total"], "parts_sent": row["parts_sent"],
        "sent_tg_message_ids": list(row["sent_tg_message_ids"]),
        "error_code": row["error_code"], "error": row["error_text"],
    }
    for key in ("created_at", "expires_at", "approved_at", "finished_at"):
        out[key] = row[key].isoformat() if row[key] else None
    out.update(extra)
    return out


# --- карточка владельцу ---

def _chat_kind(tgt: Target) -> str:
    return next((word for mark, word in CHAT_KINDS if mark in tgt.chat_type), "чат")


def _voice(tgt: Target, channel: str) -> str:
    """От чьего имени уйдёт ответ — словами из единого словаря (Р-86)."""
    return words.VOICE_OWNER if channel == "business" else words.VOICE_HELPER


def lead(row: Mapping[str, Any], tgt: Target) -> str:
    """Первая строка карточки: кому и от чьего имени (Б10). Имя не склоняется — оно чужое и любое."""
    kind, name = _chat_kind(tgt), textlib.one_line(tgt.display_name, 80) or "без названия"
    where = {"группа": f" в группу «{name}»", "канал": f" в канал «{name}»"}.get(kind, f": {name} ({kind})")
    return f"Ответ{where} · {_voice(tgt, row['channel'])}"


def _timezone() -> ZoneInfo:
    mod = runtime.current()
    try:
        return ZoneInfo(mod.state.config.timezone) if mod is not None else ZoneInfo("UTC")
    except Exception:
        return ZoneInfo("UTC")


def _messages_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "сообщение"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "сообщения"
    return "сообщений"


def _who(tgt: Target) -> str:
    """Что подделать нельзя: имя выбирает сам собеседник, поэтому в служебной строке карточки —
    номер в Telegram и @имя."""
    line = f"номер в Telegram {int(tgt.tg_id)}"
    if tgt.username and _USERNAME.match(tgt.username):
        line += f" · @{tgt.username}"
    return line


async def card_context(conn: asyncpg.Connection, row: Mapping[str, Any], tgt: Target) -> dict[str, Any]:
    """Сведения из архива для карточки: давно ли идёт переписка, на что это ответ и свёрнутый разговор
    перед ним (Р-103, convo.py) — только видимое ассистенту."""
    from .. import convo
    stats = await conn.fetchrow(
        """SELECT count(*) AS total, min(sent_at) AS first FROM messages
           WHERE chat_id = $1 AND kind = 'message' AND deleted_at IS NULL""", row["chat_id"])
    if stats["total"]:
        first = stats["first"].astimezone(_timezone()).strftime("%d.%m.%Y")
        facts = f"в архиве {stats['total']} {_messages_word(stats['total'])}, первое {first}"
    else:
        facts = ("Новый собеседник" if tgt.peer_class == "user" else "Новый чат") + ": раньше переписки не было"
    excerpt, anchor = None, None
    if row.get("reply_to_tg_id") is not None:
        found = await conn.fetchrow(
            "SELECT id, text FROM messages WHERE chat_id = $1 AND tg_message_id = $2 AND deleted_at IS NULL AND agent_visible",
            row["chat_id"], row["reply_to_tg_id"])
        if found is not None:
            excerpt, anchor = found["text"], found["id"]
    elif row.get("trigger_message_id") is not None:
        # Черновик автоответа доверенным в режиме подготовки: ответ на сообщение, которое его запустило.
        found = await conn.fetchrow(
            "SELECT id, text FROM messages WHERE id = $1 AND chat_id = $2 AND deleted_at IS NULL AND agent_visible",
            row["trigger_message_id"], row["chat_id"])
        if found is not None:
            excerpt, anchor = found["text"], found["id"]
    if anchor is not None:
        found_rows = await convo.rows(conn, row["chat_id"], anchor_id=anchor)
    else:
        found_rows = await convo.rows(conn, row["chat_id"], until=row.get("created_at"))
    conversation = convo.lines(found_rows, _timezone(), peer=textlib.one_line(tgt.display_name, 60) or None)
    return {"facts": facts, "reply_excerpt": excerpt, "conversation": conversation}


def _note(row: Mapping[str, Any]) -> str | None:
    if row["status"] in ("failed", "outcome_unknown") and row["error_text"]:
        return "Причина: " + row["error_text"]
    return None


def _service_line(row: Mapping[str, Any], tgt: Target, facts: str | None) -> str:
    """Служебное — одной строкой в конце шапки (Б10): то, что подделать нельзя, и сведения из архива."""
    parts = [_who(tgt)]
    if facts and not facts.startswith("Нов"):
        parts.append(textlib.one_line(facts, 120))
    if tgt.account_role == "assistant":
        parts.append(f"{words.HELPER_ACCOUNT} «{textlib.one_line(tgt.account_label, 40)}»")
    parts.append(f"{len(row['text'])} знаков")
    return "Служебное: " + " · ".join(parts)


def card_head(row: Mapping[str, Any], tgt: Target, *, note: str | None = None,
              reply_excerpt: str | None = None, facts: str | None = None) -> list[str]:
    """Шапка карточки: первой строкой — кому и от чьего имени; затем на что ответ и важные
    оговорки; служебное — последней строкой. Одна и та же в боте и в Mini App."""
    sends = len(policy.parts_of(row["channel"], row["text"]))
    head = [lead(row, tgt)]
    if reply_excerpt:
        head.append(f"В ответ на: «{textlib.one_line(reply_excerpt, 100)}»")
    if facts and facts.startswith("Нов"):
        head.append(textlib.one_line(facts, 120))     # новый собеседник — это важно, не служебное
    if sends > 1:
        head.append(f"Текст длинный: уйдёт {sends} сообщениями подряд.")
    if note:
        head.append(textlib.one_line(note, 300))
    head.append(_service_line(row, tgt, facts))
    return head


def _status_word(row: Mapping[str, Any], status: str | None) -> str:
    return STATUS_WORDS[status or row["status"]]


def card_rich(row: Mapping[str, Any], tgt: Target, *, status: str | None = None, note: str | None = None,
              reply_excerpt: str | None = None, facts: str | None = None,
              conversation: list[str] | None = None, used: list[str] | None = None
              ) -> list[tuple[str, list[dict[str, Any]]]]:
    """Части карточки и их оформление (richtext.py). Весь текст сообщения показывается дословно; длинный —
    в нескольких карточках подряд, без сокращений. Текст идёт последним: после него в карточке ничего нет.
    Первая строка — кому и от чьего имени, вторая — номер черновика и его состояние. Над служебной строкой
    первой части — свёрнутый разговор (Р-103), если он помещается рядом с черновиком целиком: черновик
    важнее контекста. `used` — что из найденного в архиве ассистент назвал опорой ответа."""
    from .. import convo, richtext
    word = _status_word(row, status)
    head = card_head(row, tgt, note=note, reply_excerpt=reply_excerpt, facts=facts)
    if used:
        head.insert(-1, "Учтено найденное в архиве этого чата: " + "; ".join(f"«{u}»" for u in used))
    tail = " · ниже — текст сообщения дословно"
    budget = CARD_LIMIT - 200 - sum(textlib.utf16_len(line) + 1 for line in head) - textlib.utf16_len(tail) - 60
    chunks = textlib.split_text(row["text"], budget) or [row["text"]]
    total = len(chunks)
    quote = convo.fit(conversation or [], budget - textlib.utf16_len(row["text"])) if total == 1 else []
    cards = []
    for index, chunk in enumerate(chunks, 1):
        title = f"Черновик № {row['id']} — {word}"
        if total > 1:
            title += f" · часть {index} из {total}"
        rich = richtext.Rich()
        if index == 1:
            rich.lines([head[0], title, *head[1:-1]])
            if total > 1:
                rich.line(f"Текст показан в {total} сообщениях подряд; кнопки — под последним.")
            convo.add_quote(rich, quote)
            rich.line(head[-1] + tail)
        else:
            rich.lines([head[0], title, "Продолжение текста сообщения, дословно:"])
        rich.line()
        rich.add(chunk)
        cards.append(rich.build())
    return cards


def card_parts(row: Mapping[str, Any], tgt: Target, **kwargs: Any) -> list[str]:
    """Текст частей карточки (без оформления) — см. `card_rich`."""
    return [text for text, _ in card_rich(row, tgt, **kwargs)]


def _state() -> Any:
    mod = runtime.current()
    return mod.state if mod is not None else None


async def buttons_for(conn: asyncpg.Connection, row: Mapping[str, Any], tgt: Target | None, *,
                      bot: bool = True) -> list[list[dict[str, Any]]]:
    """Кнопки под карточкой черновика ассистента — по `replies/delivery.py`, одни и те же в боте и в
    приложении (Д-46): «Отправить» — только когда доставка доступна сейчас, «Отклонить» — всегда, пока
    черновик ждёт. В боте ещё «Скопировать» (короткий текст) и «Ответить голосом» (Р-103)."""
    from ..replies import delivery
    avail = await delivery.for_draft(conn, _state(), row, tgt)
    return delivery.buttons(avail, draft=row, bot=bot)


def _card_messages(row: Mapping[str, Any]) -> dict[int, int]:
    """{номер части карточки: идентификатор сообщения владельцу}."""
    raw = row["card_messages"]
    data = json.loads(raw) if isinstance(raw, str) else (raw or {})
    return {int(part): int(message_id) for part, message_id in data.items()}


async def awaiting_owner(conn: asyncpg.Connection, *, limit: int = 50) -> list[dict[str, Any]]:
    """Черновики, которые ждут решения владельца, — для Mini App бота согласований
    (`setup_page/approvals.py`): та же шапка, что в карточке, текст дословно и те же кнопки с той
    же случайной меткой. Решение приходит обратно данными кнопки в `on_button` — тем же путём,
    что нажатие в боте. Список — ровно те черновики, нажатие по которым `on_button` не отвергнет
    сразу: ждущие, с текстом, от агента или для просмотра, не просроченные."""
    rows = await conn.fetch(
        """SELECT * FROM outbox_drafts
           WHERE status = 'pending' AND text <> '' AND origin IN ('agent', 'suggest') AND expires_at > now()
           ORDER BY id LIMIT $1""", limit)
    out = []
    for row in rows:
        tgt = await policy.target(conn, row["chat_id"])
        if tgt is None:
            continue
        if row["origin"] == "suggest":
            # «Черновик ответа» сервиса (Р-105): шапку и кнопки собирает модуль, который шлёт его карточку.
            from ..rehearsal import core as rehearsal
            entry = await rehearsal.app_entry(conn, row, tgt)
            if entry is not None:
                out.append(entry)
            continue
        context = await card_context(conn, row, tgt)
        parts = card_parts(row, tgt, **context)
        conversation = context.pop("conversation")
        out.append({
            "id": row["id"], "title": f"Черновик № {row['id']}", "head": card_head(row, tgt, **context),
            # Свёрнутый разговор перед черновиком (Р-103): те же строки, что в боте, не больше пяти.
            "context": conversation[-5:],
            "text": row["text"], "buttons": await buttons_for(conn, row, tgt, bot=False),
            # Сообщение в боте, под которым кнопки (последняя часть карточки): после решения в
            # Mini App его текст меняется так же, как после нажатия в боте.
            "card_message_id": _card_messages(row).get(len(parts)),
            "expires_at": row["expires_at"],
        })
    return out


async def refresh_cards(conn: asyncpg.Connection, row: Mapping[str, Any], tgt: Target | None, *,
                        skip_part: int | None = None, replace_with: str | None = None) -> int:
    """Заменяет текст уже доставленных карточек черновика на текущее состояние и убирает кнопки.

    Так под завершённым, заменённым или просроченным черновиком не остаётся кнопок, которые
    выглядят живыми. `replace_with` — показать вместо карточки одну строку (текст стёрт).
    Возвращает число карточек, поставленных на замену.
    """
    delivered = _card_messages(row)
    if not delivered:
        return 0
    if row["origin"] == "suggest" and replace_with is None:
        # Карточка «Черновик ответа» — своего вида (rehearsal/core.py): её и перерисовывает тот модуль.
        from ..rehearsal import core as rehearsal
        return await rehearsal.refresh_card(conn, row, delivered)
    if replace_with is None and (tgt is None or row["text"] == ""):
        replace_with = f"Черновик № {row['id']} — {STATUS_WORDS[row['status']]}. Текст удалён."
    parts = [] if replace_with is not None else card_rich(
        row, tgt, note=_note(row), **await card_context(conn, row, tgt))
    edited = 0
    for part, message_id in sorted(delivered.items()):
        if part == skip_part:
            continue
        entities = None
        if replace_with is not None:
            text = replace_with
        elif 1 <= part <= len(parts):
            text, entities = parts[part - 1]
        else:
            continue
        await bridge.edit_owner_message(conn, message_id, text, remove_buttons=True, entities=entities)
        edited += 1
    return edited


@bridge.on_result(CARD_HANDLER)
async def _card_delivered(conn: asyncpg.Connection, job: dict[str, Any], result: dict[str, Any]) -> None:
    """Запоминает, каким сообщением карточка пришла владельцу."""
    draft_id, part, message_id = job["context"].get("draft_id"), job["context"].get("part"), result.get("message_id")
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (draft_id, part, message_id)):
        return
    row = await conn.fetchrow(
        """UPDATE outbox_drafts SET card_message_ids = array_append(card_message_ids, $2),
                  card_messages = card_messages || jsonb_build_object($3::text, $2::bigint)
           WHERE id = $1 RETURNING *""",
        draft_id, message_id, str(part))
    if row is not None and row["status"] != "pending":
        # Пока карточка шла, черновик уже решился (заменён, просрочен, отменён): сразу гасим кнопки.
        await refresh_cards(conn, row, await policy.target(conn, row["chat_id"]))


@bridge.on_failure(CARD_HANDLER)
async def _card_failed(conn: asyncpg.Connection, job: dict[str, Any], error: str) -> None:
    """Карточка не дошла до владельца. Черновик, который некому согласовать, не остаётся «ждущим»."""
    draft_id = job["context"].get("draft_id")
    if not isinstance(draft_id, int):
        return
    row = await conn.fetchrow("SELECT * FROM outbox_drafts WHERE id = $1 FOR UPDATE", draft_id)
    if row is None or row["status"] != "pending":
        return
    await finish(conn, row, await policy.target(conn, row["chat_id"]), "failed", code="card_not_delivered",
                 message=policy.REASONS["card_not_delivered"], tell_owner=False)


def _source_refs(value: Any) -> list[dict[str, Any]]:
    return json.loads(value) if isinstance(value, str) else (value or [])


async def _task_binding(conn: asyncpg.Connection, tgt: Target, task_id: int | None,
                        sources: list[dict[str, Any]] | None) -> dict[str, Any]:
    if not isinstance(sources, (list, type(None))) or any(not isinstance(ref, dict) for ref in sources or []):
        raise Refused(Decision(False, "invalid_sources", "Нужен список ссылок на источники."), 422)
    encoded = json.dumps(sources or [], ensure_ascii=False, sort_keys=True)
    if task_id is None:
        if sources:
            raise Refused(Decision(False, "unbound_sources", "Источники должны быть привязаны к заданию ответа."))
        return {"sources": encoded, "policy_revision": None, "topic_tg_id": None}
    if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id <= 0:
        raise Refused(Decision(False, "invalid_task", "Неверный номер задания."), 422)
    task = await conn.fetchrow("SELECT * FROM reply_tasks WHERE id = $1 FOR UPDATE", task_id)
    if task is None or task["chat_id"] != tgt.chat_id or task["account_id"] != tgt.account_id:
        raise Refused(Decision(False, "task_target_mismatch", "Задание относится к другому адресату."))
    if task["status"] != "drafting" or await conn.fetchval("SELECT $1::timestamptz <= now()", task["expires_at"]):
        raise Refused(Decision(False, "inactive_task", "Задание больше не готовит ответ."))
    return {"sources": encoded, "policy_revision": str(task["policy_revision"]),
            "topic_tg_id": task["topic_tg_id"]}


async def _task_guard(conn: asyncpg.Connection, state: Any, row: Mapping[str, Any]) -> Decision:
    if row["task_id"] is None:
        return policy.ALLOW
    from ..replies import workflow
    return await workflow.presend(conn, state, row)


async def readiness(conn: asyncpg.Connection, tg: Any, config: Any, chat_id: int,
                    channel: str | None = None) -> dict[str, Any]:
    """Separate preparation from sending readiness without implying an enabled transport."""
    rules = await policy.load(conn, config)
    tgt = await policy.target(conn, chat_id)
    if tgt is None:
        return {"can_prepare": False, "can_send": False, "reason": "chat_not_found"}
    picked, decision = policy.pick_channel(tgt, channel)
    if picked is None:
        return {"can_prepare": False, "can_send": False, "reason": decision.code}
    decision = await policy.check_target(conn, rules, tgt)
    prepare = decision.ok
    if decision.ok:
        decision = policy.check_switch(rules)
    if decision.ok:
        decision = await policy.check_channel(conn, tg, rules, tgt, picked)
    return {"can_prepare": prepare, "can_send": decision.ok, "channel": picked,
            "reason": decision.code, "error": decision.message}


# --- создание ---

async def create(
    conn: asyncpg.Connection, state: Any, *, chat_id: int, text: str, channel: str | None = None,
    reply_to_message_id: int | None = None, idempotency_key: str | None = None,
    task_id: int | None = None, sources: list[dict[str, Any]] | None = None,
    used: list[str] | None = None,
) -> dict[str, Any]:
    """Создаёт черновик ассистента (`origin = 'agent'`) и ставит карточку владельцу. Отказ — исключение
    `Refused` с причиной. Происхождение не выбирается: «Черновик ответа» сервиса (`suggest`) создаёт только
    `create_suggestion`, внутри сервиса.

    `state` — состояние сервиса: из него берутся шлюз сессий и настройки окружения (главный
    выключатель отправки). `reply_to_message_id` — идентификатор строки архива (messages.id)
    в том же чате. `used` — что из найденного в архиве ассистент назвал опорой ответа (строка «Учтено»
    карточки, Р-103).
    """
    def refuse(decision: Decision) -> Refused:
        return Refused(decision, _status_for(decision))

    tg = state.extras.get("tg")
    cleaned = textlib.clean_outgoing(text)
    text_hash = textlib.content_hash(cleaned)
    async with conn.transaction():
        halted = await policy.check_halt(conn)
        if not halted.ok:
            raise refuse(halted)
        rules = await policy.load(conn, state.config)
        decision = policy.check_switch(rules)
        if not decision.ok:
            raise refuse(decision)
        tgt = await policy.target(conn, chat_id)
        if tgt is None or (access.is_hermes() and (
                tgt.excluded or store.is_blocked_peer(tgt.peer_class, tgt.tg_id, tgt.username))):
            # Ассистенту исключённый владельцем чат — как несуществующий, и до любых других проверок (Д-53):
            # иначе по отказам вида канала он узнавал бы, какие чаты исключены.
            raise refuse(policy.deny("chat_not_found"))
        await policy.lock_chat(conn, chat_id)
        binding = await _task_binding(conn, tgt, task_id, sources)
        if idempotency_key is not None:
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('shturman.outbox.key'), hashtext($1))",
                               idempotency_key)
            known = await conn.fetchrow("SELECT * FROM outbox_drafts WHERE idempotency_key = $1", idempotency_key)
            if known is not None:
                same = (known["chat_id"] == chat_id and known["text_hash"] == text_hash
                        and channel in (None, known["channel"])
                        and known["task_id"] == task_id and _source_refs(known["sources"]) == _source_refs(binding["sources"]))
                if not same:
                    raise refuse(policy.deny("idempotency_conflict"))
                return public(known, replayed=True)
        if await bridge.get_owner(conn) is None:
            raise refuse(policy.deny("owner_unknown"))
        picked, decision = policy.pick_channel(tgt, channel)
        if picked is None:
            raise refuse(decision)
        for decision in (await policy.check_target(conn, rules, tgt),
                         await policy.check_channel(conn, tg, rules, tgt, picked),
                         policy.check_text(rules, picked, cleaned),
                         (policy.ALLOW if task_id is not None else await policy.check_first_contact(conn, rules, tgt, picked))):
            if not decision.ok:
                raise refuse(decision)
        # Из лимитов при создании важен только повтор: остальные проверяются в момент отправки.
        if await policy.is_duplicate(conn, rules, tgt, text_hash):
            raise refuse(policy.deny("duplicate_text", temporary=True,
                                     retry_after=int(rules["duplicate_window_seconds"])))
        reply_to_tg_id = None
        if reply_to_message_id is not None:
            reply_to_tg_id = await conn.fetchval(
                """SELECT tg_message_id FROM messages
                   WHERE id = $1 AND chat_id = $2 AND deleted_at IS NULL AND agent_visible""",
                reply_to_message_id, chat_id)
            if reply_to_tg_id is None:
                raise refuse(policy.deny("reply_not_found"))
        waiting = await conn.fetch(
            "SELECT * FROM outbox_drafts WHERE chat_id = $1 AND status = 'pending' FOR UPDATE", chat_id)
        for old in waiting:
            if (old["text_hash"] == text_hash and old["channel"] == picked and idempotency_key is None
                    and old["reply_to_tg_id"] == reply_to_tg_id and old["task_id"] == task_id
                    and _source_refs(old["sources"]) == _source_refs(binding["sources"]) and old["origin"] == "agent"
                    and not await conn.fetchval("SELECT $1::timestamptz <= now()", old["expires_at"])):
                return public(old, duplicate=True)
        recent = await conn.fetchval(
            """SELECT count(*) FROM outbox_drafts
               WHERE account_id = $1 AND origin = 'agent' AND created_at > now() - interval '1 hour'""",
            tgt.account_id)
        if recent >= rules["drafts_per_hour"]:
            raise refuse(policy.deny("limit_drafts", temporary=True, cap=rules["drafts_per_hour"]))
        # В чате ждёт решения только один черновик: прежний заменяется, кнопки под его карточкой гаснут.
        for old in waiting:
            await finish(conn, old, tgt, "superseded", tell_owner=False)
        parts = policy.parts_of(picked, cleaned)
        row = await conn.fetchrow(
            """INSERT INTO outbox_drafts (account_id, chat_id, channel, text, text_hash, reply_to_tg_id,
                                          origin, idempotency_key, nonce, status, expires_at, parts_total,
                                          task_id, sources, task_policy_revision, topic_tg_id)
               VALUES ($1, $2, $3, $4, $5, $6, 'agent', $7, $8, 'pending',
                       now() + make_interval(secs => $9), $10, $11, $12::jsonb, $13, $14)
               RETURNING *""",
            tgt.account_id, chat_id, picked, cleaned, text_hash, reply_to_tg_id, idempotency_key,
            secrets.token_urlsafe(16), float(rules["draft_ttl_seconds"]), len(parts),
            task_id, binding["sources"], binding["policy_revision"], binding["topic_tg_id"])
        cards = card_rich(row, tgt, used=used, **await card_context(conn, row, tgt))
        keyboard = await buttons_for(conn, row, tgt)
        for index, (card, entities) in enumerate(cards, 1):
            await bridge.notify_owner(
                conn, card, buttons=keyboard if index == len(cards) else None,
                handler=CARD_HANDLER, context={"draft_id": row["id"], "part": index},
                dedup_key=f"outbox:card:{row['id']}:{index}", entities=entities)
        return public(row, text_changed=cleaned != text, cards=len(cards))


async def create_autoreply(
    conn: asyncpg.Connection, tgt: Target, *, channel: str, text: str, trigger_message_id: int,
    task_id: int | None = None, sources: list[dict[str, Any]] | None = None,
) -> int | None:
    """Автоответ доверенному по правилу владельца: рождается согласованным и уходит отправщиком."""
    halted = await policy.check_halt(conn)
    if not halted.ok:
        raise Refused(halted)
    binding = await _task_binding(conn, tgt, task_id, sources)
    cleaned = textlib.clean_outgoing(text)
    if task_id is not None:
        mod = runtime.current()
        if mod is None:
            raise Refused(policy.deny("service_stopped"))
        decision = await _task_guard(conn, mod.state, {
            "task_id": task_id, "account_id": tgt.account_id, "chat_id": tgt.chat_id,
            "task_policy_revision": binding["policy_revision"], "sources": binding["sources"],
            "origin": "autoreply", "topic_tg_id": binding["topic_tg_id"],
        })
        if not decision.ok:
            raise Refused(decision)
    row = await conn.fetchrow(
        """INSERT INTO outbox_drafts (account_id, chat_id, channel, text, text_hash, origin,
                                      trigger_message_id, nonce, status, expires_at, approved_at, parts_total,
                                      task_id, sources, task_policy_revision, topic_tg_id, approved_by)
           VALUES ($1, $2, $3, $4, $5, 'autoreply', $6, $7, 'approved', now() + interval '1 hour',
                   now(), $8, $9, $10::jsonb, $11, $12, $13)
           ON CONFLICT (trigger_message_id) WHERE origin = 'autoreply' AND trigger_message_id IS NOT NULL
           DO NOTHING RETURNING *""",
        tgt.account_id, tgt.chat_id, channel, cleaned, textlib.content_hash(cleaned), trigger_message_id,
        secrets.token_urlsafe(16), len(policy.parts_of(channel, cleaned)),
        task_id, binding["sources"], binding["policy_revision"], binding["topic_tg_id"], await _owner_id(conn))
    return row["id"] if row is not None else None


SUGGEST_TTL_SECONDS = 24 * 3600     # «Черновик ответа» ждёт решения не дольше суток
SUGGEST_TEXT_KEEP_SECONDS = 3600    # текст завершённого черновика сервиса стирается через час


async def create_suggestion(conn: asyncpg.Connection, tgt: Target, *, text: str, trigger_message_id: int,
                            reply_to_message_id: int | None) -> asyncpg.Record:
    """«Черновик ответа» сервиса (Р-105): предложенный текст на входящее в личном чате основного аккаунта.
    Карточку шлёт сам вызывающий (`rehearsal/core.py`). Ничего не отправляет и не согласует: отправить —
    только нажатием владельца «Отправить» (`on_button`), с той же проверкой, что у черновика ассистента.

    Проверки здесь — можно ли вообще писать в этот чат и годится ли текст. Выключатель отправки и канал —
    нет: черновик показывается и без них, а кнопки доставки решает `replies/delivery.py`. Ждущий черновик
    сервиса в чате заменяется этим: живая кнопка «Отправить» в чате одна (`outbox_drafts_one_pending`).
    Ждущий черновик ассистента или автоответа не заменяется — его владелец попросил сам или он привязан к
    задаче ответа: тогда черновика сервиса нет (`agent_draft_pending`), карточка приходит с текстом без
    кнопок доставки."""
    halted = await policy.check_halt(conn)
    if not halted.ok:
        raise Refused(halted)
    channel, decision = policy.pick_channel(tgt, None)
    if channel is None:
        raise Refused(decision)
    rules = await policy.load(conn, None)
    cleaned = textlib.clean_outgoing(text)
    for decision in (await policy.check_target(conn, rules, tgt),
                     policy.ALLOW if cleaned.strip() else policy.deny("text_empty")):
        if not decision.ok:
            raise Refused(decision)
    reply_to_tg_id = None
    if reply_to_message_id is not None:
        reply_to_tg_id = await conn.fetchval(
            """SELECT tg_message_id FROM messages
               WHERE id = $1 AND chat_id = $2 AND deleted_at IS NULL AND agent_visible""",
            reply_to_message_id, tgt.chat_id)
    await policy.lock_chat(conn, tgt.chat_id)
    waiting = await conn.fetch(
        "SELECT * FROM outbox_drafts WHERE chat_id = $1 AND status = 'pending' FOR UPDATE", tgt.chat_id)
    if any(old["origin"] != "suggest" for old in waiting):
        raise Refused(policy.deny("agent_draft_pending"))
    for old in waiting:
        await finish(conn, old, tgt, "superseded", tell_owner=False)
    return await conn.fetchrow(
        """INSERT INTO outbox_drafts (account_id, chat_id, channel, text, text_hash, reply_to_tg_id, origin,
                                      trigger_message_id, nonce, status, expires_at, parts_total)
           VALUES ($1, $2, $3, $4, $5, $6, 'suggest', $7, $8, 'pending', now() + make_interval(secs => $9), $10)
           RETURNING *""",
        tgt.account_id, tgt.chat_id, channel, cleaned, textlib.content_hash(cleaned), reply_to_tg_id,
        trigger_message_id, secrets.token_urlsafe(16), float(SUGGEST_TTL_SECONDS),
        len(policy.parts_of(channel, cleaned)))


async def cancel(conn: asyncpg.Connection, draft_id: int) -> dict[str, Any] | None:
    """Отменяет черновик, пока владелец не принял решение. Возвращает черновик или None, если такого нет."""
    async with conn.transaction():
        row = await conn.fetchrow("SELECT * FROM outbox_drafts WHERE id = $1 FOR UPDATE", draft_id)
        if row is None:
            return None
        if row["status"] != "pending":
            return public(row, cancelled=False)
        await finish(conn, row, await policy.target(conn, row["chat_id"]), "rejected", code="cancelled",
                     message=policy.REASONS["cancelled"], tell_owner=False)
        row = await conn.fetchrow("SELECT * FROM outbox_drafts WHERE id = $1", draft_id)
        return public(row, cancelled=True)


# --- завершение и сообщение владельцу ---

async def finish(
    conn: asyncpg.Connection, row: Mapping[str, Any], tgt: Target | None, status: str, *,
    code: str | None = None, message: str | None = None, tell_owner: bool = True,
    skip_part: int | None = None,
) -> bool:
    """Переводит черновик в конечное состояние, обновляет его карточки (итог вместо кнопок) и,
    если исход требует внимания, отдельно сообщает владельцу. Повторный вызов ничего не делает.

    `skip_part` — часть карточки, которую обновит сам ответ на нажатие кнопки.
    """
    done = await conn.fetchrow(
        """UPDATE outbox_drafts SET status = $2, error_code = $3, error_text = $4, finished_at = now()
           WHERE id = $1 AND status <> $2 RETURNING *""",
        row["id"], status, code, message)
    if done is None:
        return False
    if done.get("task_id") is not None:
        from ..replies import workflow
        task_status = ("completed" if status == "sent" else "declined" if status == "rejected" else
                       "expired" if status == "expired" else "cancelled" if status == "superseded" else "failed")
        await workflow.stop(conn, done["task_id"], task_status, code)
    mod = runtime.current()
    if mod is not None:
        mod.stop_typing(done["account_id"], done["chat_id"])
    edited = await refresh_cards(conn, done, tgt, skip_part=skip_part)
    if not tell_owner:
        return True
    auto = done["origin"] == "autoreply"
    who = tgt.display_name if tgt is not None else "чат"
    label = f"{who} ({'автоответ' if auto else 'черновик'} № {done['id']})"
    never_twice = "Само повторно не отправится: так сообщение не уйдёт дважды."
    if status == "sent":
        if auto or edited:
            # автоответы видны в списке отправленного; у черновика итог уже стоит в его карточке
            return True
        count = len(done["sent_tg_message_ids"])
        text = f"Отправлено: {label}." + (f" Сообщений: {count}." if count > 1 else "")
        silent = True
    elif status == "outcome_unknown":
        text = (f"Не удалось узнать, дошло ли сообщение: {label}. Откройте чат и проверьте. {never_twice} "
                "Если не дошло — попросите подготовить его заново.")
        silent = False
    else:
        if auto and code not in ("flood_wait", "partial"):
            return True  # несостоявшийся автоответ — просто отсутствие ответа
        # Часть длинного сообщения уже ушла (Д-56): так и сказано, а не «не отправлено».
        head = "Отправлено частично" if code == "partial" else "Не отправлено"
        text = f"{head}: {label}. {message or ''} {never_twice}".replace("  ", " ")
        silent = False
    await bridge.notify_owner(conn, text, silent=silent, dedup_key=f"outbox:{done['id']}:{status}")
    return True


# --- нажатие кнопки ---

@bridge.on_callback(CALLBACK_MODULE)
async def on_button(conn: asyncpg.Connection, rest: str, user_id: int) -> dict[str, Any]:
    """Нажатие под карточкой. Вызывается в транзакции; сети здесь нет — только занять черновик."""
    refused = {"answer": "Кнопка недоступна.", "edit_text": None, "remove_buttons": False}
    try:
        authority.requires_owner()
    except confirm.Refused:
        return refused
    action, _, tail = rest.partition(":")
    raw_id, _, nonce = tail.partition(":")
    if action not in ("s", "r", "p") or not raw_id.isascii() or not raw_id.isdigit() or len(raw_id) > 18:
        return refused
    owner = await bridge.get_owner(conn)
    if owner is None or int(owner["user_id"]) != int(user_id):
        return refused  # мост это уже проверил; вторая проверка — на случай ошибки выше
    row = await conn.fetchrow("SELECT * FROM outbox_drafts WHERE id = $1 FOR UPDATE", int(raw_id))
    expected = row["nonce"] if row is not None else secrets.token_urlsafe(16)
    if not hmac.compare_digest(nonce.encode(), expected.encode()) or row is None:
        return refused
    if row["error_code"] == "prepare_removed":
        # Карточка прежнего черновика «только для просмотра» (до Р-105) — ассистента или автоответа:
        # режима больше нет. Проверяется до происхождения: у автоответа своих кнопок нет, а у этих были.
        return {"answer": (PREPARE_REMOVED[:1].upper() + PREPARE_REMOVED[1:])[:190], "remove_buttons": True,
                "edit_text": f"Черновик № {row['id']}: {PREPARE_REMOVED}"}
    if row["origin"] not in ("agent", "suggest"):
        return refused
    tgt = await policy.target(conn, row["chat_id"])
    if tgt is None:
        return refused
    if row["text"] == "":
        return {"answer": ALREADY[row["status"]] if row["status"] != "pending" else refused["answer"],
                "remove_buttons": True,
                "edit_text": f"Черновик № {row['id']} — {STATUS_WORDS[row['status']]}. Текст удалён."}
    if action == "p" and row["origin"] != "suggest":
        return refused      # «В чат» — только у черновика ответа сервиса (черновикам ассистента — отложено)
    if row["origin"] == "suggest":
        # «Черновик ответа» сервиса: своя карточка, свой разбор (rehearsal/core.py), та же проверка отправки.
        from ..rehearsal import core as rehearsal
        return await rehearsal.on_draft_button(conn, action, row, tgt)
    context = await card_context(conn, row, tgt)
    pressed = len(card_parts(row, tgt, **context))   # кнопки — под последней частью карточки

    async def closed(answer: str) -> dict[str, Any]:
        fresh = await conn.fetchrow("SELECT * FROM outbox_drafts WHERE id = $1", row["id"])
        text, entities = card_rich(fresh, tgt, note=_note(fresh), **context)[-1]
        return {"answer": answer, "remove_buttons": True, "edit_text": text, "edit_entities": entities}

    if row["status"] != "pending":
        return await closed(ALREADY[row["status"]])
    if await conn.fetchval("SELECT $1::timestamptz <= now()", row["expires_at"]):
        await finish(conn, row, tgt, "expired", tell_owner=False, skip_part=pressed)
        return await closed("Срок черновика истёк. Попросите подготовить новый.")
    if action == "r":
        await finish(conn, row, tgt, "rejected", tell_owner=False, skip_part=pressed)
        return await closed("Отклонено, ничего не отправлено.")

    decided = await approve(conn, row, tgt)
    if decided.get("wait"):
        # Черновик остаётся ждать: кнопки под карточкой — какие доступны сейчас (Д-46).
        return {"answer": decided["answer"], "edit_text": None, "remove_buttons": False,
                "buttons": await buttons_for(conn, row, tgt)}
    if not decided["ok"]:
        await finish(conn, row, tgt, "failed", code=decided["code"], message=decided["message"],
                     tell_owner=False, skip_part=pressed)
        return await closed("Не отправлено: " + decided["message"])
    await refresh_cards(conn, decided["row"], tgt, skip_part=pressed)
    return await closed("Принято, отправляю.")


PREPARE_REMOVED = ("черновики «только для просмотра» заменены «Черновиком ответа» (обновление 0.0.12) — "
                   "эта карточка больше не действует, ничего не отправлено.")
# Препятствия, которые могут уйти сами или по решению владельца: черновик ждёт, кнопку можно нажать снова.
# Окно бизнес-бота (сутки) тоже: «Отправить» исчезает, а черновик остаётся — его можно отправить самому.
WAIT_CODES = frozenset({"sending_disabled", "business_window_closed", "assistant_stopped"})


async def approve(conn: asyncpg.Connection, row: Mapping[str, Any], tgt: Target) -> dict[str, Any]:
    """Нажатие «Отправить»: все проверки отправки заново и, если можно, согласование. Сети здесь нет.
    Итог: {ok, row} — согласован (отправит отправщик); {wait, answer} — пока нельзя, черновик ждёт;
    {ok: False, code, message} — нельзя совсем."""
    mod = runtime.current()
    if mod is None:
        return {"wait": True, "answer": policy.REASONS["service_stopped"] + " Попробуйте позже."}
    halted = await policy.check_halt(conn)
    if not halted.ok:
        return {"wait": True, "answer": "Пока нельзя: ассистент остановлен. Продолжить — /resume."}
    rules = await policy.load(conn, mod.state.config)
    decision = policy.check_switch(rules)
    if decision.ok:
        decision = await _task_guard(conn, mod.state, row)
    if decision.ok:
        decision = await policy.check_send(
            conn, mod.tg, rules, tgt, channel=row["channel"], text=row["text"], text_hash=row["text_hash"],
            origin="agent" if row["origin"] == "agent" else "suggest",
            draft_id=row["id"], task_bound=row["task_id"] is not None or row["origin"] == "suggest")
    if not decision.ok and (decision.temporary or decision.code in WAIT_CODES):
        return {"wait": True, "answer": "Пока нельзя: " + decision.message}
    approved = await conn.fetchrow(
        """UPDATE outbox_drafts SET status = 'approved', approved_at = now(), approved_by = $2
           WHERE id = $1 AND status = 'pending' RETURNING *""", row["id"], await _owner_id(conn))
    if not decision.ok:
        return {"ok": False, "code": decision.code, "message": decision.message}
    if row['task_id'] is not None:
        from ..replies import workflow
        await workflow.record_owner_resume(conn, int(row['task_id']))
    mod.kick()
    return {"ok": True, "row": approved}


# --- отправщик ---

async def dispatch(mod: runtime.Outbox) -> None:
    """Раздаёт согласованные черновики исполнителям: по одному исполнителю на аккаунт."""
    async with mod.state.pool.acquire() as conn:
        rows = await conn.fetch("SELECT DISTINCT account_id FROM outbox_drafts WHERE status = 'approved'")
    for r in rows:
        account_id = r["account_id"]
        task = mod.workers.get(account_id)
        if task is None or task.done():
            mod.workers[account_id] = mod.state.spawn(
                _account_worker(mod, account_id), name=f"outbox-sender-{account_id}")


async def _account_worker(mod: runtime.Outbox, account_id: int) -> None:
    try:
        while True:
            async with mod.state.pool.acquire() as conn:
                draft_id = await conn.fetchval(
                    """SELECT id FROM outbox_drafts WHERE account_id = $1 AND status = 'approved'
                       ORDER BY approved_at, id LIMIT 1""", account_id)
            if draft_id is None:
                return
            try:
                await deliver(mod, draft_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # В журнал — только номер черновика и вид ошибки: текст сообщения туда не попадает.
                logger.error("отправка черновика %s завершилась с ошибкой (%s)", draft_id, type(exc).__name__)
            # Что бы ни случилось, черновик не остаётся согласованным: иначе он ушёл бы позже сам
            # или отправщик крутился бы на нём бесконечно.
            async with mod.state.pool.acquire() as conn:
                await _give_up(conn, draft_id)
    finally:
        if mod.workers.get(account_id) is asyncio.current_task():
            mod.workers.pop(account_id, None)


async def _give_up(conn: asyncpg.Connection, draft_id: int) -> None:
    """Сбой в самом отправщике: черновик не должен остаться согласованным и уйти позже сам."""
    async with conn.transaction():
        row = await conn.fetchrow("SELECT * FROM outbox_drafts WHERE id = $1 FOR UPDATE", draft_id)
        if row is None:
            return
        tgt = await policy.target(conn, row["chat_id"])
        if row["status"] == "approved":
            await finish(conn, row, tgt, "failed", code="internal", message="Внутренняя ошибка сервиса.")
        elif row["status"] == "sending" and row["channel"] == "session":
            # отправка через сессию оборвалась, не записав исход
            await finish(conn, row, tgt, "outcome_unknown", code="outcome_unknown",
                         message=policy.REASONS["outcome_unknown"])


async def _owner_id(conn: asyncpg.Connection) -> int | None:
    owner = await bridge.get_owner(conn)
    return int(owner["user_id"]) if owner is not None else None


async def _approved_by_current_owner(conn: asyncpg.Connection, row: Mapping[str, Any]) -> bool:
    """Согласие дал нынешний владелец (Д-55): `approved_by` — номер владельца в момент согласования. Повторная
    привязка того же владельца согласия не отменяет. Смена владельца и так снимает согласованные черновики
    (`outbox.service._owner_changed`); это — вторая линия. Черновик, согласованный до миграции 0047 (номера нет), —
    прежняя сверка: запись о владельце не менялась после согласования."""
    current = await _owner_id(conn)
    if current is None or row["approved_at"] is None:
        return False
    if row["approved_by"] is not None:
        return int(row["approved_by"]) == current
    bound = await conn.fetchval("SELECT updated_at FROM settings WHERE key = 'owner'")
    return bound is not None and row["approved_at"] >= bound


async def _revoked(conn: asyncpg.Connection, state: Any, rules: dict[str, Any], row: Mapping[str, Any],
                   tgt: Target | None, *, next_part: bool = False) -> Decision:
    """Отозвано ли согласие с момента нажатия (Д-55, Д-56): «Остановить всё», выключатель, чат (исключён,
    запрещён, не тот), владелец, задача ответа и её источники, автоответ, срок согласия. Сети здесь нет.

    `next_part` — проверка перед второй и следующими частями длинного сообщения: срок согласия и «после
    входящего ничего нет» у автоответа там не проверяются — первая часть сама стала «новым сообщением»."""
    decision = await policy.check_halt(conn)
    if not decision.ok:
        return decision
    decision = policy.check_switch(rules)
    if not decision.ok:
        return decision
    if tgt is None:
        return policy.deny("chat_not_found")
    decision = await policy.check_target(conn, rules, tgt)
    if not decision.ok:
        return decision
    decision = await _task_guard(conn, state, row)
    if not decision.ok:
        return decision
    if row["origin"] == "autoreply" and row["task_id"] is None:
        from . import autoreply
        if next_part:
            decision = await autoreply.eligible(conn, tgt, row["trigger_message_id"])
        else:
            mod = runtime.current()
            if mod is None:
                return policy.deny("service_stopped")
            decision = await autoreply.still_allowed(conn, mod, await autoreply.load(conn), tgt, row)
        if not decision.ok:
            return decision
    elif not next_part and await conn.fetchval(
            "SELECT $1::timestamptz < now() - make_interval(secs => $2)",
            row["approved_at"], float(rules["approval_max_age_seconds"])):
        return policy.deny("stale")
    if not await _approved_by_current_owner(conn, row):
        return policy.deny("owner_changed")
    return policy.ALLOW


async def final_check(conn: asyncpg.Connection, state: Any, job_id: int | None,
                      payload: Mapping[str, Any]) -> Decision:
    """Последняя проверка задания `business.send` — исполнитель зовёт её прямо перед обращением к Telegram
    (Д-55). Задание ставится, когда черновик переходит в «отправляется»; пока оно ждёт очереди, владелец мог
    остановить ассистента, исключить чат, сменить правила, отозвать источник или сам смениться. Поэтому: есть
    черновик этого задания, он «отправляется», задание — его; текст, чат, ответ-на и подключение — те же, что в
    черновике; согласие не отозвано (`_revoked`). Задание без такого черновика не отправляется никогда.

    Отказ закрывает черновик здесь же (не отправлен, с причиной, владельцу — словами); исполнитель отвечает
    «not_sent». Блокировка строки черновика — до конца проверки: отзыв, пришедший в это время, ждёт её."""
    async with conn.transaction():
        if job_id is None:
            return policy.deny("draft_revoked")
        # Черновик этого задания — по `outbox_drafts.job_id` (его пишет та же транзакция, что ставит задание);
        # номер в данных задания должен с ним совпасть.
        row = await conn.fetchrow("SELECT * FROM outbox_drafts WHERE job_id = $1 FOR UPDATE", job_id)
        if row is None:
            return policy.deny("draft_revoked")
        if row["status"] != "sending":
            return policy.deny("draft_revoked")
        named = await conn.fetchval(
            "SELECT context->>'draft_id' FROM jobs WHERE id = $1 AND kind = $2", job_id, bridge.BUSINESS_SEND)
        tgt = await policy.target(conn, row["chat_id"])
        text = payload.get("text")
        if (row["channel"] != "business" or named != str(row["id"])
                or tgt is None or payload.get("chat_id") != tgt.tg_id or not isinstance(text, str) or text != row["text"]
                or textlib.content_hash(text) != row["text_hash"]
                or payload.get("reply_to_message_id") != row["reply_to_tg_id"]
                or payload.get("business_connection_id") != row["business_connection_id"]):
            decision = policy.deny("payload_mismatch")
        else:
            decision = await _revoked(conn, state, await policy.load(conn, state.config), row, tgt)
        if not decision.ok:
            await finish(conn, row, tgt, "failed", code=decision.code, message=decision.message)
        return decision


async def part_check(conn: asyncpg.Connection, state: Any, draft_id: int, *, first: bool) -> Decision:
    """Перед каждой частью отправки через аккаунт-помощника, после паузы между частями (Д-56): черновик всё ещё
    «отправляется», и согласие не отозвано — «Остановить всё», выключатель, чат, владелец, задача."""
    row = await conn.fetchrow("SELECT * FROM outbox_drafts WHERE id = $1", draft_id)
    if row is None or row["status"] != "sending":
        return policy.deny("draft_revoked")
    tgt = await policy.target(conn, row["chat_id"])
    return await _revoked(conn, state, await policy.load(conn, state.config), row, tgt, next_part=not first)


async def _presend(conn: asyncpg.Connection, mod: runtime.Outbox, rules: dict[str, Any],
                   row: Mapping[str, Any], tgt: Target) -> Decision:
    """Последняя проверка перед отправкой: за время ожидания всё могло измениться."""
    decision = await policy.check_halt(conn)
    if not decision.ok:
        return decision
    decision = await _task_guard(conn, mod.state, row)
    if not decision.ok:
        return decision
    cap = None
    if row["origin"] == "autoreply" and row["task_id"] is None:
        from . import autoreply  # автоответ опирается на этот модуль; обратная ссылка — только здесь

        settings = await autoreply.load(conn)
        cap = settings["daily_cap"]
        decision = await autoreply.still_allowed(conn, mod, settings, tgt, row)
        if not decision.ok:
            return decision
    elif await conn.fetchval(
            "SELECT $1::timestamptz < now() - make_interval(secs => $2)",
            row["approved_at"], float(rules["approval_max_age_seconds"])):
        return policy.deny("stale")
    if not await _approved_by_current_owner(conn, row):
        return policy.deny("owner_changed")
    return await policy.check_send(
        conn, mod.tg, rules, tgt, channel=row["channel"], text=row["text"], text_hash=row["text_hash"],
        origin=row["origin"], autoreply_daily_cap=cap, draft_id=row["id"],
        task_bound=row["task_id"] is not None)


async def deliver(mod: runtime.Outbox, draft_id: int) -> None:
    """Отправляет один согласованный черновик. Каждый шаг состояния — отдельная короткая транзакция."""
    pool = mod.state.pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM outbox_drafts WHERE id = $1", draft_id)
        if row is None or row["status"] != "approved":
            return
        rules = await policy.load(conn, mod.state.config)
        pause = float(rules["min_pause_seconds"])
        if row["origin"] == "autoreply":
            from . import autoreply

            pause = max(pause, float((await autoreply.load(conn))["pause_seconds"]))
        wait = await policy.pause_remaining(conn, row["account_id"], pause)
    if wait > 0:
        await asyncio.sleep(wait)

    mod.active.add(draft_id)
    try:
        async with pool.acquire() as conn, conn.transaction():
            await policy.lock_account(conn, row["account_id"])
            row = await conn.fetchrow("SELECT * FROM outbox_drafts WHERE id = $1 FOR UPDATE", draft_id)
            if row is None or row["status"] != "approved":
                return
            tgt = await policy.target(conn, row["chat_id"])
            if tgt is None:
                return
            # Правила читаются заново: пока ждали паузу, их могли изменить, а выключатель — выключить.
            rules = await policy.load(conn, mod.state.config)
            decision = await _presend(conn, mod, rules, row, tgt)
            if not decision.ok:
                await finish(conn, row, tgt, "failed", code=decision.code, message=decision.message)
                return
            parts = policy.parts_of(row["channel"], row["text"])
            connection_id = None
            if row["channel"] == "business":
                connection_id = await policy.business_connection(conn, tgt.account_id)
            await conn.execute(
                """UPDATE outbox_drafts SET status = 'sending', claimed_at = now(), parts_total = $2,
                          business_connection_id = $3 WHERE id = $1""",
                draft_id, len(parts), connection_id)
            await policy.touch_account(conn, tgt.account_id)
            if row["channel"] == "business":
                # Задание ставится в той же транзакции, что и переход в «отправляется».
                job_id = await bridge.request_business_send(
                    conn, handler=BUSINESS_HANDLER, business_connection_id=connection_id,
                    chat_id=tgt.tg_id, text=row["text"], reply_to_message_id=row["reply_to_tg_id"],
                    context={"draft_id": draft_id}, dedup_key=f"outbox:{draft_id}")
                await conn.execute("UPDATE outbox_drafts SET job_id = $2 WHERE id = $1", draft_id, job_id)
                return
        await _send_session(mod, row, tgt, parts, rules)
    finally:
        mod.active.discard(draft_id)


async def _send_session(mod: runtime.Outbox, row: Mapping[str, Any], tgt: Target,
                        parts: list[str], rules: dict[str, Any]) -> None:
    """Отправка через аккаунт-помощника. Длинный текст — несколькими сообщениями подряд с паузой."""
    pool, draft_id = mod.state.pool, row["id"]
    outcome: tuple[str, str | None, str | None] = ("sent", None, None)
    sent = 0
    for index, part in enumerate(parts):
        if index:
            await asyncio.sleep(float(rules["part_pause_seconds"]))
        # Все проверки — после паузы, прямо перед обращением к Telegram (Д-56): «Остановить всё», нажатое во
        # время паузы между частями, останавливает следующую часть.
        async with pool.acquire() as conn:
            decision = await part_check(conn, mod.state, draft_id, first=index == 0)
        if not decision.ok:
            outcome = ("failed", decision.code, decision.message)
            break
        if mod.state.config.sending is not True:
            outcome = ("failed", "sending_disabled", policy.REASONS["sending_disabled"])
            break
        tg = mod.tg
        if tg is None:
            outcome = ("failed", "session_not_configured", policy.REASONS["session_not_configured"])
            break
        try:
            thread = {"topic_tg_id": row["topic_tg_id"]} if row["topic_tg_id"] is not None else {}
            message_id = int(await asyncio.wait_for(
                tg.send_text(tgt.account_id, tgt.peer_class, tgt.tg_id, part,
                             reply_to_tg_id=row["reply_to_tg_id"] if index == 0 else None, **thread),
                timeout=float(rules["send_timeout_seconds"])))
        except FloodWait as exc:
            async with pool.acquire() as conn:
                await policy.touch_account(conn, tgt.account_id, blocked_for=exc.seconds)
            outcome = ("failed", "flood_wait", policy.REASONS["flood_wait"].format(seconds=exc.seconds))
            break
        except SendForbidden:
            outcome = ("failed", "send_forbidden", policy.REASONS["send_forbidden"])
            break
        except AccountUnavailable:
            outcome = ("failed", "session_unavailable", policy.REASONS["session_unavailable"])
            break
        except asyncio.CancelledError:
            raise  # сервис останавливается: строка останется «отправляется», при запуске её разберёт уборка
        except Exception as exc:
            # Запрос мог уйти: исход неизвестен, повторять нельзя.
            logger.warning("черновик %s: ответ об отправке не получен (%s)", draft_id, type(exc).__name__)
            outcome = ("outcome_unknown", "outcome_unknown", policy.REASONS["outcome_unknown"])
            break
        sent += 1
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """UPDATE outbox_drafts SET parts_sent = parts_sent + 1,
                          sent_tg_message_ids = array_append(sent_tg_message_ids, $2) WHERE id = $1""",
                draft_id, message_id)
            await policy.touch_account(conn, tgt.account_id)
    status, code, message = outcome
    if status == "failed" and sent:
        message = policy.REASONS["partial"].format(sent=sent, total=len(parts)) + " " + (message or "")
        code = "partial"
    async with pool.acquire() as conn, conn.transaction():
        await finish(conn, row, tgt, status, code=code, message=message)


# --- ответ бизнес-бота ---

async def _draft_of(conn: asyncpg.Connection, job: dict[str, Any]) -> tuple[Any, Target | None]:
    draft_id = job["context"].get("draft_id")
    if not isinstance(draft_id, int):
        return None, None
    row = await conn.fetchrow("SELECT * FROM outbox_drafts WHERE id = $1 FOR UPDATE", draft_id)
    return row, (await policy.target(conn, row["chat_id"]) if row is not None else None)


@bridge.on_result(BUSINESS_HANDLER)
async def _business_sent(conn: asyncpg.Connection, job: dict[str, Any], result: dict[str, Any]) -> None:
    row, tgt = await _draft_of(conn, job)
    if row is None or row["status"] not in ("sending", "outcome_unknown"):
        return
    message_id = result.get("message_id")
    if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
        # Плагин закрыл задание без номера сообщения: подтверждения доставки нет.
        if row["status"] == "sending":
            await finish(conn, row, tgt, "outcome_unknown", code="outcome_unknown",
                         message=policy.REASONS["outcome_unknown"])
        return
    await conn.execute(
        "UPDATE outbox_drafts SET parts_sent = 1, sent_tg_message_ids = ARRAY[$2::bigint] WHERE id = $1",
        row["id"], message_id)
    late = row["status"] == "outcome_unknown"
    await finish(conn, row, tgt, "sent", tell_owner=not late)
    if late:
        who = tgt.display_name if tgt is not None else "чат"
        await bridge.notify_owner(
            conn, f"Пришло подтверждение: сообщение дошло — {who} (черновик № {row['id']}).",
            silent=True, dedup_key=f"outbox:{row['id']}:confirmed")


@bridge.on_failure(BUSINESS_HANDLER)
async def _business_failed(conn: asyncpg.Connection, job: dict[str, Any], error: str) -> None:
    row, tgt = await _draft_of(conn, job)
    if row is None or row["status"] != "sending":
        return
    if error.startswith(NOT_SENT_PREFIX):
        await finish(conn, row, tgt, "failed", code="business_rejected", message=policy.REASONS["business_rejected"])
    else:
        await finish(conn, row, tgt, "outcome_unknown", code="outcome_unknown",
                     message=policy.REASONS["outcome_unknown"])


# --- уборка ---

async def sweep(mod: runtime.Outbox) -> dict[str, int]:
    """Закрывает просроченное и зависшее. Ничего не отправляет и не повторяет."""
    counts = {"expired": 0, "unknown": 0, "not_sent": 0}
    async with mod.state.pool.acquire() as conn:
        rules = await policy.load(conn, mod.state.config)
        async with conn.transaction():
            overdue = await conn.fetch(
                """SELECT * FROM outbox_drafts WHERE status = 'pending' AND expires_at <= now()
                   FOR UPDATE SKIP LOCKED""")
            for row in overdue:
                # Кнопки под карточкой просроченного черновика гаснут вместе с ним.
                await finish(conn, row, await policy.target(conn, row["chat_id"]), "expired", tell_owner=False)
            counts["expired"] = len(overdue)
        counts["purged"] = await purge_texts(conn, rules)
        counts["removed"] = await drop_excluded(conn)
        # Сессия: «отправляется» без живой отправки в этом процессе — след прерванной работы.
        stuck = await conn.fetch(
            "SELECT id FROM outbox_drafts WHERE status = 'sending' AND channel = 'session' AND id <> ALL($1::bigint[])",
            list(mod.active))
        # Бизнес-бот: задание никто не забрал, оно оборвалось или ответа нет слишком долго.
        late = await conn.fetch(
            """SELECT d.id, j.id AS job_id, j.status AS job_status,
                      d.claimed_at < now() - make_interval(secs => $1) AS overdue
               FROM outbox_drafts d LEFT JOIN jobs j ON j.id = d.job_id
               WHERE d.status = 'sending' AND d.channel = 'business' AND d.id <> ALL($2::bigint[])""",
            float(rules["send_timeout_seconds"]), list(mod.active))
        for item in [*stuck, *late]:
            async with conn.transaction():
                row = await conn.fetchrow("SELECT * FROM outbox_drafts WHERE id = $1 FOR UPDATE", item["id"])
                if row is None or row["status"] != "sending":
                    continue
                tgt = await policy.target(conn, row["chat_id"])
                job_status = item.get("job_status") if row["channel"] == "business" else None
                if row["channel"] == "business":
                    if job_status in ("queued", "running") and not item["overdue"]:
                        continue
                    if job_status == "queued":
                        # Задание ещё не взято: снимаем его, чтобы оно не ушло с опозданием.
                        if not await jobs.cancel(conn, item["job_id"], "снято: исполнитель не забрал вовремя"):
                            continue
                        await finish(conn, row, tgt, "failed", code="executor_absent",
                                     message=policy.REASONS["executor_absent"])
                        counts["not_sent"] += 1
                        continue
                await finish(conn, row, tgt, "outcome_unknown", code="outcome_unknown",
                             message=policy.REASONS["outcome_unknown"])
                counts["unknown"] += 1
    await dispatch(mod)
    return counts


# --- срок хранения текста ---

FINAL = ("sent", "failed", "outcome_unknown", "rejected", "expired", "superseded", "placed")


async def purge_texts(conn: asyncpg.Connection, rules: dict[str, Any]) -> int:
    """Стирает текст давно завершённых черновиков. Остаются состояние, номера сообщений и отпечаток.
    «Черновик ответа» сервиса (`suggest`, Р-105) — через час после завершения, а не через
    `text_retention_days`: его текст нужен, только пока владелец решает."""
    done = await conn.execute(
        """UPDATE outbox_drafts SET text = '', text_purged_at = now()
           WHERE status = ANY($1::text[]) AND text_purged_at IS NULL
             AND (COALESCE(finished_at, created_at) < now() - make_interval(days => $2)
                  OR origin = 'suggest' AND COALESCE(finished_at, created_at) < now() - make_interval(secs => $3))""",
        list(FINAL), int(rules["text_retention_days"]), float(SUGGEST_TEXT_KEEP_SECONDS))
    return int(done.split()[-1])


async def drop_excluded(conn: asyncpg.Connection, chat_id: int | None = None) -> int:
    """Чат исключён владельцем: его черновики и совпадения наблюдателя удаляются вместе с текстом.

    Согласованный, но ещё не начатый (`approved`) удаляется тоже: исключение чата отзывает согласие (Д-55).
    Черновик, отправка которого уже идёт (`sending`), остаётся до завершения — иначе пропал бы след
    возможной отправки; саму отправку останавливает сверка перед обращением к Telegram (`final_check`,
    `part_check`: чат исключён), а следующая уборка удалит и его.
    """
    async with conn.transaction():
        rows = await conn.fetch(
            """SELECT d.* FROM outbox_drafts d JOIN chats c ON c.id = d.chat_id
               WHERE c.excluded AND d.status <> 'sending'
                 AND ($1::bigint IS NULL OR d.chat_id = $1)
               FOR UPDATE OF d""", chat_id)
        for row in rows:
            # Текст есть ещё в карточках у владельца и в неотправленных заданиях на карточку.
            await refresh_cards(conn, row, None,
                                replace_with=f"Черновик № {row['id']}: не отправлен — чат исключён, текст удалён.")
            queued = await conn.fetch(
                "SELECT id FROM jobs WHERE kind = $1 AND status = 'queued' AND dedup_key LIKE $2",
                bridge.NOTIFY_OWNER, f"outbox:card:{row['id']}:%")
            for job in queued:
                await jobs.cancel(conn, job["id"], "снято: чат исключён")
        if rows:
            await conn.execute("DELETE FROM outbox_drafts WHERE id = ANY($1::bigint[])", [r["id"] for r in rows])
        await conn.execute(
            """DELETE FROM watch_hits h USING chats c
               WHERE c.id = h.chat_id AND c.excluded AND ($1::bigint IS NULL OR h.chat_id = $1)""", chat_id)
        await conn.execute(
            """DELETE FROM outbox_autoreply_log l USING chats c
               WHERE c.id = l.chat_id AND c.excluded AND ($1::bigint IS NULL OR l.chat_id = $1)""", chat_id)
    return len(rows)


async def sent_by_service(conn: asyncpg.Connection, chat_id: int, tg_message_ids: Any) -> set[int]:
    """Какие из сообщений чата (по идентификаторам Telegram) отправил сам сервис: согласованные
    черновики агента и автоответы. Нужно другим модулям, чтобы не принять текст ассистента за
    собственные слова владельца."""
    wanted = [int(i) for i in tg_message_ids]
    if not wanted:
        return set()
    rows = await conn.fetch(
        """SELECT DISTINCT sent_id FROM outbox_drafts d, unnest(d.sent_tg_message_ids) AS sent_id
           WHERE d.chat_id = $1 AND d.sent_tg_message_ids && $2::bigint[] AND sent_id = ANY($2::bigint[])""",
        chat_id, wanted)
    return {r["sent_id"] for r in rows}


async def run_sender(mod: runtime.Outbox) -> None:
    """Фоновая работа: будится нажатием и сама просыпается раз в секунду."""
    while True:
        try:
            await asyncio.wait_for(mod.wake.wait(), timeout=POLL_SECONDS)
        except asyncio.TimeoutError:
            pass
        mod.wake.clear()
        try:
            await dispatch(mod)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("отправщик: ошибка при раздаче черновиков")


async def settle(mod: runtime.Outbox) -> None:
    """Дожидается, пока все согласованные черновики будут разобраны (для тестов и остановки)."""
    while True:
        await dispatch(mod)
        tasks = [t for t in mod.workers.values() if not t.done()]
        if not tasks:
            async with mod.state.pool.acquire() as conn:
                if not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM outbox_drafts WHERE status = 'approved')"):
                    return
            continue
        await asyncio.gather(*tasks, return_exceptions=True)

