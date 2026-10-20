"""Режим «согласования и бизнес-поток ведёт бот сервиса». Только стандартная библиотека.

У сервиса переписки может быть свой бот согласований. Пока он включён, сервис отказывает
плагину кодом `own_bot` (ответ 403) в привязке владельца, нажатиях кнопок и приёме обновлений
бизнес-режима, а в `GET /api/status` отдаёт `"own_bot": true`. Повторять такие запросы
бесполезно: это не сбой, а решение владельца.

Здесь хранится один признак на процесс шлюза и правило, когда его перепроверять:

  * режим включается по отказу с кодом `own_bot` либо по ответу `GET /api/status`;
  * состояние переспрашивается у сервиса не чаще раза в `RECHECK` секунд (и при запуске),
    чтобы заметить, что владелец включил или выключил своего бота;
  * пока сервис недоступен, признак не меняется: «не ответил» не значит «бота больше нет».

В журнал пишется одна строка при входе в режим и одна при выходе. Сам режим ничего
не отключает: что именно перестаёт делать плагин, решают пересылка (`ingest`) и обработчик
кнопок. Защита бизнес-режима от режима не зависит.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from .bridge_stats import Stats
from .service_client import ServiceError, ServiceUnavailable

logger = logging.getLogger("shturman.own_bot")

CODE = "own_bot"                  # машинный код отказа сервиса
STATUS = "/api/status"
RECHECK = 300                     # секунд между вопросами сервису о его боте
RECHECK_UNREACHABLE = 60          # сервис не ответил — спросить раньше
PROBE_TIMEOUT = 5.0

ServiceCall = Callable[..., Awaitable[dict[str, Any]]]


def is_refusal(exc: BaseException) -> bool:
    """Отказ «этим занимается бот сервиса»."""
    return (isinstance(exc, ServiceError) and not isinstance(exc, ServiceUnavailable)
            and exc.code == CODE)


class OwnBot:
    """call — обращение к сервису (`await call(method, path, json_body, timeout=...)`);
    без него состояние узнаётся только по отказам."""

    def __init__(self, call: ServiceCall | None = None, *, stats: Stats | None = None,
                 recheck: float = RECHECK, now: Callable[[], float] = time.monotonic) -> None:
        self.call = call
        self.stats = stats or Stats()
        self.recheck = recheck
        self._now = now
        self.active = False
        self._next_check: float | None = None      # None — ещё не спрашивали: спросить сразу

    def refused(self, exc: BaseException) -> bool:
        """Разбирает отказ сервиса. True — это отказ `own_bot`: режим включён, повтор не нужен."""
        if not is_refusal(exc):
            return False
        self._set(True)
        return True

    def due(self) -> bool:
        return self.call is not None and (self._next_check is None or self._now() >= self._next_check)

    async def refresh(self, *, force: bool = False) -> bool:
        """Спрашивает сервис, если пришло время. Возвращает, включён ли режим."""
        if self.call is None or not (force or self.due()):
            return self.active
        self._next_check = self._now() + self.recheck
        try:
            out = await self.call("GET", STATUS, None, timeout=PROBE_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except ServiceUnavailable:
            self.stats.seen(False)
            self._next_check = self._now() + min(self.recheck, RECHECK_UNREACHABLE)
            return self.active
        except Exception:  # noqa: BLE001 — неожиданный ответ: признак не трогаем, спросим в свой срок
            return self.active
        self.stats.seen(True)
        value = out.get(CODE) if isinstance(out, dict) else None
        # Сервис прежней версии поля не отдаёт: своего бота у него нет.
        self._set(value is True, known=isinstance(value, bool))
        return self.active

    def reset(self) -> None:
        """Мост остановлен: о сервисе больше ничего не известно. В журнал не пишет."""
        self.active = False
        self._next_check = None
        self.stats.set_flag("own_bot", None)

    def _set(self, active: bool, *, known: bool = True) -> None:
        if active:
            # Только что получили отказ — сервис своё состояние уже сообщил.
            self._next_check = self._now() + self.recheck
        if active and not self.active:
            logger.info("shturman: согласования и бизнес-поток ведёт бот сервиса переписки — "
                        "плагин их не пересылает и владельца не привязывает")
        elif self.active and not active:
            logger.info("shturman: бот сервиса переписки выключен — согласования и бизнес-поток "
                        "снова идут через плагин")
        self.active = active
        self.stats.set_flag("own_bot", active if known else None)
