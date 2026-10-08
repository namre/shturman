"""Independent owner decisions. No agent-facing route establishes this authority."""
from __future__ import annotations
import hmac
import json
from typing import Any
from .. import authority, bridge
from . import workflow

REFUSED = {'answer': 'Решение недоступно.', 'edit_text': None, 'remove_buttons': False}

async def authorized(conn: Any, user_id: int) -> bool:
    principal = authority.get_owner_principal()
    owner = await bridge.get_owner(conn)
    return bool(bridge.owns_bot() and principal and principal.source == 'telegram'
                and principal.user_id == user_id and owner and int(owner['user_id']) == user_id
                and principal.chat_id == int(owner['chat_id']))

@bridge.on_callback(workflow.CALLBACK_MODULE)
async def on_button(conn: Any, rest: str, user_id: int) -> dict[str, Any]:
    parts = rest.split(':')
    if len(parts) != 3 or parts[0] not in ('y', 'n') or not parts[1].isascii() \
            or not parts[1].isdigit() or len(parts[1]) > 18 or not await authorized(conn, user_id):
        return REFUSED
    task = await workflow.get(conn, int(parts[1]), lock=True)
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
    await broker.grant_for_task(conn, task, task['source_request'], owner_id=user_id,
                                expires_at=task['expires_at'], mode='read')
    await conn.execute('UPDATE reply_tasks SET owner_decided_by=$2,owner_decided_at=now() WHERE id=$1',
                       task['id'], user_id)
    await workflow.owner_granted(conn, state, task['id'])
    return {**REFUSED, 'answer': 'Чтение разрешено для этой задачи.', 'remove_buttons': True}

async def handle_command(conn: Any, state: Any, text: str, user_id: int) -> dict[str, Any]:
    if not await authorized(conn, user_id):
        return {'text': 'Решение доступно только владельцу в управляющем боте.'}
    parts = text.split(maxsplit=3)
    if parts and parts[0].split('@')[0] == '/replytasks':
        rows = await conn.fetch("SELECT id,chat_id,status,error_code FROM reply_tasks ORDER BY id DESC LIMIT 20")
        return {'text': '\n'.join(f"№ {r['id']} · чат {r['chat_id']} · {r['status']}" for r in rows) or 'Задач нет.'}
    if len(parts) < 3 or not parts[1].isascii() or not parts[1].isdigit() or len(parts[1]) > 18:
        return {'text': 'Команды: /replytasks; /replytask ID answer <ответ>; /replytask ID cancel.'}
    task = await workflow.get(conn, int(parts[1]), lock=True)
    if task is None or task['status'] in workflow.TERMINAL:
        return {'text': 'Задача недоступна.'}
    if parts[2] == 'cancel':
        await workflow.stop(conn, task['id'], 'cancelled', 'owner_cancelled')
        return {'text': 'Задача отменена.'}
    if parts[2] != 'answer' or len(parts) != 4 or task['status'] != 'waiting_owner' \
            or not parts[3].strip() or len(parts[3]) > 2000:
        return {'text': 'Ответ нужен только задаче waiting_owner: /replytask ID answer <до 2000 знаков>.'}
    if not await workflow.valid(conn, state, task):
        await workflow.stop(conn, task['id'], 'cancelled', 'context_changed')
        return {'text': 'Контекст изменился; задача закрыта.'}
    answer = parts[3].strip()
    await conn.execute('UPDATE reply_tasks SET owner_answer=$2,owner_decided_by=$3,owner_decided_at=now(),'
                       'requires_approval=true WHERE id=$1', task['id'], answer, user_id)
    task.update(owner_answer=answer, requires_approval=True)
    await workflow.queue(conn, task)
    return {'text': 'Та же задача продолжена. Готовый ответ будет отдельным черновиком.'}
