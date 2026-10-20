"""Черновики ответов (бывшая репетиция): подписка на живые сообщения, внутренний API, решение владельца
(Р-85, Р-105).

  GET /api/rehearsal   настройки (список людей — числовые ID), есть ли бот согласований, статистика
                       за 7 дней — только числа
  PUT /api/rehearsal   изменения: {enabled?, people?, daily_cap?, pause_seconds?}

Через внутренний API (ассистент) расширение — включить, новые люди, выше предел, короче пауза — ждёт
нажатия владельца в боте согласований (действие `rehearsal.settings`); сужение — выключить, убрать
людей, ниже предел, длиннее пауза — применяется сразу, и владелец получает сообщение. Страница
настройки, команда `/drafts` (и прежняя `/rehearsal`) и кнопка «Поднять до …» в боте согласований меняют то же самое сразу:
это сам владелец. Вкладка «Черновики» приложения — тоже сразу, но с журналом и сообщением в бот (Р-89).
Ни один из этих путей ничего не отправляет и права отправки не даёт.
"""

from __future__ import annotations

import contextlib
from typing import Any, AsyncIterator

import asyncpg
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from .. import authority, bridge, confirm
from ..api_core import BadRequest, body, handler, settle
from ..app import AppState, state_of
from ..events import MESSAGE_LIVE
from ..outbox import text as textlib
from . import core
from . import settings as rs

SETTINGS = "rehearsal.settings"
EXTRAS_KEY = "rehearsal"


async def lock(conn: asyncpg.Connection) -> None:
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", "shturman.settings:rehearsal")


def via() -> str:
    """Кто меняет настройки — для журнала действий: страница, бот, приложение или внутренний API."""
    principal = authority.get_owner_principal()
    if principal is None:
        return "внутренний API"
    if principal.source == "setup":
        return "страница"
    return {"telegram.command": "бот, команда", "telegram.callback": "бот, «Да» под карточкой",
            "telegram.button": "бот, кнопка «Поднять до …»",
            "telegram.miniapp": "приложение"}.get(principal.action or "", "бот")


async def _history_words(conn: asyncpg.Connection, before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Что изменилось — для «Истории» (Р-95): имена людей — как в архиве, без номеров."""
    from .. import history
    out = []
    if before["enabled"] != after["enabled"]:
        out.append("включена" if after["enabled"] else "выключена")
    for tg_id in sorted(set(after["people"]) - set(before["people"])):
        out.append("добавлен(а) " + await history.person(conn, tg_id))
    for tg_id in sorted(set(before["people"]) - set(after["people"])):
        out.append("убран(а) " + await history.person(conn, tg_id))
    if before["daily_cap"] != after["daily_cap"]:
        out.append(f"в сутки — {after['daily_cap']} (было {before['daily_cap']})")
    if before["pause_seconds"] != after["pause_seconds"]:
        out.append(f"пауза — {after['pause_seconds']} с (была {before['pause_seconds']} с)")
    return out or ["без изменений"]


@confirm.applier(SETTINGS)
async def _apply(conn: asyncpg.Connection, payload: dict[str, Any]) -> confirm.Done:
    try:
        changes = rs.validate(payload.get("changes") or {})
    except ValueError as exc:
        raise confirm.Refused(str(exc), 400) from None
    await lock(conn)
    before = await rs.load(conn)
    after = {**before, **changes}
    try:
        await core.check_people(conn, sorted(set(after["people"]) - set(before["people"])))
    except ValueError as exc:
        raise confirm.Refused(str(exc), 400) from None
    confirm.must_not_widen(bool(rs.widening(before, after)))
    await rs.save(conn, after)
    if after != before:
        await confirm.tell(conn, "Изменены черновики ответов:\n" + "\n".join(rs.describe(after)), silent=True)
        from .. import history
        changed = {k: before[k] for k in rs.DEFAULTS if before.get(k) != after.get(k)}
        await history.record(conn, "rehearsal", "rehearsal", "Черновики ответов: " + "; ".join(
            await _history_words(conn, before, after)) + ".", {"kind": SETTINGS, "payload": {"changes": changed}})
    source = via()
    mod = core.current()

    async def journal() -> None:
        # Страница пишет журнал сама (там же видна сессия); здесь — бот и внутренний API.
        if mod is not None and source != "страница":
            from ..setup_page import audit
            await audit.write(mod.pool, "rehearsal.settings", detail=rs.audit_detail(after, source))

    return confirm.Done(result={"settings": after}, after=journal)


async def view(state: AppState, conn: asyncpg.Connection) -> dict[str, Any]:
    main = await conn.fetchval("SELECT EXISTS (SELECT 1 FROM accounts WHERE role = 'owner')")
    return {"settings": await rs.load(conn), "own_bot": bridge.owns_bot(), "main_account": bool(main),
            "stats": await core.stats(conn)}


@handler
async def get_rehearsal(request: Request) -> JSONResponse:
    async with state_of(request).ro_pool.acquire() as conn:
        return JSONResponse(await view(state_of(request), conn))


async def _summary(conn: asyncpg.Connection, before: dict[str, Any], after: dict[str, Any]) -> str:
    """Текст карточки «Подтвердите действие»: что расширяется и кто именно добавляется."""
    wider = rs.widening(before, after)
    lines = ["Изменить черновики ответов — расширяется: " + ", ".join(rs.WIDEN_WORDS[w] for w in wider) + "."]
    added = sorted(set(after["people"]) - set(before["people"]))
    if added:
        lines.append("Новые люди в списке — сервис будет готовить вам черновики ответов им:")
        for person in await core.people_names(conn, added):
            name = textlib.one_line(person["name"], 60) if person["name"] else "в архиве такого собеседника пока нет"
            lines.append(f"• {name} (идентификатор Telegram {person['tg_id']})")
    lines.append("Станет так:")
    lines += rs.describe(after)
    lines.append("Их сообщения и найденное в этом же чате уйдут модели — её провайдеру, как при автоответе.")
    return "\n".join(lines)


@handler
async def put_rehearsal(request: Request) -> JSONResponse:
    data = await body(request)
    try:
        changes = rs.validate(data)
    except ValueError as exc:
        raise BadRequest(str(exc)) from None
    if not changes:
        raise BadRequest("нечего менять")
    state = state_of(request)
    async with state.pool.acquire() as conn:
        before = await rs.load(conn)
        tighter, looser = rs.split(before, changes)
        try:
            await core.check_people(conn, sorted(set(changes.get("people", [])) - set(before["people"])))
        except ValueError as exc:
            raise BadRequest(str(exc)) from None
        if tighter:
            await settle(conn, SETTINGS, {"changes": tighter}, summary=None)
        if looser:
            now = {**before, **tighter}
            summary = await _summary(conn, now, {**now, **looser})
            answer, _ = await settle(conn, SETTINGS, {"changes": looser}, summary=summary,
                                     applied_now=tighter or None)
            if answer is not None:
                return answer
        return JSONResponse(await view(state, conn))


def routes() -> list[BaseRoute]:
    return [
        Route("/api/rehearsal", get_rehearsal, methods=["GET"]),
        Route("/api/rehearsal", put_rehearsal, methods=["PUT"]),
    ]


@contextlib.asynccontextmanager
async def lifespan(state: AppState) -> AsyncIterator[None]:
    mod = core.Rehearsal(state)
    core.set_current(mod)
    state.extras[EXTRAS_KEY] = mod

    async def on_message(payload: dict[str, Any]) -> None:
        await mod.on_message(payload)

    state.events.subscribe(MESSAGE_LIVE, on_message)
    state.spawn(mod.run(), name="rehearsal-sweeper")
    try:
        yield
    finally:
        mod.close()
        state.extras.pop(EXTRAS_KEY, None)
        if core.current() is mod:
            core.set_current(None)


async def overview(conn: asyncpg.Connection) -> dict[str, Any]:
    """Поля для `GET /api/status` и `./ops/doctor.sh`: только признаки и числа за 7 дней. `drafts` уходит в
    объект `replies` (Р-105); поля `rehearsal_*` — прежние имена (Р-85), оставлены на один выпуск."""
    s = await rs.load(conn)
    st = await core.stats(conn)
    drafts = {"enabled": s["enabled"], "people": len(s["people"]),
              "trusted": int(await conn.fetchval("SELECT count(*) FROM outbox_trusted")),
              "shown_7d": st["shown"], "sent_7d": st["sent"], "voice_7d": st["voice"], "put_7d": st["put"],
              "good_7d": st["good"],
              "almost_7d": st["almost"], "bad_7d": st["bad"], "unrated_7d": st["unrated"],
              "limited_7d": st["limited"], "failed_7d": st["failed"]}
    return {"rehearsal_enabled": s["enabled"], "rehearsal_people": len(s["people"]),
            "rehearsal_shown_7d": st["shown"], "rehearsal_good_7d": st["good"],
            "rehearsal_almost_7d": st["almost"], "rehearsal_bad_7d": st["bad"],
            "rehearsal_unrated_7d": st["unrated"], "rehearsal_limited_7d": st["limited"],
            "rehearsal_failed_7d": st["failed"], "drafts": drafts}
