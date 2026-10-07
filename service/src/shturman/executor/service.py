"""Запуск и остановка своего исполнителя, маршрут его состояния.

Что запускается, решается по настройкам сервиса — при старте и заново, когда владелец меняет
токен бота или ключ модели на странице настройки (`Control.restart`):
  * задан `SHTURMAN_BOT_TOKEN` — опрос бота согласований и дорожка заданий бота; виды
    `notify.owner` и `notify.edit` объявляются своими (`bridge.set_builtin`), а с ними внутренний
    API перестаёт принимать нажатия кнопок и владельца от плагина;
  * заданы `SHTURMAN_LLM_API_KEY` и `SHTURMAN_LLM_MODEL` — дорожка заданий модели; виды
    `llm.structured` и `llm.text` объявляются своими;
  * ничего не задано — модуль ничего не делает, всё выполняет плагин, как раньше.

Если бот настроен, но не работает (неверный токен, нет связи, бота опрашивает кто-то ещё),
задания бота плагину не возвращаются: они ждут в очереди и снимаются по сроку. Вернуть их
плагину значило бы снова пустить нажатия владельца через Hermes.

`GET /api/executor/status` отдаёт только признаки и счётчики: ни идентификаторов, ни текстов.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from .. import bridge
from ..api_core import handler
from ..app import AppState, state_of
from . import binding
from .bot import Bot
from .botapi import BotApi
from .llm import LlmClient, task_models
from .worker import Worker

logger = logging.getLogger("shturman.executor")

BOT_OWNED = (bridge.NOTIFY_OWNER, bridge.NOTIFY_EDIT)
LLM_OWNED = (bridge.LLM_STRUCTURED, bridge.LLM_TEXT)

# Только для тестов: подставные Telegram и сервер модели, короткие паузы.
# Ключи: bot_transport, llm_transport, poll, idle.
TEST_OVERRIDES: dict[str, Any] = {}


@dataclass
class Executor:
    """Действующий исполнитель — лежит в `state.extras["executor"]`."""
    api: BotApi | None = None
    bot: Bot | None = None
    llm: LlmClient | None = None
    worker: Worker | None = None
    kinds: frozenset[str] = field(default_factory=frozenset)


def _age(moment: float | None) -> int | None:
    return None if moment is None else max(0, int(time.monotonic() - moment))


@handler
async def status(request: Request) -> JSONResponse:
    state = state_of(request)
    runtime: Executor = state.extras.get("executor") or Executor()
    bot, llm, worker = runtime.bot, runtime.llm, runtime.worker
    owner_bound = False
    if bot is not None:
        async with state.ro_pool.acquire() as conn:
            owner_bound = await binding.bound_owner(conn, bot.bot_id) is not None
    out = {
        "bot": {
            "configured": bot is not None,
            "username": bot.identity["username"] if bot and bot.identity else None,
            "polling": bot.polling if bot else None,
            "problem": (runtime.api.broken if runtime.api and runtime.api.broken else bot.problem) if bot else None,
            "last_poll_age": _age(bot.last_poll_at) if bot else None,
            "last_update_age": _age(bot.last_update_at) if bot else None,
            "owner_bound": owner_bound,
            "bind_paused": bot.flood.locked() if bot else False,
            "business_capable": bot.business_capable if bot else None,
            "counters": dict(bot.counters) if bot else {},
        },
        "llm": {
            "configured": llm is not None,
            "model": llm.model if llm else None,
            "task_models": dict(llm.models) if llm else {},
            "problem": (llm.broken or llm.last_error) if llm else None,
            "last_call_ok": llm.last_ok if llm else None,
            "calls": llm.calls if llm else 0,
            "failures": llm.failures if llm else 0,
        },
        "jobs": {
            "kinds": sorted(runtime.kinds),
            "done": dict(worker.done) if worker else {},
            "failed": dict(worker.failed) if worker else {},
            "sends_unknown": worker.counters["sends_unknown"] if worker else 0,
            "reports_lost": worker.counters["reports_lost"] if worker else 0,
        },
    }
    return JSONResponse(out)


def routes() -> list[BaseRoute]:
    return [Route("/api/executor/status", status, methods=["GET"])]


class Control:
    """Запуск, остановка и перезапуск исполнителя внутри работающего сервиса.

    Перезапуск нужен странице настройки (`setup_page/`): владелец ввёл или сменил токен бота
    либо ключ модели, и они должны начать действовать без перезапуска процесса. Лежит в
    `state.extras["executor_control"]`.
    """

    def __init__(self, state: AppState) -> None:
        self.state = state
        self.lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []
        self._clients: list[Any] = []
        self._announced = False      # объявлял ли этот модуль свои виды заданий

    async def start(self) -> None:
        """Собирает исполнителя по нынешним настройкам (`state.config`) и запускает его."""
        state, config = self.state, self.state.config
        api = llm = None
        kinds: set[str] = set()
        if config.own_bot:
            api = BotApi(config.bot_token, proxy_url=config.proxy_url, transport=TEST_OVERRIDES.get("bot_transport"))
            kinds.update(BOT_OWNED)
            if api.broken:
                logger.error("бот согласований не может работать (%s)", api.broken)
        if config.own_llm:
            llm = LlmClient(
                base_url=config.llm_base_url, api_key=config.llm_api_key, model=config.llm_model,
                models=task_models(os.environ), proxy_url=config.proxy_url,
                transport=TEST_OVERRIDES.get("llm_transport"),
                tokens_param=os.environ.get("SHTURMAN_LLM_TOKENS_PARAM", "").strip(),
            )
            kinds.update(LLM_OWNED)
            if llm.broken:
                logger.error("свой доступ к модели не может работать (%s)", llm.broken)
        self._clients = [client for client in (api, llm) if client is not None]
        # При перезапуске прежний перечень заранее не снимается, а заменяется здесь: так плагину
        # ни на миг не открываются привязка владельца и нажатия. Модуль, который ничего своего
        # не объявлял, чужого перечня не трогает.
        if kinds or self._announced:
            bridge.set_builtin(kinds)
        self._announced = bool(kinds)
        if not kinds:
            state.extras["executor"] = Executor()
            return

        worker = Worker(state, api=api, llm=llm, idle=TEST_OVERRIDES.get("idle", 1.0))
        bot = None
        if api is not None:
            bot = Bot(state, api, wake=worker.wake, poll=TEST_OVERRIDES.get("poll"))
            worker.bot = bot
        state.extras["executor"] = Executor(api=api, bot=bot, llm=llm, worker=worker, kinds=frozenset(kinds))
        # Задания этих видов, поставленные до включения своего исполнителя, плагину больше не
        # отдаются: в карточках — метки кнопок, которые должны дойти только до бота согласований.
        async with state.pool.acquire() as conn:
            await conn.execute(
                """UPDATE jobs SET executor = 'builtin'
                   WHERE status IN ('queued', 'running') AND executor = 'plugin' AND kind = ANY($1::text[])""",
                sorted(kinds))
        if bot is not None:
            self._tasks.append(state.spawn(bot.run(), name="executor-bot-poll"))
            self._tasks.append(state.spawn(worker.run_lane("bot"), name="executor-jobs-bot"))
        if llm is not None:
            self._tasks.append(state.spawn(worker.run_lane("llm"), name="executor-jobs-llm"))
        logger.info("свой исполнитель запущен: бот — %s, модель — %s",
                    "да" if bot else "нет", "да" if llm else "нет")

    async def _halt(self) -> None:
        """Останавливает опрос и дорожки заданий и закрывает клиентов. Перечень своих видов
        заданий не трогает: его выставляет `start` или окончательный `stop`."""
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        clients, self._clients = self._clients, []
        for client in clients:
            with contextlib.suppress(Exception):
                await client.aclose()

    async def stop(self) -> None:
        async with self.lock:
            await self._halt()
            if self._announced:
                bridge.set_builtin(())
                self._announced = False
            self.state.extras["executor"] = Executor()

    async def restart(self) -> None:
        """Пересобирает исполнителя по изменившимся настройкам. Задание, которое выполнялось
        в этот миг, остаётся в очереди и вернётся по истечении аренды — как при перезапуске
        сервиса; отправка от имени владельца сама при этом не повторяется (см. worker.py)."""
        async with self.lock:
            await self._halt()
            await self.start()


@contextlib.asynccontextmanager
async def lifespan(state: AppState) -> AsyncIterator[None]:
    control = Control(state)
    state.extras["executor_control"] = control
    await control.start()
    try:
        yield
    finally:
        # Фоновые задачи к этому моменту остановлены (см. app.build_app).
        await control.stop()
        state.extras.pop("executor_control", None)
