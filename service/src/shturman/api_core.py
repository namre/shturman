"""Общие маршруты внутреннего API: очередь заданий, нажатия кнопок, владелец, сводное состояние."""

from __future__ import annotations

import contextlib
from typing import Any, AsyncIterator

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from . import bridge, jobs
from .app import AppState, state_of

REAP_EVERY = 60  # секунд


class BadRequest(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message, self.status = message, status


async def body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
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
            return JSONResponse({"error": exc.message}, status_code=exc.status)
    wrapped.__name__ = fn.__name__
    return wrapped


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
        ok = await bridge.deliver_result(conn, int(request.path_params["job_id"]), result)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 409)


@handler
async def fail_job(request: Request) -> JSONResponse:
    data = await body(request)
    retry = data.get("retry_in", 60)
    async with state_of(request).pool.acquire() as conn:
        status = await bridge.deliver_failure(
            conn, int(request.path_params["job_id"]), str(data.get("error") or "ошибка без описания"),
            retry_in=None if retry is None else int(retry),
        )
    return JSONResponse({"status": status})


@handler
async def telegram_callback(request: Request) -> JSONResponse:
    data = await body(request)
    async with state_of(request).pool.acquire() as conn:
        out = await bridge.dispatch_callback(conn, need_str(data, "data", limit=64), need_int(data, "from_user_id"))
    return JSONResponse(out)


@handler
async def put_owner(request: Request) -> JSONResponse:
    data = await body(request)
    async with state_of(request).pool.acquire() as conn:
        await bridge.set_owner(conn, need_int(data, "user_id"), need_int(data, "chat_id"))
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
    out = dict(row)
    out["last_message_seen_at"] = out["last_message_seen_at"].isoformat() if out["last_message_seen_at"] else None
    out["owner_known"] = bool(out["owner_known"])
    return JSONResponse(out)


def routes() -> list[BaseRoute]:
    return [
        Route("/api/status", status, methods=["GET"]),
        Route("/api/owner", put_owner, methods=["PUT"]),
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

    state.spawn(reaper(), name="jobs-reaper")
    yield
