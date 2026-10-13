"""Бот согласований: применение токена и создание бота в одно нажатие (Р-77, docs/approvals-bot.md).

Два способа получить своего бота согласований:

  * **вручную** (по умолчанию, ни от кого не зависит): владелец создаёт бота у @BotFather и
    вставляет токен на странице;
  * **в одно нажатие** через бот-менеджер посредника: сервис просит у посредника пару
    (`POST <SHTURMAN_BOT_MANAGER_API>/api/botpair/start`), страница показывает ссылку
    `https://t.me/newbot/<менеджер>/<имя>?name=…` кнопкой и QR-кодом, владелец подтверждает
    создание в Telegram, а сервис — сам, не браузер — забирает токен у посредника
    (`POST …/api/botpair/<pair_id>/token {secret}`). Секрет пары есть только в памяти сервиса;
    в браузер, журнал и базу он не попадает, как и токен.

Токен из обоих способов применяется **одним и тем же путём** — `apply_token`: проверка `getMe`,
проверка, что ботом не пользуется другая программа (409 или webhook), запись в файл значений
страницы и перезапуск исполнителя. Для бота, созданного в одно нажатие, сверх этого: токен
должен принадлежать именно тому боту, о котором сообщил посредник, а привязать себя владельцем
может только аккаунт-создатель (`executor/binding.required_creator`).

Чем владелец платит за одно нажатие: держатель посредника (бот-менеджера) технически может в любой
момент и незаметно получить токен этого бота (`getManagedBotToken`) — а с ним подделать вход
в Mini App и нажатия в нём, читать то, что приходит боту, и, если владелец подключит бота
в бизнес-режиме, его личные переписки. Это говорится владельцу до кнопки. Перейти на своего бота
можно в любой момент: создать бота у @BotFather и заменить токен на странице.

Адрес посредника — только из окружения (`SHTURMAN_BOT_MANAGER_API`): ни страница, ни внутренний
API его не меняют. Запустить создание может только сессия страницы настройки.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .. import authority
from ..executor import binding
from ..executor import service as executor_service
from . import apply, audit
from . import secrets_store as ss

logger = logging.getLogger("shturman.setup")

POLL_EVERY = 3.0                 # секунд между вопросами посреднику «бот создан?»
DEFAULT_NAME = "Согласования"
_PAIR = re.compile(r"^[a-z0-9]{6,32}$")
_SECRET = re.compile(r"^[A-Za-z0-9_-]{40,64}$")
_USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,30}[Bb][Oo][Tt]$")
_MANAGER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_TOKEN = re.compile(r"^[0-9]{5,16}:[A-Za-z0-9_-]{30,80}$")

WAITING, APPLYING, DONE, FAILED = "waiting", "applying", "done", "failed"

TEXTS = {
    "off": "Создание бота в одно нажатие на этом сервере выключено. Создайте бота у @BotFather — шаги выше.",
    "no_connection": "Не удалось связаться с посредником. Проверьте доступ сервера в интернет или создайте бота "
                     "у @BotFather вручную.",
    "manager_unavailable": "Бот-менеджер посредника сейчас не работает. Создайте бота у @BotFather вручную.",
    "busy": "Посредник сейчас занят. Повторите через несколько минут или создайте бота вручную.",
    "rate_limited": "Посредник просит подождать: слишком много попыток с этого сервера. Повторите позже "
                    "или создайте бота вручную.",
    "invite_required": "Посредник создаёт ботов только по коду приглашения. Введите код, который дал держатель "
                       "посредника, или создайте бота вручную.",
    "bad_answer": "Посредник ответил непонятно. Создайте бота у @BotFather вручную.",
    "expired": "Время ожидания вышло, а бот так и не появился. Если вы меняли адрес бота (@имя) в окне "
               "создания, посредник не узнает бота: удалите его у @BotFather (/deletebot) и начните заново, "
               "не меняя адрес, — или создайте бота вручную.",
    "conflict": "Пока бот создавался, у него сменился владелец. Удалите этого бота у @BotFather и начните заново.",
    "session_ended": "Бот создан, но ваш вход на эту страницу завершился раньше, и токен не применён. "
                     "Токен этого бота можно взять у @BotFather (/mybots → бот → API Token) и вставить вручную, "
                     "либо удалить бота (/deletebot) и начать заново.",
    "wrong_bot": "Посредник прислал токен не того бота, о котором сообщил. Токен не сохранён. Создайте бота "
                 "у @BotFather вручную и сообщите об этом держателю посредника.",
}


def available(config: Any) -> bool:
    return bool(getattr(config, "bot_manager_api", ""))


def manager_host(config: Any) -> str | None:
    api = getattr(config, "bot_manager_api", "")
    return urlsplit(api).netloc if api else None


# Готов ли бот-менеджер у посредника (`GET /api/health` → `botpair`). Спрашивается в фоне не чаще
# раза в 10 минут, пока открыта страница: кнопку, которая сейчас не сработает, страница не показывает.
HEALTH_EVERY = 600.0
_health: dict[str, tuple[float, bool | None]] = {}
_checking: set[str] = set()


def manager_ready(state: Any) -> bool | None:
    """True — посредник создаёт ботов; False — нет (бот-менеджер не задан, старая служба или
    выключено); None — ещё не известно или посредник не ответил."""
    api = getattr(state.config, "bot_manager_api", "")
    if not api:
        return False
    at, ready = _health.get(api, (0.0, None))
    if (not at or time.monotonic() - at > HEALTH_EVERY) and api not in _checking:
        _checking.add(api)
        state.spawn(_check_health(state, api), name="setup-managed-health")
    return ready


def _remember(api: str, ready: bool | None) -> None:
    _health[api] = (time.monotonic(), ready)


async def _check_health(state: Any, api: str) -> None:
    ready: bool | None = None
    try:
        async with _client(state.config) as http:
            response = await http.get("/api/health")
        ready = _answer(response).get("botpair") is True if response.status_code == 200 else False
    except (httpx.HTTPError, apply.Invalid):
        ready = None
    finally:
        _checking.discard(api)
    _remember(api, ready)


# --- общий путь токена ---------------------------------------------------------------------------

def runtime_bot(state: Any) -> tuple[Any, Any]:
    runtime = state.extras.get("executor")
    return getattr(runtime, "bot", None), getattr(runtime, "api", None)


async def record_origin(conn: Any, bot_id: int, via: str, **extra: Any) -> None:
    value = {"bot_id": bot_id, "created_via": via, "at": int(time.time()),
             **{k: v for k, v in extra.items() if v is not None}}
    await conn.execute(
        """INSERT INTO setup_state (key, value, updated_at) VALUES ($1, $2::jsonb, now())
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
        binding.ORIGIN_KEY, json.dumps(value, ensure_ascii=False))


async def apply_token(state: Any, page: Any, token: str, *, via: str, expect: dict | None = None,
                      origin: dict | None = None, detail: str = "бот проверен запросом к Telegram") -> dict[str, Any]:
    """Проверить токен бота согласований и начать им пользоваться — один путь для токена,
    вставленного владельцем (via="manual"), и полученного от бота-менеджера (via="managed").

    expect — {id, username} бота, о котором сообщил посредник: токен должен быть его.
    origin — что записать о создании бота (creator_user_id, creator_name, restricted, manager).
    Отказы — apply.Invalid с текстом для владельца; отказ проверки «другая программа» пишется
    в журнал действий, как и успех."""
    if not page.settings.editable(ss.BOT_TOKEN):
        raise apply.Invalid("Токен бота задан в настройках сервера. Изменить его можно только там.", "locked")
    bot = await apply.check_bot_token(state.config, token)
    if expect is not None and (bot["id"] != expect.get("id")
                               or bot["username"].lower() != str(expect.get("username") or "").lower()):
        await audit.write(state.pool, "bot.token", audit.REFUSED, "токен не того бота, о котором сообщил посредник")
        raise apply.Invalid(TEXTS["wrong_bot"], "wrong_bot")
    running, _ = runtime_bot(state)
    same = running is not None and running.bot_id == bot["id"] and running.polling is True
    if not same:
        # Этого бота сервис ещё не опрашивает: проверяем, не опрашивает ли его кто-то другой.
        try:
            await apply.probe_other_poller(state.config, token)
        except apply.Invalid as exc:
            why = "у бота включён webhook" if exc.code == "webhook" else "ботом уже пользуется другая программа"
            await audit.write(state.pool, "bot.token", audit.REFUSED, why)
            raise
    async with state.pool.acquire() as conn:
        # Запись — до токена: когда бот выйдет на связь, правило «привязать только создателя» уже действует.
        await record_origin(conn, bot["id"], via, **(origin or {}))
    await page.settings.save({ss.BOT_TOKEN: token})
    await audit.write(state.pool, "bot.token", audit.OK, detail)
    return {"username": bot["username"], "name": bot["name"]}


# --- в одно нажатие ------------------------------------------------------------------------------

@dataclass
class Pairing:
    """Начатое создание бота в одно нажатие. Живёт в памяти сервиса; перезапуск его снимает."""
    pair_id: str
    secret: str = field(repr=False)
    link: str = ""
    username: str = ""
    manager: str = ""
    expires_at: float = 0.0
    session_id: int = 0
    status: str = WAITING
    code: str | None = None
    message: str | None = None
    bot_username: str | None = None
    creator_name: str | None = None
    restricted: bool | None = None
    task: asyncio.Task | None = field(default=None, repr=False)

    def view(self) -> dict[str, Any]:
        """Для страницы. Секрета пары здесь нет."""
        return {"status": self.status, "link": self.link if self.status == WAITING else None,
                "username": self.username, "manager": self.manager,
                "expires_at": self.expires_at, "seconds_left": max(0, int(self.expires_at - time.time())),
                "code": self.code, "message": self.message, "bot_username": self.bot_username,
                "creator_name": self.creator_name, "restricted": self.restricted}


def _client(config: Any) -> httpx.AsyncClient:
    transport = executor_service.TEST_OVERRIDES.get("botpair_transport")
    if transport is None:
        try:
            transport = httpx.AsyncHTTPTransport(proxy=config.proxy_url or None, trust_env=False, retries=0)
        except ImportError:
            raise apply.Invalid(TEXTS["no_connection"], "proxy_needs_socksio") from None
        except Exception:  # noqa: BLE001
            raise apply.Invalid(TEXTS["no_connection"], "bad_proxy_url") from None
    return httpx.AsyncClient(transport=transport, base_url=config.bot_manager_api, trust_env=False,
                             follow_redirects=False, timeout=httpx.Timeout(15.0, connect=10.0))


def _answer(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def start(state: Any, page: Any, session_id: int, *, name: Any = None, code: Any = None) -> Pairing:
    """Попросить у посредника пару и начать ждать бота. Прежняя попытка, если была, снимается."""
    config = state.config
    if not available(config):
        raise apply.Invalid(TEXTS["off"], "off")
    if not page.settings.editable(ss.BOT_TOKEN):
        raise apply.Invalid("Токен бота задан в настройках сервера. Изменить его можно только там.", "locked")
    display = apply.clean(name, limit=64) if name is not None else ""
    display = display or DEFAULT_NAME
    body: dict[str, Any] = {"name": display}
    if code is not None:
        invite = apply.clean(code, limit=64)
        if invite:
            body["code"] = invite
    await cancel(page)
    try:
        async with _client(config) as http:
            response = await http.post("/api/botpair/start", json=body)
    except httpx.HTTPError:
        raise apply.Invalid(TEXTS["no_connection"], "no_connection") from None
    data = _answer(response)
    if response.status_code != 200:
        why = data.get("error") if isinstance(data.get("error"), str) else ""
        why = why if why in TEXTS else "rate_limited" if response.status_code == 429 else "bad_answer"
        if why == "manager_unavailable":
            _remember(config.bot_manager_api, False)
        raise apply.Invalid(TEXTS[why], why)
    pair_id, secret = data.get("pair_id"), data.get("secret")
    username, manager = data.get("username"), data.get("manager")
    ttl = data.get("ttl")
    if not (isinstance(pair_id, str) and _PAIR.match(pair_id) and isinstance(secret, str) and _SECRET.match(secret)
            and isinstance(username, str) and _USERNAME.match(username) and pair_id in username
            and isinstance(manager, str) and _MANAGER.match(manager)
            and isinstance(ttl, int) and not isinstance(ttl, bool) and 60 <= ttl <= 3600):
        raise apply.Invalid(TEXTS["bad_answer"], "bad_answer")
    # Ссылку собираем сами из имён: посредник не может подсунуть странице владельца чужую ссылку.
    link = f"https://t.me/newbot/{manager}/{username}?name={quote(display, safe='')}"
    _remember(config.bot_manager_api, True)
    pairing = Pairing(pair_id, secret, link, username, manager, time.time() + ttl, session_id)
    page.managed = pairing
    pairing.task = state.spawn(_wait(state, page, pairing), name="setup-managed-bot")
    return pairing


async def cancel(page: Any) -> bool:
    pairing = getattr(page, "managed", None)
    page.managed = None
    if pairing is None:
        return False
    if pairing.task is not None and not pairing.task.done() and pairing.status == WAITING:
        pairing.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await pairing.task
    return pairing.status == WAITING


def _fail(pairing: Pairing, code: str, message: str | None = None) -> None:
    pairing.status, pairing.code, pairing.message = FAILED, code, message or TEXTS.get(code, TEXTS["bad_answer"])
    pairing.secret = ""


async def _session_alive(state: Any, session_id: int) -> bool:
    async with state.pool.acquire() as conn:
        return bool(await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM setup_sessions WHERE id = $1 AND revoked_at IS NULL AND expires_at > now())",
            session_id))


async def _wait(state: Any, page: Any, pairing: Pairing) -> None:
    """Раз в несколько секунд спрашивает посредника, создан ли бот, и применяет токен."""
    every = float(executor_service.TEST_OVERRIDES.get("botpair_poll", POLL_EVERY))
    data: dict[str, Any] = {}
    try:
        async with _client(state.config) as http:
            while True:
                if time.time() > pairing.expires_at + 30:
                    _fail(pairing, "expired")
                    await audit.write(state.pool, "bot.managed", audit.REFUSED, "бот не появился за отведённое время")
                    return
                await asyncio.sleep(every)
                try:
                    response = await http.post(f"/api/botpair/{pairing.pair_id}/token", json={"secret": pairing.secret})
                except httpx.HTTPError:
                    continue                      # посредник недоступен — спросим ещё раз
                if response.status_code == 202:
                    continue
                if response.status_code in (404, 409):
                    code = "conflict" if response.status_code == 409 else "expired"
                    _fail(pairing, code)
                    await audit.write(state.pool, "bot.managed", audit.REFUSED,
                                      "у бота сменился владелец" if code == "conflict" else "посредник пару не знает")
                    return
                if response.status_code == 200:
                    data = _answer(response)
                    break
                # 429, 502 и прочее — временное: повторим.
    except apply.Invalid as exc:
        _fail(pairing, exc.code, exc.message)
        return
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — попытка не должна «висеть» в ожидании
        logger.warning("бот в одно нажатие: сбой ожидания (%s)", type(exc).__name__)
        _fail(pairing, "bad_answer")
        return
    pairing.secret = ""                            # пара использована: секрет больше не нужен
    try:
        await _finish(state, page, pairing, data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("бот в одно нажатие: токен не применён (%s)", type(exc).__name__)
        _fail(pairing, "bad_answer")
    finally:
        data.clear()


async def _finish(state: Any, page: Any, pairing: Pairing, data: dict[str, Any]) -> None:
    token = data.get("token")
    bot, creator = data.get("bot"), data.get("creator")
    bot_id = bot.get("id") if isinstance(bot, dict) else None
    username = bot.get("username") if isinstance(bot, dict) else None
    creator_id = creator.get("id") if isinstance(creator, dict) else None
    creator_name = creator.get("name") if isinstance(creator, dict) and isinstance(creator.get("name"), str) else ""
    if not (isinstance(token, str) and _TOKEN.match(token) and isinstance(bot_id, int) and not isinstance(bot_id, bool)
            and isinstance(username, str) and isinstance(creator_id, int) and not isinstance(creator_id, bool)):
        _fail(pairing, "bad_answer")
        await audit.write(state.pool, "bot.managed", audit.REFUSED, "посредник ответил непонятно")
        return
    pairing.status, pairing.bot_username = APPLYING, username
    pairing.creator_name = creator_name[:64] or None
    pairing.restricted = data.get("restricted") is True
    if not await _session_alive(state, pairing.session_id):
        _fail(pairing, "session_ended")
        await audit.write(state.pool, "bot.managed", audit.REFUSED, "вход на страницу завершился до создания бота")
        return
    detail = (f"бот @{username} создан в одно нажатие через бот-менеджер @{pairing.manager} посредника "
              f"{manager_host(state.config)}; проверен запросом к Telegram"
              + ("" if pairing.restricted else "; доступ к боту для посторонних посредник не закрыл"))
    try:
        with authority.setup_context(pairing.session_id, action="/shturman-setup/api/bot/managed"):
            await apply_token(state, page, token, via="managed", expect={"id": bot_id, "username": username},
                              origin={"creator_user_id": creator_id, "creator_name": pairing.creator_name,
                                      "restricted": pairing.restricted, "manager": pairing.manager,
                                      "manager_api": manager_host(state.config)},
                              detail=detail)
    except apply.Invalid as exc:
        _fail(pairing, exc.code, exc.message)
        return
    finally:
        token = data["token"] = ""
    pairing.status = DONE
