"""Модуль сервиса: аккаунты Telegram. Маршруты под /api/tg/ и запуск сессий.

Маршруты (все — JSON, токен внутреннего API):

  GET  /api/tg/accounts                          аккаунты и их состояние
  POST /api/tg/login                             начать вход по QR: {role, confirm_owner?}
  GET  /api/tg/login/{login_id}                  состояние входа (QR обновляется сам)
  POST /api/tg/login/{login_id}/password         облачный пароль: {password}
  POST /api/tg/login/{login_id}/cancel           отменить вход
  POST /api/tg/accounts/{id}/logout              выйти: сессия завершается, файл удаляется
  POST /api/tg/accounts/{id}/pause               поставить на паузу
  POST /api/tg/accounts/{id}/resume              снять с паузы
  PUT  /api/tg/accounts/{id}/options             {auto_personal?, auto_groups?, backfill_months?}
  GET  /api/tg/accounts/{id}/dialogs             чаты для экрана выбора: ?offset&limit&type&refresh
  POST /api/tg/accounts/{id}/sync                включить/выключить: {enabled, chats? | types?, since?}
  GET  /api/tg/accounts/{id}/sync                состояние синхронизации: счётчики и курсоры

Секретов в ответах нет. QR-код и ссылка входа отдаются только пока код ждёт сканирования;
облачный пароль принимается один раз и нигде не сохраняется.

Подтверждение владельцем (см. `confirm.py`). Когда у сервиса свой бот согласований, всё, что
расширяет доступ сервиса и ассистента к переписке, ждёт нажатия в боте, и маршрут отвечает 202:
начало входа в аккаунт, снятие с паузы, включение чатов (`sync` с enabled=true), настройки
«брать новые чаты сами» и более глубокая история. Выход, пауза, отмена входа, выключение чатов
и более мелкая история — ужесточения: они применяются сразу, чтобы доступ можно было закрыть
без ожидания.

Вход подтверждается разрешением: после «Да» вход в названной роли можно начать в течение
`LOGIN_PERMIT_SECONDS`, повторив тот же запрос. Сам QR-код в карточку не попадает.
"""

from __future__ import annotations

import contextlib
import time
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from .. import bridge, confirm
from ..api_core import BadRequest, body, handler, need_str, settle
from ..app import AppState, state_of
from ..sanitize import clean_line
from . import gateway, normalize, sync
from .manager import KEEP, ROLE_NAMES, TgError, TgManager

PEER_CLASSES = ("user", "chat", "channel")
LOGIN_PERMIT_SECONDS = 600   # столько действует разрешение начать вход после подтверждения владельца

LOGIN = "tg.login"
RESUME = "tg.resume"
OPTIONS = "tg.options"
SYNC = "tg.sync"
_SINCE_DEFAULT = "account_default"   # в сохранённом действии: «глубина — по настройке аккаунта»
_TYPE_NAMES = {
    "saved_messages": "«Избранное»", "personal_chat": "личные чаты", "bot_chat": "чаты с ботами",
    "private_group": "закрытые группы", "private_supergroup": "закрытые супергруппы",
    "public_supergroup": "открытые супергруппы", "private_channel": "закрытые каналы",
    "public_channel": "открытые каналы",
}

# Работающий модуль — для событий, которые приходят не через состояние сервиса.
_active: TgManager | None = None
# Разрешения начать вход: роль -> до какого момента (time.monotonic) действует.
_login_permits: dict[str, float] = {}


@bridge.on_owner_change
async def _owner_changed(conn: Any, new_user_id: int) -> None:
    if _active is not None:
        await _active.owner_changed(conn, new_user_id)


def _since(data: dict[str, Any]) -> Any:
    """Граница загрузки истории: поля нет — по настройке аккаунта; null — вся история;
    иначе дата «ГГГГ-ММ-ДД» или дата со временем (без пояса — UTC)."""
    if "since" not in data:
        return sync.ACCOUNT_DEFAULT
    raw = data["since"]
    if raw is None:
        return None
    try:
        value = datetime.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        value = None
    if value is None:
        raise BadRequest("поле since: нужна дата вида 2025-10-01 или null (вся история)")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    if value > datetime.now(timezone.utc) + timedelta(days=1) or value.year < 2013:
        raise BadRequest("поле since: дата вне разумных пределов")
    return value


def _manager(request: Request) -> TgManager:
    manager = state_of(request).extras.get("tg")
    if not isinstance(manager, TgManager):
        raise BadRequest("Модуль аккаунтов Telegram не запущен.", 503)
    try:
        manager.require_configured()
    except TgError as exc:
        raise BadRequest(exc.message, exc.status) from None
    return manager


def tg_handler(fn):
    """Как `api_core.handler`, плюс отказы модуля с текстом для владельца."""
    async def inner(request: Request) -> JSONResponse:
        try:
            return await fn(request)
        except TgError as exc:
            raise BadRequest(exc.message, exc.status) from None
        except gateway.AccountUnavailable:
            raise BadRequest("Аккаунт не подключён.", 409) from None
    inner.__name__ = fn.__name__
    return handler(inner)


def _account_id(request: Request) -> int:
    return int(request.path_params["account_id"])


def _flag(data: dict[str, Any], key: str, *, required: bool = False) -> bool | None:
    value = data.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, bool):
        raise BadRequest(f"поле {key}: нужно true или false")
    return value


def _query_int(request: Request, key: str, default: int, *, low: int, high: int) -> int:
    raw = request.query_params.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise BadRequest(f"параметр {key}: нужно целое число") from None
    return max(low, min(high, value))


@tg_handler
async def accounts(request: Request) -> JSONResponse:
    return JSONResponse({"accounts": await _manager(request).list_accounts()})


# --- действия, которые ждут владельца -------------------------------------------------------------

def _tg_applier(kind: str):
    """Регистрирует функцию применения; отказы модуля становятся отказом с текстом для владельца."""
    def deco(fn):
        async def wrapped(conn: Any, payload: dict[str, Any]) -> Any:
            if _active is None:
                raise confirm.Refused("Модуль аккаунтов Telegram не запущен.", 503)
            try:
                return await fn(_active, payload)
            except TgError as exc:
                raise confirm.Refused(exc.message, exc.status) from None
            except gateway.AccountUnavailable:
                raise confirm.Refused("Аккаунт не подключён.", 409) from None
        return confirm.applier(kind)(wrapped)
    return deco


@_tg_applier(LOGIN)
async def _apply_login(manager: TgManager, payload: dict[str, Any]) -> confirm.Done:
    role = payload.get("role")
    if role not in ROLE_NAMES:
        raise confirm.Refused("Поле role: нужно assistant или owner.", 400)
    confirm.must_not_widen(True)      # подключение аккаунта — всегда расширение
    _login_permits[role] = time.monotonic() + LOGIN_PERMIT_SECONDS
    return confirm.Done(note=f"Разрешение действует {LOGIN_PERMIT_SECONDS // 60} минут: начните вход ещё раз "
                             "(экран входа или команда tg-login) и отсканируйте код.")


@_tg_applier(RESUME)
async def _apply_resume(manager: TgManager, payload: dict[str, Any]) -> None:
    # Без нажатия владельца (аккаунт не на паузе) — только переподключение: сама пауза не снимается.
    await manager.resume(int(payload["account_id"]), unpause=not confirm.unconfirmed())


@_tg_applier(OPTIONS)
async def _apply_options(manager: TgManager, payload: dict[str, Any]) -> None:
    account_id = int(payload["account_id"])
    flags = {key: payload.get(key) for key in ("auto_personal", "auto_groups")}
    months = payload["backfill_months"] if "backfill_months" in payload else KEEP
    if confirm.unconfirmed():
        # Без нажатия владельца — только ужесточение, и проверяется оно в самой записи:
        # настройку могли изменить между чтением в маршруте и этим местом.
        confirm.must_not_widen(any(value is True for value in flags.values()))
        if months is not KEEP:
            async with manager.pool.acquire() as conn:
                narrowed = await conn.fetchval(
                    """UPDATE tg_sessions SET backfill_months = $2
                       WHERE account_id = $1 AND $2::int IS NOT NULL
                         AND (backfill_months IS NULL OR backfill_months >= $2) RETURNING true""",
                    account_id, months)
            confirm.must_not_widen(not narrowed)
            months = KEEP
    await manager.set_options(account_id, auto_personal=flags["auto_personal"],
                              auto_groups=flags["auto_groups"], backfill_months=months)


@_tg_applier(SYNC)
async def _apply_sync(manager: TgManager, payload: dict[str, Any]) -> confirm.Done:
    confirm.must_not_widen(payload.get("enabled") is True)    # включение чатов — всегда расширение
    raw, enabled = payload.get("since", _SINCE_DEFAULT), payload.get("enabled") is True
    since = sync.ACCOUNT_DEFAULT if raw == _SINCE_DEFAULT else None if raw is None else datetime.fromisoformat(raw)
    result = await manager.set_sync(
        int(payload["account_id"]), enabled=enabled,
        chats=[(str(c), int(i)) for c, i in payload.get("chats") or []],
        chat_types=list(payload.get("types") or []), since=since)
    on = sum(1 for item in result if item.get("enabled"))
    return confirm.Done(note=f"Включено чатов: {on} из {len(result)}." if enabled else None, result=result)


async def _session(request: Request, account_id: int) -> Any:
    """Сессия аккаунта с его названием и нынешними настройками."""
    async with state_of(request).pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT a.label, s.slot, s.paused, s.auto_personal, s.auto_groups, s.backfill_months
               FROM tg_sessions s JOIN accounts a ON a.id = s.account_id WHERE s.account_id = $1""", account_id)
    if row is None:
        raise TgError("У этого аккаунта нет сессии Telegram.", 404)
    return row


def _account_name(row: Any) -> str:
    return f"«{clean_line(row['label'], 40)}» ({ROLE_NAMES.get(row['slot'], row['slot'])})"


def _depth(months: int | None) -> str:
    return "вся история" if months is None else f"последние {months} мес."


_LOGIN_ASK = {
    "assistant": "Разрешить вход в аккаунт Telegram в роли помощника. После вашего «Да» на сервере можно "
                 "будет начать вход по QR-коду. Аккаунт, которым отсканируют код, станет аккаунтом-помощником: "
                 "сервис сможет читать выбранные вами чаты этого аккаунта, а при включённой отправке — "
                 "писать от его имени.",
    "owner": "Разрешить вход в ваш основной аккаунт Telegram (только чтение). После вашего «Да» на сервере "
             "можно будет начать вход по QR-коду; появится сессия основного аккаунта. Сервис ничего не "
             "читает, пока вы не выберете чаты, и ничего не отправляет от имени этого аккаунта.",
}


@tg_handler
async def login_start(request: Request) -> JSONResponse:
    manager = _manager(request)
    data = await body(request)
    role = need_str(data, "role", limit=16)
    confirm_owner = bool(_flag(data, "confirm_owner"))
    await manager.check_login(role, confirm_owner=confirm_owner)
    if _login_permits.get(role, 0.0) <= time.monotonic():
        # Подключение аккаунта — решение владельца: при своём боте оно ждёт нажатия, и вход
        # начинается повторным запросом, пока действует разрешение.
        async with state_of(request).pool.acquire() as conn:
            answer, _ = await settle(conn, LOGIN, {"role": role}, summary=_LOGIN_ASK[role])
        if answer is not None:
            return answer
    flow = await manager.start_login(role, confirm_owner=confirm_owner)
    _login_permits.pop(role, None)   # разрешение одноразовое: израсходовано начатым входом
    return JSONResponse(flow.snapshot())


@tg_handler
async def login_status(request: Request) -> JSONResponse:
    return JSONResponse(_manager(request).login(request.path_params["login_id"]).snapshot())


@tg_handler
async def login_password(request: Request) -> JSONResponse:
    manager = _manager(request)
    data = await body(request)
    password = data.get("password")
    if not isinstance(password, str) or not password:
        raise BadRequest("поле password: нужна непустая строка")
    flow = await manager.submit_password(request.path_params["login_id"], password)
    return JSONResponse(flow.snapshot())


@tg_handler
async def login_cancel(request: Request) -> JSONResponse:
    flow = await _manager(request).cancel_login(request.path_params["login_id"])
    return JSONResponse(flow.snapshot())


@tg_handler
async def logout(request: Request) -> JSONResponse:
    return JSONResponse(await _manager(request).logout(_account_id(request)))


@tg_handler
async def pause(request: Request) -> JSONResponse:
    await _manager(request).pause(_account_id(request))
    return JSONResponse({"ok": True})


@tg_handler
async def resume(request: Request) -> JSONResponse:
    _manager(request)
    account_id = _account_id(request)
    row = await _session(request, account_id)
    # Пауза — решение владельца (или защита при смене владельца); снять её — снова открыть
    # сервису доступ к аккаунту. Аккаунт не на паузе — просто переподключение.
    summary = (f"Снять с паузы аккаунт {_account_name(row)}: сервис снова подключится к Telegram и "
               "продолжит читать выбранные чаты этого аккаунта.") if row["paused"] else None
    async with state_of(request).pool.acquire() as conn:
        answer, _ = await settle(conn, RESUME, {"account_id": account_id}, summary=summary)
    return answer or JSONResponse({"ok": True})


@tg_handler
async def options(request: Request) -> JSONResponse:
    _manager(request)
    data = await body(request)
    account_id = _account_id(request)
    if "backfill_months" in data:
        months = data["backfill_months"]
        if months is not None and (isinstance(months, bool) or not isinstance(months, int)
                                   or not 1 <= months <= 600):
            raise BadRequest("поле backfill_months: нужно число месяцев от 1 до 600 или null (вся история)")
    flags = {key: _flag(data, key) for key in ("auto_personal", "auto_groups")}
    row = await _session(request, account_id)
    now: dict[str, Any] = {}     # ужесточения: применяются сразу
    later: dict[str, Any] = {}   # расширения: ждут владельца, если у сервиса свой бот
    asks: list[str] = []
    words = {"auto_personal": "сам брать в архив новые личные чаты этого аккаунта",
             "auto_groups": "сам брать в архив новые группы и каналы этого аккаунта"}
    # При своём боте то, что уже так и стоит, не пишется вовсе: писать без владельца можно
    # только ужесточения (см. confirm.must_not_widen).
    skip_same = confirm.required()
    for key, value in flags.items():
        if value is None or (skip_same and value and row[key]):
            continue
        if value and not row[key]:
            later[key] = True
            asks.append(f"• {words[key]} — без вашего выбора каждого чата")
        else:
            now[key] = value
    if "backfill_months" in data and not (skip_same and data["backfill_months"] == row["backfill_months"]):
        old, new = row["backfill_months"], data["backfill_months"]
        deeper = old is not None and (new is None or new > old)
        (later if deeper else now)["backfill_months"] = new
        if deeper:
            asks.append(f"• глубина истории для чатов, которые включат позже: было «{_depth(old)}», "
                        f"станет «{_depth(new)}»")
    async with state_of(request).pool.acquire() as conn:
        if now or not later:
            await settle(conn, OPTIONS, {"account_id": account_id, **now}, summary=None)
        if later:
            answer, _ = await settle(
                conn, OPTIONS, {"account_id": account_id, **later}, applied_now=now or None,
                summary=f"Изменить настройки аккаунта {_account_name(row)}:\n" + "\n".join(asks)
                        + "\nСервис будет сохранять больше переписки, и ассистент сможет её читать.")
            if answer is not None:
                return answer
    return JSONResponse({"ok": True})


@tg_handler
async def dialogs(request: Request) -> JSONResponse:
    manager = _manager(request)
    chat_type = request.query_params.get("type") or None
    if chat_type is not None and chat_type not in normalize.CHAT_TYPES:
        raise BadRequest("параметр type: неизвестный вид чата")
    out = await manager.list_dialogs(
        _account_id(request),
        offset=_query_int(request, "offset", 0, low=0, high=10_000_000),
        limit=_query_int(request, "limit", 100, low=1, high=500),
        chat_type=chat_type,
        refresh=request.query_params.get("refresh") in ("1", "true"),
    )
    return JSONResponse(out)


@tg_handler
async def sync_set(request: Request) -> JSONResponse:
    manager = _manager(request)
    data = await body(request)
    enabled = _flag(data, "enabled", required=True)
    chats, types_ = data.get("chats"), data.get("types")
    keys: list[tuple[str, int]] = []
    if chats is not None:
        if not isinstance(chats, list) or len(chats) > 5000:
            raise BadRequest("поле chats: нужен список чатов")
        for item in chats:
            if not isinstance(item, dict) or item.get("peer_class") not in PEER_CLASSES \
                    or isinstance(item.get("tg_id"), bool) or not isinstance(item.get("tg_id"), int):
                raise BadRequest("поле chats: у каждого чата нужны peer_class (user, chat, channel) и tg_id")
            keys.append((item["peer_class"], item["tg_id"]))
    if types_ is not None:
        if not isinstance(types_, list) or any(t not in normalize.CHAT_TYPES for t in types_):
            raise BadRequest("поле types: нужен список видов чатов")
    if not keys and not types_:
        raise BadRequest("нужно поле chats или types")
    since = _since(data)
    account_id = _account_id(request)
    row = await _session(request, account_id)
    payload = {
        "account_id": account_id, "enabled": bool(enabled), "chats": [list(k) for k in keys],
        "types": list(types_ or []),
        "since": _SINCE_DEFAULT if since is sync.ACCOUNT_DEFAULT else None if since is None else since.isoformat(),
    }
    summary = None      # выключение — ужесточение, применяется сразу
    async with state_of(request).pool.acquire() as conn:
        if enabled and not confirm.required():
            summary = ""    # своего бота нет: применится сразу, текст карточки не нужен
        elif enabled:
            # Включение чата открывает его переписку сервису и ассистенту — ждёт владельца.
            if await _already_on(conn, account_id, keys, types_, since):
                # Всё названное уже включено: менять нечего, и без владельца ничего не пишется.
                return JSONResponse({"chats": [{"peer_class": c, "tg_id": i, "enabled": True} for c, i in keys]})
            summary = await _sync_ask(manager, account_id, row, keys, list(types_ or []), since)
        answer, result = await settle(conn, SYNC, payload, summary=summary)
    return answer or JSONResponse({"chats": result})


async def _already_on(conn: Any, account_id: int, keys: list[tuple[str, int]], types_: Any, since: Any) -> bool:
    """Запрос ничего не расширяет: названные чаты уже включены, глубина истории не меняется."""
    if types_ or since is not sync.ACCOUNT_DEFAULT:
        return False
    on = {(r["peer_class"], r["tg_id"]) for r in await conn.fetch(
        "SELECT peer_class, tg_id FROM tg_sync_chats WHERE account_id = $1 AND enabled", account_id)}
    return all(key in on for key in keys)


async def _sync_ask(manager: TgManager, account_id: int, row: Any, keys: list[tuple[str, int]],
                    types_: list[str], since: Any) -> str:
    """Текст карточки: какие чаты станут читаться. Названия — из списка диалогов аккаунта."""
    known = {d.key: d for d in await manager.dialogs(account_id)}
    lines: list[str] = []
    if keys:
        names = [f"«{clean_line(known[k].chat.name, 40) or f'{k[0]} {k[1]}'}»" if k in known
                 else f"{k[0]} {k[1]} (среди диалогов аккаунта не найден)" for k in keys]
        shown = ", ".join(names[:12]) + (f" и ещё {len(names) - 12}" if len(names) > 12 else "")
        lines.append(f"• чаты ({len(keys)}): {shown}")
    if types_:
        count = sum(1 for d in known.values() if d.chat.type in types_)
        lines.append(f"• все чаты видов: {', '.join(_TYPE_NAMES.get(t, t) for t in types_)} — сейчас таких {count}")
    if since is sync.ACCOUNT_DEFAULT:
        depth = f"по настройке аккаунта — {_depth(row['backfill_months'])}"
    else:
        depth = "вся история" if since is None else f"с {since:%d.%m.%Y}"
    return (f"Включить чтение чатов аккаунта {_account_name(row)}:\n" + "\n".join(lines)
            + f"\nСервис загрузит их историю ({depth}) и будет сохранять новые сообщения; "
              "ассистент сможет их читать.")


@tg_handler
async def sync_status(request: Request) -> JSONResponse:
    return JSONResponse(await _manager(request).sync_status(_account_id(request)))


def routes() -> list[BaseRoute]:
    account = "/api/tg/accounts/{account_id:int}"
    return [
        Route("/api/tg/accounts", accounts, methods=["GET"]),
        Route("/api/tg/login", login_start, methods=["POST"]),
        Route("/api/tg/login/{login_id}", login_status, methods=["GET"]),
        Route("/api/tg/login/{login_id}/password", login_password, methods=["POST"]),
        Route("/api/tg/login/{login_id}/cancel", login_cancel, methods=["POST"]),
        Route(f"{account}/logout", logout, methods=["POST"]),
        Route(f"{account}/pause", pause, methods=["POST"]),
        Route(f"{account}/resume", resume, methods=["POST"]),
        Route(f"{account}/options", options, methods=["PUT"]),
        Route(f"{account}/dialogs", dialogs, methods=["GET"]),
        Route(f"{account}/sync", sync_set, methods=["POST"]),
        Route(f"{account}/sync", sync_status, methods=["GET"]),
    ]


@contextlib.asynccontextmanager
async def lifespan(state: AppState) -> AsyncIterator[None]:
    """Кладёт шлюз в `state.extras["tg"]` и запускает сессии, файлы которых есть на диске.

    Без ключей приложения модуль простаивает: шлюз есть, но ни один аккаунт не запущен.
    """
    global _active
    manager = TgManager(state.config, state.pool, state.events)
    state.extras["tg"] = manager
    _active = manager
    _login_permits.clear()
    await manager.start()
    try:
        yield
    finally:
        if _active is manager:
            _active = None
        _login_permits.clear()
        await manager.stop()
        state.extras.pop("tg", None)
