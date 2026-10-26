"""Карточка «Напоминания исполнителям» в «Настройках Штурмана» (`brief/remind.py`, решение владельца 25.10.2026).

  GET  remind   круг напоминаний: предлагать ли «Напомнить», кому (всем, кто обещал вам, или только
                выбранным), выбранные люди с именами и группы с названиями
  PUT  remind   изменить: {enabled?, who?, people?, groups?} — действие самого владельца, сразу; журнал
                `remind.settings` (признаки и числа, без имён и номеров)

Людей страница ищет маршрутом уведомлений (`notify/people`), группы — маршрутом наблюдателя (`watch/chats`).
Внутреннего API у этой настройки нет: ассистент её не меняет. Напоминание ничего не отправляет —
расширение круга ничего никому не раскрывает, поэтому подтверждения в боте здесь нет.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from ..api_core import BadRequest
from ..app import state_of
from ..brief import remind
from . import service as page
from .notify import _chat_titles, _people_names

REMIND = page.API + "/remind"


async def remind_view(request: Request) -> dict[str, Any]:
    async with state_of(request).ro_pool.acquire() as conn:
        s = await remind.load(conn)
        people = await _people_names(conn, s["people"])
        titles = await _chat_titles(conn, s["groups"])
    return {"settings": s, "people": people,
            "groups": [titles.get(i) or {"id": i, "title": None} for i in s["groups"]],
            "limits": {"people": remind.MAX_PEOPLE, "groups": remind.MAX_GROUPS, "daily_cap": remind.DAILY_CAP}}


@page.endpoint
async def remind_get(request: Request) -> JSONResponse:
    return JSONResponse(await remind_view(request))


@page.endpoint
async def remind_put(request: Request) -> JSONResponse:
    try:
        changes = remind.validate(await page._body(request))
    except ValueError as exc:
        raise BadRequest(str(exc)) from None
    if not changes:
        raise BadRequest("Нечего менять.")
    async with state_of(request).pool.acquire() as conn:
        after = await remind.save(conn, changes)
    await page._log(request, "remind.settings", detail=remind.audit_detail(after))
    return JSONResponse({"ok": True, **await remind_view(request)})


def routes() -> list[BaseRoute]:
    return [Route(REMIND, remind_get, methods=["GET"]), Route(REMIND, remind_put, methods=["PUT"])]
