"""Сборка сервиса: один процесс, один порт.

  /health  — без токена, только «жив»;
  /api/*   — внутренний API для плагина «Штурмана» в Hermes (токен SHTURMAN_API_TOKEN);
  /mcp     — MCP-сервер архива для агента Hermes, только чтение (токен SHTURMAN_MCP_TOKEN);
  /shturman-setup/* — страница настройки для владельца: свой вход по одноразовой ссылке,
             своя сессия без cookie; токены API и архива здесь входом не служат (setup_page/).

Порт слушает локальный адрес сервера. Обратный прокси может отдать наружу только префикс
/shturman-setup/ — и только с адреса, отличного от адреса дашборда Hermes (другой порт или
другое имя): при совпадении адресов страница снаружи не обслуживается. /api/* и /mcp наружу
не выводятся.

Модуль подключается именем в MODULES и может определить:
  routes() -> list[BaseRoute]            — свои маршруты (пути начинаются с /api/ или /mcp;
                                           у страницы настройки — с /shturman-setup/);
  lifespan(state) -> async context manager — запуск и остановка фоновой работы;
  make_shield(app, config) -> ASGI       — общая защита своего префикса (только у страницы настройки).
Обработчик получает состояние как `request.app.state.shturman`.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import importlib
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Coroutine, Sequence

import asyncpg
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__, db
from .config import Config
from .events import Events
from .setup_page import PREFIX as SETUP_PREFIX

logger = logging.getLogger("shturman")

MODULES = (
    "shturman.api_core",
    "shturman.executor.service",   # раньше остальных: кто выполняет задания, решается до их постановки
    "shturman.guard.service",      # раньше источников сообщений: живой поток пишется уже под защитой
    "shturman.sources.service",    # ограниченные источники для автоматических задач
    "shturman.ingest_api",
    "shturman.mcp_server",
    "shturman.embeddings",
    "shturman.tg.service",
    "shturman.processing.service",
    "shturman.processing.pages_service",
    "shturman.processing.mcp_tools",
    "shturman.outbox.service",
    "shturman.replies.service",    # продолжение задач после Telegram-решений владельца
    "shturman.remote_mcp",
    "shturman.setup_page.service",  # последним: страница настройки пользуется остальными модулями
)


@dataclass
class AppState:
    config: Config
    pool: asyncpg.Pool       # чтение и запись
    ro_pool: asyncpg.Pool    # только чтение — для всего, что отдаётся агенту
    events: Events = field(default_factory=Events)
    # Общие объекты модулей: модуль кладёт сюда то, чем пользуются другие (например, "tg").
    extras: dict[str, Any] = field(default_factory=dict)
    _tasks: set[asyncio.Task] = field(default_factory=set)

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task:
        """Запускает фоновую работу, которая остановится вместе с сервисом."""
        from . import authority
        task = asyncio.get_running_loop().create_task(coro, name=name, context=authority.background_context())
        self._tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("фоновая работа %s завершилась с ошибкой", task.get_name(),
                         exc_info=task.exception())

    async def stop_tasks(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class Gate:
    """Проверка токена по префиксу пути. Сравнение — за постоянное время.

    Префикс страницы настройки (`/shturman-setup`) токеном не открывается вовсе: у него свой
    вход и своя защита (`setup`), а заголовок `Authorization` там ничего не значит."""

    def __init__(self, app: ASGIApp, config: Config, *, setup: ASGIApp | None = None,
                 remote: ASGIApp | None = None) -> None:
        self.app = app
        self.setup = setup
        self.remote = remote
        self._tokens = {
            "/api": f"Bearer {config.api_token}".encode(),
            "/mcp": f"Bearer {config.mcp_token}".encode(),
        }

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            # Других видов соединений у сервиса нет: без проверки токена их не пропускаем.
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        path = scope.get("path", "")
        hosts = [v.decode("latin1") for k, v in scope.get("headers", []) if k.lower() == b"host"]
        if self.remote is not None and any(self.remote.handles(path, host) for host in hosts):
            await self.remote(scope, receive, send)
            return
        if path == "/health":
            await self.app(scope, receive, send)
            return
        if path == SETUP_PREFIX or path.startswith(SETUP_PREFIX + "/"):
            if self.setup is None:      # модуль страницы не подключён
                await JSONResponse({"error": "not_found"}, status_code=404)(scope, receive, send)
            else:
                await self.setup(scope, receive, send)
            return
        expected = next((t for p, t in self._tokens.items() if path == p or path.startswith(p + "/")), None)
        if expected is None:
            await JSONResponse({"error": "not_found"}, status_code=404)(scope, receive, send)
            return
        got = dict(scope["headers"]).get(b"authorization", b"")
        if not hmac.compare_digest(got, expected):
            await JSONResponse({"error": "unauthorized"}, status_code=401,
                               headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _load_modules(names: Sequence[str]) -> list[Any]:
    return [importlib.import_module(name) for name in names]


async def _health(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "version": __version__})


async def create_pools(config: Config) -> tuple[asyncpg.Pool, asyncpg.Pool]:
    pool = await asyncpg.create_pool(config.dsn, min_size=1, max_size=8)
    ro_pool = await asyncpg.create_pool(
        config.dsn, min_size=1, max_size=4,
        server_settings={"default_transaction_read_only": "on"},
    )
    return pool, ro_pool


def build_app(
    config: Config, *, migrate: bool = True, modules: Sequence[str] | None = None
) -> ASGIApp:
    """Собирает приложение. `modules` — для тестов: поднять только часть сервиса."""
    modules = _load_modules(MODULES if modules is None else modules)
    routes: list[BaseRoute] = [Route("/health", _health)]
    for module in modules:
        if hasattr(module, "routes"):
            routes.extend(module.routes())

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        for path in (config.sessions_dir, config.uploads_dir, config.pages_dir):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if migrate:
            conn = await db.connect(config.dsn)
            try:
                applied = await db.migrate(conn)
            finally:
                await conn.close()
            if applied:
                logger.info("применены миграции: %s", ", ".join(applied))
        pool, ro_pool = await create_pools(config)
        state = AppState(config=config, pool=pool, ro_pool=ro_pool)
        app.state.shturman = state
        try:
            from . import control_peers
            async with pool.acquire() as conn:
                await control_peers.reconcile(conn)
            async with contextlib.AsyncExitStack() as stack:
                for module in modules:
                    if hasattr(module, "lifespan"):
                        await stack.enter_async_context(module.lifespan(state))
                try:
                    yield
                finally:
                    await state.stop_tasks()
        finally:
            await ro_pool.close()
            await pool.close()

    app = Starlette(routes=routes, lifespan=lifespan)
    shield = next((m.make_shield(app, config) for m in modules if hasattr(m, "make_shield")), None)
    from .remote_mcp import Gateway
    remote = Gateway(app, config) if config.remote_mcp_origin else None
    gate = Gate(app, config, setup=shield, remote=remote)
    gate.inner = app  # для тестов: запуск жизненного цикла и доступ к состоянию
    return gate


def state_of(request: Request) -> AppState:
    return request.app.state.shturman
