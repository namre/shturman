"""Расшифровка голосовых в составе сервиса: запуск, фоновая очередь, маршрут `/api/voice/status`.

Включается переменной окружения сервиса `SHTURMAN_ASR=on` (на сервере её пишет
`./ops/asr.sh on`). Маршрута, который её включает, нет: внутренний API доступен ассистенту,
а решать, скачивать ли файлы из переписки, — владельцу.

Маршрут отдаёт только числа и состояние, текстов расшифровок в нём нет.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, AsyncIterator

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from ..config import Config
from . import core
from .asr import AsrClient, AsrUnavailable

logger = logging.getLogger("shturman.voice")

EXTRAS_KEY = "voice"


def build_client(config: Config) -> AsrClient:
    """Клиент контейнера распознавания. Тесты подменяют эту функцию."""
    return AsrClient(config.asr_url)


async def overview(conn: Any, state: Any) -> dict[str, Any]:
    """Для `/api/status`, страницы настройки и `ops/doctor.sh`: признаки и счётчики."""
    out = core.status_fields(state.extras.get(EXTRAS_KEY))
    out.update(await core.counters(conn))
    return out


async def status(request: Request) -> JSONResponse:
    state = request.app.state.shturman
    async with state.ro_pool.acquire() as conn:
        out = await overview(conn, state)
    config: Config = state.config
    out.update(voice_days=config.asr_days, voice_max_seconds=config.asr_max_seconds)
    return JSONResponse(out)


def routes() -> list[BaseRoute]:
    return [Route("/api/voice/status", status, methods=["GET"])]


def _session_fetch(state: Any) -> core.SessionFetch | None:
    from ..tg.manager import TgManager

    manager = state.extras.get("tg")
    if not isinstance(manager, TgManager) or not manager.configured:
        return None

    async def fetch(account_id: int, peer_class: str, tg_id: int, tg_message_id: int, max_bytes: int):
        return await manager.download_voice(account_id, peer_class, tg_id, tg_message_id, max_bytes=max_bytes)
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
    if not config.asr or not config.asr_url:
        if config.asr:
            logger.warning("расшифровка голосовых включена, но адрес контейнера не задан (SHTURMAN_ASR_URL)")
        yield
        return
    client = build_client(config)
    settings = core.Settings(days=config.asr_days, max_seconds=config.asr_max_seconds,
                             max_bytes=config.asr_max_bytes)
    transcriber = core.Transcriber(state.pool, client, settings,
                                   session_fetch=lambda: _session_fetch(state),
                                   bot_fetch=lambda: _bot_fetch(state))
    try:
        await client.health()
    except AsrUnavailable:
        transcriber.problem = "unreachable"
        logger.warning("контейнер распознавания речи пока не отвечает: голосовые подождут")
    state.extras[EXTRAS_KEY] = transcriber
    state.spawn(transcriber.run(), name="voice-transcriber")
    try:
        yield
    finally:
        state.extras.pop(EXTRAS_KEY, None)
        await client.close()
