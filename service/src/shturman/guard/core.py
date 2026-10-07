"""Проверка сообщений и запись итога: одна точка, через которую `agent_visible` получает значение.

Два входа:
  * `Guard.screen(ids)` — живой источник только что записал сообщения скрытыми и ждёт итога,
    прежде чем сообщить о них остальным модулям;
  * `Guard.sweep()` — фоновый обход очереди. Очередь — сами строки архива (`guard_label IS NULL`
    у входящих текстовых сообщений), поэтому перезапуск ничего не теряет. Сюда попадают импорт,
    догрузка истории и всё, что живой путь не успел или не смог проверить.

Как выносится итог по одному сообщению:
  1. владелец уже решал про точно такой же текст — берётся его решение, оценщики не спрашиваются;
  2. оценка модели не ниже порога ИЛИ сработали правила — `suspect`, сообщение скрыто;
  3. модель ответила «обычное» (или модели нет вовсе, работают одни правила) — `ok`;
  4. модель настроена, но не ответила, правила не сработали — итога нет: сообщение ВИДНО
     ассистенту и остаётся в очереди непроверенным. Это осознанный выбор: остановка модели не
     должна останавливать ассистента. Число непроверенных видно в `/api/status` и `ops/doctor.sh`.

Текст сообщений в журнал не пишется: только счётчики, идентификаторы и вид ошибки.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Sequence

import asyncpg

from .. import events as ev
from ..config import ConfigError
from . import Scorer, alerts, rules

logger = logging.getLogger("shturman.guard")

# Что проверяется: входящие (не от владельца) текстовые сообщения. Условие дословно повторяет
# предикат индекса messages_guard_queue — иначе база не сможет идти по нему.
INCOMING = "m.is_outgoing IS NOT TRUE AND m.kind = 'message' AND m.text <> ''"
QUEUE = f"m.guard_label IS NULL AND {INCOMING}"

# Порог по умолчанию выбран измерением на русском наборе: docs/guard.md, раздел «Измерение».
DEFAULT_THRESHOLD = 0.9


@dataclass(frozen=True)
class Settings:
    threshold: float = DEFAULT_THRESHOLD
    use_rules: bool = True       # правила как второй оценщик рядом с моделью
    batch: int = 16              # сообщений за один шаг фонового обхода
    pause: float = 0.3           # между шагами, секунд: не занимать процессор сервера целиком
    idle: float = 5.0            # между проверками пустой очереди
    live_timeout: float = 8.0    # сколько живое сообщение ждёт модель; дольше — открывается непроверенным
    sweep_timeout: float = 120.0
    cooldown: float = 30.0       # после сбоя модели живой путь столько секунд её не ждёт
    notify_per_hour: int = alerts.DEFAULT_PER_HOUR
    notify_every: float = 60.0   # как часто обход досылает отложенные карточки
    backoff_base: float = 2.0
    backoff_max: float = 300.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env

        def number(name: str, default: float, low: float, high: float, cast: Callable[[str], float]) -> float:
            raw = (env.get(name) or "").strip()
            if not raw:
                return default
            try:
                value = cast(raw.replace(",", "."))
            except ValueError:
                raise ConfigError(f"{name}: нужно число") from None
            if not low <= value <= high:
                raise ConfigError(f"{name}: допустимо от {low} до {high}")
            return value

        return cls(
            threshold=number("SHTURMAN_GUARD_THRESHOLD", DEFAULT_THRESHOLD, 0.01, 1.0, float),
            use_rules=(env.get("SHTURMAN_GUARD_RULES") or "on").strip().lower() not in ("off", "0", "false", "no"),
            batch=int(number("SHTURMAN_GUARD_BATCH", 16, 1, 128, int)),
            pause=number("SHTURMAN_GUARD_PAUSE_MS", 300, 0, 60_000, int) / 1000,
            notify_per_hour=int(number("SHTURMAN_GUARD_NOTIFY_PER_HOUR", alerts.DEFAULT_PER_HOUR, 1, 60, int)),
        )


@dataclass
class Outcome:
    taken: int = 0        # сколько сообщений рассмотрено
    ok: int = 0
    hidden: int = 0       # скрыто на этом шаге (suspect и повторно confirmed)
    unresolved: int = 0   # модель не ответила: остались непроверенными
    model_failed: bool = False


_BY_IDS = f"""
SELECT m.id, m.text, md5(m.text) AS digest, m.agent_visible
FROM messages m
WHERE m.id = ANY($1::bigint[]) AND {QUEUE} AND m.deleted_at IS NULL
ORDER BY m.id
"""

# Сначала придержанные (скрыты и ждут итога: живой путь не довёл проверку до конца), затем
# остальная очередь, свежие первыми: недавняя переписка важнее старого импорта.
_HELD = f"""
SELECT m.id, m.text, md5(m.text) AS digest, m.agent_visible
FROM messages m
WHERE NOT m.agent_visible AND {QUEUE} AND m.deleted_at IS NULL
ORDER BY m.id LIMIT $1
"""
_NEXT = f"""
SELECT m.id, m.text, md5(m.text) AS digest, m.agent_visible
FROM messages m
WHERE {QUEUE} AND m.agent_visible AND m.deleted_at IS NULL AND m.id < $2
ORDER BY m.id DESC LIMIT $1
"""

# Итог пишется, только если строка всё ещё ждёт и текст тот же, что оценивали: правка или
# решение владельца, случившиеся за это время, не затираются.
_WRITE = """
UPDATE messages m
SET guard_label = v.label, agent_visible = v.visible, guard_score = v.score, guard_model = v.model,
    guard_checked_at = CASE WHEN v.label IS NULL THEN NULL ELSE now() END
FROM unnest($1::bigint[], $2::text[], $3::text[], $4::boolean[], $5::real[], $6::text[])
     AS v (id, digest, label, visible, score, model)
WHERE m.id = v.id AND m.guard_label IS NULL AND md5(m.text) = v.digest
RETURNING m.id, m.guard_label, m.agent_visible
"""

_TOP = 2**62   # «с самого свежего»: больше любого идентификатора сообщения

_COUNTERS = f"""
SELECT count(*) FILTER (WHERE m.guard_label IS NOT NULL) AS guard_checked,
       count(*) FILTER (WHERE NOT m.agent_visible AND m.guard_label IN ('suspect', 'confirmed')) AS guard_hidden,
       count(*) FILTER (WHERE NOT m.agent_visible AND m.guard_label = 'suspect') AS guard_waiting_owner,
       count(*) FILTER (WHERE m.guard_label = 'released') AS guard_released,
       count(*) FILTER (WHERE {QUEUE} AND m.deleted_at IS NULL) AS guard_unchecked
FROM messages m
"""


async def counters(conn: asyncpg.Connection) -> dict[str, int]:
    """Счётчики для владельца — только числа: проверено, скрыто (из них ждут решения),
    показано владельцем, не проверено."""
    return dict(await conn.fetchrow(_COUNTERS))


async def release_held(conn: asyncpg.Connection) -> int:
    """Открывает сообщения, придержанные до проверки, которой не будет (защита выключена)."""
    done = await conn.execute(
        "UPDATE messages SET agent_visible = true WHERE NOT agent_visible AND guard_label IS NULL")
    return int(done.split()[-1])


class Guard:
    """Работающая защита. Одна на процесс сервиса; доступна как `guard.current()`."""

    def __init__(
        self, pool: asyncpg.Pool, settings: Settings, *, model: Scorer | None,
        use_rules: bool | None = None, events: ev.Events | None = None, tz: str = "UTC",
    ) -> None:
        self.pool, self.settings, self.model, self.events, self.tz = pool, settings, model, events, tz
        # Без модели правила — единственный оценщик, выключить их нельзя.
        self.rules: rules.RulesScorer | None = (
            rules.RulesScorer() if (model is None or (settings.use_rules if use_rules is None else use_rules))
            else None)
        # Что не так с моделью: None, "unreachable" (не отвечает), "model_mismatch" (отвечает не та).
        self.problem: str | None = None
        self._blocked_until = 0.0
        self._cursor: int | None = None   # где фоновый обход остановился в очереди, пока модель молчит

    @property
    def name(self) -> str:
        parts = [self.model.name] if self.model is not None else []
        if self.rules is not None:
            parts.append(self.rules.name)
        return "+".join(parts)

    # --- оценщики ---

    async def _model_scores(self, texts: Sequence[str], *, live: bool) -> list[float] | None:
        """Оценки модели либо None, если модели нет или она не ответила как надо."""
        if self.model is None:
            return None
        if live and time.monotonic() < self._blocked_until:
            return None   # недавно был сбой: живое сообщение его не ждёт, модель проверит фоновый обход
        timeout = self.settings.live_timeout if live else self.settings.sweep_timeout
        try:
            scores = await asyncio.wait_for(asyncio.to_thread(self.model.score, list(texts)), timeout)
            if not isinstance(scores, list) or len(scores) != len(texts) or not all(
                    isinstance(s, (int, float)) and not isinstance(s, bool) and math.isfinite(s) for s in scores):
                raise ValueError("оценщик вернул не то")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            problem = "model_mismatch" if getattr(exc, "mismatch", False) else "unreachable"
            if self.problem != problem:
                # Только вид ошибки: в её тексте могли бы оказаться адрес и фрагмент запроса.
                logger.warning("классификатор не ответил (%s): сообщения остаются непроверенными и видимыми",
                               getattr(exc, "reason", None) or type(exc).__name__)
            self.problem = problem
            self._blocked_until = time.monotonic() + self.settings.cooldown
            return None
        if self.problem is not None:
            logger.info("классификатор снова отвечает")
        self.problem = None
        self._blocked_until = 0.0
        return [float(s) for s in scores]

    # --- итог ---

    async def judge(self, rows: Sequence[Mapping[str, Any]], *, live: bool) -> Outcome:
        """Выносит и записывает итог по строкам очереди (поля id, text, digest, agent_visible)."""
        out = Outcome(taken=len(rows))
        if not rows:
            return out
        async with self.pool.acquire() as conn:
            known = await alerts.remembered(conn, sorted({r["digest"] for r in rows}))
        fresh = [r for r in rows if r["digest"] not in known]
        texts = [r["text"] for r in fresh]
        rule_scores = self.rules.score(texts) if (self.rules is not None and fresh) else None
        model_scores = await self._model_scores(texts, live=live) if fresh else None
        out.model_failed = bool(fresh) and self.model is not None and model_scores is None

        ids: list[int] = []
        digests: list[str] = []
        labels: list[str | None] = []
        visible: list[bool] = []
        scores: list[float | None] = []
        models: list[str | None] = []

        def put(row: Mapping[str, Any], label: str | None, shown: bool, score: float | None, model: str | None) -> None:
            ids.append(row["id"]); digests.append(row["digest"]); labels.append(label)
            visible.append(shown); scores.append(score); models.append(model)

        for row in rows:
            decided = known.get(row["digest"])
            if decided is not None:
                put(row, decided, decided == "released", None, "owner")
        for index, row in enumerate(fresh):
            by_model = model_scores is not None and model_scores[index] >= self.settings.threshold
            by_rules = rule_scores is not None and rule_scores[index] >= rules.THRESHOLD
            if by_model or by_rules:
                who = "+".join(([self.model.name] if by_model else []) + ([rules.NAME] if by_rules else []))
                put(row, "suspect", False, model_scores[index] if by_model else rule_scores[index], who)
            elif model_scores is not None:
                put(row, "ok", True, model_scores[index], self.model.name)
            elif self.model is None:
                put(row, "ok", True, rule_scores[index] if rule_scores is not None else None, rules.NAME)
            else:
                out.unresolved += 1
                if not row["agent_visible"]:
                    put(row, None, True, None, None)   # придержанное открывается непроверенным
        if not ids:
            return out
        async with self.pool.acquire() as conn:
            written = await conn.fetch(_WRITE, ids, digests, labels, visible, scores, models)
        hidden = [r["id"] for r in written if not r["agent_visible"]]
        out.hidden = len(hidden)
        out.ok = sum(1 for r in written if r["guard_label"] in ("ok", "released"))
        if hidden:
            logger.info("скрыто от ассистента сообщений: %d", len(hidden))
            if self.events is not None:
                # Сообщение могло быть видно раньше (импорт): выведенное из него должно уйти.
                self.events.publish(ev.MESSAGES_HIDDEN, {"message_ids": hidden})
            await self.notify()
        return out

    async def notify(self, *, extra: int = 0) -> dict[str, int]:
        async with self.pool.acquire() as conn:
            return await alerts.pump(conn, per_hour=self.settings.notify_per_hour, tz=self.tz, extra=extra)

    # --- живой путь ---

    async def screen(self, message_ids: Sequence[int]) -> set[int]:
        """Проверяет только что записанные сообщения. Возвращает те, что остались скрытыми.

        Никогда не бросает исключений в записывающий путь: при любой неожиданной ошибке
        придержанные сообщения открываются непроверенными, как при недоступной модели.
        """
        ids = [int(i) for i in message_ids]
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(_BY_IDS, ids)
            await self.judge(rows, live=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("проверка сообщений не выполнена (%s): открыты непроверенными", type(exc).__name__)
            try:
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        """UPDATE messages SET agent_visible = true
                           WHERE id = ANY($1::bigint[]) AND NOT agent_visible AND guard_label IS NULL""", ids)
            except Exception:
                pass
        try:
            async with self.pool.acquire() as conn:
                hidden = await conn.fetch(
                    "SELECT id FROM messages WHERE id = ANY($1::bigint[]) AND NOT agent_visible", ids)
        except Exception:
            return set()
        return {r["id"] for r in hidden}

    # --- фоновый обход ---

    async def sweep(self) -> Outcome:
        """Один шаг обхода очереди."""
        batch = self.settings.batch
        async with self.pool.acquire() as conn:
            rows = list(await conn.fetch(_HELD, batch))
            if len(rows) < batch:
                rest = await conn.fetch(_NEXT, batch - len(rows), self._cursor or _TOP)
                if not rest and self._cursor is not None:
                    # дошли до конца очереди: следующий круг — снова со свежих
                    self._cursor = None
                    rest = await conn.fetch(_NEXT, batch - len(rows), _TOP)
                rows += rest
        if not rows:
            return Outcome()
        out = await self.judge(rows, live=False)
        if out.model_failed:
            # Модель молчит. Правила эти строки уже посмотрели; чтобы не крутиться на одних и тех же,
            # обход идёт дальше вглубь очереди, а к ним вернётся на следующем круге.
            self._cursor = min(r["id"] for r in rows)
        else:
            self._cursor = None   # модель отвечает: обход снова начинает со свежих
        return out

    async def run(self, *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        """Бесконечный цикл обхода. Любой сбой — пауза с удвоением и повтор; сам не падает."""
        failures = 0
        notified_at = 0.0
        while True:
            try:
                out = await self.sweep()
                if time.monotonic() - notified_at >= self.settings.notify_every:
                    notified_at = time.monotonic()
                    await self.notify()   # досылает карточки, отложенные из-за предела в час
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                if failures == 1 or failures % 10 == 0:
                    logger.warning("обход защиты: сбой (%s), подряд: %d", type(exc).__name__, failures)
                await sleep(min(self.settings.backoff_max, self.settings.backoff_base * 2 ** min(failures - 1, 30)))
                continue
            if out.model_failed:
                failures += 1
                await sleep(min(self.settings.backoff_max, self.settings.backoff_base * 2 ** min(failures - 1, 30)))
                continue
            failures = 0
            await sleep(self.settings.pause if out.taken else self.settings.idle)
