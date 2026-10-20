"""Защита в составе сервиса: запуск, фоновый обход, маршруты `/api/guard/*`.

Включается переменной окружения сервиса `SHTURMAN_GUARD=on` (на сервере её пишет
`./ops/guard.sh on`). Маршрута, который включает или выключает защиту, нет и быть не должно:
внутренний API доступен ассистенту, а выключатель защиты от него — нет.

Маршруты отдают только числа и состояние. Текста скрытых сообщений во внутреннем API нет:
его видит только владелец — в карточке бота и в самом Telegram.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, AsyncIterator

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from .. import bridge
from ..config import Config
from . import Scorer, alerts, core, current, set_current
from .tei import TeiScorer

logger = logging.getLogger("shturman.guard")


def build_model(config: Config) -> Scorer | None:
    """Клиент модели-классификатора по настройкам. Тесты подменяют эту функцию."""
    if not config.guard_url:
        return None
    return TeiScorer(config.guard_url, config.guard_model)


async def overview(conn: Any, config: Config) -> dict[str, Any]:
    """Состояние защиты для `/api/status` и `ops/doctor.sh`: настройки и счётчики, без текстов."""
    guard = current()
    out: dict[str, Any] = {
        "guard_enabled": guard is not None,
        # Чем проверяется: «модель+rules-N», одни правила либо None, если защита выключена.
        "guard_scorer": guard.name if guard is not None else None,
        "guard_model_used": bool(guard is not None and guard.model is not None),
        # None — в порядке; unreachable — модель не отвечает; model_mismatch — отвечает не та.
        "guard_problem": guard.problem if guard is not None else None,
    }
    out.update(await core.counters(conn))
    return out


async def status(request: Request) -> JSONResponse:
    state = request.app.state.shturman
    async with state.ro_pool.acquire() as conn:
        out = await overview(conn, state.config)
        out["guard_waiting_notice"] = await alerts.waiting(conn)
    guard = current()
    if guard is not None:
        out["guard_threshold"] = guard.settings.threshold
        out["guard_notify_per_hour"] = guard.settings.notify_per_hour
    out["confirm_required"] = bridge.owns_bot()
    return JSONResponse(out)


def routes() -> list[BaseRoute]:
    return [Route("/api/guard/status", status, methods=["GET"])]


@contextlib.asynccontextmanager
async def lifespan(state: Any) -> AsyncIterator[None]:
    config: Config = state.config
    if not config.guard:
        # Защиту выключили: сообщения, придержанные до проверки, которой уже не будет, открываются.
        # Скрытые по итогу проверки (suspect, confirmed) остаются скрытыми.
        async with state.pool.acquire() as conn:
            released = await core.release_held(conn)
        if released:
            logger.info("защита выключена: открыто придержанных сообщений: %d", released)
        yield
        return
    settings = core.Settings.from_env()
    model = build_model(config)
    guard = core.Guard(state.pool, settings, model=model, events=state.events, tz=config.timezone)
    if model is None:
        logger.warning("защита включена без модели (SHTURMAN_GUARD_URL пуст): работают только правила — "
                       "это слабее модели, см. docs/guard.md")
    set_current(guard)
    state.extras["guard"] = guard
    state.spawn(guard.run(), name="guard-sweeper")
    try:
        yield
    finally:
        state.extras.pop("guard", None)
        if current() is guard:
            set_current(None)
        close = getattr(model, "close", None)
        if callable(close):
            close()
