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

**Кто создал бота — подтверждает владелец.** Ссылку создания можно подсмотреть (QR на экране) и
создать бота первым со своего аккаунта. Такой бот принадлежит постороннему: токен у него есть в
@BotFather, снять закрытый доступ он может, а с токеном подпишет данные входа Mini App за любой
аккаунт, в том числе за владельца, — никакая проверка на стороне сервиса тут не поможет. Поэтому
полученный токен **не применяется сам**: он ждёт в памяти сервиса (не дольше `CONFIRM_TTL`), а
страница показывает, каким аккаунтом создан бот (@имя, номер, имя), и просит «Да, это мой
аккаунт». «Нет, не мой» — токен выбрасывается, попытка помечается как перехваченная.

Токен из обоих способов применяется **одним и тем же путём** — `apply_token`: проверка `getMe`,
проверка, что ботом не пользуется другая программа (409 или webhook), запись в файл значений
страницы и перезапуск исполнителя. Для бота из одного нажатия сверх этого: `getMe` должен вернуть
бота с тем именем, которое сервис сам вписал в ссылку, и с номером, о котором сообщил посредник;
привязать себя владельцем может только аккаунт-создатель (`executor/binding.required_creator`).

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
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlsplit

import httpx

from .. import authority
from ..executor import binding
from ..executor import service as executor_service
from . import apply, audit
from . import secrets_store as ss

logger = logging.getLogger("shturman.setup")

POLL_EVERY = 3.0                 # секунд между вопросами посреднику «бот создан?»
CALL_DEADLINE = 20.0             # секунд на один запрос к посреднику целиком
MAX_ANSWER = 64 * 1024           # байт: ответ посредника длиннее — не ответ
UNREACHABLE_AFTER = 300.0        # секунд после срока пары без связи с посредником — попытка закончена
CONFIRM_TTL = 10 * 60            # секунд ждёт подтверждения «это мой аккаунт» полученный токен
DEFAULT_NAME = "Согласования"
_PAIR = re.compile(r"^[a-z0-9]{6,32}$")
_SECRET = re.compile(r"^[A-Za-z0-9_-]{40,64}$")
_USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,30}[Bb][Oo][Tt]$")
_MANAGER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_HANDLE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_TOKEN = re.compile(r"^[0-9]{5,16}:[A-Za-z0-9_-]{30,80}$")

# Состояния попытки. Конечные — DONE, FAILED, INTERCEPTED, CANCELLED.
WAITING, CHECKING, CONFIRM, APPLYING = "waiting", "checking", "confirm", "applying"
DONE, FAILED, INTERCEPTED, CANCELLED = "done", "failed", "intercepted", "cancelled"
FINAL = frozenset({DONE, FAILED, INTERCEPTED, CANCELLED})

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
    "wrong_bot": "Посредник прислал токен не того бота, который создавался по этой ссылке. Токен не сохранён. "
                 "Создайте бота у @BotFather вручную и сообщите об этом держателю посредника.",
    "confirm_expired": "Вы не подтвердили, что бот создан вашим аккаунтом, — токен не сохранён. Начните заново "
                       "или создайте бота вручную.",
    "intercepted": "Бота создал кто-то другой — вероятно, ссылку подсмотрели. Этот бот не используется. Создайте "
                   "бота заново или вручную у @BotFather. Удалить чужого бота из списка не получится — он не ваш.",
    "replaced": "Пока шло создание, токен бота согласований поменяли на этой странице. Созданный бот не "
                "подключён.",
    "not_pending": "Подтверждать нечего: создание бота не ждёт подтверждения.",
}


def available(config: Any) -> bool:
    return bool(getattr(config, "bot_manager_api", ""))


def manager_host(config: Any) -> str | None:
    api = getattr(config, "bot_manager_api", "")
    return urlsplit(api).netloc if api else None


# --- запросы к посреднику ---------------------------------------------------------------------------

class RelayError(Exception):
    """Посредник недоступен, не уложился в срок или ответил слишком длинно."""


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


async def _call(http: httpx.AsyncClient, method: str, path: str, body: dict | None = None) -> tuple[int, dict[str, Any]]:
    """Один запрос к посреднику: общий срок `CALL_DEADLINE`, ответ — не длиннее `MAX_ANSWER`
    (читается потоком и обрывается), разбирается как объект JSON. Иначе — RelayError."""
    async def run() -> tuple[int, dict[str, Any]]:
        async with http.stream(method, path, json=body) as response:
            buf = bytearray()
            async for chunk in response.aiter_bytes():
                buf += chunk
                if len(buf) > MAX_ANSWER:
                    raise RelayError("too_long")
            try:
                data = json.loads(bytes(buf)) if buf else {}
            except (ValueError, RecursionError):
                data = {}
            return response.status_code, data if isinstance(data, dict) else {}
    try:
        return await asyncio.wait_for(run(), CALL_DEADLINE)
    except (httpx.HTTPError, asyncio.TimeoutError) as exc:
        raise RelayError(type(exc).__name__) from None


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
            status, data = await _call(http, "GET", "/api/health")
        ready = data.get("botpair") is True if status == 200 else False
    except (RelayError, apply.Invalid):
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


def _same_bot(bot: dict[str, Any], expect: dict | None) -> bool:
    return expect is None or (bot["id"] == expect.get("id")
                              and bot["username"].lower() == str(expect.get("username") or "").lower())


async def apply_token(state: Any, page: Any, token: str, *, via: str, expect: dict | None = None,
                      origin: dict | None = None, detail: str = "бот проверен запросом к Telegram",
                      still_valid: Callable[[], Awaitable[str | None]] | None = None) -> dict[str, Any]:
    """Проверить токен бота согласований и начать им пользоваться — один путь для токена,
    вставленного владельцем (via="manual"), и полученного от бота-менеджера (via="managed").

    expect — {id, username}: токен должен быть именно этого бота (`getMe`).
    origin — что записать о создании бота (creator_user_id, creator_name, restricted, manager).
    still_valid — проверка прямо перед сохранением, под блокировкой страницы: вернула код — токен
    не сохраняется (вход завершился, токен уже поменяли вручную).
    Отказы — apply.Invalid с текстом для владельца; отказ проверки «другая программа» пишется
    в журнал действий, как и успех. Сохранения идут по одному (`page.bot_lock`)."""
    if not page.settings.editable(ss.BOT_TOKEN):
        raise apply.Invalid("Токен бота задан в настройках сервера. Изменить его можно только там.", "locked")
    bot = await apply.check_bot_token(state.config, token)
    if not _same_bot(bot, expect):
        await audit.write(state.pool, "bot.token", audit.REFUSED, "токен не того бота, который создавался по ссылке")
        raise apply.Invalid(TEXTS["wrong_bot"], "wrong_bot")
    async with page.bot_lock:
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
        if still_valid is not None:
            problem = await still_valid()
            if problem is not None:
                raise apply.Invalid(TEXTS[problem], problem)
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
    bot_id: int | None = None
    bot_username: str | None = None
    creator_id: int | None = None
    creator_username: str | None = None
    creator_name: str | None = None
    restricted: bool | None = None
    token: str = field(default="", repr=False)     # ждёт подтверждения «это мой аккаунт»
    confirm_until: float = 0.0
    task: asyncio.Task | None = field(default=None, repr=False)

    def drop_secrets(self) -> None:
        self.secret = self.token = ""

    def view(self) -> dict[str, Any]:
        """Для страницы. Ни секрета пары, ни токена здесь нет."""
        if self.status == CONFIRM and time.time() > self.confirm_until:
            _fail(self, "confirm_expired")
        creator = None
        if self.creator_id is not None:
            creator = {"id": self.creator_id, "username": self.creator_username, "name": self.creator_name}
        return {"status": self.status, "link": self.link if self.status == WAITING else None,
                "username": self.username, "manager": self.manager,
                "expires_at": self.expires_at, "seconds_left": max(0, int(self.expires_at - time.time())),
                "confirm_seconds_left": max(0, int(self.confirm_until - time.time())) if self.status == CONFIRM else None,
                "code": self.code, "message": self.message, "bot_username": self.bot_username,
                "creator": creator, "restricted": self.restricted}


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
            status, data = await _call(http, "POST", "/api/botpair/start", body)
    except RelayError:
        raise apply.Invalid(TEXTS["no_connection"], "no_connection") from None
    if status != 200:
        why = data.get("error") if isinstance(data.get("error"), str) else ""
        why = why if why in TEXTS else "rate_limited" if status == 429 else "bad_answer"
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
    """Снять попытку в любом незаконченном состоянии: ожидание бота останавливается, полученный
    и не подтверждённый токен выбрасывается. True — было что снимать."""
    pairing = getattr(page, "managed", None)
    page.managed = None
    if pairing is None or pairing.status in FINAL:
        return False
    pairing.status = CANCELLED
    pairing.drop_secrets()
    if pairing.task is not None and not pairing.task.done() and pairing.task is not asyncio.current_task():
        pairing.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await pairing.task
    return True


def _fail(pairing: Pairing, code: str, message: str | None = None) -> None:
    pairing.status = INTERCEPTED if code == "intercepted" else FAILED
    pairing.code, pairing.message = code, message or TEXTS.get(code, TEXTS["bad_answer"])
    pairing.drop_secrets()


async def _session_alive(state: Any, session_id: int) -> bool:
    async with state.pool.acquire() as conn:
        return bool(await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM setup_sessions WHERE id = $1 AND revoked_at IS NULL AND expires_at > now())",
            session_id))


async def _wait(state: Any, page: Any, pairing: Pairing) -> None:
    """Раз в несколько секунд спрашивает посредника, создан ли бот, — пока посредник не выдаст токен
    или не ответит, что пары больше нет (у посредника срок пары продлевается после создания бота)."""
    every = float(executor_service.TEST_OVERRIDES.get("botpair_poll", POLL_EVERY))
    data: dict[str, Any] = {}
    try:
        async with _client(state.config) as http:
            while pairing.status == WAITING:
                await asyncio.sleep(every)
                try:
                    status, answer = await _call(http, "POST", f"/api/botpair/{pairing.pair_id}/token",
                                                 {"secret": pairing.secret})
                except RelayError:
                    if time.time() > pairing.expires_at + UNREACHABLE_AFTER:
                        _fail(pairing, "no_connection")
                        await audit.write(state.pool, "bot.managed", audit.REFUSED, "посредник не отвечает")
                        return
                    continue                      # посредник недоступен — спросим ещё раз
                if status == 202:
                    until = answer.get("expires_at")
                    if isinstance(until, int) and not isinstance(until, bool):
                        pairing.expires_at = float(until)
                    continue
                if status in (404, 409):
                    code = "conflict" if status == 409 else "expired"
                    _fail(pairing, code)
                    await audit.write(state.pool, "bot.managed", audit.REFUSED,
                                      "у бота сменился владелец" if code == "conflict" else "посредник пару не знает")
                    return
                if status == 200:
                    data = answer
                    break
                # 429, 502 и прочее — временное: повторим.
        if pairing.status != WAITING:
            return
        pairing.secret = ""                        # пара использована: секрет больше не нужен
        await _received(state, pairing, data)
    except apply.Invalid as exc:
        _fail(pairing, exc.code, exc.message)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — попытка не должна «висеть» в ожидании
        logger.warning("бот в одно нажатие: сбой ожидания (%s)", type(exc).__name__)
        _fail(pairing, "bad_answer")
    finally:
        data.clear()


async def _received(state: Any, pairing: Pairing, data: dict[str, Any]) -> None:
    """Посредник выдал токен: проверить, что это бот, созданный по нашей ссылке, и отложить токен
    до подтверждения владельцем, что бот создан его аккаунтом."""
    token = data.get("token")
    bot, creator = data.get("bot"), data.get("creator")
    bot_id = bot.get("id") if isinstance(bot, dict) else None
    username = bot.get("username") if isinstance(bot, dict) else None
    creator_id = creator.get("id") if isinstance(creator, dict) else None
    handle = creator.get("username") if isinstance(creator, dict) else None
    name = creator.get("name") if isinstance(creator, dict) and isinstance(creator.get("name"), str) else ""
    if not (isinstance(token, str) and _TOKEN.match(token) and isinstance(bot_id, int) and not isinstance(bot_id, bool)
            and isinstance(username, str) and isinstance(creator_id, int) and not isinstance(creator_id, bool)):
        _fail(pairing, "bad_answer")
        await audit.write(state.pool, "bot.managed", audit.REFUSED, "посредник ответил непонятно")
        return
    pairing.status = CHECKING
    # Имя бота — то, что сервис сам вписал в ссылку; номер — тот, о котором сообщил посредник.
    expect = {"id": bot_id, "username": pairing.username}
    if username.lower() != pairing.username.lower():
        _fail(pairing, "wrong_bot")
        await audit.write(state.pool, "bot.managed", audit.REFUSED, "посредник сообщил о боте с другим именем")
        return
    found = await apply.check_bot_token(state.config, token)
    if not _same_bot(found, expect):
        _fail(pairing, "wrong_bot")
        await audit.write(state.pool, "bot.managed", audit.REFUSED, "токен не того бота, который создавался по ссылке")
        return
    if pairing.status != CHECKING:                 # пока шла проверка, попытку сняли
        return
    if not await _session_alive(state, pairing.session_id):
        # Начавший создание вход уже завершён: токен ждать подтверждения не остаётся.
        _fail(pairing, "session_ended")
        await audit.write(state.pool, "bot.managed", audit.REFUSED, "вход на страницу завершился до создания бота")
        return
    pairing.bot_id, pairing.bot_username = bot_id, found["username"]
    pairing.creator_id = creator_id
    pairing.creator_username = handle if isinstance(handle, str) and _HANDLE.match(handle) else None
    pairing.creator_name = name.strip()[:64] or None
    pairing.restricted = data.get("restricted") is True
    pairing.token = token
    pairing.confirm_until = time.time() + CONFIRM_TTL
    pairing.status = CONFIRM
    await audit.write(state.pool, "bot.managed", audit.OK, "бот создан; ждёт подтверждения, что создан вашим аккаунтом")


async def confirm(state: Any, page: Any, session_id: int, *, accept: bool) -> Pairing:
    """Ответ владельца на «Бот создан аккаунтом …». Да — токен применяется общим путём; нет —
    токен выбрасывается, попытка помечается перехваченной."""
    pairing = getattr(page, "managed", None)
    if pairing is None:
        raise apply.Invalid(TEXTS["not_pending"], "not_pending")
    pairing.view()                                 # просроченное подтверждение — уже не подтверждение
    if pairing.status != CONFIRM:
        raise apply.Invalid(pairing.message or TEXTS["not_pending"], pairing.code or "not_pending")
    if not accept:
        _fail(pairing, "intercepted")
        await audit.write(state.pool, "bot.managed", audit.REFUSED,
                          f"владелец не узнал аккаунт-создатель бота @{pairing.bot_username}: токен выброшен")
        return pairing
    token, pairing.token = pairing.token, ""
    pairing.status = APPLYING
    creator = f"@{pairing.creator_username}" if pairing.creator_username else "без имени пользователя"
    detail = (f"бот @{pairing.bot_username} создан в одно нажатие через бот-менеджер @{pairing.manager} посредника "
              f"{manager_host(state.config)}; создатель {creator}, номер {pairing.creator_id} — подтверждён владельцем; "
              "проверен запросом к Telegram"
              + ("" if pairing.restricted else "; доступ к боту для посторонних посредник не закрыл"))

    async def still_valid() -> str | None:
        # Прямо перед сохранением: попытку не сняли и не заменили вручную, вход ещё действует.
        if page.managed is not pairing or pairing.status != APPLYING:
            return "replaced"
        if not await _session_alive(state, session_id):
            return "session_ended"
        return None

    try:
        with authority.setup_context(session_id, action="/shturman-setup/api/bot/managed/confirm"):
            await apply_token(state, page, token, via="managed",
                              expect={"id": pairing.bot_id, "username": pairing.username},
                              origin={"creator_user_id": pairing.creator_id, "creator_username": pairing.creator_username,
                                      "creator_name": pairing.creator_name, "restricted": pairing.restricted,
                                      "manager": pairing.manager, "manager_api": manager_host(state.config)},
                              detail=detail, still_valid=still_valid)
    except apply.Invalid as exc:
        if pairing.status == APPLYING:
            _fail(pairing, exc.code, exc.message)
        if exc.code in ("session_ended", "replaced"):
            await audit.write(state.pool, "bot.managed", audit.REFUSED,
                              "вход на страницу завершился до сохранения" if exc.code == "session_ended"
                              else "токен поменяли вручную, пока шло создание")
        raise
    finally:
        token = ""
    pairing.status = DONE
    return pairing
