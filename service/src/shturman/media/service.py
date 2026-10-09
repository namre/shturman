"""Разбор фото и документов в составе сервиса: фоновая очередь и маршрут `/api/media/status`.

Очередь работает всегда, но берёт вложения, только пока владелец включил разбор на странице
настройки переписки (setup_state 'media'). Маршрута, который включает разбор, во внутреннем
API нет: он доступен ассистенту, а решать, отдавать ли файлы из переписки модели, — владельцу.

Маршрут отдаёт только числа и состояние, текстов и имён файлов в нём нет.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, AsyncIterator

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from ..config import Config
from . import core, enabled

logger = logging.getLogger("shturman.media")

EXTRAS_KEY = "media"


async def overview(conn: Any, state: Any) -> dict[str, Any]:
    """Для `/api/status`, страницы настройки и `ops/doctor.sh`: признаки и счётчики."""
    out = {"media_enabled": await enabled(conn), "media_running": state.extras.get(EXTRAS_KEY) is not None}
    out.update(await core.counters(conn))
    return out


async def status(request: Request) -> JSONResponse:
    state = request.app.state.shturman
    async with state.ro_pool.acquire() as conn:
        out = await overview(conn, state)
    config: Config = state.config
    out.update(media_days=config.media_days, media_max_bytes=config.media_max_bytes)
    return JSONResponse(out)


def routes() -> list[BaseRoute]:
    return [Route("/api/media/status", status, methods=["GET"])]


def _session_fetch(state: Any) -> core.SessionFetch | None:
    from ..tg.manager import TgManager

    manager = state.extras.get("tg")
    if not isinstance(manager, TgManager) or not manager.configured:
        return None

    async def fetch(account_id: int, peer_class: str, tg_id: int, tg_message_id: int, max_bytes: int):
        return await manager.download_media(account_id, peer_class, tg_id, tg_message_id,
                                            max_bytes=max_bytes, kinds=("photo", "file"))
    return fetch


def _bot_fetch(state: Any) -> core.BotFetch | None:
    runtime = state.extras.get("executor")
    api = getattr(runtime, "api", None)
    if api is None or getattr(api, "broken", None):
        return None

    async def fetch(file_id: str, max_bytes: int) -> bytes:
        return await api.download_file(file_id, max_bytes=max_bytes)
    return fetch


@contextlib.asynccontextmanager
async def lifespan(state: Any) -> AsyncIterator[None]:
    config: Config = state.config
    analyzer = core.Analyzer(
        state.pool, core.Settings(days=config.media_days, max_bytes=config.media_max_bytes),
        data_dir=config.data_dir, session_fetch=lambda: _session_fetch(state),
        bot_fetch=lambda: _bot_fetch(state), publish=state.events.publish)
    core._current = analyzer
    state.extras[EXTRAS_KEY] = analyzer
    state.spawn(analyzer.run(), name="media-analyzer")
    try:
        yield
    finally:
        state.extras.pop(EXTRAS_KEY, None)
        if core._current is analyzer:
            core._current = None
