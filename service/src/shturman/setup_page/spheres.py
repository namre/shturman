"""Сферы на странице настройки и в приложении «Штурман» (решение Р-82, docs/spheres.md).

Страница («Переписка», раздел «Сферы»; `PREFIX/api/…`, сессия страницы):

  GET    spheres                              сферы, предложения связанных папок
  POST   spheres                              завести: {title, description?}
  PUT    spheres/order                        порядок: {ids}
  PUT    spheres/{id}                         {title?, description?}
  DELETE spheres/{id}                         удалить = снять с чатов и проектов (сами они остаются)
  GET    spheres/{id}/chats                   чаты сферы
  POST   spheres/{id}/chats                   {account_id, chats: [{peer_class, tg_id}], add: true|false}
  PUT    spheres/chat                         сферы одного чата: {account_id, peer_class, tg_id, sphere_ids}
  GET    spheres/folders                      папки Telegram аккаунтов: сколько чатов, с какими сферами связаны
  POST   spheres/folders/make                 «Сделать сферу из папки»: {account_id, folder_id, sphere_id?}
  POST   spheres/{id}/folders                 «Связать папку со сферой»: {account_id, folder_id, link}
  POST   spheres/{id}/proposals               новые чаты связанной папки: {account_id, folder_id, accept}
  PUT    memory/projects/{id}/sphere          сфера проекта: {sphere_id | null}
  GET    tg/accounts/{id}/sections            раскладка списка чатов: ?layout&q&kind&only
  POST   tg/accounts/{id}/bulk                «Включить все» / «Выключить все» в разделе: {section, enabled, months?, q?, kind?, only?}

Приложение (`PREFIX/api/app/…`, сессия приложения) — то же, кроме проектов (в приложении их нет), и
массово — только выключить: `app/chats/{id}/bulk` с enabled=true отвергается (Р-80: включать чтение из
приложения можно только по одному чату). Изменение сфер в приложении не расширяет чтения: оно пишется
в журнал действий страницы с пометкой «из приложения», а сообщение в бот уходит только о выключении
чтения (как у экрана «Чаты»).

Изменение сфер здесь — действие самого владельца: применяется сразу (как и остальное на странице).
Через внутренний API то же самое ждёт его нажатия в боте согласований (`spheres_service.py`).

Журнал действий: `sphere.*` и `app.sphere_*`, `tg.bulk_on|off`, `app.bulk_off` — номера сфер, папок и
число чатов; ни названий сфер, ни названий чатов.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from .. import authority, spheres
from ..api_core import BadRequest
from ..app import state_of
from ..tg import dialogs as dl
from ..tg import sync
from . import audit, chats
from . import service as page

SPHERES = page.API + "/spheres"
APP_SPHERES = page.API + "/app/spheres"
_STATUS = {"not_found": 404, "exists": 409, "too_many": 409, "changed_meanwhile": 409}


def _bad(exc: spheres.SpheresError) -> BadRequest:
    return BadRequest(exc.message, _STATUS.get(exc.code, 400), exc.code)


def _int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BadRequest(f"поле {key}: нужно положительное целое число")
    return value


def _sid(request: Request) -> int:
    return int(request.path_params["sphere_id"])


async def _account(state: Any, data: dict[str, Any]) -> int:
    account_id = _int(data, "account_id")
    async with state.ro_pool.acquire() as conn:
        if not await conn.fetchval("SELECT 1 FROM accounts WHERE id = $1", account_id):
            raise BadRequest("Такого аккаунта нет.", 404, "not_found")
    return account_id


Log = Callable[[Request, str, str], Awaitable[None]]


def _logger(app: bool) -> Log:
    async def log(request: Request, action: str, detail: str = "") -> None:
        await page._log(request, ("app.sphere_" if app else "sphere.") + action, audit.OK, detail)
    return log


# --- общие действия ----------------------------------------------------------------------------------

async def overview(request: Request) -> JSONResponse:
    async with state_of(request).ro_pool.acquire() as conn:
        listed = await spheres.list_spheres(conn)
        offers = await spheres.proposals(conn)
    for p in offers:
        p.pop("keys")
    return JSONResponse({"spheres": listed, "proposals": offers, "limit": spheres.MAX_SPHERES})


def create(log: Log):
    async def handler(request: Request) -> JSONResponse:
        data = await page._body(request)
        async with state_of(request).pool.acquire() as conn:
            try:
                item = await spheres.create_sphere(conn, data.get("title"), data.get("description"))
            except spheres.SpheresError as exc:
                raise _bad(exc) from None
        await log(request, "create", f"сфера № {item['id']}")
        return JSONResponse({"ok": True, "sphere": item})
    return handler


def update(log: Log):
    async def handler(request: Request) -> JSONResponse:
        data = await page._body(request)
        changes = {k: data[k] for k in ("title", "description") if k in data}
        if not changes:
            raise BadRequest("нужно поле title или description")
        async with state_of(request).pool.acquire() as conn:
            try:
                item = await spheres.update_sphere(conn, _sid(request), **changes)
            except spheres.SpheresError as exc:
                raise _bad(exc) from None
        await log(request, "update", f"сфера № {item['id']}; " + ", ".join(
            {"title": "название", "description": "описание"}[k] for k in changes))
        return JSONResponse({"ok": True, "sphere": item})
    return handler


def order(log: Log):
    async def handler(request: Request) -> JSONResponse:
        data = await page._body(request)
        ids = data.get("ids")
        if not isinstance(ids, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
            raise BadRequest("поле ids: нужен список номеров сфер")
        async with state_of(request).pool.acquire() as conn:
            try:
                listed = await spheres.reorder(conn, ids)
            except spheres.SpheresError as exc:
                raise _bad(exc) from None
        await log(request, "update", "порядок сфер")
        return JSONResponse({"ok": True, "spheres": listed})
    return handler


def delete(log: Log):
    async def handler(request: Request) -> JSONResponse:
        await page._body(request)
        async with state_of(request).pool.acquire() as conn:
            try:
                out = await spheres.delete_sphere(conn, _sid(request))
            except spheres.SpheresError as exc:
                raise _bad(exc) from None
        await log(request, "delete", f"сфера № {out['id']}; снята с чатов: {out['chats']}, проектов: {out['projects']}")
        out.pop("title")
        return JSONResponse({"ok": True, **out})
    return handler


async def chats_of(request: Request) -> JSONResponse:
    async with state_of(request).ro_pool.acquire() as conn:
        try:
            return JSONResponse({"chats": await spheres.sphere_chats(conn, _sid(request))})
        except spheres.SpheresError as exc:
            raise _bad(exc) from None


def change_chats(log: Log):
    async def handler(request: Request) -> JSONResponse:
        data = await page._body(request)
        state = state_of(request)
        account_id = await _account(state, data)
        keys = chats.peer_keys(data.get("chats"))
        add = page._flag(data, "add", required=True)
        async with state.pool.acquire() as conn:
            try:
                n = await (spheres.add_chats if add else spheres.remove_chats)(conn, _sid(request), account_id, keys)
            except spheres.SpheresError as exc:
                raise _bad(exc) from None
        await log(request, "chats", f"сфера № {_sid(request)}; {'добавлено' if add else 'убрано'} чатов: {n}")
        return JSONResponse({"ok": True, "changed": n})
    return handler


def chat_spheres(log: Log):
    async def handler(request: Request) -> JSONResponse:
        data = await page._body(request)
        state = state_of(request)
        account_id = await _account(state, data)
        (key,) = chats.peer_keys([data])
        ids = data.get("sphere_ids")
        if not isinstance(ids, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
            raise BadRequest("поле sphere_ids: нужен список номеров сфер")
        async with state.pool.acquire() as conn:
            try:
                out = await spheres.set_chat_spheres(conn, account_id, key, ids)
            except spheres.SpheresError as exc:
                raise _bad(exc) from None
            now = (await spheres.spheres_of_keys(conn, account_id, [key])).get(key, [])
        if out["added"] or out["removed"]:
            await log(request, "chats", f"чат {key[0]}:{key[1]}; добавлен в сферы: {len(out['added'])}, "
                                        f"убран из сфер: {len(out['removed'])}")
        return JSONResponse({"ok": True, "spheres": now, **out})
    return handler


async def folders_list(request: Request) -> JSONResponse:
    """Папки Telegram каждого аккаунта: название, сколько в них чатов, оговорки о точности и связанные сферы."""
    async with state_of(request).ro_pool.acquire() as conn:
        accounts = await conn.fetch(
            """SELECT a.id, a.label, a.role FROM accounts a
               WHERE EXISTS (SELECT 1 FROM tg_folders f WHERE f.account_id = a.id) ORDER BY (a.role = 'owner') DESC, a.id""")
        links = await conn.fetch(
            """SELECT f.account_id, f.folder_id, f.propose, s.id, s.title FROM sphere_folders f
               JOIN spheres s ON s.id = f.sphere_id ORDER BY s.position, s.id""")
        now = await conn.fetchval("SELECT now()")
        out = []
        for a in accounts:
            rows = await dl.rows(conn, a["id"])
            for f in await dl.folders(conn, a["id"]):
                out.append({
                    "account_id": a["id"], "account_label": a["label"], "account_role": a["role"],
                    "folder_id": f.folder_id, "title": f.title, "emoticon": f.emoticon, "kind": f.kind,
                    "count": len(spheres.folder_members(f, rows, now=now)), "notes": spheres.folder_notes(f, rows),
                    "spheres": [{"id": r["id"], "title": r["title"], "propose": r["propose"]} for r in links
                                if r["account_id"] == a["id"] and r["folder_id"] == f.folder_id]})
    return JSONResponse({"folders": out})


def make_from_folder(log: Log):
    async def handler(request: Request) -> JSONResponse:
        data = await page._body(request)
        state = state_of(request)
        account_id = await _account(state, data)
        folder_id = _int(data, "folder_id")
        sphere_id = _int(data, "sphere_id") if data.get("sphere_id") is not None else None
        async with state.pool.acquire() as conn:
            try:
                out = await spheres.make_from_folder(conn, account_id, folder_id, sphere_id=sphere_id)
            except spheres.SpheresError as exc:
                raise _bad(exc) from None
        await log(request, "folder", f"папка № {folder_id} → сфера № {out['sphere']['id']}"
                                     f"{' (новая)' if out['created'] else ''}; добавлено чатов: {out['added']}")
        return JSONResponse({"ok": True, **out})
    return handler


def link_folder(log: Log):
    async def handler(request: Request) -> JSONResponse:
        data = await page._body(request)
        state = state_of(request)
        account_id = await _account(state, data)
        folder_id = _int(data, "folder_id")
        link = page._flag(data, "link", required=True)
        async with state.pool.acquire() as conn:
            try:
                item = await spheres.link_folder(conn, _sid(request), account_id, folder_id, propose=bool(link))
                offers = [p for p in await spheres.proposals(conn, _sid(request))
                          if p["account_id"] == account_id and p["folder_id"] == folder_id]
            except spheres.SpheresError as exc:
                raise _bad(exc) from None
        await log(request, "folder", f"папка № {folder_id} {'связана со сферой' if link else 'отвязана от сферы'} "
                                     f"№ {_sid(request)}")
        return JSONResponse({"ok": True, "sphere": item, "proposed": offers[0]["count"] if offers else 0})
    return handler


def decide_proposals(log: Log):
    async def handler(request: Request) -> JSONResponse:
        data = await page._body(request)
        state = state_of(request)
        account_id = await _account(state, data)
        folder_id = _int(data, "folder_id")
        accept = page._flag(data, "accept", required=True)
        keys = chats.peer_keys(data["chats"]) if data.get("chats") is not None else None
        async with state.pool.acquire() as conn:
            try:
                out = await spheres.decide_proposals(conn, _sid(request), account_id, folder_id,
                                                     accept=bool(accept), keys=keys)
            except spheres.SpheresError as exc:
                raise _bad(exc) from None
        await log(request, "chats", f"сфера № {_sid(request)}; папка № {folder_id}; "
                                    + (f"добавлено чатов: {out['added']}" if accept else f"не предлагать: {out['declined']}"))
        return JSONResponse({"ok": True, **out})
    return handler


# --- только страница -----------------------------------------------------------------------------

@page.endpoint
async def project_sphere(request: Request) -> JSONResponse:
    data = await page._body(request)
    if "sphere_id" not in data:
        raise BadRequest("поле sphere_id: номер сферы или null")
    sphere_id = None if data["sphere_id"] is None else _int(data, "sphere_id")
    project_id = int(request.path_params["project_id"])
    async with state_of(request).pool.acquire() as conn:
        try:
            out = await spheres.set_project_sphere(conn, project_id, sphere_id)
        except spheres.SpheresError as exc:
            raise _bad(exc) from None
    if out["changed"]:
        await page._log(request, "sphere.project", detail=f"проект № {project_id} → "
                        + (f"сфера № {sphere_id}" if sphere_id else "без сферы"))
    return JSONResponse({"ok": True, **out})


@page.endpoint
async def page_sections(request: Request) -> JSONResponse:
    state = state_of(request)
    return JSONResponse(await chats.sections(state, chats.manager(state), int(request.path_params["account_id"]),
                                             request))


@page.endpoint
async def page_bulk(request: Request) -> JSONResponse:
    data = await page._body(request)
    state = state_of(request)
    enabled = bool(page._flag(data, "enabled", required=True))
    months = chats.months_of(data) if enabled else sync.ACCOUNT_DEFAULT
    account_id = int(request.path_params["account_id"])
    out = await chats.bulk(state, chats.manager(state), account_id, request, data, enabled=enabled,
                           since=await chats.since_of(state, months))
    detail = f"раздел {out['section']}; чатов: {out['changed']}"
    if enabled:
        detail += f"; пропущено исключённых: {out['skipped']}; глубина: {chats.depth_words(months) if months is not sync.ACCOUNT_DEFAULT else 'по умолчанию'}"
    await page._log(request, "tg.bulk_on" if enabled else "tg.bulk_off", detail=detail)
    return JSONResponse({"ok": True, **out})


# --- только приложение ---------------------------------------------------------------------------

async def app_sections(request: Request) -> JSONResponse:
    state = state_of(request)
    return JSONResponse(await chats.sections(state, chats.manager(state), int(request.path_params["account_id"]),
                                             request))


async def app_bulk(request: Request) -> JSONResponse:
    """В приложении массово — только выключить (Р-80: включение — по одному чату, с сообщением владельцу)."""
    from . import miniapp
    data = await page._body(request)
    enabled = page._flag(data, "enabled", required=True)
    if enabled:
        await page._log(request, "app.bulk_on", audit.REFUSED, "массовое включение — только на странице")
        raise BadRequest("Из приложения чаты включаются по одному. Включить все чаты раздела можно в "
                         "«Настройках Штурмана».", 400, "page_only")
    state, session = state_of(request), request.state.app_session
    account_id = int(request.path_params["account_id"])
    with authority.miniapp_context(session.user_id, chat_id=session.chat_id):
        out = await chats.bulk(state, chats.manager(state), account_id, request, data, enabled=False)
    await page._log(request, "app.bulk_off", detail=f"раздел {out['section']}; чатов: {out['changed']}")
    if out["changed"]:
        title = " ".join(str(out["title"] or "").split())[:miniapp.TITLE_LIMIT] or "без названия"
        await miniapp._tell(state, f"Из приложения выключено чтение всех чатов раздела «{title}»: {out['changed']}.")
    return JSONResponse({"ok": True, **out})


# --- сборка ----------------------------------------------------------------------------------------

def routes() -> list[BaseRoute]:
    from . import miniapp

    page_log, app_log = _logger(False), _logger(True)
    out: list[BaseRoute] = []
    for base, wrap, log in ((SPHERES, page.endpoint, page_log), (APP_SPHERES, miniapp.endpoint, app_log)):
        one = base + "/{sphere_id:int}"
        out += [
            Route(base, wrap(overview), methods=["GET"]),
            Route(base, wrap(create(log)), methods=["POST"]),
            Route(base + "/order", wrap(order(log)), methods=["PUT"]),
            Route(base + "/chat", wrap(chat_spheres(log)), methods=["PUT"]),
            Route(base + "/folders", wrap(folders_list), methods=["GET"]),
            Route(base + "/folders/make", wrap(make_from_folder(log)), methods=["POST"]),
            Route(one, wrap(update(log)), methods=["PUT"]),
            Route(one, wrap(delete(log)), methods=["DELETE"]),
            Route(one + "/chats", wrap(chats_of), methods=["GET"]),
            Route(one + "/chats", wrap(change_chats(log)), methods=["POST"]),
            Route(one + "/folders", wrap(link_folder(log)), methods=["POST"]),
            Route(one + "/proposals", wrap(decide_proposals(log)), methods=["POST"]),
        ]
    account = page.API + "/tg/accounts/{account_id:int}"
    app_account = page.API + "/app/chats/{account_id:int}"
    out += [
        Route(page.API + "/memory/projects/{project_id:int}/sphere", project_sphere, methods=["PUT"]),
        Route(account + "/sections", page_sections, methods=["GET"]),
        Route(account + "/bulk", page_bulk, methods=["POST"]),
        Route(app_account + "/sections", miniapp.endpoint(app_sections), methods=["GET"]),
        Route(app_account + "/bulk", miniapp.endpoint(app_bulk), methods=["POST"]),
    ]
    return out
