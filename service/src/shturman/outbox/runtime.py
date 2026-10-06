"""Состояние модуля отправки в работающем сервисе.

Разборщики моста (`bridge.on_result`, `bridge.on_callback`) — функции уровня модуля и состояние
сервиса не получают, поэтому действующий объект хранится здесь и выставляется в `lifespan`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..tg.gateway import TgGateway

TYPING_EVERY = 4.0  # секунд: Telegram гасит «печатает…» примерно через пять


class Outbox:
    def __init__(self, state: Any) -> None:
        self.state = state
        self.wake = asyncio.Event()
        # по одному исполнителю на аккаунт: отправки аккаунта идут строго по очереди
        self.workers: dict[int, asyncio.Task] = {}
        # черновики, отправку которых ведёт этот процесс прямо сейчас
        self.active: set[int] = set()
        self.typing: dict[tuple[int, int], asyncio.Task] = {}
        self.debounce: dict[int, asyncio.Task] = {}

    @property
    def tg(self) -> TgGateway | None:
        """Шлюз сессий Telegram; его может не быть — тогда канал «помощник» недоступен."""
        return self.state.extras.get("tg")

    def kick(self) -> None:
        """Будит отправщик. С небольшой задержкой: вызывающая транзакция должна успеть закрыться."""
        asyncio.get_running_loop().call_later(0.05, self.wake.set)

    # --- «печатает…» ---

    def start_typing(self, account_id: int, chat_id: int, peer_class: str, tg_id: int, max_seconds: float) -> None:
        key = (account_id, chat_id)
        if self.tg is None or key in self.typing:
            return
        self.typing[key] = self.state.spawn(
            self._typing_loop(key, peer_class, tg_id, max_seconds), name=f"outbox-typing-{chat_id}")

    async def _typing_loop(self, key: tuple[int, int], peer_class: str, tg_id: int, max_seconds: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_seconds
        try:
            while loop.time() < deadline and self.tg is not None:
                await self.tg.set_typing(key[0], peer_class, tg_id, True)
                await asyncio.sleep(TYPING_EVERY)
        finally:
            if self.typing.get(key) is asyncio.current_task():
                self.typing.pop(key, None)

    def stop_typing(self, account_id: int, chat_id: int) -> None:
        task = self.typing.pop((account_id, chat_id), None)
        if task is not None:
            task.cancel()

    def close(self) -> None:
        for task in [*self.typing.values(), *self.debounce.values(), *self.workers.values()]:
            task.cancel()
        self.typing.clear()
        self.debounce.clear()
        self.workers.clear()


_current: Outbox | None = None


def current() -> Outbox | None:
    return _current


def set_current(mod: Outbox | None) -> None:
    global _current
    _current = mod
