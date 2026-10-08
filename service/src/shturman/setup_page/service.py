"""Маршруты страницы настройки сервиса. Всё живёт под /shturman-setup/.

  GET    /shturman-setup/                               сама страница (данных в ней нет)
  GET    /shturman-setup/static/{файл}                  её стили и скрипты

  Без входа (только чтобы войти):
  GET    /shturman-setup/api/session                    вошёл ли браузер; можно ли войти по коду от бота
  POST   /shturman-setup/api/login/link                 вход по одноразовой ссылке: {token}
  POST   /shturman-setup/api/login/code/request         прислать код в бота согласований
  POST   /shturman-setup/api/login/code                 вход по коду: {code}

  После входа:
  POST   /shturman-setup/api/logout                     завершить эту сессию
  POST   /shturman-setup/api/logout-all                 завершить все сессии
  GET    /shturman-setup/api/state                      состояние шагов и блока «Дополнительно»
  GET    /shturman-setup/api/overview                   счётчики архива и журнал действий
  PUT    /shturman-setup/api/scenario                   способ подключения: {scenario: own | staff}
  POST   /shturman-setup/api/bot/token                  проверить и сохранить токен бота: {token, separate?}
  DELETE /shturman-setup/api/bot/token                  убрать токен, введённый на странице
  POST   /shturman-setup/api/bot/bind                   ссылка привязки владельца к боту
  POST   /shturman-setup/api/bot/refresh                переспросить у Telegram свойства бота
  PUT    /shturman-setup/api/tg/keys                    ключи приложения Telegram: {api_id, api_hash}
  DELETE /shturman-setup/api/tg/keys
  POST   /shturman-setup/api/tg/login                   начать вход по QR: {role, confirm_owner?}
  GET    /shturman-setup/api/tg/login/{login_id}        состояние входа; ссылка для QR, пока он ждёт
  POST   /shturman-setup/api/tg/login/{login_id}/password    облачный пароль: {password}
  POST   /shturman-setup/api/tg/login/{login_id}/cancel
  POST   /shturman-setup/api/tg/accounts/{id}/pause | resume | logout
  DELETE /shturman-setup/api/tg/accounts/{id}                удалить из архива аккаунт без сессии
  PUT    /shturman-setup/api/tg/accounts/{id}/options   {auto_personal?, auto_groups?, backfill_months?}
  GET    /shturman-setup/api/tg/accounts/{id}/dialogs   ?offset&limit&q&kind&only&refresh
  POST   /shturman-setup/api/tg/accounts/{id}/sync      {enabled, chats? | kind?}
  POST   /shturman-setup/api/tg/accounts/{id}/exclude   {peer_class, tg_id, excluded, purge?}
  POST   /shturman-setup/api/imports                    файл result.json телом запроса
  GET    /shturman-setup/api/imports
  GET    /shturman-setup/api/imports/{import_id}
  GET    /shturman-setup/api/imports/{import_id}/scan
  POST   /shturman-setup/api/imports/{import_id}/run    {exclude?, owner_id?}
  DELETE /shturman-setup/api/imports/{import_id}
  PUT    /shturman-setup/api/llm                        {api_key?, base_url?, model, switch?}
  DELETE /shturman-setup/api/llm
  POST   /shturman-setup/api/llm/chatgpt/start          начать вход через ChatGPT: {new?, switch?} → адрес входа
  POST   /shturman-setup/api/llm/chatgpt/finish         вставленный адрес после входа: {address}
  POST   /shturman-setup/api/llm/chatgpt/cancel         снять начатую попытку входа
  PUT    /shturman-setup/api/llm/chatgpt/model          модель подписки из списка OpenAI: {model}
  DELETE /shturman-setup/api/llm/chatgpt                выйти из подписки (отзыв сессии у OpenAI)

Вход — по одноразовой ссылке; ответ на вход один раз отдаёт ключ сессии. Дальше страница
присылает его в заголовке `X-Shturman-Session`; cookie нет вовсе (почему — `auth.py`).

Три правила.

1. **Страница не ходит во внутренний API.** Маршруты зовут те же функции сервиса, что и
   `/api/*`, но напрямую; ни один адрес под префиксом не пересылает запрос в `/api/*` или `/mcp`
   и не принимает их токены.
2. **Действие со страницы — действие самого владельца** и применяется сразу, без карточки в
   боте (`confirm.apply_owner`): карточка защищает от держателя токена API, а сюда входят
   по ссылке, которую можно получить только на сервере. Каждое изменение пишется в журнал.
3. **Секреты идут только в сторону сервиса.** Токен, ключ, пароль и код приходят в теле POST;
   обратно страница получает лишь «задано / не задано», имя бота и итог проверки. В журнал
   сервиса и в журнал действий значения не попадают. Это касается и входа через ChatGPT:
   вставленный адрес (в нём код входа) приходит телом POST и никуда не пишется, токены
   подписки страница не получает — только состояние, почту учётной записи и модель.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field

from importlib import resources
from typing import Any, AsyncIterator, Awaitable, Callable

from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from .. import __version__, api_core, bridge, confirm, ingest_api, store
from ..api_core import BadRequest, error_response
from ..app import AppState, state_of
from ..executor import binding, siwc
from ..executor import commands as executor_commands
from ..executor import service as executor_service
from ..executor.subscription import ChatGptClient
from ..executor.botapi import BotApiError
from ..tg import gateway
from ..tg import service as tg_service
from ..tg.manager import KEEP, ROLE_NAMES, TgError, TgManager
from . import PREFIX, apply, audit, auth, shield, summary
from . import secrets_store as ss

logger = logging.getLogger("shturman.setup")

API = PREFIX + "/api"
MAX_BODY = 64 * 1024          # байт: JSON-запросы страницы заведомо меньше
FAIL_DELAY = 0.4              # секунд «думает» отказ во входе по ссылке
FAIL_SLOTS = 8                # столько отказов «думают» одновременно; остальные отвечают сразу
FAIL_AUDIT_EVERY = 60.0       # не чаще раза в минуту в журнал действий пишется неудачный вход
PAGE_HEADER = "x-shturman-setup"
PAGE_MARK = "x-shturman-setup-page"       # метка на ответе с самой страницей: по ней её узнаёт проверка

STATIC_TYPES = {
    "setup.css": "text/css; charset=utf-8",
    "setup.js": "text/javascript; charset=utf-8",
    "qr.js": "text/javascript; charset=utf-8",
}

# Виды чатов для отбора в списке диалогов.
KINDS = {
    "personal": ("personal_chat", "saved_messages", "bot_chat"),
    "group": ("private_group", "private_supergroup", "public_supergroup"),
    "channel": ("private_channel", "public_channel"),
}
PEER_CLASSES = ("user", "chat", "channel")


@dataclass
class Page:
    """Работающая страница — лежит в `state.extras["setup_page"]`."""
    settings: apply.Settings
    key: bytes = field(repr=False)                 # ключ подписи кодов входа
    slow: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(FAIL_SLOTS))
    fail_audit_at: float = 0.0
    logins_done: set[str] = field(default_factory=set)   # входы в Telegram, уже отмеченные в журнале
    # Начатый вход через ChatGPT — один на страницу; в памяти, перезапуск сервиса его снимает.
    chatgpt_attempt: siwc.Attempt | None = None
    chatgpt_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def slow_refusal(self) -> None:
        """Отказ во входе отвечает не мгновенно. Верный вход этим не задерживается, а когда
        «думающих» отказов уже много, лишние отвечают сразу — очередь из них не копится."""
        if self.slow.locked():
            return
        async with self.slow:
            await asyncio.sleep(FAIL_DELAY)


def _page(request: Request) -> Page:
    page = state_of(request).extras.get(summary.EXTRAS_KEY)
    if not isinstance(page, Page):
        raise BadRequest("Страница настройки не запущена.", 503)
    return page


# --- общее для всех маршрутов --------------------------------------------------------------------

def _context(request: Request) -> dict[str, Any]:
    ctx = request.scope.get(shield.SCOPE_KEY)
    if not isinstance(ctx, dict):
        # Запрос пришёл мимо общей защиты префикса — так быть не должно.
        raise BadRequest("не найдено", 404)
    return ctx


def _check_fetch(request: Request, ctx: dict[str, Any]) -> None:
    """Запрос сделала сама страница, а не другой сайт и не соседний порт того же имени.

    Это вторая линия: первая — ключ сессии в заголовке, которого у чужого скрипта нет. Заголовки
    `Origin` и `Sec-Fetch-Site` ставит браузер; скрипт подделать их не может. Отвергается всё,
    что не `same-origin`, для любого метода, включая GET: `same-site` — это дашборд Hermes на
    соседнем порту, `cross-site` — чужой сайт, `none` — адрес API, набранный в адресной строке
    (странице он не нужен). Запрос без этих заголовков (не из браузера) решает ключ сессии."""
    site = request.headers.get("sec-fetch-site")
    origin = request.headers.get("origin")
    refused = BadRequest("Запрос пришёл не со страницы настройки.", 403, "bad_origin")
    if site is not None and site != "same-origin":
        raise refused
    if request.method == "GET":
        if origin is not None and origin != ctx["origin"]:
            raise refused
        return
    if origin != ctx["origin"]:
        raise refused
    if request.headers.get(PAGE_HEADER) != "1":
        # Свой заголовок простая форма на чужом сайте выставить не может.
        raise refused


async def _session(request: Request) -> auth.Session | None:
    """Сессия по ключу из заголовка. Cookie не читаются вовсе."""
    token = request.headers.get(auth.SESSION_HEADER)
    if not token:
        return None
    async with state_of(request).pool.acquire() as conn:
        return await auth.find_session(conn, token)


Handler = Callable[[Request], Awaitable[Response]]


def endpoint(fn: Handler | None = None, *, public: bool = False) -> Any:
    """Обёртка маршрута: проверка источника запроса и сессии; отказы превращаются в ответ
    с текстом для владельца. public — маршрут входа: сессии ещё нет."""
    def deco(fn: Handler) -> Handler:
        async def wrapped(request: Request) -> Response:
            try:
                _check_fetch(request, _context(request))
                if not public:
                    session = await _session(request)
                    if session is None:
                        raise BadRequest("Вход устарел. Войдите заново.", 401, "unauthenticated")
                    request.state.setup_session = session
                if public:
                    return await fn(request)
                from .. import authority
                with authority.setup_context(session.id, action=request.url.path):
                    return await fn(request)
            except BadRequest as exc:
                return error_response(exc)
            except apply.Invalid as exc:
                return JSONResponse({"error": exc.message, "code": exc.code}, status_code=422)
            except TgError as exc:
                return JSONResponse({"error": exc.message}, status_code=exc.status)
            except confirm.Refused as exc:
                return JSONResponse({**exc.extra, "error": exc.message, **({"code": exc.code} if exc.code else {})},
                                    status_code=exc.status)
            except gateway.AccountUnavailable:
                return JSONResponse({"error": "Аккаунт не подключён."}, status_code=409)
            except ClientDisconnect:
                return JSONResponse({"error": "Соединение оборвалось до конца запроса."}, status_code=400)
        wrapped.__name__ = fn.__name__
        return wrapped
    return deco(fn) if fn is not None else deco


async def _body(request: Request) -> dict[str, Any]:
    """Тело запроса как JSON-объект, с пределом размера. Пустое тело — пустой объект."""
    import json

    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY:
        raise BadRequest("Запрос слишком большой.", 413)
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > MAX_BODY:
            raise BadRequest("Запрос слишком большой.", 413)
    if not buf.strip():
        return {}
    try:
        data = json.loads(bytes(buf))
    except (ValueError, RecursionError):
        raise BadRequest("Запрос составлен неверно.") from None
    if not isinstance(data, dict):
        raise BadRequest("Запрос составлен неверно.")
    return data


def _flag(data: dict[str, Any], key: str, *, required: bool = False) -> bool | None:
    value = data.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, bool):
        raise BadRequest(f"поле {key}: нужно true или false")
    return value


def _query_int(request: Request, key: str, default: int, *, low: int, high: int) -> int:
    raw = request.query_params.get(key) or ""
    if not raw:
        return default
    if not raw.isdigit() or len(raw) > 9:
        raise BadRequest(f"параметр {key}: нужно целое число")
    return max(low, min(high, int(raw)))


async def _log(request: Request, action: str, outcome: str = audit.OK, detail: str = "") -> None:
    await audit.write(state_of(request).pool, action, outcome, detail)


# --- бот согласований: общее --------------------------------------------------------------------

def _runtime_bot(state: AppState) -> Any:
    runtime = state.extras.get("executor")
    return getattr(runtime, "bot", None), getattr(runtime, "api", None)


async def _bound_owner(state: AppState) -> tuple[dict[str, int] | None, Any]:
    """Владелец, привязанный к боту согласований, и клиент Bot API — либо (None, None)."""
    bot, api = _runtime_bot(state)
    if bot is None or api is None or bot.bot_id is None:
        return None, None
    async with state.pool.acquire() as conn:
        owner = await binding.bound_owner(conn, bot.bot_id)
    return (owner, api) if owner else (None, None)


def _tell_owner(state: AppState, text: str) -> None:
    """Сообщение владельцу в бота согласований о событии на странице (вход, блокировка).
    Не ждём и не повторяем: это оповещение, а не часть действия."""
    async def send() -> None:
        owner, api = await _bound_owner(state)
        if owner is None:
            return
        try:
            await api.send_message(owner["chat_id"], text, no_preview=True)
        except BotApiError as exc:
            logger.warning("страница настройки: оповещение владельцу не отправлено (%s)", exc)

    state.spawn(send(), name="setup-notify")


# --- страница и её файлы -----------------------------------------------------------------------

def _static(name: str) -> bytes:
    return (resources.files("shturman.setup_page") / "static" / name).read_bytes()


async def index(request: Request) -> Response:
    """Сама страница. Тело одно и то же для любого запроса: данных в нём нет, а вошёл ли
    браузер, страница узнаёт уже своим запросом с ключом сессии."""
    return Response(_static("index.html"), media_type="text/html; charset=utf-8", headers={PAGE_MARK: "1"})


async def static_file(request: Request) -> Response:
    name = request.path_params["name"]
    media = STATIC_TYPES.get(name)
    if media is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    if request.headers.get("sec-fetch-site") not in (None, "same-origin"):
        # Скрипты и стили страницы нужны только ей самой: соседнему порту и чужому сайту — отказ.
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return Response(_static(name), media_type=media)


# --- вход ---------------------------------------------------------------------------------------

def _signed_in(token: str, session: auth.Session) -> JSONResponse:
    """Единственный ответ, в котором есть ключ сессии. Cookie не ставится."""
    return JSONResponse({"ok": True, "key": token, "expires_at": session.expires_at.isoformat()})


async def _failed_login(request: Request, how: str) -> None:
    page = _page(request)
    moment = time.monotonic()
    if moment - page.fail_audit_at >= FAIL_AUDIT_EVERY or page.fail_audit_at == 0.0:
        page.fail_audit_at = moment
        await _log(request, "login.failed", audit.REFUSED, how)


@endpoint(public=True)
async def session_info(request: Request) -> JSONResponse:
    state = state_of(request)
    session = await _session(request)
    owner, _ = await _bound_owner(state)
    # Без входа — только то, без чего не войти: есть ли кому прислать код. Версию не сообщаем.
    out: dict[str, Any] = {"authenticated": session is not None, "code_login": owner is not None}
    if session is not None:
        out.update(via=session.via, expires_at=session.expires_at.isoformat())
    return JSONResponse(out)


@endpoint(public=True)
async def login_link(request: Request) -> JSONResponse:
    data = await _body(request)
    state = state_of(request)
    created = None
    async with state.pool.acquire() as conn:
        if await auth.redeem_link(conn, data.get("token")):
            created = await auth.create_session(conn, "link")
    if created is None:
        await _page(request).slow_refusal()
        await _failed_login(request, "ссылка не подошла")
        raise BadRequest("Эта ссылка уже использована или устарела. Попросите того, кто ставил ассистента, "
                         "выдать новую — или выполните на сервере ./ops/setup-link.sh", 401, "link_invalid")
    await _log(request, "login.link", detail="прежние сессии завершены")
    _tell_owner(state, "Выполнен вход на страницу настройки Штурмана по одноразовой ссылке. "
                       "Если это были не вы — выполните на сервере ./ops/logout-all.sh")
    return _signed_in(*created)


CODE_TEXT = ("Код входа на страницу настройки Штурмана: {code}\n"
             "Действует 5 минут. Никому его не сообщайте. Если вы не запрашивали код, ничего делать не нужно.")
LOCK_TEXT = ("Кто-то несколько раз подряд ввёл неверный код входа на страницу настройки Штурмана. "
             "Вход по коду временно закрыт. Если это были не вы, ничего делать не нужно: "
             "без кода из этого чата войти нельзя.")


@endpoint(public=True)
async def login_code_request(request: Request) -> JSONResponse:
    await _body(request)
    state = state_of(request)
    owner, api = await _bound_owner(state)
    send = None
    if owner is not None:
        async def send(code: str) -> None:
            await api.send_message(owner["chat_id"], CODE_TEXT.format(code=f"{code[:4]} {code[4:]}"),
                                   no_preview=True)
    async with state.pool.acquire() as conn:
        result = await auth.request_code(conn, _page(request).key, send)
    if result == "sent":
        await _log(request, "login.code_sent")
    return JSONResponse({"result": result})


@endpoint(public=True)
async def login_code(request: Request) -> JSONResponse:
    data = await _body(request)
    state = state_of(request)
    created = None
    async with state.pool.acquire() as conn:
        result = await auth.verify_code(conn, _page(request).key, data.get("code") or "")
        if result == "ok":
            created = await auth.create_session(conn, "code")
    if created is not None:
        await _log(request, "login.code")
        _tell_owner(state, "Выполнен вход на страницу настройки Штурмана по коду из этого чата.")
        return _signed_in(*created)
    if result in ("locked_now", "locked_again"):
        await _log(request, "login.code_locked", audit.REFUSED)
        if result == "locked_now":          # одно сообщение владельцу на период блокировки
            _tell_owner(state, LOCK_TEXT)
        result = "locked"
    elif result == "wrong":
        await _failed_login(request, "код не подошёл")
    return JSONResponse({"error": "Войти не получилось.", "code": result}, status_code=401)


@endpoint
async def logout(request: Request) -> JSONResponse:
    async with state_of(request).pool.acquire() as conn:
        await auth.revoke_session(conn, request.state.setup_session.id)
    await _log(request, "logout")
    return JSONResponse({"ok": True})


@endpoint
async def logout_all(request: Request) -> JSONResponse:
    async with state_of(request).pool.acquire() as conn:
        count = await auth.revoke_all(conn)
    await _log(request, "logout.all", detail=f"сессий завершено: {count}")
    return JSONResponse({"ok": True, "sessions": count})


# --- состояние разделов ------------------------------------------------------------------------

def _value(page: Page, name: str) -> dict[str, Any]:
    source = page.settings.source(name)
    return {"set": source is not None, "source": source, "editable": page.settings.editable(name)}


def _bot_problem_text(code: str | None) -> str | None:
    if not code:
        return None
    if code in ("other_poller", "conflict"):
        return apply.OTHER_POLLER
    if code == "webhook":
        return apply.WEBHOOK
    if code == "token_rejected":
        return ("Telegram больше не принимает этот токен. Возможно, у @BotFather выпущен новый — "
                "введите действующий токен заново.")
    text = executor_commands.PROBLEMS.get(code)
    return (text[0].upper() + text[1:] + ".") if text else "Бот сейчас не на связи с Telegram."


async def _bot_state(state: AppState, page: Page) -> dict[str, Any]:
    config = state.config
    bot, api = _runtime_bot(state)
    out: dict[str, Any] = {
        "configured": bool(config.own_bot), **_value(page, ss.BOT_TOKEN),
        "username": None, "polling": None, "problem": None, "problem_text": None,
        "owner_bound": False, "owner_name": None, "owner_bound_at": None, "bind_paused": False,
        "business_capable": None,
        "business_connections": 0, "business_can_reply": False,
    }
    if bot is None:
        return out
    problem = (api.broken if api is not None and api.broken else bot.problem)
    out.update(username=bot.identity["username"] if bot.identity else None, polling=bot.polling,
               problem=problem, problem_text=_bot_problem_text(problem),
               bind_paused=bot.flood.locked(), business_capable=bot.business_capable)
    async with state.ro_pool.acquire() as conn:
        owner = await binding.bound_owner(conn, bot.bot_id)
        if owner is not None:
            out["owner_bound"] = True
            out["owner_name"] = await binding.bound_owner_name(conn, bot.bot_id)
            # Когда привязка состоялась: по смене этой отметки страница понимает, что ссылку открыли.
            bound_at = await conn.fetchval("SELECT updated_at FROM executor_state WHERE key = 'owner'")
            out["owner_bound_at"] = bound_at.isoformat() if bound_at else None
        row = await conn.fetchrow(
            """SELECT count(*) AS n, COALESCE(bool_or(can_reply), false) AS can_reply
               FROM business_connections WHERE via = 'service' AND enabled""")
    out.update(business_connections=int(row["n"]), business_can_reply=bool(row["can_reply"]))
    return out


async def _owner_known(state: AppState) -> bool:
    async with state.ro_pool.acquire() as conn:
        return await bridge.get_owner(conn) is not None or bool(await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM accounts WHERE role = 'owner')"))


async def _tg_state(state: AppState, page: Page) -> dict[str, Any]:
    manager = state.extras.get("tg")
    keys = {"configured": bool(state.config.tg_api_id and state.config.tg_api_hash),
            "source": page.settings.source(ss.TG_API_HASH),
            "editable": page.settings.editable(ss.TG_API_ID, ss.TG_API_HASH)}
    out: dict[str, Any] = {"available": isinstance(manager, TgManager), "keys": keys, "accounts": [],
                           "owner_known": await _owner_known(state)}
    if not isinstance(manager, TgManager) or not manager.configured:
        return out
    accounts = await manager.list_accounts()
    async with state.ro_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT account_id, count(*) FILTER (WHERE enabled) AS enabled,
                      count(*) FILTER (WHERE enabled AND backfill_done) AS done,
                      count(*) FILTER (WHERE enabled AND access_lost_at IS NOT NULL) AS lost
               FROM tg_sync_chats WHERE chat_id IS NOT NULL GROUP BY account_id""")
    counts = {r["account_id"]: r for r in rows}
    for account in accounts:
        row = counts.get(account["account_id"])
        account["role_name"] = ROLE_NAMES.get(account["role"], account["role"])
        account["chats_enabled"] = int(row["enabled"]) if row else 0
        account["chats_loaded"] = int(row["done"]) if row else 0
        account["chats_lost"] = int(row["lost"]) if row else 0
    out["accounts"] = accounts
    async with state.ro_pool.acquire() as conn:
        out["detached"] = await manager.detached_accounts(conn)
    out["logins"] = [{"login_id": flow.id, "role": flow.role} for flow in manager.flows.values() if not flow.done]
    return out


def _llm_state(state: AppState, page: Page) -> dict[str, Any]:
    config = state.config
    runtime = state.extras.get("executor")
    llm = getattr(runtime, "llm", None)
    problem = (llm.broken or llm.last_error) if llm is not None else None
    by_key = llm is not None and not isinstance(llm, ChatGptClient)
    return {
        "configured": bool(config.own_llm),
        # Каким способом сервис обращается к своей модели: api_key | subscription | null.
        "way": config.llm_way,
        "key": _value(page, ss.LLM_API_KEY),
        # Адрес и имя модели — не секреты: показываются как есть.
        "base_url": {**_value(page, ss.LLM_BASE_URL), "value": config.llm_base_url},
        "model": {**_value(page, ss.LLM_MODEL), "value": config.llm_model},
        "last_call_ok": llm.last_ok if by_key else None,
        "problem_text": apply.llm_problem_text(problem) if by_key else None,
        "subscription": _subscription_state(state, page, llm),
    }


def _subscription_state(state: AppState, page: Page, llm: Any) -> dict[str, Any]:
    """Подписка ChatGPT для страницы. Токенов здесь нет и быть не должно: только состояние,
    почта учётной записи (это страница самого владельца) и модель."""
    store = executor_service.chatgpt_keeper(state).store
    record = store.load()
    status = store.status()
    runtime = llm if isinstance(llm, ChatGptClient) else None
    if status == siwc.ACTIVE:
        status = {"ok": "connected"}.get(runtime.status(), runtime.status()) if runtime else "connected"
    attempt = page.chatgpt_attempt
    if attempt is not None and attempt.expired():
        attempt = None
    problem = runtime.last_error if runtime is not None else None
    text = apply.SUBSCRIPTION_STATUS.get(status, "")
    if status == "denied" and runtime is not None:
        text = apply.DENIED_TEXT.format(code=runtime.denied_code or "forbidden") + " Сервис час не обращается к модели."
    editable = state.config.chatgpt or page.settings.editable(ss.LLM_API_KEY)
    return {
        "available": bool(editable),
        "locked_text": None if editable else apply.SUBSCRIPTION_LOCKED,
        "status": status,
        "status_text": text,
        "registered": bool(record.get("client_id")),
        "email": record.get("email") if status != "none" else None,
        "model": record.get("model") or None,
        "models": record.get("models") or [],
        "last_call_ok": runtime.last_ok if runtime is not None else None,
        "problem_text": apply.subscription_problem_text(problem) if status == "connected" else None,
        "attempt": {"expires_at": _iso(attempt.expires_at)} if attempt is not None else None,
        "usage_url": siwc.USAGE_URL,
    }


def _iso(moment: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(moment, timezone.utc).isoformat()


def _imports_state(state: AppState) -> dict[str, Any]:
    registry = state.extras.get("imports")
    if registry is None:
        return {"available": False, "items": [], "max_bytes": 0}
    items = sorted(registry.items.values(), key=lambda u: u.uploaded_at, reverse=True)
    return {"available": True, "items": [u.view() for u in items], "max_bytes": registry.max_bytes}


# --- способ подключения ----------------------------------------------------------------------------
# own   — «ассистент видит всё как вы»: сессия основного аккаунта на чтение;
# staff — «ассистент — отдельный сотрудник»: бизнес-режим для личных чатов, аккаунт ассистента
#         (роль assistant) для групп.
# Выбор влияет только на то, какие шаги показывает страница: подключённое в другом способе
# не отключается и остаётся видно. Хранится в setup_state — внутренний API его не читает.

SCENARIOS = {"own": "ассистент видит всё как вы", "staff": "ассистент — отдельный сотрудник"}
SCENARIO_KEY = "scenario"


async def _scenario_state(state: AppState) -> dict[str, Any]:
    async with state.ro_pool.acquire() as conn:
        chosen = await conn.fetchval("SELECT value->>'scenario' FROM setup_state WHERE key = $1", SCENARIO_KEY)
        owner_session = await conn.fetchval(
            """SELECT EXISTS (SELECT 1 FROM tg_sessions s JOIN accounts a ON a.id = s.account_id
                              WHERE a.role = 'owner')""")
        staffish = await conn.fetchval(
            """SELECT EXISTS (SELECT 1 FROM business_connections WHERE enabled)
                   OR EXISTS (SELECT 1 FROM tg_sessions s JOIN accounts a ON a.id = s.account_id
                              WHERE a.role = 'assistant')""")
    suggested = "own" if owner_session else "staff" if staffish else None
    return {"chosen": chosen if chosen in SCENARIOS else None, "suggested": suggested}


@endpoint
async def scenario_save(request: Request) -> JSONResponse:
    data = await _body(request)
    scenario = data.get("scenario")
    if scenario not in SCENARIOS:
        raise BadRequest("Выберите один из двух способов подключения.")
    async with state_of(request).pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO setup_state (key, value, updated_at) VALUES ($1, $2::jsonb, now())
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
            SCENARIO_KEY, f'{{"scenario": "{scenario}"}}')
    await _log(request, "setup.scenario", detail=SCENARIOS[scenario])
    return JSONResponse({"ok": True, "scenario": scenario})


@endpoint
async def page_state(request: Request) -> JSONResponse:
    state, page = state_of(request), _page(request)
    return JSONResponse({
        "version": __version__,
        "scenario": await _scenario_state(state),
        "origin_set": bool(state.config.setup_external),
        "sending": bool(state.config.sending),
        "bot": await _bot_state(state, page),
        "tg": await _tg_state(state, page),
        "llm": _llm_state(state, page),
        "imports": _imports_state(state),
    })


@endpoint
async def page_overview(request: Request) -> JSONResponse:
    state = state_of(request)
    counts = await api_core.overview(state)
    async with state.ro_pool.acquire() as conn:
        log = await audit.recent(conn, 30)
        key_log = await audit.recent_important(conn, 20)
    keep = ("messages", "chats", "chats_excluded", "accounts", "last_message_seen_at", "jobs_waiting",
            "jobs_failed", "guard_enabled", "guard_model_used", "guard_problem", "guard_checked",
            "guard_hidden", "guard_released", "guard_unchecked", "embeddings_enabled", "embeddings_model",
            "embeddings_embedded", "embeddings_left", "embeddings_problem", "sending",
            "voice_enabled", "voice_problem", "voice_pending", "voice_done", "voice_failed", "voice_skipped")
    # audit_key — входы, смена ключей и аккаунтов: их не вытеснить из вида потоком мелких действий.
    return JSONResponse({"archive": {k: counts[k] for k in keep if k in counts}, "audit": log,
                         "audit_key": key_log})


# --- 1. бот согласований -----------------------------------------------------------------------

@endpoint
async def bot_token_save(request: Request) -> JSONResponse:
    """Проверяет токен живым запросом и сохраняет. Первый вызов (без `separate`) ничего не
    сохраняет: возвращает имя бота, чтобы владелец подтвердил, что бот отдельный."""
    data = await _body(request)
    state, page = state_of(request), _page(request)
    if not page.settings.editable(ss.BOT_TOKEN):
        raise apply.Invalid("Токен бота задан в настройках сервера. Изменить его можно только там.", "locked")
    token = apply.clean(data.get("token"), limit=200)
    if not token:
        raise apply.Invalid("Вставьте токен бота из сообщения @BotFather.", "empty")
    bot = await apply.check_bot_token(state.config, token)
    found = {"username": bot["username"], "name": bot["name"]}
    if _flag(data, "separate") is not True:
        return JSONResponse({"status": "confirm", "bot": found})
    running, _ = _runtime_bot(state)
    same = running is not None and running.bot_id == bot["id"] and running.polling is True
    if not same:
        # Этого бота сервис ещё не опрашивает: проверяем, не опрашивает ли его кто-то другой.
        try:
            await apply.probe_other_poller(state.config, token)
        except apply.Invalid as exc:
            why = "у бота включён webhook" if exc.code == "webhook" else "ботом уже пользуется другая программа"
            await _log(request, "bot.token", audit.REFUSED, why)
            raise
    await page.settings.save({ss.BOT_TOKEN: token})
    await _log(request, "bot.token", detail="бот проверен запросом к Telegram")
    return JSONResponse({"status": "saved", "bot": found})


@endpoint
async def bot_token_delete(request: Request) -> JSONResponse:
    await _body(request)
    await _page(request).settings.save({ss.BOT_TOKEN: None})
    await _log(request, "bot.token_removed")
    return JSONResponse({"ok": True})


@endpoint
async def bot_bind(request: Request) -> JSONResponse:
    """Одноразовая ссылка привязки владельца — тот же механизм, что у команды `shturman bot-bind`."""
    await _body(request)
    state = state_of(request)
    bot, _ = _runtime_bot(state)
    if bot is None:
        raise BadRequest("Сначала сохраните токен бота согласований.", 409, "no_bot")
    if not bot.identity:
        raise BadRequest("Бот ещё не вышел на связь с Telegram. Подождите несколько секунд и повторите.",
                         409, "bot_not_ready")
    async with state.pool.acquire() as conn:
        rebind = await binding.bound_owner(conn, bot.bot_id) is not None
        code, expires_at = await binding.create_code(conn)
    await _log(request, "bot.bind_link", detail="повторная привязка" if rebind else "")
    return JSONResponse({"link": binding.deep_link(bot.identity["username"], code),
                         "expires_at": expires_at.isoformat(), "minutes": binding.CODE_TTL // 60,
                         "rebind": rebind})


@endpoint
async def bot_refresh(request: Request) -> JSONResponse:
    """Переспрашивает у Telegram свойства бота: включён ли у него бизнес-режим."""
    await _body(request)
    bot, api = _runtime_bot(state_of(request))
    if bot is None or api is None:
        raise BadRequest("Сначала сохраните токен бота согласований.", 409, "no_bot")
    try:
        me = await api.get_me()
    except BotApiError:
        raise BadRequest("Не удалось связаться с Telegram. Попробуйте ещё раз.", 502, "no_connection") from None
    bot.business_capable = me.get("can_connect_to_business") is True
    return JSONResponse({"business_capable": bot.business_capable})


# --- 2. ключи приложения Telegram ---------------------------------------------------------------

@endpoint
async def tg_keys_save(request: Request) -> JSONResponse:
    data = await _body(request)
    api_id, api_hash = apply.check_tg_keys(data.get("api_id"), data.get("api_hash"))
    await _page(request).settings.save({ss.TG_API_ID: api_id, ss.TG_API_HASH: api_hash})
    await _log(request, "tg.keys")
    return JSONResponse({"ok": True})


@endpoint
async def tg_keys_delete(request: Request) -> JSONResponse:
    await _body(request)
    await _page(request).settings.save({ss.TG_API_ID: None, ss.TG_API_HASH: None})
    await _log(request, "tg.keys_removed")
    return JSONResponse({"ok": True})


# --- 3. аккаунты Telegram -----------------------------------------------------------------------

def _tg(request: Request) -> TgManager:
    manager = state_of(request).extras.get("tg")
    if not isinstance(manager, TgManager):
        raise BadRequest("Модуль аккаунтов Telegram не запущен.", 503)
    if not manager.configured:
        raise BadRequest("Сначала введите ключи приложения Telegram — шаг «Ключи приложения Telegram».", 409, "no_keys")
    return manager


def _account_id(request: Request) -> int:
    return int(request.path_params["account_id"])


def _flow(page: Page, flow: Any) -> dict[str, Any]:
    """Состояние входа для страницы. QR страница рисует сама из ссылки — готовой картинки нет."""
    out = flow.snapshot()
    out.pop("qr_svg", None)
    return out


async def _note_login(request: Request, flow: Any) -> None:
    page = _page(request)
    if flow.status == "completed" and flow.id not in page.logins_done:
        page.logins_done.add(flow.id)
        await _log(request, "tg.login_done", detail=f"роль: {ROLE_NAMES.get(flow.role, flow.role)}")


@endpoint
async def tg_login_start(request: Request) -> JSONResponse:
    manager = _tg(request)
    data = await _body(request)
    role = data.get("role")
    if role not in ROLE_NAMES:
        raise BadRequest("Выберите, какой аккаунт подключаете.")
    if role == "assistant" and not await _owner_known(state_of(request)):
        raise BadRequest(
            "Сначала привяжите себя к боту согласований — первый шаг на этой странице. Пока сервис не знает, "
            "какой аккаунт ваш, он не сможет отличить его от аккаунта ассистента, а тому разрешена отправка сообщений.",
            409, "owner_unknown")
    flow = await manager.start_login(role, confirm_owner=_flag(data, "confirm_owner") is True)
    await _log(request, "tg.login", detail=f"роль: {ROLE_NAMES[role]}")
    return JSONResponse(_flow(_page(request), flow))


@endpoint
async def tg_login_status(request: Request) -> JSONResponse:
    flow = _tg(request).login(request.path_params["login_id"])
    await _note_login(request, flow)
    return JSONResponse(_flow(_page(request), flow))


@endpoint
async def tg_login_password(request: Request) -> JSONResponse:
    manager = _tg(request)
    data = await _body(request)
    password = data.get("password")
    if not isinstance(password, str) or not password:
        raise BadRequest("Введите облачный пароль Telegram.")
    flow = await manager.submit_password(request.path_params["login_id"], password)
    password = data["password"] = ""      # не держим пароль дольше одной проверки
    await _note_login(request, flow)
    return JSONResponse(_flow(_page(request), flow))


@endpoint
async def tg_login_cancel(request: Request) -> JSONResponse:
    await _body(request)
    flow = await _tg(request).cancel_login(request.path_params["login_id"])
    await _log(request, "tg.login_cancel", detail=f"роль: {ROLE_NAMES.get(flow.role, flow.role)}")
    return JSONResponse(_flow(_page(request), flow))


async def _role_of(request: Request, account_id: int) -> str:
    async with state_of(request).ro_pool.acquire() as conn:
        slot = await conn.fetchval("SELECT slot FROM tg_sessions WHERE account_id = $1", account_id)
    return ROLE_NAMES.get(slot, "аккаунт")


@endpoint
async def tg_pause(request: Request) -> JSONResponse:
    await _body(request)
    role = await _role_of(request, _account_id(request))
    await _tg(request).pause(_account_id(request))
    await _log(request, "tg.pause", detail=role)
    return JSONResponse({"ok": True})


@endpoint
async def tg_resume(request: Request) -> JSONResponse:
    await _body(request)
    role = await _role_of(request, _account_id(request))
    await _tg(request).resume(_account_id(request))
    await _log(request, "tg.resume", detail=role)
    return JSONResponse({"ok": True})


@endpoint
async def tg_logout(request: Request) -> JSONResponse:
    await _body(request)
    role = await _role_of(request, _account_id(request))
    out = await _tg(request).logout(_account_id(request))
    await _log(request, "tg.logout", detail=role + ("" if out.get("terminated") else "; Telegram завершение не подтвердил"))
    return JSONResponse(out)


@endpoint
async def tg_forget(request: Request) -> JSONResponse:
    """Удалить из архива аккаунт, из которого вышли, со всей его перепиской. Нужен, когда в роль
    вошли не тем аккаунтом: запись о роли иначе мешает подключить правильный."""
    await _body(request)
    manager, state = _tg(request), state_of(request)
    registry = state.extras.get("imports")
    if registry is not None and registry.running() is not None:
        raise BadRequest("Идёт импорт выгрузки. Удалите аккаунт после его окончания.", 409, "import_running")
    async with state.pool.acquire() as conn:
        out = (await confirm.apply_owner(conn, tg_service.FORGET, {"account_id": _account_id(request)}))["result"]
    await _log(request, "tg.forget", detail=f"{ROLE_NAMES.get(out['role'], out['role'])}; сообщений: {out['messages']}")
    return JSONResponse({"ok": True, "messages": out["messages"]})


@endpoint
async def tg_options(request: Request) -> JSONResponse:
    manager = _tg(request)
    data = await _body(request)
    months: Any = KEEP
    if "backfill_months" in data:
        months = data["backfill_months"]
        if months is not None and (isinstance(months, bool) or not isinstance(months, int) or not 1 <= months <= 600):
            raise BadRequest("Глубина истории — число месяцев от 1 до 600 либо «вся история».")
    flags = {key: _flag(data, key) for key in ("auto_personal", "auto_groups")}
    await manager.set_options(_account_id(request), auto_personal=flags["auto_personal"],
                              auto_groups=flags["auto_groups"], backfill_months=months)
    parts = [f"{'новые личные чаты' if key == 'auto_personal' else 'новые группы и каналы'}: "
             f"{'брать' if value else 'не брать'}" for key, value in flags.items() if value is not None]
    if months is not KEEP:
        parts.append("глубина истории: " + ("вся" if months is None else f"{months} мес."))
    await _log(request, "tg.options", detail="; ".join(parts))
    return JSONResponse({"ok": True})


@endpoint
async def tg_dialogs(request: Request) -> JSONResponse:
    manager = _tg(request)
    kind = request.query_params.get("kind") or ""
    if kind and kind not in KINDS:
        raise BadRequest("параметр kind: personal, group или channel")
    out = await manager.list_dialogs(
        _account_id(request),
        offset=_query_int(request, "offset", 0, low=0, high=10_000_000),
        limit=_query_int(request, "limit", 50, low=1, high=200),
        chat_types=KINDS.get(kind), query=(request.query_params.get("q") or "")[:200],
        only_enabled=request.query_params.get("only") == "enabled",
        refresh=request.query_params.get("refresh") == "1",
    )
    for item in out["items"]:
        item["kind"] = next((name for name, types in KINDS.items() if item["type"] in types), "personal")
        # Служебные чаты Telegram (коды входа, @BotFather) исключены всегда: вернуть их нельзя.
        item["locked"] = store.is_blocked_peer(item["peer_class"], item["tg_id"], item["username"])
    async with state_of(request).ro_pool.acquire() as conn:
        out["enabled_total"] = int(await conn.fetchval(
            "SELECT count(*) FROM tg_sync_chats WHERE account_id = $1 AND enabled", _account_id(request)))
    return JSONResponse(out)


def _peer_keys(raw: Any) -> list[tuple[str, int]]:
    if not isinstance(raw, list) or len(raw) > 5000:
        raise BadRequest("поле chats: нужен список чатов")
    keys = []
    for item in raw:
        if not isinstance(item, dict) or item.get("peer_class") not in PEER_CLASSES \
                or isinstance(item.get("tg_id"), bool) or not isinstance(item.get("tg_id"), int):
            raise BadRequest("поле chats: у каждого чата нужны peer_class и tg_id")
        keys.append((item["peer_class"], item["tg_id"]))
    return keys


@endpoint
async def tg_sync(request: Request) -> JSONResponse:
    manager = _tg(request)
    data = await _body(request)
    enabled = _flag(data, "enabled", required=True)
    keys = _peer_keys(data["chats"]) if data.get("chats") is not None else []
    kind = data.get("kind")
    if kind is not None and kind not in KINDS:
        raise BadRequest("поле kind: personal, group или channel")
    if not keys and kind is None:
        raise BadRequest("Не выбрано ни одного чата.")
    result = await manager.set_sync(_account_id(request), enabled=bool(enabled), chats=keys,
                                    chat_types=list(KINDS[kind]) if kind else None)
    on = sum(1 for item in result if item.get("enabled"))
    await _log(request, "tg.sync_on" if enabled else "tg.sync_off",
               detail=f"чатов: {on if enabled else len(result)} из {len(result)}"
                      + (f"; все чаты вида «{kind}»" if kind else ""))
    return JSONResponse({"chats": result, "enabled": on})


@endpoint
async def tg_exclude(request: Request) -> JSONResponse:
    """Исключить чат аккаунта из архива (по желанию — стерев сохранённое) или вернуть его."""
    manager = _tg(request)
    data = await _body(request)
    (key,) = _peer_keys([data])
    excluded, purge = _flag(data, "excluded", required=True), _flag(data, "purge") is True
    if purge and not excluded:
        raise BadRequest("Стереть сообщения можно только вместе с исключением чата.")
    account_id = _account_id(request)
    dialog = next((d for d in await manager.dialogs(account_id) if d.key == key), None)
    if dialog is None:
        raise BadRequest("Чат не найден среди диалогов аккаунта.", 404)
    if store.is_blocked_peer(dialog.chat.peer_class, dialog.chat.tg_id, dialog.chat.username):
        raise BadRequest("Служебный чат Telegram исключён всегда: в нём коды входа и токены.", 409, "locked")
    purged = 0
    async with state_of(request).pool.acquire() as conn:
        chat_id, _ = await store.ensure_chat(conn, account_id, dialog.chat, refresh=True)
        if not excluded:
            await confirm.apply_owner(conn, ingest_api.CHAT_INCLUDE, {"chat_id": chat_id})
        else:
            await confirm.apply_owner(conn, ingest_api.CHAT_EXCLUDE, {"chat_id": chat_id})
            if purge:
                purged = (await confirm.apply_owner(conn, ingest_api.CHAT_PURGE, {"chat_id": chat_id}))["result"] or 0
    if excluded:
        await _log(request, "chat.exclude", detail=f"чат {key[0]}:{key[1]}")
        if purge:
            await _log(request, "chat.purge", detail=f"чат {key[0]}:{key[1]}; сообщений: {purged}")
    else:
        await _log(request, "chat.include", detail=f"чат {key[0]}:{key[1]}")
    return JSONResponse({"excluded": bool(excluded), "purged": purged})


# --- 5. выгрузка из Telegram Desktop -------------------------------------------------------------
# Загрузка, просмотр состава и состояние — те же обработчики, что у /api/imports: файл пишется на
# диск потоком, предел размера общий. Отличается только запуск: здесь его не ждёт карточка в боте.

def _imports_ready(request: Request) -> None:
    if state_of(request).extras.get("imports") is None:
        raise BadRequest("Модуль импорта не запущен.", 503)


@endpoint
async def imports_upload(request: Request) -> Response:
    _imports_ready(request)
    response = await ingest_api.upload_export(request)
    if response.status_code == 201:
        import json

        size = json.loads(response.body).get("size_bytes", 0)
        await _log(request, "import.upload", detail=f"размер: {max(1, round(size / 1024 / 1024))} МБ")
    return response


@endpoint
async def imports_list(request: Request) -> Response:
    _imports_ready(request)
    return await ingest_api.list_imports(request)


@endpoint
async def imports_status(request: Request) -> Response:
    _imports_ready(request)
    return await ingest_api.import_status(request)


@endpoint
async def imports_scan(request: Request) -> Response:
    _imports_ready(request)
    return await ingest_api.scan_import(request)


@endpoint
async def imports_run(request: Request) -> Response:
    _imports_ready(request)
    view = await ingest_api.run_import_as_owner(request)
    await _log(request, "import.run")
    return JSONResponse(view, status_code=202)


@endpoint
async def imports_delete(request: Request) -> Response:
    _imports_ready(request)
    response = await ingest_api.delete_import(request)
    if response.status_code == 200:
        await _log(request, "import.delete")
    return response


# --- 7. своя модель сервиса --------------------------------------------------------------------

@endpoint
async def llm_save(request: Request) -> JSONResponse:
    """Проверяет ключ, адрес и имя модели пробным запросом и сохраняет."""
    data = await _body(request)
    state, page = state_of(request), _page(request)
    settings = page.settings
    if not settings.editable(ss.LLM_API_KEY):
        raise apply.Invalid("Ключ модели задан в настройках сервера. Изменить его можно только там.", "locked")
    config = state.config
    # Адрес из окружения задаёт оператор (там может быть локальная модель): он не проверяется
    # и со страницы не меняется. Адрес со страницы — только https и только наружу.
    from_page = settings.editable(ss.LLM_BASE_URL)
    base_url = apply.check_llm_url(data.get("base_url")) if from_page else config.llm_base_url
    model = apply.check_llm_model(data.get("model")) if settings.editable(ss.LLM_MODEL) else config.llm_model
    key = apply.clean(data.get("api_key"), limit=500)
    if not key:
        key = settings.store.get(ss.LLM_API_KEY)      # ключ уже введён раньше: меняют только модель
        if not key:
            raise apply.Invalid("Вставьте ключ API провайдера модели.", "empty")
        if base_url != config.llm_base_url:
            # Сохранённый ключ на новый адрес не уходит никогда: иначе тот, кто получил доступ
            # к странице, одной сменой адреса увёл бы ключ на свой сервер.
            await _log(request, "llm.save", audit.REFUSED, "смена адреса без ввода ключа")
            raise apply.Invalid("Вы меняете адрес API. Вставьте ключ заново: сохранённый ключ на новый адрес "
                                "не отправляется.", "key_required")
    keeper = executor_service.chatgpt_keeper(state)
    replacing = keeper.store.active()
    if replacing and _flag(data, "switch") is not True:
        # Своя модель работает одним способом: ключ заменит подписку — пусть владелец подтвердит.
        raise apply.Invalid("Сейчас своя модель сервиса работает по подписке ChatGPT. Ключ API её заменит: "
                            "сервис выйдет из подписки. Подтвердите замену.", "switch_needed")
    restricted = from_page and base_url != ss.DEFAULT_LLM_BASE_URL
    try:
        used = await apply.check_llm(config, api_key=key, base_url=base_url, model=model, restricted=restricted)
    except apply.Invalid as exc:
        await _log(request, "llm.save", audit.REFUSED, f"проверка не прошла: {exc.code}")
        raise
    changes: dict[str, str | None] = {ss.LLM_API_KEY: key}
    if settings.editable(ss.LLM_BASE_URL):
        changes[ss.LLM_BASE_URL] = None if base_url == ss.DEFAULT_LLM_BASE_URL else base_url
    if settings.editable(ss.LLM_MODEL):
        changes[ss.LLM_MODEL] = model
    if replacing:
        confirmed = await keeper.sign_out()
        await _log(request, "llm.chatgpt_removed", detail="заменена ключом API"
                   + ("" if confirmed else "; OpenAI отзыв сессии не подтвердил"))
    await settings.save(changes)
    await _log(request, "llm.save", detail="ключ проверен пробным запросом")
    return JSONResponse({"ok": True, "model": used})


@endpoint
async def llm_delete(request: Request) -> JSONResponse:
    await _body(request)
    settings = _page(request).settings
    names = [n for n in (ss.LLM_API_KEY, ss.LLM_BASE_URL, ss.LLM_MODEL) if settings.editable(n)]
    if ss.LLM_API_KEY not in names:
        raise apply.Invalid("Ключ модели задан в настройках сервера. Изменить его можно только там.", "locked")
    await settings.save({name: None for name in names})
    await _log(request, "llm.removed")
    return JSONResponse({"ok": True})


# --- 7а. своя модель по подписке ChatGPT --------------------------------------------------------
# Вход «вставкой адреса» (executor/siwc.py): сервер на другом компьютере, чем браузер владельца,
# поэтому адрес возврата http://127.0.0.1:1455/… в браузере не открывается — владелец копирует
# его из адресной строки сюда. Вставленный адрес несёт код входа: он не пишется ни в журнал
# сервиса, ни в журнал действий, ни в ответ.

def _subscription_allowed(page: Page, state: AppState) -> None:
    if not page.settings.editable(ss.LLM_API_KEY) and not state.config.chatgpt:
        raise apply.Invalid(apply.SUBSCRIPTION_LOCKED, "locked")


def _siwc_invalid(exc: siwc.SiwcError) -> apply.Invalid:
    return apply.Invalid(apply.siwc_problem_text(exc.code), exc.code.split(":")[0])


@endpoint
async def chatgpt_start(request: Request) -> JSONResponse:
    """Новая попытка входа. Прежняя, если была, снимается. Возвращает адрес входа — его страница
    открывает в новой вкладке браузера владельца."""
    data = await _body(request)
    state, page = state_of(request), _page(request)
    _subscription_allowed(page, state)
    switch = _flag(data, "switch") is True
    if apply.page_key_set(page.settings) and not switch:
        raise apply.Invalid(apply.SWITCH_NEEDED, "switch_needed")
    store = executor_service.chatgpt_keeper(state).store
    async with page.chatgpt_lock:
        attempt, url = siwc.new_attempt(store, new_registration=_flag(data, "new") is True, switch=switch)
        page.chatgpt_attempt = attempt
    await _log(request, "llm.chatgpt_start",
               detail="новая регистрация" if attempt.client_id is None else "повторный вход")
    return JSONResponse({"url": url, "expires_at": _iso(attempt.expires_at), "minutes": siwc.ATTEMPT_TTL // 60,
                         "first": attempt.client_id is None})


@endpoint
async def chatgpt_cancel(request: Request) -> JSONResponse:
    await _body(request)
    page = _page(request)
    async with page.chatgpt_lock:
        page.chatgpt_attempt = None
    return JSONResponse({"ok": True})


async def _discard(keeper: siwc.TokenKeeper, tokens: dict[str, Any], client_id: str) -> None:
    """Отзывает сессию, которую выдал вход, если она не пригодилась."""
    try:
        async with keeper.http() as http:
            await siwc.revoke(http, {"refresh_token": tokens.get("refresh_token"), "client_id": client_id})
    except siwc.SiwcError:
        pass


@endpoint
async def chatgpt_finish(request: Request) -> JSONResponse:
    """Принимает вставленный адрес, проверяет его по попытке, меняет код на токены, проверяет
    ID token и разрешение пользоваться подпиской, получает список моделей, задаёт модели пробный
    вопрос — и только тогда включает подписку своей моделью сервиса."""
    data = await _body(request)
    state, page = state_of(request), _page(request)
    keeper = executor_service.chatgpt_keeper(state)
    store = keeper.store
    async with page.chatgpt_lock:
        attempt = page.chatgpt_attempt
        try:
            params = siwc.parse_callback(data.get("address"))
            code, client_id = siwc.check_callback(attempt, params)
        except siwc.SiwcError as exc:
            if attempt is not None and (exc.code != "wrong_state" or attempt.tries >= siwc.ATTEMPT_TRIES):
                if exc.code not in ("empty", "bad_address", "not_callback", "no_params"):
                    page.chatgpt_attempt = None       # ответ этой попытки получен — она больше не нужна
            await _log(request, "llm.chatgpt", audit.REFUSED, f"адрес не подошёл: {exc.code.split(':')[0]}")
            raise _siwc_invalid(exc) from None
        page.chatgpt_attempt = None                   # код одноразовый: попытка использована
    assert attempt is not None
    _subscription_allowed(page, state)
    if apply.page_key_set(page.settings) and not attempt.switch:
        await _log(request, "llm.chatgpt", audit.REFUSED, "ключ API введён во время входа")
        raise apply.Invalid(apply.SWITCH_NEEDED, "switch_needed")
    previous = store.load()
    try:
        async with keeper.http() as http:
            tokens = await siwc.exchange(http, attempt, code, client_id)
            try:
                claims = await siwc.verify_id_token(http, tokens["id_token"], client_id=client_id, nonce=attempt.nonce)
            except siwc.SiwcError:
                await siwc.revoke(http, {"refresh_token": tokens["refresh_token"], "client_id": client_id})
                raise
            if attempt.subject and claims["sub"] != attempt.subject:
                await siwc.revoke(http, {"refresh_token": tokens["refresh_token"], "client_id": client_id})
                raise siwc.SiwcError("other_account")
            registration = {
                **{k: previous.get(k) for k in ("model", "models") if previous.get("client_id") == client_id},
                "email": claims.get("email") if isinstance(claims.get("email"), str) else None,
                "issuer": siwc.ISSUER, "subject": claims["sub"], "client_id": client_id,
                "ext_agent_host_id": attempt.host_id, "status": siwc.SIGNED_OUT,
            }
            if not siwc.plan_allowed(tokens):
                await siwc.revoke(http, {"refresh_token": tokens["refresh_token"], "client_id": client_id})
                await keeper.save_signed_in({**registration, "need_consent": True})
                raise siwc.SiwcError("no_plan")
            models = await siwc.list_models(http, tokens["access_token"])
    except siwc.SiwcError as exc:
        await _log(request, "llm.chatgpt", audit.REFUSED, f"вход не удался: {exc.code.split(':')[0]}")
        raise _siwc_invalid(exc) from None
    if not models:
        await _discard(keeper, tokens, client_id)
        await keeper.save_signed_in(registration)
        await _log(request, "llm.chatgpt", audit.REFUSED, "нет моделей")
        raise apply.Invalid(apply.siwc_problem_text("no_models"), "no_models")
    slugs = [m["slug"] for m in models]
    model = previous.get("model") if previous.get("client_id") == client_id and previous.get("model") in slugs \
        else slugs[0]
    outcome, problem = await apply.probe_subscription(state.config, tokens["access_token"], model)
    if outcome == "hard":
        await _discard(keeper, tokens, client_id)
        await keeper.save_signed_in({**registration, "models": models, "model": model})
        await _log(request, "llm.chatgpt", audit.REFUSED, f"пробный вопрос: {(problem or '').split(':')[0]}")
        raise apply.Invalid(apply.subscription_problem_text(problem) + " Своя модель не переключена.",
                            "probe_failed")
    record = siwc.record_from_tokens(tokens, claims, client_id=client_id, host_id=attempt.host_id, previous=previous)
    record.update(model=model, models=models)
    record.pop("need_consent", None)
    await keeper.save_signed_in(record)
    names = apply.key_names(page.settings)
    if apply.page_key_set(page.settings) and names:
        await page.settings.save({name: None for name in names})       # подписка заменяет ключ
        await _log(request, "llm.removed", detail="заменён подпиской ChatGPT")
    else:
        await page.settings.refresh(restart=True)
    await _log(request, "llm.chatgpt", detail=("пробный вопрос: ответ получен" if outcome == "ok" else
                                               "пробный вопрос: лимит подписки исчерпан" if outcome == "limit" else
                                               f"пробный вопрос не прошёл: {(problem or '').split(':')[0]}"))
    return JSONResponse({
        "ok": True, "email": record.get("email"), "model": model, "models": models,
        "probe": outcome,
        "probe_text": None if outcome == "ok" else apply.subscription_problem_text(problem),
    })


@endpoint
async def chatgpt_model(request: Request) -> JSONResponse:
    """Модель подписки — из списка, который OpenAI сейчас даёт этой учётной записи."""
    data = await _body(request)
    state, page = state_of(request), _page(request)
    keeper = executor_service.chatgpt_keeper(state)
    slug = data.get("model")
    if not keeper.store.active():
        raise apply.Invalid(apply.siwc_problem_text("not_connected"), "not_connected")
    if not isinstance(slug, str) or not slug:
        raise apply.Invalid(apply.siwc_problem_text("unknown_model"), "unknown_model")
    try:
        token = await keeper.access_token()
        async with keeper.http() as http:
            models = await siwc.list_models(http, token)
    except siwc.SiwcError as exc:
        if isinstance(exc, siwc.NeedLogin):
            await page.settings.refresh()
        raise _siwc_invalid(exc) from None
    if slug not in [m["slug"] for m in models]:
        raise apply.Invalid(apply.siwc_problem_text("unknown_model"), "unknown_model")
    async with keeper.lock:
        record = keeper.store.load()
        record.update(model=slug, models=models)
        keeper.store.save(record)
    await page.settings.refresh()
    await _log(request, "llm.chatgpt_model", detail=f"модель: {slug}")
    return JSONResponse({"ok": True, "model": slug, "models": models})


@endpoint
async def chatgpt_remove(request: Request) -> JSONResponse:
    """Выход из подписки: отзыв сессии у OpenAI, токены стираются. Выданный client_id и номер
    сервера остаются для следующего входа."""
    await _body(request)
    state, page = state_of(request), _page(request)
    keeper = executor_service.chatgpt_keeper(state)
    if keeper.store.status() == "none":
        raise apply.Invalid(apply.siwc_problem_text("not_connected"), "not_connected")
    confirmed = await keeper.sign_out()
    async with page.chatgpt_lock:
        page.chatgpt_attempt = None
    await page.settings.refresh()
    await _log(request, "llm.chatgpt_removed",
               detail="" if confirmed else "OpenAI отзыв сессии не подтвердил")
    return JSONResponse({"ok": True, "revoked": confirmed})


# --- сборка ------------------------------------------------------------------------------------

def routes() -> list[BaseRoute]:
    account = API + "/tg/accounts/{account_id:int}"
    upload = API + "/imports/{import_id}"
    return [
        Route(PREFIX + "/", index, methods=["GET"]),
        Route(PREFIX + "/static/{name}", static_file, methods=["GET"]),
        Route(API + "/session", session_info, methods=["GET"]),
        Route(API + "/login/link", login_link, methods=["POST"]),
        Route(API + "/login/code/request", login_code_request, methods=["POST"]),
        Route(API + "/login/code", login_code, methods=["POST"]),
        Route(API + "/logout", logout, methods=["POST"]),
        Route(API + "/logout-all", logout_all, methods=["POST"]),
        Route(API + "/state", page_state, methods=["GET"]),
        Route(API + "/overview", page_overview, methods=["GET"]),
        Route(API + "/scenario", scenario_save, methods=["PUT"]),
        Route(API + "/bot/token", bot_token_save, methods=["POST"]),
        Route(API + "/bot/token", bot_token_delete, methods=["DELETE"]),
        Route(API + "/bot/bind", bot_bind, methods=["POST"]),
        Route(API + "/bot/refresh", bot_refresh, methods=["POST"]),
        Route(API + "/tg/keys", tg_keys_save, methods=["PUT"]),
        Route(API + "/tg/keys", tg_keys_delete, methods=["DELETE"]),
        Route(API + "/tg/login", tg_login_start, methods=["POST"]),
        Route(API + "/tg/login/{login_id}", tg_login_status, methods=["GET"]),
        Route(API + "/tg/login/{login_id}/password", tg_login_password, methods=["POST"]),
        Route(API + "/tg/login/{login_id}/cancel", tg_login_cancel, methods=["POST"]),
        Route(account + "/pause", tg_pause, methods=["POST"]),
        Route(account + "/resume", tg_resume, methods=["POST"]),
        Route(account + "/logout", tg_logout, methods=["POST"]),
        Route(account, tg_forget, methods=["DELETE"]),
        Route(account + "/options", tg_options, methods=["PUT"]),
        Route(account + "/dialogs", tg_dialogs, methods=["GET"]),
        Route(account + "/sync", tg_sync, methods=["POST"]),
        Route(account + "/exclude", tg_exclude, methods=["POST"]),
        Route(API + "/imports", imports_upload, methods=["POST"]),
        Route(API + "/imports", imports_list, methods=["GET"]),
        Route(upload, imports_status, methods=["GET"]),
        Route(upload, imports_delete, methods=["DELETE"]),
        Route(upload + "/scan", imports_scan, methods=["GET"]),
        Route(upload + "/run", imports_run, methods=["POST"]),
        Route(API + "/llm", llm_save, methods=["PUT"]),
        Route(API + "/llm", llm_delete, methods=["DELETE"]),
        Route(API + "/llm/chatgpt/start", chatgpt_start, methods=["POST"]),
        Route(API + "/llm/chatgpt/finish", chatgpt_finish, methods=["POST"]),
        Route(API + "/llm/chatgpt/cancel", chatgpt_cancel, methods=["POST"]),
        Route(API + "/llm/chatgpt/model", chatgpt_model, methods=["PUT"]),
        Route(API + "/llm/chatgpt", chatgpt_remove, methods=["DELETE"]),
    ]


def make_shield(app: Any, config: Any) -> shield.Shield:
    """Общая защита префикса; её ставит `app.build_app` перед маршрутами страницы."""
    return shield.Shield(app, config)


JANITOR_EVERY = 3600      # секунд


@contextlib.asynccontextmanager
async def lifespan(state: AppState) -> AsyncIterator[None]:
    page = Page(settings=apply.Settings(state), key=ss.signing_key(state.config.data_dir))
    state.extras[summary.EXTRAS_KEY] = page

    async def janitor() -> None:
        while True:
            try:
                async with state.pool.acquire() as conn:
                    await auth.cleanup(conn)
                    await audit.trim(conn)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("страница настройки: уборка не выполнена (%s)", type(exc).__name__)
            await asyncio.sleep(JANITOR_EVERY)

    state.spawn(janitor(), name="setup-janitor")
    reason = state.config.setup_reason
    if reason == "same_origin":
        logger.error(
            "страница настройки: её адрес (SHTURMAN_SETUP_ORIGIN) совпадает с адресом дашборда Hermes "
            "(SHTURMAN_DASHBOARD_ORIGIN) — по внешнему адресу страница НЕ отдаётся; дайте ей другой порт "
            "или другое имя. Под локальными именами страница работает")
    elif reason == "no_origin":
        logger.info("страница настройки: внешний адрес не задан, страница отвечает только под локальными именами")
    else:
        logger.info("страница настройки: внешний адрес %s, вход — по ссылке из ./ops/setup-link.sh",
                    state.config.setup_external)
    try:
        yield
    finally:
        state.extras.pop(summary.EXTRAS_KEY, None)



