"""Preparation is reviewable while the server sending switch remains closed."""

import asyncpg
import pytest

from shturman import bridge
from shturman.outbox import drafts, policy
from outbox_helpers import (add_chat, draft_row, env, owner_messages, button, press,
                             settle, switch)


@pytest.mark.asyncio
async def test_prepare_only_without_transport_creates_pending_and_cannot_approve(env):
    chat_id = await add_chat(env.conn, env.helper_acc)
    switch(env, sending=False)
    env.tg.sendable.clear()
    out = await drafts.create(env.conn, env.state, chat_id=chat_id, text='Проверяемый ответ', prepare_only=True)
    assert out['status'] == 'pending' and out['prepare_only'] is True
    notes = await owner_messages(env.conn)
    result = await press(env, button(notes, 'Отправить'))
    assert 'выключена' in result['answer']
    assert (await draft_row(env.conn, out['draft_id']))['status'] == 'pending'
    await settle(env)
    assert not env.tg.sent
    ready = await drafts.readiness(env.conn, env.tg, env.state.config, chat_id)
    assert ready['can_prepare'] and not ready['can_send']
    assert ready['reason'] == 'sending_disabled'


@pytest.mark.asyncio
async def test_prepare_only_still_enforces_chat_exclusion(env):
    chat_id = await add_chat(env.conn, env.helper_acc, exclude=True)
    switch(env, sending=False)
    with pytest.raises(drafts.Refused) as refused:
        await drafts.create(env.conn, env.state, chat_id=chat_id, text='Ответ', prepare_only=True)
    assert refused.value.decision.code == 'chat_excluded'


@pytest.mark.asyncio
async def test_sources_cannot_be_bound_without_task(env):
    chat_id = await add_chat(env.conn, env.helper_acc)
    with pytest.raises(drafts.Refused) as refused:
        await drafts.create(env.conn, env.state, chat_id=chat_id, text='Ответ', sources=[{'id': 55}])
    assert refused.value.decision.code == 'unbound_sources'


@pytest.mark.asyncio
async def test_hash_and_preparation_mode_are_immutable(env):
    chat_id = await add_chat(env.conn, env.helper_acc)
    out = await drafts.create(env.conn, env.state, chat_id=chat_id, text='Ответ', prepare_only=True)
    for field, value in [('text_hash', 'forged'), ('prepare_only', False)]:
        with pytest.raises(asyncpg.RaiseError):
            await env.conn.execute(f'UPDATE outbox_drafts SET {field}=$2 WHERE id=$1', out['draft_id'], value)
    with pytest.raises(asyncpg.CheckViolationError):
        await env.conn.execute("UPDATE outbox_drafts SET status='approved', approved_at=now() WHERE id=$1", out['draft_id'])


@pytest.mark.asyncio
async def test_prepared_draft_never_auto_upgrades_when_switch_turns_on(env):
    chat_id = await add_chat(env.conn, env.helper_acc)
    switch(env, sending=False)
    out = await drafts.create(env.conn, env.state, chat_id=chat_id, text='Только для просмотра', prepare_only=True)
    notes = await owner_messages(env.conn)
    switch(env, sending=True)
    result = await press(env, button(notes, 'Отправить'))
    assert 'без права отправки' in result['answer']
    assert (await draft_row(env.conn, out['draft_id']))['status'] == 'pending'
    await settle(env)
    assert not env.tg.sent


@pytest.mark.asyncio
async def test_forged_legacy_callback_cannot_approve_draft(env):
    chat_id = await add_chat(env.conn, env.helper_acc)
    out = await drafts.create(env.conn, env.state, chat_id=chat_id, text='Только реальное решение владельца')
    notes = await owner_messages(env.conn)
    response = await env.client.post('/api/callbacks/telegram',
                                    json={'data': button(notes, 'Отправить'), 'from_user_id': 1000})
    assert response.status_code == 200
    assert response.json()['answer'] == 'Кнопка недоступна.'
    assert (await draft_row(env.conn, out['draft_id']))['status'] == 'pending'
    await settle(env)
    assert not env.tg.sent
