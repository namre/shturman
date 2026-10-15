"""Уведомления владельцу: подписка на живые сообщения, фоновая отправка, внутренний API (Р-84).

  GET /api/notify     настройки, умолчание ли это, счётчики (только числа), есть ли бот согласований
  PUT /api/notify     изменения настроек: {personal?, people?, spheres?, groups?, aliases?, preview?,
                      mode?, gap_minutes?, digest_minutes?, quiet?, quiet_from?, quiet_to?}

Через внутренний API (ассистент) расширение ждёт нажатия владельца в боте согласований (действие
`notify.settings`), сужение и время применяются сразу, и владелец получает сообщение об изменении.
Страница настройки меняет то же самое своим входом и сразу (`setup_page/notify.py`).
"""

from __future__ import annotations

import contextlib
from typing import Any, AsyncIterator

import asyncpg
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from .. import bridge, confirm
from ..api_core import BadRequest, body, handler, settle
from ..app import AppState, state_of
from ..events import MESSAGE_HELD, MESSAGE_LIVE
from . import buttons as _buttons  # noqa: F401 — кнопки под уведомлениями (регистрация нажатий)
from . import core
from . import settings as ns

SETTINGS = "notify.settings"
EXTRAS_KEY = "notify"


async def sphere_titles(conn: asyncpg.Connection) -> dict[int, str]:
    return {r["id"]: r["title"] for r in await conn.fetch("SELECT id, title FROM spheres")}


async def check(conn: asyncpg.Connection, after: dict[str, Any]) -> None:
    """Перечень сфер — только существующие сферы (ValueError с объяснением)."""
    if after["spheres"]:
        known = set(await sphere_titles(conn))
        missing = [i for i in after["spheres"] if i not in known]
        if missing:
            raise ValueError("поле spheres: таких сфер нет — " + ", ".join(f"№ {i}" for i in missing))
    if after["personal"] == "spheres" and not after["spheres"]:
        raise ValueError("личные «только из выбранных сфер»: выберите хотя бы одну сферу")
    if after["personal"] == "people" and not after["people"]:
        raise ValueError("личные «только от выбранных людей»: выберите хотя бы одного человека")


async def lock(conn: asyncpg.Connection) -> None:
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", "shturman.settings:notify")


@confirm.applier(SETTINGS)
async def _apply(conn: asyncpg.Connection, payload: dict[str, Any]) -> confirm.Done:
    try:
        changes = ns.validate(payload.get("changes") or {})
    except ValueError as exc:
        raise confirm.Refused(str(exc), 400) from None
    await lock(conn)
    before = await ns.load(conn)
    after = {**before, **changes}
    try:
        await check(conn, after)
    except ValueError as exc:
        raise confirm.Refused(str(exc), 400) from None
    confirm.must_not_widen(bool(ns.widening(before, after)))
    await ns.save(conn, after)
    if after != before:
        await confirm.tell(conn, "Изменены уведомления о новых сообщениях:\n"
                           + "\n".join(ns.describe(after, sphere_titles=await sphere_titles(conn))), silent=True)
    return confirm.Done(result={"settings": after})


async def view(state: AppState, conn: asyncpg.Connection) -> dict[str, Any]:
    return {"settings": await ns.load(conn), "default": not await ns.stored(conn),
            "own_bot": bridge.owns_bot(), "status": await core.status(conn)}


@handler
async def get_notify(request: Request) -> JSONResponse:
    async with state_of(request).ro_pool.acquire() as conn:
        return JSONResponse(await view(state_of(request), conn))


WIDEN_WORDS = {"personal": "больше личных сообщений в уведомлениях", "groups": "уведомления о прямых обращениях в группах",
               "aliases": "новые обращения по имени", "preview": "начало сообщения в тексте уведомления",
               "muted": "снова сообщать о чатах, о которых вы просили не сообщать"}


@handler
async def put_notify(request: Request) -> JSONResponse:
    data = await body(request)
    try:
        changes = ns.validate(data)
    except ValueError as exc:
        raise BadRequest(str(exc)) from None
    if not changes:
        raise BadRequest("нечего менять")
    async with state_of(request).pool.acquire() as conn:
        before = await ns.load(conn)
        after = {**before, **changes}
        try:
            await check(conn, after)
        except ValueError as exc:
            raise BadRequest(str(exc)) from None
        wider = ns.widening(before, after)
        summary = None
        if wider:
            summary = ("Изменить уведомления о новых сообщениях — расширяется: "
                       + ", ".join(WIDEN_WORDS[w] for w in wider) + ".\nСтанет так:\n"
                       + "\n".join(ns.describe(after, sphere_titles=await sphere_titles(conn))))
        answer, result = await settle(conn, SETTINGS, {"changes": changes}, summary=summary)
    return answer or JSONResponse(result)


def routes() -> list[BaseRoute]:
    return [
        Route("/api/notify", get_notify, methods=["GET"]),
        Route("/api/notify", put_notify, methods=["PUT"]),
    ]


@contextlib.asynccontextmanager
async def lifespan(state: AppState) -> AsyncIterator[None]:
    notifier = core.Notifier(state.pool, tz=lambda: state.config.timezone)
    state.extras[EXTRAS_KEY] = notifier

    async def on_message(payload: dict[str, Any]) -> None:
        await notifier.on_message(payload)

    state.events.subscribe(MESSAGE_LIVE, on_message)
    state.events.subscribe(MESSAGE_HELD, on_message)
    state.spawn(notifier.run(), name="notify-sender")
    try:
        yield
    finally:
        state.extras.pop(EXTRAS_KEY, None)


async def overview(conn: asyncpg.Connection) -> dict[str, Any]:
    """Поля для `GET /api/status` и `./ops/doctor.sh`: только признаки и числа."""
    s = await ns.load(conn)
    st = await core.status(conn)
    return {"notify_personal": s["personal"], "notify_groups": s["groups"], "notify_mode": s["mode"],
            "notify_waiting": st["waiting"], "notify_sent_24h": st["sent_24h"]}
