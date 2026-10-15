"""Кнопки под уведомлением «💬 Вам написали» и «📣 Вас упомянули» (Б14, решение Р-89).

  «Не сообщать об этом чате»  — ужесточение: сразу. Чат попадает в `muted_chats` настроек уведомлений
                                (`notify.settings`); вернуть его — в «Настройках Штурмана», раздел
                                «Уведомления» (это расширение, в приложении и в боте его нет);
  «Напомнить через час»       — та же строка очереди `notify_items` снова «ждёт» со сроком через час
                                (`reminder`); уходит, только если вы к тому времени не ответили в этом чате;
  «Подготовить ответ»         — только у личных сообщений, по способу работы:
        «Через ваш аккаунт»       → разовая репетиция для этого сообщения (`rehearsal.request_once`):
                                    даже если человека нет в списке, ничего не отправляется, входит в
                                    дневной предел репетиции;
        «Через бота и помощника»  → черновик ответа только для просмотра — если черновики ответов
                                    включены (`/prepare on`); иначе — ответ, как включить.
    Остановка ассистента (Р-87) «Подготовить ответ» не пропускает.

Кнопка несёт номер строки очереди и её случайную метку (`notify_items.nonce`): чужая или устаревшая
кнопка ничего не делает. Нажатие принимается только от привязанного владельца в его личном чате с
ботом (`bridge.dispatch_callback`). Текст сообщения кнопки не несут.

Черновик по «Подготовить ответ» готовится тем же запросом к модели, что автоответ и репетиция
(`autoreply.gather_context`, `autoreply.build_messages`, `replies.workflow.model_request`), и
создаётся обычным черновиком «только для просмотра» (`drafts.create(prepare_only=True)`): прав отправки
он не добавляет и уйти не может.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from typing import Any

import asyncpg

from .. import authority, bridge, words
from . import settings as ns

logger = logging.getLogger("shturman.notify")

CALLBACK_MODULE = "nt"
DRAFT_HANDLER = "notify.draft"
REMIND_AFTER = 3600          # секунд — «Напомнить через час»
MUTE = "Не сообщать об этом чате"
REMIND = "Напомнить через час"
PREPARE = "Подготовить ответ"
MUTED_TEXT = f"Готово: об этом чате больше не сообщаю. Вернуть — {words.IN_SETUP_PAGE}, раздел «Уведомления»."
REFUSED = {"answer": "Кнопка недоступна.", "edit_text": None, "remove_buttons": False}


def buttons(item_id: int, nonce: str, *, personal: bool) -> list[list[dict[str, str]]]:
    rows = [[bridge.button(PREPARE, CALLBACK_MODULE, f"p:{item_id}:{nonce}")]] if personal else []
    rows.append([bridge.button(REMIND, CALLBACK_MODULE, f"l:{item_id}:{nonce}"),
                 bridge.button(MUTE, CALLBACK_MODULE, f"m:{item_id}:{nonce}")])
    return rows


async def attach(conn: asyncpg.Connection, item_id: int, *, personal: bool) -> list[list[dict[str, str]]]:
    """Новая метка строки очереди и кнопки с ней — для уведомления об одном чате."""
    nonce = secrets.token_urlsafe(9)
    await conn.execute("UPDATE notify_items SET nonce = $2 WHERE id = $1", item_id, nonce)
    return buttons(item_id, nonce, personal=personal)


def _state() -> Any:
    from ..outbox import runtime
    mod = runtime.current()
    return mod.state if mod is not None else None


@bridge.on_callback(CALLBACK_MODULE)
async def _pressed(conn: asyncpg.Connection, rest: str, user_id: int) -> dict[str, Any]:
    principal = authority.requires_owner()
    if principal.user_id != user_id or principal.source != "telegram":
        return REFUSED
    action, _, tail = rest.partition(":")
    raw_id, _, nonce = tail.partition(":")
    if action not in ("m", "l", "p") or not raw_id.isascii() or not raw_id.isdigit() or len(raw_id) > 18:
        return REFUSED
    item = await conn.fetchrow("SELECT * FROM notify_items WHERE id = $1 FOR UPDATE", int(raw_id))
    if item is None or not item["nonce"] or not hmac.compare_digest(item["nonce"], nonce):
        return {**REFUSED, "answer": "Это уведомление устарело."}
    if action == "m":
        return await _mute(conn, item)
    if action == "l":
        return await _remind(conn, item)
    return await _prepare(conn, item)


async def mute_chat(conn: asyncpg.Connection, chat_id: int) -> bool:
    """«Не сообщать об этом чате»: чат — в `muted_chats` (и уведомления, и разбор входящих его больше
    не показывают), ждущие уведомления чата снимаются. False — чат уже был в списке."""
    from .. import confirm
    from . import service as notify_service
    current = await ns.load(conn)
    if chat_id in current["muted_chats"]:
        return False
    async with conn.transaction():
        await confirm.apply_owner(conn, notify_service.SETTINGS,
                                  {"changes": {"muted_chats": current["muted_chats"] + [chat_id]}})
    await conn.execute(
        """UPDATE notify_items SET state = 'dropped', dropped_reason = 'disabled'
           WHERE chat_id = $1 AND state = 'waiting'""", chat_id)
    from ..setup_page import audit
    await audit.write_conn(conn, "notify.mute", detail=f"чат № {chat_id}; из бота")
    return True


async def _mute(conn: asyncpg.Connection, item: Any) -> dict[str, Any]:
    if not await mute_chat(conn, item["chat_id"]):
        return {"answer": "Об этом чате уже не сообщаю.", "edit_text": None, "remove_buttons": False}

    left = [row for row in buttons(item["id"], item["nonce"], personal=item["kind"] == "personal")]
    left[-1] = [b for b in left[-1] if b["text"] != MUTE]
    return {"answer": "Больше не сообщаю об этом чате.", "edit_text": None, "remove_buttons": False,
            "buttons": left, "reply": {"text": MUTED_TEXT}}


async def _remind(conn: asyncpg.Connection, item: Any) -> dict[str, Any]:
    if item["state"] == "waiting" and item["reminder"]:
        return {"answer": "Уже напомню — через час, если вы не ответите.", "edit_text": None, "remove_buttons": False}
    if item["state"] != "sent":
        return {"answer": "Это уведомление устарело.", "edit_text": None, "remove_buttons": False}
    await conn.execute(
        """UPDATE notify_items SET state = 'waiting', reminder = true, quiet = false, digest = false,
                  held_for_rehearsal = false, sent_at = NULL, dropped_reason = NULL,
                  due_at = now() + make_interval(secs => $2) WHERE id = $1""", item["id"], float(REMIND_AFTER))
    return {"answer": "Напомню через час, если вы не ответите в этом чате.", "edit_text": None,
            "remove_buttons": False}


async def _prepare(conn: asyncpg.Connection, item: Any) -> dict[str, Any]:
    return await prepare_reply(conn, item["message_id"], personal=item["kind"] == "personal")


async def prepare_reply(conn: asyncpg.Connection, message_id: int, *, personal: bool) -> dict[str, Any]:
    """«Подготовить ответ» — у уведомления и у карточки разбора входящих (Р-90): разовая репетиция или
    черновик только для просмотра, по способу работы. Ничего не отправляет."""
    from .. import halt
    from ..executor import owner_views
    state = _state()
    if state is None:
        return {**REFUSED, "answer": "Сервис сейчас не готов. Попробуйте через минуту."}
    if not personal:
        return {**REFUSED, "answer": "Подготовить ответ можно только на личное сообщение."}
    if await halt.is_stopped(conn):
        return {"answer": "Ассистент остановлен — ответ не готовится.", "edit_text": None, "remove_buttons": False,
                "reply": {"text": "Ассистент остановлен: ответ не готовится. Продолжить — /resume."}}
    mode = await owner_views.work_mode(conn)
    if mode == "staff":
        out = await request_draft(state, message_id)
    else:
        from ..rehearsal import core as rehearsal
        out = await rehearsal.request_once(state, message_id)
    return {"answer": "Готовлю." if out["ok"] else "Не получилось.", "edit_text": None, "remove_buttons": False,
            "reply": {"text": out["text"]}}


# --- черновик по «Подготовить ответ» (способ «Через бота и помощника») ------------------------------------

DRAFTS_OFF = (f"«{words.DRAFTS_MODE}» выключены — поэтому ответ не готовится. Включить — командой /prepare on: "
              "ассистент станет готовить ответы на новые сообщения и показывать их вам, ничего не отправляя.")


async def request_draft(state: Any, message_id: int) -> dict[str, Any]:
    """Просит модель подготовить ответ на это сообщение; результат — черновик «только для просмотра»."""
    from ..outbox import autoreply, policy
    if not getattr(state.config, "prepare_only", False):
        return {"ok": False, "text": DRAFTS_OFF}
    async with state.pool.acquire() as conn, conn.transaction():
        if not (await policy.check_halt(conn)).ok:
            return {"ok": False, "text": "Ассистент остановлен — ответ не готовится. Продолжить — /resume."}
        msg = await conn.fetchrow(
            """SELECT id, chat_id, text FROM messages WHERE id = $1 AND deleted_at IS NULL AND agent_visible
                 AND kind = 'message' AND is_outgoing IS NOT TRUE""", message_id)
        if msg is None or not (msg["text"] or "").strip():
            return {"ok": False, "text": "Не получилось: сообщение удалено, скрыто защитой или без текста."}
        tgt = await policy.target(conn, msg["chat_id"])
        if tgt is None or tgt.excluded:
            return {"ok": False, "text": "Не получилось: чат исключён из архива."}
        rules = await policy.load(conn, state.config)
        decision = await policy.check_target(conn, rules, tgt)
        if not decision.ok:
            return {"ok": False, "text": "Не получилось: " + decision.message}
        channel, decision = policy.pick_channel(tgt, None)
        if channel is None:
            return {"ok": False, "text": "Не получилось: " + decision.message}
        settings = await autoreply.load(conn)
        topic, recent, hits = await autoreply.gather_context(conn, state, settings, msg["chat_id"], message_id,
                                                             msg["text"])
        from ..notify import core as notify_core
        from ..replies import model, workflow
        messages = autoreply.build_messages(settings, tgt, channel, msg["text"], list(recent), hits,
                                            notify_core.zone(state.config.timezone))
        offered: list[int] = []
        for mid in [message_id] + [r["id"] for r in recent] + [h["id"] for h in hits]:
            if mid not in offered:
                offered.append(mid)
        refs = [{"kind": "chat", "source_id": str(msg["chat_id"]), "message_id": mid, "topic_tg_id": topic}
                for mid in offered]
        instructions, input_text = workflow.model_request(state, messages, refs)
        job = await bridge.request_structured(
            conn, handler=DRAFT_HANDLER, instructions=instructions, input=input_text, json_schema=model.SCHEMA,
            schema_name="reply_outcome", task="shturman_reply", max_tokens=4000,
            context={"chat_id": msg["chat_id"], "message_id": message_id, "offered": offered},
            dedup_key=f"notify-draft:{message_id}")
    if job is None:
        return {"ok": True, "text": "Ответ на это сообщение уже готовится — черновик придёт сюда."}
    return {"ok": True, "text": "Готовлю черновик ответа — пришлю карточкой. Он только для просмотра: "
                                "ничего не отправится."}


@bridge.on_result(DRAFT_HANDLER)
async def _draft_ready(conn: asyncpg.Connection, job: dict[str, Any], result: dict[str, Any]) -> None:
    await conn.execute("UPDATE jobs SET payload = '{}'::jsonb, result = NULL WHERE id = $1", job["id"])
    from ..outbox import autoreply, drafts, policy
    from ..replies import model
    ctx = job.get("context") or {}
    chat_id, message_id = ctx.get("chat_id"), ctx.get("message_id")
    state = _state()
    if state is None or not isinstance(chat_id, int) or not isinstance(message_id, int):
        return
    if not (await policy.check_halt(conn)).ok:
        return          # пока модель думала, владелец остановил ассистента
    settings = await autoreply.load(conn)
    try:
        outcome = model.parse(result.get("parsed"), [{"message_id": i} for i in ctx.get("offered") or []],
                              max_chars=settings["max_reply_chars"] + 500)
    except (ValueError, TypeError, KeyError):
        await bridge.notify_owner(conn, "Черновик ответа не подготовлен: модель вернула непонятный ответ.")
        return
    if outcome.kind != "reply":
        why = {"decline": "ассистент решил, что отвечать на это сообщение не нужно.",
               "ask_owner": "ассистенту не хватило сведений — он спросил бы вас: «"
                            + outcome.question[:300] + "».",
               "need_source": "ассистенту нужны сведения из другого источника — по кнопке они не читаются."}
        await bridge.notify_owner(conn, "Черновик ответа не подготовлен: " + why.get(outcome.kind, "нет ответа."))
        return
    try:
        await drafts.create(conn, state, chat_id=chat_id, text=outcome.text, reply_to_message_id=message_id,
                            idempotency_key=f"notify-draft:{message_id}", prepare_only=True)
    except drafts.Refused as exc:
        await bridge.notify_owner(conn, "Черновик ответа не подготовлен: " + exc.decision.message)


@bridge.on_failure(DRAFT_HANDLER)
async def _draft_failed(conn: asyncpg.Connection, job: dict[str, Any], error: str) -> None:
    await bridge.notify_owner(conn, "Черновик ответа не подготовлен: модель не ответила. " + bridge.model_hint())
