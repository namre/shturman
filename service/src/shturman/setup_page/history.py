"""«История» на странице «Настройки Штурмана» и во вкладке «История» приложения «Штурман» (Р-95, С15, Х6).

  GET   history?tab=decisions|assistant|logins&days=14
        decisions — что ассистент просил и что вы решили (подтверждения действий), изменения правил
                    с «Вернуть как было», выданные и отозванные разрешения; сверху — действующие
                    разрешения на источники и доступы внешних программ с «Отозвать»;
        assistant — что ассистент делал: черновики ответов и их судьба, ответы на сообщения и вопросы
                    вам, репетиция, разбор входящих (числом за день);
        logins    — входы на страницу (по ссылке, по коду) и в приложение, выходы; ниже — весь журнал.
        Всё по дням, словами. Текста переписки, черновиков и ответов нет: только имена и названия чатов,
        как в архиве, и исходы.
  POST  history/{id}/revert               «Вернуть как было» (`history.revert`): ужесточение — сразу,
                                          ослабление — карточкой в бот, «Да» только там
  POST  history/grants/source/{id}/revoke отозвать разрешение прочитать источник — сразу
  POST  history/grants/mcp/{id}/revoke    отозвать доступ внешней программы к архиву — сразу

То же для приложения — под `app/history…` (сессия приложения, контекст владельца из Telegram). Отзыв —
ужесточение, поэтому и из приложения сразу; о нём владельцу уходит сообщение в бот, как о любом
изменении из приложения (Р-80). Журнал действий: `history.revert`, `history.revert_request`,
`grant.revoke` и `app.*` с пометкой — номера и виды, без имён.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from .. import authority, bridge, confirm, history, words
from ..api_core import BadRequest
from ..app import state_of
from ..executor import owner_views
from ..sanitize import clean_line
from . import audit
from . import service as page

HISTORY = page.API + "/history"
APP_HISTORY = page.API + "/app/history"
TABS = ("decisions", "assistant", "logins")
DAYS = (1, 3, 7, 14, 30)
LIMIT = 400

ACTION_STATUS = {"pending": "ждёт вашего решения", "applied": "вы подтвердили", "rejected": "вы отклонили",
                 "expired": "срок вышел, не применено", "failed": "не получилось применить"}
KIND_WORDS = {
    "outbox": "Ответы и отправка", "watch": "Наблюдатель", "archive": "Архив", "tg": "Аккаунт Telegram",
    "commitments": "Договорённость", "people": "Люди", "pages": "Память", "projects": "Проекты",
    "facts": "Профиль", "spheres": "Сферы", "notify": "Уведомления", "rehearsal": "Репетиция",
    "halt": "Остановка", "setup": "Вход на страницу", "triage": "Разбор входящих", "owner": "Ваша просьба",
}
GRANT_WHAT = {"chat": "переписку", "memory": "память о людях", "external": "внешний источник"}
LOGINS = ("login.link", "login.code", "login.code_sent", "login.failed", "login.code_locked", "login.code_denied",
          "login.code_setting", "logout", "logout.all", "app.login", "app.login_failed", "app.logout")


def _zone(config: Any) -> ZoneInfo | None:
    try:
        return ZoneInfo(config.timezone) if getattr(config, "timezone", None) else None
    except Exception:  # noqa: BLE001
        return None


def _day(at: datetime, zone: ZoneInfo | None) -> str:
    return (at.astimezone(zone) if zone else at).date().isoformat()


def _item(at: datetime, kind: str, title: str, **extra: Any) -> dict[str, Any]:
    return {"at": at.isoformat(), "kind": kind, "title": title, **extra}


def _group(items: list[dict[str, Any]], zone: ZoneInfo | None) -> list[dict[str, Any]]:
    items.sort(key=lambda i: i["at"], reverse=True)
    days: dict[str, list[dict[str, Any]]] = {}
    for item in items[:LIMIT]:
        days.setdefault(_day(datetime.fromisoformat(item["at"]), zone), []).append(item)
    return [{"date": d, "items": got} for d, got in days.items()]


def _first_line(text: str | None, limit: int = 300) -> str:
    return clean_line((text or "").strip().split("\n", 1)[0], limit) or "действие"


def _request(raw: Any) -> dict[str, Any]:
    value = json.loads(raw) if isinstance(raw, str) else raw
    return value if isinstance(value, dict) else {}


async def active_grants(conn: asyncpg.Connection, config: Any) -> list[dict[str, Any]]:
    """Действующие разрешения: прочитать источник для ответа и доступ внешних программ к архиву."""
    out: list[dict[str, Any]] = []
    rows = await conn.fetch(
        """SELECT g.id, g.kind, g.mode, g.request, g.created_at, g.expires_at, g.task_id, c.title FROM source_grants g
           JOIN chats c ON c.id = g.target_chat_id
           WHERE g.revoked_at IS NULL AND g.expires_at > now() ORDER BY g.id DESC LIMIT 100""")
    for r in rows:
        query = _request(r["request"]).get("query")
        out.append({"ref": f"source:{r['id']}", "id": r["id"], "type": "source",
                    "title": f"Прочитать {GRANT_WHAT.get(r['kind'], 'источник')}"
                             + (f" по запросу «{clean_line(query, 60)}»" if isinstance(query, str) and query else "")
                             + f" для ответа в «{history.name(r['title'], 'чат без названия')}»",
                    "detail": ("можно пересказать собеседнику" if r["mode"] == "disclose" else "только прочитать")
                              + ("; и для будущих ответов в этом чате" if r["task_id"] is None else ""),
                    "since": r["created_at"].isoformat(),
                    "until": r["expires_at"].isoformat(),
                    "until_words": owner_views.moment(r["expires_at"], config)})
    rows = await conn.fetch(
        """SELECT g.id, c.name, g.created_at, g.expires_at FROM remote_mcp_grants g
           JOIN remote_mcp_clients c ON c.id = g.client_id
           WHERE g.revoked_at IS NULL AND g.expires_at > now() ORDER BY g.id DESC LIMIT 50""")
    for r in rows:
        out.append({"ref": f"mcp:{r['id']}", "id": r["id"], "type": "mcp",
                    "title": f"Внешняя программа «{history.name(r['name'], 'без имени')}» читает архив",
                    "detail": "только чтение", "since": r["created_at"].isoformat(),
                    "until": r["expires_at"].isoformat(), "until_words": owner_views.moment(r["expires_at"], config)})
    return out


async def decisions(conn: asyncpg.Connection, config: Any, days: int, *, app: bool) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    rows = await conn.fetch(
        """SELECT id, kind, summary, status, created_at, decided_at, expires_at FROM pending_actions
           WHERE created_at > now() - make_interval(days => $1) ORDER BY id DESC LIMIT $2""", days, LIMIT)
    for r in rows:
        status = "expired" if r["status"] == "pending" and r["expires_at"] <= datetime.now(timezone.utc) else r["status"]
        items.append(_item(r["created_at"], "action", _first_line(r["summary"]),
                           group=KIND_WORDS.get(r["kind"].split(".")[0], "Действие"),
                           status=ACTION_STATUS.get(status, status), status_code=status,
                           decided_at=r["decided_at"].isoformat() if r["decided_at"] else None,
                           who="Просьба подтвердить"))
    for e in await history.entries(conn, days=days, limit=LIMIT):
        actions = []
        if e["can_revert"] and not (app and e["page_only"]):
            actions.append({"ref": f"revert:{e['id']}", "label": "Вернуть как было"})
        items.append({"at": e["at"], "kind": "change", "title": e["title"], "group": e["area_title"],
                      "who": "Изменено: " + e["via"], "status": "возвращено" if e["undone"] else None,
                      "actions": actions,
                      "note": "Правила разбора входящих возвращаются в «Настройках Штурмана»."
                      if app and e["page_only"] and e["can_revert"] else None})
    rows = await conn.fetch(
        """SELECT g.id, g.kind, g.mode, g.created_at, g.revoked_at, g.expires_at, c.title FROM source_grants g
           JOIN chats c ON c.id = g.target_chat_id
           WHERE g.created_at > now() - make_interval(days => $1) OR g.revoked_at > now() - make_interval(days => $1)
           ORDER BY g.id DESC LIMIT $2""", days, LIMIT)
    for r in rows:
        what = (f"прочитать {GRANT_WHAT.get(r['kind'], 'источник')} для ответа в "
                f"«{history.name(r['title'], 'чат без названия')}»")
        items.append(_item(r["created_at"], "grant", "Разрешено " + what, group="Разрешения",
                           who="Вы разрешили в боте", status=None))
        if r["revoked_at"] is not None:
            items.append(_item(r["revoked_at"], "grant", "Отозвано разрешение " + what, group="Разрешения",
                               who=None, status=None))
    rows = await conn.fetch(
        """SELECT g.id, c.name, g.created_at, g.revoked_at FROM remote_mcp_grants g
           JOIN remote_mcp_clients c ON c.id = g.client_id
           WHERE g.created_at > now() - make_interval(days => $1) OR g.revoked_at > now() - make_interval(days => $1)
           ORDER BY g.id DESC LIMIT 50""", days)
    for r in rows:
        who = history.name(r["name"], "без имени")
        items.append(_item(r["created_at"], "grant", f"Открыт доступ к архиву программе «{who}»", group="Внешние программы",
                           who=None, status=None))
        if r["revoked_at"] is not None:
            items.append(_item(r["revoked_at"], "grant", f"Закрыт доступ к архиву программе «{who}»",
                               group="Внешние программы", who=None, status=None))
    return items


TASK_WORDS = owner_views.TASK_STATE
DRAFT_WORDS = {"pending": "ждёт вашего решения", "approved": "вы приняли, отправляется", "sending": "отправляется",
               "sent": "отправлен", "failed": "не отправлен", "outcome_unknown": "неизвестно, дошёл ли",
               "rejected": "вы отклонили", "expired": "срок вышел, не отправлен", "superseded": "заменён новым"}


async def assistant(conn: asyncpg.Connection, days: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    rows = await conn.fetch(
        """SELECT d.id, d.origin, d.status, d.prepare_only, d.channel, d.created_at, d.finished_at, c.title, c.type
           FROM outbox_drafts d JOIN chats c ON c.id = d.chat_id
           WHERE d.created_at > now() - make_interval(days => $1) ORDER BY d.id DESC LIMIT $2""", days, LIMIT)
    for r in rows:
        where = history.name(r["title"], "чат без названия")
        what = ("Автоответ доверенному" if r["origin"] == "autoreply" else "Черновик ответа") + f" в «{where}»"
        if r["prepare_only"]:
            what += " — только для просмотра"
        items.append(_item(r["created_at"], "draft", what, group=words.DRAFTS_MODE if r["prepare_only"] else "Черновики",
                           status=("отправлен сам (вы включили автоответ)" if r["origin"] == "autoreply"
                                   and r["status"] == "sent" else DRAFT_WORDS.get(r["status"], r["status"])),
                           who=words.VOICE_OWNER if r["channel"] == "business" else words.VOICE_HELPER))
    rows = await conn.fetch(
        """SELECT t.id, t.status, t.created_at, t.owner_decided_at, c.title,
                  (t.owner_question IS NOT NULL) AS asked
           FROM reply_tasks t LEFT JOIN chats c ON c.id = t.chat_id
           WHERE t.created_at > now() - make_interval(days => $1) ORDER BY t.id DESC LIMIT $2""", days, LIMIT)
    for r in rows:
        where = history.name(r["title"], "чат без названия")
        title = (f"Ассистент спросил вас, как ответить в «{where}»" if r["asked"]
                 else f"Ответ на сообщение в «{where}»")
        items.append(_item(r["created_at"], "task", title, group=words.REPLY_QUESTION if r["asked"] else words.REPLY_SOURCE,
                           status=TASK_WORDS.get(r["status"], r["status"]), who=None))
    from ..rehearsal import core as rehearsal
    rows = await conn.fetch(
        """SELECT r.id, r.created_at, r.outcome, r.rating, COALESCE(c.title, p.name) AS who
           FROM rehearsal_items r JOIN chats c ON c.id = r.chat_id JOIN peers p ON p.id = c.peer_id
           WHERE r.state = 'shown' AND r.created_at > now() - make_interval(days => $1)
           ORDER BY r.id DESC LIMIT $2""", days, LIMIT)
    from . import rehearsal as rehearsal_page
    for r in rows:
        items.append(_item(r["created_at"], "rehearsal",
                           f"Репетиция ответа «{history.name(r['who'], 'собеседнику')}»: "
                           + (rehearsal_page.OUTCOME_WORDS.get(r["outcome"] or "", "") or "карточка"),
                           group=words.REHEARSAL,
                           status=("ваша оценка: " + rehearsal.RATING_WORDS[r["rating"]]) if r["rating"] else "без оценки",
                           who=None))
    rows = await conn.fetch(
        """SELECT date_trunc('day', created_at) AS day, min(created_at) AS at, count(*) AS analyzed,
                  count(*) FILTER (WHERE shown_at IS NOT NULL) AS shown,
                  count(*) FILTER (WHERE reminded_at IS NOT NULL) AS reminded
           FROM triage_items WHERE state = 'done' AND created_at > now() - make_interval(days => $1)
           GROUP BY 1 ORDER BY 1 DESC""", days)
    for r in rows:
        items.append(_item(r["at"], "triage", f"Разбор входящих: прочитано {r['analyzed']}, вам показано {r['shown']}"
                           + (f", напоминаний {r['reminded']}" if r["reminded"] else ""), group="Разбор входящих",
                           status=None, who=None))
    return items


async def logins(conn: asyncpg.Connection, days: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = await conn.fetch(
        """SELECT at, action, outcome, detail FROM setup_audit WHERE action = ANY($1::text[])
             AND at > now() - make_interval(days => $2) ORDER BY id DESC LIMIT $3""", list(LOGINS), days, LIMIT)
    items = []
    for r in rows:
        row = audit._row(r)
        items.append({"at": row["at"], "kind": "login", "title": row["title"], "group": "Входы",
                      "status": None if r["outcome"] == audit.OK else "не состоялся", "who": row["detail"] or None})
    log = await audit.recent(conn, 60)
    return items, log


async def view(request: Request, *, app: bool) -> dict[str, Any]:
    state = state_of(request)
    tab = request.query_params.get("tab") or "decisions"
    if tab not in TABS:
        raise BadRequest("Такой вкладки нет.")
    days = page._query_int(request, "days", 14, low=1, high=30)
    zone = _zone(state.config)
    out: dict[str, Any] = {"tab": tab, "days": days, "day_choices": list(DAYS), "timezone": state.config.timezone}
    async with state.ro_pool.acquire() as conn:
        if tab == "decisions":
            out["grants"] = await active_grants(conn, state.config)
            out["days_list"] = _group(await decisions(conn, state.config, days, app=app), zone)
            out["own_bot"] = bridge.owns_bot()
        elif tab == "assistant":
            out["days_list"] = _group(await assistant(conn, days), zone)
        else:
            items, log = await logins(conn, days)
            out["days_list"] = _group(items, zone)
            out["log"] = log
            out["log_key"] = await audit.recent_important(conn, 20)
    return out


@page.endpoint
async def history_get(request: Request) -> JSONResponse:
    return JSONResponse(await view(request, app=False))


def _id(request: Request) -> int:
    return int(request.path_params["item_id"])


async def _revert(request: Request, where: str) -> dict[str, Any]:
    state = state_of(request)
    entry = _id(request)
    async with state.pool.acquire() as conn:
        try:
            out = await history.revert(conn, state, entry, where=where)
        except history.RevertRefused as exc:
            raise BadRequest(exc.message, exc.status, exc.code) from None
        except confirm.NoOwner:
            raise BadRequest("Это возврат нужно подтвердить в боте согласований, а владелец к нему не привязан.",
                             409, "owner_unknown") from None
        except confirm.TooManyPending:
            raise BadRequest("В боте уже ждут решения много карточек. Ответьте на них и повторите.", 429,
                             "too_many_pending") from None
    return out


@page.endpoint
async def history_revert(request: Request) -> JSONResponse:
    out = await _revert(request, "page")
    pending = out.get("status") == confirm.PENDING
    await page._log(request, "history.revert_request" if pending else "history.revert",
                    detail=f"запись № {_id(request)}; откуда: страница")
    return JSONResponse({"ok": True, **{k: out[k] for k in ("status", "text") if k in out},
                         "action_id": out.get("action_id")}, status_code=202 if pending else 200)


async def _revoke(request: Request) -> tuple[str, int, str]:
    """Отозвать разрешение. Возвращает (вид, номер, что отозвано словами)."""
    state = state_of(request)
    kind, grant = request.path_params["grant_type"], _id(request)
    async with state.pool.acquire() as conn:
        if kind == "source":
            row = await conn.fetchrow(
                """SELECT g.kind, c.title FROM source_grants g JOIN chats c ON c.id = g.target_chat_id
                   WHERE g.id = $1 AND g.revoked_at IS NULL""", grant)
            owner = await bridge.get_owner(conn)
            if row is None or owner is None:
                raise BadRequest("Этого разрешения уже нет — список обновлён.", 404, "gone")
            from ..sources import broker
            from ..sources.registry import SourceError
            try:
                await broker.revoke_grant(conn, grant, owner_id=int(owner["user_id"]))
            except SourceError:
                raise BadRequest("Отозвать можно только при своём боте согласований с привязанным владельцем.",
                                 409, "owner_required") from None
            what = (f"разрешение прочитать {GRANT_WHAT.get(row['kind'], 'источник')} для ответа в "
                    f"«{history.name(row['title'], 'чат без названия')}»")
        elif kind == "mcp":
            name = await conn.fetchval(
                """SELECT c.name FROM remote_mcp_grants g JOIN remote_mcp_clients c ON c.id = g.client_id
                   WHERE g.id = $1 AND g.revoked_at IS NULL""", grant)
            if name is None:
                raise BadRequest("Этого доступа уже нет — список обновлён.", 404, "gone")
            from ..remote_mcp import core
            await core.revoke_grant(conn, grant)
            what = f"доступ к архиву программы «{history.name(name, 'без имени')}»"
        else:
            raise BadRequest("Такого разрешения нет.", 404, "gone")
    return kind, grant, what


@page.endpoint
async def grant_revoke(request: Request) -> JSONResponse:
    kind, grant, what = await _revoke(request)
    await page._log(request, "grant.revoke", detail=f"{'источник' if kind == 'source' else 'внешняя программа'} № {grant}; "
                                                     "откуда: страница")
    return JSONResponse({"ok": True, "text": "Отозвано: " + what + "."})


# --- приложение ---------------------------------------------------------------------------------------

async def app_history_get(request: Request) -> JSONResponse:
    return JSONResponse(await view(request, app=True))


async def app_history_revert(request: Request) -> JSONResponse:
    from . import miniapp
    session = request.state.app_session
    with authority.miniapp_context(session.user_id, chat_id=session.chat_id):
        out = await _revert(request, "app")
    pending = out.get("status") == confirm.PENDING
    await page._log(request, "app.history_revert", detail=f"запись № {_id(request)}; "
                    + ("ждёт нажатия в боте" if pending else "сразу"))
    if not pending:
        await miniapp._tell(state_of(request), "В приложении «Штурман» нажато «Вернуть как было» — "
                            "изменение возвращено. Если это были не вы — нажмите «Выйти на всех устройствах» "
                            "в «Настройках Штурмана».")
    return JSONResponse({"ok": True, **{k: out[k] for k in ("status", "text") if k in out},
                         "action_id": out.get("action_id")}, status_code=202 if pending else 200)


async def app_grant_revoke(request: Request) -> JSONResponse:
    from . import miniapp
    session = request.state.app_session
    with authority.miniapp_context(session.user_id, chat_id=session.chat_id):
        kind, grant, what = await _revoke(request)
    await page._log(request, "app.revoke", detail=f"{'источник' if kind == 'source' else 'внешняя программа'} № {grant}")
    await miniapp._tell(state_of(request), "Из приложения «Штурман» отозвано " + what + ".")
    return JSONResponse({"ok": True, "text": "Отозвано: " + what + "."})


def routes() -> list[BaseRoute]:
    from . import miniapp
    grant = "/grants/{grant_type:str}/{item_id:int}/revoke"
    return [
        Route(HISTORY, history_get, methods=["GET"]),
        Route(HISTORY + "/{item_id:int}/revert", history_revert, methods=["POST"]),
        Route(HISTORY + grant, grant_revoke, methods=["POST"]),
        Route(APP_HISTORY, miniapp.endpoint(app_history_get), methods=["GET"]),
        Route(APP_HISTORY + "/{item_id:int}/revert", miniapp.endpoint(app_history_revert), methods=["POST"]),
        Route(APP_HISTORY + grant, miniapp.endpoint(app_grant_revoke), methods=["POST"]),
    ]
