"""Owner source cards and buttons expose and grant only the displayed read scope."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shturman import authority, bridge
from shturman.replies import owner, workflow
from shturman.sources import broker
from shturman.sources.registry import Connector


def request(kind='external', sid='travel'):
    return broker.canonical_source_spec({
        'kind': kind, 'source_id': sid, 'query': 'билет Казань',
        'since': '2026-10-01T00:00:00Z' if kind != 'memory' else None,
        'until': '2026-10-08T00:00:00Z' if kind != 'memory' else None,
        'limit': 3, 'max_chars': 1200, 'reason': 'Проверить рейс'})


def task(spec=None):
    return {'id': 1, 'chat_id': 7, 'topic_tg_id': 42, 'generation': 2,
            'status': 'waiting_source', 'nonce': 'valid',
            'expires_at': datetime.now(timezone.utc) + timedelta(minutes=20),
            'decision_expires_at': datetime.now(timezone.utc) + timedelta(minutes=10),
            'source_request': spec or request()}


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,sid,name,expected', [
    ('external', 'travel', None, 'Почта с билетами'),
    ('chat', '9', 'Секретариат', 'переписка: Секретариат'),
    ('memory', 'person:5', 'Иван Петров', 'память о человеке: Иван Петров'),
    ('chat', None, None, 'общий архив доступной переписки'),
    ('memory', None, None, 'общая память о людях'),
])
async def test_card_plain_names_exact_scope_and_buttons(monkeypatch, kind, sid, name, expected):
    conn = SimpleNamespace(execute=AsyncMock(), fetchval=AsyncMock(side_effect=['Рабочая группа', name]))
    notify = AsyncMock()
    monkeypatch.setattr(bridge, 'owns_bot', lambda: True)
    monkeypatch.setattr(bridge, 'notify_owner', notify)
    connector = Connector('travel', 'Почта с билетами', 'https://sources.example.org/mcp',
                          {'Authorization': 'Bearer private-secret'})
    monkeypatch.setattr(workflow, 'state_current', lambda: SimpleNamespace(extras={'source_registry': {'travel': connector}}))
    monkeypatch.setattr(workflow.secrets, 'token_urlsafe', lambda _: 'fresh')
    t = task(request(kind, sid))
    await workflow.notify_waiting(conn, t, 'source', 'Проверить рейс')
    summary = notify.call_args.args[1]
    assert expected in summary and 'Получатель: Рабочая группа (чат № 7), тема № 42' in summary
    assert 'Запрос: "билет Казань"' in summary
    assert 'Не более 3 фрагментов, всего до 1200 знаков' in summary
    assert 'только чтения' in summary and 'Раскрытие собеседнику согласуется отдельно' in summary
    assert 'тот же источник, запрос, период, пределы, получатель и тема' in summary
    assert 'private-secret' not in summary and 'https://' not in summary
    assert '{' not in summary and 'source_id' not in summary and 'max_chars' not in summary
    if kind == 'memory':
        assert 'без фильтра по датам' in summary
    else:
        assert t['source_request']['since'] in summary and t['source_request']['until'] in summary
        assert 'включительно' in summary and 'не включая эту дату' in summary
    buttons = [b for row in notify.call_args.kwargs['buttons'] for b in row]
    assert [b['text'] for b in buttons] == ['Прочитать один раз', 'Этот поиск — на 30 дней', 'Отказать']
    assert [b['data'] for b in buttons] == ['sh:rp:y:1:fresh', 'sh:rp:r:1:fresh', 'sh:rp:n:1:fresh']
    assert conn.execute.call_args.args[1:] == (1, 'fresh')


@pytest.mark.asyncio
async def test_query_is_not_shortened_and_absent_dates_are_explicit(monkeypatch):
    conn = SimpleNamespace(execute=AsyncMock(), fetchval=AsyncMock(return_value='Адресат'))
    notify = AsyncMock()
    monkeypatch.setattr(bridge, 'owns_bot', lambda: True)
    monkeypatch.setattr(bridge, 'notify_owner', notify)
    query = 'смета ' + 'я' * 480
    spec = broker.canonical_source_spec({'kind': 'chat', 'source_id': '9', 'query': query})
    await workflow.notify_waiting(conn, task(spec), 'source', 'Проверить смету')
    summary = notify.call_args.args[1]
    assert query in summary and 'truncated' not in summary
    assert 'начала архива' in summary and 'конца архива' in summary


def callbacks(monkeypatch, t):
    conn = SimpleNamespace(fetchval=AsyncMock(return_value=False), execute=AsyncMock())
    state = SimpleNamespace()
    monkeypatch.setattr(bridge, 'owns_bot', lambda: True)
    monkeypatch.setattr(bridge, 'get_owner', AsyncMock(return_value={'user_id': 1000, 'chat_id': 1000}))
    monkeypatch.setattr(workflow, 'get', AsyncMock(return_value=t))
    monkeypatch.setattr(workflow, 'state_current', lambda: state)
    monkeypatch.setattr(workflow, 'valid', AsyncMock(return_value=True))
    monkeypatch.setattr(workflow, 'stop', AsyncMock())
    monkeypatch.setattr(workflow, 'owner_granted', AsyncMock())
    grant = AsyncMock()
    monkeypatch.setattr(broker, 'grant_for_task', grant)
    return conn, grant, state


@pytest.mark.asyncio
@pytest.mark.parametrize('choice', ['y', 'r'])
async def test_owner_buttons_only_grant_exact_read_scope(monkeypatch, choice):
    t = task()
    conn, grant, state = callbacks(monkeypatch, t)
    with authority.owner_context(1000, chat_id=1000):
        result = await owner.on_button(conn, f'{choice}:1:valid', 1000)
    assert result['remove_buttons']
    args, kwargs = grant.call_args
    assert args == (conn, t, t['source_request'])
    assert kwargs['mode'] == 'read' and kwargs['owner_id'] == 1000
    if choice == 'r':
        assert kwargs['persistent'] is True and 'expires_at' not in kwargs
    else:
        assert kwargs['expires_at'] == t['expires_at'] and not kwargs.get('persistent')
    workflow.owner_granted.assert_awaited_once_with(conn, state, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['no_authority', 'wrong_user', 'wrong_chat', 'stale_nonce', 'expired', 'invalid_task', 'wrong_status'])
async def test_persistent_button_rejects_invalid_owner_nonce_and_task(monkeypatch, failure):
    t = task()
    conn, grant, _ = callbacks(monkeypatch, t)
    if failure == 'expired':
        conn.fetchval.return_value = True
    if failure == 'invalid_task':
        workflow.valid.return_value = False
    if failure == 'wrong_status':
        t['status'] = 'waiting_owner'
    nonce = 'old' if failure == 'stale_nonce' else 'valid'
    user_id = 999 if failure == 'wrong_user' else 1000
    if failure == 'no_authority':
        result = await owner.on_button(conn, f'r:1:{nonce}', user_id)
    else:
        with authority.owner_context(user_id, chat_id=999 if failure == 'wrong_chat' else 1000):
            result = await owner.on_button(conn, f'r:1:{nonce}', user_id)
    assert result['answer'] in ('Решение недоступно.', 'Контекст изменился; задача закрыта.')
    grant.assert_not_awaited()
    workflow.owner_granted.assert_not_awaited()


@pytest.mark.asyncio
async def test_broker_persistent_default_is_30_days_not_task_expiry(monkeypatch):
    t = task()
    monkeypatch.setattr(bridge, 'owns_bot', lambda: True)
    monkeypatch.setattr(bridge, 'get_owner', AsyncMock(return_value={'user_id': 1000, 'chat_id': 1000}))
    monkeypatch.setattr(broker, '_task', AsyncMock(return_value=t))
    captured = []
    async def insert(query, *args):
        captured.append(args)
        return {'id': 8, 'expires_at': args[-1]}
    conn = SimpleNamespace(fetchrow=insert)
    before = datetime.now(timezone.utc)
    with authority.owner_context(1000, chat_id=1000):
        result = await broker.grant_for_task(conn, t, t['source_request'], owner_id=1000,
                                            persistent=True, mode='read')
    expiry = datetime.fromisoformat(result['expires_at'])
    assert before + timedelta(days=30) <= expiry <= datetime.now(timezone.utc) + timedelta(days=30)
    assert expiry > t['expires_at']
    assert captured[0][:3] == (None, 7, 42) and captured[0][6] == 'read'
