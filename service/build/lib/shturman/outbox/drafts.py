"""Черновики и отправка: карточка владельцу, нажатие кнопки, сама отправка, уборка зависшего.

Путь черновика:

    pending ──нажатие «Отправить»──► approved ──отправщик──► sending ──► sent
       │                                 │                      ├──────► failed
       ├─► rejected  (нажатие «Отклонить» или отмена)           └──────► outcome_unknown
       ├─► expired   (владелец не ответил в срок)               (ответ не пришёл; повтора нет,
       └─► superseded (для чата подготовлен новый)               позднее подтверждение → sent)
                                         └─► failed (перед отправкой правила уже не разрешают)

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
import logging
import secrets
from typing import Any, Mapping

import asyncpg

from .. import bridge, jobs
from ..tg.gateway import AccountUnavailable, FloodWait, SendForbidden
from . import policy, runtime
from . import text as textlib
from .policy import Decision, Target

logger = logging.getLogger("shturman.outbox")

CALLBACK_MODULE = "ob"
CARD_HANDLER = "outbox.card"
BUSINESS_HANDLER = "outbox.sent"
CARD_LIMIT = 3900          # знаков на одну карточку вместе с шапкой (предел Telegram — 4096)
POLL_SECONDS = 1.0         # как часто отправщик сам проверяет, нет ли согласованных черновиков
# Плагин ставит эту приставку в текст ошибки, когда Telegram ответил отказом и сообщение точно
# не ушло. Любая другая ошибка отправки через бизнес-бота считается исходом «неизвестно».
NOT_SENT_PREFIX = "not_sent:"

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
        "text": row["text"], "reply_to_tg_id": row["reply_to_tg_id"],
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
    if channel == "business":
        return "от вашего имени (через бизнес-бота)"
    return f"от имени помощника (аккаунт «{textlib.one_line(tgt.account_label, 40)}»)"


def card_parts(row: Mapping[str, Any], tgt: Target, *, status: str | None = None,
               note: str | None = None, reply_excerpt: str | None = None) -> list[str]:
    """Текст карточки. Весь текст сообщения показывается дословно; длинный — в нескольких
    карточках подряд, без сокращений. Текст идёт последним: после него в карточке ничего нет."""
    word = STATUS_WORDS[status or row["status"]]
    sends = len(policy.parts_of(row["channel"], row["text"]))
    head = [
        f"Кому: {tgt.display_name} ({_chat_kind(tgt)})",
        f"От кого: {_voice(tgt, row['channel'])}",
    ]
    if reply_excerpt:
        head.append(f"В ответ на: «{textlib.one_line(reply_excerpt, 100)}»")
    if sends > 1:
        head.append(f"Текст длинный: уйдёт {sends} сообщениями подряд.")
    if note:
        head.append(textlib.one_line(note, 300))
    tail = f"Знаков: {len(row['text'])}. Всё, что ниже этой строки, — текст сообщения дословно."
    budget = CARD_LIMIT - 200 - sum(textlib.utf16_len(line) + 1 for line in head) - textlib.utf16_len(tail)
    chunks = textlib.split_text(row["text"], budget) or [row["text"]]
    total = len(chunks)
    cards = []
    for index, chunk in enumerate(chunks, 1):
        title = f"Черновик № {row['id']} — {word}"
        if total > 1:
            title += f" · часть {index} из {total}"
        if index == 1:
            lines = [title, *head]
            if total > 1:
                lines.append(f"Текст показан в {total} сообщениях подряд; кнопки — под последним.")
            lines.append(tail)
        else:
            lines = [title, "Продолжение текста сообщения, дословно:"]
        cards.append("\n".join(lines) + "\n\n" + chunk)
    return cards


def _buttons(row: Mapping[str, Any]) -> list[list[dict[str, str]]]:
    rest = f"{row['id']}:{row['nonce']}"
    return [[bridge.button("Отправить", CALLBACK_MODULE, f"s:{rest}"),
             bridge.button("Отклонить", CALLBACK_MODULE, f"r:{rest}")]]


async def _reply_excerpt(conn: asyncpg.Connection, row: Mapping[str, Any]) -> str | None:
    if row["reply_to_tg_id"] is None:
        return None
    return await conn.fetchval(
        "SELECT text FROM messages WHERE chat_id = $1 AND tg_message_id = $2",
        row["chat_id"], row["reply_to_tg_id"])


async def _last_card(conn: asyncpg.Connection, row: Mapping[str, Any], tgt: Target, *,
                     status: str | None = None, note: str | None = None) -> str:
    """Карточка с кнопками (последняя) в новом состоянии — ею заменяется текст после нажатия."""
    return card_parts(row, tgt, status=status, note=note,
                      reply_excerpt=await _reply_excerpt(conn, row))[-1]


@bridge.on_result(CARD_HANDLER)
async def _card_delivered(conn: asyncpg.Connection, job: dict[str, Any], result: dict[str, Any]) -> None:
    """Запоминает, каким сообщением карточка пришла владельцу."""
    draft_id, message_id = job["context"].get("draft_id"), result.get("message_id")
    if isinstance(draft_id, int) and isinstance(message_id, int) and not isinstance(message_id, bool):
        await conn.execute(
            "UPDATE outbox_drafts SET card_message_ids = array_append(card_message_ids, $2) WHERE id = $1",
            draft_id, message_id)


# --- создание ---

async def create(
    conn: asyncpg.Connection, tg: Any, *, chat_id: int, text: str, channel: str | None = None,
    reply_to_message_id: int | None = None, idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Создаёт черновик и ставит карточку владельцу. Отказ — исключение `Refused` с причиной.

    `reply_to_message_id` — идентификатор строки архива (messages.id) в том же чате.
    """
    def refuse(decision: Decision) -> Refused:
        return Refused(decision, _status_for(decision))

    cleaned = textlib.clean_outgoing(text)
    text_hash = textlib.content_hash(cleaned)
    async with conn.transaction():
        tgt = await policy.target(conn, chat_id)
        if tgt is None:
            raise refuse(policy.deny("chat_not_found"))
        await policy.lock_chat(conn, chat_id)
        if idempotency_key is not None:
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('shturman.outbox.key'), hashtext($1))",
                               idempotency_key)
            known = await conn.fetchrow("SELECT * FROM outbox_drafts WHERE idempotency_key = $1", idempotency_key)
            if known is not None:
                same = (known["chat_id"] == chat_id and known["text_hash"] == text_hash
                        and channel in (None, known["channel"]))
                if not same:
                    raise refuse(policy.deny("idempotency_conflict"))
                return public(known, replayed=True)
        if await bridge.get_owner(conn) is None:
            raise refuse(policy.deny("owner_unknown"))
        rules = await policy.load(conn)
        picked, decision = policy.pick_channel(tgt, channel)
        if picked is None:
            raise refuse(decision)
        for decision in (await policy.check_target(conn, rules, tgt),
                         await policy.check_channel(conn, tg, rules, tgt, picked),
                         policy.check_text(rules, picked, cleaned)):
            if not decision.ok:
                raise refuse(decision)
        # Из лимитов при создании важен только повтор: остальные проверяются в момент отправки.
        if await policy.is_duplicate(conn, rules, tgt, text_hash):
            raise refuse(policy.deny("duplicate_text", temporary=True,
                                     retry_after=int(rules["duplicate_window_seconds"])))
        reply_to_tg_id = None
        if reply_to_message_id is not None:
            reply_to_tg_id = await conn.fetchval(
                "SELECT tg_message_id FROM messages WHERE id = $1 AND chat_id = $2 AND deleted_at IS NULL",
                reply_to_message_id, chat_id)
            if reply_to_tg_id is None:
                raise refuse(policy.deny("reply_not_found"))
        waiting = await conn.fetchrow(
            "SELECT * FROM outbox_drafts WHERE chat_id = $1 AND status = 'pending' AND expires_at > now()", chat_id)
        if (waiting is not None and waiting["text_hash"] == text_hash and waiting["channel"] == picked
                and waiting["reply_to_tg_id"] == reply_to_tg_id and idempotency_key is None):
            return public(waiting, duplicate=True)
        recent = await conn.fetchval(
            """SELECT count(*) FROM outbox_drafts
               WHERE account_id = $1 AND origin = 'agent' AND created_at > now() - interval '1 hour'""",
            tgt.account_id)
        if recent >= rules["drafts_per_hour"]:
            raise refuse(policy.deny("limit_drafts", temporary=True, cap=rules["drafts_per_hour"]))
        # В чате ждёт решения только один черновик: прежний заменяется, его кнопки перестают работать.
        await conn.execute(
            """UPDATE outbox_drafts SET status = 'superseded', finished_at = now()
               WHERE chat_id = $1 AND status = 'pending'""", chat_id)
        parts = policy.parts_of(picked, cleaned)
        row = await conn.fetchrow(
            """INSERT INTO outbox_drafts (account_id, chat_id, channel, text, text_hash, reply_to_tg_id,
                                          origin, idempotency_key, nonce, status, expires_at, parts_total)
               VALUES ($1, $2, $3, $4, $5, $6, 'agent', $7, $8, 'pending',
                       now() + make_interval(secs => $9), $10)
               RETURNING *""",
            tgt.account_id, chat_id, picked, cleaned, text_hash, reply_to_tg_id, idempotency_key,
            secrets.token_urlsafe(16), float(rules["draft_ttl_seconds"]), len(parts))
        cards = card_parts(row, tgt, reply_excerpt=await _reply_excerpt(conn, row))
        for index, card in enumerate(cards, 1):
            await bridge.notify_owner(
                conn, card, buttons=_buttons(row) if index == len(cards) else None,
                handler=CARD_HANDLER, context={"draft_id": row["id"], "part": index},
                dedup_key=f"outbox:card:{row['id']}:{index}")
        return public(row, text_changed=cleaned != text, cards=len(cards))


async def create_autoreply(
    conn: asyncpg.Connection, tgt: Target, *, channel: str, text: str, trigger_message_id: int,
) -> int | None:
    """Записывает автоответ сразу согласованным. На одно входящее — не больше одной записи."""
    return await conn.fetchval(
        """INSERT INTO outbox_drafts (account_id, chat_id, channel, text, text_hash, origin,
                                      trigger_message_id, nonce, status, expires_at, approved_at, parts_total)
           VALUES ($1, $2, $3, $4, $5, 'autoreply', $6, $7, 'approved', now() + interval '1 hour', now(), $8)
           ON CONFLICT (trigger_message_id) WHERE origin = 'autoreply' AND trigger_message_id IS NOT NULL
           DO NOTHING
           RETURNING id""",
        tgt.account_id, tgt.chat_id, channel, text, textlib.content_hash(text), trigger_message_id,
        secrets.token_urlsafe(16), len(policy.parts_of(channel, text)))


async def cancel(conn: asyncpg.Connection, draft_id: int) -> dict[str, Any] | None:
    """Отменяет черновик, пока владелец не принял решение. Возвращает черновик или None, если такого нет."""
    async with conn.transaction():
        row = await conn.fetchrow("SELECT * FROM outbox_drafts WHERE id = $1 FOR UPDATE", draft_id)
        if row is None:
            return None
        if row["status"] != "pending":
            return public(row, cancelled=False)
        row = await conn.fetchrow(
            """UPDATE outbox_drafts SET status = 'rejected', error_code = 'cancelled', error_text = $2,
                      finished_at = now() WHERE id = $1 RETURNING *""",
            draft_id, policy.REASONS["cancelled"])
        return public(row, cancelled=True)


# --- завершение и сообщение владельцу ---

async def finish(
    conn: asyncpg.Connection, row: Mapping[str, Any], tgt: Target | None, status: str, *,
    code: str | None = None, message: str | None = None, tell_owner: bool = True,
) -> bool:
    """Переводит черновик в конечное состояние и сообщает владельцу. Повторный вызов ничего не делает."""
    done = await conn.fetchrow(
        """UPDATE outbox_drafts SET status = $2, error_code = $3, error_text = $4, finished_at = now()
           WHERE id = $1 AND status <> $2 RETURNING *""",
        row["id"], status, code, message)
    if done is None:
        return False
    mod = runtime.current()
    if mod is not None:
        mod.stop_typing(done["account_id"], done["chat_id"])
    if not tell_owner:
        return True
    auto = done["origin"] == "autoreply"
    who = tgt.display_name if tgt is not None else "чат"
    label = f"{who} ({'автоответ' if auto else 'черновик'} № {done['id']})"
    never_twice = "Само повторно не отправится: так сообщение не уйдёт дважды."
    if status == "sent":
        if auto:
            return True  # автоответы видны в списке отправленного, отдельно о каждом не сообщаем
        count = len(done["sent_tg_message_ids"])
        text = f"Отправлено: {label}." + (f" Сообщений: {count}." if count > 1 else "")
        silent = True
    elif status == "outcome_unknown":
        text = (f"Не удалось узнать, дошло ли сообщение: {label}. Откройте чат и проверьте. {never_twice} "
                "Если не дошло — попросите подготовить его заново.")
        silent = False
    else:
        if auto and code != "flood_wait":
            return True  # несостоявшийся автоответ — просто отсутствие ответа
        text = f"Не отправлено: {label}. {message or ''} {never_twice}".replace("  ", " ")
        silent = False
    await bridge.notify_owner(conn, text, silent=silent, dedup_key=f"outbox:{done['id']}:{status}")
    return True


# --- нажатие кнопки ---

@bridge.on_callback(CALLBACK_MODULE)
async def on_button(conn: asyncpg.Connection, rest: str, user_id: int) -> dict[str, Any]:
    """Нажатие под карточкой. Вызывается в транзакции; сети здесь нет — только занять черновик."""
    refused = {"answer": "Кнопка недоступна.", "edit_text": None, "remove_buttons": False}
    action, _, tail = rest.partition(":")
    raw_id, _, nonce = tail.partition(":")
    if action not in ("s", "r") or not raw_id.isascii() or not raw_id.isdigit() or len(raw_id) > 18:
        return refused
    owner = await bridge.get_owner(conn)
    if owner is None or int(owner["user_id"]) != int(user_id):
        return refused  # мост это уже проверил; вторая проверка — на случай ошибки выше
    row = await conn.fetchrow("SELECT * FROM outbox_drafts WHERE id = $1 FOR UPDATE", int(raw_id))
    expected = row["nonce"] if row is not None else secrets.token_urlsafe(16)
    if not hmac.compare_digest(nonce.encode(), expected.encode()) or row is None or row["origin"] != "agent":
        return refused
    tgt = await policy.target(conn, row["chat_id"])
    if tgt is None:
        return refused

    async def closed(status: str, answer: str, note: str | None = None) -> dict[str, Any]:
        return {"answer": answer, "remove_buttons": True,
                "edit_text": await _last_card(conn, row, tgt, status=status, note=note)}

    if row["status"] != "pending":
        return await closed(row["status"], ALREADY[row["status"]])
    if await conn.fetchval("SELECT $1::timestamptz <= now()", row["expires_at"]):
        await conn.execute("UPDATE outbox_drafts SET status = 'expired', finished_at = now() WHERE id = $1", row["id"])
        return await closed("expired", "Срок черновика истёк. Попросите подготовить новый.")
    if action == "r":
        await conn.execute("UPDATE outbox_drafts SET status = 'rejected', finished_at = now() WHERE id = $1", row["id"])
        return await closed("rejected", "Отклонено, ничего не отправлено.")

    mod = runtime.current()
    if mod is None:
        return {"answer": policy.REASONS["service_stopped"] + " Попробуйте позже.",
                "edit_text": None, "remove_buttons": False}
    rules = await policy.load(conn)
    decision = await policy.check_send(
        conn, mod.tg, rules, tgt, channel=row["channel"], text=row["text"], text_hash=row["text_hash"],
        draft_id=row["id"])
    if not decision.ok and decision.temporary:
        # Черновик остаётся ждать: когда препятствие уйдёт, кнопку можно нажать снова.
        return {"answer": "Пока нельзя: " + decision.message, "edit_text": None, "remove_buttons": False}
    await conn.execute(
        "UPDATE outbox_drafts SET status = 'approved', approved_at = now() WHERE id = $1 AND status = 'pending'",
        row["id"])
    if not decision.ok:
        await finish(conn, row, tgt, "failed", code=decision.code, message=decision.message, tell_owner=False)
        return await closed("failed", "Не отправлено: " + decision.message, note="Причина: " + decision.message)
    mod.kick()
    return await closed("approved", "Принято, отправляю.")


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


async def _presend(conn: asyncpg.Connection, mod: runtime.Outbox, rules: dict[str, Any],
                   row: Mapping[str, Any], tgt: Target) -> Decision:
    """Последняя проверка перед отправкой: за время ожидания всё могло измениться."""
    cap = None
    if row["origin"] == "autoreply":
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
    return await policy.check_send(
        conn, mod.tg, rules, tgt, channel=row["channel"], text=row["text"], text_hash=row["text_hash"],
        origin=row["origin"], autoreply_daily_cap=cap, draft_id=row["id"])


async def deliver(mod: runtime.Outbox, draft_id: int) -> None:
    """Отправляет один согласованный черновик. Каждый шаг состояния — отдельная короткая транзакция."""
    pool = mod.state.pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM outbox_drafts WHERE id = $1", draft_id)
        if row is None or row["status"] != "approved":
            return
        rules = await policy.load(conn)
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
        tg = mod.tg
        if tg is None:
            outcome = ("failed", "session_not_configured", policy.REASONS["session_not_configured"])
            break
        if index:
            await asyncio.sleep(float(rules["part_pause_seconds"]))
        try:
            message_id = int(await asyncio.wait_for(
                tg.send_text(tgt.account_id, tgt.peer_class, tgt.tg_id, part,
                             reply_to_tg_id=row["reply_to_tg_id"] if index == 0 else None),
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
        rules = await policy.load(conn)
        done = await conn.execute(
            """UPDATE outbox_drafts SET status = 'expired', finished_at = now()
               WHERE status = 'pending' AND expires_at <= now()""")
        counts["expired"] = int(done.split()[-1])
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
