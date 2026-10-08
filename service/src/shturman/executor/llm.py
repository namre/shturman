# Основано на NousResearch/hermes-agent (MIT), agent/plugin_llm.py@f97608f
"""Свой доступ сервиса к модели: Chat Completions по API, совместимому с OpenAI. Только httpx.

Из `agent/plugin_llm.py` Hermes Agent (версия v2026.9.24) взяты порядок
сборки запроса «JSON по схеме» (указание в системном сообщении, имя схемы и сама схема в тексте
запроса) и разбор ответа (первый блок в тройных кавычках либо весь текст). Так ответы своей
модели сервиса имеют тот же вид, что и ответы, которые плагин получает через Hermes.

Адрес — `SHTURMAN_LLM_BASE_URL` (по умолчанию https://api.openai.com/v1), ключ —
`SHTURMAN_LLM_API_KEY`, модель — `SHTURMAN_LLM_MODEL`. Для отдельных задач модель можно
заменить: `SHTURMAN_LLM_MODEL_EXTRACT`, `SHTURMAN_LLM_MODEL_REPLY`, `SHTURMAN_LLM_MODEL_WATCH`.
OpenRouter и локальный сервер подключаются сменой адреса.

Как называется предел длины ответа. В API OpenAI параметр `max_tokens` объявлен устаревшим,
его замена — `max_completion_tokens`; модели с рассуждением (серия o, GPT-5) на `max_tokens`
отвечают 400: «Unsupported parameter: 'max_tokens' is not supported with this model. Use
'max_completion_tokens' instead.» (`param: "max_tokens"`, `code: "unsupported_parameter"`).
Остальные модели OpenAI принимают оба имени. Многие совместимые серверы знают только `max_tokens`,
а незнакомое имя молча пропускают — и тогда предел не действует вовсе. Отсюда порядок:
  * адрес api.openai.com — сначала `max_completion_tokens`;
  * любой другой адрес — сначала `max_tokens`;
  * на ответ 400 (или 422), в котором названо отправленное имя, запрос один раз повторяется
    с другим именем, и выбор запоминается для этой модели до перезапуска сервиса;
  * `SHTURMAN_LLM_TOKENS_PARAM` (`max_tokens` или `max_completion_tokens`) задаёт имя жёстко.
Сверено 7 октября 2026: заявление сотрудника OpenAI на community.openai.com (тема 938077:
«max_tokens continues to be supported in all existing models, but the o1 series only supports
max_completion_tokens») и дословный текст ошибки в github.com/simonw/llm/issues/724. Страницу
справочника platform.openai.com открыть не удалось. На настоящем API клиент не запускался.

Режим JSON — `response_format: {"type": "json_object"}`. Сервер, который такого поля не знает
и отвечает 400 с его именем, получает тот же запрос без него (требование вернуть JSON остаётся
в тексте); это тоже запоминается для модели.

Повторы внутри клиента — только при ответах 429 и 5xx и при ошибке соединения, с нарастающей
паузой. Истёкшее время и обрыв связи клиент не повторяет: задание вернётся в очередь.

Перенаправлениям клиент не следует никогда: ответ 3xx — отказ. Иначе ключ в заголовке ушёл бы
по адресу, который назвал чужой сервер.

Адрес, введённый на странице настройки (`restricted=True`), дополнительно проходит фильтр
`netguard`: только https, только адрес в интернете, соединение — с адресом, проверенным в
момент запроса. Адрес из окружения (`SHTURMAN_LLM_BASE_URL`) задаёт оператор, фильтра для него нет.

Тексты запросов и ответов в журнал и в ошибки не попадают: только код ответа и вид ошибки.

Вложения (`Attachment`: картинка или файл) прикладываются к последнему сообщению владельца:
картинка — частью `image_url` с адресом `data:`, файл — частью `file` (`filename`, `file_data`).
Какие файлы примет модель, решает провайдер; Chat Completions OpenAI принимает так только PDF.
Тот же вид вызова (`chat`, `ready`, `model_for`, счётчики) у клиента подписки ChatGPT
(`subscription.py`): исполнитель заданий работает с любым из двух.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import urlsplit

import httpx

from .. import netguard

logger = logging.getLogger("shturman.executor.llm")

TASK_ENV = {
    "shturman_extract": "SHTURMAN_LLM_MODEL_EXTRACT",
    "shturman_reply": "SHTURMAN_LLM_MODEL_REPLY",
    "shturman_watch": "SHTURMAN_LLM_MODEL_WATCH",
}
TOKEN_PARAMS = ("max_tokens", "max_completion_tokens")
JSON_ONLY = ("Respond with a single JSON object that matches the requested shape. "
             "Do not include prose or markdown fences.")
RETRY_DELAYS = (2.0, 5.0)         # паузы перед повторами внутри одного обращения
MAX_RETRY_AFTER = 30.0

_FENCE = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL | re.IGNORECASE)
_CONNECT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


class LlmError(Exception):
    """Обращение к модели не удалось. `code` — короткий код без текста запроса и ответа;
    `final` — повторять бессмысленно (неверный ключ, неизвестная модель, неверный запрос);
    `pause` — секунд, через которые задание стоит повторить, не расходуя его попыток (исчерпан
    лимит подписки, нужно войти заново): дело не в задании, а в доступе к модели."""

    def __init__(self, code: str, *, final: bool = False, pause: int | None = None) -> None:
        super().__init__(code)
        self.code, self.final, self.pause = code, final, pause


@dataclass(frozen=True)
class Attachment:
    """Вложение к сообщению владельца для модели: картинка или файл, целиком в памяти."""
    kind: str      # "image" | "file"
    mime: str
    name: str
    data: bytes

    def __repr__(self) -> str:      # содержимое и имя файла в журнал не попадают
        return f"<Attachment {self.kind} {self.mime} {len(self.data)} байт>"

    def data_url(self) -> str:
        return f"data:{self.mime};base64,{base64.b64encode(self.data).decode('ascii')}"


def check_attachments(attachments: list[Attachment] | None) -> list[Attachment]:
    out = list(attachments or [])
    for item in out:
        if not isinstance(item, Attachment) or item.kind not in ("image", "file") or not item.mime:
            raise LlmError("bad_attachment", final=True)
    return out


def with_attachments(messages: list[dict[str, Any]], attachments: list[Attachment]) -> list[dict[str, Any]]:
    """Сообщения для Chat Completions с вложениями у последнего сообщения владельца."""
    if not attachments:
        return messages
    out = [dict(m) for m in messages]
    index = next((i for i in range(len(out) - 1, -1, -1) if out[i].get("role") == "user"), None)
    if index is None:
        out.append({"role": "user", "content": ""})
        index = len(out) - 1
    parts: list[dict[str, Any]] = []
    text = out[index].get("content")
    if isinstance(text, str) and text:
        parts.append({"type": "text", "text": text})
    for item in attachments:
        if item.kind == "image":
            parts.append({"type": "image_url", "image_url": {"url": item.data_url()}})
        else:
            parts.append({"type": "file", "file": {"filename": item.name or "file", "file_data": item.data_url()}})
    out[index]["content"] = parts
    return out


def structured_messages(instructions: str, text: str, schema: dict[str, Any] | None,
                        schema_name: str | None) -> list[dict[str, str]]:
    """Сообщения для запроса «JSON по схеме». Схема уходит текстом в запросе, а не параметром
    API: строгий режим схем есть не у всех провайдеров, а ответ сервис проверяет сам."""
    header = instructions.rstrip()
    if schema is not None:
        header = f"{header}\n\nJSON schema:\n{json.dumps(schema, ensure_ascii=False, sort_keys=True)}"
    header = header.strip()
    if schema_name:
        header = f"{header}\n\nSchema name: {schema_name}"
    return [{"role": "system", "content": JSON_ONLY}, {"role": "user", "content": f"{header}\n\n{text}"}]


def parse_json(text: str) -> Any:
    """Разбирает ответ модели как JSON. None — не разобралось (это не сбой: решает сервис)."""
    if not text:
        return None
    found = _FENCE.search(text)
    try:
        return json.loads(found.group(1).strip() if found else text.strip())
    except (ValueError, RecursionError):
        return None


_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def _is_type(value: Any, name: str) -> bool:
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    expected = _TYPES.get(name)
    return expected is None or isinstance(value, expected)


def matches_schema(value: Any, schema: Any, _depth: int = 0) -> bool:
    """Подходит ли значение под схему — для отметки `schema_valid` в результате задания.

    Та же проверка, что в плагине (`plugins/shturman/shturman_core/textlimits.py`): type, enum,
    required, properties, additionalProperties: false, items. Решений по отметке никто не
    принимает: сервис проверяет ответ модели сам и терпимее.
    """
    if not isinstance(schema, dict) or _depth > 32:
        return True
    kind = schema.get("type")
    if isinstance(kind, str) and not _is_type(value, kind):
        return False
    if isinstance(kind, list) and not any(isinstance(k, str) and _is_type(value, k) for k in kind):
        return False
    if isinstance(schema.get("enum"), list) and value not in schema["enum"]:
        return False
    if isinstance(value, dict):
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        if any(key not in value for key in required):
            return False
        if schema.get("additionalProperties") is False and any(key not in properties for key in value):
            return False
        return all(matches_schema(value[key], sub, _depth + 1) for key, sub in properties.items() if key in value)
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        return all(matches_schema(item, schema["items"], _depth + 1) for item in value)
    return True


def task_models(env: Mapping[str, str]) -> dict[str, str]:
    """Модели для отдельных задач из окружения: {задача: модель}."""
    return {task: env[name].strip() for task, name in TASK_ENV.items() if env.get(name, "").strip()}


def _error_names(response: httpx.Response) -> str:
    """Текст отказа в нижнем регистре — только чтобы найти в нём имя параметра. Никуда не пишется."""
    try:
        return response.text[:4000].lower()
    except Exception:  # noqa: BLE001
        return ""


class LlmClient:
    """Обращения к модели. `transport` — для тестов: подставной сервер вместо сети."""

    def __init__(
        self, *, base_url: str, api_key: str, model: str, models: Mapping[str, str] | None = None,
        proxy_url: str = "", transport: httpx.AsyncBaseTransport | None = None, timeout: float = 90.0,
        slots: int = 2, tokens_param: str = "", sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        restricted: bool = False,
    ) -> None:
        self.model = model
        self.models = dict(models or {})
        self.broken: str | None = None
        self._client: httpx.AsyncClient | None = None
        self._sleep = sleep
        self._slots = asyncio.Semaphore(max(1, min(int(slots), 2)))
        self._fixed_param = tokens_param if tokens_param in TOKEN_PARAMS else None
        openai = (urlsplit(base_url).hostname or "").lower() == "api.openai.com"
        self._first_param = "max_completion_tokens" if openai else "max_tokens"
        self._param: dict[str, str] = {}          # имя предела, которое приняла модель
        self._no_json_mode: set[str] = set()      # модели, чей сервер не знает response_format
        self.calls = 0
        self.failures = 0
        self.last_ok: bool | None = None
        self.last_error: str | None = None
        if transport is None:
            try:
                transport = httpx.AsyncHTTPTransport(proxy=proxy_url or None, trust_env=False, retries=0)
            except ImportError:
                self.broken = "proxy_needs_socksio"
                return
            except Exception:  # noqa: BLE001
                self.broken = "bad_proxy_url"
                return
        if restricted:
            # Адрес пришёл со страницы настройки: каждый запрос — только наружу (netguard.py).
            transport = netguard.PinnedTransport(
                transport, connect_by_name=netguard.proxy_resolves_names(proxy_url))
        self._client = httpx.AsyncClient(
            transport=transport, base_url=base_url.rstrip("/"), trust_env=False, follow_redirects=False,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(timeout, connect=10.0, pool=timeout),
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    def model_for(self, task: Any) -> str:
        """Модель задачи; незнакомая задача получает модель по умолчанию."""
        return self.models.get(task, self.model) if isinstance(task, str) else self.model

    def _token_param(self, model: str) -> str:
        return self._fixed_param or self._param.get(model) or self._first_param

    def ready(self) -> bool:
        """Можно ли сейчас брать задания. У доступа по ключу пауз нет."""
        return True

    async def chat(self, messages: list[dict[str, str]], *, task: Any = None, max_tokens: int,
                   json_mode: bool = False, schema: dict[str, Any] | None = None, schema_name: str | None = None,
                   attachments: list[Attachment] | None = None) -> tuple[str, str]:
        """Один ответ модели: (текст, имя модели). Бросает `LlmError`.

        schema и schema_name здесь не используются: схема уже в тексте запроса
        (`structured_messages`), а строгий режим схем есть не у всех провайдеров."""
        async with self._slots:
            self.calls += 1
            try:
                files = check_attachments(attachments)
                out = await self._chat(with_attachments(messages, files), self.model_for(task), max_tokens, json_mode)
            except LlmError as exc:
                self.failures += 1
                self.last_ok, self.last_error = False, exc.code
                raise
            self.last_ok, self.last_error = True, None
            return out

    async def _chat(self, messages: list[dict[str, Any]], model: str, max_tokens: int,
                    json_mode: bool) -> tuple[str, str]:
        if self._client is None:
            raise LlmError(self.broken or "not_configured", final=False)
        attempt = 0
        swapped_param = dropped_json = False
        param = self._token_param(model)
        with_format = json_mode and model not in self._no_json_mode
        while True:
            body: dict[str, Any] = {"model": model, "messages": messages, param: int(max_tokens)}
            if with_format:
                body["response_format"] = {"type": "json_object"}
            wait: float | None = None
            try:
                response = await self._client.post("/chat/completions", json=body)
            except netguard.Blocked as exc:
                # Адрес со страницы ведёт не туда, куда можно: повторять бессмысленно.
                raise LlmError(exc.code, final=True) from None
            except _CONNECT as exc:
                code, wait = f"connect:{type(exc).__name__}", 0.0
            except httpx.TimeoutException:
                raise LlmError("timeout") from None
            except httpx.HTTPError as exc:
                raise LlmError(f"network:{type(exc).__name__}") from None
            else:
                status = response.status_code
                if status == 200:
                    # Запоминаем то, что сервер принял: в следующий раз начнём с этого.
                    self._param[model] = param
                    if json_mode and not with_format:
                        self._no_json_mode.add(model)
                    return self._read(response, model)
                code = f"http_{status}"
                if status in (400, 422):
                    names = _error_names(response)
                    if not self._fixed_param and not swapped_param and param in names:
                        # Сервер не принимает это имя предела длины — один раз пробуем второе.
                        swapped_param = True
                        param = next(p for p in TOKEN_PARAMS if p != param)
                        continue
                    if with_format and not dropped_json and "response_format" in names:
                        dropped_json, with_format = True, False
                        continue
                    raise LlmError(code, final=True)
                if status == 429 or status >= 500:
                    wait = _retry_after(response)
                else:
                    raise LlmError(code, final=status != 408)
            if attempt >= len(RETRY_DELAYS):
                raise LlmError(code)
            pause = max(RETRY_DELAYS[attempt], wait or 0.0)
            attempt += 1
            logger.info("модель: повтор обращения (%s)", code.split(":")[0])
            await self._sleep(pause)

    @staticmethod
    def _read(response: httpx.Response, model: str) -> tuple[str, str]:
        try:
            data = response.json()
            message = data["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise LlmError("bad_response") from None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):     # некоторые серверы отдают ответ частями
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict) and isinstance(p.get("text"), str))
        text = content if isinstance(content, str) else ""
        used = data.get("model") if isinstance(data.get("model"), str) and data.get("model") else model
        return text, used


def _retry_after(response: httpx.Response) -> float:
    raw = response.headers.get("retry-after", "")
    try:
        return max(0.0, min(float(raw), MAX_RETRY_AFTER))
    except ValueError:
        return 0.0
