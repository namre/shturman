"""Independent owner decisions. No agent-facing route establishes this authority.

Ответ владельца на вопрос ассистента (П1, Б4) — одна функция `answer` для всех трёх входов: команда
`/replytask N answer текст`, кнопка «Ответить» в боте (ответ следующим сообщением, ForceReply,
`executor/owner_chat.py`) и поле «Ваш ответ» в приложении «Штурман» (`setup_page/miniapp.py`). Прав
она не добавляет: тот же проверенный владелец из Telegram в личном чате бота согласований, та же задача
в состоянии `waiting_owner`, та же проверка, что переписка не изменилась, тот же черновик, который
ждёт нажатия «Отправить».
"""
from __future__ import annotations
import hmac
from typing import Any
from .. import authority, bridge
from . import workflow

REFUSED = {'answer': 'Решение недоступно.', 'edit_text': None, 'remove_buttons': False}
MAX_ANSWER = 2000

CLOSED = 'Этот вопрос уже закрыт — ассистент не дождался ответа.'
ANSWERED = 'На этот вопрос вы уже ответили — ассистент готовит ответ.'
LATE = 'Время на ответ вышло — ассистент не дождался ответа. Вопрос закрыт.'
CHANGED = 'Переписка изменилась, пока ассистент ждал: этот вопрос закрыт.'
EMPTY = 'Ответ пустой — напишите хотя бы несколько слов.'
TOO_LONG = f'Ответ длиннее {MAX_ANSWER} знаков — сократите и отправьте ещё раз.'
THANKS = 'Спасибо, ассистент продолжает. Готовый ответ придёт отдельным черновиком.'
PROMPT = ('Напишите ответ на вопрос ассистента № {id} одним сообщением{until}. Передумали — нажмите '
          '«Не отвечать» под вопросом.')


async def authorized(conn: Any, user_id: int) -> bool:
    principal = authority.get_owner_principal()
    owner = await bridge.get_owner(conn)
    return bool(bridge.owns_bot() and principal and principal.source == 'telegram'
                and principal.user_id == user_id and owner and int(owner['user_id']) == user_id
                and principal.chat_id == int(owner['chat_id']))


async def _window_open(conn: Any, task: dict[str, Any]) -> bool:
    return task['decision_expires_at'] is not None and not await conn.fetchval(
        'SELECT $1::timestamptz <= clock_timestamp()', task['decision_expires_at'])


async def answer(conn: Any, state: Any, task_id: int, text: Any, user_id: int, *,
                 nonce: str | None = None, window: bool = False) -> dict[str, Any]:
    """Ответ владельца на вопрос ассистента. Возвращает {ok, code, text} — текст для владельца словами.

    nonce — метка карточки, на которой нажали «Ответить» (бот) или которую видело приложение: вопрос с
    тех пор сменился — ответ не принимается. window — проверять и окно решения карточки (кнопка и
    приложение показывают его владельцу «до ЧЧ:ММ»); команда, как и раньше, — до конца задачи."""
    if not await authorized(conn, user_id):
        return {'ok': False, 'code': 'not_owner', 'text': 'Решение доступно только владельцу в управляющем боте.'}
    if not isinstance(text, str) or not text.strip():
        return {'ok': False, 'code': 'empty', 'text': EMPTY}
    text = text.strip()
    if len(text) > MAX_ANSWER:
        return {'ok': False, 'code': 'too_long', 'text': TOO_LONG}
    task = await workflow.get(conn, task_id, lock=True)
    if task is None:
        return {'ok': False, 'code': 'closed', 'text': CLOSED}
    if task['status'] != 'waiting_owner':
        if task.get('owner_answer') and task['status'] in ('generating', 'drafting', 'draft_ready', 'completed'):
            return {'ok': False, 'code': 'answered', 'text': ANSWERED}
        return {'ok': False, 'code': 'closed', 'text': CLOSED}
    if nonce is not None and not hmac.compare_digest(nonce.encode(), task['nonce'].encode()):
        return {'ok': False, 'code': 'closed', 'text': CLOSED}
    if window and not await _window_open(conn, task):
        return {'ok': False, 'code': 'late', 'text': LATE}
    if not await workflow.valid(conn, state, task):
        await workflow.stop(conn, task['id'], 'cancelled', 'context_changed')
        return {'ok': False, 'code': 'changed', 'text': CHANGED}
    await conn.execute('UPDATE reply_tasks SET owner_answer=$2,owner_decided_by=$3,owner_decided_at=now(),'
                       'requires_approval=true WHERE id=$1', task['id'], text, user_id)
    await workflow.record_owner_resume(conn, task['id'])
    task.update(owner_answer=text, requires_approval=True)
    await workflow.queue(conn, task)
    current = await workflow.get(conn, task['id'])
    if current is not None and current['status'] == 'cancelled' and current.get('error_code') == 'owner_stopped':
        from .. import halt
        return {'ok': False, 'code': 'stopped', 'text': halt.STOPPED_TEXT}
    return {'ok': True, 'code': 'ok', 'text': THANKS}


@bridge.on_callback(workflow.CALLBACK_MODULE)
async def on_button(conn: Any, rest: str, user_id: int) -> dict[str, Any]:
    parts = rest.split(':')
    if len(parts) != 3 or parts[0] not in ('y', 'r', 'n', 'a') or not parts[1].isascii() \
            or not parts[1].isdigit() or len(parts[1]) > 18 or not await authorized(conn, user_id):
        return REFUSED
    task = await workflow.get(conn, int(parts[1]), lock=True)
    if parts[0] == 'a':
        return await _ask_answer(conn, task, parts[2])
    if task is None or task['status'] not in ('waiting_source', 'waiting_owner') \
            or not hmac.compare_digest(parts[2].encode(), task['nonce'].encode()):
        return REFUSED
    if task['decision_expires_at'] is None or await conn.fetchval(
            'SELECT $1::timestamptz <= clock_timestamp()', task['decision_expires_at']):
        return REFUSED
    state = workflow.state_current()
    if not await workflow.valid(conn, state, task):
        await workflow.stop(conn, task['id'], 'cancelled', 'context_changed')
        return {**REFUSED, 'answer': 'Контекст изменился; задача закрыта.', 'remove_buttons': True}
    if parts[0] == 'n':
        await workflow.stop(conn, task['id'], 'declined')
        return {**REFUSED, 'answer': 'Отклонено.', 'remove_buttons': True}
    if task['status'] != 'waiting_source' or not task['source_request']:
        return REFUSED
    from ..sources import broker
    persistent = parts[0] == 'r'
    grant_options = {'persistent': True} if persistent else {'expires_at': task['expires_at']}
    await broker.grant_for_task(conn, task, task['source_request'], owner_id=user_id,
                                mode='read', **grant_options)
    await conn.execute('UPDATE reply_tasks SET owner_decided_by=$2,owner_decided_at=now() WHERE id=$1',
                       task['id'], user_id)
    await workflow.record_owner_resume(conn, task['id'])
    resumed = await workflow.owner_granted(conn, state, task['id'])
    answer = 'Этот поиск разрешён на 30 дней для того же получателя и темы.' if persistent \
        else 'Чтение разрешено для этой задачи.'
    if not resumed:
        current = await workflow.get(conn, task['id'])
        if current is not None and current['status'] == 'waiting_source':
            # A read grant is a completed owner decision; do not leave an unusable card.
            await workflow.stop(conn, task['id'], 'failed', 'source_unavailable')
            current = await workflow.get(conn, task['id'])
        if current is not None and current.get('error_code') == 'context_changed':
            answer += ' Контекст изменился; ответ не подготовлен, задача закрыта.'
        elif current is not None and current.get('error_code') == 'source_unavailable':
            answer += ' Источник недоступен; ответ не подготовлен, задача закрыта.'
        else:
            answer += ' Ответ не подготовлен; задача закрыта.'
    return {**REFUSED, 'answer': answer, 'remove_buttons': True}


async def _ask_answer(conn: Any, task: dict[str, Any] | None, nonce: str) -> dict[str, Any]:
    """«Ответить» под вопросом ассистента: бот попросит ответ следующим сообщением (ForceReply).
    Сама кнопка ничего не меняет — ответ проверяется заново, когда придёт (`answer`)."""
    if task is None or task['status'] != 'waiting_owner' \
            or not hmac.compare_digest(nonce.encode(), task['nonce'].encode()):
        return {**REFUSED, 'answer': CLOSED, 'remove_buttons': True}
    if not await _window_open(conn, task):
        return {**REFUSED, 'answer': LATE, 'remove_buttons': True}
    until = task['decision_expires_at']
    from .workflow import _owner_zone
    words_until = f' — до {until.astimezone(_owner_zone()):%H:%M}'
    return {'answer': 'Напишите ответ следующим сообщением.', 'edit_text': None, 'remove_buttons': False,
            'prompt': {'kind': 'reply', 'id': task['id'], 'nonce': nonce, 'until': until.timestamp(),
                       'text': PROMPT.format(id=task['id'], until=words_until),
                       'placeholder': 'Ваш ответ ассистенту'}}


async def handle_command(conn: Any, state: Any, text: str, user_id: int) -> dict[str, Any]:
    if not await authorized(conn, user_id):
        return {'text': 'Решение доступно только владельцу в управляющем боте.'}
    parts = text.split(maxsplit=3)
    if parts and parts[0].split('@')[0] == '/replytasks':
        from ..executor import owner_views
        return {'text': await owner_views.reply_tasks_text(conn)}
    if len(parts) < 3 or not parts[1].isascii() or not parts[1].isdigit() or len(parts[1]) > 18:
        return {'text': 'Так: /replytask номер answer ваш ответ — ответить ассистенту; '
                        '/replytask номер cancel — отменить. Номера — в /replytasks.'}
    task = await workflow.get(conn, int(parts[1]), lock=True)
    if task is None or task['status'] in workflow.TERMINAL:
        return {'text': f'Ответа на сообщение № {parts[1]} нет или он уже завершён. Список — /replytasks.'}
    if parts[2] == 'cancel':
        await workflow.stop(conn, task['id'], 'cancelled', 'owner_cancelled')
        return {'text': f'Ответ на сообщение № {task["id"]} отменён: ассистент его больше не готовит. '
                        'Нужен снова — попросите ассистента.'}
    if parts[2] != 'answer' or len(parts) != 4 or not parts[3].strip():
        return {'text': 'Ответить можно, только когда ассистент задал вопрос (в /replytasks — «ассистент ждёт '
                        'вашего уточнения»). Так: /replytask номер answer ваш ответ — до 2000 знаков. '
                        'Проще — кнопкой «Ответить» под вопросом.'}
    if task['status'] != 'waiting_owner':
        return {'text': CLOSED}
    out = await answer(conn, state, task['id'], parts[3], user_id)
    return {'text': out['text']}

