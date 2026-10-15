"""«Остановить всё» и «Продолжить» (Х2, решение Р-87).

Один флаг на сервер — строка `assistant_halt` (миграция 0035). Пока он стоит, ассистент ничего не
делает сам. Его проверяет каждая точка, где ассистент действует без нажатия владельца:

  | Точка | Где | Что делает при остановке |
  |---|---|---|
  | задачи ответа и вопросы ассистента | `replies/workflow.py` (`register`, `queue`, `notify_waiting`) | не заводятся, не продолжаются |
  | автоответ доверенным и черновики ответов (`/prepare`) | `outbox/autoreply.py` | не готовятся |
  | черновик от ассистента | `outbox/drafts.py` (`create`, `create_autoreply`) | отказ «Владелец остановил ассистента» |
  | отправка — даже если включена на сервере | `outbox/drafts.py` (`on_button`, `deliver`), `policy.check_send`, исполнитель `business.send` | не уходит |
  | репетиция | `rehearsal/core.py` | не готовится, карточка не уходит |
  | наблюдатель | `outbox/watcher.py` | срабатывания не разбираются, сообщения не уходят |
  | подтверждения действий от ассистента и внутреннего API | `confirm.request` | новые отклоняются сразу, с причиной |
  | «Подготовить ответ» у уведомления | `notify/buttons.py` | отказ |

Вторая линия — триггеры базы (миграция 0035): пока флаг стоит, в `reply_tasks`, `outbox_drafts`,
`rehearsal_items`, `watch_hits`, `pending_actions` (кроме карточек «Продолжить» и входа по коду) не
добавляется ни строки, черновик не переходит в «согласован» и «отправляется», задание `business.send`
не ставится — даже если какую-то точку код пропустил.

Флаг сильнее настроек, а сами настройки не трогаются: после «Продолжить» возвращается ровно то, что
было. Что продолжает работать: сбор переписки в архив, уведомления владельцу о новых сообщениях (это не
действие ассистента), разбор договорённостей и памяти и их карточки, чтение архива ассистентом по MCP.
Отдельная галочка «И перестать читать переписку» ставит все аккаунты на паузу (той же паузой, что на
странице); «Продолжить» её не снимает, если владелец не отметит это отдельно.

В момент остановки отменяется то, что уже ждёт: задачи ответа и вопросы ассистента, неотправленные
черновики, ждущие подтверждения действий, начатые репетиции, вопрос «напишите ответ» в боте.

Кто и как:
  * «Остановить всё» — ужесточение: сразу, без подтверждения, со страницы (`setup_page/halt.py`), из
    приложения и в боте согласований (`/pause`, `/stop`, кнопка «Пауза»);
  * «Продолжить» — ослабление: только нажатием владельца в боте согласований. Из бота — сообщение со
    списком того, что снова заработает, и кнопками (`CALLBACK_MODULE`); со страницы и из приложения —
    подтверждение действия `halt.resume` (`confirm.request_card`). Внутреннего API у флага нет вовсе.
  Нажатие «Продолжить» в приложении «Штурман» не принимается (`BOT_ONLY` в `confirm.py`): войти в
  приложение может и держатель токена бота согласований (Р-80), а нажатие в самом боте подделать нельзя.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from datetime import datetime
from typing import Any

import asyncpg

from . import authority, bridge, words

logger = logging.getLogger("shturman.halt")

CODE = "assistant_stopped"
STATUS = 423
CALLBACK_MODULE = "hl"
RESUME_KIND = "halt.resume"
STOPPED_TEXT = ("Владелец остановил ассистента: пока он не нажмёт «Продолжить» в боте согласований, ассистент "
                "ничего не делает сам — не готовит ответы, не задаёт вопросов, не просит подтверждений и не "
                "отправляет.")
VIA_WORDS = {"page": "в «Настройках Штурмана»", "app": "в приложении «Штурман»", "bot": "в боте согласований"}
VIAS = tuple(VIA_WORDS)

# Задачи ответа, которые ещё не закончились: остановка их отменяет.
_OPEN_TASKS = ("queued", "generating", "waiting_source", "waiting_owner", "drafting")


class Stopped(Exception):
    """Действие ассистента отклонено: владелец его остановил."""


# Состояние сервиса — для работы после нажатия в боте (снять паузу аккаунтов). Ставит `api_core.lifespan`.
_state: Any = None


def bind(state: Any) -> None:
    global _state
    _state = state


def is_halt_error(exc: BaseException) -> bool:
    """Отказ триггера базы (вторая линия): запись, которая начала бы действие ассистента."""
    return isinstance(exc, asyncpg.PostgresError) and getattr(exc, "sqlstate", None) == "55000" \
        and "assistant_stopped" in str(exc)


async def is_stopped(conn: asyncpg.Connection) -> bool:
    return await conn.fetchval("SELECT shturman_halted()") is True


async def current(conn: asyncpg.Connection) -> dict[str, Any]:
    row = await conn.fetchrow("SELECT * FROM assistant_halt WHERE id = 1")
    if row is None:
        return {"stopped": False, "since": None, "via": None, "paused_accounts": [], "resumed_at": None}
    return {"stopped": row["stopped"], "since": row["since"], "via": row["via"],
            "paused_accounts": list(row["paused_accounts"]), "resumed_at": row["resumed_at"]}


def public(halt: dict[str, Any]) -> dict[str, Any]:
    """Для страницы, приложения и `GET /api/status`: признаки и время, без меток кнопок."""
    since = halt.get("since")
    return {"stopped": bool(halt.get("stopped")), "since": since.isoformat() if isinstance(since, datetime) else None,
            "via": halt.get("via"), "reading_paused": len(halt.get("paused_accounts") or [])}


async def refuse_if_stopped(conn: asyncpg.Connection) -> None:
    """Отказ с понятной причиной — для маршрутов и функций, которые начали бы действие ассистента."""
    if await is_stopped(conn):
        from .confirm import Refused
        raise Refused(STOPPED_TEXT, STATUS, CODE)


def since_words(halt: dict[str, Any], config: Any) -> str:
    """«сегодня в 14:03 в боте согласований» — для /status и сообщений («Остановлено сегодня в 14:03 …»)."""
    from .executor import owner_views
    return owner_views.moment(halt.get("since"), config) + (" " + VIA_WORDS[halt["via"]] if halt.get("via") in VIA_WORDS else "")


# --- что снова заработает -----------------------------------------------------------------------

async def what_resumes(conn: asyncpg.Connection, state: Any, *, reading: bool | None = None) -> list[str]:
    """Список строками: что снова заработает после «Продолжить». Только по настройкам — они при
    остановке не менялись."""
    from .executor import owner_views
    from .rehearsal import settings as rs
    config = state.config
    halt = await current(conn)
    mode = await owner_views.work_mode(conn)
    lines = ["• вопросы ассистента и просьбы подтвердить его действия снова будут приходить"]
    if getattr(config, "prepare_only", False) and mode != "own":
        lines.append(f"• {words.DRAFTS_MODE.lower()}: ассистент снова готовит ответы на новые сообщения (без отправки)")
    rehearsal = await rs.load(conn)
    if rehearsal["enabled"] and mode != "staff":
        lines.append(f"• {words.REHEARSAL.lower()}: снова для людей из списка ({len(rehearsal['people'])})")
    auto = await conn.fetchval("SELECT count(*) FROM outbox_accounts WHERE autoreply_enabled")
    if auto:
        lines.append(f"• автоответ доверенным людям — включён у {words.counted(auto, 'аккаунта', 'аккаунтов', 'аккаунтов')}")
    rules = await conn.fetchval("SELECT count(*) FROM watch_rules WHERE enabled")
    if rules:
        lines.append(f"• наблюдатель: {words.counted(rules, 'правило', 'правила', 'правил')} снова разбирают сообщения")
    if config.sending:
        lines.append("• отправка: включена на сервере — согласованные вами черновики снова будут уходить")
    paused = len(halt["paused_accounts"])
    if paused:
        if reading:
            lines.append(f"• чтение переписки: снова (аккаунтов на паузе: {paused})")
        else:
            lines.append(f"• чтение переписки останется на паузе (аккаунтов: {paused}) — его включают отдельно: "
                         f"отметкой «и снова читать переписку» или кнопкой «Возобновить» у аккаунта {words.IN_SETUP_PAGE}")
    return lines


# --- остановка -----------------------------------------------------------------------------------

async def _cancel_waiting(conn: asyncpg.Connection) -> dict[str, int]:
    """Отменяет то, что уже ждёт: задачи ответа, черновики, подтверждения, репетиции, вопрос в боте."""
    from .outbox import drafts, policy
    from .replies import workflow
    counts = {"questions": 0, "tasks": 0, "drafts": 0, "actions": 0, "rehearsals": 0}
    for row in await conn.fetch(
            "SELECT id, status FROM reply_tasks WHERE status = ANY($1::text[]) ORDER BY id FOR UPDATE",
            list(_OPEN_TASKS)):
        await workflow.stop(conn, row["id"], "cancelled", "owner_stopped")
        counts["questions" if row["status"] == "waiting_owner" else "tasks"] += 1
    note = "Ассистент остановлен владельцем: черновик снят, ничего не отправлено."
    for row in await conn.fetch(
            "SELECT * FROM outbox_drafts WHERE status IN ('pending', 'approved') ORDER BY id FOR UPDATE"):
        await drafts.finish(conn, row, await policy.target(conn, row["chat_id"]), "rejected",
                            code="owner_stopped", message=note, tell_owner=False)
        counts["drafts"] += 1
    # Отправка бизнес-ботом, которую исполнитель ещё не забрал: снимается, и сообщение точно не уходит.
    for row in await conn.fetch(
            """SELECT d.* FROM outbox_drafts d JOIN jobs j ON j.id = d.job_id
               WHERE d.status = 'sending' AND d.channel = 'business' AND j.status = 'queued'
               ORDER BY d.id FOR UPDATE OF d"""):
        from . import jobs
        if await jobs.cancel(conn, row["job_id"], "снято: владелец остановил ассистента"):
            await drafts.finish(conn, row, await policy.target(conn, row["chat_id"]), "failed",
                                code="owner_stopped", message=note, tell_owner=False)
            counts["drafts"] += 1
    for row in await conn.fetch(
            """UPDATE pending_actions SET status = 'rejected', decided_at = now(), error = 'owner_stopped'
               WHERE status = 'pending' AND kind <> $1 RETURNING summary, card_message_id""", RESUME_KIND):
        counts["actions"] += 1
        if row["card_message_id"]:
            await bridge.edit_owner_message(
                conn, row["card_message_id"], "Отклонено — ассистент остановлен:\n" + row["summary"])
    for row in await conn.fetch(
            """UPDATE rehearsal_items SET state = 'skipped', reason = 'stopped', updated_at = now()
               WHERE state IN ('queued', 'preparing') RETURNING chat_id, trigger_message_id"""):
        from .rehearsal import core as rehearsal
        await rehearsal.release_notify(conn, row["chat_id"], row["trigger_message_id"])
        counts["rehearsals"] += 1
    await conn.execute("DELETE FROM executor_state WHERE key = 'prompt'")
    return counts


async def stop(state: Any, *, via: str, reading: bool = False) -> dict[str, Any]:
    """«Остановить всё». Сразу, без подтверждения: это ужесточение. Возвращает, что сделано.

    reading — заодно поставить на паузу все аккаунты Telegram («И перестать читать переписку»).
    Повторная остановка ничего не отменяет заново, но галочку «перестать читать» выполняет."""
    if via not in VIAS:
        raise ValueError("via: page, app или bot")
    async with state.pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('shturman.halt'))")
            was = await conn.fetchval("SELECT stopped FROM assistant_halt WHERE id = 1 FOR UPDATE")
            if not was:
                await conn.execute(
                    """UPDATE assistant_halt SET stopped = true, since = now(), via = $1, resume_nonce = NULL,
                              paused_accounts = '{}', updated_at = now() WHERE id = 1""", via)
            counts = await _cancel_waiting(conn)
    paused = await _pause_reading(state) if reading else []
    async with state.pool.acquire() as conn:
        halt = await current(conn)
    from .setup_page import audit
    detail = (f"откуда: {via}; вопросов: {counts['questions']}; задач ответа: {counts['tasks']}; "
              f"черновиков: {counts['drafts']}; подтверждений: {counts['actions']}; репетиций: {counts['rehearsals']}"
              + (f"; аккаунтов на паузу: {len(paused)}" if reading else ""))
    await audit.write(state.pool, "halt.stop" if not was else "halt.stop_again", detail=detail)
    logger.info("ассистент остановлен (%s)", via)
    return {"already": bool(was), "counts": counts, "paused": paused, "halt": halt}


async def _pause_reading(state: Any) -> list[int]:
    """Все аккаунты Telegram с сессией — на паузу; номера тех, кого поставили сейчас, — в флаг."""
    from .tg.manager import TgManager
    manager = state.extras.get("tg")
    if not isinstance(manager, TgManager):
        return []
    async with state.pool.acquire() as conn:
        ids = [r["account_id"] for r in await conn.fetch(
            "SELECT account_id FROM tg_sessions WHERE NOT paused AND account_id IS NOT NULL ORDER BY account_id")]
    done: list[int] = []
    for account_id in ids:
        try:
            await manager.pause(account_id)
            done.append(account_id)
        except Exception as exc:  # noqa: BLE001 — остальные аккаунты всё равно ставим на паузу
            logger.warning("остановка: аккаунт не поставлен на паузу (%s)", type(exc).__name__)
    if done:
        async with state.pool.acquire() as conn:
            await conn.execute(
                """UPDATE assistant_halt SET paused_accounts = ARRAY(
                       SELECT DISTINCT unnest(paused_accounts || $1::bigint[]) ORDER BY 1), updated_at = now()
                   WHERE id = 1 AND stopped""", done)
    return done


def stopped_message(result: dict[str, Any], config: Any) -> str:
    """Сообщение владельцу в бот после остановки: что остановлено, что отменено, как продолжить."""
    counts, halt = result["counts"], result["halt"]
    lines = ["⏸ Ассистент остановлен" + (" (уже был остановлен)" if result.get("already") else "") + ".",
             "Когда: " + since_words(halt, config) + ".", "",
             "Остановлено: ответы и черновики, вопросы ассистента, репетиция, автоответ, наблюдатель, "
             "просьбы подтвердить действия и отправка — даже если она включена на сервере. Настройки не "
             "изменились: после «Продолжить» всё вернётся как было."]
    cancelled = [words.counted(counts["questions"], "вопрос ассистента", "вопроса ассистента", "вопросов ассистента"),
                 words.counted(counts["tasks"], "задача ответа", "задачи ответа", "задач ответа"),
                 words.counted(counts["drafts"], "черновик", "черновика", "черновиков"),
                 words.counted(counts["actions"], "подтверждение", "подтверждения", "подтверждений"),
                 words.counted(counts["rehearsals"], "репетиция", "репетиции", "репетиций")]
    lines.append("Отменено: " + ", ".join(cancelled) + ".")
    paused = len(halt.get("paused_accounts") or [])
    if paused:
        lines.append(f"Чтение переписки остановлено: аккаунтов на паузе — {paused}.")
    else:
        lines.append("Переписка по-прежнему собирается в архив, уведомления о новых сообщениях приходят как раньше.")
    lines += ["", "Продолжить — кнопка ниже или /resume: я покажу, что снова заработает, и попрошу подтвердить."]
    return "\n".join(lines)


# --- «Продолжить» --------------------------------------------------------------------------------

async def resume_prompt(conn: asyncpg.Connection, state: Any) -> dict[str, Any] | None:
    """Сообщение «Продолжить?» в боте: список и кнопки с новой меткой. None — не остановлено."""
    halt = await current(conn)
    if not halt["stopped"]:
        return None
    nonce = secrets.token_urlsafe(9)
    await conn.execute("UPDATE assistant_halt SET resume_nonce = $1, updated_at = now() WHERE id = 1", nonce)
    text = ("Продолжить работу ассистента?\nОстановлен " + since_words(halt, state.config) + ".\n\n"
            "Снова заработает:\n" + "\n".join(await what_resumes(conn, state)))
    # Кнопки — парами (подпись, данные): сообщение отправляет сам бот (owner_chat), не очередь заданий.
    buttons = [[("Продолжить", bridge.callback_data(CALLBACK_MODULE, f"r:{nonce}"))]]
    if halt["paused_accounts"]:
        buttons.append([("Продолжить и снова читать переписку", bridge.callback_data(CALLBACK_MODULE, f"a:{nonce}"))])
    buttons.append([("Оставить остановленным", bridge.callback_data(CALLBACK_MODULE, f"k:{nonce}"))])
    return {"text": text, "buttons": buttons}


def _from_bot_press() -> None:
    """«Продолжить» принимается только нажатием в самом боте согласований — не из приложения."""
    principal = authority.requires_owner()
    if principal.source != "telegram" or principal.action == "telegram.miniapp":
        from .confirm import Refused
        raise Refused("Продолжить можно только нажатием в боте согласований.", 403, "bot_only")


async def resume(conn: asyncpg.Connection, *, reading: bool, via: str) -> dict[str, Any] | None:
    """Снимает флаг. Вызывается под нажатием владельца в боте (кнопка или карточка подтверждения),
    внутри транзакции. Возвращает номера аккаунтов, чью паузу снять после фиксации, — или None,
    если остановлено не было."""
    _from_bot_press()
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext('shturman.halt'))")
    halt = await current(conn)
    if not halt["stopped"]:
        return None
    await conn.execute(
        """UPDATE assistant_halt SET stopped = false, since = NULL, via = NULL, resume_nonce = NULL,
                  paused_accounts = '{}', resumed_at = now(), updated_at = now() WHERE id = 1""")
    return {"accounts": halt["paused_accounts"] if reading else [], "kept": [] if reading else halt["paused_accounts"],
            "via": via}


async def resume_reading(state: Any, accounts: list[int]) -> int:
    """После фиксации: снимает паузу с аккаунтов, поставленных на неё остановкой."""
    from .tg.manager import TgManager
    manager = state.extras.get("tg") if state is not None else None
    if not accounts or not isinstance(manager, TgManager):
        return 0
    done = 0
    for account_id in accounts:
        try:
            await manager.resume(account_id)
            done += 1
        except Exception as exc:  # noqa: BLE001 — аккаунт без сессии и т. п.: владелец увидит на странице
            logger.warning("продолжение: пауза аккаунта не снята (%s)", type(exc).__name__)
    return done


def resumed_text(out: dict[str, Any]) -> str:
    text = "▶ Ассистент снова работает: ответы, вопросы, репетиция, наблюдатель и отправка — по прежним настройкам."
    if out["accounts"]:
        text += f"\nЧтение переписки возобновлено (аккаунтов: {len(out['accounts'])})."
    elif out["kept"]:
        text += (f"\nЧтение переписки осталось на паузе (аккаунтов: {len(out['kept'])}) — «Возобновить» у аккаунта "
                 f"{words.IN_SETUP_PAGE}.")
    return text


@bridge.on_callback(CALLBACK_MODULE)
async def _pressed(conn: asyncpg.Connection, rest: str, user_id: int) -> dict[str, Any]:
    """Кнопки сообщения «Продолжить?»: r — продолжить, a — и снова читать переписку, k — оставить."""
    principal = authority.requires_owner()
    if principal.user_id != user_id or principal.source != "telegram":
        return {"answer": "Кнопка недоступна.", "edit_text": None, "remove_buttons": False}
    choice, _, nonce = rest.partition(":")
    row = await conn.fetchrow("SELECT stopped, resume_nonce FROM assistant_halt WHERE id = 1 FOR UPDATE")
    if choice not in ("r", "a", "k") or row is None or not row["stopped"] or not row["resume_nonce"] \
            or not hmac.compare_digest(row["resume_nonce"], nonce):
        return {"answer": "Это сообщение устарело.", "edit_text": None, "remove_buttons": True}
    if choice == "k":
        await conn.execute("UPDATE assistant_halt SET resume_nonce = NULL WHERE id = 1")
        return {"answer": "Ассистент остаётся остановленным.", "remove_buttons": True,
                "edit_text": "Ассистент остаётся остановленным. Продолжить — /resume."}
    try:
        out = await resume(conn, reading=choice == "a", via="bot")
    except Exception as exc:
        from .confirm import Refused
        if isinstance(exc, Refused):
            return {"answer": exc.message[:190], "edit_text": None, "remove_buttons": False}
        raise
    if out is None:
        return {"answer": "Ассистент уже работает.", "edit_text": None, "remove_buttons": True}
    from .setup_page import audit
    await audit.write_conn(conn, "halt.resume", detail="подтверждено в боте; запрошено в боте"
                           + (f"; чтение переписки — снова (аккаунтов: {len(out['accounts'])})" if out["accounts"] else ""))
    state = _state

    async def after() -> None:
        if state is not None:
            await resume_reading(state, out["accounts"])

    return {"answer": "Ассистент снова работает.", "remove_buttons": True, "edit_text": resumed_text(out),
            "after_commit": after}


async def apply_resume(conn: asyncpg.Connection, payload: dict[str, Any]) -> Any:
    """Подтверждение `halt.resume` (карточка со страницы или из приложения) — нажато в боте."""
    from .confirm import Done, Refused
    via = payload.get("via") if payload.get("via") in VIAS else "page"
    out = await resume(conn, reading=payload.get("reading") is True, via=via)
    if out is None:
        raise Refused("Ассистент уже работает — продолжать нечего.", 409, "not_stopped")
    from .setup_page import audit
    await audit.write_conn(conn, "halt.resume", detail=f"подтверждено в боте; запрошено: {via}"
                           + (f"; чтение переписки — снова (аккаунтов: {len(out['accounts'])})" if out["accounts"] else ""))
    state = _state

    async def after() -> None:
        if state is not None:
            await resume_reading(state, out["accounts"])

    return Done(note=resumed_text(out), result={"resumed": True}, after=after)


async def request_resume(conn: asyncpg.Connection, state: Any, *, via: str, reading: bool) -> dict[str, Any]:
    """Со страницы или из приложения: карточка подтверждения в бот. Само по себе ничего не снимает."""
    from . import confirm
    halt = await current(conn)
    if not halt["stopped"]:
        raise confirm.Refused("Ассистент не остановлен — продолжать нечего.", 409, "not_stopped")
    summary = ("Продолжить работу ассистента (запрошено " + VIA_WORDS[via] + ").\nОстановлен "
               + since_words(halt, state.config) + ".\n\nСнова заработает:\n"
               + "\n".join(await what_resumes(conn, state, reading=reading)))
    return await confirm.request_card(conn, RESUME_KIND, summary, {"reading": bool(reading), "via": via})


# Подтверждение «Продолжить» со страницы и из приложения: регистрация функции применения.
from . import confirm as _confirm  # noqa: E402 — confirm сам зовёт halt только внутри функций

_confirm.applier(RESUME_KIND)(apply_resume)
