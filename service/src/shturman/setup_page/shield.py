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
порта может подложить свою. Поэтому сессию страница держит не в cookie (см. `auth.py`), а здесь
заголовок `Set-Cookie` вырезается из любого ответа под префиксом. Одно исключение (Р-88): cookie
попытки входа по коду `__Secure-shturman-code` (`HttpOnly; Secure; SameSite=Strict`) — только в
ответе на запрос и ввод кода (путь `/shturman-setup/api/login/code…`) и только с этим именем и этим
путём. На адрес дашборда она не уходит: путь `/shturman-setup*` в блоке дашборда закрыт прокси; скрипт
дашборда её не прочитает (`HttpOnly`); подложенная им вторая cookie с тем же именем делает вход по
коду недействительным, а не чужим (`code_login.attempt_cookie`). Сама по себе она ничего не даёт —
нужен ещё код из бота.

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

Одно исключение из строгих заголовков — страница Mini App (`/shturman-setup/app/`, только GET): её
разрешено открыть в рамке у https://web.telegram.org — так её показывает Telegram Web (`APP_HEADERS`).
Её скрипты, стили и запросы к API, как и вся остальная страница, в рамке не открываются.

Отказ (ответ JSON с кодом 400 и выше), который получил переход браузера на страницу, а не запрос
скрипта, человек видит короткой страницей по-русски (`human_page`): код ответа и решение те же.

Заголовки `Authorization` и `Cookie` здесь ничего не значат: токены внутреннего API и архива
входом на страницу не служат, а cookie страница не читает. Заголовков CORS
(`Access-Control-*`) страница не отдаёт никогда: чужому origin её ответы не читаются.
"""

from __future__ import annotations

from typing import Any

from starlette.responses import JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import APP_PATH, PREFIX
from .auth import CODE_COOKIE, CODE_PATH

_CODE_COOKIE = (CODE_COOKIE + "=").encode()
_CODE_COOKIE_TAIL = f"; Path={CODE_PATH}; HttpOnly; Secure; SameSite=Strict".encode()


def _code_cookie(path: str, name: bytes, value: bytes) -> bool:
    """Единственная cookie, которую страница может поставить: попытка входа по коду, на её пути."""
    return (name.lower() == b"set-cookie" and (path == CODE_PATH or path == CODE_PATH + "/request")
            and value.startswith(_CODE_COOKIE) and _CODE_COOKIE_TAIL in value and b"Domain" not in value)

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

# Mini App (`miniapp.py`) — сама страница приложения, и только она. Telegram открывает Mini App на
# телефоне и компьютере в своём встроенном браузере, а в Telegram Web (web.telegram.org) — в рамке
# (iframe) своей страницы. Поэтому ей, единственной под префиксом, разрешена рамка — и только у
# https://web.telegram.org. `X-Frame-Options` у неё нет: ALLOW-FROM браузеры больше не понимают,
# а DENY запретил бы рамку и там. Остальное то же: свои скрипты и стили, запросы только к себе.
# Скрипт telegram-web-app.js с telegram.org не подключается (`static/app.js`).
TELEGRAM_WEB = "https://web.telegram.org"
APP_CSP = CSP.replace("frame-ancestors 'none'", "frame-ancestors " + TELEGRAM_WEB)
APP_HEADERS: tuple[tuple[bytes, bytes], ...] = tuple(
    (name, APP_CSP.encode() if name == b"content-security-policy" else value)
    for name, value in HEADERS if name != b"x-frame-options")
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


# --- отказ, который видит человек ---------------------------------------------------------------
#
# Отказы под префиксом — ответы JSON для скриптов страницы и приложения. Но человек тоже может
# открыть такой адрес в браузере или в окне Telegram: набрать адрес API, пойти по устаревшей или
# неверной ссылке, открыть страницу под чужим именем узла. Тогда вместо `{"error": "not_found"}`
# он видит короткую страницу по-русски. Меняется только тело уже отказанного ответа и только для
# перехода браузера на страницу (`Sec-Fetch-Mode: navigate`; у старых браузеров без этого
# заголовка — `Accept` с `text/html`): код ответа, заголовки защиты и решения те же, а запросы
# `fetch` из страницы и приложения получают JSON, как раньше. В странице нет ничего из запроса.

_SWAPPED_TYPES = (b"application/json", b"text/plain")
_LINK_PAGE = f'<p><a href="{PREFIX}/">Открыть «Настройки Штурмана»</a></p>'
_OPEN_APP = "Приложение открывается кнопкой <strong>«Штурман»</strong> внизу чата с вашим ботом согласований."


def _navigation(headers: dict[bytes, bytes]) -> bool:
    mode = headers.get(b"sec-fetch-mode")
    if mode is not None:
        return mode == b"navigate"
    return b"text/html" in headers.get(b"accept", b"")


def human_page(path: str, status: int) -> bytes:
    """Страница вместо отказа JSON для человека, открывшего адрес в браузере. Текст выбирается только
    по пути и коду ответа; из запроса в неё не попадает ничего."""
    styled = True
    if status == 421:
        # Имя узла чужое: о сервисе ничего не сообщаем, как и прежний ответ JSON.
        title, body, styled = ("Адрес не подходит", "<p>По этому адресу страница не открывается. Откройте её ровно по "
                               "тому адресу, который вам выдали, — с тем же именем и портом.</p>", False)
    elif path.startswith(PREFIX + "/api/app/") or path == PREFIX + "/api/app":
        title, body = "Это служебный адрес", f"<p>Через него приложение «Штурман» получает данные — страницы здесь нет.</p><p>{_OPEN_APP}</p>"
    elif path.startswith(APP_PATH):
        title, body = "Такой страницы нет", f"<p>{_OPEN_APP}</p>"
    elif path.startswith(PREFIX + "/api/") or path == PREFIX + "/api":
        title, body = "Это служебный адрес", "<p>Через него «Настройки Штурмана» получают данные — страницы здесь нет.</p>" + _LINK_PAGE
    else:
        title, body = "Такой страницы нет", _LINK_PAGE
    css = f'<link rel="stylesheet" href="{PREFIX}/static/app.css">' if styled else ""
    return (
        '<!doctype html>\n<html lang="ru">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="robots" content="noindex, nofollow">\n<meta name="color-scheme" content="light dark">\n'
        f"<title>{title}</title>\n{css}\n</head>\n<body>\n<main>\n<section class=\"state\">\n"
        f"<h1>{title}</h1>\n{body}\n<p class=\"muted\">Код ответа: {status}.</p>\n</section>\n</main>\n</body>\n</html>\n"
    ).encode("utf-8")


class Shield:
    def __init__(self, app: ASGIApp, config: Any) -> None:
        self.app = app
        self.origins = origins(config)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Рамку разрешаем только ответу с самой страницей приложения: ровно этот путь, только GET.
        own = APP_HEADERS if scope.get("path") == APP_PATH and scope.get("method") == "GET" else HEADERS
        headers = dict(scope["headers"])
        for_person = scope.get("method") == "GET" and _navigation(headers)
        swapped = False

        async def guarded(message: Message) -> None:
            nonlocal swapped
            if message["type"] == "http.response.start":
                kept = [(k, v) for k, v in message.get("headers", ())
                        if (k.lower() not in _OWN and not k.lower().startswith(_NEVER))
                        or _code_cookie(scope.get("path", ""), k, v)]
                kind = dict((k.lower(), v) for k, v in kept).get(b"content-type", b"").lower()
                if for_person and message["status"] >= 400 and kind.startswith(_SWAPPED_TYPES):
                    swapped = True
                    page = human_page(scope.get("path", ""), message["status"])
                    kept = [(k, v) for k, v in kept if k.lower() not in (b"content-type", b"content-length")]
                    await send({"type": "http.response.start", "status": message["status"],
                                "headers": kept + [(b"content-type", b"text/html; charset=utf-8"),
                                                   (b"content-length", str(len(page)).encode())] + list(HEADERS)})
                    await send({"type": "http.response.body", "body": page, "more_body": False})
                    return
                message = {**message, "headers": kept + list(own)}
            elif swapped:
                return                 # тело отказа JSON уже заменено страницей
            await send(message)

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
        if path in (PREFIX, APP_PATH.rstrip("/")):
            # Адрес без косой черты на конце. Перенаправление — относительным адресом: собственное
            # перенаправление маршрутизатора строится из схемы запроса, а за обратным прокси она
            # http, и браузер ушёл бы на http:// по порту https (а у приложения сломались бы
            # относительные адреса его файлов).
            await RedirectResponse(path + "/", status_code=308)(scope, receive, guarded)
            return
        scope[SCOPE_KEY] = {"origin": origin}
        await self.app(scope, receive, guarded)
