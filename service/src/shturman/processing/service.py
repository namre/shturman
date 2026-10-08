"""Модуль сервиса «обработка»: маршруты /api/... и ночной запуск.

Маршруты (токен внутреннего API; обращается плагин «Штурмана» в Hermes):

  POST /api/processing/run                 {since?, limit?, rescan?} -> счётчики прогона
  GET  /api/processing/status              -> отметка, последний прогон
  GET  /api/commitments                    ?view=&person_id=&chat_id=&direction=&limit=
  GET  /api/commitments/{id}
  POST /api/commitments/{id}/{действие}    close | cancel | reopen | reschedule {due} | accept | reject
  GET  /api/people                         ?query=&chat_id=&limit=
  GET  /api/people/proposals
  POST /api/people/proposals/{id}/reject
  POST /api/people/merge                   {proposal_id} или {source_id, target_id}
  GET  /api/people/{id}
  POST /api/people/{id}/aliases            {alias}
  DELETE /api/people/{id}/aliases          {alias}
  POST /api/people/{id}/split              {peer_id}

Изменения обязательств и реестра людей через эти маршруты — действия владельца: плагин вызывает
их по его команде или нажатию.

Видимость. Обязательства исключённых чатов и удалённых сообщений не отдаются ни одним маршрутом
(404 — как будто их нет). Человек отдаётся, только если у него есть видимый след: неисключённый
личный чат или неудалённое сообщение в неисключённом чате; иначе — 404 и отсутствие в поиске.

Чужой текст. Ответы читает агент через инструменты плагина. В каждом словаре обязательства,
человека и предложения слияния есть ключ `untrusted_fields` — список полей (пути через точку,
`[]` — элементы списка), значения которых взяты из сообщений третьих лиц, их имён и названий
чатов: `what`, `source_quote`, `due_expression`, `debtor.name`, `creditor.name`, `chat.title`;
`display_name`, `first_name`, `middle_name`, `last_name`, `aliases[].alias`, `peers[].name`,
`peers[].username`. Плагин обязан подавать эти значения агенту как данные (в рамке «недоверенный
текст»), а не как указания: в них может быть написано что угодно.

view для /api/commitments: open, overdue, today, week (с сегодня до воскресенья), next_week
(с понедельника по воскресенье следующей недели), proposed, closed, all. Кроме person_id
(people.id) принимается peer_id (peers.id — учётная запись Telegram).

Подтверждение владельцем (см. `confirm.py`). Новое обязательство и новая страница становятся
для ассистента фактом только с одобрения владельца. Когда у сервиса свой бот согласований,
это одобрение нельзя дать по HTTP: ждут нажатия в боте
  * принятие предложенного обязательства любым путём — `accept`, а также `reopen` из состояний
    «ждёт решения», «отклонено», «не подтверждено» и `close` из «ждёт решения»;
  * объединение и разделение записей о людях (меняют, кого ассистент считает одним человеком,
    и подтверждают человека в реестре — после этого о нём заводится страница);
  * алиас для человека, которого владелец ещё не подтверждал (алиас подтверждает запись).
Без подтверждения остаются: закрыть, отменить, вернуть в работу и перенести уже принятое
обязательство (обратимо, пишется в журнал обязательства, этим пользуется инструмент агента),
отклонить предложение, добавить алиас подтверждённому человеку, убрать алиас.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from datetime import date, datetime, time
from typing import Any, AsyncIterator
from zoneinfo import ZoneInfo

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from .. import confirm, events
from ..api_core import BadRequest, body, handler, need_int, need_str, settle
from ..app import AppState, state_of
from ..sanitize import clean_line
from . import commitments, people, pipeline

logger = logging.getLogger("shturman.processing")

NIGHTLY_DEFAULT = time(3, 30)
TICK_SECONDS = 60

COMMITMENT_DECIDE = "commitments.decide"
PEOPLE_MERGE = "people.merge"
PEOPLE_SPLIT = "people.split"
PEOPLE_ALIAS = "people.alias"
# Статус не доказывает согласия: в том числе cancelled/open могут быть наследием обхода.
UNDECIDED = ("proposed", "rejected", "expired", "cancelled")

# Состояние работающего сервиса: функции применения действий его не получают.
_state: AppState | None = None


def _today_now() -> date | None:
    return datetime.now(ZoneInfo(_state.config.timezone)).date() if _state is not None else None


def nightly_time(config: Any) -> time:
    """Время ночного прогона по часам владельца: `config.nightly_at` или SHTURMAN_NIGHTLY_AT («ЧЧ:ММ»)."""
    raw = getattr(config, "nightly_at", None) or os.environ.get("SHTURMAN_NIGHTLY_AT", "").strip()
    if not raw:
        return NIGHTLY_DEFAULT
    try:
        return time.fromisoformat(str(raw))
    except ValueError:
        logger.warning("время ночного прогона задано неверно, используется %s", NIGHTLY_DEFAULT.strftime("%H:%M"))
        return NIGHTLY_DEFAULT


def _today(request: Request) -> date:
    return datetime.now(ZoneInfo(state_of(request).config.timezone)).date()


def _query_int(request: Request, key: str) -> int | None:
    raw = request.query_params.get(key)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        raise BadRequest(f"параметр {key}: нужно целое число") from None


def _outcome(result: dict[str, Any]) -> JSONResponse:
    """Итог команды над обязательством: 404 — нет такого, 409 — статус не позволяет, 422 — срок не понят."""
    if result.get("ok"):
        return JSONResponse(result)
    code = result.get("code")
    status = 404 if code == "not_found" else 409 if code in ("bad_status", "changed_meanwhile", "approval_required") else 422
    return JSONResponse(result, status_code=status)


# --- прогоны -------------------------------------------------------------------------------------

@handler
async def run_processing(request: Request) -> JSONResponse:
    data = await body(request) if await request.body() else {}
    state = state_of(request)
    since = None
    if data.get("since") is not None:
        try:
            since = datetime.fromisoformat(need_str(data, "since", limit=40))
        except ValueError:
            raise BadRequest("поле since: нужна дата или время в формате ISO, например 2026-09-01") from None
    limit = None
    if data.get("limit") is not None:
        limit = need_int(data, "limit")
        if limit < 1:
            raise BadRequest("поле limit: нужно положительное число")
    async with state.pool.acquire() as conn:
        result = await pipeline.plan_run(
            conn, tz=state.config.timezone, trigger="manual", since=since, limit=limit,
            rescan=data.get("rescan") is True)
    return JSONResponse(result)


@handler
async def processing_status(request: Request) -> JSONResponse:
    async with state_of(request).ro_pool.acquire() as conn:
        state = await pipeline.load_state(conn)
        run = await conn.fetchrow("SELECT * FROM processing_runs ORDER BY id DESC LIMIT 1")
        counts = await conn.fetchrow(
            """SELECT (SELECT count(*) FROM commitments WHERE status = 'proposed') AS proposed,
                      (SELECT count(*) FROM commitments WHERE status = 'open') AS open,
                      (SELECT count(*) FROM person_proposals WHERE status = 'pending') AS merge_proposals""")
    last = None
    if run is not None:
        last = {"id": run["id"], "trigger": run["trigger"], "status": run["status"],
                "started_at": run["started_at"].isoformat(),
                "finished_at": run["finished_at"].isoformat() if run["finished_at"] else None,
                "stats": pipeline._loads(run["stats"])}
    return JSONResponse({"state": state, "last_run": last, "counts": dict(counts)})


# --- обязательства ---------------------------------------------------------------------------------

@handler
async def list_commitments(request: Request) -> JSONResponse:
    view = request.query_params.get("view", "open")
    if view not in commitments.VIEWS:
        raise BadRequest("параметр view: " + ", ".join(commitments.VIEWS))
    direction = request.query_params.get("direction") or None
    if direction not in (None, "owner_owes", "owed_to_owner", "others"):
        raise BadRequest("параметр direction: owner_owes, owed_to_owner или others")
    state = state_of(request)
    async with state.ro_pool.acquire() as conn:
        person_id = _query_int(request, "person_id")
        if person_id is not None:
            person_id = await people.visible_id(conn, person_id)
            if person_id is None:
                raise BadRequest("такого человека нет в реестре", status=404)
        items = await commitments.list_commitments(
            conn, view=view, today=_today(request), person_id=person_id,
            peer_id=_query_int(request, "peer_id"),
            chat_id=_query_int(request, "chat_id"), direction=direction,
            limit=_query_int(request, "limit") or 100)
    return JSONResponse({"view": view, "commitments": items})


@handler
async def get_commitment(request: Request) -> JSONResponse:
    async with state_of(request).ro_pool.acquire() as conn:
        item = await commitments.get_commitment(
            conn, request.path_params["commitment_id"], today=_today(request), with_events=True)
    if item is None:
        raise BadRequest("такого обязательства нет", status=404)
    return JSONResponse(item)


@handler
async def change_commitment(request: Request) -> JSONResponse:
    commitment_id, action = request.path_params["commitment_id"], request.path_params["action"]
    state = state_of(request)
    today = _today(request)
    async with state.pool.acquire() as conn:
        payload = {"commitment_id": commitment_id, "action": action}
        if action == "reschedule":
            data = await body(request)
            payload["due"] = need_str(data, "due", limit=120)
            result = await commitments.reschedule(
                conn, commitment_id, payload["due"], tz=state.config.timezone, today=today, actor="agent")
        else:
            command = _COMMANDS.get(action)
            if command is None:
                raise BadRequest("неизвестное действие", status=404)
            result = await command(conn, commitment_id, today=today, actor="agent")
        if result.get("code") == "approval_required":
            item = await commitments.get_commitment(conn, commitment_id, today=today)
            if item is None:
                return _outcome({"ok": False, "code": "not_found"})
            payload["fingerprint"] = item["approval_fingerprint"]
            summary = _commitment_ask(action, item)
            if action == "reschedule":
                summary += " Новый срок: " + clean_line(payload["due"], 120)
            answer, result = await settle(conn, COMMITMENT_DECIDE, payload, summary=summary)
            if answer is not None:
                return answer
    return _outcome(result)


_COMMANDS = {"close": commitments.close, "cancel": commitments.cancel, "reopen": commitments.reopen,
             "accept": commitments.accept, "reject": commitments.reject}


def _approves(action: str, status: str, owner_approved: bool = False) -> bool:
    """Становится ли от этого действия принятым то, что владелец не одобрял."""
    return action == "reschedule" or (not owner_approved and action in ("accept", "reopen", "close"))


def _commitment_ask(action: str, item: dict[str, Any]) -> str:
    # Формулировка обязательства и слова о сроке взяты из чужих сообщений — в карточку они не
    # попадают: только номер, стороны и дата. Текст владелец видит в сводке предложений и в кабинете.
    who = clean_line(commitments.who_line(item), 120)
    due = f"срок {date.fromisoformat(item['due_date']):%d.%m.%Y}" if item["due_date"] else "срок не определён"
    state = commitments.STATUS_TEXT.get(item["status"], item["status"])
    head = {
        "accept": f"Принять предложенное обязательство № {item['id']}",
        "reopen": f"Вернуть в работу обязательство № {item['id']}, которое вы не подтверждали (сейчас: {state})",
        "close": f"Отметить выполненным обязательство № {item['id']}, которое вы ещё не подтверждали",
        "reschedule": f"Перенести срок обязательства № {item['id']}",
    }[action]
    return (f"{head} ({who}; {due}). Содержание: «{clean_line(item['what'], 1200)}». "
            "Ассистент будет считать его договорённостью, которую вы подтвердили, "
            "и показывать в сводках.")


@confirm.applier(COMMITMENT_DECIDE)
async def _apply_commitment(conn: Any, payload: dict[str, Any]) -> confirm.Done:
    action = payload.get("action")
    command = _COMMANDS.get(action) if action in ("accept", "reopen", "close") else None
    if action == "reschedule":
        command = commitments.reschedule
    if command is None:
        raise confirm.Refused("неизвестное действие", 404)
    confirm.must_not_widen(True)      # одобрение предложения — только с нажатия владельца
    kwargs = {"today": _today_now(), "expected_fingerprint": payload.get("fingerprint"), "actor": "owner"}
    if action == "reschedule":
        kwargs.update(wording=str(payload["due"]), tz=_state.config.timezone if _state is not None else "UTC")
    result = await command(conn, int(payload["commitment_id"]), **kwargs)
    if not result.get("ok"):
        code = result.get("code")
        raise confirm.Refused(result.get("error") or "не получилось", 404 if code == "not_found" else 409,
                              code, extra=result)
    return confirm.Done(result=result)


# --- люди ---------------------------------------------------------------------------------------------

def _people_error(exc: people.PeopleError) -> BadRequest:
    return BadRequest(str(exc), status=409)


async def _need_visible(conn, person_id: int) -> None:
    """Человек без видимого следа для маршрутов не существует — в том числе для правок."""
    if await people.visible_id(conn, person_id) is None:
        raise BadRequest("такого человека нет в реестре", status=404)


@handler
async def list_people(request: Request) -> JSONResponse:
    async with state_of(request).ro_pool.acquire() as conn:
        found = await people.search_people(
            conn, request.query_params.get("query"), chat_id=_query_int(request, "chat_id"),
            limit=_query_int(request, "limit") or 50)
    return JSONResponse({"people": found})


@handler
async def get_person(request: Request) -> JSONResponse:
    async with state_of(request).ro_pool.acquire() as conn:
        person = await people.get_person(conn, request.path_params["person_id"])
    if person is None:
        raise BadRequest("такого человека нет в реестре", status=404)
    return JSONResponse(person)


@handler
async def person_aliases(request: Request) -> JSONResponse:
    data = await body(request)
    alias = need_str(data, "alias", limit=120)
    person_id = request.path_params["person_id"]
    async with state_of(request).pool.acquire() as conn:
        await _need_visible(conn, person_id)
        if request.method == "DELETE":
            try:
                result = await people.remove_alias(conn, person_id, alias)
            except people.PeopleError as exc:
                raise _people_error(exc) from None
        else:
            # Алиас от владельца подтверждает человека в реестре, а о подтверждённом заводится
            # страница. Для ещё не подтверждённой записи это — одобрение владельца: ждёт его.
            row = await conn.fetchrow(
                "SELECT confirmed, merged_into FROM people WHERE id = $1", await people.active_id(conn, person_id))
            summary = None
            if not people.fold(alias) and not people._clean_display(alias).startswith("@"):
                raise BadRequest("Алиас должен содержать буквы.", status=409)   # до карточки, а не после
            if row is not None and not row["confirmed"] and row["merged_into"] is None:
                summary = (f"Добавить имя «{clean_line(alias, 120)}» к записи о человеке "
                           f"{await _person_name(conn, person_id)}. Вы эту запись ещё не подтверждали: вместе с "
                           "новым именем она станет подтверждённой, и о человеке будет заведена страница памяти.")
            answer, result = await settle(conn, PEOPLE_ALIAS, {"person_id": person_id, "alias": alias},
                                          summary=summary)
            if answer is not None:
                return answer
        person = await people.get_person(conn, person_id)
    return JSONResponse({**result, "person": person})


@confirm.applier(PEOPLE_ALIAS)
async def _apply_alias(conn: Any, payload: dict[str, Any]) -> confirm.Done:
    person_id = int(payload["person_id"])
    await _visible_or_refuse(conn, person_id)
    # Без нажатия владельца алиас добавляется только уже подтверждённому человеку
    # (подтверждение назад не снимается, поэтому блокировка строки здесь не нужна).
    confirm.must_not_widen(not await conn.fetchval(
        "SELECT confirmed FROM people WHERE id = $1", await people.active_id(conn, person_id)))
    try:
        result = await people.add_alias(conn, person_id, str(payload.get("alias") or ""))
    except people.PeopleError as exc:
        raise confirm.Refused(str(exc), 409) from None
    return confirm.Done(result=result)


@handler
async def merge_people(request: Request) -> JSONResponse:
    data = await body(request)
    async with state_of(request).pool.acquire() as conn:
        if data.get("proposal_id") is not None:
            proposal_id = need_int(data, "proposal_id")
            row = await conn.fetchrow(
                "SELECT status, person_id, other_person_id FROM person_proposals WHERE id = $1", proposal_id)
            if row is None or row["status"] != "pending":
                # Решать нечего: ответ тот же, что и раньше («нет такого» или «уже решено»).
                try:
                    result = await people.decide_proposal(conn, proposal_id, accept=True)
                except people.PeopleError as exc:
                    raise _people_error(exc) from None
                return JSONResponse({**result, "person": None})
            payload, pair = {"proposal_id": proposal_id}, (row["person_id"], row["other_person_id"])
        else:
            source_id, target_id = need_int(data, "source_id"), need_int(data, "target_id")
            await _need_visible(conn, source_id)
            await _need_visible(conn, target_id)
            if source_id == target_id:
                raise BadRequest("Нельзя объединить запись саму с собой.", status=409)
            payload, pair = {"source_id": source_id, "target_id": target_id}, (source_id, target_id)
        names = [await _person_name(conn, person_id) for person_id in pair]
        answer, result = await settle(
            conn, PEOPLE_MERGE, payload,
            summary=f"Объединить записи о людях: {names[0]} и {names[1]} станут одним человеком. Их учётные "
                    "записи, имена и обязательства ассистент будет относить к одному человеку, и страница "
                    "памяти у них будет общая.")
        if answer is not None:
            return answer
        person = await people.get_person(conn, result["person_id"]) if result.get("person_id") else None
    return JSONResponse({**result, "person": person})


async def _person_name(conn: Any, person_id: int) -> str:
    name = await conn.fetchval("SELECT display_name FROM people WHERE id = $1", person_id)
    return f"«{clean_line(name, 60) or 'без имени'}» (запись № {person_id})"


async def _visible_or_refuse(conn: Any, person_id: int) -> None:
    if await people.visible_id(conn, person_id) is None:
        raise confirm.Refused("такого человека нет в реестре", 404)


@confirm.applier(PEOPLE_MERGE)
async def _apply_merge(conn: Any, payload: dict[str, Any]) -> confirm.Done:
    confirm.must_not_widen(True)      # объединение людей — только с нажатия владельца
    try:
        if payload.get("proposal_id") is not None:
            result = await people.decide_proposal(conn, int(payload["proposal_id"]), accept=True)
        else:
            source_id, target_id = int(payload["source_id"]), int(payload["target_id"])
            await _visible_or_refuse(conn, source_id)
            await _visible_or_refuse(conn, target_id)
            result = await people.merge_people(conn, source_id, target_id)
    except people.PeopleError as exc:
        raise confirm.Refused(str(exc), 409) from None
    return confirm.Done(result=result)


@confirm.applier(PEOPLE_SPLIT)
async def _apply_split(conn: Any, payload: dict[str, Any]) -> confirm.Done:
    confirm.must_not_widen(True)      # разделение заводит нового подтверждённого человека
    person_id = int(payload["person_id"])
    await _visible_or_refuse(conn, person_id)
    try:
        result = await people.split_person(conn, person_id, int(payload["peer_id"]))
    except people.PeopleError as exc:
        raise confirm.Refused(str(exc), 409) from None
    return confirm.Done(result=result)


@handler
async def split_person(request: Request) -> JSONResponse:
    data = await body(request)
    person_id, peer_id = request.path_params["person_id"], need_int(data, "peer_id")
    async with state_of(request).pool.acquire() as conn:
        await _need_visible(conn, person_id)
        peer = await conn.fetchrow(
            """SELECT p.name, p.username, p.tg_id FROM person_peers pp JOIN peers p ON p.id = pp.peer_id
               WHERE pp.person_id = $1 AND pp.peer_id = $2""", person_id, peer_id)
        if peer is None:    # спрашивать владельца не о чем
            raise BadRequest("Эта учётная запись не привязана к этому человеку.", status=409)
        account = clean_line(peer["name"], 60) or (
            f"@{peer['username']}" if peer["username"] else f"Telegram {peer['tg_id']}")
        answer, result = await settle(
            conn, PEOPLE_SPLIT, {"person_id": person_id, "peer_id": peer_id},
            summary=f"Отделить учётную запись Telegram «{account}» от записи "
                    f"{await _person_name(conn, person_id)} в самостоятельного человека. Ассистент будет "
                    "считать их разными людьми; для нового человека заводится запись в реестре, "
                    "а за ней — страница памяти.")
    return answer or JSONResponse(result)


@handler
async def list_proposals(request: Request) -> JSONResponse:
    async with state_of(request).ro_pool.acquire() as conn:
        found = await people.list_proposals(conn, limit=_query_int(request, "limit") or 50)
    return JSONResponse({"proposals": found})


@handler
async def reject_proposal(request: Request) -> JSONResponse:
    async with state_of(request).pool.acquire() as conn:
        try:
            result = await people.decide_proposal(conn, request.path_params["proposal_id"], accept=False)
        except people.PeopleError as exc:
            raise BadRequest(str(exc), status=404) from None
    return JSONResponse(result)


def routes() -> list[BaseRoute]:
    return [
        Route("/api/processing/run", run_processing, methods=["POST"]),
        Route("/api/processing/status", processing_status, methods=["GET"]),
        Route("/api/commitments", list_commitments, methods=["GET"]),
        Route("/api/commitments/{commitment_id:int}", get_commitment, methods=["GET"]),
        Route("/api/commitments/{commitment_id:int}/{action}", change_commitment, methods=["POST"]),
        Route("/api/people", list_people, methods=["GET"]),
        Route("/api/people/proposals", list_proposals, methods=["GET"]),
        Route("/api/people/proposals/{proposal_id:int}/reject", reject_proposal, methods=["POST"]),
        Route("/api/people/merge", merge_people, methods=["POST"]),
        Route("/api/people/{person_id:int}", get_person, methods=["GET"]),
        Route("/api/people/{person_id:int}/aliases", person_aliases, methods=["POST", "DELETE"]),
        Route("/api/people/{person_id:int}/split", split_person, methods=["POST"]),
    ]


@contextlib.asynccontextmanager
async def lifespan(state: AppState) -> AsyncIterator[None]:
    at = nightly_time(state.config)

    async def on_deleted(payload: dict[str, Any]) -> None:
        """Сообщения удалены у собеседника: убираем выведенные из них обязательства."""
        ids = [i for i in payload.get("message_ids") or [] if isinstance(i, int)]
        if ids:
            async with state.pool.acquire() as conn:
                await commitments.purge_for_messages(conn, ids)

    async def nightly() -> None:
        while True:
            # сначала пауза: сразу после запуска сервис ещё принимает пропущенные сообщения
            await asyncio.sleep(TICK_SECONDS)
            try:
                async with state.pool.acquire() as conn:
                    await pipeline.finish_stale_runs(conn)
                    await pipeline.nightly_tick(conn, tz=state.config.timezone, at=at)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # только вид ошибки: в тексте ошибки базы может оказаться фрагмент переписки
                logger.error("ночной прогон обработки не запустился: %s", type(exc).__name__)

    async def on_chat_excluded(payload: dict[str, Any]) -> None:
        """Владелец исключил чат: выведенные из него обязательства убираются сразу, не дожидаясь ночи.
        (Показывать их перестают ещё раньше — исключённый чат отсекается в каждом запросе чтения.)"""
        async with state.pool.acquire() as conn:
            await commitments.purge_orphans(conn)

    state.events.subscribe(events.MESSAGES_DELETED, on_deleted)
    state.events.subscribe(events.CHAT_EXCLUDED, on_chat_excluded)
    state.spawn(nightly(), name="processing-nightly")
    global _state
    _state = state
    try:
        yield
    finally:
        if _state is state:
            _state = None

