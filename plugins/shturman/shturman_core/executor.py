"""Исполнитель заданий сервиса переписки. Только стандартная библиотека.

У сервиса нет ни модели, ни бота: то и другое есть только у Hermes. Сервис ставит задания
в очередь, а этот исполнитель в процессе шлюза забирает их, выполняет и сообщает итог.
Видов заданий пять (договор — `service/src/shturman/bridge.py`), и зачем они нужны сервису,
исполнитель не знает:

  llm.structured, llm.text          — обращение к модели Hermes;
  notify.owner, notify.edit         — сообщение владельцу в управляющий чат и его правка;
  business.send                     — отправка от имени владельца через бизнес-бота.

Две независимые дорожки, чтобы долгий ответ модели не задерживал нажатие владельца «Отправить»:
дорожка бота берёт по одному заданию и выполняет их по порядку, дорожка модели — до двух сразу.

Главное правило — отправка от имени владельца не повторяется никогда. Итог `business.send`
бывает только трёх видов:
  * ушло — сервису возвращается номер сообщения;
  * Telegram точно отказал (ответ 4xx) либо запрос не покидал сервер — ошибка начинается
    с «not_sent:»;
  * всё остальное (истекло время, оборвалась связь) — исход неизвестен: сообщение могло уйти.
Повтор (`retry_in`) для этого вида всегда пуст.

В журнал пишется только вид задания и вид ошибки: тексты сообщений, содержимое заданий
и токен туда не попадают.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Sequence

from .bridge_stats import Stats
from .service_client import ServiceError, ServiceUnavailable

logger = logging.getLogger("shturman.executor")

LLM_STRUCTURED = "llm.structured"
LLM_TEXT = "llm.text"
NOTIFY_OWNER = "notify.owner"
NOTIFY_EDIT = "notify.edit"
BUSINESS_SEND = "business.send"
LLM_KINDS = (LLM_STRUCTURED, LLM_TEXT)
BOT_KINDS = (NOTIFY_OWNER, NOTIFY_EDIT, BUSINESS_SEND)

NOT_SENT_PREFIX = "not_sent:"
CALLBACK_PREFIX = "sh:"           # чужие префиксы кнопок (ядра Hermes, плагина бизнес-режима) не пропускаются
CALLBACK_DATA_LIMIT = 64          # байт — предел Telegram
TEXT_LIMIT = 4096                 # знаков в одном сообщении Telegram
MAX_BUTTON_ROWS = 20
MAX_BUTTONS_IN_ROW = 8
KEYBOARDS_REMEMBERED = 500

# Вспомогательные задачи модели: владелец может закрепить за ними дешёвую модель в настройках Hermes.
AUX_TASKS: dict[str, tuple[str, str]] = {
    "shturman_extract": (
        "Штурман: разбор переписки",
        "Извлечение обязательств и сроков из переписки и проверка сообщений наблюдателем. "
        "Фоновая работа: подойдёт недорогая модель.",
    ),
    "shturman_reply": (
        "Штурман: текст ответа",
        "Черновики ответов и автоответ доверенным собеседникам.",
    ),
}
DEFAULT_TASK = {LLM_STRUCTURED: "shturman_extract", LLM_TEXT: "shturman_reply"}
DEFAULT_MAX_TOKENS = {LLM_STRUCTURED: 2000, LLM_TEXT: 1500}
MAX_TOKENS_CAP = 8000

IDLE_SECONDS = 2.5
MAX_BACKOFF = 60.0
REPORT_DELAYS = (0, 1, 2, 4, 8, 15)     # секунд перед попытками сообщить итог

ServiceCall = Callable[..., Awaitable[dict[str, Any]]]
Buttons = list[list[tuple[str, str]]]


class NotSent(Exception):
    """Telegram точно не принял сообщение: он ответил отказом либо запрос не покидал сервер."""

    def __init__(self, reason: str, *, retry_after: int | None = None, replied: bool = True) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after
        self.replied = replied          # False — Telegram не отвечал: запрос до него не дошёл


class _Fail(Exception):
    def __init__(self, error: str, retry_in: int | None) -> None:
        super().__init__(error)
        self.error, self.retry_in = error, retry_in


@dataclass(frozen=True)
class Outcome:
    result: dict[str, Any] | None = None
    error: str = ""
    retry_in: int | None = None

    @property
    def ok(self) -> bool:
        return self.result is not None


def parse_buttons(raw: Any) -> Buttons | None:
    """Кнопки задания как строки пар (подпись, данные). Данные передаются без изменений.

    Принимаются только кнопки сервиса (данные начинаются с «sh:»): иначе сервис мог бы показать
    владельцу кнопку, которую разберёт ядро Hermes — например, одобрение команды.
    """
    if raw is None or raw == []:
        return None
    if not isinstance(raw, list) or len(raw) > MAX_BUTTON_ROWS:
        raise ValueError("кнопки заданы неверно")
    rows: Buttons = []
    for row in raw:
        if not isinstance(row, list) or not row or len(row) > MAX_BUTTONS_IN_ROW:
            raise ValueError("кнопки заданы неверно")
        out: list[tuple[str, str]] = []
        for item in row:
            text = item.get("text") if isinstance(item, dict) else None
            data = item.get("data") if isinstance(item, dict) else None
            if not isinstance(text, str) or not text.strip() or not isinstance(data, str):
                raise ValueError("кнопки заданы неверно")
            if not data.startswith(CALLBACK_PREFIX) or len(data.encode("utf-8")) > CALLBACK_DATA_LIMIT:
                raise ValueError("данные кнопки не принадлежат сервису или длиннее 64 байт")
            out.append((text, data))
        rows.append(out)
    return rows


def _text(payload: Mapping[str, Any], key: str = "text") -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _Fail(f"в задании нет поля {key}", None)
    return value


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _brief(exc: BaseException, limit: int = 300) -> str:
    return f"{type(exc).__name__}: {str(exc)[:limit]}".rstrip(": ")


class Executor:
    """Забирает задания у сервиса и выполняет их.

    call   — обращение к сервису: `await call(method, path, json_body)`; бросает `ServiceError`;
    llm    — `ctx.llm` Hermes (или None, пока его нет);
    bot    — отправка в Telegram: `ready()`, `send_owner`, `edit_owner`, `send_business`;
    owner  — функция, возвращающая привязанного владельца ({user_id, chat_id}) или {}.
    """

    def __init__(
        self, call: ServiceCall, *, llm: Any, bot: Any, owner: Callable[[], Mapping[str, Any]],
        stats: Stats | None = None, tasks: Sequence[str] = tuple(AUX_TASKS), worker: str = "shturman-plugin",
        llm_slots: int = 2, idle: float = IDLE_SECONDS, llm_timeout: float = 90.0, bot_timeout: float = 30.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.call = call
        self.llm = llm
        self.bot = bot
        self.owner = owner
        self.stats = stats or Stats()
        self.tasks = tuple(tasks)
        self.worker = worker
        self.llm_slots = max(1, min(int(llm_slots), 2))
        self.idle = idle
        self.llm_timeout = llm_timeout
        self.bot_timeout = bot_timeout
        self._sleep = sleep
        self._wake: dict[str, asyncio.Event] = {}
        # Клавиатуры отправленных сообщений: Telegram снимает кнопки при правке текста,
        # если их не передать заново, а спросить у него прежние нельзя.
        self._keyboards: OrderedDict[int, Buttons] = OrderedDict()

    # --- дорожки ---

    def kinds(self, lane: str) -> tuple[str, ...]:
        if lane == "llm":
            return LLM_KINDS if self.llm is not None else ()
        try:
            return BOT_KINDS if self.bot is not None and self.bot.ready() else ()
        except Exception:
            return ()

    def wake(self, lane: str = "bot") -> None:
        """Просит дорожку проверить очередь сейчас, а не через паузу (после нажатия кнопки)."""
        event = self._wake.get(lane)
        if event is not None:
            event.set()

    async def run_lane(self, lane: str) -> None:
        """Бесконечный цикл дорожки. Останавливается отменой задачи."""
        self._wake[lane] = asyncio.Event()
        failures = 0
        while True:
            handled = 0
            try:
                handled = await self.poll(lane)
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # сервис недоступен или ответил неожиданно — пауза с нарастанием
                failures += 1
                if failures == 1 or failures % 20 == 0:
                    logger.warning("shturman: очередь заданий недоступна (%s)", type(exc).__name__)
            if handled:
                continue          # была работа — сразу проверяем очередь снова
            pause = self.idle if not failures else min(MAX_BACKOFF, self.idle * 2 ** min(failures, 6))
            await self._pause(lane, pause)

    async def _pause(self, lane: str, seconds: float) -> None:
        event = self._wake[lane]
        try:
            await asyncio.wait_for(event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        event.clear()

    async def poll(self, lane: str) -> int:
        """Один заход: забрать задания дорожки и выполнить. Возвращает число забранных."""
        kinds = self.kinds(lane)
        if not kinds:
            return 0
        limit = self.llm_slots if lane == "llm" else 1
        try:
            out = await self.call("POST", "/api/jobs/claim",
                                  {"kinds": list(kinds), "limit": limit, "worker": self.worker})
        except ServiceUnavailable:
            self.stats.seen(False)
            raise
        self.stats.seen(True)
        jobs = [j for j in (out.get("jobs") or []) if isinstance(j, dict)]
        if lane == "llm":
            await asyncio.gather(*(self._handle_safely(job) for job in jobs))
        else:
            for job in jobs:
                await self._handle_safely(job)
        return len(jobs)

    async def _handle_safely(self, job: Mapping[str, Any]) -> None:
        try:
            await self.handle(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — одно задание не должно ронять дорожку
            logger.warning("shturman: сбой при обработке задания (%s)", type(exc).__name__)

    async def handle(self, job: Mapping[str, Any]) -> None:
        job_id, kind = _int(job.get("id")), job.get("kind")
        if job_id is None or not isinstance(kind, str):
            return
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        attempt = _int(job.get("attempt")) or 1
        outcome = await self.execute(kind, payload, attempt)
        self.stats.job_finished(outcome.ok)
        if not outcome.ok:
            if kind == BUSINESS_SEND and not outcome.error.startswith(NOT_SENT_PREFIX):
                self.stats.bump("sends_unknown")
            logger.warning("shturman: задание вида %s не выполнено", kind)
        await self.report(job_id, outcome)

    async def report(self, job_id: int, outcome: Outcome) -> bool:
        """Сообщает итог сервису. Выполнение задания при этом не повторяется никогда."""
        if outcome.ok:
            path, body = f"/api/jobs/{job_id}/complete", {"result": outcome.result}
        else:
            # retry_in передаётся всегда: без поля сервис назначил бы повтор сам.
            path, body = f"/api/jobs/{job_id}/fail", {"error": outcome.error[:1900], "retry_in": outcome.retry_in}
        for delay in REPORT_DELAYS:
            if delay:
                await self._sleep(delay)
            try:
                await self.call("POST", path, body)
                self.stats.seen(True)
                return True
            except ServiceUnavailable:
                self.stats.seen(False)
            except ServiceError:
                break             # сервис задание уже закрыл (например, вышла аренда)
        self.stats.bump("reports_lost")
        logger.warning("shturman: итог задания не удалось сообщить сервису")
        return False

    # --- выполнение ---

    async def execute(self, kind: str, payload: Mapping[str, Any], attempt: int = 1) -> Outcome:
        """Выполняет одно задание. Исключений наружу не выпускает (кроме отмены)."""
        if kind == BUSINESS_SEND:
            return await self._business_send(payload)
        handler = {LLM_STRUCTURED: self._llm_structured, LLM_TEXT: self._llm_text,
                   NOTIFY_OWNER: self._notify_owner, NOTIFY_EDIT: self._notify_edit}.get(kind)
        if handler is None:
            return Outcome(error="исполнитель не знает такого вида заданий", retry_in=None)
        try:
            return Outcome(result=await handler(payload, attempt))
        except asyncio.CancelledError:
            raise
        except _Fail as exc:
            return Outcome(error=exc.error, retry_in=exc.retry_in)
        except Exception as exc:  # noqa: BLE001 — любая ошибка становится итогом задания
            return Outcome(error=_brief(exc), retry_in=self._backoff(attempt))

    @staticmethod
    def _backoff(attempt: int) -> int:
        return int(min(600, 30 * 2 ** max(0, attempt - 1)))

    # --- модель ---

    def _llm_args(self, kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self.llm is None:
            raise _Fail("у плагина нет доступа к модели Hermes", 300)
        task = payload.get("task")
        if task not in self.tasks:
            # Сервис не может направить запрос в чужую или встроенную задачу Hermes.
            task = DEFAULT_TASK[kind]
        tokens = _int(payload.get("max_tokens")) or DEFAULT_MAX_TOKENS[kind]
        return {"task": task, "max_tokens": max(1, min(tokens, MAX_TOKENS_CAP)),
                "timeout": self.llm_timeout, "purpose": f"shturman.{kind}"}

    async def _llm_structured(self, payload: Mapping[str, Any], attempt: int) -> dict[str, Any]:
        instructions, text = _text(payload, "instructions"), _text(payload, "input")
        schema = payload.get("json_schema") if isinstance(payload.get("json_schema"), dict) else None
        name = payload.get("schema_name") if isinstance(payload.get("schema_name"), str) else None
        args = self._llm_args(LLM_STRUCTURED, payload)
        # Hermes ждёт входные данные списком блоков, а не строкой (agent/plugin_llm.py:239-262).
        result = await asyncio.wait_for(
            self.llm.acomplete_structured(
                instructions=instructions, input=[{"type": "text", "text": text}],
                json_schema=schema, json_mode=schema is None, schema_name=name, **args),
            timeout=self.llm_timeout + 15)
        parsed = getattr(result, "parsed", None)
        return {"parsed": parsed if isinstance(parsed, (dict, list)) else None,
                "text": str(getattr(result, "text", "") or ""), "model": str(getattr(result, "model", "") or "")}

    async def _llm_text(self, payload: Mapping[str, Any], attempt: int) -> dict[str, Any]:
        raw = payload.get("messages")
        if not isinstance(raw, list) or not raw:
            raise _Fail("в задании нет поля messages", None)
        messages = []
        for item in raw:
            role = item.get("role") if isinstance(item, dict) else None
            content = item.get("content") if isinstance(item, dict) else None
            if role not in ("system", "user", "assistant") or not isinstance(content, str):
                raise _Fail("поле messages задано неверно", None)
            messages.append({"role": role, "content": content})
        result = await asyncio.wait_for(
            self.llm.acomplete(messages, **self._llm_args(LLM_TEXT, payload)), timeout=self.llm_timeout + 15)
        return {"text": str(getattr(result, "text", "") or ""), "model": str(getattr(result, "model", "") or "")}

    # --- сообщения владельцу ---

    def _owner_chat(self) -> int:
        try:
            chat_id = _int((self.owner() or {}).get("chat_id"))
        except Exception:
            chat_id = None
        if chat_id is None:
            raise _Fail("владелец не привязан к боту", 300)     # может привязаться позже
        return chat_id

    def _remember(self, message_id: int, buttons: Buttons) -> None:
        self._keyboards[message_id] = buttons
        self._keyboards.move_to_end(message_id)
        while len(self._keyboards) > KEYBOARDS_REMEMBERED:
            self._keyboards.popitem(last=False)

    async def _notify_owner(self, payload: Mapping[str, Any], attempt: int) -> dict[str, Any]:
        text = _text(payload)[:TEXT_LIMIT]
        try:
            buttons = parse_buttons(payload.get("buttons"))
        except ValueError as exc:
            raise _Fail(str(exc), None) from None
        chat_id = self._owner_chat()
        if not self.bot.ready():
            raise _Fail("бот не подключён", 30)
        try:
            message_id = await asyncio.wait_for(
                self.bot.send_owner(chat_id, text, buttons, bool(payload.get("silent"))),
                timeout=self.bot_timeout)
        except NotSent as exc:
            raise _Fail(f"Telegram отказал: {exc.reason}", (exc.retry_after or 299) + 1) from None
        if buttons and _int(message_id) is not None:
            self._remember(message_id, buttons)
        return {"message_id": message_id}

    async def _notify_edit(self, payload: Mapping[str, Any], attempt: int) -> dict[str, Any]:
        message_id = _int(payload.get("message_id"))
        if message_id is None:
            raise _Fail("в задании нет поля message_id", None)
        text = _text(payload)[:TEXT_LIMIT]
        buttons = None
        if payload.get("remove_buttons") is False:
            buttons = self._keyboards.get(message_id)
            if buttons is None:
                # Не делаем молча другое: без клавиатуры в запросе Telegram снял бы кнопки.
                raise _Fail("keep_buttons_unavailable: кнопки этого сообщения исполнителю неизвестны", None)
        chat_id = self._owner_chat()
        if not self.bot.ready():
            raise _Fail("бот не подключён", 30)
        try:
            await asyncio.wait_for(self.bot.edit_owner(chat_id, message_id, text, buttons),
                                   timeout=self.bot_timeout)
        except NotSent as exc:
            raise _Fail(f"Telegram отказал: {exc.reason}",
                        exc.retry_after + 1 if exc.retry_after else None) from None
        if buttons is None:
            self._keyboards.pop(message_id, None)
        return {}

    # --- отправка от имени владельца ---

    async def _business_send(self, payload: Mapping[str, Any]) -> Outcome:
        """Одна попытка, без повторов. См. правило в начале файла."""
        def not_sent(reason: str) -> Outcome:
            return Outcome(error=f"{NOT_SENT_PREFIX} {reason}", retry_in=None)

        connection_id, chat_id, text = payload.get("business_connection_id"), _int(payload.get("chat_id")), payload.get("text")
        reply_to = payload.get("reply_to_message_id")
        if not isinstance(connection_id, str) or not connection_id or chat_id is None:
            return not_sent("в задании нет подключения или чата")
        if not isinstance(text, str) or not text.strip() or len(text) > TEXT_LIMIT:
            return not_sent("текст пуст или длиннее одного сообщения Telegram")
        if reply_to is not None and (_int(reply_to) is None or reply_to <= 0):
            return not_sent("неверный номер сообщения, на которое нужно ответить")
        try:
            ready = self.bot is not None and self.bot.ready()
        except Exception:
            ready = False
        if not ready:
            return not_sent("бот не подключён к Telegram")
        try:
            message_id = await asyncio.wait_for(
                self.bot.send_business(connection_id, chat_id, text, reply_to), timeout=self.bot_timeout)
        except asyncio.CancelledError:
            raise                  # шлюз останавливается; сервис сам отметит «исход неизвестен»
        except NotSent as exc:
            return not_sent(exc.reason)
        except Exception as exc:  # noqa: BLE001 — запрос мог уйти: исход неизвестен, повтора нет
            return Outcome(error=f"исход неизвестен: {type(exc).__name__}", retry_in=None)
        return Outcome(result={"message_id": message_id})
