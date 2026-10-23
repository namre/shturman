"""Справка при контакте: подписка на живые входящие и кнопки под справкой (`brief/core.py`)."""

from __future__ import annotations

import contextlib
import logging
from typing import Any, AsyncIterator

from ..app import AppState
from ..events import MESSAGE_LIVE
from . import core

logger = logging.getLogger("shturman.brief")


@contextlib.asynccontextmanager
async def lifespan(state: AppState) -> AsyncIterator[None]:
    core.OWNER_TZ["name"] = state.config.timezone

    async def on_message(payload: dict[str, Any]) -> None:
        if payload.get("outgoing") is not False or payload.get("edited") is not False \
                or payload.get("via_bot") is not False:
            return
        message_id = payload.get("message_id")
        if not isinstance(message_id, int):
            return
        try:
            async with state.pool.acquire() as conn:
                await core.on_message(conn, message_id, tz=state.config.timezone)
        except Exception as exc:  # справка не главное: сбой не мешает остальным подписчикам
            logger.error("справка не собрана: %s", type(exc).__name__)

    state.events.subscribe(MESSAGE_LIVE, on_message)
    yield
