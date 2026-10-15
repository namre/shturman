"""Уведомления, наблюдатель и повтор после сбоя — на странице настройки и в приложении (Р-84).

Страница («Переписка»; `PREFIX/api/…`, сессия страницы) — действие самого владельца, сразу:

  GET    notify                уведомления: настройки, умолчание ли, счётчики, сферы, выбранные люди
  PUT    notify                изменить: поля как у `PUT /api/notify` (notify/service.py)
  GET    notify/people?q=      собеседники личных чатов основного аккаунта — для «только от выбранных людей»
  GET    watch                 правила наблюдателя (с названиями чатов и сфер), шаблоны, сферы
  POST   watch                 завести правило: {name, description, keywords?, regexes?, chat_ids?, sphere_ids?,
                               use_lemmas?, enabled?}
  PUT    watch/{id}            изменить правило (те же поля, любые)
  DELETE watch/{id}            удалить правило вместе с журналом срабатываний
  GET    watch/hits?rule_id=   последние срабатывания: правило, чат, время, итог, совпавшие слова, причина
                               от модели — без текста сообщения
  GET    watch/chats?q=        группы и каналы архива — для выбора чатов правила
  GET    retry                 что не получилось из-за сбоя модели и можно повторить; прошлый повтор
  POST   retry                 повторить: {since?: "ГГГГ-ММ-ДД"} — с какой даты считать сбой

Приложение (`PREFIX/api/app/notify`, сессия приложения): смотреть настройки и сужать — выключить,
убрать людей или сферы, не показывать текст; время и частоту — тоже. Расширение — только на странице:
войти в приложение может и держатель токена бота согласований (Р-80), а уведомления с началом
сообщений приходят в чат этого бота. Каждое изменение из приложения бот сообщает владельцу.

Наблюдатель здесь — те же действия, что через внутренний API (`watch.rule_create|update|delete`), но
применяются сразу (`confirm.apply_owner`). Повтор — `retry.run`; во внутреннем API его нет.

Журнал действий: `notify.settings`, `app.notify`, `watch.create|update|delete`, `retry.failed` — коды,
номера и числа; ни имён, ни названий, ни слов правил.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from .. import authority, confirm, retry, store
from ..api_core import BadRequest
from ..app import state_of
from ..notify import core as notify_core
from ..notify import service as notify_service
from ..notify import settings as ns
from ..outbox import service as outbox_service
from ..outbox import text as textlib
from ..outbox import watcher
from ..sanitize import clean_line
from . import audit
from . import service as page

NOTIFY = page.API + "/notify"
WATCH = page.API + "/watch"
RETRY = page.API + "/retry"
APP_NOTIFY = page.API + "/app/notify"

# Готовые правила наблюдателя: владелец выбирает чаты или сферы, остальное можно поправить.
TEMPLATES = [
    {"id": "money", "name": "Сроки и деньги",
     "keywords": ["срок", "дедлайн", "аванс", "оплата", "счёт", "договор"], "use_lemmas": True,
     "description": "Сообщение про сроки, оплату, счета, авансы или договоры, которое касается меня или "
                    "моих дел и требует внимания."},
    {"id": "urgent", "name": "Срочно",
     "keywords": ["срочно", "горит", "asap", "немедленно", "как можно скорее"], "use_lemmas": True,
     "description": "Срочная просьба или проблема, на которую нужно быстро отреагировать."},
    {"id": "name", "name": "Ваше имя", "keywords": [], "ask_name": True, "use_lemmas": True,
     "description": "Обсуждают меня или обращаются ко мне по имени; важно, если от меня что-то ждут."},
]


# --- уведомления -----------------------------------------------------------------------------------

async def _people_names(conn: Any, ids: list[int]) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT tg_id, name, username FROM peers WHERE class = 'user' AND tg_id = ANY($1::bigint[])", ids)
    found = {r["tg_id"]: r for r in rows}
    return [{"tg_id": i, "name": clean_line(found[i]["name"], 80) if i in found and found[i]["name"] else None,
             "username": found[i]["username"] if i in found else None} for i in ids]


async def notify_view(request: Request) -> dict[str, Any]:
    state = state_of(request)
    async with state.ro_pool.acquire() as conn:
        out = await notify_service.view(state, conn)
        titles = await notify_service.sphere_titles(conn)
        out["people"] = await _people_names(conn, out["settings"]["people"])
        out["spheres"] = [{"id": r["id"], "title": r["title"]}
                          for r in await conn.fetch("SELECT id, title FROM spheres ORDER BY position, id")]
        out["describe"] = ns.describe(out["settings"], sphere_titles=titles)
        # «Не сообщать об этом чате» (кнопка под уведомлением): чаты с названиями — вернуть можно здесь.
        muted = await _chat_titles(conn, out["settings"]["muted_chats"])
        out["muted"] = [muted.get(i) or {"id": i, "title": None} for i in out["settings"]["muted_chats"]]
    out["timezone"] = state.config.timezone
    return out


@page.endpoint
async def notify_get(request: Request) -> JSONResponse:
    return JSONResponse(await notify_view(request))


def _changes(data: dict[str, Any]) -> dict[str, Any]:
    try:
        changes = ns.validate(data)
    except ValueError as exc:
        raise BadRequest(str(exc)) from None
    if not changes:
        raise BadRequest("Нечего менять.")
    return changes


@page.endpoint
async def notify_put(request: Request) -> JSONResponse:
    changes = _changes(await page._body(request))
    async with state_of(request).pool.acquire() as conn:
        out = await confirm.apply_owner(conn, notify_service.SETTINGS, {"changes": changes})
    await page._log(request, "notify.settings", detail=ns.audit_detail(out["result"]["settings"]))
    return JSONResponse({"ok": True, **await notify_view(request)})


async def people_search(request: Request) -> JSONResponse:
    """Собеседники личных чатов основного аккаунта по имени — для уведомлений и репетиции (страница и
    вкладка «Репетиция» приложения)."""
    q = " ".join((request.query_params.get("q") or "").casefold().split())[:80]
    limit = page._query_int(request, "limit", 30, low=1, high=100)
    async with state_of(request).ro_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT p.tg_id, COALESCE(c.title, p.name) AS name, p.username,
                      (SELECT max(m.sent_at) FROM messages m WHERE m.chat_id = c.id) AS last_at
               FROM chats c JOIN peers p ON p.id = c.peer_id JOIN accounts a ON a.id = c.account_id
               WHERE c.type = 'personal_chat' AND p.class = 'user' AND NOT c.excluded AND a.role = 'owner'
                 AND p.is_bot IS NOT TRUE
                 AND ($1 = '' OR strpos(lower(COALESCE(c.title, p.name, '')), $1) > 0
                      OR strpos(lower(COALESCE(p.username, '')), ltrim($1, '@')) > 0)
               ORDER BY last_at DESC NULLS LAST, p.tg_id LIMIT $2""", q, limit + 10)
    items = [{"tg_id": r["tg_id"], "name": clean_line(r["name"], 80) or None, "username": r["username"],
              "last_at": r["last_at"].isoformat() if r["last_at"] else None}
             for r in rows if not store.is_blocked_peer("user", r["tg_id"], r["username"])][:limit]
    return JSONResponse({"items": items})


notify_people = page.endpoint(people_search)


# --- приложение: смотреть и сужать ------------------------------------------------------------------

async def app_notify_get(request: Request) -> JSONResponse:
    return JSONResponse(await notify_view(request))


async def app_notify_put(request: Request) -> JSONResponse:
    from . import miniapp
    changes = _changes(await page._body(request))
    state, session = state_of(request), request.state.app_session
    async with state.pool.acquire() as conn:
        before = await ns.load(conn)
        after = {**before, **changes}
        wider = ns.widening(before, after)
        if wider:
            await page._log(request, "app.notify", audit.REFUSED, "расширение — только на странице: " + ",".join(wider))
            raise BadRequest("Из приложения уведомления можно только сузить или перенастроить по времени. "
                             "Включить больше — в «Настройках Штурмана».", 400, "page_only")
        with authority.miniapp_context(session.user_id, chat_id=session.chat_id):
            out = await confirm.apply_owner(conn, notify_service.SETTINGS, {"changes": changes})
        titles = await notify_service.sphere_titles(conn)
    after = out["result"]["settings"]
    await page._log(request, "app.notify", detail=ns.audit_detail(after))
    if after != before:
        await miniapp._tell(state, "Из приложения изменены уведомления о новых сообщениях:\n"
                            + "\n".join(ns.describe(after, sphere_titles=titles)))
    return JSONResponse({"ok": True, **await notify_view(request)})


# --- наблюдатель -------------------------------------------------------------------------------------

async def _chat_titles(conn: Any, ids: list[int]) -> dict[int, dict[str, Any]]:
    rows = await conn.fetch(
        """SELECT c.id, COALESCE(c.title, p.name) AS title, c.type, c.excluded FROM chats c
           JOIN peers p ON p.id = c.peer_id WHERE c.id = ANY($1::bigint[])""", ids)
    return {r["id"]: {"id": r["id"], "title": clean_line(r["title"], 80) or None, "type": r["type"],
                      "excluded": r["excluded"]} for r in rows}


@page.endpoint
async def watch_list(request: Request) -> JSONResponse:
    async with state_of(request).ro_pool.acquire() as conn:
        rules = [watcher.rule_public(r) for r in await conn.fetch("SELECT * FROM watch_rules ORDER BY id")]
        chats = await _chat_titles(conn, sorted({i for r in rules for i in r["chat_ids"]}))
        sphere_rows = await conn.fetch("SELECT id, title FROM spheres ORDER BY position, id")
        counts = {r["rule_id"]: dict(r) for r in await conn.fetch(
            """SELECT rule_id, count(*) AS total,
                      count(*) FILTER (WHERE notified AND created_at > now() - interval '7 days') AS notified_7d,
                      max(created_at) AS last_at
               FROM watch_hits GROUP BY rule_id""")}
    titles = {r["id"]: r["title"] for r in sphere_rows}
    for rule in rules:
        rule["chats"] = [chats.get(i) or {"id": i, "title": None, "type": None, "excluded": None}
                         for i in rule["chat_ids"]]
        rule["spheres"] = [{"id": i, "title": titles.get(i)} for i in rule["sphere_ids"]]
        c = counts.get(rule["id"]) or {}
        rule["hits"] = {"total": int(c.get("total") or 0), "notified_7d": int(c.get("notified_7d") or 0),
                        "last_at": c["last_at"].isoformat() if c.get("last_at") else None}
    return JSONResponse({"rules": rules, "templates": TEMPLATES,
                         "spheres": [{"id": r["id"], "title": r["title"]} for r in sphere_rows],
                         "limits": {"keywords": watcher.MAX_KEYWORDS, "regexes": watcher.MAX_REGEXES,
                                    "chats": watcher.MAX_CHATS}})


def _rule(data: dict[str, Any], *, partial: bool) -> dict[str, Any]:
    try:
        return watcher.validate_rule(data, partial=partial)
    except ValueError as exc:
        raise BadRequest(str(exc)) from None


def _rule_detail(rule: dict[str, Any]) -> str:
    return (f"правило № {rule['id']}; чатов: {len(rule['chat_ids'])}; сфер: {len(rule['sphere_ids'])}; "
            f"слов: {len(rule['keywords'])}; выражений: {len(rule['regexes'])}; "
            f"{'включено' if rule['enabled'] else 'выключено'}")


@page.endpoint
async def watch_create(request: Request) -> JSONResponse:
    rule = _rule(await page._body(request), partial=False)
    async with state_of(request).pool.acquire() as conn:
        out = await confirm.apply_owner(conn, outbox_service.RULE_CREATE, rule)
    await page._log(request, "watch.create", detail=_rule_detail(out["result"]))
    return JSONResponse({"ok": True, "rule": out["result"]})


@page.endpoint
async def watch_update(request: Request) -> JSONResponse:
    changes = _rule(await page._body(request), partial=True)
    if not changes:
        raise BadRequest("Нечего менять.")
    rule_id = int(request.path_params["rule_id"])
    async with state_of(request).pool.acquire() as conn:
        out = await confirm.apply_owner(conn, outbox_service.RULE_UPDATE, {"rule_id": rule_id, "changes": changes})
    await page._log(request, "watch.update", detail=_rule_detail(out["result"]))
    return JSONResponse({"ok": True, "rule": out["result"]})


@page.endpoint
async def watch_delete(request: Request) -> JSONResponse:
    rule_id = int(request.path_params["rule_id"])
    async with state_of(request).pool.acquire() as conn:
        hits = await conn.fetchval("SELECT count(*) FROM watch_hits WHERE rule_id = $1", rule_id)
        await confirm.apply_owner(conn, outbox_service.RULE_DELETE, {"rule_id": rule_id})
    await page._log(request, "watch.delete", detail=f"правило № {rule_id}; срабатываний: {hits}")
    return JSONResponse({"ok": True})


STATUS_WORDS = {"checking": "проверяет модель", "relevant": "важно", "not_relevant": "не важно",
                "duplicate": "повтор", "limited": "предел проверок", "failed": "модель недоступна",
                "no_answer": "модель не ответила"}


@page.endpoint
async def watch_hits(request: Request) -> JSONResponse:
    rule_id = page._query_int(request, "rule_id", 0, low=0, high=10**12) or None
    limit = page._query_int(request, "limit", 30, low=1, high=100)
    async with state_of(request).ro_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT h.id, h.rule_id, r.name AS rule_name, h.chat_id, COALESCE(c.title, p.name) AS chat_title,
                      h.created_at, h.status, h.matched, h.reason, h.notified, m.tg_message_id,
                      p.class AS peer_class, p.tg_id, p.username
               FROM watch_hits h JOIN watch_rules r ON r.id = h.rule_id JOIN chats c ON c.id = h.chat_id
               JOIN peers p ON p.id = c.peer_id JOIN messages m ON m.id = h.message_id
               WHERE ($1::bigint IS NULL OR h.rule_id = $1) AND NOT c.excluded AND m.deleted_at IS NULL
               ORDER BY h.id DESC LIMIT $2""", rule_id, limit)
    items = [{"id": r["id"], "rule_id": r["rule_id"], "rule_name": r["rule_name"], "chat_id": r["chat_id"],
              "chat_title": clean_line(r["chat_title"], 80) or None, "at": r["created_at"].isoformat(),
              "status": r["status"], "status_text": STATUS_WORDS.get(r["status"], r["status"]),
              "matched": list(r["matched"]), "reason": textlib.one_line(r["reason"], 200) or None,
              "notified": r["notified"], "link": watcher._link(r)} for r in rows]
    return JSONResponse({"items": items})


@page.endpoint
async def watch_chats(request: Request) -> JSONResponse:
    q = " ".join((request.query_params.get("q") or "").casefold().split())[:80]
    limit = page._query_int(request, "limit", 30, low=1, high=100)
    async with state_of(request).ro_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT c.id, COALESCE(c.title, p.name) AS title, c.type, a.role
               FROM chats c JOIN peers p ON p.id = c.peer_id JOIN accounts a ON a.id = c.account_id
               WHERE p.class IN ('chat', 'channel') AND NOT c.excluded
                 AND ($1 = '' OR strpos(lower(COALESCE(c.title, p.name, '')), $1) > 0)
               ORDER BY lower(COALESCE(c.title, p.name, '')), c.id LIMIT $2""", q, limit)
    return JSONResponse({"items": [{"id": r["id"], "title": clean_line(r["title"], 80) or None, "type": r["type"],
                                    "account_role": r["role"]} for r in rows]})


# --- повтор после сбоя --------------------------------------------------------------------------------

def _since(data: dict[str, Any]) -> datetime | None:
    raw = data.get("since")
    if raw in (None, ""):
        return None
    if not isinstance(raw, str):
        raise BadRequest("поле since: дата вида 2026-10-01")
    try:
        day = datetime.strptime(raw.strip(), "%Y-%m-%d")
    except ValueError:
        raise BadRequest("поле since: дата вида 2026-10-01") from None
    return day.replace(tzinfo=notify_core.zone("UTC"))


@page.endpoint
async def retry_get(request: Request) -> JSONResponse:
    state = state_of(request)
    async with state.ro_pool.acquire() as conn:
        found = await retry.candidates(conn, media_days=state.config.media_days)
        last = await retry.last(conn)
    return JSONResponse({**found, "media_days": state.config.media_days, "last": last})


@page.endpoint
async def retry_post(request: Request) -> JSONResponse:
    state = state_of(request)
    since = _since(await page._body(request))
    if since is not None:
        since = since.replace(tzinfo=notify_core.zone(state.config.timezone))
    async with state.pool.acquire() as conn:
        out = await retry.run(conn, media_days=state.config.media_days, since=since, by="page")
    media = state.extras.get("media")
    if media is not None and out["media"]:
        media.wake.set()
    await page._log(request, "retry.failed", detail=f"вложений: {out['media']}; запросов обработки: {out['requests']}"
                    + (f"; с {since.date().isoformat()}" if since else ""))
    return JSONResponse({"ok": True, **out})


# --- сборка ----------------------------------------------------------------------------------------

def routes() -> list[BaseRoute]:
    from . import miniapp

    return [
        Route(NOTIFY, notify_get, methods=["GET"]),
        Route(NOTIFY, notify_put, methods=["PUT"]),
        Route(NOTIFY + "/people", notify_people, methods=["GET"]),
        Route(WATCH, watch_list, methods=["GET"]),
        Route(WATCH, watch_create, methods=["POST"]),
        Route(WATCH + "/hits", watch_hits, methods=["GET"]),
        Route(WATCH + "/chats", watch_chats, methods=["GET"]),
        Route(WATCH + "/{rule_id:int}", watch_update, methods=["PUT"]),
        Route(WATCH + "/{rule_id:int}", watch_delete, methods=["DELETE"]),
        Route(RETRY, retry_get, methods=["GET"]),
        Route(RETRY, retry_post, methods=["POST"]),
        Route(APP_NOTIFY, miniapp.endpoint(app_notify_get), methods=["GET"]),
        Route(APP_NOTIFY, miniapp.endpoint(app_notify_put), methods=["PUT"]),
    ]
