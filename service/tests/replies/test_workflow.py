"""Transition tests invoke real handlers with isolated service dependencies, no Telegram."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from shturman.outbox import autoreply, drafts, policy, runtime
from shturman.replies import workflow
from shturman.sources import broker

@pytest.fixture
def transition(monkeypatch):
    task={'id':7,'status':'generating','job_id':11,'generation':1,'source_refs':[],
          'input_messages':[{'role':'system','content':'rules'},{'role':'user','content':'question'}],
          'chat_id':3,'trigger_message_id':9,'prepare_only':False,'requires_approval':False}
    conn=SimpleNamespace(execute=AsyncMock())
    monkeypatch.setattr(workflow,'state_current',lambda:SimpleNamespace(config=SimpleNamespace()))
    monkeypatch.setattr(workflow,'get',AsyncMock(return_value=task))
    monkeypatch.setattr(workflow,'valid',AsyncMock(return_value=True))
    monkeypatch.setattr(autoreply,'load',AsyncMock(return_value={'max_reply_chars':3000}))
    monkeypatch.setattr(policy,'target',AsyncMock(return_value=object()))
    monkeypatch.setattr(policy,'pick_channel',lambda tgt,_:('session',policy.ALLOW))
    monkeypatch.setattr(runtime,'current',lambda:None)
    return conn,task,{'id':11,'context':{'task_id':7,'generation':1}}

@pytest.mark.asyncio
@pytest.mark.parametrize('manual,preparing',[(False,False),(True,False),(False,True)])
async def test_reply_selects_auto_only_inside_existing_disclosure(transition,monkeypatch,manual,preparing):
    conn,task,job=transition
    task.update(requires_approval=manual,prepare_only=preparing)
    auto=AsyncMock(return_value=20);pending=AsyncMock(return_value={'draft_id':21})
    monkeypatch.setattr(drafts,'create_autoreply',auto)
    monkeypatch.setattr(drafts,'create',pending)
    await workflow.on_result(conn,job,{'parsed':{'outcome':'reply','text':'Answer'}})
    assert auto.await_count == (0 if manual or preparing else 1)
    assert pending.await_count == (1 if manual or preparing else 0)
    selected=pending if manual or preparing else auto
    assert selected.call_args.kwargs['task_id']==7
    assert selected.call_args.kwargs['sources']==[]
    assert any("status='draft_ready'" in c.args[0] for c in conn.execute.call_args_list)

@pytest.mark.asyncio
async def test_missing_source_waits_without_grant_or_draft(transition,monkeypatch):
    conn,task,job=transition
    request={'kind':'chat','source_id':'4','reason':'Check amount'}
    monkeypatch.setattr(broker,'read_for_task',AsyncMock(return_value={'status':'access_required','request':request}))
    grant=AsyncMock();monkeypatch.setattr(broker,'grant_for_task',grant)
    notify=AsyncMock();monkeypatch.setattr(workflow,'notify_waiting',notify)
    await workflow.on_result(conn,job,{'parsed':{'outcome':'need_source','request':request}})
    grant.assert_not_called()
    assert notify.call_args.args[2]=='source'
    assert any('waiting_source' in c.args for c in conn.execute.call_args_list)

@pytest.mark.asyncio
async def test_stale_generation_cannot_modify_task(transition,monkeypatch):
    conn,task,job=transition;job['context']['generation']=0
    create=AsyncMock();monkeypatch.setattr(drafts,'create_autoreply',create)
    await workflow.on_result(conn,job,{'parsed':{'outcome':'reply','text':'Answer'}})
    create.assert_not_called()
    assert len(conn.execute.call_args_list)==1 # scrub only the consumed stale job

@pytest.mark.asyncio
async def test_read_without_disclosure_continues_same_task_pending(transition,monkeypatch):
    conn,task,_=transition
    monkeypatch.setattr(broker,'disclosure_allowed',AsyncMock(return_value=False))
    queue=AsyncMock();monkeypatch.setattr(workflow,'queue',queue)
    await workflow.add_source(conn,None,task,{'source_refs':[{'receipt_id':42}],
                         'snippets':[{'text':'100','source_ref':{'receipt_id':42}}]})
    assert task['id']==7 and task['requires_approval']
    assert task['source_refs']==[{'receipt_id':42}]
    queue.assert_awaited_once_with(conn,task)
