"""Forum updates pass through the real ingestion, task and draft pipeline without a network."""
import asyncio
import dataclasses
import json
from datetime import datetime, timezone

import pytest
from telethon import utils
from telethon.sessions import MemorySession
from telethon.tl import types

from shturman import authority, bridge, jobs, store
from shturman.events import MESSAGE_LIVE
from shturman.outbox import scopes
from shturman.replies import workflow
from shturman.tg import normalize, sync
from shturman.tg.client import GuardedClient, RequestPolicy
from shturman.tg.live import LiveIngest

OWNER = 1000
HELPER = 5000
PERSON = 2001
GROUP = 4002
SELECTED_TOPIC = 5
OTHER_TOPIC = 9
MESSAGE_ID = 101
ANSWER = "Смета будет готова в пятницу."


def forum_update(case, now):
    """Only Telegram TL fields establish sender, mention and forum metadata."""
    mentioned = case in ("id_mention", "forwarded_mention")
    text = "😀 Помощник, когда будет смета?" if mentioned else "Когда будет смета?"
    topic = SELECTED_TOPIC if case == "selected_topic" else OTHER_TOPIC
    message = types.Message(
        id=MESSAGE_ID, peer_id=types.PeerChannel(GROUP), from_id=types.PeerUser(PERSON),
        date=now, message=text, out=False,
        entities=[types.MessageEntityMentionName(offset=3, length=8, user_id=HELPER)] if mentioned else [],
        reply_to=types.MessageReplyHeader(
            reply_to_msg_id=topic, reply_to_top_id=topic, forum_topic=True),
        fwd_from=types.MessageFwdHeader(date=now) if case == "forwarded_mention" else None,
    )
    return types.UpdateNewChannelMessage(message=message, pts=1, pts_count=1), topic


@pytest.mark.parametrize("case,allowed", [
    ("id_mention", True),
    ("selected_topic", True),
    ("unselected_topic", False),
    ("forwarded_mention", False),
])
async def test_forum_raw_update_reaches_only_eligible_bound_pending_draft(
    make_client, conn, config, monkeypatch, case, allowed,
):
    """Exercise real TL dispatch through Postgres to a model job and bound pending draft."""
    cfg = dataclasses.replace(config, sending=False, prepare_only=True)
    api, state = await make_client(
        "shturman.api_core", "shturman.outbox.service", "shturman.replies.service", cfg=cfg,
    )
    now = datetime.now(timezone.utc)
    forum = types.Channel(
        id=GROUP, title="Сметы", photo=types.ChatPhotoEmpty(), date=now,
        access_hash=GROUP * 7, megagroup=True, forum=True,
    )
    owner = types.User(id=OWNER, first_name="Владелец", bot=False, access_hash=OWNER * 7)
    helper = types.User(id=HELPER, first_name="Помощник", bot=False, is_self=True,
                        access_hash=HELPER * 7)
    person = types.User(id=PERSON, first_name="Иван", bot=False, access_hash=PERSON * 7)
    entities = [owner, helper, person, forum]

    await store.ensure_account(conn, OWNER, "Владелец", role="owner")
    account_id = await store.ensure_account(conn, HELPER, "Помощник", role="assistant")
    # This one-time setup represents independently verified owner decisions.
    with authority.owner_context(OWNER, chat_id=OWNER, action="test.owner.forum-setup"):
        await bridge.set_owner(conn, OWNER, OWNER)
        chat_id, enabled = await sync.enable_chat(
            conn, account_id, normalize.chat_record(forum, self_id=HELPER))
        assert enabled is True
        enabled_response = await api.put("/api/outbox/autoreply", json={
            "account_id": account_id, "enabled": True, "debounce_seconds": 0,
        })
        assert enabled_response.status_code == 200, enabled_response.text
        await scopes.put(conn, chat_id, topic_tg_id=SELECTED_TOPIC)
    assert authority.is_owner() is False

    telegram = GuardedClient(
        MemorySession(), 12345, "0123456789abcdef", policy=RequestPolicy("assistant", sending=False),
    )
    telegram._mb_entity_cache.set_self_user(HELPER, False, HELPER * 7)
    rpc_calls = []

    def refuse_network(*args, **kwargs):
        rpc_calls.append((args, kwargs))
        raise AssertionError("This vertical test must not make a Telegram RPC")

    monkeypatch.setattr(telegram._sender, "send", refuse_network)
    ingest = LiveIngest(
        pool=state.pool, events=state.events, account_id=account_id, self_id=HELPER,
        wake=asyncio.Event(),
    )
    await ingest.reload()
    ingest.register(telegram)
    captured = []

    async def observed(payload):
        captured.append(dict(payload))

    state.events.subscribe(MESSAGE_LIVE, observed)
    update, topic = forum_update(case, now)
    update._entities = {utils.get_peer_id(entity): entity for entity in entities}
    try:
        # The actual Telethon dispatcher invokes the registered Raw handler.
        await telegram._dispatch_update(update)
        # debounce_seconds=0 makes the listener await preparation directly, so drain
        # waits for the actual model job, rather than for a scheduled timer.
        await state.events.drain()

        message = await conn.fetchrow("SELECT * FROM messages WHERE chat_id=$1", chat_id)
        assert message is not None
        assert message["sources"] == ["session"]
        assert message["topic_tg_id"] == topic
        assert message["reply_to_tg_id"] == topic
        assert message["is_outgoing"] is False
        assert message["telegram_sender_bot"] is False
        assert message["telegram_via_bot"] is False
        assert message["is_forwarded"] is (case == "forwarded_mention")
        sender = await conn.fetchrow("SELECT class,tg_id FROM peers WHERE id=$1",
                                     message["sender_peer_id"])
        assert (sender["class"], sender["tg_id"]) == ("user", PERSON)
        metadata = json.loads(message["telegram_entities"])
        if case in ("id_mention", "forwarded_mention"):
            assert metadata == [{
                "type": "mention_name", "offset": 3, "length": 8,
                "text": "Помощник", "user_id": HELPER,
            }]
        else:
            assert metadata == []
        assert captured == [{
            "account_id": account_id, "chat_id": chat_id, "message_id": message["id"],
            "source": "session", "outgoing": False, "edited": False, "via_bot": False,
        }]

        tasks = await conn.fetch("SELECT * FROM reply_tasks")
        claimed = await jobs.claim(conn, [bridge.LLM_STRUCTURED], worker="forum-vertical", limit=10)
        if not allowed:
            # Rejected messages still belong in the selected archive, but cannot start work.
            assert tasks == [] and claimed == []
            assert await conn.fetchval("SELECT count(*) FROM outbox_drafts") == 0
            return

        assert len(tasks) == len(claimed) == 1
        task, job = tasks[0], claimed[0]
        assert task["status"] == "generating"
        assert (task["account_id"], task["chat_id"], task["target_tg_id"],
                task["trigger_message_id"], task["topic_tg_id"]) == (
            account_id, chat_id, GROUP, message["id"], topic,
        )
        assert task["prepare_only"] is True
        stored_job = await jobs.get(conn, job["id"])
        assert stored_job["context"]["task_id"] == task["id"]
        assert stored_job["handler"] == workflow.HANDLER
        assert job["payload"]["json_schema"]["properties"]["outcome"]["enum"]
        offered = json.loads(job["payload"]["input"])["offered_sources"]
        assert len(offered) == 1
        assert offered[0]["ref"]["message_id"] == message["id"]
        assert offered[0]["ref"]["topic_tg_id"] == topic
        # The only fake is the model answer at the existing structured-job boundary.
        completed = await bridge.deliver_result(conn, job["id"], {
            "parsed": {"outcome": "reply", "text": ANSWER,
                       "source_keys": [offered[0]["key"]]},
            "model": "test-forum-vertical",
        })
        assert completed is True
        await state.events.drain()

        task_after = await conn.fetchrow("SELECT * FROM reply_tasks WHERE id=$1", task["id"])
        draft = await conn.fetchrow("SELECT * FROM outbox_drafts WHERE task_id=$1", task["id"])
        assert task_after["status"] == "draft_ready"
        assert task_after["draft_id"] == draft["id"]
        assert draft["status"] == "pending" and draft["prepare_only"] is True
        assert draft["text"] == ANSWER and draft["channel"] == "session"
        assert (draft["account_id"], draft["chat_id"], draft["topic_tg_id"],
                draft["reply_to_tg_id"]) == (account_id, chat_id, topic, MESSAGE_ID)
        assert draft["task_policy_revision"] == task_after["policy_revision"]
        assert json.loads(draft["sources"]) == json.loads(task_after["source_refs"])
        assert await conn.fetchval("SELECT count(*) FROM outbox_drafts") == 1
        assert draft["parts_sent"] == 0 and draft["sent_tg_message_ids"] == []
        assert state.config.sending is False
    finally:
        telegram.remove_event_handler(ingest.on_update)
        assert rpc_calls == []
