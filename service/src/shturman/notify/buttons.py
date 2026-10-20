"""Кнопки под уведомлением «💬 Вам написали» и «📣 Вас упомянули» (Б14, решение Р-89).

  «Не сообщать об этом чате»  — ужесточение: сразу. Чат попадает в `muted_chats` настроек уведомлений
                                (`notify.settings`); вернуть его — в «Настройках Штурмана», раздел
                                «Уведомления» (это расширение, в приложении и в боте его нет);
  «Напомнить через час»       — та же строка очереди `notify_items` снова «ждёт» со сроком через час
                                (`reminder`); уходит, только если вы к тому времени не ответили в этом чате;
  «Подготовить ответ»         — только у личных сообщений, в любом способе работы (Р-105): «Черновик
                                ответа» на это сообщение (`rehearsal.request_once`) — даже если человека
                                нет в списке; ничего не отправляется, входит в дневной предел черновиков.
    Если ответ на это сообщение уже готовит автоответ доверенным (`autoreply.claim`, Д-31), второй не
    готовится: владелец узнаёт, где черновик автоответа или что он придёт.
    Остановка ассистента (Р-87) «Подготовить ответ» не пропускает.
    Ответ готовится на всю пачку (packs.py, Р-93): пересланное в ней — цитата третьего лица, вложения
    пачки сначала разбираются (до `packs.MEDIA_WAIT`). Если ответ на сообщение подготовить нельзя
    заранее — скрыто защитой, не личное, нечем отвечать, — кнопки «Подготовить ответ» нет
    (`can_prepare`); если обстоятельства изменились после уведомления — ответ называет точную причину.

  «🎙 Ответить голосом»     — только у личных сообщений и со своим ботом (Р-103, dictation/): владелец
                                наговаривает или набрасывает ответ, сервис записывает его грамотно и
                                показывает карточкой; ничего не отправляется.

Кнопка несёт номер строки очереди и её случайную метку (`notify_items.nonce`): чужая или устаревшая
кнопка ничего не делает. Нажатие принимается только от привязанного владельца в его личном чате с
ботом (`bridge.dispatch_callback`). Текст сообщения кнопки не несут.

Черновик по «Подготовить ответ» готовится тем же запросом к модели, что автоответ
(`autoreply.gather_context`, `autoreply.build_messages`, `replies.workflow.model_request`); кнопки под ним —
по `replies/delivery.py`: отправить можно только нажатием владельца «Отправить».
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
REMIND_AFTER = 3600          # секунд — «Напомнить через час»
MUTE = "Не сообщать об этом чате"
REMIND = "Напомнить через час"
PREPARE = "Подготовить ответ"
MUTED_TEXT = f"Готово: об этом чате больше не сообщаю. Вернуть — {words.IN_SETUP_PAGE}, раздел «Уведомления»."
REFUSED = {"answer": "Кнопка недоступна.", "edit_text": None, "remove_buttons": False}


def buttons(item_id: int, nonce: str, *, personal: bool, prepare: bool = True) -> list[list[dict[str, str]]]:
    rows = [[bridge.button(PREPARE, CALLBACK_MODULE, f"p:{item_id}:{nonce}")]] if personal and prepare else []
    if personal:
        # «Ответить голосом» (Р-103, dictation/): владелец наговаривает ответ, сервис записывает его
        # грамотно и показывает — ничего не отправляя.
        from ..dictation import core as dictation
        rows += dictation.voice_row("n", item_id, nonce)
    rows.append([bridge.button(REMIND, CALLBACK_MODULE, f"l:{item_id}:{nonce}"),
                 bridge.button(MUTE, CALLBACK_MODULE, f"m:{item_id}:{nonce}")])
    return rows


async def attach(conn: asyncpg.Connection, item_id: int, *, personal: bool,
                 prepare: bool = True) -> list[list[dict[str, str]]]:
    """Новая метка строки очереди и кнопки с ней — для уведомления об одном чате."""
    nonce = secrets.token_urlsafe(9)
    await conn.execute("UPDATE notify_items SET nonce = $2 WHERE id = $1", item_id, nonce)
    return buttons(item_id, nonce, personal=personal, prepare=prepare)


async def can_prepare(conn: asyncpg.Connection, message_id: int) -> str | None:
    """Почему «Подготовить ответ» на это сообщение (и его пачку) невозможен — код, тогда кнопки нет;
    None — кнопка будет (`rehearsal.why_not_once`). Временное — остановка ассистента, дневной предел — кнопку
    не убирает: на нажатие владелец узнает точную причину и как её снять."""
    from ..rehearsal import core as rehearsal
    return await rehearsal.why_not_once(conn, message_id)


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

    personal = item["kind"] == "personal"
    prepare = personal and await can_prepare(conn, item["message_id"]) is None
    left = [row for row in buttons(item["id"], item["nonce"], personal=personal, prepare=prepare)]
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
    """«Подготовить ответ» — у уведомления и у карточки разбора входящих (Р-92): «Черновик ответа» на это
    сообщение, в любом способе работы (Р-105). Ничего не отправляет."""
    from .. import halt
    from ..rehearsal import core as rehearsal
    state = _state()
    if state is None:
        return {**REFUSED, "answer": "Сервис сейчас не готов. Попробуйте через минуту."}
    if not personal:
        return {**REFUSED, "answer": "Подготовить ответ можно только на личное сообщение."}
    if await halt.is_stopped(conn):
        return {"answer": "Ассистент остановлен — ответ не готовится.", "edit_text": None, "remove_buttons": False,
                "reply": {"text": "Ассистент остановлен: ответ не готовится. Продолжить — /resume."}}
    out = await rehearsal.request_once(state, message_id)
    reply: dict[str, Any] = {"text": out["text"]}
    if out.get("buttons"):
        # Кнопки сервиса под ответом (дневной предел — «Поднять до …»): бот шлёт ответ сам, парами.
        reply["buttons"] = [[(b["text"], b["data"]) for b in row] for row in out["buttons"]]
    answer = out.get("answer") or ("Готовлю." if out["ok"] else "Не получилось.")
    return {"answer": answer, "edit_text": None, "remove_buttons": False, "reply": reply}


# --- совместимость с 0.0.11: задания «Подготовить ответ» прежнего вида -----------------------------------
# До Р-105 «Подготовить ответ» в способе «Через бота и помощника» ставил задание модели с обработчиком
# `notify.draft` (черновик «только для просмотра»). Задание, поставленное до обновления, может вернуться уже
# после него: обработчика у него больше нет, и итог пропал бы молча. Здесь он не теряется — владелец узнаёт,
# что нажать ещё раз. Удалить вместе с этим блоком в выпуске после 0.0.12.
LEGACY_DRAFT_HANDLER = "notify.draft"
LEGACY_DRAFT_TEXT = ("Не получилось подготовить черновик ответа: запрос был отправлен до обновления. "
                     "Нажмите «Подготовить ответ» под уведомлением ещё раз.")


@bridge.on_result(LEGACY_DRAFT_HANDLER)
async def _legacy_draft_ready(conn: asyncpg.Connection, job: dict[str, Any], result: dict[str, Any]) -> None:
    # Ответ модели прочитан — второй его копии в задании не остаётся; текст ответа нигде не сохраняется.
    await conn.execute("UPDATE jobs SET payload = '{}'::jsonb, result = NULL WHERE id = $1", job["id"])
    await bridge.notify_owner(conn, LEGACY_DRAFT_TEXT, dedup_key=f"notify-draft-legacy:{job['id']}")


@bridge.on_failure(LEGACY_DRAFT_HANDLER)
async def _legacy_draft_failed(conn: asyncpg.Connection, job: dict[str, Any], error: str) -> None:
    await bridge.notify_owner(conn, LEGACY_DRAFT_TEXT, dedup_key=f"notify-draft-legacy:{job['id']}")
