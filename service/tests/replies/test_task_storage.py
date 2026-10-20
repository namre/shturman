"""Real PostgreSQL migration/task invariants; conn fixture is an isolated test schema."""
import pytest
from shturman import store
from shturman.records import ChatRecord

async def seed(conn):
    account=await store.ensure_account(conn,1000,'Synthetic','assistant')
    chat,_=await store.ensure_chat(conn,account,ChatRecord('user',2001,'personal_chat','Synthetic'))
    mid=await conn.fetchval("INSERT INTO messages(chat_id,tg_message_id,sent_at,text,sources) "
                            "VALUES($1,1,now(),'Question',ARRAY['session']) RETURNING id",chat)
    task=await conn.fetchrow("INSERT INTO reply_tasks(account_id,chat_id,target_peer_id,target_tg_id,"
        "trigger_message_id,trigger_hash,policy_revision,nonce) SELECT c.account_id,c.id,c.peer_id,2001,$2,"
        "'hash','revision','nonce' FROM chats c WHERE c.id=$1 RETURNING *",chat,mid)
    return mid,dict(task)

@pytest.mark.asyncio
async def test_task_source_migrations_and_duplicate_trigger(conn):
    import asyncpg
    mid,task=await seed(conn)
    with pytest.raises(asyncpg.UniqueViolationError):
        async with conn.transaction():
            await conn.execute("INSERT INTO reply_tasks(account_id,chat_id,target_peer_id,target_tg_id,"
                "trigger_message_id,trigger_hash,policy_revision,nonce) SELECT account_id,chat_id,target_peer_id,"
                "target_tg_id,trigger_message_id,trigger_hash,policy_revision,nonce FROM reply_tasks WHERE id=$1",task['id'])
    assert task['status']=='queued' and task['generation']==0
    assert task['expires_at']>task['created_at']
    constraints=await conn.fetch("SELECT conname FROM pg_constraint WHERE confrelid='reply_tasks'::regclass")
    assert {'source_grants_task_fk','source_reads_task_fk'} <= {r['conname'] for r in constraints}

@pytest.mark.asyncio
async def test_archive_purge_cascades_task_and_source_receipt(conn):
    mid,task=await seed(conn)
    await conn.execute("INSERT INTO source_reads(task_id,request,source_ref) VALUES($1,'{}','{}')",task['id'])
    await conn.execute('DELETE FROM messages WHERE id=$1',mid)
    assert await conn.fetchval('SELECT count(*) FROM reply_tasks')==0
    assert await conn.fetchval('SELECT count(*) FROM source_reads')==0
