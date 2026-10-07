"""Исполнитель заданий внутри сервиса: забирает из очереди задания с исполнителем `builtin`.

Делает то же, что исполнитель плагина в Hermes (`plugins/shturman/shturman_core/executor.py`),
задание в задание, только бот и модель здесь свои:

  notify.owner, notify.edit — сообщение владельцу в бота согласований и его правка;
  business.send             — отправка от имени владельца через бизнес-подключение этого бота;
  llm.structured, llm.text  — обращение к своей модели сервиса.

Две дорожки, чтобы долгий ответ модели не задерживал нажатие владельца «Отправить»: дорожка бота
выполняет задания по одному, дорожка модели — до двух сразу.

Главное правило — отправка от имени владельца не повторяется никогда. Итог `business.send`
бывает только трёх видов:
  * ушло — возвращается номер сообщения;
  * Telegram точно отказал (ответ 4xx) либо запрос не покидал сервер — ошибка начинается
    с «not_sent:»;
  * всё остальное (истекло время, оборвалась связь, ошибка 5xx) — исход неизвестен: сообщение
    могло уйти.
Повтор (`retry_in`) для этого вида всегда пуст, и запрос к Telegram ровно один.

В ошибку задания (`jobs.error`) и в журнал попадают только вид задания и код ошибки: ни текста
сообщений, ни ответа модели, ни токена.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter, OrderedDict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from .. import bridge, jobs
from ..app import AppState
from . import binding
from .bot import Bot
from .botapi import BotApi, NeverLeft, OutcomeUnknown, Refused, keyboard
from .llm import LlmClient, LlmError, matches_schema, parse_json, structured_messages

logger = logging.getLogger("shturman.executor.worker")

WORKER = "builtin"
BOT_KINDS = (bridge.NOTIFY_OWNER, bridge.NOTIFY_EDIT, bridge.BUSINESS_SEND)
LLM_KINDS = (bridge.LLM_STRUCTURED, bridge.LLM_TEXT)
MAX_BUTTON_ROWS = 20
MAX_BUTTONS_IN_ROW = 8
KEYBOARDS_REMEMBERED = 500
DEFAULT_MAX_TOKENS = {bridge.LLM_STRUCTURED: 2000, bridge.LLM_TEXT: 1500}
MAX_TOKENS_CAP = 8000
IDLE_SECONDS = 1.0
MAX_BACKOFF = 60.0
REPORT_DELAYS = (0, 1, 2, 4, 8, 15)      # секунд перед попытками записать итог

Buttons = list[list[tuple[str, str]]]


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
    """Кнопки задания как строки пар (подпись, данные).

    Принимаются только кнопки сервиса (данные начинаются с «sh:»): только их нажатие бот
    передаёт на разбор.
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
            if (not data.startswith(bridge.CALLBACK_PREFIX)
                    or len(data.encode("utf-8")) > bridge.CALLBACK_DATA_LIMIT):
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


def _no_nul(value: Any) -> Any:
    """Убирает нулевой знак: в jsonb его записать нельзя, а в ответе модели он встречается."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [_no_nul(v) for v in value]
    if isinstance(value, dict):
        return {_no_nul(k): _no_nul(v) for k, v in value.items()}
    return value


def _backoff(attempt: int) -> int:
    return int(min(600, 30 * 2 ** max(0, attempt - 1)))


class Worker:
    """api и bot — бот согласований (или None); llm — своя модель (или None)."""

    def __init__(
        self, state: AppState, *, api: BotApi | None = None, bot: Bot | None = None,
        llm: LlmClient | None = None, idle: float = IDLE_SECONDS, bot_timeout: float = 30.0,
        llm_timeout: float = 240.0, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.state, self.api, self.bot, self.llm = state, api, bot, llm
        self.idle, self.bot_timeout, self.llm_timeout = idle, bot_timeout, llm_timeout
        self._sleep = sleep
        self._wake: dict[str, asyncio.Event] = {}
        self.done: Counter[str] = Counter()
        self.failed: Counter[str] = Counter()
        self.counters: Counter[str] = Counter()
        # Клавиатуры отправленных сообщений: Telegram снимает кнопки при правке текста,
        # если их не передать заново, а спросить у него прежние нельзя.
        self._keyboards: OrderedDict[int, Buttons] = OrderedDict()

    # --- дорожки ---

    def kinds(self, lane: str) -> tuple[str, ...]:
        if lane == "llm":
            return LLM_KINDS if self.llm is not None else ()
        return BOT_KINDS if self.api is not None else ()

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
                handled = await self.run_once(lane)
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — база недоступна: пауза с нарастанием
                failures += 1
                if failures == 1 or failures % 20 == 0:
                    logger.warning("исполнитель: очередь заданий недоступна (%s)", type(exc).__name__)
            if handled:
                continue
            pause = self.idle if not failures else min(MAX_BACKOFF, self.idle * 2 ** min(failures, 6))
            event = self._wake[lane]
            try:
                await asyncio.wait_for(event.wait(), timeout=pause)
            except asyncio.TimeoutError:
                pass
            event.clear()

    async def run_once(self, lane: str) -> int:
        """Один заход: забрать задания дорожки и выполнить. Возвращает число забранных."""
        kinds = self.kinds(lane)
        if not kinds:
            return 0
        async with self.state.pool.acquire() as conn:
            claimed = await jobs.claim(conn, kinds, worker=WORKER, limit=2 if lane == "llm" else 1,
                                       executor="builtin")
        if lane == "llm":
            await asyncio.gather(*(self._handle_safely(job) for job in claimed))
        else:
            for job in claimed:
                await self._handle_safely(job)
        return len(claimed)

    async def _handle_safely(self, job: Mapping[str, Any]) -> None:
        try:
            await self.handle(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — одно задание не должно ронять дорожку
            logger.warning("исполнитель: сбой при обработке задания (%s)", type(exc).__name__)

    async def handle(self, job: Mapping[str, Any]) -> None:
        job_id, kind = int(job["id"]), str(job["kind"])
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        outcome = await self.execute(kind, payload, _int(job.get("attempt")) or 1)
        (self.done if outcome.ok else self.failed)[kind] += 1
        if not outcome.ok:
            if kind == bridge.BUSINESS_SEND and not outcome.error.startswith(bridge.NOT_SENT_PREFIX):
                self.counters["sends_unknown"] += 1
            logger.warning("исполнитель: задание вида %s не выполнено", kind)
        await self.report(job_id, outcome)

    async def report(self, job_id: int, outcome: Outcome) -> bool:
        """Записывает итог. Выполнение задания при этом не повторяется никогда."""
        for delay in REPORT_DELAYS:
            if delay:
                await self._sleep(delay)
            try:
                async with self.state.pool.acquire() as conn:
                    if outcome.ok:
                        await bridge.deliver_result(conn, job_id, outcome.result or {})
                    else:
                        await bridge.deliver_failure(conn, job_id, outcome.error[:1900], retry_in=outcome.retry_in)
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — база недоступна либо разбор итога упал
                logger.warning("исполнитель: итог задания не записан (%s)", type(exc).__name__)
        self.counters["reports_lost"] += 1
        return False

    # --- выполнение ---

    async def execute(self, kind: str, payload: Mapping[str, Any], attempt: int = 1) -> Outcome:
        """Выполняет одно задание. Исключений наружу не выпускает (кроме отмены)."""
        if kind == bridge.BUSINESS_SEND:
            return await self._business_send(payload)
        handler = {bridge.LLM_STRUCTURED: self._llm_structured, bridge.LLM_TEXT: self._llm_text,
                   bridge.NOTIFY_OWNER: self._notify_owner, bridge.NOTIFY_EDIT: self._notify_edit}.get(kind)
        if handler is None:
            return Outcome(error="исполнитель не знает такого вида заданий", retry_in=None)
        try:
            return Outcome(result=await handler(payload, attempt))
        except asyncio.CancelledError:
            raise
        except _Fail as exc:
            return Outcome(error=exc.error, retry_in=exc.retry_in)
        except Exception as exc:  # noqa: BLE001 — только вид ошибки: в тексте бывают куски ответа модели
            return Outcome(error=type(exc).__name__, retry_in=_backoff(attempt))

    # --- модель ---

    def _max_tokens(self, kind: str, payload: Mapping[str, Any]) -> int:
        tokens = _int(payload.get("max_tokens")) or DEFAULT_MAX_TOKENS[kind]
        return max(1, min(tokens, MAX_TOKENS_CAP))

    async def _ask(self, kind: str, payload: Mapping[str, Any], messages: list[dict[str, str]],
                   attempt: int, *, json_mode: bool) -> tuple[str, str]:
        if self.llm is None:
            raise _Fail("у сервиса нет своего доступа к модели", 300)
        try:
            return await asyncio.wait_for(
                self.llm.chat(messages, task=payload.get("task"), max_tokens=self._max_tokens(kind, payload),
                              json_mode=json_mode),
                timeout=self.llm_timeout)
        except LlmError as exc:
            raise _Fail(f"модель: {exc.code}", None if exc.final else _backoff(attempt)) from None
        except asyncio.TimeoutError:
            raise _Fail("модель: timeout", _backoff(attempt)) from None

    async def _llm_structured(self, payload: Mapping[str, Any], attempt: int) -> dict[str, Any]:
        instructions, text = _text(payload, "instructions"), _text(payload, "input")
        schema = payload.get("json_schema") if isinstance(payload.get("json_schema"), dict) else None
        name = payload.get("schema_name") if isinstance(payload.get("schema_name"), str) else None
        answer, model = await self._ask(
            bridge.LLM_STRUCTURED, payload, structured_messages(instructions, text, schema, name),
            attempt, json_mode=True)
        parsed = parse_json(answer)
        # Расхождение со схемой и неразборчивый JSON — не сбой задания: сервис проверяет ответ сам.
        valid = parsed is not None and (schema is None or matches_schema(parsed, schema))
        return _no_nul({"parsed": parsed, "text": answer, "model": model, "schema_valid": valid})

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
        answer, model = await self._ask(bridge.LLM_TEXT, payload, messages, attempt, json_mode=False)
        return _no_nul({"text": answer, "model": model})

    # --- сообщения владельцу ---

    async def _owner_chat(self) -> int:
        if self.bot is None or self.bot.bot_id is None:
            raise _Fail("бот согласований ещё не подключён к Telegram", 30)
        async with self.state.pool.acquire() as conn:
            owner = await binding.bound_owner(conn, self.bot.bot_id)
        if owner is None:
            raise _Fail("владелец не привязан к боту согласований", 300)     # может привязаться позже
        return owner["chat_id"]

    @staticmethod
    def _notify_fail(exc: Exception, attempt: int) -> _Fail:
        """Когда повторить сообщение владельцу. Ответ Telegram с кодом 4xx («бот заблокирован»,
        «чат не найден») окончателен: повтор дал бы тот же отказ. Повторяем, только если
        Telegram просит подождать либо запрос до него не дошёл."""
        if isinstance(exc, Refused):
            return _Fail(f"Telegram отказал: {exc}", int(exc.retry_after) + 1 if exc.retry_after else None)
        return _Fail(f"нет связи с Telegram: {exc}", _backoff(attempt))

    def _remember(self, message_id: int, buttons: Buttons) -> None:
        self._keyboards[message_id] = buttons
        self._keyboards.move_to_end(message_id)
        while len(self._keyboards) > KEYBOARDS_REMEMBERED:
            self._keyboards.popitem(last=False)

    async def _notify_owner(self, payload: Mapping[str, Any], attempt: int) -> dict[str, Any]:
        text = bridge.fit_message(_text(payload))
        try:
            buttons = parse_buttons(payload.get("buttons"))
        except ValueError as exc:
            raise _Fail(str(exc), None) from None
        chat_id = await self._owner_chat()
        try:
            message_id = await asyncio.wait_for(
                self.api.send_message(chat_id, text, buttons=buttons, silent=bool(payload.get("silent")),
                                      no_preview=True),
                timeout=self.bot_timeout)
        except (Refused, NeverLeft) as exc:
            raise self._notify_fail(exc, attempt) from None
        if buttons:
            self._remember(message_id, buttons)
        return {"message_id": message_id}

    async def _notify_edit(self, payload: Mapping[str, Any], attempt: int) -> dict[str, Any]:
        message_id = _int(payload.get("message_id"))
        if message_id is None:
            raise _Fail("в задании нет поля message_id", None)
        text = bridge.fit_message(_text(payload))
        buttons = None
        if payload.get("remove_buttons") is False:
            buttons = self._keyboards.get(message_id)
            if buttons is None:
                # Не делаем молча другое: без клавиатуры в запросе Telegram снял бы кнопки.
                raise _Fail("keep_buttons_unavailable: кнопки этого сообщения исполнителю неизвестны", None)
        chat_id = await self._owner_chat()
        try:
            await asyncio.wait_for(
                self.api.edit_message_text(chat_id, message_id, text, reply_markup=keyboard(buttons)),
                timeout=self.bot_timeout)
        except Refused as exc:
            # Текст уже такой либо сообщения больше нет — править нечего.
            if exc.reason not in ("not_modified", "message_not_found"):
                raise self._notify_fail(exc, attempt) from None
        except NeverLeft as exc:
            raise self._notify_fail(exc, attempt) from None
        if buttons is None:
            self._keyboards.pop(message_id, None)
        return {}

    # --- отправка от имени владельца ---

    async def _business_send(self, payload: Mapping[str, Any]) -> Outcome:
        """Одна попытка, один запрос к Telegram, без повторов. См. правило в начале файла."""
        def not_sent(reason: str) -> Outcome:
            return Outcome(error=f"{bridge.NOT_SENT_PREFIX} {reason}", retry_in=None)

        connection_id, chat_id = payload.get("business_connection_id"), _int(payload.get("chat_id"))
        text, reply_to = payload.get("text"), payload.get("reply_to_message_id")
        if not isinstance(connection_id, str) or not connection_id or chat_id is None:
            return not_sent("в задании нет подключения или чата")
        # Текст, согласованный владельцем, не обрезается: не помещается — не отправляется.
        if not isinstance(text, str) or not text.strip() or bridge.utf16_len(text) > bridge.MESSAGE_LIMIT:
            return not_sent("текст пуст или длиннее одного сообщения Telegram")
        if reply_to is not None and (_int(reply_to) is None or reply_to <= 0):
            return not_sent("неверный номер сообщения, на которое нужно ответить")
        if self.api is None:
            return not_sent("бот согласований не подключён")
        if self.state.config.sending is not True:
            # Последняя проверка выключателя — прямо перед обращением к Telegram.
            return not_sent("отправка выключена")
        try:
            async with self.state.pool.acquire() as conn:
                link = await conn.fetchrow(
                    "SELECT via, enabled, can_reply FROM business_connections WHERE id = $1", connection_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — до Telegram дело не дошло
            return not_sent("подключение не удалось проверить")
        if link is None or link["via"] != "service" or not link["enabled"] or not link["can_reply"]:
            return not_sent("подключение не принадлежит боту согласований, выключено или без права отвечать")
        try:
            message_id = await asyncio.wait_for(
                self.api.send_message(chat_id, text, business_connection_id=connection_id, reply_to=reply_to),
                timeout=self.bot_timeout)
        except asyncio.CancelledError:
            raise                  # сервис останавливается; уборка сама отметит «исход неизвестен»
        except (Refused, NeverLeft) as exc:
            return not_sent(str(exc))
        except Exception as exc:  # noqa: BLE001 — запрос мог уйти: исход неизвестен, повтора нет
            kind = exc.kind if isinstance(exc, OutcomeUnknown) else type(exc).__name__
            return Outcome(error=f"исход неизвестен: {kind}", retry_in=None)
        return Outcome(result={"message_id": message_id})
