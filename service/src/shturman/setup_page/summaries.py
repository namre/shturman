"""Блок «Утренняя сводка и обзор недели» на странице «Настройки Штурмана» и строка во вкладке «🔔»
приложения «Штурман» (Р-95, находка Х3). Сам выбор и его путь к Hermes — `shturman/summaries.py`.

  GET  summaries    выбор владельца, что сейчас в Hermes (есть ли задача, включена ли, время, следующий
                    запуск по поясу шлюза), совпадают ли; available — плагин Hermes хоть раз ответил
                    (в режиме без Hermes блока нет)
  PUT  summaries    {"morning-brief"?: {enabled?, time?}, "weekly-review"?: {enabled?, time?, day?}, again?: true}
                    — сразу (сводка приходит самому владельцу); журнал `summaries.schedule`, «История»

То же для приложения — `app/summaries` (сразу, журнал с пометкой «приложение» и короткое сообщение в бот).
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from .. import authority, summaries
from ..api_core import BadRequest
from ..app import state_of
from . import service as page

SUMMARIES = page.API + "/summaries"
APP_SUMMARIES = page.API + "/app/summaries"


async def view(request: Request) -> dict[str, Any]:
    async with state_of(request).ro_pool.acquire() as conn:
        return await summaries.view(conn)


@page.endpoint
async def summaries_get(request: Request) -> JSONResponse:
    return JSONResponse(await view(request))


def _detail(value: dict[str, Any], via: str) -> str:
    m, w = value["morning-brief"], value["weekly-review"]
    return (f"утро: {m['time']} {'вкл' if m['enabled'] else 'выкл'}; неделя: {summaries.DAYS[int(w.get('day', 5))]} "
            f"{w['time']} {'вкл' if w['enabled'] else 'выкл'}; через: {via}")


async def _save(request: Request) -> tuple[dict[str, Any], bool]:
    data = await page._body(request)
    again = data.pop("again", False) is True
    state = state_of(request)
    async with state.pool.acquire() as conn:
        try:
            value = await summaries.set_desired(conn, data, state=state, again=again)
        except ValueError as exc:
            raise BadRequest(str(exc)) from None
    return value, again


@page.endpoint
async def summaries_put(request: Request) -> JSONResponse:
    value, again = await _save(request)
    await page._log(request, "summaries.schedule", detail=_detail(value, "страница") + ("; применить снова" if again else ""))
    return JSONResponse({"ok": True, **await view(request)})


async def app_summaries_get(request: Request) -> JSONResponse:
    return JSONResponse(await view(request))


async def app_summaries_put(request: Request) -> JSONResponse:
    from . import miniapp
    session = request.state.app_session
    with authority.miniapp_context(session.user_id, chat_id=session.chat_id):
        value, again = await _save(request)
    await page._log(request, "summaries.schedule", detail=_detail(value, "приложение") + ("; применить снова" if again else ""))
    await miniapp._tell(state_of(request), "В приложении «Штурман» изменено расписание сводок: "
                        + "; ".join(summaries.words(k, value[k]) for k in summaries.KEYS) + ".")
    return JSONResponse({"ok": True, **await view(request)})


def routes() -> list[BaseRoute]:
    from . import miniapp
    return [
        Route(SUMMARIES, summaries_get, methods=["GET"]),
        Route(SUMMARIES, summaries_put, methods=["PUT"]),
        Route(APP_SUMMARIES, miniapp.endpoint(app_summaries_get), methods=["GET"]),
        Route(APP_SUMMARIES, miniapp.endpoint(app_summaries_put), methods=["PUT"]),
    ]
