"""Справка при контакте: подписка на живые входящие и кнопки под справкой (`brief/core.py`); напоминания
(`brief/remind.py`): состояние сервиса для «В чат» и уборка при исключении чата."""

from __future__ import annotations

import contextlib
import logging
from typing import Any, AsyncIterator

from ..app import AppState
from ..events import CHAT_EXCLUDED, MESSAGE_LIVE
from . import core, remind

logger = logging.getLogger("shturman.brief")


@contextlib.asynccontextmanager
async def lifespan(state: AppState) -> AsyncIterator[None]:
    core.OWNER_TZ["name"] = state.config.timezone
    remind.set_state(state)

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

    async def chat_excluded(payload: dict[str, Any]) -> None:
        chat_id = payload.get("chat_id")
        if isinstance(chat_id, int):
            async with state.pool.acquire() as conn:
                await remind.drop_chat(conn, chat_id)

    state.events.subscribe(MESSAGE_LIVE, on_message)
    state.events.subscribe(CHAT_EXCLUDED, chat_excluded)
    try:
        yield
    finally:
        remind.set_state(None)
