"""Клиент внутреннего API сервиса переписки. Только стандартная библиотека.

Сервис необязателен: пока не заданы адрес и токен, `from_env()` возвращает None, и всё,
что на нём построено (исполнитель заданий, пересылка бизнес-сообщений, инструменты агента),
молча не работает. Вход, мастер и защита бизнес-режима от сервиса не зависят.

Правила:
  * токен, тексты сообщений и тела запросов в журнал и в тексты ошибок не попадают;
  * запрос идёт напрямую, без прокси из окружения: адрес локальный;
  * перенаправления не выполняются: токен не должен уйти по чужому адресу;
  * отправить можно только то, что разрешено перечнем маршрутов клиента (`service_routes`).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

from .service_routes import Routes, allowed

DEFAULT_URL = "http://127.0.0.1:8765"
URL_ENV = "SHTURMAN_SERVICE_URL"
TOKEN_ENV = "SHTURMAN_API_TOKEN"
DEFAULT_TIMEOUT = 5.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class ServiceError(Exception):
    """Сервис ответил отказом. `status` — код HTTP, `code` — машинный код из ответа, если есть."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None,
                 payload: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.payload = dict(payload or {})


class ServiceUnavailable(ServiceError):
    """Сервис недоступен: нет связи, истекло время или он отвечает ошибкой сервера."""


class NotAllowed(ServiceError):
    """Запрос не входит в перечень разрешённых для этого клиента. В сеть он не уходил."""


def env_value(name: str) -> str:
    """Значение настройки так же, как читается токен бота: из Hermes, иначе из окружения."""
    try:
        from hermes_cli.config import get_env_value  # type: ignore

        return (get_env_value(name) or "").strip()
    except Exception:
        return os.environ.get(name, "").strip()


def normalize_base_url(raw: str) -> str:
    """Адрес сервиса без пути и хвостов. Пустая строка, если адрес не годится."""
    value = (raw or "").strip() or DEFAULT_URL
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        return ""
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{parts.scheme}://{host}" + (f":{port}" if port else "")


def settings() -> tuple[str, str]:
    """(адрес, токен) из настроек. Токен пуст — сервис не подключён."""
    return normalize_base_url(env_value(URL_ENV)), env_value(TOKEN_ENV)


def configured() -> bool:
    base_url, token = settings()
    return bool(base_url and token)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401, ANN001
        return None      # ответ 3xx станет ошибкой HTTP: стандартный обработчик унёс бы токен дальше


def _opener() -> urllib.request.OpenerDirector:
    # Пустой ProxyHandler отключает прокси из переменных окружения.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


class ServiceClient:
    def __init__(self, base_url: str, token: str, *, allow: Routes, timeout: float = DEFAULT_TIMEOUT) -> None:
        base = normalize_base_url(base_url)
        if not base:
            raise ValueError("адрес сервиса переписки задан неверно")
        if not token:
            raise ValueError("не задан токен сервиса переписки")
        self.base_url = base
        self._token = token
        self._allow = allow
        self.timeout = timeout
        self._open = _opener().open

    def __repr__(self) -> str:       # токен не должен оказаться в журнале вместе с объектом
        return f"ServiceClient({self.base_url!r})"

    @classmethod
    def from_env(cls, allow: Routes, *, timeout: float = DEFAULT_TIMEOUT) -> "ServiceClient | None":
        base_url, token = settings()
        if not base_url or not token:
            return None
        return cls(base_url, token, allow=allow, timeout=timeout)

    def request(self, method: str, path: str, *, json_body: Any = None,
                query: Mapping[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        """Выполняет запрос и возвращает JSON-объект ответа.

        Отказ сервиса (4xx) — `ServiceError` с кодом и текстом из ответа; нет связи, истекло
        время, ответ 5xx или не JSON — `ServiceUnavailable`.
        """
        return self.call(method, path, json_body=json_body, query=query, timeout=timeout)[1]

    def call(self, method: str, path: str, *, json_body: Any = None,
             query: Mapping[str, Any] | None = None, timeout: float | None = None,
             ) -> tuple[int, dict[str, Any]]:
        """То же, что `request`, но вместе с кодом ответа (например, 202 «ещё считается»)."""
        method = method.upper()
        if not allowed(self._allow, method, path):
            raise NotAllowed("этот запрос к сервису переписки не разрешён", code="not_allowed")
        url = self.base_url + path
        if query:
            pairs = [(k, str(v)) for k, v in query.items() if v is not None and v != ""]
            if pairs:
                url += "?" + urllib.parse.urlencode(pairs)
        data = None
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._open(request, timeout=timeout or self.timeout) as response:
                status = response.status
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                raw = exc.read(MAX_RESPONSE_BYTES + 1)
            except Exception:
                raw = b""
            finally:
                exc.close()
        except Exception as exc:  # нет связи, истекло время, обрыв
            # Только вид ошибки: в её тексте бывает адрес, а в цепочке причин — заголовки запроса.
            raise ServiceUnavailable(
                f"сервис переписки недоступен ({type(exc).__name__})", code="unavailable") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ServiceUnavailable("ответ сервиса переписки слишком большой", status=status, code="too_large")
        payload: Any = None
        if raw:
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                payload = None
        if not isinstance(payload, dict):
            payload = None
        if 200 <= status < 300:
            if payload is None:
                raise ServiceUnavailable("сервис переписки ответил не JSON-объектом", status=status,
                                         code="bad_response")
            return status, payload
        body = payload or {}
        message = body.get("error") if isinstance(body.get("error"), str) else None
        # Машинный код отказа: `code` у приёма сообщений, `reason` у шлюза отправки.
        code = next((body[k] for k in ("code", "reason") if isinstance(body.get(k), str)), None)
        if status == 401:
            raise ServiceUnavailable("сервис переписки не принял токен", status=status, code="unauthorized")
        if status >= 500 or 300 <= status < 400:
            raise ServiceUnavailable(message or f"сервис переписки ответил кодом {status}",
                                     status=status, code=code or "unavailable", payload=body)
        raise ServiceError(message or f"сервис переписки ответил кодом {status}",
                           status=status, code=code, payload=body)
