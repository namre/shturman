"""Модуль сервиса: аккаунты Telegram. Маршруты под /api/tg/ и запуск сессий.

Маршруты (все — JSON, токен внутреннего API):

  GET  /api/tg/accounts                          аккаунты и их состояние
  POST /api/tg/login                             начать вход по QR: {role, confirm_owner?}
  GET  /api/tg/login/{login_id}                  состояние входа (QR обновляется сам)
  POST /api/tg/login/{login_id}/password         облачный пароль: {password}
  POST /api/tg/login/{login_id}/cancel           отменить вход
  POST /api/tg/accounts/{id}/logout              выйти: сессия завершается, файл удаляется
  POST /api/tg/accounts/{id}/pause               поставить на паузу
  POST /api/tg/accounts/{id}/resume              снять с паузы
  PUT  /api/tg/accounts/{id}/options             {auto_personal?, auto_groups?}
  GET  /api/tg/accounts/{id}/dialogs             чаты для экрана выбора: ?offset&limit&type&refresh
  POST /api/tg/accounts/{id}/sync                включить/выключить: {enabled, chats? | types?}
  GET  /api/tg/accounts/{id}/sync                состояние синхронизации: счётчики и курсоры

Секретов в ответах нет. QR-код и ссылка входа отдаются только пока код ждёт сканирования;
облачный пароль принимается один раз и нигде не сохраняется.
"""

from __future__ import annotations

import contextlib
from typing import Any, AsyncIterator

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from ..api_core import BadRequest, body, handler, need_str
from ..app import AppState, state_of
from . import gateway, normalize
from .manager import TgError, TgManager

PEER_CLASSES = ("user", "chat", "channel")


def _manager(request: Request) -> TgManager:
    manager = state_of(request).extras.get("tg")
    if not isinstance(manager, TgManager):
        raise BadRequest("Модуль аккаунтов Telegram не запущен.", 503)
    try:
        manager.require_configured()
    except TgError as exc:
        raise BadRequest(exc.message, exc.status) from None
    return manager


def tg_handler(fn):
    """Как `api_core.handler`, плюс отказы модуля с текстом для владельца."""
    async def inner(request: Request) -> JSONResponse:
        try:
            return await fn(request)
        except TgError as exc:
            raise BadRequest(exc.message, exc.status) from None
        except gateway.AccountUnavailable:
            raise BadRequest("Аккаунт не подключён.", 409) from None
    inner.__name__ = fn.__name__
    return handler(inner)


def _account_id(request: Request) -> int:
    return int(request.path_params["account_id"])


def _flag(data: dict[str, Any], key: str, *, required: bool = False) -> bool | None:
    value = data.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, bool):
        raise BadRequest(f"поле {key}: нужно true или false")
    return value


def _query_int(request: Request, key: str, default: int, *, low: int, high: int) -> int:
    raw = request.query_params.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise BadRequest(f"параметр {key}: нужно целое число") from None
    return max(low, min(high, value))


@tg_handler
async def accounts(request: Request) -> JSONResponse:
    return JSONResponse({"accounts": await _manager(request).list_accounts()})


@tg_handler
async def login_start(request: Request) -> JSONResponse:
    manager = _manager(request)
    data = await body(request)
    flow = await manager.start_login(need_str(data, "role", limit=16),
                                     confirm_owner=bool(_flag(data, "confirm_owner")))
    return JSONResponse(flow.snapshot())


@tg_handler
async def login_status(request: Request) -> JSONResponse:
    return JSONResponse(_manager(request).login(request.path_params["login_id"]).snapshot())


@tg_handler
async def login_password(request: Request) -> JSONResponse:
    manager = _manager(request)
    data = await body(request)
    password = data.get("password")
    if not isinstance(password, str) or not password:
        raise BadRequest("поле password: нужна непустая строка")
    flow = await manager.submit_password(request.path_params["login_id"], password)
    return JSONResponse(flow.snapshot())


@tg_handler
async def login_cancel(request: Request) -> JSONResponse:
    flow = await _manager(request).cancel_login(request.path_params["login_id"])
    return JSONResponse(flow.snapshot())


@tg_handler
async def logout(request: Request) -> JSONResponse:
    return JSONResponse(await _manager(request).logout(_account_id(request)))


@tg_handler
async def pause(request: Request) -> JSONResponse:
    await _manager(request).pause(_account_id(request))
    return JSONResponse({"ok": True})


@tg_handler
async def resume(request: Request) -> JSONResponse:
    await _manager(request).resume(_account_id(request))
    return JSONResponse({"ok": True})


@tg_handler
async def options(request: Request) -> JSONResponse:
    manager = _manager(request)
    data = await body(request)
    await manager.set_options(_account_id(request), auto_personal=_flag(data, "auto_personal"),
                              auto_groups=_flag(data, "auto_groups"))
    return JSONResponse({"ok": True})


@tg_handler
async def dialogs(request: Request) -> JSONResponse:
    manager = _manager(request)
    chat_type = request.query_params.get("type") or None
    if chat_type is not None and chat_type not in normalize.CHAT_TYPES:
        raise BadRequest("параметр type: неизвестный вид чата")
    out = await manager.list_dialogs(
        _account_id(request),
        offset=_query_int(request, "offset", 0, low=0, high=10_000_000),
        limit=_query_int(request, "limit", 100, low=1, high=500),
        chat_type=chat_type,
        refresh=request.query_params.get("refresh") in ("1", "true"),
    )
    return JSONResponse(out)


@tg_handler
async def sync_set(request: Request) -> JSONResponse:
    manager = _manager(request)
    data = await body(request)
    enabled = _flag(data, "enabled", required=True)
    chats, types_ = data.get("chats"), data.get("types")
    keys: list[tuple[str, int]] = []
    if chats is not None:
        if not isinstance(chats, list) or len(chats) > 5000:
            raise BadRequest("поле chats: нужен список чатов")
        for item in chats:
            if not isinstance(item, dict) or item.get("peer_class") not in PEER_CLASSES \
                    or isinstance(item.get("tg_id"), bool) or not isinstance(item.get("tg_id"), int):
                raise BadRequest("поле chats: у каждого чата нужны peer_class (user, chat, channel) и tg_id")
            keys.append((item["peer_class"], item["tg_id"]))
    if types_ is not None:
        if not isinstance(types_, list) or any(t not in normalize.CHAT_TYPES for t in types_):
            raise BadRequest("поле types: нужен список видов чатов")
    if not keys and not types_:
        raise BadRequest("нужно поле chats или types")
    result = await manager.set_sync(_account_id(request), enabled=bool(enabled), chats=keys,
                                    chat_types=list(types_ or []))
    return JSONResponse({"chats": result})


@tg_handler
async def sync_status(request: Request) -> JSONResponse:
    return JSONResponse(await _manager(request).sync_status(_account_id(request)))


def routes() -> list[BaseRoute]:
    account = "/api/tg/accounts/{account_id:int}"
    return [
        Route("/api/tg/accounts", accounts, methods=["GET"]),
        Route("/api/tg/login", login_start, methods=["POST"]),
        Route("/api/tg/login/{login_id}", login_status, methods=["GET"]),
        Route("/api/tg/login/{login_id}/password", login_password, methods=["POST"]),
        Route("/api/tg/login/{login_id}/cancel", login_cancel, methods=["POST"]),
        Route(f"{account}/logout", logout, methods=["POST"]),
        Route(f"{account}/pause", pause, methods=["POST"]),
        Route(f"{account}/resume", resume, methods=["POST"]),
        Route(f"{account}/options", options, methods=["PUT"]),
        Route(f"{account}/dialogs", dialogs, methods=["GET"]),
        Route(f"{account}/sync", sync_set, methods=["POST"]),
        Route(f"{account}/sync", sync_status, methods=["GET"]),
    ]


@contextlib.asynccontextmanager
async def lifespan(state: AppState) -> AsyncIterator[None]:
    """Кладёт шлюз в `state.extras["tg"]` и запускает сессии, файлы которых есть на диске.

    Без ключей приложения модуль простаивает: шлюз есть, но ни один аккаунт не запущен.
    """
    manager = TgManager(state.config, state.pool, state.events)
    state.extras["tg"] = manager
    await manager.start()
    try:
        yield
    finally:
        await manager.stop()
        state.extras.pop("tg", None)
