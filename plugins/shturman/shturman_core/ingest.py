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

Сервис может принять запрос, но сообщение не записать (ответ 200, `stored: false`). Такие
сообщения переданными не считаются, у каждой причины свой счётчик:
  * connection_disabled — подключение у сервиса выключено. Один раз настоящее подключение
    запрашивается у Telegram, передаётся сервису, и сообщение отправляется снова. Не помогло —
    на странице состояния поднимается признак `business_disabled`;
  * excluded — чат исключён владельцем; прочее (например, у сообщения нет номера) — отдельно.

У сервиса может быть свой бот согласований (`own_bot.py`). Пока он включён, сервис отвечает
на всё перечисленное отказом `own_bot`, и пересылка стоит: владелец не передаётся, очередь
пуста, новые обновления в неё не кладутся (сервис получает их через своего бота). Повторов
нет; раз в несколько минут у сервиса спрашивается, включён ли бот. Когда он выключен,
владелец передаётся заново (и отложенное «владелец отвязан» — тоже), пересылка возобновляется.

Здесь же сервису сообщается владелец: при запуске шлюза и при каждой смене привязки.
Сообщение «владелец отвязан» (DELETE /api/owner) необратимо для сервиса — он отклоняет ждущие
черновики и очищает список доверенных, — поэтому уходит только в двух случаях:
  * есть отметка, что привязку сбросил сам плагин (вход по ссылке восстановления);
  * этот процесс уже передавал владельца, а теперь его запись достоверно отсутствует.
Свежий процесс с пустым состоянием (первая установка, потерянный каталог состояния) владельца
сервиса не трогает. Не удалось прочитать файл — ничего не делается, попытка повторится.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from .bridge_stats import Stats
from .own_bot import OwnBot
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
    owner            — привязанный владелец ({user_id, chat_id}); {} — записи достоверно нет;
                       исключение — прочитать не удалось (тогда ничего не решается);
    owner_version    — значение, меняющееся при смене привязки (время изменения файла);
    unbound_marker   — есть ли отметка «привязку сбросил сам плагин»; clear_marker её снимает;
    fetch_connection — запрос подключения у Telegram: словарь, None (такого нет) либо исключение;
    own_bot          — режим «бизнес-поток ведёт бот сервиса»; общий с обработчиком кнопок.
    """

    def __init__(
        self, call: ServiceCall, *, owner: Callable[[], Mapping[str, Any]],
        owner_version: Callable[[], Any] = lambda: 0,
        fetch_connection: Callable[[str], Awaitable[dict[str, Any] | None]] | None = None,
        unbound_marker: Callable[[], bool] = lambda: False, clear_marker: Callable[[], None] = lambda: None,
        stats: Stats | None = None, capacity: int = CAPACITY, idle: float = IDLE_SECONDS,
        now: Callable[[], float] = time.monotonic, own_bot: OwnBot | None = None,
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
        self.unbound_marker = unbound_marker
        self.clear_marker = clear_marker
        self._pushed_version: Any = object()        # ни с чем не равно: при запуске владелец передаётся
        self._owner_known = False                   # этот процесс передал сервису владельца
        self._read_failed = False
        self._refused: dict[str, float] = {}
        self._disabled: dict[str, float] = {}       # подключения, которые сервис держит выключенными
        self.own_bot = own_bot or OwnBot(call, stats=self.stats, now=now)
        self._standing_down = False                 # пересылка остановлена режимом own_bot

    # --- приём от обработчиков Telegram: мгновенно и без исключений ---

    def put(self, path: str, body: dict[str, Any], connection_id: str | None = None) -> bool:
        if self.own_bot.active:
            return False                # бизнес-поток ведёт бот сервиса: это не потеря
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
        await self.own_bot.refresh()
        if self._stand_down():
            return False
        await self.sync_owner()
        if self._stand_down() or not self._queue:
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

    def _stand_down(self) -> bool:
        """Останавливает пересылку на время режима own_bot и возобновляет её после. True — стоим."""
        if self.own_bot.active:
            if not self._standing_down or self._queue:
                self._standing_down = True
                self._queue.clear()     # сервис их не примет: он получает бизнес-поток через своего бота
                self.stats.set_queue(0)
                self._refused.clear()
                self._disabled.clear()
                self.stats.set_flag("business_disabled", None)
            return True
        if self._standing_down:
            self._standing_down = False
            self._pushed_version = object()     # сервис снова принимает владельца — передать заново
        return False

    async def sync_owner(self, *, force: bool = False) -> bool:
        """Сообщает сервису владельца, если привязка изменилась. True — сервис знает владельца."""
        if self.own_bot.active:
            return False                # владельца привязывает бот сервиса; повторов нет
        version = self.owner_version()
        if not force and version == self._pushed_version:
            return self._owner_known
        try:
            unbound = bool(self.unbound_marker())
            owner = self.owner() or {}
        except Exception as exc:  # noqa: BLE001 — не прочитали: это не значит «владельца нет»
            if not self._read_failed:      # проверка повторяется каждые несколько секунд — пишем один раз
                logger.warning("shturman: состояние привязки не прочитано (%s), повторим позже", type(exc).__name__)
            self._read_failed = True
            return self._owner_known
        self._read_failed = False
        if version != self._pushed_version:
            # с новым владельцем прежние отказы подключениям не действуют
            self._refused.clear()
            self._disabled.clear()
        user_id, chat_id = owner.get("user_id"), owner.get("chat_id")
        bound = _is_id(user_id) and _is_id(chat_id)
        if unbound or (not bound and self._owner_known):
            # Привязку сбросил сам плагин (отметка) либо владелец, которого этот процесс передавал,
            # достоверно исчез. Сервис останавливает бизнес-подключения, отклоняет ждущие черновики
            # и выключает автоответ, пока не привяжется новый владелец.
            try:
                await self.call("DELETE", "/api/owner", None)
            except ServiceUnavailable:
                self.stats.seen(False)
                raise _Later() from None
            except ServiceError as exc:
                if self.own_bot.refused(exc):
                    # Отметка сброса остаётся: «владелец отвязан» уйдёт, когда сервис снова примет.
                    return False
            self._owner_known = False
            try:
                self.clear_marker()
            except Exception:  # noqa: BLE001 — отметка останется: DELETE повторится, это безвредно
                pass
        if not bound:
            self._pushed_version = version
            return False
        try:
            await self.call("PUT", "/api/owner", {"user_id": user_id, "chat_id": chat_id})
        except ServiceUnavailable:
            self.stats.seen(False)
            raise _Later() from None
        except ServiceError as exc:
            self.own_bot.refused(exc)
            return False
        self.stats.seen(True)
        self._pushed_version = version
        self._owner_known = True
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
                out = await self._post(item.path, item.body)
            except ServiceError as exc:
                if self.own_bot.refused(exc):
                    return              # не отказ этому обновлению: пересылка останавливается целиком
                if attempt == 1 and exc.code == "unknown_connection" and cid and await self._resend_connection(cid):
                    continue
                if attempt == 1 and exc.code == "owner_unknown" and await self.sync_owner(force=True):
                    continue
                break
            reason = self._not_stored(item, out)
            if reason is None:
                self.stats.bump(_COUNTER[item.path])
                if item.path == MESSAGE:
                    self._disabled.pop(cid or "", None)
                    self.stats.set_flag("business_disabled", False)
                return
            if reason == "connection_disabled":
                # Сервис держит подключение выключенным. Один раз сверяемся с Telegram: если там
                # оно включено и принадлежит владельцу, сервис включит его и примет сообщение.
                if (attempt == 1 and item.path == MESSAGE and cid and not self._is_disabled(cid)
                        and await self._resend_connection(cid)):
                    continue
                if cid:
                    self._mark_disabled(cid)
                self.stats.bump("not_stored_disabled")
                self.stats.set_flag("business_disabled", True)
            else:
                self.stats.bump("not_stored_excluded" if reason == "excluded" else "not_stored_other")
            return
        if not self.own_bot.active:     # отказ own_bot — не отказ этому обновлению
            self.stats.bump("rejected")

    @staticmethod
    def _not_stored(item: Item, out: Mapping[str, Any]) -> str | None:
        """Причина, по которой сервис принял запрос, но ничего не записал; None — записал."""
        reason = out.get("reason") if isinstance(out.get("reason"), str) else None
        if item.path == MESSAGE and out.get("stored") is False:
            return reason or "unknown"
        if item.path == DELETED and reason == "connection_disabled":
            return reason
        return None

    def _is_disabled(self, connection_id: str) -> bool:
        until = self._disabled.get(connection_id)
        if until is None:
            return False
        if until <= self._now():
            del self._disabled[connection_id]
            return False
        return True

    def _mark_disabled(self, connection_id: str) -> None:
        if len(self._disabled) > 500:
            self._disabled.clear()
        self._disabled.setdefault(connection_id, self._now() + REFUSED_TTL)

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
                if self.own_bot.refused(exc):
                    return False
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
