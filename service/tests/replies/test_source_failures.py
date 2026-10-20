"""A saved owner read grant is not a false promise that the source was read."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shturman import authority, bridge
from shturman.replies import owner, workflow
from shturman.sources import broker
from shturman.sources.registry import Connector, SourceError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outbox"))
from outbox_helpers import OWNER, add_chat, env, settle, take  # noqa: E402,F401
from test_outbox_autoreply import incoming, trusted_setup  # noqa: E402


@pytest.fixture
def decision(monkeypatch):
    spec = broker.canonical_source_spec(dict(kind="external", source_id="travel", query="билет",
                                             reason="Проверить билет"))
    task = dict(id=7, chat_id=3, status="waiting_source", nonce="fresh", source_request=spec,
                error_code=None, expires_at=datetime.now(timezone.utc) + timedelta(minutes=20),
                decision_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10))
    conn = SimpleNamespace(fetchval=AsyncMock(return_value=False), execute=AsyncMock())
    state = SimpleNamespace()
    monkeypatch.setattr(bridge, "owns_bot", lambda: True)
    monkeypatch.setattr(bridge, "get_owner", AsyncMock(return_value=dict(user_id=OWNER, chat_id=OWNER)))
    monkeypatch.setattr(workflow, "get", AsyncMock(return_value=task))
    monkeypatch.setattr(workflow, "state_current", lambda: state)
    monkeypatch.setattr(workflow, "valid", AsyncMock(return_value=True))
    async def close(conn, task_id, status, code=None):
        task.update(status=status, error_code=code)
    monkeypatch.setattr(workflow, "stop", AsyncMock(side_effect=close))
    monkeypatch.setattr(broker, "grant_for_task", AsyncMock())
    monkeypatch.setattr(broker, "read_for_task", AsyncMock(return_value=dict(status="unavailable")))
    monkeypatch.setattr(workflow, "add_source", AsyncMock())
    return conn, state, task


@pytest.mark.parametrize("choice", ["y", "r"])
@pytest.mark.parametrize("exception", [False, True])
async def test_unavailable_read_closes_task_and_truthfully_consumes_card(decision, choice, exception):
    conn, _, task = decision
    if exception:
        broker.read_for_task.side_effect = SourceError("offline")
    with authority.owner_context(OWNER, chat_id=OWNER):
        result = await owner.on_button(conn, f'{choice}:7:fresh', OWNER)
    broker.grant_for_task.assert_awaited_once()
    assert task["status"] == "failed" and task["error_code"] == "source_unavailable"
    workflow.stop.assert_awaited_once_with(conn, 7, "failed", "source_unavailable")
    workflow.add_source.assert_not_awaited()
    assert result["remove_buttons"] and "Источник недоступен" in result["answer"]
    assert "ответ не подготовлен" in result["answer"]
    assert any("owner_resume_at" in c.args[0] for c in conn.execute.call_args_list)


async def test_context_change_during_read_is_distinct_from_offline(decision):
    conn, _, task = decision
    workflow.valid.side_effect = [True, True, False]
    with authority.owner_context(OWNER, chat_id=OWNER):
        result = await owner.on_button(conn, "r:7:fresh", OWNER)
    assert task["status"] == "cancelled" and task["error_code"] == "context_changed"
    assert "Контекст изменился" in result["answer"] and "Источник недоступен" not in result["answer"]
    broker.grant_for_task.assert_awaited_once()
    workflow.add_source.assert_not_awaited()


async def test_false_resume_cannot_leave_waiting_task_with_consumed_card(decision, monkeypatch):
    conn, _, task = decision
    monkeypatch.setattr(workflow, "owner_granted", AsyncMock(return_value=False))
    with authority.owner_context(OWNER, chat_id=OWNER):
        result = await owner.on_button(conn, "r:7:fresh", OWNER)
    assert task["status"] == "failed" and task["error_code"] == "source_unavailable"
    assert result["remove_buttons"] and "ответ не подготовлен" in result["answer"]


async def test_read_success_that_hits_round_limit_does_not_claim_resume(decision):
    conn, _, task = decision
    broker.read_for_task.return_value = dict(status="ok")
    async def too_many(conn, state, task, data):
        await workflow.stop(conn, task["id"], "failed", "too_many_rounds")
    workflow.add_source.side_effect = too_many
    with authority.owner_context(OWNER, chat_id=OWNER):
        result = await owner.on_button(conn, "r:7:fresh", OWNER)
    assert task["error_code"] == "too_many_rounds"
    assert result["remove_buttons"] and "Ответ не подготовлен" in result["answer"]
    assert "Источник недоступен" not in result["answer"]


async def test_actual_resume_keeps_success_message(decision):
    conn, _, task = decision
    broker.read_for_task.return_value = dict(status="ok")
    async def continue_task(conn, state, task, data):
        task["status"] = "generating"
    workflow.add_source.side_effect = continue_task
    with authority.owner_context(OWNER, chat_id=OWNER):
        result = await owner.on_button(conn, "r:7:fresh", OWNER)
    assert task["status"] == "generating"
    assert result["remove_buttons"] and "30 дней" in result["answer"]
    assert "не подготовлен" not in result["answer"]
    workflow.stop.assert_not_awaited()


async def test_offline_external_read_keeps_persistent_grant_without_retry_or_draft(env, own_bot, monkeypatch):
    from shturman.replies import service as reply_service
    from shturman.sources import mcp_client
    await trusted_setup(env)
    env.state.extras["source_registry"] = dict(travel=Connector(
        "travel", "Билеты", "https://sources.example.org/mcp", search_tool="search", read_tool="read"))
    search = AsyncMock(side_effect=SourceError("offline"))
    monkeypatch.setattr(mcp_client, "search", search)
    chat = await add_chat(env.conn, env.helper_acc)
    await incoming(env, chat, 21, "Когда отправляется поезд?")
    request = dict(kind="external", source_id="travel", query="билет", limit=2,
                   max_chars=500, reason="Проверить билет")
    completion = dict(parsed=dict(outcome="need_source", request=request), model="test")
    await take(env.conn, bridge.LLM_STRUCTURED, complete=completion)
    task = await workflow.get(env.conn, await env.conn.fetchval("SELECT id FROM reply_tasks"))
    assert task["status"] == "waiting_source"
    with authority.owner_context(OWNER, chat_id=OWNER):
        async with env.conn.transaction():
            result = await owner.on_button(env.conn, f'r:{task["id"]}:{task["nonce"]}', OWNER)
    failed = await workflow.get(env.conn, task["id"])
    assert (failed["status"], failed["error_code"]) == ("failed", "source_unavailable")
    assert "ответ не подготовлен" in result["answer"] and result["remove_buttons"]
    grant = await env.conn.fetchrow("SELECT * FROM source_grants")
    assert grant["task_id"] is None and grant["mode"] == "read" and grant["revoked_at"] is None
    assert grant["expires_at"] > failed["expires_at"]
    assert await env.conn.fetchval("SELECT outcome FROM outbox_autoreply_log WHERE id=$1",
                                  failed["outcome_log_id"]) == "failed"
    await reply_service.sweep(env.state)
    await reply_service.sweep(env.state)
    await settle(env)
    assert search.await_count == 1
    assert await take(env.conn, bridge.LLM_STRUCTURED) == []
    assert await env.conn.fetchval("SELECT count(*) FROM outbox_drafts") == 0
    assert env.tg.sent == []
    # Recovery on a new exact request uses the saved read grant, never the failed task.
    search.side_effect = None
    search.return_value = [dict(resource_id="ticket-1", remote_revision="v1", text="Поезд в 8 утра.")]
    await incoming(env, chat, 22, "Проверь время отправления ещё раз.")
    await take(env.conn, bridge.LLM_STRUCTURED, complete=completion)
    new_id = await env.conn.fetchval("SELECT id FROM reply_tasks ORDER BY id DESC LIMIT 1")
    resumed = await workflow.get(env.conn, new_id)
    assert new_id != task["id"] and resumed["status"] == "generating"
    assert resumed["requires_approval"] and resumed["source_refs"][-1]["receipt_id"]
    assert resumed["owner_resume_at"] is None
    assert await env.conn.fetchval("SELECT count(*) FROM source_grants") == 1
    assert env.tg.sent == []
