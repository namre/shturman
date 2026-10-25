"""Приложение «Штурман»: экран «Память» (шаг 6 записки «держать, подкладывать, сигналить»).

  GET  app/memory                       ?q= — люди (подтверждённые), действующие проекты и «Вы» (профиль)
  GET  app/memory/{kind}/{id}           kind: person | project | owner (id — 0); что действует сейчас, что ждёт
                                        решения, лента изменений; ?as_of=ГГГГ-ММ-ДД — что действовало в тот день,
                                        ?known_at=ГГГГ-ММ-ДД — что ассистент знал к концу того дня
  POST app/memory/facts/{id}/correct    «Поправить»: {text, v} — исправление ошибки извлечения (`facts.correct_fact`)
  POST app/memory/facts/{id}/wrong      «Не так»: {v} — значение неверно (`facts.retract_fact`)

`v` — начало отпечатка версии, которую владелец видел: изменилась — действие не выполняется.

Что показывается. Формулировка, ключ, с какого дня, основание («сказали вы», «сам человек», «со слов …»,
«приняли вы»), источник (чат и дата сообщения) и цитата — чужой текст, только текстом. Предложенное и
противоречия решаются во вкладке «Согласования» теми же кнопками, что в боте: здесь — только список.

Кто решает. Исправление и «Не так» — решения владельца о памяти, которую ассистент читает как верную, —
применяются сразу, в контексте владельца из Telegram (`authority.miniapp_context`). Цена та же, что у
экрана «Чаты» (Р-80): войти в приложение может любой, у кого есть токен бота согласований, — значит, и
вписать в память свой текст. Поэтому каждое исправление и «Не так» отсюда сервис сообщает владельцу
отдельным сообщением в бот (`miniapp._tell`) и пишет в журнал действий (`app.fact_correct`, `app.fact_wrong`:
номер факта, без текста). Внутренний API и дашборд Hermes этих маршрутов не получают.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from .. import authority
from ..api_core import BadRequest
from ..app import state_of
from ..processing import facts, memory_service, pages_build, projects
from ..sanitize import clean_line
from . import audit, miniapp
from . import service as page

APP_MEMORY = page.API + "/app/memory"
LIST_LIMIT = 60
QUERY_CHARS = 80
KINDS = ("person", "project", "owner")
BASIS = {"owner": "сказали вы", "self": "сам человек", "member": "участник чата проекта",
         "approved": "приняли вы", "said": "со слов другого"}
CHANGED = "Значение изменилось — экран обновлён. Посмотрите и решите ещё раз."


def _row(item: dict[str, Any]) -> dict[str, Any]:
    """Версия для экрана: только нужное, чужой текст — строкой без разметки."""
    basis = BASIS.get(item.get("basis") or "")
    if item.get("said_by") and item.get("basis") in ("said", None):
        basis = f"со слов: {clean_line(item['said_by'], 60)}"
    return {
        "id": item["id"], "v": item["approval_fingerprint"][:16], "kind": item["kind"],
        "slot": clean_line(item["slot"], 40) or None, "text": clean_line(item["text"], 240),
        "since": item["valid_from"], "until": item["valid_to"], "basis": basis,
        "correction": item.get("change_kind") == "correction",
        "quote": clean_line(item["source_quote"], 200), "said_at": item["source"]["sent_at"],
        "chat": clean_line((item.get("chat") or {}).get("title"), 80) or None,
        "learned_at": item["created_at"], "applied_at": item.get("applied_at"), "decided_at": item.get("decided_at"),
        "state": item.get("state") or item.get("state_at") or ("current" if item.get("current") else None),
    }


def _day(request: Request, name: str) -> date | None:
    raw = (request.query_params.get(name) or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise BadRequest("Дата — в виде ГГГГ-ММ-ДД.", 400, "bad_date") from None


async def app_memory_list(request: Request) -> JSONResponse:
    query = (request.query_params.get("q") or "").strip()[:QUERY_CHARS].casefold()
    async with state_of(request).ro_pool.acquire() as conn:
        people = [p for p in await pages_build.list_pages(conn, limit=500, entity_type="person")
                  if p["person_id"] is not None]
        active = await projects.list_projects(conn, "active")
        waiting = {(r["subject_type"], r["project_id"]): r["n"] for r in await conn.fetch(
            """SELECT subject_type, project_id, count(*) AS n FROM facts WHERE status = 'proposed'
               AND subject_type IN ('project', 'owner') GROUP BY 1, 2""")}
    out_people = [{"kind": "person", "id": p["person_id"], "title": clean_line(p["title"], 80)}
                  for p in people if not query or query in (p["title"] or "").casefold()][:LIST_LIMIT]
    out_projects = [{"kind": "project", "id": p["id"], "title": clean_line(p["title"], 80),
                     "waiting": waiting.get(("project", p["id"]), 0)}
                    for p in active if not query or query in (p["title"] or "").casefold()
                    or any(query in (a or "").casefold() for a in p.get("aliases") or [])][:LIST_LIMIT]
    return JSONResponse({"people": out_people, "projects": out_projects,
                         "owner": {"kind": "owner", "id": 0, "title": "Вы", "waiting": waiting.get(("owner", None), 0)},
                         "query": query})


async def _title(conn: Any, kind: str, subject_id: int | None) -> str:
    if kind == "owner":
        return "Вы"
    if kind == "project":
        row = await conn.fetchrow("SELECT title, status FROM projects WHERE id = $1", subject_id)
        if row is None or row["status"] != "active":
            raise BadRequest("Такого проекта нет среди действующих.", 404, "not_found")
        return clean_line(row["title"], 80)
    from ..processing import people
    if await people.visible_id(conn, subject_id) is None:
        raise BadRequest("Такого человека нет в памяти.", 404, "not_found")
    found = await pages_build.get_page(conn, entity_id=f"person:{subject_id}")
    return clean_line(found["title"] if found else None, 80) or "Человек"


async def app_memory_subject(request: Request) -> JSONResponse:
    kind, raw_id = request.path_params["kind"], request.path_params["subject_id"]
    if kind not in KINDS:
        raise BadRequest("Такого раздела нет.", 404, "not_found")
    subject_id = None if kind == "owner" else int(raw_id)
    as_of, known_at = _day(request, "as_of"), _day(request, "known_at")
    if as_of and known_at:
        raise BadRequest("Выберите что-то одно: «действовало» или «знал».", 400, "both_dates")
    state = state_of(request)
    async with state.ro_pool.acquire() as conn:
        title = await _title(conn, kind, subject_id)
        try:
            if as_of is not None:
                now = await facts.facts_in_force(conn, kind, subject_id, as_of)
            elif known_at is not None:
                now = await facts.facts_known_at(conn, kind, subject_id,
                                                 memory_service.end_of_day(known_at, state.config.timezone))
            else:
                now = await facts.list_facts(conn, kind, subject_id)
            feed = await facts.history(conn, kind, subject_id)
        except facts.FactsError as exc:
            raise BadRequest(str(exc), 400, exc.code) from None
    dated = as_of is not None or known_at is not None
    current = [_row(f) for f in now if not dated or f.get("state_at") == "active"]
    pending = [_row(f) for f in (now if known_at else feed) if (f.get("state_at") if known_at else f["state"]) == "proposed"]
    return JSONResponse({
        "kind": kind, "id": subject_id or 0, "title": title,
        "as_of": as_of.isoformat() if as_of else None, "known_at": known_at.isoformat() if known_at else None,
        "current": current, "pending": pending,
        "history": [{**_row(f), "state_text": facts.STATE_WORDS.get(f["state"], f["state"])} for f in feed],
        "editable": not dated,
    })


async def _shown(conn: Any, fact_id: int, v: Any) -> dict[str, Any]:
    item = await facts.get_fact(conn, fact_id)
    if item is None or not item["current"]:
        raise BadRequest("Этого значения уже нет среди действующих — экран обновлён.", 409, "gone")
    if not isinstance(v, str) or not item["approval_fingerprint"].startswith(v) or len(v) < 8:
        raise BadRequest(CHANGED, 409, "changed")
    return item


async def _act(request: Request, action: str) -> JSONResponse:
    data = await page._body(request)
    state, session = state_of(request), request.state.app_session
    fact_id = int(request.path_params["fact_id"])
    async with state.pool.acquire() as conn:
        with authority.miniapp_context(session.user_id, chat_id=session.chat_id):
            await _shown(conn, fact_id, data.get("v"))
            try:
                if action == "correct":
                    text = data.get("text")
                    if not isinstance(text, str):
                        raise BadRequest("Напишите, как правильно.", 400, "empty")
                    out = await facts.correct_fact(conn, fact_id, text)
                    said = "Поправил. Прежняя запись осталась в истории как исправленная."
                else:
                    out = await facts.retract_fact(conn, fact_id)
                    said = "Отмечено: неверно. Если оно сменило прежнее значение, прежнее снова действует."
            except facts.FactsError as exc:
                await page._log(request, f"app.fact_{action}", audit.REFUSED, f"факт № {fact_id}; причина: {exc.code}")
                raise BadRequest(str(exc), 409 if exc.code in ("not_current", "bad_status") else 400, exc.code) from None
    new_id = (out.get("fact") or {}).get("id")
    detail = f"факт № {fact_id}" + (f" → № {new_id}" if action == "correct" else "")
    await page._log(request, f"app.fact_{action}", detail=detail)
    word = "исправлено" if action == "correct" else "отмечено неверным"
    await miniapp._tell(state, f"Из приложения «Штурман» в памяти {word} одно значение (№ {fact_id}). Если это были не вы — "
                               "посмотрите «Историю» в «Настройках Штурмана».")
    return JSONResponse({"ok": True, "answer": said})


async def app_fact_correct(request: Request) -> JSONResponse:
    return await _act(request, "correct")


async def app_fact_wrong(request: Request) -> JSONResponse:
    return await _act(request, "wrong")


def routes() -> list[BaseRoute]:
    return [
        Route(APP_MEMORY, miniapp.endpoint(app_memory_list), methods=["GET"]),
        Route(APP_MEMORY + "/{kind}/{subject_id:int}", miniapp.endpoint(app_memory_subject), methods=["GET"]),
        Route(APP_MEMORY + "/facts/{fact_id:int}/correct", miniapp.endpoint(app_fact_correct), methods=["POST"]),
        Route(APP_MEMORY + "/facts/{fact_id:int}/wrong", miniapp.endpoint(app_fact_wrong), methods=["POST"]),
    ]

