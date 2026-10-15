"""Раздел «Репетиция ответов» в «Настройках Штурмана» (решение Р-85).

  GET  rehearsal   настройки, люди списка с именами, есть ли бот согласований и основной аккаунт,
                   статистика за 7 дней (только числа)
  PUT  rehearsal   изменить: {enabled?, people?, daily_cap?, pause_seconds?} — действие самого владельца,
                   сразу (`confirm.apply_owner`); журнал `rehearsal.settings` (в списке важных)

Людей страница ищет тем же маршрутом, что уведомления (`notify/people`): собеседники личных чатов
основного аккаунта, не боты и не служебные чаты Telegram. Отправки в разделе нет: её здесь не
включают, и репетиция её не делает.

Вкладка «Репетиция» приложения «Штурман» (П4, решение владельца «Второй. Принимаю риск.», Р-89):

  GET  app/rehearsal          способ работы, настройки, люди с именами, статистика за 7 дней и последние
                              карточки: кто, исход, оценка и свой вариант владельца (его слова) — без
                              предложенного ответа и без текста собеседника (их в базе нет)
  PUT  app/rehearsal          {enabled?, people?, daily_cap?, pause_seconds?} — сразу, и расширение тоже:
                              так решил владелец. Цена — войти в приложение может и держатель токена бота
                              согласований (Р-80), а с ним — включить репетицию, добавить людей и поднять
                              предел. Поэтому каждое изменение отсюда — запись в журнал
                              (`rehearsal.settings`, «через: приложение») и сообщение владельцу в бот
                              «В приложении изменена репетиция: … Вернуть: …» с командами отката
  GET  app/rehearsal/people   собеседники личных чатов основного аккаунта — поиск по имени, без номеров

Предел в сутки и паузу приложение меняет так же, как включение и список людей (дополнение к Р-89):
сразу, с журналом и сообщением в бот (`/rehearsal cap N`, `/rehearsal pause S` — вернуть). Внутренний
API (`PUT /api/rehearsal`, роль дашборда в плагине) не меняется: там расширение по-прежнему ждёт нажатия
в боте. В способе «Через бота и помощника» вкладка показывает одну строку, почему репетиции там нет.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from .. import authority, bridge, confirm, words
from ..api_core import BadRequest
from ..executor import owner_views
from ..sanitize import clean_line
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


# --- вкладка «Репетиция» приложения «Штурман» ---------------------------------------------------------

APP_REHEARSAL = page.API + "/app/rehearsal"
RECENT = 20                  # последних карточек во вкладке
OUTCOME_WORDS = {"reply": "предложил ответ", "decline": "не стал бы отвечать", "ask_owner": "спросил бы вас",
                 "need_source": "попросил бы ещё источник"}


async def app_view(request: Request) -> dict[str, Any]:
    state = state_of(request)
    async with state.ro_pool.acquire() as conn:
        mode = await owner_views.work_mode(conn)
        settings = await rs.load(conn)
        stats = await rehearsal.stats(conn)
        people = await rehearsal.people_names(conn, settings["people"])
        rows = await conn.fetch(
            """SELECT r.id, r.created_at, r.outcome, r.rating, r.owner_variant, r.on_demand,
                      COALESCE(c.title, p.name) AS who
               FROM rehearsal_items r JOIN chats c ON c.id = r.chat_id JOIN peers p ON p.id = c.peer_id
               WHERE r.state = 'shown' AND r.created_at > now() - interval '7 days'
               ORDER BY r.id DESC LIMIT $1""", RECENT)
    recent = [{"id": r["id"], "at": r["created_at"].isoformat(), "who": clean_line(r["who"], 80) or None,
               "outcome": r["outcome"], "outcome_text": OUTCOME_WORDS.get(r["outcome"] or "", ""),
               "rating": r["rating"], "rating_text": rehearsal.RATING_WORDS.get(r["rating"] or ""),
               "variant": r["owner_variant"], "on_demand": r["on_demand"]} for r in rows]
    return {"mode": mode, "available": mode != "staff", "own_bot": bridge.owns_bot(),
            "unavailable_text": words.REHEARSAL_ELSEWHERE if mode == "staff" else None,
            "settings": {"enabled": settings["enabled"], "people": settings["people"],
                         "daily_cap": settings["daily_cap"], "pause_seconds": settings["pause_seconds"]},
            "people": people, "stats": stats, "recent": recent,
            "limits": {"people": rs.MAX_PEOPLE, "daily_cap": list(rs.DAILY_CAP), "pause_seconds": list(rs.PAUSE)}}


async def app_rehearsal_get(request: Request) -> JSONResponse:
    return JSONResponse(await app_view(request))


def _undo(before: dict[str, Any], after: dict[str, Any], names: dict[int, str]) -> tuple[list[str], list[str]]:
    """Что изменилось и как вернуть — строками для сообщения владельцу."""
    what, undo = [], []
    if before["enabled"] != after["enabled"]:
        what.append("репетиция " + ("включена" if after["enabled"] else "выключена"))
        undo.append("/rehearsal " + ("off" if after["enabled"] else "on"))
    for tg_id in sorted(set(after["people"]) - set(before["people"])):
        what.append(f"добавлен в список: {names.get(tg_id) or f'№ {tg_id}'}")
        undo.append(f"/rehearsal remove {tg_id}")
    for tg_id in sorted(set(before["people"]) - set(after["people"])):
        what.append(f"убран из списка: {names.get(tg_id) or f'№ {tg_id}'}")
        undo.append(f"/rehearsal add {tg_id}")
    if before["daily_cap"] != after["daily_cap"]:
        what.append(f"предел — {after['daily_cap']} в сутки (был {before['daily_cap']})")
        undo.append(f"/rehearsal cap {before['daily_cap']}")
    if before["pause_seconds"] != after["pause_seconds"]:
        what.append(f"пауза между карточками — {after['pause_seconds']} с (была {before['pause_seconds']} с)")
        undo.append(f"/rehearsal pause {before['pause_seconds']}")
    return what, undo


APP_FIELDS = {"daily_cap": ("Карточек в сутки", rs.DAILY_CAP, ""),
              "pause_seconds": ("Пауза между карточками", rs.PAUSE, " секунд")}


def _app_words(error: str) -> str:
    """Отказ проверки словами приложения: поле — как оно подписано во вкладке."""
    for key, (label, (lo, hi), unit) in APP_FIELDS.items():
        if error.startswith(f"поле {key}:"):
            return f"«{label}» — целое число от {lo} до {hi}{unit}."
    return error


async def app_rehearsal_put(request: Request) -> JSONResponse:
    from . import audit, miniapp
    data = await page._body(request)
    try:
        changes = rs.validate(data)
    except ValueError as exc:
        raise BadRequest(_app_words(str(exc))) from None
    if not changes:
        raise BadRequest("Нечего менять.")
    state, session = state_of(request), request.state.app_session
    async with state.pool.acquire() as conn:
        mode = await owner_views.work_mode(conn)
        if mode == "staff":
            raise BadRequest("В способе «Через бота и помощника» репетиции нет — только черновики ответов.",
                             409, "wrong_mode")
        before = await rs.load(conn)
        if "people" in changes:
            try:
                await rehearsal.check_people(conn, sorted(set(changes["people"]) - set(before["people"])))
            except ValueError as exc:
                raise BadRequest(str(exc)) from None
        with authority.miniapp_context(session.user_id, chat_id=session.chat_id):
            out = await confirm.apply_owner(conn, rehearsal_service.SETTINGS, {"changes": changes})
        after = out["result"]["settings"]
        names = {p["tg_id"]: p["name"] for p in await rehearsal.people_names(
            conn, sorted(set(before["people"]) | set(after["people"])))}
    await audit.write(state.pool, "rehearsal.settings", detail=rs.audit_detail(after, "приложение"))
    what, undo = _undo(before, after, names)
    if what:
        await miniapp._tell(state, "В приложении изменена репетиция: " + "; ".join(what) + ".\nВернуть: "
                            + ", ".join(undo) + " — или во вкладке «Репетиция» приложения. Если это были не вы — "
                            "верните и нажмите «Выйти на всех устройствах» в «Настройках Штурмана».")
    return JSONResponse({"ok": True, **await app_view(request)})


async def app_rehearsal_people(request: Request) -> JSONResponse:
    from . import notify as notify_page
    return await notify_page.people_search(request)


def routes() -> list[BaseRoute]:
    from . import miniapp
    return [
        Route(REHEARSAL, rehearsal_get, methods=["GET"]),
        Route(REHEARSAL, rehearsal_put, methods=["PUT"]),
        Route(APP_REHEARSAL, miniapp.endpoint(app_rehearsal_get), methods=["GET"]),
        Route(APP_REHEARSAL, miniapp.endpoint(app_rehearsal_put), methods=["PUT"]),
        Route(APP_REHEARSAL + "/people", miniapp.endpoint(app_rehearsal_people), methods=["GET"]),
    ]
