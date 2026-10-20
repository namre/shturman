"""«История» изменений правил и «Вернуть как было» (решение Р-95).

Каждая функция, которая меняет правила ассистента (доверенные, автоответ, черновики по чатам, группы и
обращения, правила отправки, репетиция, уведомления, разбор входящих, «Остановить всё», утренняя
сводка), пишет здесь строку в той же транзакции: что стало, откуда и как вернуть (`record`). Строки
показываются владельцу на странице «Настройки Штурмана» и во вкладке «История» приложения «Штурман».

«Вернуть как было» (`revert`) — только у последнего изменения каждой вещи (`target`) и не старше
`REVERT_DAYS`. Возврат идёт тем же путём, что любое изменение:

  * ужесточение (вернуть «запрещено», убрать доверенного, выключить) — сразу, откуда бы ни нажали:
    функция применения сама проверяет под блокировкой, что ничего не расширяет (`confirm.apply_narrowing`);
  * ослабление — только нажатием владельца в самом боте согласований: ставится карточка `owner.widen`
    (`confirm.BOT_ONLY`) — и со страницы, и из приложения. Остановка (`halt.resume`) и разбор входящих
    (`triage.enable`) — своими карточками, которые и так подтверждаются только в боте.

Чего здесь нет: текста переписки и черновиков. Имена людей и названия чатов — как в архиве, в той же
форме, что в карточках бота (`sanitize.clean_line`). Номер в Telegram — только в `undo`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

from . import authority, confirm
from .sanitize import clean_line

KEEP_DAYS = 90
KEEP_ROWS = 2000
REVERT_DAYS = 7              # «Вернуть как было» — у изменений не старше недели
TITLE_LIMIT = 600

AREAS = {
    "trusted": "Доверенные люди",
    "autoreply": "Автоответ",
    "drafting": "Черновики в чате",
    "groups": "Группы и темы для ответов",
    "addressing": "Обращения к ассистенту",
    "policy": "Правила отправки",
    "rehearsal": "Репетиция ответов",
    "notify": "Уведомления",
    "triage": "Разбор входящих",
    "halt": "Остановка ассистента",
    "summaries": "Сводки по расписанию",
}

# Вид «как вернуть», которые применяются не функцией `confirm`, а своим путём (`revert`).
SPECIAL = frozenset({"halt.stop", "halt.resume", "triage.enable", "triage.disable", "triage.settings",
                     "summaries.schedule"})


def via() -> str:
    """Откуда изменение — словами, для строки «Истории»."""
    principal = authority.get_owner_principal()
    if principal is None:
        return "ассистент или внутренний API"
    app = principal.action == "telegram.miniapp"
    if confirm.card_press():
        return "приложение, «Да» под карточкой" if app else "бот, «Да» под карточкой"
    if principal.source == "setup":
        return "страница"
    if app:
        return "приложение"
    return {"telegram.command": "бот, команда", "telegram.callback": "бот, кнопка",
            "telegram.button": "бот, кнопка", "telegram.reply": "бот, ответ"}.get(principal.action or "", "бот")


async def record(conn: asyncpg.Connection, area: str, target: str, title: str,
                 undo: dict[str, Any] | None = None, *, source: str | None = None) -> int:
    """Строка «Истории». Вызывать в транзакции изменения: появляется вместе с ним или не появляется."""
    if area not in AREAS:
        raise KeyError(f"неизвестная область истории: {area}")
    text = " ".join(title.split())
    if len(text) > TITLE_LIMIT:
        text = text[:TITLE_LIMIT - 1] + "…"
    return await conn.fetchval(
        """INSERT INTO settings_history (area, target, title, via, undo) VALUES ($1, $2, $3, $4, $5::jsonb)
           RETURNING id""",
        area, target, text, source or via(), json.dumps(undo, ensure_ascii=False) if undo else None)


def name(value: Any, fallback: str, limit: int = 60) -> str:
    return clean_line(value, limit) or fallback


async def person(conn: asyncpg.Connection, tg_id: int) -> str:
    """Имя собеседника, как в архиве; нет в архиве — «человек № …»."""
    row = await conn.fetchrow(
        """SELECT COALESCE(c.title, p.name) AS name FROM peers p
           LEFT JOIN chats c ON c.peer_id = p.id AND c.type = 'personal_chat'
           WHERE p.class = 'user' AND p.tg_id = $1 ORDER BY c.id NULLS LAST LIMIT 1""", tg_id)
    return name(row["name"] if row else None, f"человек № {tg_id}")


async def chat(conn: asyncpg.Connection, chat_id: int) -> str:
    title = await conn.fetchval("SELECT title FROM chats WHERE id = $1", chat_id)
    return name(title, f"чат № {chat_id}")


async def trim(conn: asyncpg.Connection) -> None:
    await conn.execute("DELETE FROM settings_history WHERE at < now() - make_interval(days => $1)", KEEP_DAYS)
    await conn.execute(
        """DELETE FROM settings_history WHERE id <= COALESCE(
               (SELECT id FROM settings_history ORDER BY id DESC OFFSET $1 LIMIT 1), 0)""", KEEP_ROWS)


def _loads(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def entries(conn: asyncpg.Connection, *, days: int = 14, limit: int = 300) -> list[dict[str, Any]]:
    """Изменения за `days` дней, новые сверху; у каждого — можно ли вернуть."""
    rows = await conn.fetch(
        """SELECT h.*, (h.id = (SELECT max(id) FROM settings_history x WHERE x.target = h.target)) AS latest
           FROM settings_history h WHERE h.at > now() - make_interval(days => $1)
           ORDER BY h.id DESC LIMIT $2""", days, limit)
    edge = datetime.now(timezone.utc) - timedelta(days=REVERT_DAYS)
    out = []
    for r in rows:
        undo = _loads(r["undo"])
        can = bool(undo) and r["undone_by"] is None and r["latest"] and r["at"] > edge
        out.append({"id": r["id"], "at": r["at"].isoformat(), "area": r["area"],
                    "area_title": AREAS.get(r["area"], r["area"]), "title": r["title"], "via": r["via"],
                    "undone": r["undone_by"] is not None, "can_revert": can,
                    "page_only": bool(undo) and undo.get("kind") == "triage.settings"})
    return out


class RevertRefused(Exception):
    def __init__(self, message: str, status: int = 409, code: str = "cannot_revert") -> None:
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


async def _mark(conn: asyncpg.Connection, entry_id: int, target: str) -> None:
    """Строку, которую вернули, отмечает последней строкой того же target (её только что записал возврат)."""
    newest = await conn.fetchval("SELECT max(id) FROM settings_history WHERE target = $1", target)
    if newest is not None and newest != entry_id:
        await conn.execute("UPDATE settings_history SET undone_by = $2 WHERE id = $1 AND undone_by IS NULL",
                           entry_id, newest)


async def mark_by_payload(conn: asyncpg.Connection, history_id: Any) -> None:
    """После нажатия «Да» под карточкой `owner.widen` с возвратом — отметить возвращённую строку."""
    if isinstance(history_id, int) and not isinstance(history_id, bool):
        target = await conn.fetchval("SELECT target FROM settings_history WHERE id = $1", history_id)
        if target is not None:
            await _mark(conn, history_id, target)


def _when(at: datetime, zone: str | None) -> str:
    try:
        from zoneinfo import ZoneInfo
        local = at.astimezone(ZoneInfo(zone)) if zone else at
    except Exception:  # noqa: BLE001
        local = at
    return f"{local:%d.%m %H:%M}"


async def revert(conn: asyncpg.Connection, state: Any, entry_id: int, *, where: str) -> dict[str, Any]:
    """«Вернуть как было». where — page | app. Вызывать в контексте владельца, вне транзакции.

    Ответ: {"status": "applied", "text"} либо {"status": "pending_confirmation", "action_id", "text"}."""
    authority.requires_owner()
    row = await conn.fetchrow("SELECT * FROM settings_history WHERE id = $1", entry_id)
    if row is None:
        raise RevertRefused("Такой записи нет — список обновлён.", 404, "gone")
    undo = _loads(row["undo"])
    newest = await conn.fetchval("SELECT max(id) FROM settings_history WHERE target = $1", row["target"])
    if not undo:
        raise RevertRefused("Это изменение вернуть отсюда нельзя.")
    if row["undone_by"] is not None:
        raise RevertRefused("Это изменение уже возвращено.", 409, "already")
    if newest != row["id"]:
        raise RevertRefused("После этого изменения было ещё одно — вернуть можно только последнее.", 409, "superseded")
    if row["at"] < datetime.now(timezone.utc) - timedelta(days=REVERT_DAYS):
        raise RevertRefused(f"Вернуть можно изменения не старше {REVERT_DAYS} дней.", 409, "too_old")
    kind, payload = undo.get("kind"), undo.get("payload") or {}
    if kind in SPECIAL:
        return await _special(conn, state, row, kind, payload, where=where)
    try:
        await confirm.apply_narrowing(conn, kind, payload, quiet=where == "page")
    except confirm.Refused as exc:
        if not confirm.widens(exc):
            raise RevertRefused(exc.message, exc.status, exc.code or "refused") from None
        return await _card(conn, state, row, kind, payload, where=where)
    async with conn.transaction():
        await _mark(conn, row["id"], row["target"])
    return {"status": confirm.APPLIED, "text": "Вернул как было."}


WIDEN_WORDS = {
    "outbox.trusted_add": "Человек снова станет доверенным: если автоответ включён, ассистент будет отвечать ему сам.",
    "outbox.autoreply": "Автоответ доверенным снова заработает так, как был настроен.",
    "outbox.chat_drafting": "Ассистенту снова можно будет готовить черновики в этом чате. Отправка — только по вашему нажатию.",
    "outbox.account_drafting": "Ассистенту снова можно будет готовить черновики во всех чатах аккаунта.",
    "outbox.group_scope": "Ассистенту снова можно будет отвечать в этой группе или теме.",
    "outbox.addressing": "Ассистент снова будет откликаться на эти обращения в группах.",
    "outbox.policy": "Правила отправки снова станут свободнее.",
    "rehearsal.settings": "Репетиция снова будет отдавать модели сообщения этих людей.",
    "notify.settings": "Уведомления снова станут шире.",
}


async def _card(conn: asyncpg.Connection, state: Any, row: Any, kind: str, payload: dict[str, Any], *,
                where: str) -> dict[str, Any]:
    zone = getattr(getattr(state, "config", None), "timezone", None)
    place = "в приложении «Штурман»" if where == "app" else "в «Настройках Штурмана»"
    summary = (f"Вернуть как было — {place} вы попросили отменить изменение от {_when(row['at'], zone)}: "
               f"«{row['title']}».\n" + WIDEN_WORDS.get(kind, "Это расширит то, что может ассистент."))
    out = await request_widen(conn, kind, payload, summary, history_id=row["id"], where=where)
    return {**out, "text": "Возврат расширяет то, что может ассистент: подтвердите его в боте согласований — "
                           "карточка уже там."}


WIDEN = "owner.widen"
# Что можно попросить карточкой `owner.widen`: настройки ответов и доверия, репетиция, уведомления.
# Не сюда: «Продолжить» (`halt.resume`), разбор входящих (`triage.enable`), вход по коду, чаты, аккаунты —
# у них свои карточки и свои правила.
WIDEN_KINDS = frozenset({"outbox.trusted_add", "outbox.chat_drafting", "outbox.account_drafting", "outbox.autoreply",
                         "outbox.group_scope", "outbox.addressing", "outbox.policy", "rehearsal.settings",
                         "notify.settings"})


async def request_widen(conn: asyncpg.Connection, kind: str, payload: dict[str, Any], summary: str, *,
                        history_id: int | None = None, where: str) -> dict[str, Any]:
    """Карточка `owner.widen` в бот: ослабление, о котором владелец попросил со страницы или из приложения.
    Подтвердить — только нажатием в самом боте (`confirm.BOT_ONLY`)."""
    if kind not in WIDEN_KINDS:
        raise RevertRefused("Это изменение так не просят.", 400, "bad_kind")
    from . import halt
    await halt.refuse_if_stopped(conn)
    body: dict[str, Any] = {"kind": kind, "payload": payload, "from": where}
    if history_id is not None:
        body["history_id"] = history_id
    return await confirm.request_card(conn, WIDEN, summary, body)


@confirm.applier(WIDEN)
async def _apply_widen(conn: asyncpg.Connection, payload: dict[str, Any]) -> confirm.Done:
    """Нажатие «Да, сделать» под карточкой `owner.widen` — только в самом боте (`confirm.BOT_ONLY`).
    Применяет та же функция, что и всегда: она заново проверяет всё, что могло измениться."""
    principal = authority.requires_owner()
    if principal.source != "telegram" or principal.action == "telegram.miniapp" or not confirm.card_press():
        raise confirm.Refused(confirm.BOT_ONLY_NOTE, 403, "bot_only")
    kind, inner = payload.get("kind"), payload.get("payload")
    if kind not in WIDEN_KINDS or not isinstance(inner, dict):
        raise confirm.Refused("Такого изменения нет.", 400, "bad_kind")
    done = await confirm.run_inner(conn, kind, inner)
    await mark_by_payload(conn, payload.get("history_id"))
    return done


async def change(conn: asyncpg.Connection, kind: str, payload: dict[str, Any], *, where: str,
                 summary: str) -> dict[str, Any]:
    """Изменение настроек ответов и доверия самим владельцем (Р-95). Вне транзакции, в контексте владельца.

      page — страница «Настройки Штурмана»: сразу и в ту и в другую сторону (`confirm.apply_owner`);
      bot  — кнопка или команда в самом боте согласований: сразу (`confirm.apply_owner`);
      app  — приложение «Штурман»: ужесточение сразу (`confirm.apply_narrowing`), ослабление — только
             карточкой в бот (`owner.widen`), её «Да» в приложении не принимается.
    """
    if where == "app":
        try:
            out = await confirm.apply_narrowing(conn, kind, payload)
        except confirm.Refused as exc:
            if not confirm.widens(exc):
                raise
            out = await request_widen(conn, kind, payload, summary, where=where)
            return {**out, "text": "Это расширяет то, что может ассистент: подтвердите в боте согласований — "
                                   "карточка уже там."}
        return {**out, "text": "Готово."}
    if where not in ("page", "bot"):
        raise ValueError("where: page, app или bot")
    out = await confirm.apply_owner(conn, kind, payload)
    return {**out, "text": "Готово."}


async def _special(conn: asyncpg.Connection, state: Any, row: Any, kind: str, payload: dict[str, Any], *,
                   where: str) -> dict[str, Any]:
    if kind == "halt.stop":
        from . import halt
        await halt.stop(state, via=where, reading=False)
        async with conn.transaction():
            await _mark(conn, row["id"], row["target"])
        return {"status": confirm.APPLIED, "text": "Ассистент снова остановлен."}
    if kind == "halt.resume":
        from . import halt
        out = await halt.request_resume(conn, state, via=where, reading=False)
        return {**out, "text": "«Продолжить» подтверждается только нажатием в боте согласований — карточка уже там."}
    if kind == "triage.enable":
        from .triage import service as triage_service
        out = await triage_service.request_enable(conn)
        return {**out, "text": "Включить разбор входящих можно только нажатием в боте согласований — карточка уже там."}
    if kind == "triage.disable":
        from .triage import service as triage_service
        await triage_service.disable(conn, via_words=via())
        async with conn.transaction():
            await _mark(conn, row["id"], row["target"])
        return {"status": confirm.APPLIED, "text": "Разбор входящих выключен."}
    if kind == "triage.settings":
        if where != "page":
            raise RevertRefused("Правила разбора входящих возвращаются в «Настройках Штурмана».", 409, "page_only")
        from .triage import service as triage_service
        await triage_service.restore_settings(conn, payload.get("changes") or {})
        async with conn.transaction():
            await _mark(conn, row["id"], row["target"])
        return {"status": confirm.APPLIED, "text": "Вернул как было."}
    if kind == "summaries.schedule":
        from . import summaries
        await summaries.set_desired(conn, payload.get("changes") or {}, state=state)
        async with conn.transaction():
            await _mark(conn, row["id"], row["target"])
        return {"status": confirm.APPLIED, "text": "Вернул как было."}
    raise RevertRefused("Это изменение вернуть отсюда нельзя.")
