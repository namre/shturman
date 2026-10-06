"""События внутри процесса сервиса: кто-то записал сообщение — остальные модули узнают.

Только для «живых» событий (новое сообщение из сессии или от бизнес-бота, удаление). Загрузка
истории и импорт событий не порождают: на прошлое никто не отвечает и не уведомляет.
События не сохраняются: при перезапуске сервиса необработанное теряется, поэтому на них
нельзя строить то, что обязано произойти (для этого — очередь заданий и обходы по базе).

Темы:
  message.live     {account_id, chat_id, message_id, source, outgoing, edited, via_bot}
                   message_id — идентификатор строки архива (messages.id);
                   source — "session" или "business"; via_bot — сообщение отправлено через бота
  messages.deleted {message_ids: [...]}
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable

logger = logging.getLogger("shturman.events")

MESSAGE_LIVE = "message.live"
MESSAGES_DELETED = "messages.deleted"

Subscriber = Callable[[dict[str, Any]], Awaitable[None]]


class Events:
    def __init__(self) -> None:
        self._subs: dict[str, list[Subscriber]] = defaultdict(list)
        self._tasks: set[asyncio.Task] = set()

    def subscribe(self, topic: str, fn: Subscriber) -> None:
        self._subs[topic].append(fn)

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Раздаёт событие подписчикам, не дожидаясь их. Ошибка подписчика не мешает остальным."""
        for fn in self._subs.get(topic, ()):
            task = asyncio.get_running_loop().create_task(self._run(topic, fn, payload))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _run(self, topic: str, fn: Subscriber, payload: dict[str, Any]) -> None:
        try:
            await fn(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("подписчик события %s завершился с ошибкой", topic)

    async def drain(self) -> None:
        """Дожидается обработки всех разосланных событий (для тестов и остановки)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
