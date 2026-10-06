"""Счётчики моста к сервису переписки. Только числа и признаки — содержимого здесь нет.

Исполнитель и пересылка работают в процессе шлюза, а страницу состояния отдаёт процесс
дашборда. Поэтому шлюз время от времени записывает счётчики в файл состояния `bridge`,
а дашборд читает его и по свежести записи судит, работает ли исполнитель.

Счётчики идут с запуска шлюза и при перезапуске начинаются заново (`started_at`).
"""

from __future__ import annotations

import time
from typing import Any, Callable

from .state import Store

STATE_NAME = "bridge"
HEARTBEAT_EVERY = 30          # секунд между записями, когда ничего не изменилось
STALE_AFTER = 90              # запись старше — исполнитель считается остановленным

COUNTERS = (
    "forwarded_messages",      # бизнес-сообщений (новых и изменённых) принято сервисом
    "forwarded_connections",   # подключений бизнес-режима передано
    "forwarded_deleted",       # уведомлений об удалении передано
    "dropped",                 # потеряно: очередь пересылки была полна, сервис долго не отвечал
    "rejected",                # сервис отказался принять (чужое подключение, неразборчивое сообщение)
    "jobs_done",
    "jobs_failed",
    "sends_unknown",           # отправок от имени владельца с неизвестным исходом
    "reports_lost",            # итог задания не удалось сообщить сервису
    "callbacks",               # нажатий кнопок передано сервису
    "callbacks_refused",       # нажатий не от владельца — не переданы
)


class Stats:
    def __init__(self, *, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self.started_at = int(now())
        self.counters: dict[str, int] = {name: 0 for name in COUNTERS}
        self.last_job_at: int | None = None
        self.reachable: bool | None = None       # None — к сервису ещё не обращались
        self.checked_at: int | None = None
        self.queue = 0
        self.version = 0                          # растёт при любом изменении

    def bump(self, name: str, amount: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + amount
        self.version += 1

    def job_finished(self, ok: bool) -> None:
        self.last_job_at = int(self._now())
        self.bump("jobs_done" if ok else "jobs_failed")

    def seen(self, reachable: bool) -> None:
        """Итог последнего обращения к сервису."""
        if self.reachable is not reachable:
            self.version += 1
        self.reachable = reachable
        self.checked_at = int(self._now())

    def set_queue(self, size: int) -> None:
        if size != self.queue:
            self.queue = size
            self.version += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "heartbeat_at": int(self._now()),
            "reachable": self.reachable,
            "checked_at": self.checked_at,
            "last_job_at": self.last_job_at,
            "queue": self.queue,
            "counters": dict(self.counters),
        }


class Heartbeat:
    """Записывает счётчики в файл состояния: при изменениях и раз в полминуты без них."""

    def __init__(self, store: Store, stats: Stats, *, now: Callable[[], float] = time.time) -> None:
        self.store, self.stats, self._now = store, stats, now
        self._written_version = -1
        self._written_at = 0.0

    def tick(self, *, force: bool = False) -> bool:
        now = self._now()
        changed = self.stats.version != self._written_version
        if not force and not changed and now - self._written_at < HEARTBEAT_EVERY:
            return False
        self.store.write(STATE_NAME, self.stats.snapshot())
        self._written_version, self._written_at = self.stats.version, now
        return True

    def clear(self) -> None:
        self.store.delete(STATE_NAME)


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def status(store: Store, *, configured: bool, now: Callable[[], float] = time.time) -> dict[str, Any]:
    """Состояние моста для страницы владельца: только числа и признаки."""
    data = store.read(STATE_NAME)
    heartbeat = _int(data.get("heartbeat_at"))
    running = bool(configured and heartbeat is not None and now() - heartbeat <= STALE_AFTER)
    raw = data.get("counters") if isinstance(data.get("counters"), dict) else {}
    reachable = data.get("reachable") if isinstance(data.get("reachable"), bool) else None
    return {
        "configured": bool(configured),
        "executor_running": running,
        # Что видел исполнитель при последнем обращении; пока он не работает — неизвестно.
        "reachable": reachable if running else None,
        "checked_at": _int(data.get("checked_at")) if running else None,
        "started_at": _int(data.get("started_at")),
        "last_job_at": _int(data.get("last_job_at")),
        "queue": _int(data.get("queue")) or 0,
        "counters": {name: _int(raw.get(name)) or 0 for name in COUNTERS},
    }
