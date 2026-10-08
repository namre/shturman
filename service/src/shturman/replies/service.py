"""Task recovery and bounded retention; no network or agent authority here."""
from __future__ import annotations
import asyncio
import contextlib
import logging
from typing import AsyncIterator, Any
from . import owner, workflow  # register bridge handlers

logger = logging.getLogger('shturman.replies')

async def sweep(state: Any) -> None:
    async with state.pool.acquire() as conn, conn.transaction():
        await conn.execute("UPDATE reply_tasks SET status='expired',error_code='expired',updated_at=now() "
                           "WHERE expires_at<=now() AND status NOT IN ('declined','cancelled','expired','failed')")
        await conn.execute("UPDATE reply_tasks SET input_messages='[]'::jsonb,owner_answer=NULL,owner_question=NULL "
                           "WHERE updated_at<now()-interval '24 hours' AND status IN "
                           "('draft_ready','declined','cancelled','expired','failed')")
        rows = await conn.fetch("SELECT chat_id,trigger_message_id FROM reply_tasks WHERE status='queued' "
                                "AND expires_at>now() ORDER BY id LIMIT 20")
    from ..outbox import autoreply, runtime
    mod = runtime.current()
    if mod is not None:
        for row in rows:
            await autoreply._prepare(mod, row['chat_id'], row['trigger_message_id'])

@contextlib.asynccontextmanager
async def lifespan(state: Any) -> AsyncIterator[None]:
    async def run() -> None:
        while True:
            try:
                await sweep(state)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('reply task sweep failed')
            await asyncio.sleep(30)
    state.spawn(run(), name='reply-task-sweeper')
    yield
