"""Своя модель сервиса по подписке ChatGPT: Responses API OpenAI с токеном входа через ChatGPT.

Вид вызова — тот же, что у клиента по ключу API (`llm.py`): `chat`, `ready`, `model_for`,
счётчики. Исполнитель заданий (`worker.py`) работает с любым из двух.

Требования предварительной версии (developers.openai.com/siwc/token-sharing-open-source,
«Preview limitations» и «Models and inference», сверено 8 октября 2026; на настоящем сервисе
OpenAI клиент не запускался):
  * только `POST https://api.openai.com/v1/responses`, `Authorization: Bearer <access_token>`;
  * в каждом запросе `store: false` и `stream: true`; ответ читается потоком (SSE) до события
    `response.completed` — только оно означает успех;
  * `input` — массив; указания — полем `instructions`: сообщения с ролью system отвергаются;
  * не передаются `max_output_tokens`, `temperature`, `top_p`, `metadata`, `user`, `truncation`
    и прочие поля из перечня неподдерживаемых. Поэтому предел длины ответа задания здесь не
    действует.

«JSON по схеме». Сначала — `text.format` вида `json_schema` (имя, схема, `strict: true`). Если
этот путь отвечает 400 `subscription_sharing_unsupported_capability` с `param` про `text`/`format`,
запрос повторяется без поля: требование вернуть JSON и сама схема уже есть в тексте указаний
(`llm.structured_messages`), а ответ сервис разбирает сам (`llm.parse_json`). Отказ запоминается
для модели до перезапуска сервиса. Схема, которую строгий режим не принимает (400 с `param`
`text.format…`), запоминается так же — для этой модели и этого имени схемы.

Отказы (раздел «Errors and recovery»):
  * 429 `subscription_sharing_usage_limit_exceeded` (в том числе событием `response.failed`
    посреди потока) — лимит подписки исчерпан: новые обращения на паузе (`LIMIT_PAUSE`), задание
    возвращается в очередь, не тратя попыток;
  * 401 — токен обновляется один раз и запрос повторяется; снова 401 либо отказ в обновлении —
    «нужно войти заново»;
  * 403 (`subscription_sharing_user_not_eligible`, ограничение по стране и прочие) — подписка
    не может обслуживать сервис: задание не выполняется, новые обращения час не идут, вход не
    повторяется;
  * 503 и 5xx — повтор с нарастающей паузой, учётные данные не трогаются.
В журнал попадают только код ответа, код ошибки и номер запроса OpenAI (`x-request-id`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Awaitable, Callable, Mapping

import httpx

from . import siwc
from .llm import RETRY_DELAYS, Attachment, LlmError, _retry_after, check_attachments

logger = logging.getLogger("shturman.executor.chatgpt")

LIMIT_PAUSE = 1800        # секунд паузы после «лимит подписки исчерпан»
DENIED_PAUSE = 3600       # после отказа в доступе (403) — столько не обращаемся вовсе
RELOGIN_PAUSE = 1800      # задание ждёт столько, пока владелец не войдёт заново
STREAM_LIMIT = 4_000_000  # знаков ответа; больше — обрыв как при сбое

OK, LIMIT, DENIED, RELOGIN = "ok", "limit", "denied", "relogin"

_CONNECT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
_NAME = re.compile(r"[^A-Za-z0-9_-]")

# Модели, у которых путь подписки не принял text.format, и пары (модель, имя схемы), схему
# которых не принял строгий режим. До перезапуска сервиса — общие для всех клиентов.
_NO_FORMAT: set[str] = set()
_NO_SCHEMA: set[tuple[str, str]] = set()


class _Retry(Exception):
    def __init__(self, code: str, wait: float = 0.0) -> None:
        super().__init__(code)
        self.code, self.wait = code, wait


def _error_of(data: Any) -> tuple[str, str]:
    """(код, param) из ответа об ошибке: `{"error": {"code", "param"}}` либо `{"detail": …}`."""
    if not isinstance(data, dict):
        return "", ""
    error = data.get("error")
    if isinstance(error, dict):
        return siwc.safe_code(error.get("code") or error.get("type")), str(error.get("param") or "")[:200]
    return siwc.safe_code(data.get("code")), ""


def _content(item: dict[str, Any]) -> str:
    content = item.get("content")
    return content if isinstance(content, str) else ""


def responses_input(messages: list[dict[str, Any]], attachments: list[Attachment]) -> tuple[str, list[dict[str, Any]]]:
    """Сообщения Chat Completions → (instructions, input) для Responses API.

    Сообщения system собираются в `instructions` (на этом пути system в `input` отвергается),
    вложения — частями `input_image` и `input_file` у последнего сообщения владельца."""
    instructions = "\n\n".join(_content(m) for m in messages if m.get("role") == "system" and _content(m))
    items: list[dict[str, Any]] = [{"role": m["role"], "content": _content(m)}
                                   for m in messages if m.get("role") in ("user", "assistant")]
    if attachments:
        index = next((i for i in range(len(items) - 1, -1, -1) if items[i]["role"] == "user"), None)
        if index is None:
            items.append({"role": "user", "content": ""})
            index = len(items) - 1
        parts: list[dict[str, Any]] = []
        if items[index]["content"]:
            parts.append({"type": "input_text", "text": items[index]["content"]})
        for item in attachments:
            if item.kind == "image":
                parts.append({"type": "input_image", "image_url": item.data_url(), "detail": "auto"})
            else:
                parts.append({"type": "input_file", "filename": item.name or "file", "file_data": item.data_url()})
        items[index] = {"role": "user", "content": parts}
    return instructions, items


def _output_text(response: Any) -> str | None:
    """Текст ответа из итогового объекта `response` (события response.completed)."""
    if not isinstance(response, dict) or not isinstance(response.get("output"), list):
        return None
    chunks: list[str] = []
    for item in response["output"]:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                chunks.append(part["text"])
    return "".join(chunks) if chunks else None


class ChatGptClient:
    """Обращения к модели по подписке ChatGPT. `transport` — для тестов: подставной сервер."""

    def __init__(
        self, keeper: siwc.TokenKeeper, *, model: str, models: Mapping[str, str] | None = None,
        proxy_url: str = "", transport: httpx.AsyncBaseTransport | None = None, timeout: float = 90.0,
        slots: int = 2, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.keeper = keeper
        self.model = model
        self.models = dict(models or {})
        self.broken: str | None = None
        self.calls = 0
        self.failures = 0
        self.last_ok: bool | None = None
        self.last_error: str | None = None
        self.state = OK
        self.denied_code = ""
        self._hold_until = 0.0
        self._sleep, self._clock = sleep, clock
        self._slots = asyncio.Semaphore(max(1, min(int(slots), 2)))
        self._client: httpx.AsyncClient | None = None
        try:
            self._client = siwc.http_client(proxy_url=proxy_url, transport=transport, timeout=timeout,
                                            base_url=siwc.API_BASE)
        except siwc.SiwcError as exc:
            self.broken = exc.code
        if not model:
            self.broken = self.broken or "no_model"

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    def model_for(self, task: Any) -> str:
        return self.models.get(task, self.model) if isinstance(task, str) else self.model

    # --- пауза ---

    def status(self) -> str:
        """ok | limit (лимит подписки исчерпан) | denied (нет доступа) | relogin (войти заново)."""
        if self.state == RELOGIN:
            return RELOGIN
        if self.state in (LIMIT, DENIED) and self._clock() < self._hold_until:
            return self.state
        return OK

    def ready(self) -> bool:
        """Можно ли сейчас брать задания: не на паузе и вход действует."""
        return self._client is not None and not self.broken and self.status() == OK

    def _hold(self, state: str, seconds: float) -> None:
        self.state = state
        self._hold_until = self._clock() + seconds

    def _remaining(self) -> int:
        return max(60, int(self._hold_until - self._clock()))

    def _paused_error(self) -> LlmError:
        status = self.status()
        if status == LIMIT:
            return LlmError("usage_limit", pause=self._remaining())
        if status == DENIED:
            return LlmError(f"denied:{self.denied_code or 'forbidden'}", final=True)
        return LlmError("relogin", pause=RELOGIN_PAUSE)

    # --- обращение ---

    async def chat(self, messages: list[dict[str, str]], *, task: Any = None, max_tokens: int = 0,
                   json_mode: bool = False, schema: dict[str, Any] | None = None, schema_name: str | None = None,
                   attachments: list[Attachment] | None = None) -> tuple[str, str]:
        """Один ответ модели: (текст, имя модели). Бросает `LlmError`. max_tokens не передаётся:
        на этом пути предел длины ответа не поддерживается."""
        async with self._slots:
            self.calls += 1
            try:
                files = check_attachments(attachments)
                out = await self._chat(messages, self.model_for(task), files, json_mode, schema, schema_name)
            except LlmError as exc:
                self.failures += 1
                self.last_ok, self.last_error = False, exc.code
                raise
            self.last_ok, self.last_error = True, None
            if self.state != RELOGIN:
                self.state = OK
            return out

    def _format(self, model: str, json_mode: bool, schema: Any, schema_name: str | None) -> dict[str, Any] | None:
        if not json_mode or not isinstance(schema, dict) or model in _NO_FORMAT:
            return None
        name = _NAME.sub("_", schema_name or "result")[:64] or "result"
        if (model, name) in _NO_SCHEMA:
            return None
        return {"format": {"type": "json_schema", "name": name, "schema": schema, "strict": True}}

    async def _chat(self, messages: list[dict[str, Any]], model: str, attachments: list[Attachment],
                    json_mode: bool, schema: Any, schema_name: str | None) -> tuple[str, str]:
        if self._client is None or self.broken:
            raise LlmError(self.broken or "not_configured")
        if self.status() != OK:
            raise self._paused_error()
        instructions, items = responses_input(messages, attachments)
        text_format = self._format(model, json_mode, schema, schema_name)
        attempt = 0
        refreshed = False
        failed_token: str | None = None
        while True:
            body: dict[str, Any] = {"model": model, "input": items, "store": False, "stream": True}
            if instructions:
                body["instructions"] = instructions
            if text_format is not None:
                body["text"] = text_format
            try:
                token = await self.keeper.access_token(failed=failed_token)
            except siwc.NeedLogin:
                self.state = RELOGIN
                raise LlmError("relogin", pause=RELOGIN_PAUSE) from None
            except siwc.SiwcError as exc:          # Transient: токены целы, повторим позже
                raise LlmError(exc.code) from None
            failed_token = None
            wait: float | None = None
            try:
                return await self._post(body, token, model)
            except _Retry as exc:
                code, wait = exc.code, exc.wait
            except _Unauthorized:
                if refreshed:
                    await self.keeper.mark_relogin()
                    self.state = RELOGIN
                    raise LlmError("relogin", pause=RELOGIN_PAUSE) from None
                refreshed, failed_token = True, token
                continue
            except _NoFormat as exc:
                if text_format is None:
                    raise LlmError("http_400:unsupported_capability", final=True) from None
                if exc.whole:
                    _NO_FORMAT.add(model)
                else:
                    _NO_SCHEMA.add((model, text_format["format"]["name"]))
                logger.info("подписка ChatGPT: «JSON по схеме» через text.format не принят, схема — в тексте")
                text_format = None
                continue
            except _CONNECT as exc:
                code, wait = f"connect:{type(exc).__name__}", 0.0
            except httpx.TimeoutException:
                raise LlmError("timeout") from None
            except httpx.HTTPError as exc:
                raise LlmError(f"network:{type(exc).__name__}") from None
            if attempt >= len(RETRY_DELAYS):
                raise LlmError(code)
            pause = max(RETRY_DELAYS[attempt], wait or 0.0)
            attempt += 1
            logger.info("подписка ChatGPT: повтор обращения (%s)", code.split(":")[0])
            await self._sleep(pause)

    async def _post(self, body: dict[str, Any], token: str, model: str) -> tuple[str, str]:
        assert self._client is not None
        headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
        async with self._client.stream("POST", "/responses", json=body, headers=headers) as response:
            rid = siwc.safe_code(response.headers.get("x-request-id"), 80) or "-"
            status = response.status_code
            if status == 200:
                return await self._read_stream(response, model, rid)
            try:
                raw = await response.aread()
                data = json.loads(raw[:200_000]) if raw else {}
            except (ValueError, httpx.HTTPError):
                data = {}
            code, param = _error_of(data)
            self._refused(status, code, param, rid, response, body)
        raise LlmError(f"http_{status}")      # недостижимо: _refused бросает всегда

    def _refused(self, status: int, code: str, param: str, rid: str, response: httpx.Response,
                 body: dict[str, Any]) -> None:
        """Отказ до начала потока. Бросает исключение всегда."""
        logger.warning("подписка ChatGPT: отказ %s %s (запрос %s)", status, code or "-", rid)
        lowered = param.lower()
        if status == 400 and "text" in body:
            if code == "subscription_sharing_unsupported_capability" and ("text" in lowered or "format" in lowered):
                raise _NoFormat(whole=True)
            if lowered.startswith("text.format") or lowered.startswith("text"):
                raise _NoFormat(whole=False)
        if status == 401:
            raise _Unauthorized()
        if status == 403:
            self.denied_code = code or "forbidden"
            self._hold(DENIED, DENIED_PAUSE)
            raise LlmError(f"denied:{self.denied_code}", final=True)
        if status == 429 and code == "subscription_sharing_usage_limit_exceeded":
            self._hold(LIMIT, LIMIT_PAUSE)
            raise LlmError("usage_limit", pause=LIMIT_PAUSE)
        if status == 429 or status >= 500:
            raise _Retry(f"http_{status}" + (f":{code}" if code else ""), _retry_after(response))
        raise LlmError(f"http_{status}" + (f":{code}" if code else ""), final=status != 408)

    async def _read_stream(self, response: httpx.Response, model: str, rid: str) -> tuple[str, str]:
        """Читает поток событий до `response.completed`. Иной конец — ошибка."""
        deltas: list[str] = []
        size = 0
        event_name = ""
        data_lines: list[str] = []
        result: list[tuple[str, str]] = []

        def dispatch() -> None:
            nonlocal size
            raw = "\n".join(data_lines)
            if not raw or raw == "[DONE]":
                return
            try:
                payload = json.loads(raw)
            except ValueError:
                return
            if not isinstance(payload, dict):
                return
            kind = payload.get("type") or event_name
            if kind == "response.output_text.delta" and isinstance(payload.get("delta"), str):
                deltas.append(payload["delta"])
                size += len(payload["delta"])
                if size > STREAM_LIMIT:
                    raise LlmError("too_long")
            elif kind == "response.completed":
                done = payload.get("response") if isinstance(payload.get("response"), dict) else {}
                text = _output_text(done)
                used = done.get("model") if isinstance(done.get("model"), str) and done.get("model") else model
                result.append((text if text is not None else "".join(deltas), used))
            elif kind in ("response.failed", "error"):
                failed = payload.get("response") if isinstance(payload.get("response"), dict) else {}
                error = failed.get("error") if isinstance(failed.get("error"), dict) else payload.get("error")
                if not isinstance(error, dict):
                    error = payload
                self._failed(siwc.safe_code(error.get("code") or error.get("type")), rid)
            elif kind == "response.incomplete":
                done = payload.get("response") if isinstance(payload.get("response"), dict) else {}
                details = done.get("incomplete_details") if isinstance(done.get("incomplete_details"), dict) else {}
                reason = siwc.safe_code(details.get("reason")) or "unknown"
                logger.warning("подписка ChatGPT: ответ не закончен (%s, запрос %s)", reason, rid)
                raise LlmError(f"incomplete:{reason}")

        async for line in response.aiter_lines():
            if result:
                break
            if line == "":
                dispatch()
                event_name, data_lines = "", []
                continue
            if line.startswith(":"):
                continue
            name, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if name == "event":
                event_name = value
            elif name == "data":
                data_lines.append(value)
        if not result and data_lines:
            dispatch()
        if not result:
            logger.warning("подписка ChatGPT: поток оборвался до response.completed (запрос %s)", rid)
            raise LlmError("stream_cut")
        return result[0]

    def _failed(self, code: str, rid: str) -> None:
        """Ошибка, пришедшая событием посреди потока. Бросает исключение всегда."""
        logger.warning("подписка ChatGPT: ответ не удался (%s, запрос %s)", code or "-", rid)
        if code == "subscription_sharing_usage_limit_exceeded":
            self._hold(LIMIT, LIMIT_PAUSE)
            raise LlmError("usage_limit", pause=LIMIT_PAUSE)
        if code in ("subscription_sharing_usage_unavailable", "subscription_sharing_user_unavailable",
                    "server_error", "rate_limit_exceeded"):
            raise _Retry(f"failed:{code}")
        if code == "subscription_sharing_user_not_eligible":
            self.denied_code = code
            self._hold(DENIED, DENIED_PAUSE)
            raise LlmError(f"denied:{code}", final=True)
        raise LlmError(f"failed:{code or 'unknown'}")


class _Unauthorized(Exception):
    pass


class _NoFormat(Exception):
    def __init__(self, *, whole: bool) -> None:
        super().__init__("no_format")
        self.whole = whole
