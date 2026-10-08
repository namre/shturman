"""Owner commands use genuine private Telegram identity, not actor strings or HTTP claims."""
import json

import pytest

from shturman import authority
from shturman.executor import owner_chat
from exec_fakes import OWNER, OWNER_USER, STRANGER_USER, bind, rig


@pytest.mark.asyncio
async def test_command_without_server_owner_context_refused():
    with pytest.raises(PermissionError):
        await owner_chat.handle_command(None, None, '/policy {"daily_cap":1}', OWNER)


@pytest.mark.asyncio
async def test_owner_menu_and_explicit_policy(rig):
    await bind(rig)
    rig.tg.text('/menu')
    await rig.bot.poll_once()
    assert '/sourcepolicy' in rig.tg.sent()[-1]['text']
    assert rig.tg.sent()[-1]['reply_markup']['inline_keyboard']
    rig.tg.text('/policy {"daily_cap":17}')
    await rig.bot.poll_once()
    value = json.loads(await rig.conn.fetchval("SELECT value FROM settings WHERE key='outbox.policy'"))
    assert value['daily_cap'] == 17
    assert not await rig.conn.fetchval('SELECT EXISTS(SELECT 1 FROM pending_actions)')
    assert authority.is_owner() is False


@pytest.mark.asyncio
async def test_stranger_and_group_cannot_change_owner_policy(rig):
    await bind(rig)
    before = len(rig.tg.sent())
    rig.tg.text('/policy {"daily_cap":17}', user=STRANGER_USER)
    rig.tg.text('/policy {"daily_cap":18}', chat={'id': -55, 'type': 'group'})
    await rig.bot.poll_once()
    assert len(rig.tg.sent()) == before
    assert await rig.conn.fetchval("SELECT value FROM settings WHERE key='outbox.policy'") is None


@pytest.mark.asyncio
async def test_current_bound_chat_required_again(rig):
    await bind(rig)
    assert await owner_chat.handle_message(rig.conn, rig.state, '/menu', bot_id=rig.bot.bot_id,
                                          user_id=OWNER, chat_id=OWNER+1) is None


@pytest.mark.asyncio
async def test_menu_callback_other_chat_refused(rig):
    await bind(rig)
    before = await rig.conn.fetchval("SELECT count(*) FROM jobs")
    rig.tg.press('sh:oc:help', chat={'id': -55, 'type': 'group'})
    await rig.bot.poll_once()
    assert await rig.conn.fetchval("SELECT count(*) FROM jobs") == before
    assert rig.tg.calls('answerCallbackQuery')[-1]['text'] == 'Кнопка недоступна.'


@pytest.mark.asyncio
async def test_menu_callback_verified_owner(rig):
    await bind(rig)
    rig.tg.press('sh:oc:help')
    await rig.bot.poll_once()
    assert await rig.conn.fetchval("SELECT EXISTS(SELECT 1 FROM jobs WHERE kind='notify.owner')")
    assert not authority.is_owner()


@pytest.mark.asyncio
async def test_owner_prepare_switch_is_saved_without_enabling_sender(rig):
    await bind(rig)
    rig.tg.text('/prepare on')
    await rig.bot.poll_once()
    value = json.loads(await rig.conn.fetchval("SELECT value FROM settings WHERE key='outbox.prepare_only'"))
    assert value == {'enabled': True}
    assert rig.state.config.prepare_only is True
    assert rig.state.config.sending is rig.config.sending
    rig.tg.text('/prepare off')
    await rig.bot.poll_once()
    assert rig.state.config.prepare_only is False


@pytest.mark.asyncio
async def test_draft_callback_without_verified_context_is_refused_before_database():
    from shturman.outbox import drafts
    result = await drafts.on_button(None, 's:1:nonce', OWNER)
    assert result['answer'] == 'Кнопка недоступна.'
