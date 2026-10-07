"""Общие маршруты внутреннего API: очередь заданий, нажатия кнопок, владелец, сводное состояние."""

from __future__ import annotations

import contextlib
from typing import Any, AsyncIterator

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from . import bridge, confirm, embeddings, jobs
from .guard import service as guard_service
from .app import AppState, state_of

REAP_EVERY = 60  # секунд


class BadRequest(Exception):
    def __init__(self, message: str, status: int = 400, code: str | None = None,
                 extra: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message, self.status, self.code, self.extra = message, status, code, extra or {}


MAX_BODY = 1024 * 1024  # байт: JSON-запросы внутреннего API заведомо меньше


async def body(request: Request) -> dict[str, Any]:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY:
        raise BadRequest("тело запроса слишком большое", 413)
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            raise BadRequest("тело запроса слишком большое", 413)
        chunks.append(chunk)
    try:
        import json as _json

        data = _json.loads(b"".join(chunks) or b"null")
    except Exception:
        raise BadRequest("тело запроса должно быть JSON-объектом") from None
    if not isinstance(data, dict):
        raise BadRequest("тело запроса должно быть JSON-объектом")
    return data


def need_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise BadRequest(f"поле {key}: нужно целое число")
    return value


def need_str(data: dict[str, Any], key: str, *, limit: int = 10_000) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BadRequest(f"поле {key}: нужна непустая строка")
    return value[:limit]


def handler(fn):
    """Оборачивает обработчик: ошибки запроса превращаются в ответ с кодом и текстом."""
    async def wrapped(request: Request) -> JSONResponse:
        try:
            return await fn(request)
        except BadRequest as exc:
            return error_response(exc)
        except (ValueError, TypeError):
            # число не того вида в запросе — это ошибка запроса, а не сервиса
            return JSONResponse({"error": "неверное значение в запросе"}, status_code=400)
    wrapped.__name__ = fn.__name__
    return wrapped


def error_response(exc: BadRequest) -> JSONResponse:
    payload = {**exc.extra, "error": exc.message}
    if exc.code:
        payload["code"] = exc.code
    return JSONResponse(payload, status_code=exc.status)


# --- подтверждение владельцем (см. confirm.py) ---

async def settle(
    conn, kind: str, payload: dict[str, Any], *, summary: str | None,
    applied_now: dict[str, Any] | None = None,
) -> tuple[JSONResponse | None, Any]:
    """Применяет действие или ставит его ждать нажатия владельца в боте согласований.

    summary — что именно изменится, простыми словами: этот текст увидит владелец на карточке.
    summary=None значит «это ужесточение»: действие применяется сразу в любом режиме.

    Возвращает (ответ, результат). Ответ не None — действие ждёт подтверждения: его и нужно
    вернуть вызывающему (HTTP 202, тело {"status": "pending_confirmation", "action_id", "summary",
    "expires_at", "note"}; в applied_now — то, что из запроса применено сразу). Иначе действие
    применено, и результат — то, что вернула функция применения (`confirm.Done.result`).

    Вызывать вне транзакции (см. `confirm.apply`).
    """
    try:
        if summary is None:
            out = await confirm.apply(conn, kind, payload)
        else:
            out = await confirm.request(conn, kind, summary, payload)
    except confirm.Refused as exc:
        raise BadRequest(exc.message, exc.status, exc.code, exc.extra) from None
    except confirm.TooManyPending:
        raise BadRequest(
            "Слишком много действий уже ждут вашего подтверждения в боте согласований. "
            "Ответьте на карточки в боте или отмените лишние, затем повторите.", 429, "too_many_pending") from None
    except confirm.NoOwner:
        raise BadRequest(
            "Это действие нужно подтвердить в боте согласований, а владелец к боту ещё не привязан: "
            "подтвердить некому. Сначала привяжите владельца.", 409, "owner_unknown") from None
    if out["status"] != confirm.PENDING:
        return None, out.get("result")
    answer = {"status": confirm.PENDING, "action_id": out["action_id"], "summary": out["summary"],
              "expires_at": out["expires_at"], "note": out["note"]}
    if applied_now:
        answer["applied_now"] = applied_now
    return JSONResponse(answer, status_code=202), None


@handler
async def claim_jobs(request: Request) -> JSONResponse:
    data = await body(request)
    kinds = [k for k in data.get("kinds") or bridge.EXECUTOR_KINDS if k in bridge.EXECUTOR_KINDS]
    if not kinds:
        raise BadRequest("поле kinds: нет известных видов заданий")
    async with state_of(request).pool.acquire() as conn:
        claimed = await jobs.claim(
            conn, kinds, worker=str(data.get("worker") or "plugin")[:64],
            limit=int(data.get("limit") or 1),
        )
    return JSONResponse({"jobs": claimed})


@handler
async def complete_job(request: Request) -> JSONResponse:
    data = await body(request)
    result = data.get("result")
    if not isinstance(result, dict):
        raise BadRequest("поле result: нужен объект")
    async with state_of(request).pool.acquire() as conn:
        ok = await bridge.deliver_result(
            conn, int(request.path_params["job_id"]), result, executor="plugin",
        )
    return JSONResponse({"ok": ok}, status_code=200 if ok else 409)


@handler
async def fail_job(request: Request) -> JSONResponse:
    data = await body(request)
    retry = data.get("retry_in", 60)
    async with state_of(request).pool.acquire() as conn:
        status = await bridge.deliver_failure(
            conn, int(request.path_params["job_id"]), str(data.get("error") or "ошибка без описания"),
            retry_in=None if retry is None else int(retry), executor="plugin",
        )
    return JSONResponse({"status": status})


OWN_BOT = "own_bot"  # код отказа: по нему плагин понимает, что повторять запрос бесполезно


def only_without_own_bot() -> None:
    """Со своим ботом сервиса нажатия, привязка владельца и бизнес-поток через плагин
    не принимаются: иначе их мог бы подделать ассистент, у которого в Hermes есть терминал."""
    if bridge.owns_bot():
        raise BadRequest("это действие выполняется только через бота согласований", 403, OWN_BOT)


@handler
async def telegram_callback(request: Request) -> JSONResponse:
    only_without_own_bot()
    data = await body(request)
    async with state_of(request).pool.acquire() as conn:
        out = await bridge.dispatch_callback(conn, need_str(data, "data", limit=64), need_int(data, "from_user_id"))
    return JSONResponse(out)


@handler
async def put_owner(request: Request) -> JSONResponse:
    only_without_own_bot()
    data = await body(request)
    async with state_of(request).pool.acquire() as conn:
        await bridge.set_owner(conn, need_int(data, "user_id"), need_int(data, "chat_id"))
    return JSONResponse({"ok": True})


@handler
async def delete_owner(request: Request) -> JSONResponse:
    only_without_own_bot()
    async with state_of(request).pool.acquire() as conn:
        await bridge.clear_owner(conn)
    return JSONResponse({"ok": True})


@handler
async def status(request: Request) -> JSONResponse:
    """Сводное состояние без содержимого переписки: только счётчики."""
    async with state_of(request).ro_pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT (SELECT count(*) FROM accounts) AS accounts,
                      (SELECT count(*) FROM chats WHERE NOT excluded) AS chats,
                      (SELECT count(*) FROM chats WHERE excluded) AS chats_excluded,
                      (SELECT count(*) FROM messages) AS messages,
                      (SELECT max(first_seen_at) FROM messages) AS last_message_seen_at,
                      (SELECT count(*) FROM jobs WHERE status IN ('queued', 'running')) AS jobs_waiting,
                      (SELECT count(*) FROM jobs WHERE status = 'failed') AS jobs_failed,
                      (SELECT value IS NOT NULL FROM settings WHERE key = 'owner') AS owner_known"""
        )
        guard = await guard_service.overview(conn, state_of(request).config)
        search = await embeddings.overview(conn, state_of(request))
    out = dict(row)
    # Защита от внедрённых инструкций: включена ли, чем проверяет, и счётчики (проверено, скрыто,
    # показано владельцем, не проверено). Только числа и состояние.
    out.update(guard)
    # Поиск по смыслу: включён ли, какой моделью, сколько сообщений с вектором этой модели
    # и сколько ещё осталось посчитать (после смены модели — пересчитать).
    out.update(search)
    config = state_of(request).config
    out.update(sending=config.sending, own_bot=bridge.owns_bot(),
               own_llm=bridge.LLM_TEXT in bridge.builtin_kinds())
    out["last_message_seen_at"] = out["last_message_seen_at"].isoformat() if out["last_message_seen_at"] else None
    out["owner_known"] = bool(out["owner_known"])
    return JSONResponse(out)


@handler
async def confirmations(request: Request) -> JSONResponse:
    async with state_of(request).ro_pool.acquire() as conn:
        return JSONResponse({"required": confirm.required(), "pending": await confirm.list_pending(conn)})


@handler
async def confirmation(request: Request) -> JSONResponse:
    """Что стало с действием, которое ждало владельца: по номеру из ответа 202."""
    async with state_of(request).ro_pool.acquire() as conn:
        out = await confirm.get(conn, int(request.path_params["action_id"]))
    if out is None:
        raise BadRequest("такого действия нет", 404)
    return JSONResponse(out)


@handler
async def cancel_confirmation(request: Request) -> JSONResponse:
    async with state_of(request).pool.acquire() as conn:
        ok = await confirm.cancel(conn, int(request.path_params["action_id"]))
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


def routes() -> list[BaseRoute]:
    return [
        Route("/api/confirmations", confirmations, methods=["GET"]),
        Route("/api/confirmations/{action_id:int}", confirmation, methods=["GET"]),
        Route("/api/confirmations/{action_id:int}/cancel", cancel_confirmation, methods=["POST"]),
        Route("/api/status", status, methods=["GET"]),
        Route("/api/owner", put_owner, methods=["PUT"]),
        Route("/api/owner", delete_owner, methods=["DELETE"]),
        Route("/api/jobs/claim", claim_jobs, methods=["POST"]),
        Route("/api/jobs/{job_id:int}/complete", complete_job, methods=["POST"]),
        Route("/api/jobs/{job_id:int}/fail", fail_job, methods=["POST"]),
        Route("/api/callbacks/telegram", telegram_callback, methods=["POST"]),
    ]


@contextlib.asynccontextmanager
async def lifespan(state: AppState) -> AsyncIterator[None]:
    import asyncio

    async def reaper() -> None:
        while True:
            await asyncio.sleep(REAP_EVERY)
            async with state.pool.acquire() as conn:
                await bridge.reap_lost(conn)
                await jobs.scrub_finished(conn)
                await confirm.expire(conn)

    state.spawn(reaper(), name="jobs-reaper")
    yield
