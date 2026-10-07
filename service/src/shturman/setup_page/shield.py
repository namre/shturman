"""Общая защита всего префикса /shturman-setup/: имя узла, вид запроса, строгие заголовки.

Стоит перед маршрутами страницы и срабатывает на любой ответ под префиксом, в том числе на
«не найдено» и на ошибку. Вход, сессию и защиту от подделки запроса проверяют сами маршруты
(`service.py`): им для этого нужна база.

Чему страница верит, а чему нет. Сервис стоит за обратным прокси, и до него доходят заголовки,
которые мог вписать кто угодно:

  * `Host` — сверяется с перечнем: внешнее имя из `SHTURMAN_SETUP_ORIGIN` и локальные имена
    сервиса (`SHTURMAN_ALLOWED_HOSTS`, по умолчанию 127.0.0.1:8765 и localhost:8765). Запрос
    с другим именем получает отказ: так страницу нельзя открыть под подставным именем
    (подмена DNS на локальный адрес);
  * `Origin` и `Sec-Fetch-Site` — их ставит браузер, страница их подделать не может; по ним
    отсекаются запросы с чужих сайтов (см. `service.py`);
  * `X-Forwarded-For`, `X-Forwarded-Proto`, `X-Real-IP`, `Forwarded` — не читаются вовсе. Ни
    одно решение от адреса клиента не зависит: ограничения входа общие на всех, а не «на адрес»,
    поэтому подставной заголовок ничего не даёт. Будет ли cookie помечена `Secure`, решает не
    заголовок, а настройка: схема внешнего адреса из `SHTURMAN_SETUP_ORIGIN`.

Заголовок `Authorization` здесь ничего не значит: токены внутреннего API и архива входом
на страницу не служат.
"""

from __future__ import annotations

from typing import Any

from starlette.responses import JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import PREFIX

METHODS = frozenset({"GET", "POST", "PUT", "DELETE"})
SCOPE_KEY = "shturman.setup"

# Скрипты и стили — только свои файлы: ни встроенных, ни чужих. Запросы — только к себе.
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
       "font-src 'self'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'")
HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"content-security-policy", CSP.encode()),
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-store"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
    (b"x-robots-tag", b"noindex, nofollow"),
)
_OWN = frozenset(name for name, _ in HEADERS)
_BAD_RAW = (b"%2f", b"%5c", b"%2e", b"%00", b"\\")


def origins(config: Any) -> dict[str, str]:
    """Имя узла из заголовка Host → адрес (Origin), под которым страница при этом открыта."""
    out = {host.lower(): f"http://{host.lower()}" for host in config.allowed_hosts}
    if config.setup_origin:
        out[config.setup_origin.split("://", 1)[1]] = config.setup_origin
    return out


class Shield:
    def __init__(self, app: ASGIApp, config: Any) -> None:
        self.app = app
        self.origins = origins(config)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def guarded(message: Message) -> None:
            if message["type"] == "http.response.start":
                kept = [(k, v) for k, v in message.get("headers", ()) if k.lower() not in _OWN]
                message = {**message, "headers": kept + list(HEADERS)}
            await send(message)

        headers = dict(scope["headers"])
        host = headers.get(b"host", b"").decode("latin-1").strip().lower()
        origin = self.origins.get(host)
        if origin is None:
            # Ответ один на все чужие имена и ничего не сообщает о сервисе.
            await JSONResponse({"error": "misdirected"}, status_code=421)(scope, receive, guarded)
            return
        path, raw = scope.get("path", ""), (scope.get("raw_path") or b"").lower()
        segments = path.split("/")
        if "" in segments[1:-1] or ".." in segments or "." in segments or any(mark in raw for mark in _BAD_RAW):
            # Ничего, кроме простых путей: ни «..», ни закодированных косых черт.
            await JSONResponse({"error": "not_found"}, status_code=404)(scope, receive, guarded)
            return
        if scope.get("method") not in METHODS:
            await JSONResponse({"error": "method_not_allowed"}, status_code=405)(scope, receive, guarded)
            return
        if path == PREFIX:
            await RedirectResponse(PREFIX + "/", status_code=308)(scope, receive, guarded)
            return
        scope[SCOPE_KEY] = {"origin": origin, "secure": origin.startswith("https://")}
        await self.app(scope, receive, guarded)
