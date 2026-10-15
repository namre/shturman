"""«Остановить всё» и «Продолжить» на странице настройки и в приложении «Штурман» (Х2, Р-87).

Страница (`PREFIX/api/…`, сессия страницы):
  GET   halt           остановлено ли, с какого времени, откуда, сколько аккаунтов на паузе, ждёт ли
                       подтверждения «Продолжить»
  POST  halt           остановить: {reading?: true — и перестать читать переписку}. Сразу (ужесточение)
  POST  halt/resume    попросить «Продолжить»: {reading?: true — и снова читать переписку}. Само ничего
                       не снимает: в бот согласований уходит карточка подтверждения (`halt.resume`), и
                       только нажатие владельца в боте снимает остановку

Приложение (`PREFIX/api/app/…`, сессия приложения) — те же два действия: `app/halt`, `app/halt/resume`.

После остановки владелец получает сообщение в боте: что остановлено, что отменено (числа), как
продолжить. Журнал действий: `halt.stop` (откуда и что отменено), `halt.resume_request` (откуда), сам
`halt.resume` пишется, когда владелец нажал в боте (`shturman/halt.py`).
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from .. import authority, bridge, confirm, halt
from ..api_core import BadRequest
from ..app import AppState, state_of
from . import service as page

HALT = page.API + "/halt"
APP_HALT = page.API + "/app/halt"


async def view(state: AppState) -> dict[str, Any]:
    async with state.ro_pool.acquire() as conn:
        current = await halt.current(conn)
        waiting = await conn.fetchrow(
            """SELECT id, expires_at FROM pending_actions WHERE kind = $1 AND status = 'pending'
                 AND expires_at > now() ORDER BY id DESC LIMIT 1""", halt.RESUME_KIND)
        resumes = await halt.what_resumes(conn, state) if current["stopped"] else []
    out = halt.public(current)
    out["resumes"] = resumes
    out["resume_pending"] = {"action_id": waiting["id"], "expires_at": waiting["expires_at"].isoformat()} \
        if waiting is not None and current["stopped"] else None
    out["own_bot"] = bridge.owns_bot()
    return out


async def tell_stopped(state: AppState, result: dict[str, Any]) -> None:
    """Сообщение владельцу в бот после остановки со страницы или из приложения."""
    from ..executor import owner_chat
    rows: list[list[dict[str, Any]]] = [[bridge.button(owner_chat.RESUME_BUTTON + "…", owner_chat.MODULE, "resume")]]
    if not result["halt"].get("paused_accounts"):
        rows.append([bridge.button("Ещё и перестать читать переписку", owner_chat.MODULE, "pausereading")])
    if bridge.owns_bot():
        # Кнопку приложения ставит сам исполнитель бота по адресу, который знает сервис (worker.parse_buttons).
        rows.append([{"text": owner_chat.OPEN_APP, "app": True}])
    async with state.pool.acquire() as conn:
        await bridge.notify_owner(conn, halt.stopped_message(result, state.config), buttons=rows)


def _reading(data: dict[str, Any]) -> bool:
    value = data.get("reading", False)
    if not isinstance(value, bool):
        raise BadRequest("поле reading: true или false")
    return value


async def _stop(request: Request, via: str) -> JSONResponse:
    state = state_of(request)
    reading = _reading(await page._body(request))
    result = await halt.stop(state, via=via, reading=reading)
    await tell_stopped(state, result)
    return JSONResponse({"ok": True, "already": result["already"], "cancelled": result["counts"],
                         "paused": len(result["paused"]), **await view(state)})


async def _resume(request: Request, via: str) -> JSONResponse:
    state = state_of(request)
    reading = _reading(await page._body(request))
    async with state.pool.acquire() as conn:
        try:
            out = await halt.request_resume(conn, state, via=via, reading=reading)
        except confirm.NoOwner:
            raise BadRequest("Продолжить можно только нажатием в боте согласований, а бот не настроен или "
                             "владелец к нему не привязан.", 409, "owner_unknown") from None
        except confirm.TooManyPending:
            raise BadRequest("В боте уже ждут решения много карточек. Ответьте на них и повторите.", 429,
                             "too_many_pending") from None
    from . import audit
    await audit.write(state.pool, "halt.resume_request", detail=f"откуда: {via}; чтение переписки: "
                      + ("снова" if reading else "как есть"))
    return JSONResponse({"ok": True, "status": out["status"], "action_id": out.get("action_id"),
                         "note": "Подтвердите «Продолжить» в боте согласований — карточка уже там.",
                         **await view(state)}, status_code=202)


@page.endpoint
async def halt_get(request: Request) -> JSONResponse:
    return JSONResponse(await view(state_of(request)))


@page.endpoint
async def halt_stop(request: Request) -> JSONResponse:
    return await _stop(request, "page")


@page.endpoint
async def halt_resume(request: Request) -> JSONResponse:
    return await _resume(request, "page")


async def app_halt_get(request: Request) -> JSONResponse:
    return JSONResponse(await view(state_of(request)))


async def app_halt_stop(request: Request) -> JSONResponse:
    session = request.state.app_session
    with authority.miniapp_context(session.user_id, chat_id=session.chat_id):
        return await _stop(request, "app")


async def app_halt_resume(request: Request) -> JSONResponse:
    session = request.state.app_session
    with authority.miniapp_context(session.user_id, chat_id=session.chat_id):
        return await _resume(request, "app")


def routes() -> list[BaseRoute]:
    from . import miniapp

    return [
        Route(HALT, halt_get, methods=["GET"]),
        Route(HALT, halt_stop, methods=["POST"]),
        Route(HALT + "/resume", halt_resume, methods=["POST"]),
        Route(APP_HALT, miniapp.endpoint(app_halt_get), methods=["GET"]),
        Route(APP_HALT, miniapp.endpoint(app_halt_stop), methods=["POST"]),
        Route(APP_HALT + "/resume", miniapp.endpoint(app_halt_resume), methods=["POST"]),
    ]
