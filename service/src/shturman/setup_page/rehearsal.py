"""Раздел «Репетиция ответов» в «Настройках Штурмана» (решение Р-85).

  GET  rehearsal   настройки, люди списка с именами, есть ли бот согласований и основной аккаунт,
                   статистика за 7 дней (только числа)
  PUT  rehearsal   изменить: {enabled?, people?, daily_cap?, pause_seconds?} — действие самого владельца,
                   сразу (`confirm.apply_owner`); журнал `rehearsal.settings` (в списке важных)

Людей страница ищет тем же маршрутом, что уведомления (`notify/people`): собеседники личных чатов
основного аккаунта, не боты и не служебные чаты Telegram. Отправки в разделе нет: её здесь не
включают, и репетиция её не делает.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from .. import confirm
from ..api_core import BadRequest
from ..app import state_of
from ..rehearsal import core as rehearsal
from ..rehearsal import service as rehearsal_service
from ..rehearsal import settings as rs
from . import service as page

REHEARSAL = page.API + "/rehearsal"


async def rehearsal_view(request: Request) -> dict[str, Any]:
    state = state_of(request)
    async with state.ro_pool.acquire() as conn:
        out = await rehearsal_service.view(state, conn)
        out["people"] = await rehearsal.people_names(conn, out["settings"]["people"])
    out["describe"] = rs.describe(out["settings"])
    out["limits"] = {"daily_cap": list(rs.DAILY_CAP), "pause_seconds": list(rs.PAUSE), "people": rs.MAX_PEOPLE}
    out["sending"] = bool(state.config.sending)
    return out


@page.endpoint
async def rehearsal_get(request: Request) -> JSONResponse:
    return JSONResponse(await rehearsal_view(request))


@page.endpoint
async def rehearsal_put(request: Request) -> JSONResponse:
    try:
        changes = rs.validate(await page._body(request))
    except ValueError as exc:
        raise BadRequest(str(exc)) from None
    if not changes:
        raise BadRequest("Нечего менять.")
    async with state_of(request).pool.acquire() as conn:
        out = await confirm.apply_owner(conn, rehearsal_service.SETTINGS, {"changes": changes})
    await page._log(request, "rehearsal.settings", detail=rs.audit_detail(out["result"]["settings"], "страница"))
    return JSONResponse({"ok": True, **await rehearsal_view(request)})


def routes() -> list[BaseRoute]:
    return [
        Route(REHEARSAL, rehearsal_get, methods=["GET"]),
        Route(REHEARSAL, rehearsal_put, methods=["PUT"]),
    ]
