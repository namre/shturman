"""Мост к сервису переписки в процессе шлюза Hermes: исполнитель заданий, пересылка обновлений
бизнес-режима и счётчики. Логика лежит в `shturman_core` — здесь только стыковка с Hermes
и python-telegram-bot.

Где запускается фоновая работа и почему именно там (Hermes 0.21.5, тег v2026.9.24):

  * Hermes вызывает фабрику обработчиков (`shturman_telegram.wire`) из `connect()` адаптера
    Telegram — это корутина, цикл событий уже работает
    (plugins/platforms/telegram/adapter.py:3238, gateway/platforms/base.py:2239-2268).
    Отсюда и создаются задачи: `asyncio.get_running_loop().create_task(...)`.
  * `Application.post_init` не годится: его вызывают только `run_polling`/`run_webhook`
    (python-telegram-bot 22.8, telegram/ext/_application.py:479), а Hermes вызывает
    `initialize()` и `start()` сам (adapter.py:3240-3241).
  * `job_queue` не годится: в поставке Hermes его нет (нужна необязательная зависимость).
  * Отдельного события «шлюз запущен» у плагинов нет (hermes_cli/plugins.py:108-204).

В момент вызова фабрики бот ещё не инициализирован, а при переподключении Hermes строит новое
приложение и вызывает фабрику снова (adapter.py:3112-3121). Поэтому исполнитель один на процесс
и всегда берёт текущее приложение; задания для бота он забирает, только когда оно работает.

Процесс дашборда и разовые запуски Hermes фабрику не вызывают — там исполнителя нет.

Библиотека python-telegram-bot (LGPL) импортируется только внутри функций, как и в остальном плагине.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from shturman_core import service_routes
from shturman_core.bridge_stats import Heartbeat, Stats
from shturman_core.executor import AUX_TASKS, Buttons, Executor, NotSent
from shturman_core.ingest import Ingest
from shturman_core.service_client import ServiceClient, ServiceUnavailable
from shturman_core.state import Store

logger = logging.getLogger("shturman.bridge")

CONFIG_RECHECK_SECONDS = 30       # как часто перепроверять настройки, пока сервис не подключён
HEARTBEAT_TICK = 10
CALLBACK_TIMEOUT = 5.0

# Причины, при которых запрос не покинул сервер: соединение не установлено либо не нашлось
# свободного соединения. Имена классов httpx; python-telegram-bot кладёт их в причину ошибки
# (telegram/request/_httpxrequest.py, блок except в do_request).
_NEVER_LEFT = ("ConnectError", "ConnectTimeout", "PoolTimeout")


def refusal(exc: BaseException) -> NotSent | None:
    """Точно ли Telegram не принял запрос. None — неизвестно: сообщение могло уйти.

    Точный отказ — ответ с кодом 4xx: python-telegram-bot превращает его в BadRequest (400),
    Forbidden (403), InvalidToken (401, 404), Conflict (409), RetryAfter (429), ChatMigrated
    (telegram/request/_baserequest.py, разбор кода ответа). Ошибки сервера (5xx), истёкшее время
    и обрыв связи — NetworkError и TimedOut: по ним понять, ушло ли сообщение, нельзя.
    """
    try:
        from telegram.error import (
            BadRequest, ChatMigrated, Conflict, EndPointNotFound, Forbidden, InvalidToken, RetryAfter,
        )
    except Exception:
        return None
    if isinstance(exc, RetryAfter):
        raw = getattr(exc, "retry_after", None)
        seconds = raw.total_seconds() if hasattr(raw, "total_seconds") else raw
        try:
            seconds = int(seconds)
        except (TypeError, ValueError):
            seconds = 30
        return NotSent("Telegram просит подождать (слишком много запросов)", retry_after=max(1, seconds))
    if isinstance(exc, (BadRequest, Forbidden, InvalidToken, ChatMigrated, Conflict, EndPointNotFound)):
        return NotSent(f"{type(exc).__name__}: {str(exc)[:200]}")
    cause = exc.__cause__
    if cause is not None and type(cause).__name__ in _NEVER_LEFT \
            and type(cause).__module__.split(".")[0] == "httpx":
        return NotSent(f"нет связи с Telegram ({type(cause).__name__})", replied=False)
    return None


class PtbBot:
    """Отправка в Telegram через бота Hermes. Всё — обычным текстом, без разметки:
    в сообщениях есть чужой текст, и разметка позволила бы подделать вид сообщения."""

    def __init__(self, runtime: "Runtime") -> None:
        self._runtime = runtime

    def _application(self) -> Any:
        return self._runtime.application

    def ready(self) -> bool:
        app = self._application()
        return app is not None and bool(getattr(app, "running", False))

    async def _call(self, method: str, **kwargs: Any) -> Any:
        app = self._application()
        if app is None:
            raise NotSent("бот не подключён к Telegram", replied=False)
        try:
            # Прямой вызов бота: ни python-telegram-bot, ни Hermes запрос сами не повторяют
            # (запасные адреса Hermes пробует только при ошибке соединения —
            # plugins/platforms/telegram/telegram_network.py:141-190, 304-305).
            return await getattr(app.bot, method)(**kwargs)
        except Exception as exc:
            refused = refusal(exc)
            if refused is not None:
                raise refused from None
            raise

    @staticmethod
    def _markup(buttons: Buttons | None) -> Any:
        if not buttons:
            return None
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        return InlineKeyboardMarkup(
            [[InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in buttons])

    async def send_owner(self, chat_id: int, text: str, buttons: Buttons | None, silent: bool) -> int:
        from telegram import LinkPreviewOptions

        message = await self._call(
            "send_message", chat_id=chat_id, text=text, parse_mode=None, reply_markup=self._markup(buttons),
            disable_notification=bool(silent), link_preview_options=LinkPreviewOptions(is_disabled=True))
        return int(message.message_id)

    async def edit_owner(self, chat_id: int, message_id: int, text: str, buttons: Buttons | None) -> None:
        from telegram import LinkPreviewOptions

        try:
            await self._call(
                "edit_message_text", chat_id=chat_id, message_id=message_id, text=text, parse_mode=None,
                reply_markup=self._markup(buttons), link_preview_options=LinkPreviewOptions(is_disabled=True))
        except NotSent as exc:
            if same_or_gone(exc.reason):
                return             # текст уже такой либо сообщения больше нет — править нечего
            raise

    async def send_business(self, connection_id: str, chat_id: int, text: str, reply_to: int | None) -> int:
        from telegram import ReplyParameters

        message = await self._call(
            "send_message", chat_id=chat_id, text=text, parse_mode=None,
            business_connection_id=connection_id,
            reply_parameters=ReplyParameters(message_id=reply_to) if reply_to else None)
        return int(message.message_id)

    async def business_connection(self, connection_id: str) -> dict[str, Any] | None:
        """Подключение бизнес-режима как объект Bot API. None — Telegram такого не знает."""
        try:
            connection = await self._call("get_business_connection", business_connection_id=connection_id)
        except NotSent as exc:
            if exc.retry_after or not exc.replied:
                raise              # временно: спросим позже
            return None
        return to_plain(connection)


def same_or_gone(reason: str) -> bool:
    lowered = reason.lower()
    return "not modified" in lowered or "not found" in lowered


def to_plain(obj: Any) -> dict[str, Any]:
    """Объект python-telegram-bot как словарь в виде Bot API (даты — числом, `from` вместо `from_user`)."""
    return json.loads(obj.to_json())


class Runtime:
    """Единственный на процесс владелец фоновой работы моста."""

    def __init__(self) -> None:
        self.application: Any = None
        self.store: Store | None = None
        self.stats = Stats()
        self.executor: Executor | None = None
        self.ingest: Ingest | None = None
        self._ctx: Any = None
        self._client: ServiceClient | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._home_loop: asyncio.AbstractEventLoop | None = None    # цикл шлюза; переживает остановку моста
        self._disabled = False
        self._next_config_check = 0.0

    # --- подключение ---

    def configure(self, ctx: Any) -> None:
        """Вызывается из register(): запоминает контекст плагина (доступ к модели Hermes)."""
        self._ctx = ctx
        self._disabled = False
        # Плагин перезагрузили на ходу: фабрику Hermes повторно не вызовет (она уже подключена
        # к этому приложению), поэтому работу возобновляем сами — в цикле шлюза.
        loop = self._home_loop
        if self.application is not None and loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self.ensure_started)
            except RuntimeError:
                pass

    def attach(self, application: Any, store: Store | None = None) -> None:
        """Вызывается фабрикой обработчиков: текущее приложение Telegram. При переподключении
        Hermes строит новое приложение — исполнитель остаётся прежним и берёт его."""
        self.application = application
        if store is not None:
            self.store = store
        self.ensure_started()

    def _llm(self) -> Any:
        try:
            return self._ctx.llm if self._ctx is not None else None
        except Exception:
            logger.warning("shturman: доступ к модели Hermes не получен", exc_info=True)
            return None

    @property
    def running(self) -> bool:
        return any(not task.done() for task in self._tasks.values())

    def ensure_started(self) -> bool:
        """Запускает фоновую работу, если сервис подключён и есть работающий цикл событий.
        Безопасно вызывать сколько угодно раз: работа запускается в одном экземпляре."""
        if self._disabled or self.application is None:
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False               # цикла ещё нет — запустимся при первом обновлении
        if self._loop is loop and self._tasks and all(not t.done() for t in self._tasks.values()):
            return True
        if self._loop is not loop:
            self._reset()
        if self._client is None:
            now = time.monotonic()
            if now < self._next_config_check:
                return False
            try:
                self._client = ServiceClient.from_env(service_routes.BRIDGE)
            except ValueError:
                self._client = None
            if self._client is None:
                # Сервис не подключён: всё новое молчит. Настройки перечитываются не на каждое обновление.
                self._next_config_check = now + CONFIG_RECHECK_SECONDS
                return False
        if self.executor is None:
            store = self.store or Store()
            self.store = store
            bot = PtbBot(self)
            self.executor = Executor(self.call, llm=self._llm(), bot=bot, stats=self.stats,
                                     owner=lambda: store.read("owner"), tasks=tuple(AUX_TASKS))
            self.ingest = Ingest(self.call, owner=lambda: store.read("owner"),
                                 owner_version=lambda: store.mtime("owner"),
                                 fetch_connection=bot.business_connection, stats=self.stats)
        self._loop = self._home_loop = loop
        jobs = {
            "shturman:jobs-bot": lambda: self.executor.run_lane("bot"),
            "shturman:jobs-llm": lambda: self.executor.run_lane("llm"),
            "shturman:ingest": lambda: self.ingest.run(),
            "shturman:heartbeat": self._heartbeat,
        }
        for name, factory in jobs.items():
            task = self._tasks.get(name)
            if task is None or task.done():
                self._tasks[name] = loop.create_task(factory(), name=name)
        logger.info("shturman: мост к сервису переписки запущен")
        return True

    def _reset(self) -> None:
        for task in self._tasks.values():
            if not task.done():
                try:
                    task.cancel()
                except RuntimeError:       # цикл той задачи уже закрыт
                    pass
        self._tasks = {}
        self._loop = None

    def shutdown(self) -> None:
        """Останавливает фоновую работу (выгрузка плагина). Повторно не запустится до register()."""
        self._disabled = True
        self._reset()
        self.executor = self.ingest = None
        self._client = None
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

    async def stop(self) -> None:
        """Останавливает работу и ждёт завершения задач."""
        tasks = [t for t in self._tasks.values() if not t.done()]
        self.shutdown()
        await asyncio.gather(*tasks, return_exceptions=True)

    # --- обращение к сервису ---

    async def call(self, method: str, path: str, json_body: Any = None, *, timeout: float | None = None) -> dict[str, Any]:
        """Запрос к сервису в отдельном потоке: клиент синхронный, цикл шлюза ждать не должен."""
        client = self._client
        if client is None:
            raise ServiceUnavailable("сервис переписки не подключён", code="not_configured")
        return await asyncio.get_running_loop().run_in_executor(
            self._threads(), functools.partial(client.request, method, path, json_body=json_body, timeout=timeout))

    def _threads(self) -> ThreadPoolExecutor:
        if self._pool is None:
            # Свой небольшой пул: общий пул цикла нужен самому Hermes.
            self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="shturman-service")
        return self._pool

    async def _heartbeat(self) -> None:
        heartbeat = Heartbeat(self.store or Store(), self.stats)
        try:
            while True:
                try:
                    # Запись на диск — в потоке: цикл шлюза её не ждёт.
                    await asyncio.get_running_loop().run_in_executor(self._threads(), heartbeat.tick)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — запись состояния не должна ронять мост
                    logger.warning("shturman: не удалось записать состояние моста (%s)", type(exc).__name__)
                await asyncio.sleep(HEARTBEAT_TICK)
        finally:
            try:
                heartbeat.clear()          # остановились — страница состояния увидит это сразу
            except Exception:
                pass

    # --- обновления бизнес-режима: только положить в очередь ---

    def forward(self, update: Any) -> None:
        """Кладёт обновление бизнес-режима в очередь пересылки. Исключений не выпускает."""
        try:
            if not self.ensure_started() or self.ingest is None:
                return
            if not (self.store or Store()).read("owner").get("user_id"):
                # Владельца нет (ещё не привязан либо привязка сброшена ссылкой восстановления):
                # ничью переписку в архив не отправляем. Сервис помнит прежнего владельца,
                # пока не получит нового, и сам бы это не остановил.
                self.stats.bump("rejected")
                return
            connection = getattr(update, "business_connection", None)
            if connection is not None:
                self.ingest.put_connection(to_plain(connection))
                return
            message = getattr(update, "business_message", None)
            edited = getattr(update, "edited_business_message", None)
            if message is not None or edited is not None:
                self.ingest.put_message(to_plain(message or edited), edited=message is None)
                return
            deleted = getattr(update, "deleted_business_messages", None)
            if deleted is not None:
                self.ingest.put_deleted(to_plain(deleted))
        except Exception as exc:  # noqa: BLE001 — пересылка не должна мешать шлюзу
            logger.warning("shturman: обновление бизнес-режима не поставлено в очередь (%s)", type(exc).__name__)

    def wake(self) -> None:
        if self.executor is not None:
            self.executor.wake("bot")


_runtime = Runtime()


def runtime() -> Runtime:
    return _runtime
