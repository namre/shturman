"""Только синтетические данные: служебный bot ID нельзя вернуть в архив другим входом."""
from datetime import datetime, timezone
from dataclasses import replace
import json

import pytest

from shturman import archive, control_peers, jobs, store
from shturman.processing import pages_build
from shturman.records import ChatRecord, MessageRecord

BOT = 4321001
OWNER = 1000


def message(text="Код 12345678", **kw):
    return MessageRecord(tg_message_id=1, sent_at=datetime.now(timezone.utc), kind="message",
        sender_class="user", sender_tg_id=BOT, sender_name="Служебный бот", text=text,
        entities=None, reply_to_tg_id=None, forwarded_from=None, edited_at=None,
        media_type=None, media_path=None, service_action=None, **kw)


def test_display_name_or_username_is_not_an_identity():
    assert not store.is_blocked_peer("user", BOT, "BotFather")
    assert store.is_blocked_peer("user", 777000, "renamed")
    assert not store.is_blocked_peer("channel", 777000)


async def scene(conn):
    a = await store.ensure_account(conn, OWNER, "Тестовый владелец")
    c, _ = await store.ensure_chat(conn, a, ChatRecord("user", BOT, "bot_chat", "Служебный бот"))
    out = await store.upsert_messages(conn, [(c, message())], source="session", owner_tg_id=OWNER)
    return a, c, out.new_ids[0]


async def test_registration_purges_messages_versions_and_blocks_all_future_ingest(conn):
    a, c, m = await scene(conn)
    await conn.execute("INSERT INTO message_versions (message_id,text) VALUES ($1,'Старый код')", m)
    await control_peers.register(conn, BOT)
    assert await archive.get_message(conn, m) is None
    assert await archive.chat_by_id(conn, c) is None
    assert await conn.fetchval("SELECT count(*) FROM message_versions") == 0
    # Прямой обход флага исключения не помогает даже при ошибке обработчика.
    await conn.execute("UPDATE chats SET excluded = false WHERE id = $1", c)
    assert await conn.fetchval("SELECT excluded FROM chats WHERE id = $1", c)
    for source in ("session", "import", "business"):
        out = await store.upsert_messages(conn, [(c, message())], source=source, owner_tg_id=OWNER)
        assert out.new == out.known == 0
    # Замена бота и другой аккаунт не открывают прежний ID; совпадение имени не запрещает другой ID.
    await control_peers.register(conn, BOT + 1)
    another = await store.ensure_account(conn, OWNER + 1, "Помощник", "assistant")
    _, excluded = await store.ensure_chat(conn, another, ChatRecord("user", BOT, "bot_chat", "Новое имя"))
    assert excluded
    _, excluded = await store.ensure_chat(conn, a, ChatRecord("user", BOT + 2, "bot_chat", "Служебный бот"))
    assert not excluded


async def test_derived_page_and_queued_model_payload_are_inaccessible(conn):
    _, c, m = await scene(conn)
    person = await conn.fetchval("INSERT INTO people(display_name) VALUES('Человек') RETURNING id")
    page = await conn.fetchval(
        """INSERT INTO pages(entity_type,entity_id,person_id,path,title)
           VALUES('person',$1,$2,'people/test.md','Человек') RETURNING id""", f"person:{person}", person)
    entry = await conn.fetchval(
        """INSERT INTO page_entries(page_id,block,key,text,n_sources)
           VALUES($1,'summary','0','Код 12345678',1) RETURNING id""", page)
    await conn.execute("INSERT INTO page_entry_sources VALUES($1,$2)", entry, m)
    await conn.execute("INSERT INTO page_blocks VALUES($1,'summary','Код 12345678')", page)
    job = await jobs.enqueue(conn, kind="llm.text", payload={"messages": [{"role":"user","content":"Код 12345678"}]},
                             context={"chat_id": c})
    await control_peers.register(conn, BOT)
    assert await pages_build.get_page(conn, person) is None
    assert await pages_build.search_pages(conn, "Код") == []
    assert await pages_build.list_pages(conn) == []
    assert await conn.fetchval("SELECT security_quarantined FROM pages WHERE id=$1", page)
    assert await jobs.claim(conn, ["llm.text"], worker="untrusted") == []
    row = await conn.fetchrow("SELECT payload,result,context FROM jobs WHERE id=$1", job)
    assert row["payload"] == row["context"] == "{}" and row["result"] is None


def test_unverified_transport_cannot_create_telegram_addressing_proof():
    record = message(telegram_entities=[], topic_tg_id=44, is_forwarded=False,
                     telegram_via_bot=False, telegram_sender_bot=False)
    assert store._row(1, record, OWNER, False, "import")[-10:-5] == (None,) * 5
    assert store._row(1, record, OWNER, False, "business")[-10:-5] == (None,) * 5
    assert store._row(1, record, OWNER, False, "session")[-10:-5] == ("[]", 44, False, False, False)


@pytest.mark.parametrize("source_kind", ["chat", "memory", "trigger"])
async def test_registration_scrubs_all_reply_generations_and_source_derived_drafts(conn, source_kind):
    a, protected_chat, protected_message = await scene(conn)
    c, _ = await store.ensure_chat(conn, a, ChatRecord("user", BOT + 10, "personal_chat", "Получатель"))
    record = replace(message("Обычный вопрос"), sender_tg_id=BOT + 10, sender_name="Получатель")
    ordinary = (await store.upsert_messages(conn, [(c, record)], source="session", owner_tg_id=OWNER)).new_ids[0]
    source_ref = {"kind": "chat", "source_id": str(protected_chat), "message_id": protected_message}
    if source_kind == "memory":
        person = await conn.fetchval("INSERT INTO people(display_name) VALUES('Человек') RETURNING id")
        entity = f"person:{person}"
        page = await conn.fetchval(
            """INSERT INTO pages(entity_type,entity_id,person_id,path,title)
               VALUES('person',$1,$2,'people/reply-source.md','Человек') RETURNING id""", entity, person)
        entry = await conn.fetchval(
            """INSERT INTO page_entries(page_id,block,key,text,n_sources)
               VALUES($1,'summary','0','Код 12345678',1) RETURNING id""", page)
        await conn.execute("INSERT INTO page_entry_sources VALUES($1,$2)", entry, protected_message)
        source_ref = {"kind": "memory", "source_id": entity, "block": "summary"}
    trigger = protected_message if source_kind == "trigger" else ordinary
    if source_kind == "trigger":
        c = protected_chat
    target_tg_id = BOT if source_kind == "trigger" else BOT + 10
    peer = await conn.fetchval("SELECT peer_id FROM chats WHERE id=$1", c)
    task = await conn.fetchval(
        """INSERT INTO reply_tasks(account_id,chat_id,target_peer_id,target_tg_id,trigger_message_id,
               trigger_hash,policy_revision,nonce,input_messages,source_refs,status)
           VALUES($1,$2,$3,$4,$5,'hash','policy','nonce',$6::jsonb,$7::jsonb,'generating') RETURNING id""",
        a, c, peer, target_tg_id, trigger,
        json.dumps([{"role": "user", "content": "Код 12345678"}]), json.dumps([source_ref]))
    draft = await conn.fetchval(
        """INSERT INTO outbox_drafts(account_id,chat_id,channel,text,text_hash,origin,nonce,status,
               expires_at,task_id,task_policy_revision,sources)
           VALUES($1,$2,'session','Код 12345678','hash','agent','nonce','pending',
                  now()+interval '1 hour',$3,'policy',$4::jsonb) RETURNING id""",
        a, c, task, json.dumps([source_ref]))
    affected_jobs = []
    for generation in (1, 2):
        affected_jobs.append(await jobs.enqueue(conn, kind="llm.text", payload={"text": "Код 12345678"},
            context={"task_id": task, "generation": generation}))
    await conn.execute("UPDATE reply_tasks SET job_id=$2 WHERE id=$1", task, affected_jobs[-1])
    affected_jobs.append(await jobs.enqueue(conn, kind="llm.text", payload={"text": "Код 12345678"},
                                           context={"draft_id": draft}))
    affected_jobs.append(await jobs.enqueue(conn, kind="llm.text", payload={"text": "Код 12345678"},
                                           dedup_key=f"reply-wait:{task}:2"))
    ordinary_job = await jobs.enqueue(conn, kind="llm.text", payload={"text": "Обычный вопрос"})

    await control_peers.register(conn, BOT)

    assert not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM outbox_drafts WHERE id=$1)", draft)
    for job in affected_jobs:
        row = await conn.fetchrow("SELECT status,payload,result,context FROM jobs WHERE id=$1", job)
        assert row["status"] == "failed"
        assert row["payload"] == row["context"] == "{}" and row["result"] is None
    row = await conn.fetchrow("SELECT * FROM reply_tasks WHERE id=$1", task)
    if source_kind == "trigger":
        assert row is None
    else:
        assert row["status"] == "cancelled"
        assert row["input_messages"] == row["source_refs"] == "[]"
        assert row["source_request"] is row["owner_question"] is row["owner_answer"] is None
    assert await conn.fetchval("SELECT status FROM jobs WHERE id=$1", ordinary_job) == "queued"
