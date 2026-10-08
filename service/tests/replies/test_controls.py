from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from shturman import authority, bridge
from shturman.replies import owner, workflow

@pytest.mark.asyncio
async def test_model_or_api_user_id_does_not_establish_owner(monkeypatch):
    monkeypatch.setattr(bridge,'owns_bot',lambda:True)
    monkeypatch.setattr(bridge,'get_owner',AsyncMock(return_value={'user_id':1,'chat_id':1}))
    assert not await owner.authorized(None,1)
    with authority.owner_context(1,chat_id=2): assert not await owner.authorized(None,1)
    with authority.owner_context(1,chat_id=1): assert await owner.authorized(None,1)

@pytest.mark.asyncio
async def test_legacy_bot_does_not_grant(monkeypatch):
    monkeypatch.setattr(bridge,'owns_bot',lambda:False)
    monkeypatch.setattr(bridge,'get_owner',AsyncMock(return_value={'user_id':1,'chat_id':1}))
    with authority.owner_context(1,chat_id=1): assert not await owner.authorized(None,1)

@pytest.mark.asyncio
@pytest.mark.parametrize('changed', ['prepare_only','sources','revision','target','requires_approval','revoked'])
async def test_presend_rechecks_immutable_bindings(monkeypatch, changed):
    task={'id':1,'status':'draft_ready','prepare_only':False,'chat_id':3,'account_id':2,
          'policy_revision':'r','source_refs':[{'message_id':9}],'requires_approval':False}
    row={'task_id':1,'chat_id':3,'account_id':2,'task_policy_revision':'r','sources':[{'message_id':9}],'origin':'autoreply'}
    if changed=='prepare_only': task['prepare_only']=True
    if changed=='sources': row['sources']=[]
    if changed=='revision': row['task_policy_revision']=None
    if changed=='target': row['chat_id']=4
    if changed=='requires_approval': task['requires_approval']=True
    monkeypatch.setattr(workflow,'get',AsyncMock(return_value=task))
    monkeypatch.setattr(workflow,'valid',AsyncMock(return_value=changed!='revoked'))
    from shturman.sources import broker
    monkeypatch.setattr(broker,'disclosure_allowed',AsyncMock(return_value=True))
    assert not (await workflow.presend(None,None,row)).ok

@pytest.mark.asyncio
async def test_exact_reviewed_extra_disclosure_keeps_pending_path(monkeypatch):
    task={'status':'draft_ready','prepare_only':False,'chat_id':3,'account_id':2,
          'policy_revision':'r','source_refs':[{'receipt_id':1}],'requires_approval':True}
    row={'task_id':1,'chat_id':3,'account_id':2,'task_policy_revision':'r',
         'sources':[{'receipt_id':1}],'origin':'agent'}
    monkeypatch.setattr(workflow,'get',AsyncMock(return_value=task))
    monkeypatch.setattr(workflow,'valid',AsyncMock(return_value=True))
    assert (await workflow.presend(None,None,row)).ok

@pytest.mark.asyncio
async def test_grant_resume_requires_independent_owner(monkeypatch):
    monkeypatch.setattr(bridge,'owns_bot',lambda:True)
    conn=SimpleNamespace(fetchrow=AsyncMock())
    assert not await workflow.owner_granted(conn,None,1)
    conn.fetchrow.assert_not_called()

@pytest.mark.asyncio
@pytest.mark.parametrize('nonce,expired', [('old',False),('valid',True)])
async def test_stale_owner_buttons_are_not_decisions(monkeypatch,nonce,expired):
    monkeypatch.setattr(owner,'authorized',AsyncMock(return_value=True))
    monkeypatch.setattr(workflow,'get',AsyncMock(return_value={'status':'waiting_source','nonce':'valid',
              'decision_expires_at':'synthetic'}))
    conn=SimpleNamespace(fetchval=AsyncMock(return_value=expired),execute=AsyncMock())
    assert await owner.on_button(conn,f'y:1:{nonce}',1)==owner.REFUSED
    conn.execute.assert_not_called()

@pytest.mark.asyncio
async def test_revoked_disclosure_grant_blocks_auto_even_if_read_is_valid(monkeypatch):
    from shturman.sources import broker
    task={'id':1,'status':'draft_ready','prepare_only':False,'chat_id':3,'account_id':2,
          'policy_revision':'r','source_refs':[{'receipt_id':42}],'requires_approval':False}
    row={'task_id':1,'chat_id':3,'account_id':2,'task_policy_revision':'r',
         'sources':[{'receipt_id':42}],'origin':'autoreply'}
    monkeypatch.setattr(workflow,'get',AsyncMock(return_value=task))
    monkeypatch.setattr(workflow,'valid',AsyncMock(return_value=True))
    monkeypatch.setattr(broker,'disclosure_allowed',AsyncMock(return_value=False))
    assert not (await workflow.presend(None,None,row)).ok

@pytest.mark.asyncio
async def test_allowed_automatic_presend_keeps_preapproved_path(monkeypatch):
    from shturman.sources import broker
    task={'id':1,'status':'draft_ready','prepare_only':False,'chat_id':3,'account_id':2,
          'policy_revision':'r','source_refs':[{'message_id':9}],'requires_approval':False}
    row={'task_id':1,'chat_id':3,'account_id':2,'task_policy_revision':'r',
         'sources':task['source_refs'],'origin':'autoreply'}
    monkeypatch.setattr(workflow,'get',AsyncMock(return_value=task))
    monkeypatch.setattr(workflow,'valid',AsyncMock(return_value=True))
    disclosure=AsyncMock(return_value=True)
    monkeypatch.setattr(broker,'disclosure_allowed',disclosure)
    assert (await workflow.presend(None,None,row)).ok
    disclosure.assert_awaited_once_with(None,task,task['source_refs'])
