"""Экран «Память ассистента» на странице настройки: люди, проекты, профиль владельца и то, что ждёт его решения.

  GET    /shturman-setup/api/memory/pages                          страницы о людях; ?q= — поиск
  GET    /shturman-setup/api/memory/pages/{person_id}              страница: блоки текстом, факты, пометки
  PUT    /shturman-setup/api/memory/pages/{person_id}/owner-block  ваши заметки: {text}
  GET    /shturman-setup/api/memory/projects                       заведённые проекты (действующие, затем архив)
  POST   /shturman-setup/api/memory/projects                       завести проект: {title, chat_ids?, aliases?}
  GET    /shturman-setup/api/memory/projects/{id}                  страница проекта, факты, решения, чаты
  PUT    /shturman-setup/api/memory/projects/{id}/chats            весь перечень чатов: {chat_ids}
  POST   /shturman-setup/api/memory/projects/{id}/archive          в архив
  PUT    /shturman-setup/api/memory/projects/{id}/owner-block      ваши заметки о проекте: {text}
  GET    /shturman-setup/api/memory/chats                          чаты, которые читает сервис (для выбора)
  GET    /shturman-setup/api/memory/profile                        профиль: одобренные факты о вас, ваши правила
  PUT    /shturman-setup/api/memory/profile/owner-block            ваши правила и указания: {text}
  POST   /shturman-setup/api/memory/facts/{id}/retract             «Неверно»: факт больше не действует
  GET    /shturman-setup/api/memory/pending                        что ждёт решения владельца
  POST   /shturman-setup/api/memory/pending/pages/{person_id}      завести страницу или нет: {accept}
  POST   /shturman-setup/api/memory/pending/commitments/{id}       договорённость верна или нет: {accept, fingerprint}
  POST   /shturman-setup/api/memory/pending/projects/{id}          завести предложенный проект или нет: {accept}
  POST   /shturman-setup/api/memory/pending/owner-facts/{id}       факт о вас верен или нет: {accept, fingerprint}

Те же функции сервиса, что у внутреннего API (`processing/pages_service.py`, `processing/service.py`),
но напрямую: внутренний API страница не проксирует. Изменение — действие самого владельца и
применяется сразу (`confirm.apply_owner`) тем же кодом, что нажатие кнопки в боте согласований:
заметки (человека, проекта, профиля) — `pages.owner_block` (коммит «Правка владельца»), согласие
завести страницу — `pages.accept`, принятие договорённости — `commitments.decide` с отпечатком того,
что владелец видел; проект — `projects.create|chats|archive|accept` (`processing/memory_service.py`),
«Неверно» — `facts.retract`. Факт о владельце решает `facts.decide_owner_fact` в контексте владельца,
тоже с отпечатком. Отказы (не заводить страницу или проект, договорённость или факт неверны)
применяются сразу, как и через API.

Что уходит в браузер. Блоки страницы — текстом, без разметки: ссылка на сообщение-источник
становится счётчиком «по N сообщениям», знаки экранирования снимаются; HTML страница из этих
строк не собирает (всё через textContent). Новые договорённости — то же, что бот присылает в
сводке: кто кому, что, срок и короткая цитата.

Журнал действий: виды `memory.*`. В записи — номера (человека, проекта, факта, договорённости) и
числа; ни имён, ни названий, ни текста заметок и переписки.

Как добавить раздел: маршруты — сюда же, в `routes()`, под `/memory/…`; новые виды действий — в
`audit.ACTIONS`; на странице — вкладка в `MEMORY_TABS` (`static/setup.js`). Сводка «ждут решения»
собирается в `pending_items()` списками по видам: новый вид — ещё один ключ ответа и ещё один
раздел вкладки.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from .. import confirm
from ..api_core import BadRequest
from ..app import state_of
from ..processing import commitments, facts, memory_service, pages, pages_build, pages_service, people, projects
from ..processing import service as processing_service
from ..sanitize import clean_line
from . import service as page

MEMORY = page.API + "/memory"
OWNER_LIMIT = 20_000                  # знаков в заметках владельца — как у внутреннего API
OWNER_BODY = 160 * 1024               # байт тела запроса: 20 000 знаков кириллицы с запасом на экранирование
LIST_LIMIT = 500
SEARCH_LIMIT = 50
QUERY_CHARS = 200

TOO_LONG = "Текст блока владельца: строка не длиннее 20 000 знаков."
HAS_MARKER = ("В тексте не должно быть меток блоков страницы "
              "(<!-- summary …, owner, commitments, decisions, facts, timeline).")
CHATS_LIMIT = 500                     # чатов в выборе для проекта

BLOCK_NAMES = {"head": "имени", "summary": "сводке", "owner": "ваших заметках",
               "commitments": "договорённостях", "timeline": "хронологии"}


# --- блоки страницы — текстом -------------------------------------------------------------------------

_LINK = re.compile(r"(?<!\\)\[сообщение\]\(msg:\d{1,18}\)")
_UNESCAPE = re.compile(r"\\([\\\[\]<>|`])")
_ORIGIN = re.compile(r"\s*\((" + "|".join(pages.ORIGIN_TEXT.values()) + r")\)\s*$")
_DAY = re.compile(r"(\d{4}-\d{2}-\d{2}) — (.*)", re.S)
_PIPE = re.compile(r"(?<!\\)\|")
_SPACES = re.compile(r"[ \t]+")
_ITALIC = re.compile(r"_(.+)_")


def plain(line: str) -> dict[str, Any]:
    """Строка блока без разметки: {text, sources, origin}. Ссылки на сообщения не становятся
    ссылками — от них остаётся только счётчик источников."""
    sources = len(_LINK.findall(line))
    line = _LINK.sub(" ", line)
    origin = None
    found = _ORIGIN.search(line)
    if found:
        origin, line = found.group(1), line[: found.start()]
    line = _UNESCAPE.sub(r"\1", line)
    return {"text": _SPACES.sub(" ", line).strip(), "sources": sources, "origin": origin}


def _item_lines(block: str) -> list[str]:
    return [line.rstrip() for line in (block or "").split("\n") if line.strip()]


def summary_items(block: str) -> list[dict[str, Any]]:
    out = []
    for line in _item_lines(block):
        note = _ITALIC.fullmatch(line.strip())
        if note:                     # «Сводки пока нет», «Сводка не обновлена…»
            out.append({"text": note.group(1), "sources": 0, "origin": None, "disputed": False, "note": True})
            continue
        item = plain(line[2:] if line.startswith("- ") else line)
        disputed = item["text"].startswith(pages.DISPUTED_MARK.strip())
        if disputed:
            item["text"] = item["text"][len(pages.DISPUTED_MARK.strip()):].strip()
        out.append({**item, "disputed": disputed, "note": False})
    return out


def commitment_rows(block: str) -> list[dict[str, str]]:
    rows = []
    for line in _item_lines(block):
        if not line.startswith("|"):
            continue
        cells = [_UNESCAPE.sub(r"\1", cell).strip() for cell in _PIPE.split(line)[1:-1]]
        if len(cells) < 3 or cells[:3] == ["Что", "Срок", "Статус"] or all(set(c) <= set("-: ") for c in cells):
            continue
        rows.append({"what": cells[0], "due": cells[1], "status": cells[2]})
    return rows


def timeline_items(block: str) -> list[dict[str, Any]]:
    out = []
    for line in _item_lines(pages.without_keys(block)):
        body = line[2:] if line.startswith("- ") else line
        found = _DAY.fullmatch(body)
        day, body = (found.group(1), found.group(2)) if found else ("", body)
        out.append({"day": day, **plain(body)})
    return out


def snippet(text: str | None) -> str:
    return plain(clean_line(text, 400))["text"] if text else ""


# --- общее ---------------------------------------------------------------------------------------------

def _today(request: Request) -> date:
    return datetime.now(ZoneInfo(state_of(request).config.timezone)).date()


def _row(item: dict[str, Any]) -> dict[str, Any]:
    return {"person_id": item["person_id"], "title": item["title"], "updated": item["updated"],
            "flags": item["flags"]}


def _accept(data: dict[str, Any]) -> bool:
    value = data.get("accept")
    if not isinstance(value, bool):
        raise BadRequest("поле accept: нужно true или false")
    return value


# --- блоки страницы и факты --------------------------------------------------------------------------

async def _blocks(state: Any, found: dict[str, Any] | None) -> dict[str, Any]:
    """Страница текстом: из файла, а если его ещё нет или он не читается — из копии для поиска."""
    if found is None:
        return {"updated": None, "flags": [], "problem": None, "editable": True, "written": False,
                "summary": [], "owner": "", "commitments": [], "timeline": []}
    blocks, problem = found["blocks"], found["problem"]
    owner, readable = blocks.get("owner", ""), False
    try:
        text = await asyncio.to_thread(pages.read_page, state.config.pages_dir, found["path"])
        if text is not None:
            parsed = pages.parse(text)
            blocks = {"summary": parsed.summary, "commitments": parsed.commitments, "timeline": parsed.timeline}
            owner, readable = parsed.owner.strip("\r\n"), True
    except pages.PageError as exc:
        problem = problem or str(exc)
    return {
        "updated": found["updated"], "flags": found["flags"], "problem": problem,
        # заметки правятся, пока страница не заморожена; ещё не записанный файл сервис запишет сам
        "editable": not problem, "written": readable,
        "summary": summary_items(blocks.get("summary", "")),
        "owner": owner,
        "commitments": commitment_rows(blocks.get("commitments", "")),
        "timeline": timeline_items(blocks.get("timeline", "")),
    }


def _fact(item: dict[str, Any]) -> dict[str, Any]:
    """Действующий факт или решение для экрана: номер (для «Неверно»), слот, текст, с какого дня."""
    return {"id": item["id"], "kind": item["kind"], "slot": clean_line(item["slot"], 40) or None,
            "text": clean_line(item["text"], 240), "since": item["valid_from"],
            "origin": pages.ORIGIN_TEXT.get(item["origin"])}


async def _facts(conn: Any, subject_type: str, subject_id: int | None = None) -> dict[str, list[dict[str, Any]]]:
    items = [_fact(f) for f in await facts.list_facts(conn, subject_type, subject_id)]
    return {"facts": [f for f in items if f["kind"] == "fact"],
            "decisions": [f for f in items if f["kind"] == "decision"]}


async def _save_owner_block(request: Request, target: int | str, where: str) -> JSONResponse:
    """Заметки владельца на странице человека, проекта или профиля — тем же кодом, что нажатие в боте."""
    data = await page._body(request, limit=OWNER_BODY)
    text = data.get("text")
    if not isinstance(text, str):
        raise BadRequest("поле text: нужна строка")
    if len(text) > OWNER_LIMIT:
        raise BadRequest(TOO_LONG)
    if pages.has_marker(text):
        raise BadRequest(HAS_MARKER)
    payload = {"person_id": target} if isinstance(target, int) else {"entity_id": target}
    async with state_of(request).pool.acquire() as conn:
        out = (await confirm.apply_owner(conn, pages_service.OWNER_BLOCK, {**payload, "text": text}))["result"]
    what = "очищены" if not text.strip() else f"знаков: {len(text)}"
    action = "memory.profile_block" if target == pages.OWNER_ENTITY else "memory.owner_block"
    await page._log(request, action, detail=f"{where}; {what}" + ("" if out["changed"] else "; без изменений"))
    return JSONResponse({"ok": True, "changed": out["changed"], "saved_to_history": bool(out.get("commit"))})


# --- люди ----------------------------------------------------------------------------------------------

@page.endpoint
async def list_pages(request: Request) -> JSONResponse:
    """Страницы о людях по имени; с запросом — сначала совпадения в имени, затем в тексте страниц."""
    query = (request.query_params.get("q") or "").strip()[:QUERY_CHARS]
    async with state_of(request).ro_pool.acquire() as conn:
        # только страницы о людях: проекты и профиль — своими разделами
        listed = [p for p in await pages_build.list_pages(conn, limit=LIST_LIMIT, entity_type="person")
                  if p["person_id"] is not None]
        if not query:
            return JSONResponse({"pages": [_row(p) for p in listed], "query": ""})
        needle = query.casefold()
        found = [{**_row(p), "match": "head", "match_text": BLOCK_NAMES["head"], "snippet": ""}
                 for p in listed if needle in (p["title"] or "").casefold()]
        seen = {p["person_id"] for p in found}
        by_person = {p["person_id"]: p for p in listed}
        for hit in await pages_build.search_pages(conn, query, SEARCH_LIMIT, entity_type="person"):
            item = by_person.get(hit["person_id"])
            if item is None or hit["person_id"] in seen:
                continue
            seen.add(hit["person_id"])
            found.append({**_row(item), "match": hit["block"], "match_text": BLOCK_NAMES.get(hit["block"], ""),
                          "snippet": "" if hit["block"] == "head" else snippet(hit["snippet"])})
    return JSONResponse({"pages": found, "query": query})


@page.endpoint
async def get_page(request: Request) -> JSONResponse:
    state = state_of(request)
    async with state.ro_pool.acquire() as conn:
        found = await pages_build.get_page(conn, request.path_params["person_id"])
        if found is None or found["person_id"] is None:
            raise BadRequest("У этого человека нет страницы.", 404, "not_found")
        known = await _facts(conn, "person", found["person_id"])
    return JSONResponse({
        **_row(found), "aliases": [a for a in found["aliases"] if a != found["title"]],
        **await _blocks(state, found), "facts": known["facts"] + known["decisions"],
    })


@page.endpoint
async def put_owner_block(request: Request) -> JSONResponse:
    person_id = request.path_params["person_id"]
    return await _save_owner_block(request, person_id, f"запись о человеке № {person_id}")


# --- проекты -------------------------------------------------------------------------------------------

def _project_row(item: dict[str, Any]) -> dict[str, Any]:
    return {"id": item["id"], "title": item["title"], "status": item["status"],
            "chats": [clean_line(c["title"], 80) or "без названия" for c in item["chats"]],
            "commitments_open": item["commitments_open"], "facts": item["facts"], "decisions": item["decisions"],
            "updated": (item["page"] or {}).get("updated")}


def _projects_error(exc: projects.ProjectsError) -> BadRequest:
    return BadRequest(str(exc), {"not_found": 404, "bad_chat": 404}.get(exc.code, 409 if exc.code in (
        "bad_status", "exists") else 400), exc.code)


@page.endpoint
async def list_projects(request: Request) -> JSONResponse:
    """Заведённые проекты: действующие, затем в архиве. Предложенные — в «Ждут решения»."""
    async with state_of(request).ro_pool.acquire() as conn:
        items = [p for p in await projects.list_projects(conn) if p["status"] in ("active", "archived")]
    items.sort(key=lambda p: p["status"] != "active")
    return JSONResponse({"projects": [_project_row(p) for p in items]})


async def _active_project(conn: Any, project_id: int) -> dict[str, Any]:
    item = await projects.get_project(conn, project_id)
    if item is None or item["status"] not in ("active", "archived"):
        raise BadRequest("Такого проекта нет.", 404, "not_found")
    return item


@page.endpoint
async def get_project(request: Request) -> JSONResponse:
    state = state_of(request)
    project_id = request.path_params["project_id"]
    async with state.ro_pool.acquire() as conn:
        item = await _active_project(conn, project_id)
        found = await pages_build.get_page(conn, entity_id=f"project:{project_id}")
        known = await _facts(conn, "project", project_id)
    view = await _blocks(state, found)
    return JSONResponse({
        **_project_row(item), **view, **known,
        "aliases": [clean_line(a, 80) for a in item["aliases"]],
        "chats": [{"id": c["id"], "title": clean_line(c["title"], 80) or "без названия", "type": c["type"]}
                  for c in item["chats"]],
        "participants": [clean_line(n, 80) for n in item.get("participants") or []][:20],
        "description": clean_line(item.get("description"), 2000) or None,
    })


def _ids(value: Any, key: str) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > projects.MAX_CHATS * 2 or not all(
            isinstance(i, int) and not isinstance(i, bool) and i > 0 for i in value):
        raise BadRequest(f"поле {key}: нужен список номеров чатов")
    return list(dict.fromkeys(value))


def _names(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > projects.MAX_ALIASES * 2 or not all(
            isinstance(i, str) for i in value):
        raise BadRequest("поле aliases: нужен список названий")
    return [v for v in value if v.strip()]


@page.endpoint
async def create_project(request: Request) -> JSONResponse:
    data = await page._body(request)
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        raise BadRequest("Впишите название проекта.")
    chat_ids, aliases = _ids(data.get("chat_ids"), "chat_ids"), _names(data.get("aliases"))
    async with state_of(request).pool.acquire() as conn:
        try:
            projects.clean_title(title)
        except projects.ProjectsError as exc:
            raise _projects_error(exc) from None
        out = (await confirm.apply_owner(conn, memory_service.PROJECT_CREATE,
                                         {"title": title, "chat_ids": chat_ids, "aliases": aliases}))["result"]
    project = out["project"]
    await page._log(request, "memory.project_create",
                    detail=f"проект № {project['id']}; чатов: {len(project['chats'])}; "
                           f"других названий: {len(project['aliases'])}")
    return JSONResponse({"ok": True, "created": out["created"], "project": _project_row(project)})


@page.endpoint
async def project_chats(request: Request) -> JSONResponse:
    data = await page._body(request)
    project_id = request.path_params["project_id"]
    if "chat_ids" not in data:
        raise BadRequest("поле chat_ids: нужен список номеров чатов")
    chat_ids = _ids(data["chat_ids"], "chat_ids")
    async with state_of(request).pool.acquire() as conn:
        await _active_project(conn, project_id)
        out = (await confirm.apply_owner(conn, memory_service.PROJECT_CHATS,
                                         {"project_id": project_id, "chat_ids": chat_ids}))["result"]
    if out["changed"]:
        await page._log(request, "memory.project_chats",
                        detail=f"проект № {project_id}; добавлено: {len(out['added'])}; убрано: {len(out['removed'])}")
    return JSONResponse({"ok": True, "changed": out["changed"], "project": _project_row(out["project"])})


@page.endpoint
async def archive_project(request: Request) -> JSONResponse:
    await page._body(request)
    project_id = request.path_params["project_id"]
    async with state_of(request).pool.acquire() as conn:
        item = await _active_project(conn, project_id)
        if item["status"] == "archived":
            raise BadRequest("Проект уже в архиве.", 409, "decided")
        await confirm.apply_owner(conn, memory_service.PROJECT_ARCHIVE, {"project_id": project_id})
    await page._log(request, "memory.project_archive", detail=f"проект № {project_id}")
    return JSONResponse({"ok": True})


@page.endpoint
async def project_owner_block(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    async with state_of(request).ro_pool.acquire() as conn:
        await _active_project(conn, project_id)
    return await _save_owner_block(request, f"project:{project_id}", f"проект № {project_id}")


@page.endpoint
async def list_chats(request: Request) -> JSONResponse:
    """Чаты, которые сервис читает (не исключённые, с сообщениями в архиве) — для выбора чатов проекта.
    Названия — страница самого владельца."""
    async with state_of(request).ro_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT c.id, c.title, c.type, max(m.sent_at) AS last FROM chats c
               JOIN messages m ON m.chat_id = c.id AND m.deleted_at IS NULL
               WHERE NOT c.excluded GROUP BY c.id ORDER BY last DESC NULLS LAST, c.id LIMIT $1""", CHATS_LIMIT)
    return JSONResponse({"chats": [{"id": r["id"], "title": clean_line(r["title"], 80) or "без названия",
                                    "kind": _chat_kind(r["type"])} for r in rows]})


def _chat_kind(chat_type: str | None) -> str:
    if chat_type in page.KINDS["group"]:
        return "group"
    if chat_type in page.KINDS["channel"]:
        return "channel"
    return "personal"


# --- профиль владельца и факты ------------------------------------------------------------------------------

@page.endpoint
async def get_profile(request: Request) -> JSONResponse:
    state = state_of(request)
    async with state.ro_pool.acquire() as conn:
        found = await pages_build.get_page(conn, entity_id=pages.OWNER_ENTITY)
        known = await _facts(conn, "owner")
    view = await _blocks(state, found)
    return JSONResponse({"facts": known["facts"] + known["decisions"], "owner": view["owner"],
                         "editable": view["editable"], "written": view["written"], "updated": view["updated"],
                         "flags": view["flags"], "problem": view["problem"]})


@page.endpoint
async def profile_owner_block(request: Request) -> JSONResponse:
    return await _save_owner_block(request, pages.OWNER_ENTITY, "профиль")


@page.endpoint
async def retract_fact(request: Request) -> JSONResponse:
    await page._body(request)
    fact_id = request.path_params["fact_id"]
    async with state_of(request).pool.acquire() as conn:
        item = await facts.get_fact(conn, fact_id)
        if item is None:
            raise BadRequest("Такого факта нет.", 404, "not_found")
        if item["status"] != "active":
            raise BadRequest("Уже решено.", 409, "decided")
        await confirm.apply_owner(conn, memory_service.FACT_RETRACT, {"fact_id": fact_id})
    await page._log(request, "memory.fact_retract", detail=f"факт № {fact_id}")
    return JSONResponse({"ok": True})


# --- ждут решения ----------------------------------------------------------------------------------------

def _short(text: str | None, limit: int) -> str:
    return clean_line((text or "").replace("⏎", " "), limit)


async def pending_items(conn: Any, today: date) -> dict[str, Any]:
    """Что ждёт решения владельца — по видам. Новый вид — ещё один ключ."""
    days = pages_build.Options().propose_days
    waiting = await projects.pending_approvals(conn)
    proposed_projects = []
    for p in waiting["projects"]:
        chats = await conn.fetch(
            """SELECT c.title FROM project_chats pc JOIN chats c ON c.id = pc.chat_id AND NOT c.excluded
               WHERE pc.project_id = $1 ORDER BY pc.added_at, c.id LIMIT 5""", p["id"])
        proposed_projects.append({"id": p["id"], "title": clean_line(p["title"], 80) or "без названия",
                                  "reason": p["text"], "chats": [clean_line(c["title"], 80) for c in chats]})
    owner_facts = [{"id": f["id"], "slot": clean_line(f["title"], 40) if f["title"] != "о вас" else None,
                    "text": _short(f["text"], 240), "since": f["valid_from"], "quote": _short(f["quote"], 160),
                    "fingerprint": f["fingerprint"]} for f in waiting["owner_facts"]]
    proposals = [{"person_id": p["person_id"], "name": clean_line(p["display_name"], 80) or "без имени",
                  "reason": pages_build._reason_text(p["reason"], days)}
                 for p in await pages_build.list_proposals(conn, status="pending")]
    found = [{"id": c["id"], "fingerprint": c["approval_fingerprint"],
              "who": _short(commitments.who_line(c), 160), "what": _short(c["what"], 200),
              "due": commitments.due_line(c, today), "quote": _short(c["source_quote"], 160)}
             for c in await commitments.list_commitments(conn, view="proposed", today=today, limit=100)]
    return {"projects": proposed_projects, "owner_facts": owner_facts, "pages": proposals, "commitments": found,
            "total": len(proposed_projects) + len(owner_facts) + len(proposals) + len(found)}


@page.endpoint
async def pending(request: Request) -> JSONResponse:
    async with state_of(request).ro_pool.acquire() as conn:
        return JSONResponse(await pending_items(conn, _today(request)))


def _fingerprint(data: dict[str, Any]) -> str:
    shown = data.get("fingerprint")
    if not isinstance(shown, str) or not re.fullmatch(r"[0-9a-f]{64}", shown):
        raise BadRequest("Обновите список: не видно, что именно вы принимаете.")
    return shown


@page.endpoint
async def decide_page(request: Request) -> JSONResponse:
    data = await page._body(request)
    accept = _accept(data)
    person_id = request.path_params["person_id"]
    async with state_of(request).pool.acquire() as conn:
        status = await conn.fetchval("SELECT status FROM page_proposals WHERE person_id = $1", person_id)
        if status is None:
            raise BadRequest("Такого предложения нет.", 404, "not_found")
        if status != "pending":
            raise BadRequest("Уже решено.", 409, "decided")
        if accept:
            await confirm.apply_owner(conn, pages_service.PAGE_ACCEPT, {"person_id": person_id})
        else:
            try:
                await pages_build.decide_proposal(conn, person_id, False)
            except (pages_build.PagesError, people.PeopleError) as exc:
                raise BadRequest(str(exc), 409) from None
    await page._log(request, "memory.page_accept" if accept else "memory.page_reject",
                    detail=f"запись о человеке № {person_id}")
    return JSONResponse({"ok": True, "accepted": accept})


@page.endpoint
async def decide_commitment(request: Request) -> JSONResponse:
    data = await page._body(request)
    accept = _accept(data)
    commitment_id = request.path_params["commitment_id"]
    async with state_of(request).pool.acquire() as conn:
        status = await conn.fetchval("SELECT status FROM commitments WHERE id = $1", commitment_id)
        if status is None or not await commitments.is_visible(conn, commitment_id):
            raise BadRequest("Такой договорённости нет.", 404, "not_found")
        if status != "proposed":
            raise BadRequest("Уже решено.", 409, "decided")
        if accept:
            await confirm.apply_owner(conn, processing_service.COMMITMENT_DECIDE,
                                      {"commitment_id": commitment_id, "action": "accept",
                                       "fingerprint": _fingerprint(data)})
        else:
            result = await commitments.reject(conn, commitment_id, actor="owner", today=_today(request))
            if not result.get("ok"):
                code = result.get("code")
                raise BadRequest(result.get("error") or "Не получилось.", 404 if code == "not_found" else 409, code)
    await page._log(request, "memory.commitment_accept" if accept else "memory.commitment_reject",
                    detail=f"№ {commitment_id}")
    return JSONResponse({"ok": True, "accepted": accept})


@page.endpoint
async def decide_project(request: Request) -> JSONResponse:
    data = await page._body(request)
    accept = _accept(data)
    project_id = request.path_params["project_id"]
    async with state_of(request).pool.acquire() as conn:
        status = await conn.fetchval("SELECT status FROM projects WHERE id = $1", project_id)
        if status is None:
            raise BadRequest("Такого предложения нет.", 404, "not_found")
        if status != "proposed":
            raise BadRequest("Уже решено.", 409, "decided")
        if accept:
            await confirm.apply_owner(conn, memory_service.PROJECT_ACCEPT, {"project_id": project_id})
        else:
            try:
                await projects.decide_project_proposal(conn, project_id, False)
            except projects.ProjectsError as exc:
                raise _projects_error(exc) from None
    await page._log(request, "memory.project_accept" if accept else "memory.project_reject",
                    detail=f"проект № {project_id}")
    return JSONResponse({"ok": True, "accepted": accept})


@page.endpoint
async def decide_owner_fact(request: Request) -> JSONResponse:
    data = await page._body(request)
    accept = _accept(data)
    fact_id = request.path_params["fact_id"]
    async with state_of(request).pool.acquire() as conn:
        item = await facts.get_fact(conn, fact_id)
        if item is None or item["subject_type"] != "owner":
            raise BadRequest("Такого предложения нет.", 404, "not_found")
        if item["status"] != "proposed":
            raise BadRequest("Уже решено.", 409, "decided")
        try:
            await facts.decide_owner_fact(conn, fact_id, accept,
                                          expected_fingerprint=_fingerprint(data) if accept else None)
        except facts.FactsError as exc:
            raise BadRequest(str(exc), {"not_found": 404}.get(exc.code, 409), exc.code) from None
    await page._log(request, "memory.owner_fact_accept" if accept else "memory.owner_fact_reject",
                    detail=f"факт № {fact_id}")
    return JSONResponse({"ok": True, "accepted": accept})


def routes() -> list[BaseRoute]:
    person = MEMORY + "/pages/{person_id:int}"
    project = MEMORY + "/projects/{project_id:int}"
    return [
        Route(MEMORY + "/pages", list_pages, methods=["GET"]),
        Route(person, get_page, methods=["GET"]),
        Route(person + "/owner-block", put_owner_block, methods=["PUT"]),
        Route(MEMORY + "/projects", list_projects, methods=["GET"]),
        Route(MEMORY + "/projects", create_project, methods=["POST"]),
        Route(project, get_project, methods=["GET"]),
        Route(project + "/chats", project_chats, methods=["PUT"]),
        Route(project + "/archive", archive_project, methods=["POST"]),
        Route(project + "/owner-block", project_owner_block, methods=["PUT"]),
        Route(MEMORY + "/chats", list_chats, methods=["GET"]),
        Route(MEMORY + "/profile", get_profile, methods=["GET"]),
        Route(MEMORY + "/profile/owner-block", profile_owner_block, methods=["PUT"]),
        Route(MEMORY + "/facts/{fact_id:int}/retract", retract_fact, methods=["POST"]),
        Route(MEMORY + "/pending", pending, methods=["GET"]),
        Route(MEMORY + "/pending/pages/{person_id:int}", decide_page, methods=["POST"]),
        Route(MEMORY + "/pending/commitments/{commitment_id:int}", decide_commitment, methods=["POST"]),
        Route(MEMORY + "/pending/projects/{project_id:int}", decide_project, methods=["POST"]),
        Route(MEMORY + "/pending/owner-facts/{fact_id:int}", decide_owner_fact, methods=["POST"]),
    ]
