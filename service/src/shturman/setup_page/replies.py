"""«Ответы и доверие» без JSON и номеров (Р-95, находка Б3): страница «Настройки Штурмана» и вкладка
«История» приложения «Штурман» (раздел «Доверие»).

  GET    replies                         доверенные люди (имена из архива, заметка), автоответ по аккаунтам,
                                         черновики по чатам, группы и темы для ответов, обращения
  GET    replies/people?q=               собеседники личных чатов по имени или @имени — кого сделать доверенным
  GET    replies/chats?q=&groups=1       чаты по названию: для «черновики в этом чате» и для групп помощника
  POST   replies/trusted                 {tg_user_id, note?} — добавить доверенного
  DELETE replies/trusted/{tg_user_id}    убрать
  PUT    replies/drafting/{chat_id}      {value: allow | deny | default}
  PUT    replies/autoreply/{account_id}  {enabled}
  POST   replies/groups                  {chat_id, topic_tg_id?} — разрешить отвечать в группе (теме)
  DELETE replies/groups/{chat_id}?topic= — запретить
  PUT    replies/addressing              {aliases: [...]} — обращения к ассистенту в группах («Штурман, …»)

Применяют те же функции, что команды бота и внутренний API (`confirm`, виды `outbox.*`), и каждая пишет
строку «Истории». Кто что может (`history.change`):
  * страница — сам владелец: и расширение, и сужение сразу (`confirm.apply_owner`), журнал `replies.*`;
  * приложение (`app/replies…`) — сужение сразу (`confirm.apply_narrowing`, функция применения сама
    проверяет под блокировкой), расширение — только карточкой `owner.widen` в бот, «Да» под ней в
    приложении не принимается (`confirm.BOT_ONLY`): войти в приложение может держатель токена бота (Р-80).
    Журнал `app.replies` / `app.replies_request`. Сужение из приложения бот сообщает владельцу сам
    (`confirm.tell` в функции применения).
Внутренний API (`/api/outbox/*`) не менялся: там расширение по-прежнему ждёт нажатия в боте.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from .. import authority, bridge, confirm, history, store
from ..api_core import BadRequest
from ..app import state_of
from ..executor import owner_views
from ..outbox import autoreply, policy, scopes
from ..outbox import service as outbox
from ..sanitize import clean_line
from . import service as page

REPLIES = page.API + "/replies"
APP_REPLIES = page.API + "/app/replies"
DRAFTING = ("allow", "deny", "default")
DRAFTING_WORDS = {"allow": "можно", "deny": "нельзя", "default": "как обычно"}


async def view(request: Request) -> dict[str, Any]:
    state = state_of(request)
    config = state.config
    async with state.ro_pool.acquire() as conn:
        trusted = await conn.fetch(
            """SELECT t.tg_user_id, t.note, t.created_at, p.name, p.username,
                      (SELECT c.title FROM chats c WHERE c.peer_id = p.id AND c.type = 'personal_chat' LIMIT 1) AS title
               FROM outbox_trusted t LEFT JOIN peers p ON p.class = 'user' AND p.tg_id = t.tg_user_id
               ORDER BY t.created_at, t.tg_user_id""")
        accounts = await conn.fetch(
            """SELECT a.id AS account_id, a.label, a.role, COALESCE(o.autoreply_enabled, false) AS enabled
               FROM accounts a LEFT JOIN outbox_accounts o ON o.account_id = a.id ORDER BY a.id""")
        chats = await conn.fetch(
            """SELECT o.chat_id, o.drafting, c.title, c.type, a.role FROM outbox_chats o
               JOIN chats c ON c.id = o.chat_id JOIN accounts a ON a.id = c.account_id ORDER BY c.title NULLS LAST""")
        found = [s for s in await scopes.list_scopes(conn) if s.get("enabled", True)]
        aliases = (await scopes.addressing(conn))["aliases"]
        rules = await policy.stored(conn)
        helper_groups = await conn.fetchval(
            """SELECT count(*) FROM chats c JOIN accounts a ON a.id = c.account_id
               WHERE a.role = 'assistant' AND NOT c.excluded AND c.type = ANY($1::text[])""", sorted(scopes.GROUP_TYPES))
        mode = await owner_views.work_mode(conn)
        settings = await autoreply.load(conn)
    sending, preparing = bool(config.sending), bool(getattr(config, "prepare_only", False))
    return {
        "mode": mode, "own_bot": bridge.owns_bot(), "sending": sending, "prepare_only": preparing,
        "trusted": [{"tg_user_id": r["tg_user_id"], "name": clean_line(r["title"] or r["name"], 80) or None,
                     "username": r["username"], "note": r["note"], "since": r["created_at"].isoformat()}
                    for r in trusted],
        "autoreply": {"available": sending or preparing,
                      "why_not": None if sending or preparing else policy.REASONS["sending_disabled"],
                      "accounts": [{"account_id": r["account_id"], "label": clean_line(r["label"], 40) or None,
                                    "role": r["role"], "enabled": r["enabled"]} for r in accounts],
                      "daily_cap": settings.get("daily_cap")},
        "drafting": {"default": rules.get("drafting_default"),
                     "chats": [{"chat_id": r["chat_id"], "value": r["drafting"],
                                "title": clean_line(r["title"], 80) or None, "kind": owner_views.chat_kind(r["type"]),
                                "helper": r["role"] == "assistant"} for r in chats]},
        "groups": [{"chat_id": s["chat_id"], "topic_tg_id": s["topic_tg_id"] or None,
                    "title": clean_line(s.get("title"), 80) or None,
                    "account": clean_line(s.get("account_label"), 40) or None} for s in found],
        "helper_groups": int(helper_groups or 0),
        "addressing": aliases,
    }


@page.endpoint
async def replies_get(request: Request) -> JSONResponse:
    return JSONResponse(await view(request))


async def people_search(request: Request) -> JSONResponse:
    """Собеседники личных чатов любого аккаунта: кого сделать доверенным. Номер — только в ответе для выбора."""
    q = " ".join((request.query_params.get("q") or "").casefold().split())[:80]
    limit = page._query_int(request, "limit", 20, low=1, high=50)
    async with state_of(request).ro_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT p.tg_id, COALESCE(c.title, p.name) AS name, p.username,
                      EXISTS (SELECT 1 FROM outbox_trusted t WHERE t.tg_user_id = p.tg_id) AS trusted,
                      (SELECT max(m.sent_at) FROM messages m WHERE m.chat_id = c.id) AS last_at
               FROM chats c JOIN peers p ON p.id = c.peer_id
               WHERE c.type = 'personal_chat' AND p.class = 'user' AND NOT c.excluded AND p.is_bot IS NOT TRUE
                 AND p.tg_id NOT IN (SELECT tg_user_id FROM accounts WHERE tg_user_id IS NOT NULL)
                 AND ($1 = '' OR strpos(lower(COALESCE(c.title, p.name, '')), $1) > 0
                      OR strpos(lower(COALESCE(p.username, '')), ltrim($1, '@')) > 0)
               ORDER BY last_at DESC NULLS LAST, p.tg_id LIMIT $2""", q, limit + 10)
    seen: set[int] = set()
    items = []
    for r in rows:
        if r["tg_id"] in seen or store.is_blocked_peer("user", r["tg_id"], r["username"]):
            continue
        seen.add(r["tg_id"])
        items.append({"tg_id": r["tg_id"], "name": clean_line(r["name"], 80) or None, "username": r["username"],
                      "trusted": r["trusted"]})
    return JSONResponse({"items": items[:limit]})


async def chats_search(request: Request) -> JSONResponse:
    """Чаты по названию: для правила черновиков (любой читаемый чат) и для групп помощника (groups=1)."""
    q = " ".join((request.query_params.get("q") or "").casefold().split())[:80]
    groups = request.query_params.get("groups") == "1"
    limit = page._query_int(request, "limit", 20, low=1, high=50)
    async with state_of(request).ro_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT c.id, c.title, c.type, a.role, o.drafting,
                      (SELECT max(m.sent_at) FROM messages m WHERE m.chat_id = c.id) AS last_at
               FROM chats c JOIN accounts a ON a.id = c.account_id JOIN peers p ON p.id = c.peer_id
               LEFT JOIN outbox_chats o ON o.chat_id = c.id
               WHERE NOT c.excluded AND p.is_bot IS NOT TRUE
                 AND (NOT $3 OR (a.role = 'assistant' AND c.type = ANY($4::text[])))
                 AND ($1 = '' OR strpos(lower(COALESCE(c.title, '')), $1) > 0)
               ORDER BY last_at DESC NULLS LAST, c.id LIMIT $2""", q, limit, groups, sorted(scopes.GROUP_TYPES))
    return JSONResponse({"items": [{"chat_id": r["id"], "title": clean_line(r["title"], 80) or None,
                                    "kind": owner_views.chat_kind(r["type"]), "helper": r["role"] == "assistant",
                                    "drafting": r["drafting"] or "default"} for r in rows]})


# --- изменения --------------------------------------------------------------------------------------

def _int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BadRequest(f"поле {field}: нужно положительное целое число")
    return value


async def _change(request: Request, where: str) -> tuple[str, dict[str, Any], dict[str, Any], str]:
    """Разбирает запрос: (вид действия, его содержимое, что ответить и записать, текст карточки для бота)."""
    state = state_of(request)
    method, path = request.method, request.url.path
    tail = path.split("/replies", 1)[1]
    data = await page._body(request) if method in ("POST", "PUT") else {}
    place = "в приложении «Штурман»" if where == "app" else "в «Настройках Штурмана»"
    async with state.ro_pool.acquire() as conn:
        if tail == "/trusted" and method == "POST":
            tg_id = _int(data.get("tg_user_id"), "tg_user_id")
            note = data.get("note")
            if note is not None and not isinstance(note, str):
                raise BadRequest("поле note: нужна строка")
            problem, _ = await outbox._trusted_problem(conn, tg_id)
            if problem:
                raise BadRequest(problem[:1].upper() + problem[1:] + ".")
            who = await history.person(conn, tg_id)
            summary = (f"Добавить в доверенные: {who} — вы попросили {place}. Если автоответ включён, ассистент будет "
                       "отвечать этому человеку сам, без вашего согласования каждого ответа.")
            return outbox.TRUSTED_ADD, {"tg_user_id": tg_id, "note": clean_line(note, 200) or None}, \
                {"what": "добавлен доверенный"}, summary
        if tail.startswith("/trusted/") and method == "DELETE":
            tg_id = int(request.path_params["tg_user_id"])
            return outbox.TRUSTED_REMOVE, {"tg_user_id": tg_id}, {"what": "убран доверенный"}, ""
        if tail.startswith("/drafting/"):
            chat_id, value = int(request.path_params["chat_id"]), data.get("value")
            if value not in DRAFTING:
                raise BadRequest("value: allow, deny или default")
            title = await history.chat(conn, chat_id)
            summary = (f"Черновики ответов в чате «{title}»: {DRAFTING_WORDS[value]} — вы попросили {place}. "
                       "Ассистент сможет готовить там черновики; отправка — только по вашему нажатию.")
            return outbox.CHAT_DRAFTING, {"chat_id": chat_id, "value": value}, {"what": "черновики в чате — " + DRAFTING_WORDS[value]}, summary
        if tail.startswith("/autoreply/"):
            account_id, enabled = int(request.path_params["account_id"]), data.get("enabled")
            if not isinstance(enabled, bool):
                raise BadRequest("enabled: true или false")
            label = await conn.fetchval("SELECT label FROM accounts WHERE id = $1", account_id)
            if label is None:
                raise BadRequest("Такого аккаунта нет.", 404)
            count = await conn.fetchval("SELECT count(*) FROM outbox_trusted")
            summary = (f"Включить автоответ доверенным для аккаунта «{clean_line(label, 40)}» — вы попросили {place}. "
                       f"Доверенных: {count}. Этим людям ассистент будет отвечать сам, без вашего согласования "
                       "каждого ответа, если отправка включена на сервере.")
            return outbox.AUTOREPLY, {"account_id": account_id, "enabled": enabled}, \
                {"what": "автоответ " + ("включён" if enabled else "выключен")}, summary
        if tail == "/groups" and method == "POST":
            chat_id = _int(data.get("chat_id"), "chat_id")
            topic = data.get("topic_tg_id")
            topic = _int(topic, "topic_tg_id") if topic is not None else None
            title = await history.chat(conn, chat_id)
            summary = (f"Разрешить ассистенту отвечать в группе «{title}»" + (f", тема № {topic}" if topic else "")
                       + f" — вы попросили {place}. Ответ уходит от аккаунта помощника, когда к нему обращаются, и только "
                       "если отправка включена на сервере.")
            return outbox.GROUP_SCOPE, {"chat_id": chat_id, "topic_tg_id": topic, "enabled": True}, \
                {"what": "группа разрешена"}, summary
        if tail.startswith("/groups/") and method == "DELETE":
            chat_id = int(request.path_params["chat_id"])
            raw = request.query_params.get("topic") or ""
            topic = int(raw) if raw.isdigit() and 0 < int(raw) < 10 ** 18 else None
            return outbox.GROUP_SCOPE, {"chat_id": chat_id, "topic_tg_id": topic, "enabled": False}, \
                {"what": "группа запрещена"}, ""
        if tail == "/addressing":
            try:
                aliases = scopes.validate_addressing({"aliases": data.get("aliases")})["aliases"]
            except ValueError:
                raise BadRequest("Обращение — короткое имя без знаков препинания, например «Штурман»; "
                                 "не больше 16.") from None
            before = (await scopes.addressing(conn))["aliases"]
            new = [a for a in aliases if a.casefold() not in {b.casefold() for b in before}]
            summary = ("Обращения к ассистенту в группах: " + (", ".join(f"«{a}»" for a in aliases) or "нет")
                       + (f" (новые: {', '.join(f'«{a}»' for a in new)})" if new else "")
                       + f" — вы попросили {place}. На такое обращение ассистент откликается в разрешённых группах.")
            return outbox.ADDRESSING, {"aliases": aliases}, {"what": "обращения", "count": len(aliases)}, summary
    raise BadRequest("Такого изменения нет.", 404)


def _refused(exc: Exception) -> BadRequest:
    if isinstance(exc, confirm.NoOwner):
        return BadRequest("Это нужно подтвердить в боте согласований, а владелец к нему не привязан.", 409, "owner_unknown")
    if isinstance(exc, confirm.TooManyPending):
        return BadRequest("В боте уже ждут решения много карточек. Ответьте на них и повторите.", 429, "too_many_pending")
    if isinstance(exc, history.RevertRefused):
        return BadRequest(exc.message, exc.status, exc.code)
    assert isinstance(exc, confirm.Refused)
    return BadRequest(exc.message, exc.status, exc.code)


async def _apply(request: Request, where: str) -> tuple[dict[str, Any], dict[str, Any]]:
    kind, payload, said, summary = await _change(request, where)
    async with state_of(request).pool.acquire() as conn:
        try:
            out = await history.change(conn, kind, payload, where=where, summary=summary or "Изменение настроек.")
        except (confirm.Refused, confirm.NoOwner, confirm.TooManyPending, history.RevertRefused) as exc:
            raise _refused(exc) from None
    return out, said


def _detail(said: dict[str, Any], via: str) -> str:
    return f"что: {said['what']}" + (f"; обращений: {said['count']}" if "count" in said else "") + f"; через: {via}"


@page.endpoint
async def replies_change(request: Request) -> JSONResponse:
    out, said = await _apply(request, "page")
    await page._log(request, "replies.change", detail=_detail(said, "страница"))
    return JSONResponse({"ok": True, "status": out["status"], "text": out.get("text"), **await view(request)})


async def app_replies_get(request: Request) -> JSONResponse:
    return JSONResponse(await view(request))


async def app_replies_change(request: Request) -> JSONResponse:
    session = request.state.app_session
    with authority.miniapp_context(session.user_id, chat_id=session.chat_id):
        out, said = await _apply(request, "app")
    pending = out["status"] == confirm.PENDING
    await page._log(request, "app.replies_request" if pending else "app.replies", detail=_detail(said, "приложение"))
    return JSONResponse({"ok": True, "status": out["status"], "text": out.get("text"),
                         "action_id": out.get("action_id"), **await view(request)},
                        status_code=202 if pending else 200)


def routes() -> list[BaseRoute]:
    from . import miniapp
    out: list[BaseRoute] = []
    for base, wrap_get, wrap_change in (
            (REPLIES, page.endpoint, page.endpoint),
            (APP_REPLIES, miniapp.endpoint, miniapp.endpoint)):
        getter = replies_get if base == REPLIES else wrap_get(app_replies_get)
        change = replies_change if base == REPLIES else wrap_change(app_replies_change)
        out += [
            Route(base, getter, methods=["GET"]),
            Route(base + "/people", wrap_get(people_search), methods=["GET"]),
            Route(base + "/chats", wrap_get(chats_search), methods=["GET"]),
            Route(base + "/trusted", change, methods=["POST"]),
            Route(base + "/trusted/{tg_user_id:int}", change, methods=["DELETE"]),
            Route(base + "/drafting/{chat_id:int}", change, methods=["PUT"]),
            Route(base + "/autoreply/{account_id:int}", change, methods=["PUT"]),
            Route(base + "/groups", change, methods=["POST"]),
            Route(base + "/groups/{chat_id:int}", change, methods=["DELETE"]),
            Route(base + "/addressing", change, methods=["PUT"]),
        ]
    return out

