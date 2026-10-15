"""Блок «Разбор входящих» в карточке «Уведомления» «Настроек Штурмана» (решение Р-92).

  GET   triage          включён ли, ждёт ли включение нажатия в боте, правила с именами людей и названиями
                        групп, сферы, срок напоминания, счётчики за сутки, оценка объёма по последним 7 дням,
                        что уходит модели — только числа и слова, без текста переписки
  PUT   triage          правила и срок: {remind_minutes?, important_people?, lower_people?, quiet_people?,
                        important_chats?, lower_chats?, quiet_chats?, important_spheres?, enabled?: false} —
                        сразу (это сам владелец). `enabled: true` здесь не принимается (400 `bot_only`)
  POST  triage/enable   попросить включить: 202 и карточка `triage.enable` в бот согласований. Само ничего
                        не включает — включает только нажатие владельца в самом боте (не в приложении)

Людей страница ищет тем же маршрутом, что уведомления (`notify/people`), группы — тем же, что
наблюдатель (`watch/chats`). Журнал: `triage.enable_request`, `triage.disable`, `triage.settings` (в списке
важных; числа, без имён и номеров), `triage.enable` пишет нажатие в боте (`triage/service.py`).
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from .. import bridge, confirm, halt
from ..api_core import BadRequest
from ..app import state_of
from ..rehearsal import core as rehearsal
from ..triage import core as triage
from ..triage import service as triage_service
from ..triage import settings as ts
from . import service as page

TRIAGE = page.API + "/triage"


async def triage_view(request: Request) -> dict[str, Any]:
    state = state_of(request)
    async with state.ro_pool.acquire() as conn:
        s = await ts.load(conn)
        people_ids = sorted({i for f in ts.PEOPLE.values() for i in s[f]})
        names = {p["tg_id"]: p for p in await rehearsal.people_names(conn, people_ids)}
        chat_ids = sorted({i for f in ts.CHATS.values() for i in s[f]})
        from .notify import _chat_titles
        titles = await _chat_titles(conn, chat_ids)
        spheres = [{"id": r["id"], "title": r["title"]}
                   for r in await conn.fetch("SELECT id, title FROM spheres ORDER BY position, id")]
        estimate = await triage.estimate(conn)
        out = {
            "settings": s,
            "people": [{**names.get(i, {"tg_id": i, "name": None, "username": None}), "level": level}
                       for level, field in ts.PEOPLE.items() for i in s[field]],
            "chats": [{**(titles.get(i) or {"id": i, "title": None}), "level": level}
                      for level, field in ts.CHATS.items() for i in s[field]],
            "spheres": spheres,
            "counts": await triage.status(conn),
            "estimate": estimate,
            "what_goes": triage_service.what_goes(),
            "pending": await triage_service.pending_enable(conn),
            "stopped": await halt.is_stopped(conn),
            "own_bot": bridge.owns_bot(),
            "remind_choices": list(ts.REMIND_CHOICES),
        }
    return out


@page.endpoint
async def triage_get(request: Request) -> JSONResponse:
    return JSONResponse(await triage_view(request))


@page.endpoint
async def triage_put(request: Request) -> JSONResponse:
    data = await page._body(request)
    if data.get("enabled") is True:
        raise BadRequest("Включить разбор входящих можно только нажатием в боте согласований: кнопка "
                         "«Включить разбор…» пришлёт туда карточку.", 400, "bot_only")
    try:
        changes = ts.validate(data)
    except ValueError as exc:
        raise BadRequest(str(exc)) from None
    if not changes:
        raise BadRequest("Нечего менять.")
    state = state_of(request)
    async with state.pool.acquire() as conn:
        if changes.get("important_spheres"):
            known = {r["id"] for r in await conn.fetch("SELECT id FROM spheres")}
            missing = [i for i in changes["important_spheres"] if i not in known]
            if missing:
                raise BadRequest("Таких сфер нет: " + ", ".join(f"№ {i}" for i in missing))
        async with conn.transaction():
            await ts.lock(conn)
            before = await ts.load(conn)
            after = ts.merge(before, changes)
            await ts.save(conn, after)
            off = before["enabled"] and not after["enabled"]
            fallback = await triage.switch_off(conn) if off else []
            mod = triage.current()
            if fallback and mod is not None:
                await mod.fallback(conn, fallback)
    if off:
        await page._log(request, "triage.disable", detail=ts.audit_detail(after, "страница")
                        + (f"; обычным уведомлением: {len(fallback)}" if fallback else ""))
    if any(k != "enabled" for k in changes):
        await page._log(request, "triage.settings", detail=ts.audit_detail(after, "страница"))
    return JSONResponse({"ok": True, **await triage_view(request)})


@page.endpoint
async def triage_enable(request: Request) -> JSONResponse:
    state = state_of(request)
    async with state.pool.acquire() as conn:
        if not bridge.owns_bot():
            raise BadRequest("Разбор входящих пишет в бот согласований — сначала настройте его (шаг 1).", 409, "no_bot")
        try:
            out = await triage_service.request_enable(conn)
        except confirm.Refused as exc:
            raise BadRequest(exc.message, exc.status, exc.code) from None
        except confirm.NoOwner:
            raise BadRequest("Включить можно только нажатием в боте согласований, а владелец к нему не привязан.",
                             409, "owner_unknown") from None
        except confirm.TooManyPending:
            raise BadRequest("В боте уже ждут решения много карточек. Ответьте на них и повторите.", 429,
                             "too_many_pending") from None
    await page._log(request, "triage.enable_request", detail="откуда: страница")
    return JSONResponse({"ok": True, "status": out["status"], "action_id": out.get("action_id"),
                         "note": "Подтвердите включение в боте согласований — карточка уже там.",
                         **await triage_view(request)}, status_code=202)


def routes() -> list[BaseRoute]:
    return [
        Route(TRIAGE, triage_get, methods=["GET"]),
        Route(TRIAGE, triage_put, methods=["PUT"]),
        Route(TRIAGE + "/enable", triage_enable, methods=["POST"]),
    ]
