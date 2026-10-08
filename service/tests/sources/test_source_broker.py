from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from shturman import authority, bridge, store
from shturman.records import ChatRecord, MessageRecord
from shturman.sources import broker
from shturman.sources.registry import Connector, SourceError


async def message(conn, chat_id, mid, text):
    return await conn.fetchval("""INSERT INTO messages(chat_id,tg_message_id,sent_at,text,sources)
        VALUES($1,$2,now(),$3,ARRAY['session']) RETURNING id""", chat_id, mid, text)


async def task_for(conn, chat_id, trigger):
    row = await conn.fetchrow("""INSERT INTO reply_tasks
        (account_id,chat_id,target_peer_id,target_tg_id,trigger_message_id,trigger_hash,policy_revision,nonce)
        SELECT c.account_id,c.id,c.peer_id,p.tg_id,$2,'trigger','policy','test'
        FROM chats c JOIN peers p ON p.id=c.peer_id WHERE c.id=$1 RETURNING *""", chat_id, trigger)
    return dict(row)


@pytest_asyncio.fixture
async def source_env(conn, own_bot):
    await bridge.set_owner(conn, 1000, 1000)
    account = await store.ensure_account(conn, 1000, "Тест", "owner")
    target, _ = await store.ensure_chat(conn, account, ChatRecord("user", 2001, "personal_chat", "Адресат"))
    other, _ = await store.ensure_chat(conn, account, ChatRecord("user", 2002, "personal_chat", "Источник"))
    tid = await message(conn, target, 1, "Прислать смету")
    oid = await message(conn, other, 1, "Смета содержит 100 рублей")
    task = await task_for(conn, target, tid)
    return SimpleNamespace(conn=conn, state=SimpleNamespace(extras={"source_registry": {}}),
                           target=target, other=other, target_message=tid, other_message=oid, task=task)


async def test_same_chat_receipts_detect_edits_and_hidden_messages(source_env):
    e = source_env
    spec = {"kind": "chat", "source_id": str(e.target), "query": ""}
    result = await broker.read_for_task(e.conn, e.state, e.task, spec)
    assert result["status"] == "ok" and result["source_refs"][0]["receipt_id"]
    assert await broker.validate_refs(e.conn, e.state, e.task, result["source_refs"])
    assert await broker.disclosure_allowed(e.conn, e.task, result["source_refs"])
    forged = [{**result["source_refs"][0], "revision": "forged"}]
    assert not await broker.validate_refs(e.conn, e.state, e.task, forged)
    await e.conn.execute("UPDATE messages SET text='Новая смета' WHERE id=$1", e.target_message)
    assert not await broker.validate_refs(e.conn, e.state, e.task, result["source_refs"])
    await e.conn.execute("UPDATE messages SET agent_visible=false WHERE id=$1", e.target_message)
    assert not await broker.validate_refs(e.conn, e.state, e.task, result["source_refs"])
    assert (await broker.read_for_task(e.conn, e.state, e.task, spec))["snippets"] == []


async def test_extra_source_requires_independent_owner_read_and_disclosure_grants(source_env):
    e = source_env
    spec = {"kind": "chat", "source_id": str(e.other), "query": "смета", "reason": "Проверить сумму"}
    assert (await broker.read_for_task(e.conn, e.state, e.task, spec))["status"] == "access_required"
    with pytest.raises(SourceError, match="authority"):
        await broker.grant_for_task(e.conn, e.task, spec, owner_id=1000)
    with authority.owner_context(9999):
        with pytest.raises(SourceError, match="authority"):
            await broker.grant_for_task(e.conn, e.task, spec, owner_id=1000)
    with authority.owner_context(1000):
        read = await broker.grant_for_task(e.conn, e.task, spec, owner_id=1000)
    data = await broker.read_for_task(e.conn, e.state, e.task, spec)
    assert data["status"] == "ok" and "100" in data["snippets"][0]["text"]
    assert await broker.validate_refs(e.conn, e.state, e.task, data["source_refs"])
    assert not await broker.disclosure_allowed(e.conn, e.task, data["source_refs"])
    with authority.owner_context(1000):
        await broker.grant_for_task(e.conn, e.task, spec, owner_id=1000, mode="disclose")
    assert await broker.disclosure_allowed(e.conn, e.task, data["source_refs"])
    assert (await broker.read_for_task(e.conn, e.state, e.task, {**spec, "query": "рублей"}))["status"] == "access_required"
    with authority.owner_context(1000):
        await broker.revoke_grant(e.conn, read["id"], owner_id=1000)
    assert not await broker.validate_refs(e.conn, e.state, e.task, data["source_refs"])
    assert not await broker.disclosure_allowed(e.conn, e.task, data["source_refs"])


async def test_bounded_persistent_policy_reuses_exact_scope_but_not_a_different_chat(source_env):
    e = source_env
    spec = {"kind": "chat", "source_id": str(e.other), "query": "смета", "limit": 2}
    with authority.owner_context(1000):
        grant = await broker.grant_for_task(e.conn, e.task, spec, owner_id=1000, persistent=True,
                                           expires_at="2099-01-01T00:00:00Z")
    assert (datetime.fromisoformat(grant["expires_at"]) - datetime.now(timezone.utc)).days <= 30
    next_task = await task_for(e.conn, e.target, await message(e.conn, e.target, 2, "А сейчас?"))
    assert (await broker.read_for_task(e.conn, e.state, next_task,
                                      {**spec, "reason": "Повторно проверить сумму"}))["status"] == "ok"
    # The target in a caller-supplied task cannot replace the persisted target.
    with pytest.raises(SourceError, match="invalid_task"):
        await broker.read_for_task(e.conn, e.state, {**next_task, "chat_id": e.other}, spec)
    changed = {**spec, "limit": 3}
    assert (await broker.read_for_task(e.conn, e.state, next_task, changed))["status"] == "access_required"


async def test_global_memory_read_omits_control_chats_excluded_and_hidden(source_env):
    e = source_env
    hidden = await message(e.conn, e.other, 2, "Смета скрытая")
    await e.conn.execute("UPDATE messages SET agent_visible=false WHERE id=$1", hidden)
    control, _ = await store.ensure_chat(e.conn, e.task["account_id"], ChatRecord("user", 777000, "personal_chat", "Служебный"))
    await message(e.conn, control, 1, "Смета секретная")
    spec = {"kind": "chat", "query": "смета"}
    with authority.owner_context(1000):
        await broker.grant_for_task(e.conn, e.task, spec, owner_id=1000)
    result = await broker.read_for_task(e.conn, e.state, e.task, spec)
    refs = result["source_refs"]
    assert {r["message_id"] for r in refs} == {e.target_message, e.other_message}
    await e.conn.execute("UPDATE chats SET excluded=true WHERE id=$1", e.other)
    assert not await broker.validate_refs(e.conn, e.state, e.task, refs)


async def test_external_transport_not_called_before_grant_and_revocation_invalidates_receipt(source_env, monkeypatch):
    e = source_env
    e.state.extras["source_registry"] = {"docs": Connector("docs", "Документы", "https://docs.example.org/mcp",
                                                           search_tool="search", read_tool="read")}
    calls = []
    async def search(connector, spec):
        calls.append("search")
        return [{"resource_id": "document-1", "text": "Фрагмент документа", "remote_revision": "1", "date": None}]
    async def read(connector, rid):
        calls.append("read")
        return {"resource_id": rid, "text": "Фрагмент документа", "remote_revision": "1"}
    monkeypatch.setattr(broker.mcp_client, "search", search)
    monkeypatch.setattr(broker.mcp_client, "read", read)
    spec = {"kind": "external", "source_id": "docs", "query": "договор"}
    assert (await broker.read_for_task(e.conn, e.state, e.task, spec))["status"] == "access_required"
    assert not calls
    with authority.owner_context(1000):
        grant = await broker.grant_for_task(e.conn, e.task, spec, owner_id=1000)
    result = await broker.read_for_task(e.conn, e.state, e.task, spec)
    assert await broker.validate_refs(e.conn, e.state, e.task, result["source_refs"])
    assert calls == ["search", "read"]
    with authority.owner_context(1000):
        await broker.revoke_grant(e.conn, grant["id"], owner_id=1000)
    assert not await broker.validate_refs(e.conn, e.state, e.task, result["source_refs"])
    assert calls == ["search", "read"]


async def test_memory_page_revokes_all_derived_blocks_when_original_source_is_hidden(source_env):
    e = source_env
    person = await e.conn.fetchval("INSERT INTO people(display_name) VALUES('Автор') RETURNING id")
    entity = f"person:{person}"
    page = await e.conn.fetchval("""INSERT INTO pages(entity_type,entity_id,person_id,path,title,dirty)
        VALUES('person',$1,$2,'people/test.md','Автор',false) RETURNING id""", entity, person)
    await e.conn.execute("INSERT INTO page_blocks(page_id,block,text) VALUES($1,'summary','Смета согласована')", page)
    entry = await e.conn.fetchval("""INSERT INTO page_entries(page_id,block,key,n_sources,text)
        VALUES($1,'summary','1',1,'Смета согласована') RETURNING id""", page)
    await e.conn.execute("INSERT INTO page_entry_sources(entry_id,message_id) VALUES($1,$2)", entry, e.other_message)
    spec = {"kind": "memory", "source_id": entity, "query": ""}
    assert (await broker.read_for_task(e.conn, e.state, e.task, spec))["status"] == "access_required"
    with authority.owner_context(1000):
        await broker.grant_for_task(e.conn, e.task, spec, owner_id=1000)
    result = await broker.read_for_task(e.conn, e.state, e.task, spec)
    assert "Смета" in result["snippets"][0]["text"]
    assert await broker.validate_refs(e.conn, e.state, e.task, result["source_refs"])
    await e.conn.execute("UPDATE messages SET agent_visible=false WHERE id=$1", e.other_message)
    assert not await broker.validate_refs(e.conn, e.state, e.task, result["source_refs"])
    assert (await broker.read_for_task(e.conn, e.state, e.task, spec))["snippets"] == []
