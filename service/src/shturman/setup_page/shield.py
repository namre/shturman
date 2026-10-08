"""Общая защита всего префикса /shturman-setup/: имя узла, вид запроса, строгие заголовки.

Стоит перед маршрутами страницы и срабатывает на любой ответ под префиксом, в том числе на
«не найдено» и на ошибку. Вход, сессию и источник запроса проверяют сами маршруты
(`service.py`): им для этого нужна база.

Страница живёт на своём origin. Дашборд Hermes — место, где ассистент может исполнять свой
JavaScript (расширения дашборда). На одном origin с ним такой скрипт читал бы хранилище страницы,
слал бы запросы от её имени и ставил бы service worker, перехватывающий ввод. Поэтому внешний
адрес страницы обязан отличаться от адреса дашборда — другим портом того же имени или другим
именем. Другой порт — другой origin: `localStorage`, service worker и `fetch` дашборда до страницы
не дотягиваются. Если адреса совпали (`config.setup_reason == "same_origin"`), внешнего адреса
в перечне ниже нет, и снаружи страница не отвечает вовсе.

Чего порт НЕ разделяет — cookie: браузер шлёт их на любой порт того же имени, а скрипт соседнего
порта может подложить свою. Поэтому страница не ставит и не читает cookie вообще (см. `auth.py`),
а здесь заголовок `Set-Cookie` на всякий случай вырезается из любого ответа под префиксом.

Чему страница верит, а чему нет. Сервис стоит за обратным прокси, и до него доходят заголовки,
которые мог вписать кто угодно:

  * `Host` — сверяется с перечнем: внешнее имя ВМЕСТЕ С ПОРТОМ из `SHTURMAN_SETUP_ORIGIN` и
    локальные имена сервиса (`SHTURMAN_ALLOWED_HOSTS`, по умолчанию 127.0.0.1:8765 и
    localhost:8765). Запрос с другим именем или с тем же именем, но другим портом (так выглядит
    запрос, по ошибке прокси пришедший с адреса дашборда) получает отказ: страницу нельзя
    открыть ни под подставным именем, ни на origin дашборда;
  * `Origin` и `Sec-Fetch-Site` — их ставит браузер, страница их подделать не может; по ним
    отсекаются запросы с чужих сайтов (см. `service.py`);
  * `X-Forwarded-For`, `X-Forwarded-Proto`, `X-Real-IP`, `Forwarded` — не читаются вовсе. Ни
    одно решение от адреса клиента не зависит: ограничения входа общие на всех, а не «на адрес»,
    поэтому подставной заголовок ничего не даёт;
  * `Service-Worker: script` — так браузер запрашивает файл service worker'а. Своего у страницы
    нет, поэтому такой запрос получает отказ: под префиксом service worker не зарегистрировать.

Заголовки `Authorization` и `Cookie` здесь ничего не значат: токены внутреннего API и архива
входом на страницу не служат, а cookie страница не читает. Заголовков CORS
(`Access-Control-*`) страница не отдаёт никогда: чужому origin её ответы не читаются.
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
    # Страница не делит процесс и `document.domain` с соседними портами того же имени.
    (b"origin-agent-cluster", b"?1"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
    (b"x-robots-tag", b"noindex, nofollow"),
)
_OWN = frozenset(name for name, _ in HEADERS)
_NEVER = (b"set-cookie", b"access-control-")       # таких заголовков в ответах страницы не бывает
_BAD_RAW = (b"%2f", b"%5c", b"%2e", b"%00", b"\\")


def origins(config: Any) -> dict[str, str]:
    """Имя узла из заголовка Host → адрес (Origin), под которым страница при этом открыта.

    Внешний адрес попадает сюда, только если он не совпал с адресом дашборда Hermes."""
    out = {host.lower(): f"http://{host.lower()}" for host in config.allowed_hosts}
    external = config.setup_external
    if external:
        out[external.split("://", 1)[1]] = external
    return out


class Shield:
    def __init__(self, app: ASGIApp, config: Any) -> None:
        self.app = app
        self.origins = origins(config)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def guarded(message: Message) -> None:
            if message["type"] == "http.response.start":
                kept = [(k, v) for k, v in message.get("headers", ())
                        if k.lower() not in _OWN and not k.lower().startswith(_NEVER)]
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
        if b"service-worker" in headers:
            await JSONResponse({"error": "not_found"}, status_code=404)(scope, receive, guarded)
            return
        if scope.get("method") not in METHODS:
            await JSONResponse({"error": "method_not_allowed"}, status_code=405)(scope, receive, guarded)
            return
        if path == PREFIX:
            await RedirectResponse(PREFIX + "/", status_code=308)(scope, receive, guarded)
            return
        scope[SCOPE_KEY] = {"origin": origin}
        await self.app(scope, receive, guarded)
