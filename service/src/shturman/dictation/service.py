"""Ответ голосом в составе сервиса (Р-103): кнопки и приём регистрирует `core`, здесь — запуск и обход.

Маршрутов нет: голосовой ответ начинается только нажатием владельца в боте согласований, а внутренний
API доступен ассистенту. Обход раз в минуту: ожидание, у которого вышел срок, — «истекло»; строки старше
суток удаляются вместе с расшифровкой и записанным текстом.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, AsyncIterator

from . import core

logger = logging.getLogger("shturman.dictation")

SWEEP_EVERY = 60.0


async def _sweeper(state: Any) -> None:
    while True:
        try:
            async with state.pool.acquire() as conn:
                await core.sweep(conn)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — сбой одного обхода не останавливает голосовые ответы
            logger.error("голосовые ответы: обход не выполнен (%s)", type(exc).__name__)
        await asyncio.sleep(SWEEP_EVERY)


@contextlib.asynccontextmanager
async def lifespan(state: Any) -> AsyncIterator[None]:
    core.set_state(state)
    state.spawn(_sweeper(state), name="dictation-sweeper")
    try:
        yield
    finally:
        if core.current_state() is state:
            core.set_state(None)
