"""Forum replies keep exact thread identity and never use the owner's session."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.tl import types

from shturman.tg.gateway import SendForbidden
from shturman.tg.manager import TgManager


class Client:
    def __init__(self):
        self.requests = []
        self.resolutions = []

    async def get_input_entity(self, peer):
        self.resolutions.append(peer)
        return types.InputPeerChannel(55, 123)

    async def __call__(self, request):
        self.requests.append(request)
        return types.UpdateShortSentMessage(id=90, pts=1, pts_count=1,
                                           date=datetime.now(timezone.utc), out=True)


def manager(role='assistant', sending=True):
    client = Client()
    events = SimpleNamespace(subscribe=lambda *args: None)
    mgr = TgManager(SimpleNamespace(sending=sending), None, events)
    mgr._role_of = AsyncMock(return_value=role)
    mgr._running = lambda _: SimpleNamespace(client=client, policy=SimpleNamespace(can_send=True),
                                              self_id=777, live=None)
    return mgr, client


@pytest.mark.asyncio
async def test_forum_reply_uses_both_reply_and_top_message():
    mgr, client = manager()
    assert await mgr.send_text(1, 'channel', 55, 'Ответ', reply_to_tg_id=12, topic_tg_id=7) == 90
    request = client.requests[0]
    assert request.reply_to.reply_to_msg_id == 12
    assert request.reply_to.top_msg_id == 7


@pytest.mark.asyncio
async def test_later_multipart_message_stays_in_same_topic():
    mgr, client = manager()
    await mgr.send_text(1, 'channel', 55, 'Часть 2', topic_tg_id=7)
    assert client.requests[0].reply_to.reply_to_msg_id == 7
    assert client.requests[0].reply_to.top_msg_id == 7


@pytest.mark.asyncio
@pytest.mark.parametrize('role,sending', [('owner', True), ('assistant', False)])
async def test_group_transport_refuses_before_peer_resolution(role, sending):
    mgr, client = manager(role, sending)
    with pytest.raises(SendForbidden):
        await mgr.send_text(1, 'channel', 55, 'Ответ', topic_tg_id=7)
    assert not client.resolutions and not client.requests


@pytest.mark.asyncio
@pytest.mark.parametrize('peer,topic', [('user', 7), ('channel', 0), ('channel', True)])
async def test_invalid_topic_refused_before_peer_resolution(peer, topic):
    mgr, client = manager()
    with pytest.raises(ValueError):
        await mgr.send_text(1, peer, 55, 'Ответ', topic_tg_id=topic)
    assert not client.resolutions


@pytest.mark.asyncio
async def test_manager_background_task_drops_transient_owner_authority():
    import asyncio
    from shturman import authority
    mgr, _ = manager()
    observed = []

    async def event_worker():
        observed.append(authority.is_owner())

    with authority.owner_context(1000, chat_id=1000):
        mgr._spawn(event_worker(), 'synthetic-live-worker')
        await asyncio.gather(*list(mgr._background))
        assert authority.is_owner()
    assert observed == [False]
