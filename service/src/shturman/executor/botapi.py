"""Клиент Telegram Bot API для бота согласований. Только httpx.

Три правила, ради которых клиент написан своими руками:

1. **Один вызов — один запрос.** Клиент ничего не повторяет сам: повторять или нет, решает
   вызывающий. Для отправки от имени владельца это главное — см. `worker.py`.
2. **Итог вызова бывает трёх видов**, и вызывающий всегда знает какой:
     * результат — Telegram выполнил запрос;
     * `Refused`  — Telegram ответил отказом (код 4xx): запрос точно не выполнен;
     * `NeverLeft` — запрос не покинул сервер (нет соединения, нет свободного соединения);
     * `OutcomeUnknown` — всё остальное (истекло время, оборвалась связь, ошибка 5xx,
       неразборчивый ответ): Telegram мог выполнить запрос.
3. **Токен не выходит за пределы этого файла.** Bot API требует токен в адресе запроса, а httpx
   пишет адрес в журнал (уровень INFO) и кладёт запрос в исключения. Поэтому клиент httpx
   работает с адресом без токена (`/bot/<метод>`), а токен подставляет обёртка транспорта
   в последний момент. Исключения httpx наружу не выходят — только вид ошибки.

Что сверено и когда (7 октября 2026):
  * по документации core.telegram.org/bots/api (Bot API 10.3 от 24 августа 2026): виды обновлений
    `message`, `callback_query`, `business_connection`, `business_message`,
    `edited_business_message`, `deleted_business_messages`; параметры `sendMessage`
    (`business_connection_id`, `reply_parameters`, `link_preview_options`, `disable_notification`,
    `reply_markup`); правила `getUpdates` — обновление подтверждается вызовом с `offset` больше
    его номера, неполученные обновления хранятся не дольше 24 часов, при включённом webhook
    метод не работает;
  * по python-telegram-bot 22.8 (тот, что в поставке Hermes 0.21.5; только имена, код не взят):
    методы `getMe`, `editMessageText`, `editMessageReplyMarkup`, `answerCallbackQuery`,
    `getBusinessConnection` и поля `BusinessConnection`, `BusinessMessagesDeleted`.
На настоящем Telegram клиент не запускался.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

API_URL = "https://api.telegram.org"
ALLOWED_UPDATES = (
    "message", "callback_query", "business_connection", "business_message",
    "edited_business_message", "deleted_business_messages",
)
POLL_SECONDS = 25                 # сколько Telegram держит запрос getUpdates открытым
_TOKEN = re.compile(r"^\d{3,20}:[A-Za-z0-9_-]{20,128}$")
_PLACEHOLDER = b"/bot/"
_FILE_PLACEHOLDER = b"/file/bot/"
# Запрос не покинул сервер: соединение не установлено либо не нашлось свободного (имена httpx).
_NEVER_LEFT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

# Известные отказы Telegram → короткий код. В журнал и в очередь заданий попадает только код:
# описание ошибки целиком туда не пишется.
_REASONS = (
    ("message is not modified", "not_modified"),
    ("message to edit not found", "message_not_found"),
    ("message to be replied not found", "reply_not_found"),
    ("message can't be edited", "not_editable"),
    ("chat not found", "chat_not_found"),
    ("bot was blocked by the user", "bot_blocked"),
    ("user is deactivated", "user_deactivated"),
    ("can't initiate conversation", "not_started"),
    ("message is too long", "too_long"),
    ("query is too old", "query_too_old"),
    ("terminated by other getupdates", "other_poller"),
    ("webhook is active", "webhook"),
    ("business connection not found", "business_connection_not_found"),
    ("too many requests", "too_many_requests"),
    ("unauthorized", "unauthorized"),
)
_UPPER_CODE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")


class BotApiError(Exception):
    """Общий предок. В тексте исключения нет ни адреса запроса, ни токена, ни текста сообщений."""


class Refused(BotApiError):
    """Telegram ответил отказом: запрос точно не выполнен."""

    def __init__(self, code: int, reason: str, retry_after: int | None = None) -> None:
        super().__init__(f"{code} {reason}")
        self.code, self.reason, self.retry_after = code, reason, retry_after


class NeverLeft(BotApiError):
    """Запрос не покинул сервер."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class OutcomeUnknown(BotApiError):
    """Неизвестно, выполнил ли Telegram запрос."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


def reason_code(description: Any) -> str:
    """Короткий код отказа по описанию ошибки Telegram. Незнакомое описание — «other»."""
    if not isinstance(description, str):
        return "other"
    lowered = description.lower()
    for needle, code in _REASONS:
        if needle in lowered:
            return code
    found = _UPPER_CODE.search(description)      # коды вида BUSINESS_PEER_INVALID
    return found.group(0).lower()[:60] if found else "other"


def keyboard(buttons: list[list[tuple[str, str]]] | None) -> dict[str, Any] | None:
    """Строки пар (подпись, данные) → клавиатура Bot API."""
    if not buttons:
        return None
    return {"inline_keyboard": [[{"text": text, "callback_data": data} for text, data in row] for row in buttons]}


class _TokenTransport(httpx.AsyncBaseTransport):
    """Подставляет токен в адрес уже после того, как запрос ушёл из клиента httpx.

    Клиент (а значит журнал httpx и исключения) видит адрес `/bot/<метод>`; настоящий адрес
    `/bot<токен>/<метод>` существует только внутри этого вызова.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport, token: str) -> None:
        self._inner = inner
        self._prefix = b"/bot" + token.encode("ascii") + b"/"
        self._file_prefix = b"/file/bot" + token.encode("ascii") + b"/"

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path
        if path.startswith(_PLACEHOLDER):
            real_path = self._prefix + path[len(_PLACEHOLDER):]
        elif path.startswith(_FILE_PLACEHOLDER):
            # Скачивание файла по file_path из getFile: /file/bot<токен>/<путь>.
            real_path = self._file_prefix + path[len(_FILE_PLACEHOLDER):]
        else:
            raise httpx.UnsupportedProtocol("адрес запроса не принадлежит Bot API")
        real = httpx.Request(
            request.method, request.url.copy_with(raw_path=real_path),
            headers=request.headers, stream=request.stream, extensions=request.extensions,
        )
        return await self._inner.handle_async_request(real)

    async def aclose(self) -> None:
        await self._inner.aclose()


class BotApi:
    """Вызовы Bot API. `transport` — для тестов: подставной Telegram вместо сети."""

    def __init__(self, token: str, *, proxy_url: str = "", transport: httpx.AsyncBaseTransport | None = None,
                 base_url: str = API_URL, timeout: float = 20.0) -> None:
        self._token = token
        self.broken: str | None = None      # почему клиент не может работать вовсе (код причины)
        self._client: httpx.AsyncClient | None = None
        if not _TOKEN.match(token):
            self.broken = "bad_token_format"
            return
        if transport is None:
            try:
                # trust_env=False: прокси и сертификаты берутся только из настроек сервиса,
                # а не из случайных переменных окружения.
                transport = httpx.AsyncHTTPTransport(proxy=proxy_url or None, trust_env=False, retries=0)
            except ImportError:
                # Прокси SOCKS без пакета socksio. Мимо заданного владельцем прокси не ходим.
                self.broken = "proxy_needs_socksio"
                return
            except Exception:  # noqa: BLE001 — неверный адрес прокси
                self.broken = "bad_proxy_url"
                return
        self._client = httpx.AsyncClient(
            transport=_TokenTransport(transport, token), base_url=base_url, trust_env=False,
            timeout=httpx.Timeout(timeout, connect=10.0, pool=5.0),
        )

    def scrub(self, text: str) -> str:
        """Убирает токен из строки — на случай, если он всё же оказался в чужом тексте."""
        return text.replace(self._token, "***") if self._token else text

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def call(self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        """Один запрос к Bot API. Возвращает поле `result` либо бросает `BotApiError`."""
        if self._client is None:
            raise NeverLeft(self.broken or "not_configured")
        body = {k: v for k, v in (params or {}).items() if v is not None}
        failure: BotApiError | None = None
        try:
            response = await self._client.post(
                f"/bot/{method}", json=body,
                timeout=httpx.USE_CLIENT_DEFAULT if timeout is None else httpx.Timeout(timeout, connect=10.0, pool=5.0),
            )
        except _NEVER_LEFT as exc:
            failure = NeverLeft(type(exc).__name__)
        except (httpx.HTTPError, OSError, RuntimeError) as exc:
            failure = OutcomeUnknown(type(exc).__name__)
        if failure is not None:
            # Бросаем уже вне блока except: исходное исключение httpx (в нём бывает адрес запроса)
            # не остаётся ни причиной, ни контекстом нашего.
            raise failure
        status = response.status_code
        try:
            data = response.json()
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get("ok") is True and status == 200:
            return data.get("result")
        if isinstance(data, dict) and data.get("ok") is False and 400 <= status < 500:
            extra = data.get("parameters") if isinstance(data.get("parameters"), dict) else {}
            wait = extra.get("retry_after")
            wait = int(wait) if isinstance(wait, (int, float)) and not isinstance(wait, bool) and wait > 0 else None
            reason = "too_many_requests" if status == 429 else reason_code(data.get("description"))
            raise Refused(status, reason, wait if status == 429 else None)
        # Ошибка сервера, ответ не от Telegram (прокси, заглушка) или неразборчивый ответ:
        # считать это отказом нельзя.
        raise OutcomeUnknown(f"http_{status}")

    # --- методы ---

    async def download_file(self, file_id: str, *, max_bytes: int) -> bytes:
        """Файл по file_id: getFile, затем скачивание. Bot API отдаёт файлы до 20 МБ.
        Бросает Refused (файла нет, слишком большой), NeverLeft и OutcomeUnknown."""
        info = await self.call("getFile", {"file_id": file_id})
        path = info.get("file_path") if isinstance(info, dict) else None
        size = info.get("file_size") if isinstance(info, dict) else None
        if not isinstance(path, str) or not path or ".." in path or path.startswith("/"):
            raise Refused(400, "file_unavailable")
        if isinstance(size, int) and size > max_bytes:
            raise Refused(400, "file_too_big")
        assert self._client is not None
        failure: BotApiError | None = None
        try:
            async with self._client.stream("GET", "/file/bot/" + path) as response:
                if response.status_code != 200:
                    failure = Refused(response.status_code, "file_unavailable") if 400 <= response.status_code < 500 \
                        else OutcomeUnknown(f"http_{response.status_code}")
                else:
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data += chunk
                        if len(data) > max_bytes:
                            failure = Refused(400, "file_too_big")
                            break
        except _NEVER_LEFT as exc:
            failure = NeverLeft(type(exc).__name__)
        except (httpx.HTTPError, OSError, RuntimeError) as exc:
            failure = OutcomeUnknown(type(exc).__name__)
        if failure is not None:
            raise failure
        return bytes(data)

    async def get_me(self) -> dict[str, Any]:
        out = await self.call("getMe")
        return out if isinstance(out, dict) else {}

    async def get_updates(self, offset: int | None, *, poll: int = POLL_SECONDS) -> list[dict[str, Any]]:
        out = await self.call(
            "getUpdates", {"offset": offset, "timeout": poll, "allowed_updates": list(ALLOWED_UPDATES)},
            timeout=poll + 10.0,
        )
        return [u for u in out if isinstance(u, dict)] if isinstance(out, list) else []

    async def send_message(
        self, chat_id: int, text: str, *, buttons: list[list[tuple[str, str]]] | None = None,
        silent: bool = False, business_connection_id: str | None = None, reply_to: int | None = None,
        no_preview: bool = False,
    ) -> int:
        """Обычный текст, без разметки: в сообщениях есть чужой текст, и разметка позволила бы
        подделать вид сообщения. Возвращает номер отправленного сообщения."""
        out = await self.call("sendMessage", {
            "chat_id": chat_id, "text": text,
            "business_connection_id": business_connection_id,
            "reply_markup": keyboard(buttons),
            "disable_notification": True if silent else None,
            "reply_parameters": {"message_id": reply_to} if reply_to else None,
            "link_preview_options": {"is_disabled": True} if no_preview else None,
        })
        message_id = out.get("message_id") if isinstance(out, dict) else None
        if isinstance(message_id, bool) or not isinstance(message_id, int):
            # Telegram ответил «выполнено», но без номера сообщения: оно ушло, номер неизвестен.
            raise OutcomeUnknown("no_message_id")
        return message_id

    async def edit_message_text(self, chat_id: int, message_id: int, text: str, *,
                                reply_markup: dict[str, Any] | None = None) -> None:
        """Меняет текст. Клавиатура остаётся, только если передать её заново."""
        await self.call("editMessageText", {
            "chat_id": chat_id, "message_id": message_id, "text": text, "reply_markup": reply_markup,
            "link_preview_options": {"is_disabled": True},
        })

    async def remove_keyboard(self, chat_id: int, message_id: int) -> None:
        await self.call("editMessageReplyMarkup", {
            "chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}})

    async def answer_callback_query(self, query_id: str, text: str | None = None) -> None:
        await self.call("answerCallbackQuery", {"callback_query_id": query_id, "text": text or None})

    async def get_business_connection(self, connection_id: str) -> dict[str, Any] | None:
        out = await self.call("getBusinessConnection", {"business_connection_id": connection_id})
        return out if isinstance(out, dict) else None
