"""Экран «Память ассистента» на странице настройки: страницы о людях и решения, которые ждут владельца.

  GET    /shturman-setup/api/memory/pages                          список страниц о людях; ?q= — поиск
  GET    /shturman-setup/api/memory/pages/{person_id}              страница: блоки текстом и пометки
  PUT    /shturman-setup/api/memory/pages/{person_id}/owner-block  ваши заметки: {text}
  GET    /shturman-setup/api/memory/pending                        что ждёт решения владельца
  POST   /shturman-setup/api/memory/pending/pages/{person_id}      завести страницу или нет: {accept}
  POST   /shturman-setup/api/memory/pending/commitments/{id}       договорённость верна или нет: {accept, fingerprint}

Те же функции сервиса, что у внутреннего API (`processing/pages_service.py`, `processing/service.py`),
но напрямую: внутренний API страница не проксирует. Изменение — действие самого владельца и
применяется сразу (`confirm.apply_owner`) тем же кодом, что нажатие кнопки в боте согласований:
заметки — `pages.owner_block` (коммит «Правка владельца»), согласие завести страницу — `pages.accept`,
принятие договорённости — `commitments.decide` с отпечатком того, что владелец видел. Отказы
(не заводить страницу, договорённость неверна) применяются сразу, как и через API.

Что уходит в браузер. Блоки страницы — текстом, без разметки: ссылка на сообщение-источник
становится счётчиком «по N сообщениям», знаки экранирования снимаются; HTML страница из этих
строк не собирает (всё через textContent). Новые договорённости — то же, что бот присылает в
сводке: кто кому, что, срок и короткая цитата.

Журнал действий: виды `memory.*`. В записи — номер записи о человеке или договорённости и число
знаков; ни имён, ни текста заметок и переписки.

Как добавить раздел (проекты, профиль владельца): маршруты — сюда же, в `routes()`, под
`/memory/…`; новые виды действий — в `audit.ACTIONS`; на странице — вкладка в `MEMORY_TABS`
(`static/setup.js`). Сводка «ждут решения» собирается в `pending()` списками по видам: новый вид —
ещё один ключ ответа и ещё один раздел вкладки.
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
from ..processing import commitments, pages, pages_build, pages_service, people
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
HAS_MARKER = "В тексте не должно быть меток блоков страницы (<!-- summary …, owner, commitments, timeline)."

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


# --- страницы ------------------------------------------------------------------------------------------

@page.endpoint
async def list_pages(request: Request) -> JSONResponse:
    """Страницы о людях по имени; с запросом — сначала совпадения в имени, затем в тексте страниц."""
    query = (request.query_params.get("q") or "").strip()[:QUERY_CHARS]
    async with state_of(request).ro_pool.acquire() as conn:
        # только страницы о людях: страницы других видов (проекты, профиль) — своими разделами
        listed = [p for p in await pages_build.list_pages(conn, limit=LIST_LIMIT)
                  if p["person_id"] is not None and str(p["entity_id"]).startswith("person:")]
        if not query:
            return JSONResponse({"pages": [_row(p) for p in listed], "query": ""})
        needle = query.casefold()
        found = [{**_row(p), "match": "head", "match_text": BLOCK_NAMES["head"], "snippet": ""}
                 for p in listed if needle in (p["title"] or "").casefold()]
        seen = {p["person_id"] for p in found}
        by_person = {p["person_id"]: p for p in listed}
        for hit in await pages_build.search_pages(conn, query, SEARCH_LIMIT):
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
    if found is None:
        raise BadRequest("У этого человека нет страницы.", 404, "not_found")
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
    return JSONResponse({
        **_row(found), "aliases": [a for a in found["aliases"] if a != found["title"]], "problem": problem,
        # заметки правятся, пока страница не заморожена; ещё не записанный файл сервис запишет сам
        "editable": not problem, "written": readable,
        "summary": summary_items(blocks.get("summary", "")),
        "owner": owner,
        "commitments": commitment_rows(blocks.get("commitments", "")),
        "timeline": timeline_items(blocks.get("timeline", "")),
    })


@page.endpoint
async def put_owner_block(request: Request) -> JSONResponse:
    data = await page._body(request, limit=OWNER_BODY)
    text = data.get("text")
    if not isinstance(text, str):
        raise BadRequest("поле text: нужна строка")
    if len(text) > OWNER_LIMIT:
        raise BadRequest(TOO_LONG)
    if pages.has_marker(text):
        raise BadRequest(HAS_MARKER)
    person_id = request.path_params["person_id"]
    async with state_of(request).pool.acquire() as conn:
        out = (await confirm.apply_owner(conn, pages_service.OWNER_BLOCK,
                                         {"person_id": person_id, "text": text}))["result"]
    what = "очищены" if not text.strip() else f"знаков: {len(text)}"
    await page._log(request, "memory.owner_block",
                    detail=f"запись о человеке № {person_id}; {what}" + ("" if out["changed"] else "; без изменений"))
    return JSONResponse({"ok": True, "changed": out["changed"], "saved_to_history": bool(out.get("commit"))})


# --- ждут решения ----------------------------------------------------------------------------------------

def _short(text: str | None, limit: int) -> str:
    return clean_line((text or "").replace("⏎", " "), limit)


async def pending_items(conn: Any, today: date) -> dict[str, Any]:
    """Что ждёт решения владельца — по видам. Новый вид — ещё один ключ."""
    days = pages_build.Options().propose_days
    proposals = [{"person_id": p["person_id"], "name": clean_line(p["display_name"], 80) or "без имени",
                  "reason": pages_build._reason_text(p["reason"], days)}
                 for p in await pages_build.list_proposals(conn, status="pending")]
    found = [{"id": c["id"], "fingerprint": c["approval_fingerprint"],
              "who": _short(commitments.who_line(c), 160), "what": _short(c["what"], 200),
              "due": commitments.due_line(c, today), "quote": _short(c["source_quote"], 160)}
             for c in await commitments.list_commitments(conn, view="proposed", today=today, limit=100)]
    return {"pages": proposals, "commitments": found, "total": len(proposals) + len(found)}


@page.endpoint
async def pending(request: Request) -> JSONResponse:
    async with state_of(request).ro_pool.acquire() as conn:
        return JSONResponse(await pending_items(conn, _today(request)))


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
            shown = data.get("fingerprint")
            if not isinstance(shown, str) or not re.fullmatch(r"[0-9a-f]{64}", shown):
                raise BadRequest("Обновите список: не видно, что именно вы принимаете.")
            await confirm.apply_owner(conn, processing_service.COMMITMENT_DECIDE,
                                      {"commitment_id": commitment_id, "action": "accept", "fingerprint": shown})
        else:
            result = await commitments.reject(conn, commitment_id, actor="owner", today=_today(request))
            if not result.get("ok"):
                code = result.get("code")
                raise BadRequest(result.get("error") or "Не получилось.", 404 if code == "not_found" else 409, code)
    await page._log(request, "memory.commitment_accept" if accept else "memory.commitment_reject",
                    detail=f"№ {commitment_id}")
    return JSONResponse({"ok": True, "accepted": accept})


def routes() -> list[BaseRoute]:
    person = MEMORY + "/pages/{person_id:int}"
    return [
        Route(MEMORY + "/pages", list_pages, methods=["GET"]),
        Route(person, get_page, methods=["GET"]),
        Route(person + "/owner-block", put_owner_block, methods=["PUT"]),
        Route(MEMORY + "/pending", pending, methods=["GET"]),
        Route(MEMORY + "/pending/pages/{person_id:int}", decide_page, methods=["POST"]),
        Route(MEMORY + "/pending/commitments/{commitment_id:int}", decide_commitment, methods=["POST"]),
    ]
