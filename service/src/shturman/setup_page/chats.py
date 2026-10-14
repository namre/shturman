"""Какие чаты читать: общее для страницы настройки и экрана «Чаты» приложения «Штурман».

Шаг «Какие чаты читать» на странице (`service.py`: `tg_dialogs`, `tg_sync`, `tg_exclude`) и экран
«Чаты» Mini App бота согласований (`miniapp.py`, решение Р-80) делают одно и то же одними и теми же
функциями: список диалогов аккаунта с поиском и отбором, включение и выключение чтения чата,
исключение чата из архива и возврат. Различия — только в том, что разрешено вызывающему:

  * приложение берёт список диалогов **только из памяти** сервиса (`cached=True`): в Telegram оно
    ходит лишь по кнопке «Обновить список из Telegram». Включение и исключение там тоже ищут чат
    только в уже загруженном списке;
  * приложение включает и выключает чаты поштучно — «все группы разом» есть только на странице;
  * приложение не стирает уже сохранённые сообщения — `purge` только на странице.

Чего здесь нет: журнала действий и сообщений владельцу — их пишет вызывающий маршрут, потому что
пометка («со страницы» или «из приложения») и текст у них свои.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request

from .. import confirm, ingest_api, store
from ..api_core import BadRequest
from ..app import AppState
from ..tg import sync
from ..tg.manager import ROLE_NAMES, TgManager

# Виды чатов для отбора в списке диалогов.
KINDS = {
    "personal": ("personal_chat", "saved_messages", "bot_chat"),
    "group": ("private_group", "private_supergroup", "public_supergroup"),
    "channel": ("private_channel", "public_channel"),
}
PEER_CLASSES = ("user", "chat", "channel")
MAX_MONTHS = 600                 # глубина истории — от месяца до 50 лет либо «вся история»


def manager(state: AppState) -> TgManager:
    found = state.extras.get("tg")
    if not isinstance(found, TgManager):
        raise BadRequest("Модуль аккаунтов Telegram не запущен.", 503)
    if not found.configured:
        raise BadRequest("Сначала введите ключи приложения Telegram — шаг «Ключи приложения Telegram».", 409, "no_keys")
    return found


def query_int(request: Request, key: str, default: int, *, low: int, high: int) -> int:
    raw = request.query_params.get(key) or ""
    if not raw:
        return default
    if not raw.isdigit() or len(raw) > 9:
        raise BadRequest(f"параметр {key}: нужно целое число")
    return max(low, min(high, int(raw)))


async def accounts(state: AppState, tg: TgManager) -> list[dict[str, Any]]:
    """Аккаунты с сессией и сколько чатов у каждого выбрано, загружено и потеряно."""
    found = await tg.list_accounts()
    async with state.ro_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT account_id, count(*) FILTER (WHERE enabled) AS enabled,
                      count(*) FILTER (WHERE enabled AND backfill_done) AS done,
                      count(*) FILTER (WHERE enabled AND access_lost_at IS NOT NULL) AS lost
               FROM tg_sync_chats WHERE chat_id IS NOT NULL GROUP BY account_id""")
    counts = {r["account_id"]: r for r in rows}
    for account in found:
        row = counts.get(account["account_id"])
        account["role_name"] = ROLE_NAMES.get(account["role"], account["role"])
        account["chats_enabled"] = int(row["enabled"]) if row else 0
        account["chats_loaded"] = int(row["done"]) if row else 0
        account["chats_lost"] = int(row["lost"]) if row else 0
    return found


async def enabled_total(state: AppState, account_id: int) -> int:
    async with state.ro_pool.acquire() as conn:
        return int(await conn.fetchval(
            "SELECT count(*) FROM tg_sync_chats WHERE account_id = $1 AND enabled", account_id))


async def list_dialogs(state: AppState, tg: TgManager, account_id: int, request: Request, *,
                       cached: bool = False) -> dict[str, Any]:
    """Страница списка диалогов по параметрам запроса `?offset&limit&q&kind&only&refresh` —
    с видом чата, отметкой служебных чатов и числом выбранных."""
    kind = request.query_params.get("kind") or ""
    if kind and kind not in KINDS:
        raise BadRequest("параметр kind: personal, group или channel")
    out = await tg.list_dialogs(
        account_id,
        offset=query_int(request, "offset", 0, low=0, high=10_000_000),
        limit=query_int(request, "limit", 50, low=1, high=200),
        chat_types=KINDS.get(kind), query=(request.query_params.get("q") or "")[:200],
        only_enabled=request.query_params.get("only") == "enabled",
        refresh=request.query_params.get("refresh") == "1", cached=cached,
    )
    for item in out["items"]:
        item["kind"] = next((name for name, types in KINDS.items() if item["type"] in types), "personal")
        # Служебные чаты Telegram (коды входа, @BotFather) исключены всегда: вернуть их нельзя.
        item["locked"] = store.is_blocked_peer(item["peer_class"], item["tg_id"], item["username"])
    out["enabled_total"] = await enabled_total(state, account_id)
    return out


def peer_keys(raw: Any) -> list[tuple[str, int]]:
    if not isinstance(raw, list) or len(raw) > 5000:
        raise BadRequest("поле chats: нужен список чатов")
    keys = []
    for item in raw:
        if not isinstance(item, dict) or item.get("peer_class") not in PEER_CLASSES \
                or isinstance(item.get("tg_id"), bool) or not isinstance(item.get("tg_id"), int):
            raise BadRequest("поле chats: у каждого чата нужны peer_class и tg_id")
        keys.append((item["peer_class"], item["tg_id"]))
    return keys


def months_of(data: dict[str, Any], key: str = "months") -> Any:
    """Глубина истории из запроса: число месяцев, None — вся история; поля нет — `sync.ACCOUNT_DEFAULT`."""
    if key not in data:
        return sync.ACCOUNT_DEFAULT
    months = data[key]
    if months is not None and (isinstance(months, bool) or not isinstance(months, int) or not 1 <= months <= MAX_MONTHS):
        raise BadRequest("Глубина истории — число месяцев от 1 до 600 либо «вся история».")
    return months


async def since_of(state: AppState, months: Any) -> Any:
    """Граница загрузки истории для `months_of`: та же арифметика, что у настройки аккаунта
    (`now() - make_interval(months => …)` в `sync.enable_chat`)."""
    if months is sync.ACCOUNT_DEFAULT or months is None:
        return months
    async with state.ro_pool.acquire() as conn:
        return await conn.fetchval("SELECT now() - make_interval(months => $1)", int(months))


def depth_words(months: Any) -> str:
    """«12 месяцев», «2 года», «вся история» — для сообщений владельцу."""
    if months is None:
        return "вся история"
    if months % 12 == 0:
        years = months // 12
        word = "год" if years % 10 == 1 and years % 100 != 11 else \
            "года" if years % 10 in (2, 3, 4) and years % 100 not in (12, 13, 14) else "лет"
        return f"{years} {word}"
    word = "месяц" if months % 10 == 1 and months % 100 != 11 else \
        "месяца" if months % 10 in (2, 3, 4) and months % 100 not in (12, 13, 14) else "месяцев"
    return f"{months} {word}"


async def set_sync(state: AppState, tg: TgManager, account_id: int, *, enabled: bool, keys: list[tuple[str, int]],
                   kind: str | None = None, since: Any = sync.ACCOUNT_DEFAULT,
                   cached: bool = False) -> tuple[list[dict[str, Any]], int]:
    """Включить или выключить чтение чатов; возвращает итог по каждому и сколько включено."""
    if kind is not None and kind not in KINDS:
        raise BadRequest("поле kind: personal, group или channel")
    if not keys and kind is None:
        raise BadRequest("Не выбрано ни одного чата.")
    result = await tg.set_sync(account_id, enabled=enabled, chats=keys,
                               chat_types=list(KINDS[kind]) if kind else None, since=since, cached=cached)
    return result, sum(1 for item in result if item.get("enabled"))


async def find_dialog(tg: TgManager, account_id: int, key: tuple[str, int], *, cached: bool = False) -> Any:
    dialog = next((d for d in await tg.dialogs(account_id, cached=cached) if d.key == key), None)
    if dialog is None:
        raise BadRequest("Чат не найден среди диалогов аккаунта.", 404)
    return dialog


async def exclude(state: AppState, tg: TgManager, account_id: int, key: tuple[str, int], *, excluded: bool,
                  purge: bool = False, cached: bool = False) -> tuple[Any, int]:
    """Исключить чат аккаунта из архива (по желанию — стерев сохранённое) или вернуть его.
    Действие самого владельца: применяется сразу (`confirm.apply_owner`). Возвращает (диалог,
    сколько сообщений стёрто)."""
    if purge and not excluded:
        raise BadRequest("Стереть сообщения можно только вместе с исключением чата.")
    dialog = await find_dialog(tg, account_id, key, cached=cached)
    if store.is_blocked_peer(dialog.chat.peer_class, dialog.chat.tg_id, dialog.chat.username):
        raise BadRequest("Служебный чат Telegram исключён всегда: в нём коды входа и токены.", 409, "locked")
    purged = 0
    async with state.pool.acquire() as conn:
        chat_id, _ = await store.ensure_chat(conn, account_id, dialog.chat, refresh=True)
        if not excluded:
            await confirm.apply_owner(conn, ingest_api.CHAT_INCLUDE, {"chat_id": chat_id})
        else:
            await confirm.apply_owner(conn, ingest_api.CHAT_EXCLUDE, {"chat_id": chat_id})
            if purge:
                purged = (await confirm.apply_owner(conn, ingest_api.CHAT_PURGE, {"chat_id": chat_id}))["result"] or 0
    return dialog, purged

