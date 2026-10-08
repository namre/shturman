"""A live owner decision resumes one bounded task beyond automatic trigger age."""

from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shturman import authority, bridge
from shturman.outbox import autoreply, drafts, policy
from shturman.replies import owner, workflow
from shturman.sources import broker

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outbox"))
from outbox_helpers import OWNER, IVAN, MARIA, add_chat, add_message, env, settle, take  # noqa: E402,F401
from test_outbox_autoreply import incoming, model_says, trusted_setup  # noqa: E402


@pytest.fixture
def bounded_task(monkeypatch):
    task = dict(id=7, status="waiting_source", expires_at=datetime.now(timezone.utc),
                chat_id=3, account_id=4, target_peer_id=5, target_tg_id=IVAN,
                trigger_message_id=9, trigger_hash=workflow.digest("question"),
                policy_revision="same", source_refs=[])
    gates = dict(expired=False, old=True, newer=False)
    async def fetchval(sql, *args):
        if "clock_timestamp()" in sql:
            return gates["expired"]
        if "sent_at <" in sql:
            return gates["old"]
        return gates["newer"]
    conn = SimpleNamespace(
        fetchval=AsyncMock(side_effect=fetchval),
        fetchrow=AsyncMock(return_value=dict(text="question", deleted_at=None,
                                             agent_visible=True, edited_at=None)),
        execute=AsyncMock())
    target = SimpleNamespace(account_id=4, peer_id=5, tg_id=IVAN)
    monkeypatch.setattr(bridge, "owns_bot", lambda: True)
    monkeypatch.setattr(bridge, "get_owner", AsyncMock(return_value=dict(user_id=OWNER, chat_id=OWNER)))
    monkeypatch.setattr(policy, "target", AsyncMock(return_value=target))
    monkeypatch.setattr(autoreply, "eligible", AsyncMock(return_value=policy.ALLOW))
    monkeypatch.setattr(autoreply, "load", AsyncMock(return_value=dict(max_age_seconds=300)))
    monkeypatch.setattr(workflow, "policy_revision", AsyncMock(return_value="same"))
    monkeypatch.setattr(broker, "validate_refs", AsyncMock(return_value=True))
    return conn, SimpleNamespace(), task, gates


@pytest.mark.parametrize("status", ["waiting_source", "waiting_owner", "draft_ready"])
async def test_late_waiting_task_requires_fresh_private_bound_owner(bounded_task, status):
    conn, state, task, _ = bounded_task
    task["status"] = status
    assert not await workflow.valid(conn, state, task)
    with authority.owner_context(OWNER, chat_id=OWNER):
        assert await workflow.valid(conn, state, task)


@pytest.mark.parametrize("context", [
    lambda: authority.owner_context(OWNER + 1, chat_id=OWNER + 1),
    lambda: authority.owner_context(OWNER, chat_id=OWNER + 1),
    lambda: authority.setup_context("authenticated-page"),
])
async def test_other_authority_does_not_extend_automatic_age(bounded_task, context):
    conn, state, task, _ = bounded_task
    with context():
        assert not await workflow.valid(conn, state, task)
        with pytest.raises(PermissionError):
            await workflow.record_owner_resume(conn, task["id"])
    conn.execute.assert_not_awaited()


@pytest.mark.parametrize("status", ["queued", "generating", "drafting"])
async def test_owner_presence_does_not_extend_unsolicited_work(bounded_task, status):
    conn, state, task, _ = bounded_task
    task["status"] = status
    with authority.owner_context(OWNER, chat_id=OWNER):
        assert not await workflow.valid(conn, state, task)


@pytest.mark.parametrize("by,via,has_time,allowed", [
    (OWNER, "telegram", True, True),
    (OWNER + 1, "telegram", True, False),
    (OWNER, None, True, False),
    (OWNER, "telegram", False, False),
])
async def test_background_continuation_requires_same_owner_receipt(bounded_task, by, via, has_time, allowed):
    conn, state, task, _ = bounded_task
    task.update(status="generating", owner_resume_by=by, owner_resume_via=via,
                owner_resume_at=datetime.now(timezone.utc) if has_time else None)
    assert await workflow.valid(conn, state, task) is allowed


@pytest.mark.parametrize("gate", ["expiry", "newer", "edited", "policy", "source"])
@pytest.mark.parametrize("durable", [False, True])
async def test_owner_attention_preserves_other_validity_gates(bounded_task, gate, durable):
    conn, state, task, gates = bounded_task
    if durable:
        task.update(status="generating", owner_resume_by=OWNER, owner_resume_via="telegram",
                    owner_resume_at=datetime.now(timezone.utc))
    if gate == "expiry":
        gates["expired"] = True
    elif gate == "newer":
        gates["newer"] = True
    elif gate == "edited":
        conn.fetchrow.return_value["edited_at"] = datetime.now(timezone.utc)
    elif gate == "policy":
        workflow.policy_revision.return_value = "changed"
    else:
        broker.validate_refs.return_value = False
    with nullcontext() if durable else authority.owner_context(OWNER, chat_id=OWNER):
        assert not await workflow.valid(conn, state, task)


async def _age_trigger(env, task):
    await env.conn.execute("UPDATE messages SET sent_at=sent_at-interval '6 minutes' WHERE chat_id=$1 AND id<=$2",
                           task["chat_id"], task["trigger_message_id"])


async def _source_task(env):
    await trusted_setup(env)
    chat = await add_chat(env.conn, env.helper_acc)
    source = await add_chat(env.conn, env.helper_acc, MARIA, name="Источник")
    await add_message(env.conn, source, 20, "Смета составляет 100 рублей.", sender=MARIA)
    await incoming(env, chat, 21, "Какова сумма сметы?")
    request = dict(kind="chat", source_id=str(source), query="смета", limit=2,
                   max_chars=500, reason="Проверить сумму")
    await take(env.conn, bridge.LLM_STRUCTURED, complete=dict(
        parsed=dict(outcome="need_source", request=request), model="test"))
    task = await workflow.get(env.conn, await env.conn.fetchval("SELECT id FROM reply_tasks"))
    assert task["status"] == "waiting_source"
    return task, chat, request


async def _approve(env, draft):
    with authority.owner_context(OWNER, chat_id=OWNER):
        async with env.conn.transaction():
            answer = await drafts.on_button(env.conn, f's:{draft["id"]}:{draft["nonce"]}', OWNER)
    assert answer["remove_buttons"]
    env.mod.kick()
    await settle(env)


async def test_minute_six_source_decision_resumes_and_sends_exact_draft(env, own_bot):
    task, _, _ = await _source_task(env)
    await _age_trigger(env, task)
    with authority.owner_context(OWNER, chat_id=OWNER):
        async with env.conn.transaction():
            refused = await owner.on_button(env.conn, f'y:{task["id"]}:wrong-nonce', OWNER)
    assert not refused["remove_buttons"]
    assert await env.conn.fetchval("SELECT owner_resume_at FROM reply_tasks WHERE id=$1", task["id"]) is None
    with authority.owner_context(OWNER, chat_id=OWNER):
        async with env.conn.transaction():
            accepted = await owner.on_button(env.conn, f'y:{task["id"]}:{task["nonce"]}', OWNER)
    assert accepted["remove_buttons"]
    resumed = await workflow.get(env.conn, task["id"])
    assert (resumed["status"], resumed["owner_resume_by"], resumed["owner_resume_via"]) == (
        "generating", OWNER, "telegram")
    await model_says(env, "Сумма сметы — 100 рублей.")
    draft = await env.conn.fetchrow("SELECT * FROM outbox_drafts")
    assert draft["status"] == "pending" and env.tg.sent == []
    await _approve(env, draft)
    assert [(m["tg_id"], m["text"]) for m in env.tg.sent] == [(IVAN, "Сумма сметы — 100 рублей.")]


async def test_minute_six_owner_clarification_survives_background_completion(env, own_bot):
    await trusted_setup(env)
    chat = await add_chat(env.conn, env.helper_acc)
    await incoming(env, chat)
    await take(env.conn, bridge.LLM_STRUCTURED, complete=dict(
        parsed=dict(outcome="ask_owner", question="Какой срок обещать?"), model="test"))
    task = await workflow.get(env.conn, await env.conn.fetchval("SELECT id FROM reply_tasks"))
    assert task["status"] == "waiting_owner"
    await _age_trigger(env, task)
    with authority.owner_context(OWNER, chat_id=OWNER):
        async with env.conn.transaction():
            result = await owner.handle_command(env.conn, env.state, f'/replytask {task["id"]} answer В пятницу.', OWNER)
    assert "продолжена" in result["text"]
    await model_says(env, "Смета будет в пятницу.")
    draft = await env.conn.fetchrow("SELECT * FROM outbox_drafts")
    assert draft["status"] == "pending"
    await _approve(env, draft)
    assert [m["text"] for m in env.tg.sent] == ["Смета будет в пятницу."]


async def test_persistent_read_grant_does_not_supply_fresh_attention_to_new_task(env, own_bot):
    task, chat, request = await _source_task(env)
    with authority.owner_context(OWNER, chat_id=OWNER):
        async with env.conn.transaction():
            await owner.on_button(env.conn, f'r:{task["id"]}:{task["nonce"]}', OWNER)
    await model_says(env, "БЕЗ_ОТВЕТА")
    await incoming(env, chat, 22, "Уточни сумму сметы.")
    await take(env.conn, bridge.LLM_STRUCTURED, complete=dict(
        parsed=dict(outcome="need_source", request=request), model="test"))
    second_id = await env.conn.fetchval("SELECT id FROM reply_tasks ORDER BY id DESC LIMIT 1")
    second = await workflow.get(env.conn, second_id)
    assert second["status"] == "generating" and second["requires_approval"]
    assert second["owner_resume_at"] is None
    await model_says(env, "Сумма сметы — 100 рублей.")
    draft = await env.conn.fetchrow("SELECT * FROM outbox_drafts WHERE task_id=$1", second_id)
    assert draft["status"] == "pending" and env.tg.sent == []
    await _age_trigger(env, second)
    assert not await workflow.valid(env.conn, env.state, await workflow.get(env.conn, second_id))
    await _approve(env, draft)
    assert await env.conn.fetchval("SELECT owner_resume_by FROM reply_tasks WHERE id=$1", second_id) == OWNER
    assert [m["text"] for m in env.tg.sent] == ["Сумма сметы — 100 рублей."]


async def test_late_unsolicited_model_reply_is_still_cancelled(env, own_bot):
    await trusted_setup(env)
    chat = await add_chat(env.conn, env.helper_acc)
    await incoming(env, chat)
    task = await workflow.get(env.conn, await env.conn.fetchval("SELECT id FROM reply_tasks"))
    await _age_trigger(env, task)
    await model_says(env, "Ответ пришёл слишком поздно.")
    assert await env.conn.fetchval("SELECT status FROM reply_tasks") == "cancelled"
    assert await env.conn.fetchval("SELECT count(*) FROM outbox_drafts") == 0
    assert env.tg.sent == []


async def test_old_initial_incoming_does_not_start_model_work(env):
    await trusted_setup(env)
    chat = await add_chat(env.conn, env.helper_acc)
    await incoming(env, chat, age=360)
    assert await take(env.conn, bridge.LLM_STRUCTURED) == []
    assert await env.conn.fetchval("SELECT count(*) FROM outbox_drafts") == 0
    assert env.tg.sent == []
