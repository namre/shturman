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
        # Recover completion missed by an interrupted sender before considering task expiry.
        finished = await conn.fetch(
            "SELECT t.id,d.status,d.error_code FROM reply_tasks t JOIN outbox_drafts d ON d.id=t.draft_id "
            "WHERE t.status='draft_ready' AND d.status IN "
            "('sent','rejected','superseded','expired','failed','outcome_unknown')")
        for row in finished:
            status = ('completed' if row['status'] == 'sent' else 'declined' if row['status'] == 'rejected' else
                      'expired' if row['status'] == 'expired' else
                      'cancelled' if row['status'] == 'superseded' else 'failed')
            await workflow.stop(conn, row['id'], status, row['error_code'])
        expired = await conn.fetch(
            "SELECT t.id FROM reply_tasks t LEFT JOIN outbox_drafts d ON d.id=t.draft_id "
            "WHERE t.expires_at<=now() AND t.status NOT IN "
            "('completed','declined','cancelled','expired','failed') "
            "AND (d.status IS NULL OR d.status NOT IN ('approved','sending'))")
        for row in expired:
            await workflow.stop(conn, row['id'], 'expired', 'expired')
        await conn.execute("UPDATE reply_tasks SET input_messages='[]'::jsonb,owner_answer=NULL,owner_question=NULL "
                           "WHERE updated_at<now()-interval '24 hours' AND status IN "
                           "('completed','declined','cancelled','expired','failed')")
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
