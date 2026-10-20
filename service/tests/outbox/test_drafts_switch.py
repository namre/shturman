"""Черновик ассистента и выключатель отправки после Р-105: режима «только для просмотра» больше нет."""

import asyncpg
import pytest

from shturman.outbox import drafts
from outbox_helpers import (add_chat, button, draft_row, env, owner_messages, press,  # noqa: F401
                            settle, switch)


@pytest.mark.asyncio
async def test_without_sending_agent_draft_is_refused_and_readiness_says_why(env):
    chat_id = await add_chat(env.conn, env.helper_acc)
    switch(env, sending=False)
    with pytest.raises(drafts.Refused) as refused:
        await drafts.create(env.conn, env.state, chat_id=chat_id, text='Проверяемый ответ')
    assert refused.value.decision.code == 'sending_disabled'
    ready = await drafts.readiness(env.conn, env.tg, env.state.config, chat_id)
    assert ready['can_prepare'] and not ready['can_send'] and ready['reason'] == 'sending_disabled'


@pytest.mark.asyncio
async def test_create_takes_no_preparation_flag_and_is_always_the_agent(env):
    chat_id = await add_chat(env.conn, env.helper_acc)
    with pytest.raises(TypeError):
        await drafts.create(env.conn, env.state, chat_id=chat_id, text='Ответ', prepare_only=True)
    out = await drafts.create(env.conn, env.state, chat_id=chat_id, text='Ответ')
    assert (await draft_row(env.conn, out['draft_id']))['origin'] == 'agent'
    assert out['prepare_only'] is False        # прежний ключ ответа — ещё один выпуск, всегда false


@pytest.mark.asyncio
async def test_sources_cannot_be_bound_without_task(env):
    chat_id = await add_chat(env.conn, env.helper_acc)
    with pytest.raises(drafts.Refused) as refused:
        await drafts.create(env.conn, env.state, chat_id=chat_id, text='Ответ', sources=[{'id': 55}])
    assert refused.value.decision.code == 'unbound_sources'


@pytest.mark.asyncio
async def test_hash_is_immutable_and_the_old_column_is_gone(env):
    chat_id = await add_chat(env.conn, env.helper_acc)
    out = await drafts.create(env.conn, env.state, chat_id=chat_id, text='Ответ')
    with pytest.raises(asyncpg.RaiseError):
        await env.conn.execute('UPDATE outbox_drafts SET text_hash=$2 WHERE id=$1', out['draft_id'], 'forged')
    columns = {r['column_name'] for r in await env.conn.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name IN ('outbox_drafts', 'reply_tasks')")}
    assert 'prepare_only' not in columns


@pytest.mark.asyncio
async def test_old_view_only_card_answers_politely_and_sends_nothing(env):
    """Карточка черновика «только для просмотра» из прежней версии: миграция закрыла его (prepare_removed),
    обе прежние кнопки отвечают словами и убирают кнопки — ничего не отправляется."""
    chat_id = await add_chat(env.conn, env.helper_acc)
    out = await drafts.create(env.conn, env.state, chat_id=chat_id, text='Только для просмотра')
    notes = await owner_messages(env.conn)
    await env.conn.execute("UPDATE outbox_drafts SET status = 'expired', error_code = 'prepare_removed', "
                           "finished_at = now() WHERE id = $1", out['draft_id'])
    for data in (button(notes, 'Отклонить'), button(notes, 'Отправить')):
        result = await press(env, data)
        assert 'заменены «Черновиком ответа»' in result['answer'] and result['remove_buttons'] is True
    await settle(env)
    assert not env.tg.sent
    assert (await draft_row(env.conn, out['draft_id']))['status'] == 'expired'


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


@pytest.mark.asyncio
async def test_old_view_only_cards_say_the_mode_is_gone_including_autoreply_ones(env):
    """Карточки черновиков «только для просмотра», закрытых миграцией 0045, — и ассистента, и автоответа —
    отвечают на нажатие, что режима больше нет, и теряют кнопки (а не «Кнопка недоступна»)."""
    from shturman.outbox.drafts import PREPARE_REMOVED
    chat_id = await add_chat(env.conn, env.helper_acc)
    for n, origin in enumerate(("agent", "autoreply")):
        draft = await env.conn.fetchval(
            """INSERT INTO outbox_drafts (account_id, chat_id, channel, text, text_hash, origin, nonce, status,
                                          expires_at, trigger_message_id)
               VALUES ($1, $2, 'session', 'Старый текст', 'h', $3, $4, 'pending', now() + interval '1 hour', NULL)
               RETURNING id""", env.helper_acc, chat_id, origin, f"old-nonce-{n:04d}")
        # как это делает 0045: закрыть как истёкший с кодом prepare_removed и стереть текст
        await env.conn.execute("UPDATE outbox_drafts SET status = 'expired', error_code = 'prepare_removed', "
                               "finished_at = now() WHERE id = $1", draft)
        await env.conn.execute("UPDATE outbox_drafts SET text = '', text_purged_at = now() WHERE id = $1", draft)
        out = await press(env, f"sh:ob:r:{draft}:old-nonce-{n:04d}")
        assert out["answer"].startswith("Черновики «только для просмотра» заменены"), origin
        assert out["remove_buttons"] and PREPARE_REMOVED in out["edit_text"]
    # чужая метка — по-прежнему «Кнопка недоступна»
    assert (await press(env, f"sh:ob:r:{draft}:wrong-nonce-01"))["answer"] == "Кнопка недоступна."


@pytest.mark.asyncio
async def test_model_job_from_before_the_update_is_not_lost(env):
    """Задание «Подготовить ответ» прежнего вида (`notify.draft`), вернувшееся после обновления: владелец
    узнаёт, что нажать ещё раз; ответ модели не сохраняется."""
    from shturman import bridge, jobs
    from shturman.notify import buttons as nb
    job = await jobs.enqueue(env.conn, bridge.LLM_STRUCTURED, {"input": "x"}, handler=nb.LEGACY_DRAFT_HANDLER,
                             context={"chat_id": 1, "message_id": 2})
    await env.conn.execute("UPDATE jobs SET status = 'running' WHERE id = $1", job)
    await bridge.deliver_result(env.conn, job, {"parsed": {"outcome": "reply", "text": "Секретный ответ"}})
    texts = [m["payload"]["text"] for m in await owner_messages(env.conn)]
    assert nb.LEGACY_DRAFT_TEXT in texts
    assert await env.conn.fetchval("SELECT count(*) FROM jobs WHERE payload::text LIKE '%Секретный%' "
                                   "OR result::text LIKE '%Секретный%'") == 0
