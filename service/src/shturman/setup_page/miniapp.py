"""Mini App бота согласований: вход по данным Telegram, своя узкая сессия, маршруты приложения.

  GET    /shturman-setup/app/                           приложение (данных в нём нет)
  POST   /shturman-setup/api/app/login                  вход: {init_data} — строка Telegram.WebApp.initData
  POST   /shturman-setup/api/app/logout                 завершить эту сессию приложения
  GET    /shturman-setup/api/app/approvals              что ждёт решения владельца — с теми же кнопками, что в боте
  POST   /shturman-setup/api/app/approvals/decide       нажать одну из этих кнопок: {ref}
  GET    /shturman-setup/api/app/chats                  экран «Чаты»: аккаунты Telegram с сессией
  GET    /shturman-setup/api/app/chats/{id}/dialogs     диалоги аккаунта ?offset&limit&q&kind&only&refresh —
                                                        из базы; refresh=1 запускает обновление из Telegram
                                                        в фоне и сразу отвечает (ход — в поле load)
  POST   /shturman-setup/api/app/chats/{id}/sync        включить или выключить чтение одного чата:
                                                        {peer_class, tg_id, enabled, months} (months — при включении)
  POST   /shturman-setup/api/app/chats/{id}/exclude     не сохранять чат или вернуть его: {peer_class, tg_id, excluded};
                                                        уже сохранённое не стирается

Приложение открывается кнопкой меню «Штурман» в чате владельца с ботом согласований (её ставит
сервис: `executor/menu_button.py`). Это ещё одно окно к тем же решениям, что в боте: экран
«Согласования» (`approvals.py`), и к выбору чатов — экран «Чаты» (решение владельца Р-80, ниже).
Остальное — первая настройка, ключи и токены, вход в аккаунт Telegram, выгрузка, своя модель,
удаление сохранённых сообщений — только на странице настройки.

Где живёт. На адресе страницы настройки, под её префиксом — не на адресе дашборда Hermes: там
ассистент может исполнять свой JavaScript (Р-56). Поэтому обратный прокси менять не нужно ни в
одном из трёх способов адреса, а общая защита префикса (`shield.py`) действует и здесь. Рамку
разрешено открыть только web.telegram.org и только для самой страницы приложения.

Вход. Telegram передаёт приложению строку `initData`, подписанную токеном бота (HMAC, `initdata.py`).
Сервис проверяет подпись токеном своего бота согласований, срок (`initdata.MAX_AGE`) и что открыл
приложение тот, кто привязан к этому боту владельцем (`binding.bound_owner`). Токена этого бота у
Hermes нет, поэтому данные входа ассистент подделать не может; любой отказ — один и тот же ответ
без причины (причина — только в журнале действий, кодом).

Сессия — отдельная от сессии страницы и уже её:
  * ключ (256 бит) отдаётся один раз, в ответе на вход; в базе — SHA-256. Приложение держит его в
    памяти и в `sessionStorage` своего origin и присылает в заголовке `X-Shturman-App`. Cookie нет —
    по тем же причинам, что у страницы (`auth.py`);
  * живёт `IDLE` (30 минут) без обращений и не дольше `MAX_AGE` (4 часа) от входа;
  * одни данные входа — одна сессия. Повтор тех же данных (даже с переставленными полями: сессия
    привязана к подписи Telegram, а не к записи строки) в течение срока их действия — отказ.
    Строку initData перехватить можно (она живёт в адресе страницы у Telegram), поэтому окно
    повтора закрыто, а не только сужено сроком;
  * действует, только пока бот тот же и владелец тот же. Любая привязка или отвязка владельца и
    смена токена бота согласований её завершают (`revoke_all`), а не «усыпляют»: привязка A → B → A
    или тот же токен, введённый заново, её не оживят. Срок `IDLE` сдвигается только после сверки;
  * пока ботом согласований пользуется кто-то ещё (опрос получает 409 или включён webhook —
    так выглядит и тот же токен у бота Hermes), вход закрыт и сессии не действуют (`conflicted`);
  * «выйти везде» на странице и `./ops/logout-all.sh` гасят и её (`auth.revoke_all`);
  * ключ сессии страницы здесь не действует, ключ приложения — на странице (тест это проверяет).

Что приложение может:
  * показать то, что ждёт решения, и нажать кнопку, которая под этим стоит в боте (`approvals.py`).
    Решение идёт тем же вызовом, что нажатие в боте (`bridge.dispatch_callback`), в контексте
    владельца из Telegram (`authority.miniapp_context`). Ничего сверх того, что уже умеют кнопки
    бота: ни настроек, ни правил, ни текста переписки за пределами карточек;
  * экран «Чаты» (Р-80): то же, что шаг «Какие чаты читать» на странице, теми же функциями
    (`chats.py`), применяется сразу, в контексте владельца из Telegram. Отличия от страницы:
    список диалогов — из базы, обновление из Telegram запускает только кнопка «Обновить список из
    Telegram» (Р-81: в фоне); чаты включаются по одному (без «все группы разом»); исключение не стирает
    сохранённое. Цена и смягчения — `docs/decisions.md`, Р-80: войти в приложение может любой, у кого
    есть токен бота согласований, — значит, и менять, какие чаты читает ассистент. Поэтому каждое
    изменение отсюда сервис сообщает владельцу отдельным сообщением в бот согласований (`_tell`) и
    пишет в журнал действий страницы с пометкой «из приложения».

Журнал действий: `app.login`, `app.login_failed` (не чаще раза в минуту; повтор уже использованных
данных входа — всегда, и владельцу уходит сообщение в бот), `app.logout`,
`app.decide` — вид и номер пункта и подпись кнопки, без имён и текстов; `app.sync_on`,
`app.sync_off`, `app.chat_exclude`, `app.chat_include` — номер чата в Telegram и глубина, без названий.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

import asyncpg
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from .. import authority, bridge, confirm
from ..api_core import BadRequest, error_response
from ..app import AppState, state_of
from ..executor import binding
from ..tg import gateway, sync
from ..tg.manager import TgError, TgManager
from . import APP_PATH, approvals, audit, auth, chats, initdata
from . import service as page

APP_API = page.API + "/app"
HEADER = "x-shturman-app"
IDLE = 30 * 60                # секунд без обращений — и сессия приложения гаснет
MAX_AGE = 4 * 3600            # секунд от входа — предел, продлением не сдвигается
FAIL_AUDIT_EVERY = 60.0

LOGIN_FAILED = ("Не получилось войти. Закройте приложение и откройте его снова кнопкой «Штурман» "
                "в боте согласований.")
SIGNED_OUT = "Вход в приложение устарел. Закройте его и откройте снова кнопкой «Штурман» в боте согласований."

REVOKE_ALL = "UPDATE miniapp_sessions SET revoked_at = now() WHERE revoked_at IS NULL"

# Часы проверки срока данных входа. Тесты подменяют.
now: Callable[[], float] = time.time


@dataclass(frozen=True)
class AppSession:
    id: int
    bot_id: int
    user_id: int
    chat_id: int          # личный чат владельца с ботом согласований — туда ушли бы и карточки
    expires_at: datetime


# --- сессии -------------------------------------------------------------------------------------

def _init_hash(init: initdata.InitData) -> str:
    return hashlib.sha256(init.signature_hash.encode("ascii")).hexdigest()


async def create_session(conn: asyncpg.Connection, init: initdata.InitData, bot_id: int) -> tuple[str, datetime] | None:
    """Новая сессия по проверенным данным входа. None — по этим данным сессия уже выдавалась."""
    token = auth.new_token()
    row = await conn.fetchrow(
        """INSERT INTO miniapp_sessions (token_hash, init_hash, bot_id, user_id, expires_at)
           VALUES ($1, $2, $3, $4, now() + make_interval(secs => $5))
           ON CONFLICT (init_hash) DO NOTHING RETURNING expires_at""",
        auth.token_hash(token), _init_hash(init), bot_id, init.user_id, float(MAX_AGE))
    return (token, row["expires_at"]) if row is not None else None


async def find_session(conn: asyncpg.Connection, token: Any) -> asyncpg.Record | None:
    """Действующая сессия по ключу из заголовка. Обращение не отмечает: срок `IDLE` сдвигает
    `touch` — и только после того, как сверены бот и владелец."""
    if not auth.well_formed(token):
        return None
    return await conn.fetchrow(
        """SELECT id, bot_id, user_id, expires_at FROM miniapp_sessions
           WHERE token_hash = $1 AND revoked_at IS NULL AND expires_at > now()
             AND last_seen_at > now() - make_interval(secs => $2)""",
        auth.token_hash(token), float(IDLE))


async def touch(conn: asyncpg.Connection, session_id: int) -> None:
    await conn.execute("UPDATE miniapp_sessions SET last_seen_at = now() WHERE id = $1", session_id)


async def revoke_all(conn: asyncpg.Connection) -> int:
    """Завершает все сессии приложения. Так делают привязка и отвязка владельца (`bridge.set_owner`,
    `bridge.clear_owner`) и смена токена бота согласований (`executor/service.py`): сессия не
    «засыпает» до возврата прежнего владельца или бота, а кончается — иначе привязка A → B → A или
    тот же токен, введённый заново, оживили бы её."""
    done = await conn.execute(REVOKE_ALL)
    return int(done.split()[-1])


async def cleanup(conn: asyncpg.Connection) -> None:
    """Убирает давно закрытые сессии. Запись живёт сутки после конца: дольше срока данных входа,
    иначе повтор тех же данных снова получил бы сессию."""
    await conn.execute(
        """DELETE FROM miniapp_sessions
           WHERE expires_at < now() - interval '1 day' OR revoked_at < now() - interval '1 day'
              OR last_seen_at < now() - interval '1 day'""")


async def _signed_in(request: Request) -> AppSession:
    """Сессия приложения из заголовка `X-Shturman-App` — и только из него. Действует, пока бот тот
    же и владелец, привязанный к нему, тот же."""
    refused = BadRequest(SIGNED_OUT, 401, "unauthenticated")
    token = request.headers.get(HEADER)
    if not token:
        raise refused
    state = state_of(request)
    bot, api = page._runtime_bot(state)
    if bot is None or bot.bot_id is None or conflicted(bot, api):
        raise refused
    async with state.pool.acquire() as conn:
        row = await find_session(conn, token)
        owner = await binding.bound_owner(conn, bot.bot_id)
        if row is None or owner is None or row["bot_id"] != bot.bot_id or row["user_id"] != owner["user_id"]:
            raise refused
        await touch(conn, row["id"])
    return AppSession(row["id"], row["bot_id"], row["user_id"], owner["chat_id"], row["expires_at"])


# Ботом согласований пользуется кто-то ещё: Telegram отвечает 409 на опрос (тот же токен опрашивает
# другая программа) или у бота включён webhook. Так выглядит и ошибка «токен бота согласований — тот
# же, что у бота ассистента в Hermes»: тогда токен есть у Hermes, и он мог бы сам подписать данные
# входа. Пока это так, вход в приложение закрыт и сессии не действуют. Номер бота Hermes сервис
# узнать не может, не читая секретов Hermes, — поэтому судит по опросу.
CONFLICTS = frozenset({"other_poller", "conflict", "webhook"})


def conflicted(bot: Any, api: Any) -> bool:
    return bot.problem in CONFLICTS or bool(api is not None and api.broken in CONFLICTS)


# --- обёртка маршрутов ----------------------------------------------------------------------------

Handler = Callable[[Request], Awaitable[Response]]


def endpoint(fn: Handler | None = None, *, public: bool = False) -> Any:
    """Обёртка маршрута приложения: источник запроса — как у страницы (`page._check_fetch`), сессия —
    своя. Контекста страницы настройки (`authority.setup_context`) здесь нет: права приложения уже,
    решение принимается в контексте владельца из Telegram и только в `approvals.decide`."""
    def deco(fn: Handler) -> Handler:
        async def wrapped(request: Request) -> Response:
            try:
                page._check_fetch(request, page._context(request))
                if not public:
                    request.state.app_session = await _signed_in(request)
                return await fn(request)
            except BadRequest as exc:
                return error_response(exc)
            except confirm.Refused as exc:
                return JSONResponse({"error": exc.message, **({"code": exc.code} if exc.code else {})},
                                    status_code=exc.status)
            except TgError as exc:
                extra = {"retry_after": exc.retry_after} if exc.retry_after else {}
                return JSONResponse({"error": exc.message, **extra}, status_code=exc.status)
            except gateway.AccountUnavailable:
                return JSONResponse({"error": "Аккаунт не подключён.", "code": "offline"}, status_code=409)
            except ClientDisconnect:
                return JSONResponse({"error": "Соединение оборвалось до конца запроса."}, status_code=400)
        wrapped.__name__ = fn.__name__
        wrapped.miniapp = True          # type: ignore[attr-defined] — по этой метке тест сверяет маршруты
        return wrapped
    return deco(fn) if fn is not None else deco


# --- маршруты ------------------------------------------------------------------------------------

async def index(request: Request) -> Response:
    """Страница приложения. Тело одно для любого запроса: данных в нём нет."""
    return Response(page._static("app.html"), media_type="text/html; charset=utf-8")


REPLAY_TEXT = ("Кто-то открыл приложение «Штурман» с теми же данными входа Telegram, по которым уже был вход. "
               "Если вы сейчас не смогли войти в приложение — значит, кто-то вошёл с вашими данными раньше вас. "
               "Тогда откройте «Настройки Штурмана» и нажмите «Выйти на всех устройствах», "
               "затем снова откройте приложение кнопкой «Штурман». Если вы просто открыли "
               "приложение повторно, ничего делать не нужно.")
REPLAY_NOTICES = 200          # столько подписей помнится, чтобы не писать владельцу дважды об одной


async def _failed(request: Request, code: str, init: initdata.InitData | None = None) -> None:
    holder = page._page(request)
    if code == "replayed" and init is not None:
        # Повтор уже использованных данных входа — всегда в журнал и владельцу в бот: это либо он
        # сам, либо тот, кто перехватил его данные входа. Об одной подписи — одно сообщение.
        await page._log(request, "app.login_failed", audit.REFUSED, "причина: replayed")
        mark = _init_hash(init)
        if mark not in holder.app_replays:
            if len(holder.app_replays) >= REPLAY_NOTICES:
                holder.app_replays.clear()
            holder.app_replays.add(mark)
            page._tell_owner(state_of(request), REPLAY_TEXT)
        return
    moment = time.monotonic()
    if holder.app_fail_audit_at == 0.0 or moment - holder.app_fail_audit_at >= FAIL_AUDIT_EVERY:
        holder.app_fail_audit_at = moment
        await page._log(request, "app.login_failed", audit.REFUSED, f"причина: {code}")


@endpoint(public=True)
async def login(request: Request) -> JSONResponse:
    data = await page._body(request)
    state, holder = state_of(request), page._page(request)
    bot, api = page._runtime_bot(state)
    bot_token = state.config.bot_token
    created = init = None
    try:
        # Токен, которым проверяем, — именно того бота, который сейчас работает.
        if bot is None or bot.bot_id is None or not bot_token or bot_token.split(":", 1)[0] != str(bot.bot_id):
            raise initdata.Invalid("no_bot")
        if conflicted(bot, api):
            raise initdata.Invalid("bot_conflict")
        init = initdata.check(data.get("init_data"), bot_token, now=now())
        async with state.pool.acquire() as conn:
            owner = await binding.bound_owner(conn, bot.bot_id)
            if owner is None or owner["user_id"] != init.user_id:
                raise initdata.Invalid("not_owner")
            created = await create_session(conn, init, bot.bot_id)
        if created is None:
            raise initdata.Invalid("replayed")
    except initdata.Invalid as exc:
        await holder.slow_refusal()
        await _failed(request, exc.code, init)
        raise BadRequest(LOGIN_FAILED, 401, "app_login_failed") from None
    await page._log(request, "app.login")
    key, expires_at = created
    return JSONResponse({"ok": True, "key": key, "expires_at": expires_at.isoformat(), "idle_seconds": IDLE})


@endpoint
async def logout(request: Request) -> JSONResponse:
    await page._body(request)
    async with state_of(request).pool.acquire() as conn:
        await conn.execute("UPDATE miniapp_sessions SET revoked_at = now() WHERE id = $1 AND revoked_at IS NULL",
                           request.state.app_session.id)
    await page._log(request, "app.logout")
    return JSONResponse({"ok": True})


def _sending(config: Any) -> str:
    return "on" if config.sending else "prepare_only" if getattr(config, "prepare_only", False) else "off"


def setup_url(config: Any) -> str | None:
    """Адрес «Настроек Штурмана» для кнопки «Открыть настройки в браузере» (П4) — без ключа и без входа:
    там вход по коду из бота или по ссылке с сервера."""
    origin = config.setup_external
    return origin + page.PREFIX + "/" if isinstance(origin, str) and origin.startswith("https://") else None


@endpoint
async def approvals_list(request: Request) -> JSONResponse:
    from .. import halt
    from ..executor import owner_views
    state = state_of(request)
    async with state.ro_pool.acquire() as conn:
        collected = await approvals.collect(conn, state.config)
        stopped = halt.public(await halt.current(conn))
        mode = await owner_views.work_mode(conn)
    return JSONResponse({**approvals.public(collected), "sending": _sending(state.config), "halt": stopped,
                         "setup_url": setup_url(state.config), "mode": mode})


@endpoint
async def approvals_answer(request: Request) -> JSONResponse:
    """Ответ на вопрос ассистента из приложения (П1): поле «Ваш ответ». Тот же путь на сервере, что у
    команды /replytask N answer и кнопки «Ответить» в боте (`replies.owner.answer`): владелец из
    Telegram, задача ждёт его, переписка не изменилась, окно ответа не вышло."""
    from ..replies import owner as replies_owner
    data = await page._body(request)
    ref, text = data.get("ref"), data.get("text")
    match = approvals.ANSWER_REF.fullmatch(ref) if isinstance(ref, str) else None
    if match is None:
        raise BadRequest("Такого вопроса нет.", 400, "bad_ref")
    if not isinstance(text, str):
        raise BadRequest("Напишите ответ.", 400, "empty")
    session: AppSession = request.state.app_session
    state = state_of(request)
    async with state.pool.acquire() as conn:
        item = approvals.find_answer(await approvals.collect(conn, state.config), ref)
        if item is None:
            await page._log(request, "app.answer", audit.REFUSED, "вопроса уже нет в списке")
            raise BadRequest(replies_owner.CLOSED, 409, "gone")
        with authority.miniapp_context(session.user_id, chat_id=session.chat_id):
            async with conn.transaction():
                out = await replies_owner.answer(conn, state, int(match.group(1)), text,
                                                 session.user_id, nonce=match.group(2), window=True)
    if not out["ok"]:
        await page._log(request, "app.answer", audit.REFUSED, f"вопрос № {item['id']}; причина: {out['code']}")
        raise BadRequest(out["text"], 409 if out["code"] in ("closed", "late", "changed", "answered", "stopped") else 400,
                         out["code"])
    approvals.wake(state)
    await page._log(request, "app.answer", detail=f"вопрос № {item['id']}; знаков: {len(text.strip())}")
    return JSONResponse({"ok": True, "answer": out["text"]})


@endpoint
async def approvals_decide(request: Request) -> JSONResponse:
    data = await page._body(request)
    ref = data.get("ref")
    if not isinstance(ref, str) or not approvals.REF.fullmatch(ref):
        raise BadRequest("Такой кнопки нет.", 400, "bad_ref")
    session: AppSession = request.state.app_session
    state = state_of(request)
    async with state.pool.acquire() as conn:
        found = approvals.find(await approvals.collect(conn, state.config), ref)
        if found is None:
            # Кнопки нет в списке прямо сейчас: решено в боте или на странице, срок вышел, карточка
            # сменилась. Решение не передаётся — как нажатие на погасшую кнопку.
            await page._log(request, "app.decide", audit.REFUSED, "кнопки уже нет в списке")
            raise BadRequest("Это решение уже недоступно — список обновлён.", 409, "gone")
        item, action = found
        out = await approvals.decide(conn, item, ref, user_id=session.user_id, chat_id=session.chat_id)
    approvals.wake(state)
    await page._log(request, "app.decide", detail=f"{item['kind_title']} № {item['id']}: «{action['label']}»")
    return JSONResponse({"ok": True, "answer": str(out.get("answer") or "Готово.")})


# --- экран «Чаты» (Р-80) ---------------------------------------------------------------------------
#
# То же, что шаг «Какие чаты читать» на странице, теми же функциями (`chats.py`). Решение владельца
# от 2026-10-14 с ценой: войти в приложение может любой, у кого есть токен бота согласований (у бота
# «в одно нажатие» — и держатель посредника), — значит, и менять, какие чаты читает ассистент.
# Смягчения: каждое изменение — отдельным сообщением владельцу в бот согласований и записью в
# журнале страницы; список диалогов — из базы, без запросов к Telegram, кроме кнопки «Обновить»;
# по одному чату; без стирания сохранённого.

TITLE_LIMIT = 80              # знаков названия чата в сообщении владельцу
IF_NOT_YOU = ("Если это были не вы — {fix} в «Настройках Штурмана» и нажмите там «Выйти на всех устройствах». "
              "Если бот согласований создан в одно нажатие, замените его ботом, созданным вручную у @BotFather "
              "(«Сменить токен»).")


def _account_id(request: Request) -> int:
    return int(request.path_params["account_id"])


def _title(dialog: Any) -> str:
    name = " ".join(str(getattr(getattr(dialog, "chat", None), "name", None) or "").split())
    if not name:
        return "без названия"
    return name if len(name) <= TITLE_LIMIT else name[:TITLE_LIMIT - 1].rstrip() + "…"


async def _tell(state: AppState, text: str) -> None:
    """Сообщение владельцу в бот согласований о том, что сделано из приложения. Тем же путём, что
    остальные служебные сообщения сервиса (`bridge.notify_owner`: очередь заданий, повторы при
    сбое Telegram), а не «по возможности»: по нему владелец замечает чужие изменения."""
    async with state.pool.acquire() as conn:
        await bridge.notify_owner(conn, text)


@endpoint
async def chats_accounts(request: Request) -> JSONResponse:
    """Аккаунты Telegram с сессией: какой выбран по способу подключения, подключён ли, сколько
    чатов выбрано и глубина истории по умолчанию. Без ключей приложения и входа в аккаунт — пусто."""
    state = state_of(request)
    tg = state.extras.get("tg")
    if not isinstance(tg, TgManager) or not tg.configured:
        return JSONResponse({"accounts": [], "mine": None})
    found = [a for a in await chats.accounts(state, tg) if a["account_id"] is not None]
    chosen = (await page._scenario_state(state))["chosen"]
    keep = ("account_id", "role", "role_name", "label", "status", "backfill_months", "chats_enabled")
    return JSONResponse({"accounts": [{k: a[k] for k in keep} for a in found],
                         "mine": {"own": "owner", "staff": "assistant"}.get(chosen or "")})


@endpoint
async def chats_dialogs(request: Request) -> JSONResponse:
    state = state_of(request)
    return JSONResponse(await chats.list_dialogs(state, chats.manager(state), _account_id(request), request,
                                                 cached=True))


async def _known_title(tg: TgManager, account_id: int, key: tuple[str, int]) -> str:
    """Название чата из уже загруженного списка — для сообщения владельцу. Без запроса к Telegram."""
    try:
        return _title(await chats.find_dialog(tg, account_id, key))
    except (BadRequest, TgError):
        return f"№ {key[1]}"


@endpoint
async def chats_sync(request: Request) -> JSONResponse:
    data = await page._body(request)
    if "chats" in data or "kind" in data:
        raise BadRequest("Из приложения чаты включаются по одному. Все чаты одного вида разом — в "
                         "«Настройках Штурмана».", 400, "one_chat")
    enabled = bool(page._flag(data, "enabled", required=True))
    (key,) = chats.peer_keys([data])
    if enabled and "months" not in data:
        raise BadRequest("Выберите глубину истории для этого чата.")
    months = chats.months_of(data) if enabled else sync.ACCOUNT_DEFAULT
    state, session = state_of(request), request.state.app_session
    tg, account_id = chats.manager(state), _account_id(request)
    if enabled:
        title = _title(await chats.find_dialog(tg, account_id, key))
    else:
        title = await _known_title(tg, account_id, key)
    with authority.miniapp_context(session.user_id, chat_id=session.chat_id):
        result, on = await chats.set_sync(state, tg, account_id, enabled=enabled, keys=[key],
                                          since=await chats.since_of(state, months))
    chat = f"чат {key[0]}:{key[1]}"
    if enabled and not on:
        await page._log(request, "app.sync_on", audit.REFUSED, chat + "; чат исключён из архива")
    elif enabled:
        depth = chats.depth_words(months)
        await page._log(request, "app.sync_on", detail=f"{chat}; глубина: {depth}")
        await _tell(state, f"Из приложения включено чтение чата «{title}» (глубина: {depth}).\n\n"
                           + IF_NOT_YOU.format(fix="выключите этот чат"))
    else:
        await page._log(request, "app.sync_off", detail=chat)
        await _tell(state, f"Из приложения выключено чтение чата «{title}».")
    return JSONResponse({"chats": result, "enabled": on, "enabled_total": await chats.enabled_total(state, account_id)})


@endpoint
async def chats_exclude(request: Request) -> JSONResponse:
    """Не сохранять чат или вернуть его в архив. Стереть уже сохранённое отсюда нельзя."""
    data = await page._body(request)
    if data.get("purge") not in (None, False):
        raise BadRequest("Стереть сохранённые сообщения чата можно только в «Настройках Штурмана».", 400, "page_only")
    excluded = bool(page._flag(data, "excluded", required=True))
    (key,) = chats.peer_keys([data])
    state, session = state_of(request), request.state.app_session
    tg, account_id = chats.manager(state), _account_id(request)
    with authority.miniapp_context(session.user_id, chat_id=session.chat_id):
        dialog, _ = await chats.exclude(state, tg, account_id, key, excluded=excluded)
    title, chat = _title(dialog), f"чат {key[0]}:{key[1]}"
    if excluded:
        await page._log(request, "app.chat_exclude", detail=chat)
        await _tell(state, f"Из приложения чат «{title}» исключён из архива: новые сообщения из него не "
                           "сохраняются, уже сохранённое осталось.\n\n" + IF_NOT_YOU.format(fix="верните чат в архив"))
    else:
        await page._log(request, "app.chat_include", detail=chat)
        await _tell(state, f"Из приложения чат «{title}» возвращён в архив (читать его — отдельной отметкой).")
    return JSONResponse({"excluded": excluded, "purged": 0})


def routes() -> list[BaseRoute]:
    account = APP_API + "/chats/{account_id:int}"
    return [
        Route(APP_PATH, index, methods=["GET"]),
        Route(APP_API + "/login", login, methods=["POST"]),
        Route(APP_API + "/logout", logout, methods=["POST"]),
        Route(APP_API + "/approvals", approvals_list, methods=["GET"]),
        Route(APP_API + "/approvals/decide", approvals_decide, methods=["POST"]),
        Route(APP_API + "/approvals/answer", approvals_answer, methods=["POST"]),
        Route(APP_API + "/chats", chats_accounts, methods=["GET"]),
        Route(account + "/dialogs", chats_dialogs, methods=["GET"]),
        Route(account + "/sync", chats_sync, methods=["POST"]),
        Route(account + "/exclude", chats_exclude, methods=["POST"]),
    ]
