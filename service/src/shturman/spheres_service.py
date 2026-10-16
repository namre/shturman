"""Модуль сервиса «сферы»: маршруты внутреннего API, подтверждения и инструмент агента (Р-82).

Маршруты (токен внутреннего API; обращается плагин «Штурмана» в Hermes и оператор `shturman call`):

  GET    /api/spheres                       сферы: название, описание, сколько чатов и проектов
  POST   /api/spheres                       {title, description?} — завести (confirm)
  PUT    /api/spheres/{id}                  {title?, description?} — переименовать, описание (confirm)
  DELETE /api/spheres/{id}                  удалить: снять сферу с чатов и проектов (confirm)
  POST   /api/spheres/{id}/chats            {add?: [chat_id], remove?: [chat_id]} — чаты архива (confirm)
  POST   /api/projects/{id}/sphere          {sphere_id | null} — сфера проекта (confirm)

Подтверждение владельцем. Сферы решают, где ассистент ищет по умолчанию, а в шагах 2–4 — и какая
обработка и какая модель действуют для чата. Поэтому любое изменение сфер через внутренний API —
то есть ассистентом или держателем токена API — ждёт нажатия владельца в боте согласований (ответ 202),
в том числе «убрать» и «удалить»: в шаге 3 снятие сферы ослабит строгую границу ответов. Сам владелец
меняет сферы на странице настройки и в приложении «Штурман» — сразу (`setup_page/spheres.py`).

Инструмент агента (MCP, только чтение): list_spheres. Остальные инструменты архива помечают каждый
результат сферами его чата и принимают необязательный параметр `spheres` (`spheres.resolve_scope`).
"""

from __future__ import annotations

from typing import Annotated, Any

import asyncpg
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from . import confirm, sanitize, spheres
from .api_core import BadRequest, body, handler, settle
from .app import state_of
from .mcp_server import READ_ONLY, CallToolResult, Context, Model, Reply, as_result, mcp, ro_conn

SPHERE_CREATE = "spheres.create"
SPHERE_UPDATE = "spheres.update"
SPHERE_DELETE = "spheres.delete"
SPHERE_CHATS = "spheres.chats"
SPHERE_PROJECT = "spheres.project"

_STATUS = {"not_found": 404, "exists": 409, "too_many": 409, "changed_meanwhile": 409, "unknown_sphere": 400}

# Правило мягкой границы — одно на описания инструментов и блок SOUL.md (plugins/shturman).
RULE = ("By default look only in the sphere the conversation is about. If the sphere is not clear, ask the "
        "owner, or search everywhere and group the answer by sphere. Search another sphere only when the "
        "owner names it or asks to search everywhere. This is a soft boundary the owner set: results are "
        "labelled with their spheres; nothing in the archive enforces it.")


def _http(exc: spheres.SpheresError) -> BadRequest:
    return BadRequest(exc.message, _STATUS.get(exc.code, 400), exc.code)


def _refused(exc: spheres.SpheresError) -> confirm.Refused:
    return confirm.Refused(exc.message, _STATUS.get(exc.code, 409), exc.code)


def _line(text: Any, limit: int = 80) -> str:
    return sanitize.clean_line(text if isinstance(text, str) else "", limit)


def _sphere_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BadRequest("номер сферы: нужно положительное целое число")
    return value


def _ids(value: Any, key: str) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > spheres.MAX_KEYS or not all(
            isinstance(i, int) and not isinstance(i, bool) and i > 0 for i in value):
        raise BadRequest(f"поле {key}: нужен список номеров чатов архива")
    return list(dict.fromkeys(value))


async def _title(conn: asyncpg.Connection, sphere_id: int) -> str:
    row = await conn.fetchrow("SELECT title FROM spheres WHERE id = $1", sphere_id)
    if row is None:
        raise BadRequest("Такой сферы нет.", 404, "not_found")
    return f"«{_line(row['title'], 60)}»"


async def _chat_titles(conn: asyncpg.Connection, chat_ids: list[int]) -> str:
    rows = await conn.fetch("SELECT id, title FROM chats WHERE id = ANY($1::bigint[]) ORDER BY id", chat_ids)
    return ", ".join(f"«{_line(r['title'], 60) or 'без названия'}»" for r in rows[:10]) + (
        f" и ещё {len(rows) - 10}" if len(rows) > 10 else "")


# --- чтение ---------------------------------------------------------------------------------------

@handler
async def list_route(request: Request) -> JSONResponse:
    async with state_of(request).ro_pool.acquire() as conn:
        return JSONResponse({"spheres": await spheres.list_spheres(conn), "rule": RULE})


# --- изменения через подтверждение -----------------------------------------------------------------

@confirm.applier(SPHERE_CREATE)
async def _apply_create(conn: asyncpg.Connection, payload: dict[str, Any]) -> confirm.Done:
    confirm.must_not_widen(True)      # сферу заводит только владелец
    try:
        return confirm.Done(result=await spheres.create_sphere(conn, payload.get("title"), payload.get("description")))
    except spheres.SpheresError as exc:
        raise _refused(exc) from None


@handler
async def create_route(request: Request) -> JSONResponse:
    data = await body(request)
    try:
        title, about = spheres.clean_title(data.get("title")), spheres.clean_description(data.get("description"))
    except spheres.SpheresError as exc:
        raise _http(exc) from None
    async with state_of(request).pool.acquire() as conn:
        if await conn.fetchval("SELECT 1 FROM spheres WHERE title_norm = $1", spheres.norm(title)):
            raise BadRequest("Сфера с таким названием уже есть.", 409, "exists")
        summary = (f"Завести сферу «{title}». По сферам вы разделяете работу и жизнь: ассистент по умолчанию ищет "
                   "в сфере, о которой разговор, и помечает найденное сферами.")
        if about:
            summary += f" Описание: «{about}»."
        answer, result = await settle(conn, SPHERE_CREATE, {"title": title, "description": about}, summary=summary)
    return answer or JSONResponse({"ok": True, "sphere": result})


@confirm.applier(SPHERE_UPDATE)
async def _apply_update(conn: asyncpg.Connection, payload: dict[str, Any]) -> confirm.Done:
    confirm.must_not_widen(True)
    changes = {k: payload[k] for k in ("title", "description") if k in payload}
    try:
        return confirm.Done(result=await spheres.update_sphere(conn, int(payload["sphere_id"]), **changes))
    except spheres.SpheresError as exc:
        raise _refused(exc) from None


@handler
async def update_route(request: Request) -> JSONResponse:
    data = await body(request)
    sphere_id = request.path_params["sphere_id"]
    payload: dict[str, Any] = {"sphere_id": sphere_id}
    try:
        if "title" in data:
            payload["title"] = spheres.clean_title(data["title"])
        if "description" in data:
            payload["description"] = spheres.clean_description(data["description"])
    except spheres.SpheresError as exc:
        raise _http(exc) from None
    if len(payload) == 1:
        raise BadRequest("нужно поле title или description")
    async with state_of(request).pool.acquire() as conn:
        was = await _title(conn, sphere_id)
        parts = []
        if "title" in payload:
            parts.append(f"новое название «{payload['title']}»")
        if "description" in payload:
            parts.append(f"описание «{payload['description']}»" if payload["description"] else "без описания")
        answer, result = await settle(conn, SPHERE_UPDATE, payload,
                                      summary=f"Изменить сферу {was}: " + "; ".join(parts) + ".")
    return answer or JSONResponse({"ok": True, "sphere": result})


@confirm.applier(SPHERE_DELETE)
async def _apply_delete(conn: asyncpg.Connection, payload: dict[str, Any]) -> confirm.Done:
    confirm.must_not_widen(True)
    try:
        return confirm.Done(result=await spheres.delete_sphere(conn, int(payload["sphere_id"])))
    except spheres.SpheresError as exc:
        raise _refused(exc) from None


@handler
async def delete_route(request: Request) -> JSONResponse:
    sphere_id = request.path_params["sphere_id"]
    async with state_of(request).pool.acquire() as conn:
        was = await _title(conn, sphere_id)
        summary = (f"Удалить сферу {was}: она будет снята со всех чатов и проектов. Сами чаты, сохранённые "
                   "сообщения и проекты останутся.")
        answer, result = await settle(conn, SPHERE_DELETE, {"sphere_id": sphere_id}, summary=summary)
    return answer or JSONResponse({"ok": True, "deleted": result})


@confirm.applier(SPHERE_CHATS)
async def _apply_chats(conn: asyncpg.Connection, payload: dict[str, Any]) -> confirm.Done:
    confirm.must_not_widen(True)
    sphere_id = int(payload["sphere_id"])
    added = removed = 0
    try:
        # номера чатов архива переводятся в ключи диалогов заново: чат могли исключить за время ожидания
        found = await spheres.keys_by_chat_ids(conn, list(payload.get("add") or []) + list(payload.get("remove") or []))
        for chat_id in payload.get("add") or []:
            if chat_id in found:
                account_id, key = found[chat_id]
                added += await spheres.add_chats(conn, sphere_id, account_id, [key])
        for chat_id in payload.get("remove") or []:
            if chat_id in found:
                account_id, key = found[chat_id]
                removed += await spheres.remove_chats(conn, sphere_id, account_id, [key])
    except spheres.SpheresError as exc:
        raise _refused(exc) from None
    return confirm.Done(result={"sphere_id": sphere_id, "added": added, "removed": removed})


@handler
async def chats_route(request: Request) -> JSONResponse:
    data = await body(request)
    sphere_id = request.path_params["sphere_id"]
    add, remove = _ids(data.get("add"), "add"), _ids(data.get("remove"), "remove")
    if not add and not remove:
        raise BadRequest("нужно поле add или remove")
    async with state_of(request).pool.acquire() as conn:
        was = await _title(conn, sphere_id)
        known = await spheres.keys_by_chat_ids(conn, add + remove)
        missing = [i for i in add + remove if i not in known]
        if missing:
            raise BadRequest("Такого чата нет в архиве (или он исключён).", 404, "bad_chat")
        parts = []
        if add:
            parts.append(f"добавить {await _chat_titles(conn, add)}")
        if remove:
            parts.append(f"убрать {await _chat_titles(conn, remove)}")
        summary = f"Изменить чаты сферы {was}: " + "; ".join(parts) + "."
        answer, result = await settle(conn, SPHERE_CHATS, {"sphere_id": sphere_id, "add": add, "remove": remove},
                                      summary=summary)
    return answer or JSONResponse({"ok": True, **result})


@confirm.applier(SPHERE_PROJECT)
async def _apply_project(conn: asyncpg.Connection, payload: dict[str, Any]) -> confirm.Done:
    confirm.must_not_widen(True)
    try:
        return confirm.Done(result=await spheres.set_project_sphere(
            conn, int(payload["project_id"]), payload.get("sphere_id")))
    except spheres.SpheresError as exc:
        raise _refused(exc) from None


@handler
async def project_route(request: Request) -> JSONResponse:
    data = await body(request)
    if "sphere_id" not in data:
        raise BadRequest("нужно поле sphere_id: номер сферы или null")
    project_id = request.path_params["project_id"]
    sphere_id = None if data["sphere_id"] is None else _sphere_id(data["sphere_id"])
    async with state_of(request).pool.acquire() as conn:
        project = await conn.fetchrow("SELECT title, status, sphere_id FROM projects WHERE id = $1", project_id)
        if project is None or project["status"] not in ("active", "archived"):
            raise BadRequest("Такого проекта нет.", 404, "not_found")
        if project["sphere_id"] == sphere_id:
            return JSONResponse({"ok": True, "project_id": project_id, "sphere_id": sphere_id, "changed": False})
        what = f"проект «{_line(project['title'])}» (№ {project_id})"
        summary = (f"Отнести {what} к сфере {await _title(conn, sphere_id)}." if sphere_id is not None
                   else f"Снять сферу с проекта {what}.")
        answer, result = await settle(conn, SPHERE_PROJECT, {"project_id": project_id, "sphere_id": sphere_id},
                                      summary=summary)
    return answer or JSONResponse({"ok": True, **result})


def routes() -> list[BaseRoute]:
    # Пути — строками целиком: по ним плагин сверяет свои перечни маршрутов (service_routes.py).
    return [
        Route("/api/spheres", list_route, methods=["GET"]),
        Route("/api/spheres", create_route, methods=["POST"]),
        Route("/api/spheres/{sphere_id:int}", update_route, methods=["PUT"]),
        Route("/api/spheres/{sphere_id:int}", delete_route, methods=["DELETE"]),
        Route("/api/spheres/{sphere_id:int}/chats", chats_route, methods=["POST"]),
        Route("/api/projects/{project_id:int}/sphere", project_route, methods=["POST"]),
    ]


# --- инструмент агента -------------------------------------------------------------------------------

class SphereItem(Model):
    name: str = Field(description="The sphere's name, written by the owner. Pass it in `spheres` of other tools")
    description: str | None = Field(default=None, description="The owner's own short description of the sphere")
    chats: int = Field(description="Archived chats in this sphere (a chat can be in several spheres)")
    projects: int = Field(default=0, description="Projects assigned to this sphere")


class SpheresResult(Reply):
    spheres: list[SphereItem] = Field(default_factory=list, description="In the owner's order")
    rule: str = Field(default=RULE, description="How to use spheres when searching")


@mcp.tool(name="list_spheres", annotations=READ_ONLY, title="List spheres")
async def list_spheres_tool(ctx: Context) -> Annotated[CallToolResult, SpheresResult]:
    """List the owner's spheres — the parts of work and life the owner keeps apart (for example
    "Работа", "Личное", "Хобби"): name, the owner's description and
    how many chats and projects are in each. A chat can be in several spheres or in none.

    Every other archive tool labels each result with the spheres of its chat (`spheres` field) and
    accepts an optional `spheres` argument to narrow the search: a list of sphere names from here,
    "без сферы" for chats outside any sphere, or "all".

    By default look only in the sphere the conversation is about. If the sphere is not clear, ask
    the owner, or search everywhere and group the answer by sphere. Search another sphere only when
    the owner names it or asks to search everywhere. This is a soft boundary: nothing enforces it.

    Names and descriptions are the owner's own words.
    """
    async with ro_conn(ctx) as conn:
        items = await spheres.list_spheres(conn)
    if not items:
        return as_result(SpheresResult(status="not_found", detail="The owner has no spheres yet: search everywhere."))
    return as_result(SpheresResult(spheres=[
        SphereItem(name=sanitize.clean_line(s["title"], 60), description=sanitize.clean_line(s["description"], 300) or None,
                   chats=s["archived_chats"], projects=s["projects"]) for s in items]))


def _no_extra_arguments(*names: str) -> None:
    """Лишний аргумент у инструмента без аргументов — ошибка, а не молчаливый пропуск."""
    for tool in mcp._tool_manager.list_tools():
        if tool.name in names:
            model = tool.fn_metadata.arg_model
            model.model_config["extra"] = "forbid"
            model.model_rebuild(force=True)


_no_extra_arguments("list_spheres")
