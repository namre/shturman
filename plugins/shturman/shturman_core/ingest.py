"""Пересылка обновлений бизнес-режима в сервис переписки. Только стандартная библиотека.

Обработчик Telegram лишь кладёт обновление в очередь и сразу возвращает управление: шлюз
Hermes не ждёт сервис и не страдает от его отказов. Очередь ограничена. Пока сервис недоступен,
обновления копятся; когда очередь заполнена, новые теряются, и это честно видно в счётчике
`dropped`. Очередь живёт в памяти: при перезапуске шлюза неотправленное тоже теряется.

Сервис принимает сообщения только по подключению, которое создал владелец:
  * 409 unknown_connection — подключение запрашивается у Telegram, передаётся сервису,
    и сообщение отправляется ещё раз;
  * 409 owner_unknown — сервису передаётся привязанный владелец, и запрос повторяется.
Остальные отказы (чужое подключение, неразборчивое сообщение) окончательны: обновление
отбрасывается и считается в `rejected`.

Здесь же сервису сообщается владелец: при запуске шлюза и при каждой смене привязки.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from .bridge_stats import Stats
from .service_client import ServiceError, ServiceUnavailable

logger = logging.getLogger("shturman.ingest")

CAPACITY = 2000                    # обновлений в очереди
IDLE_SECONDS = 5.0                 # как часто без работы проверяется смена владельца
MAX_BACKOFF = 30.0
REFUSED_TTL = 600                  # секунд не переспрашивать подключение, которое сервис отклонил

CONNECTION = "/api/ingest/business/connection"
MESSAGE = "/api/ingest/business/message"
DELETED = "/api/ingest/business/deleted"
_COUNTER = {CONNECTION: "forwarded_connections", MESSAGE: "forwarded_messages", DELETED: "forwarded_deleted"}

ServiceCall = Callable[..., Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class Item:
    path: str
    body: dict[str, Any]
    connection_id: str | None = None


class _Later(Exception):
    """Сейчас не получилось по временной причине — повторить позже, обновление остаётся в очереди."""


class Ingest:
    """Очередь и её разбор.

    call             — обращение к сервису (бросает `ServiceError`);
    owner            — привязанный владелец ({user_id, chat_id}) или {};
    owner_version    — значение, меняющееся при смене привязки (время изменения файла);
    fetch_connection — запрос подключения у Telegram: словарь, None (такого нет) либо исключение.
    """

    def __init__(
        self, call: ServiceCall, *, owner: Callable[[], Mapping[str, Any]],
        owner_version: Callable[[], Any] = lambda: 0,
        fetch_connection: Callable[[str], Awaitable[dict[str, Any] | None]] | None = None,
        stats: Stats | None = None, capacity: int = CAPACITY, idle: float = IDLE_SECONDS,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.call = call
        self.owner = owner
        self.owner_version = owner_version
        self.fetch_connection = fetch_connection
        self.stats = stats or Stats()
        self.capacity = capacity
        self.idle = idle
        self._now = now
        self._queue: deque[Item] = deque()
        self._wake: asyncio.Event | None = None
        self._pushed_version: Any = object()        # ни с чем не равно: при запуске владелец передаётся
        self._refused: dict[str, float] = {}

    # --- приём от обработчиков Telegram: мгновенно и без исключений ---

    def put(self, path: str, body: dict[str, Any], connection_id: str | None = None) -> bool:
        if len(self._queue) >= self.capacity:
            self.stats.bump("dropped")
            return False
        self._queue.append(Item(path, body, connection_id))
        self.stats.set_queue(len(self._queue))
        if self._wake is not None:
            self._wake.set()
        return True

    def put_connection(self, connection: dict[str, Any]) -> bool:
        return self.put(CONNECTION, {"connection": connection}, _str(connection.get("id")))

    def put_message(self, message: dict[str, Any], *, edited: bool) -> bool:
        return self.put(MESSAGE, {"message": message, "edited": bool(edited)},
                        _str(message.get("business_connection_id")))

    def put_deleted(self, deleted: dict[str, Any]) -> bool:
        return self.put(DELETED, deleted, _str(deleted.get("business_connection_id")))

    def __len__(self) -> int:
        return len(self._queue)

    # --- разбор ---

    async def run(self) -> None:
        """Бесконечный цикл. Останавливается отменой задачи."""
        self._wake = asyncio.Event()
        failures = 0
        while True:
            try:
                await self.step()
                failures = 0
            except asyncio.CancelledError:
                raise
            except _Later:
                failures += 1
            except Exception as exc:  # noqa: BLE001
                failures += 1
                logger.warning("shturman: сбой пересылки в сервис переписки (%s)", type(exc).__name__)
            if failures:
                pause = min(MAX_BACKOFF, 2 ** min(failures - 1, 5))
            elif self._queue:
                continue
            else:
                pause = self.idle
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=pause)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def step(self) -> bool:
        """Передаёт владельца, если он сменился, и одно обновление из очереди. True — была работа."""
        await self.sync_owner()
        if not self._queue:
            return False
        item = self._queue[0]
        done = False
        try:
            await self._deliver(item)
            done = True
        except _Later:
            raise                       # обновление остаётся первым в очереди
        finally:
            if done and self._queue and self._queue[0] is item:
                self._queue.popleft()
                self.stats.set_queue(len(self._queue))
        return True

    async def sync_owner(self, *, force: bool = False) -> bool:
        """Сообщает сервису владельца, если привязка изменилась. True — сервис знает владельца."""
        version = self.owner_version()
        if not force and version == self._pushed_version:
            return True
        if version != self._pushed_version:
            self._refused.clear()       # с новым владельцем прежние отказы подключениям не действуют
        owner = self.owner() or {}
        user_id, chat_id = owner.get("user_id"), owner.get("chat_id")
        if not _is_id(user_id) or not _is_id(chat_id):
            # Владельца нет (ещё не привязан или привязка сброшена ссылкой восстановления).
            # Сервис должен об этом узнать: он останавливает бизнес-подключения, отклоняет
            # ждущие черновики и выключает автоответ, пока не привяжется новый владелец.
            try:
                await self.call("DELETE", "/api/owner", None)
            except ServiceUnavailable:
                self.stats.seen(False)
                raise _Later() from None
            except ServiceError:
                pass
            self._pushed_version = version
            return False
        try:
            await self.call("PUT", "/api/owner", {"user_id": user_id, "chat_id": chat_id})
        except ServiceUnavailable:
            self.stats.seen(False)
            raise _Later() from None
        except ServiceError:
            return False
        self.stats.seen(True)
        self._pushed_version = version
        return True

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            out = await self.call("POST", path, body)
        except ServiceUnavailable:
            self.stats.seen(False)
            raise _Later() from None
        self.stats.seen(True)
        return out

    async def _deliver(self, item: Item) -> None:
        """Доставляет обновление либо отбрасывает его окончательно. `_Later` — повторить позже."""
        cid = item.connection_id
        if cid and item.path != CONNECTION and self._is_refused(cid):
            self.stats.bump("rejected")
            return
        for attempt in (1, 2):
            try:
                await self._post(item.path, item.body)
            except ServiceError as exc:
                if attempt == 1 and exc.code == "unknown_connection" and cid and await self._resend_connection(cid):
                    continue
                if attempt == 1 and exc.code == "owner_unknown" and await self.sync_owner(force=True):
                    continue
                break
            else:
                self.stats.bump(_COUNTER[item.path])
                return
        self.stats.bump("rejected")

    def _is_refused(self, connection_id: str) -> bool:
        until = self._refused.get(connection_id)
        if until is None:
            return False
        if until <= self._now():
            del self._refused[connection_id]
            return False
        return True

    def _refuse(self, connection_id: str) -> None:
        if len(self._refused) > 500:
            self._refused.clear()
        self._refused[connection_id] = self._now() + REFUSED_TTL

    async def _resend_connection(self, connection_id: str) -> bool:
        """Сервис не знает подключения: спрашиваем его у Telegram и передаём. True — принято."""
        if self.fetch_connection is None:
            return False
        try:
            connection = await self.fetch_connection(connection_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise _Later() from None          # Telegram сейчас недоступен
        if not isinstance(connection, dict):
            self._refuse(connection_id)
            return False
        for attempt in (1, 2):
            try:
                await self._post(CONNECTION, {"connection": connection})
                self.stats.bump("forwarded_connections")
                return True
            except ServiceError as exc:
                if attempt == 1 and exc.code == "owner_unknown" and await self.sync_owner(force=True):
                    continue
                break
        # Подключение создал не владелец (или владелец ещё не привязан): какое-то время не переспрашиваем.
        self._refuse(connection_id)
        return False


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _is_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value != 0
