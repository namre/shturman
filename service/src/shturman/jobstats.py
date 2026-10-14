"""Счётчик итогов заданий модели за последний час — для строки `jobs-failing` в `./ops/doctor.sh`.

Очередь заданий в базе хранит только последний итог задания: неудачная попытка с повтором
возвращает его в очередь, и по базе не видно, сколько попыток упало за час. Поэтому сервис
считает итоги сам, в памяти, по минутам: сколько попыток заданий модели (`llm.structured`,
`llm.text`) закончились удачно, сколько — неудачно и с какими видами ошибок. Считаются итоги
обоих исполнителей — плагина в Hermes и своего исполнителя сервиса.

Только числа и виды ошибок (`label`): имя класса исключения или короткий код. Ни текста ошибки,
ни номеров заданий, ни содержимого. После перезапуска сервиса счёт начинается заново —
`window_s` в сводке говорит, за сколько секунд он собран.
"""

from __future__ import annotations

import re
import threading
import time
from collections import Counter
from typing import Any, Callable

WINDOW = 3600          # секунд
BUCKET = 60            # секунд в одной корзине
TOP = 5                # видов ошибок в сводке
MAX_LABELS = 50        # разных видов ошибок в одной корзине

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
# Постоянные строки ошибок исполнителей (их пишет код, а не переписка) и их коды.
_KNOWN = (
    ("у плагина нет доступа к модели", "no_model_access"),
    ("у сервиса нет своего доступа к модели", "no_own_model"),
    ("в задании нет поля", "bad_payload"),
    ("поле ", "bad_payload"),
    ("исполнитель не знает", "unknown_kind"),
    ("исполнитель не ответил", "lost"),
    ("исполнитель не забрал вовремя", "expired"),
)


def label_of(error: str | None) -> str:
    """Вид ошибки: имя класса, «модель: код» своего исполнителя или код постоянной фразы.
    Всё прочее — `other`: свободный текст в сводку не попадает."""
    e = (error or "").strip()
    if _IDENT.fullmatch(e):
        return e
    if e.startswith("модель:"):
        code = e.partition(":")[2].strip()
        return f"model:{code}" if _IDENT.fullmatch(code) else "model"
    for prefix, code in _KNOWN:
        if e.startswith(prefix):
            return code
    return "other"


class JobStats:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._started = clock()
        self._lock = threading.Lock()
        self._buckets: dict[int, list[Any]] = {}        # минута -> [удачно, неудачно, Counter видов]

    def _bucket(self, now: float) -> list[Any]:
        key = int(now // BUCKET)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = self._buckets[key] = [0, 0, Counter()]
            oldest = key - WINDOW // BUCKET
            for stale in [k for k in self._buckets if k <= oldest]:
                del self._buckets[stale]
        return bucket

    def record(self, ok: bool, error: str | None = None) -> None:
        with self._lock:
            bucket = self._bucket(self._clock())
            if ok:
                bucket[0] += 1
                return
            bucket[1] += 1
            label = label_of(error)
            if label in bucket[2] or len(bucket[2]) < MAX_LABELS:
                bucket[2][label] += 1
            else:
                bucket[2]["other"] += 1

    def snapshot(self) -> dict[str, Any]:
        """{window_s, done, failed, errors: [[вид, число], …]} за последний час (или с запуска)."""
        with self._lock:
            now = self._clock()
            oldest = int(now // BUCKET) - WINDOW // BUCKET
            done = failed = 0
            errors: Counter = Counter()
            for key, (ok_n, fail_n, labels) in self._buckets.items():
                if key > oldest:
                    done += ok_n
                    failed += fail_n
                    errors.update(labels)
        return {"window_s": int(min(WINDOW, now - self._started)), "done": done, "failed": failed,
                "errors": [[label, n] for label, n in errors.most_common(TOP)]}


_stats = JobStats()


def stats() -> JobStats:
    return _stats


def reset(clock: Callable[[], float] = time.monotonic) -> JobStats:
    """Новый счётчик (для тестов)."""
    global _stats
    _stats = JobStats(clock)
    return _stats
