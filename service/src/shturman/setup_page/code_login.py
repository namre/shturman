"""Вход на страницу настройки по коду из бота согласований (П4, решение Р-88).

Правила кода (6 цифр, 5 минут, один раз, привязка к браузеру, пределы) — в `auth.py`. Здесь — то,
что вокруг них:

  * **включён ли вход по коду.** Бот создан владельцем у @BotFather (или токен задан на сервере) —
    включён по умолчанию; создан «в одно нажатие» через посредника (Р-77, строка `approvals-origin` в
    `./ops/doctor.sh`) — выключен по умолчанию: держатель посредника технически может читать сообщения
    бота, а значит и коды. Решение владельца хранится в `setup_state.code_login` вместе с номером бота:
    для другого бота снова действует умолчание. Выключить — со страницы, сразу (ужесточение). Включить
    при боте от посредника — только нажатием владельца в боте (`setup.code_login`, `confirm.request_card`,
    `BOT_ONLY`); при своём боте — со страницы, сразу: это действие самого владельца;
  * **сообщение с кодом** — код, когда и откуда запрошен (время, адрес и браузер словами),
    «Никому не сообщайте код, даже ассистенту» и две кнопки: «Это не я» (гасит код и все ждущие попытки,
    запись в журнал) и «Выйти на всех устройствах» (все сессии страницы и приложения, `auth.revoke_all`);
  * **адрес и браузер** — только для этого сообщения. Адрес берётся у обратного прокси (последний
    адрес `X-Forwarded-For`, который дописывает сам прокси) или у соединения; никакое решение от него не
    зависит, в базу и журнал он не пишется.

Агенту и дашборду Hermes здесь ничего не доступно: маршрутов во внутреннем API нет, запросить или
ввести код можно только на адресе страницы (`shield.py` пускает сюда только её origin).

  GET  code-login    {enabled, default, origin, reason, can_enable_now, logins_7d}
  PUT  code-login    {enabled: false} — сразу; {enabled: true} — сразу при своём боте, иначе карточка в бот
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

import asyncpg
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from .. import authority, bridge, confirm, words
from ..api_core import BadRequest
from ..app import AppState, state_of
from ..executor import binding
from . import auth

logger = logging.getLogger("shturman.setup")

STATE_KEY = "code_login"
KIND = "setup.code_login"
CALLBACK_MODULE = "lc"
NOT_ME = "Это не я"
LOGOUT_ALL = "Выйти на всех устройствах"


# --- включён ли вход по коду ----------------------------------------------------------------------

async def setting(conn: asyncpg.Connection, bot_id: int | None) -> dict[str, Any]:
    """{enabled, default, origin, reason}. reason: None (включён), managed (выключен по умолчанию: бот
    от посредника), off (выключил владелец), no_bot (бота нет)."""
    if bot_id is None:
        return {"enabled": False, "default": False, "origin": None, "reason": "no_bot"}
    origin = (await binding.bot_origin(conn, bot_id) or {}).get("created_via")
    default = origin != "managed"
    raw = await conn.fetchval("SELECT value FROM setup_state WHERE key = $1", STATE_KEY)
    stored = json.loads(raw) if isinstance(raw, str) else raw
    enabled = default
    if isinstance(stored, dict) and stored.get("bot_id") == bot_id and isinstance(stored.get("enabled"), bool):
        enabled = stored["enabled"]
    reason = None if enabled else ("managed" if origin == "managed" and enabled == default else "off")
    return {"enabled": enabled, "default": default, "origin": origin, "reason": reason}


async def save(conn: asyncpg.Connection, bot_id: int, enabled: bool) -> None:
    await conn.execute(
        """INSERT INTO setup_state (key, value, updated_at) VALUES ($1, $2::jsonb, now())
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
        STATE_KEY, json.dumps({"enabled": enabled, "bot_id": bot_id}))


async def _current_bot(conn: asyncpg.Connection) -> int | None:
    """Номер бота согласований, которым сейчас работает сервис (его записывает сам бот при запуске)."""
    bot = await binding.get_state(conn, "bot")
    return bot.get("id") if isinstance(bot, dict) and isinstance(bot.get("id"), int) else None


@confirm.applier(KIND)
async def _apply(conn: asyncpg.Connection, payload: dict[str, Any]) -> confirm.Done:
    """Включение входа по коду при боте от посредника — нажато владельцем в боте (`BOT_ONLY`)."""
    principal = authority.requires_owner()
    if principal.source != "telegram" or principal.action == "telegram.miniapp":
        raise confirm.Refused("Включить вход по коду можно только нажатием в боте согласований.", 403, "bot_only")
    bot_id = payload.get("bot_id")
    if not isinstance(bot_id, int) or bot_id != await _current_bot(conn):
        raise confirm.Refused("Бот согласований сменился — запросите включение заново.", 409, "changed_meanwhile")
    await save(conn, bot_id, True)
    from . import audit
    await audit.write_conn(conn, "login.code_setting", detail="включён; подтверждено в боте")
    return confirm.Done(note="Вход на страницу по коду из бота включён.", result={"enabled": True})


# --- откуда запрошен код ----------------------------------------------------------------------------

def client_address(request: Request) -> str | None:
    """Адрес, с которого запросили код, — только для сообщения владельцу. За обратным прокси — последний
    адрес `X-Forwarded-For` (его дописывает сам прокси), иначе — адрес соединения."""
    peer = request.client.host if request.client else None
    try:
        local = peer is None or ipaddress.ip_address(peer).is_loopback
    except ValueError:
        local = True
    if local:
        chain = ",".join(request.headers.getlist("x-forwarded-for"))
        last = chain.rsplit(",", 1)[-1].strip() if chain else ""
        try:
            return str(ipaddress.ip_address(last)) if last else None
        except ValueError:
            return None
    return peer


_BROWSERS = (("YaBrowser", "Яндекс Браузер"), ("Edg", "Edge"), ("OPR", "Opera"), ("Firefox", "Firefox"),
             ("FxiOS", "Firefox"), ("CriOS", "Chrome"), ("Chrome", "Chrome"), ("Safari", "Safari"))
_SYSTEMS = (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Windows", "Windows"),
            ("Mac OS X", "macOS"), ("Macintosh", "macOS"), ("CrOS", "ChromeOS"), ("Linux", "Linux"))


def browser_words(user_agent: str | None) -> str:
    """«Chrome на Windows» по заголовку User-Agent. Незнакомое — «неизвестный браузер»."""
    ua = user_agent or ""
    browser = next((name for mark, name in _BROWSERS if mark in ua), None)
    system = next((name for mark, name in _SYSTEMS if mark in ua), None)
    if browser and system:
        return f"{browser} на {system}"
    return browser or (f"браузер на {system}" if system else "неизвестный браузер")


def _when(config: Any) -> str:
    from ..executor import owner_views
    return owner_views.moment(datetime.now(timezone.utc), config)


def message(code: str, request: Request, config: Any) -> str:
    address = client_address(request)
    where = f"с адреса {address}" if address else "адрес не известен"
    return (f"🔐 Код входа в «{words.SETUP_PAGE}»: {code[:3]} {code[3:]}\n\n"
            f"Запрошен {_when(config)}, {where}, браузер: {browser_words(request.headers.get('user-agent'))}.\n"
            "Действует 5 минут и только в том браузере, где его запросили.\n\n"
            "Никому не сообщайте код, даже ассистенту.\n"
            f"Если вы не запрашивали код — нажмите «{NOT_ME}».")


def message_buttons(code_id: int, nonce: str) -> list[list[tuple[str, str]]]:
    return [[(NOT_ME, bridge.callback_data(CALLBACK_MODULE, f"x:{code_id}:{nonce}"))],
            [(LOGOUT_ALL, bridge.callback_data(CALLBACK_MODULE, f"o:{code_id}:{nonce}"))]]


# --- «Это не я» и «Выйти на всех устройствах» ------------------------------------------------------

@bridge.on_callback(CALLBACK_MODULE)
async def _pressed(conn: asyncpg.Connection, rest: str, user_id: int) -> dict[str, Any]:
    principal = authority.requires_owner()
    refused = {"answer": "Кнопка недоступна.", "edit_text": None, "remove_buttons": False}
    if principal.user_id != user_id or principal.source != "telegram":
        return refused
    choice, _, tail = rest.partition(":")
    raw_id, _, nonce = tail.partition(":")
    if choice not in ("x", "o") or not raw_id.isascii() or not raw_id.isdigit() or len(raw_id) > 18:
        return refused
    row = await conn.fetchrow("SELECT id, nonce FROM setup_codes WHERE id = $1", int(raw_id))
    if row is None or not hmac.compare_digest(row["nonce"], nonce):
        return {**refused, "answer": "Это сообщение устарело."}
    from . import audit
    if choice == "x":
        cancelled = await auth.cancel_codes(conn, "not_me")
        await audit.write_conn(conn, "login.code_denied", audit.REFUSED,
                               f"«Это не я» в боте; погашено попыток: {cancelled}")
        return {"answer": "Код погашен.", "remove_buttons": False,
                "edit_text": ("Код погашен: «Это не я». Все ждущие попытки входа по коду отменены — этим кодом "
                              "больше не войти.\n\nЕсли на странице уже кто-то вошёл, нажмите "
                              f"«{LOGOUT_ALL}» — ниже."),
                "buttons": [[{"text": LOGOUT_ALL, "data": bridge.callback_data(CALLBACK_MODULE, f"o:{row['id']}:{nonce}")}]]}
    sessions = await auth.revoke_all(conn)
    await audit.write_conn(conn, "logout.all", detail=f"из бота, по кнопке под кодом; сессий завершено: {sessions}")
    return {"answer": "Все входы завершены.", "remove_buttons": True,
            "edit_text": (f"Выполнен выход на всех устройствах: «{words.SETUP_PAGE}» и приложение «Штурман». "
                          "Ждущие коды погашены. Войти снова — по новому коду или по ссылке с сервера.")}


# --- страница: включить и выключить ------------------------------------------------------------------

async def view(state: AppState) -> dict[str, Any]:
    from . import service as page
    bot, _ = page._runtime_bot(state)
    bot_id = bot.bot_id if bot is not None else None
    async with state.ro_pool.acquire() as conn:
        out = await setting(conn, bot_id)
        out["logins_7d"] = await auth.logins_by_code(conn)
        out["owner_bound"] = bot_id is not None and await binding.bound_owner(conn, bot_id) is not None
        waiting = await conn.fetchval(
            """SELECT id FROM pending_actions WHERE kind = $1 AND status = 'pending' AND expires_at > now()
               ORDER BY id DESC LIMIT 1""", KIND)
    out["enable_pending"] = waiting
    out["needs_bot_press"] = out["origin"] == "managed"
    return out


def _routes_endpoint():
    from . import service as page
    return page


async def code_login_get(request: Request) -> JSONResponse:
    return JSONResponse(await view(state_of(request)))


async def code_login_put(request: Request) -> JSONResponse:
    page = _routes_endpoint()
    data = await page._body(request)
    enabled = page._flag(data, "enabled", required=True)
    state = state_of(request)
    bot, _ = page._runtime_bot(state)
    if bot is None or bot.bot_id is None:
        raise BadRequest("Сначала настройте бота согласований.", 409, "no_bot")
    async with state.pool.acquire() as conn:
        current = await setting(conn, bot.bot_id)
        if not enabled:
            await save(conn, bot.bot_id, False)
            cancelled = await auth.cancel_codes(conn, "disabled")
            await page._log(request, "login.code_setting", detail=f"выключен; погашено попыток: {cancelled}")
            return JSONResponse({"ok": True, **await view(state)})
        if current["enabled"]:
            return JSONResponse({"ok": True, **await view(state)})
        if current["origin"] != "managed":
            await save(conn, bot.bot_id, True)
            await page._log(request, "login.code_setting", detail="включён")
            return JSONResponse({"ok": True, **await view(state)})
        summary = ("Включить вход в «Настройки Штурмана» по коду из этого бота.\n\nБот согласований создан «в одно "
                   "нажатие» через посредника: его держатель технически может читать сообщения бота — и коды "
                   "входа. Включайте, только если доверяете ему. Код действует только в том браузере, где его "
                   "запросили, и только 5 минут.")
        try:
            out = await confirm.request_card(conn, KIND, summary, {"bot_id": bot.bot_id})
        except confirm.NoOwner:
            raise BadRequest("Подтвердить можно только в боте согласований, а владелец к нему не привязан.",
                             409, "owner_unknown") from None
    await page._log(request, "login.code_setting", detail="запрошено включение — ждёт нажатия в боте")
    return JSONResponse({"ok": True, "status": out["status"], "action_id": out.get("action_id"),
                         "note": "Подтвердите включение в боте согласований — карточка уже там.",
                         **await view(state)}, status_code=202)


def routes() -> list[BaseRoute]:
    page = _routes_endpoint()
    return [
        Route(page.API + "/code-login", page.endpoint(code_login_get), methods=["GET"]),
        Route(page.API + "/code-login", page.endpoint(code_login_put), methods=["PUT"]),
    ]


def well_formed_cookie(value: Any) -> bool:
    return auth.well_formed(value)


_COOKIE_PAIR = re.compile(r"\s*([^=;\s]+)\s*=\s*([^;]*)")


def attempt_cookie(request: Request) -> str | None:
    """Идентификатор попытки из cookie `__Secure-shturman-code`. Ровно одно значение — иначе нет:
    вторая cookie с тем же именем значит, что кто-то подложил свою (соседний порт того же имени)."""
    found: list[str] = []
    for header in request.headers.getlist("cookie"):
        for part in header.split(";"):
            match = _COOKIE_PAIR.match(part)
            if match and match.group(1) == auth.CODE_COOKIE:
                found.append(match.group(2).strip())
    return found[0] if len(found) == 1 and auth.well_formed(found[0]) else None


def set_cookie(response: JSONResponse, value: str | None) -> None:
    """Ставит (или стирает, value=None) cookie попытки. Только на пути входа по коду."""
    base = f"{auth.CODE_COOKIE}={value or ''}; Path={auth.CODE_PATH}; HttpOnly; Secure; SameSite=Strict"
    response.headers.append("set-cookie", base + (f"; Max-Age={auth.CODE_TTL}" if value else "; Max-Age=0"))
