"""Фоновые запросы Telethon по каналам (`updates.GetChannelDifference`) и страницы диалогов.

Найденный случай: после загрузки списка диалогов Telethon раз в 15 минут запрашивал
разницу по десяткам каналов подряд за полсекунды и получал FLOOD_WAIT по каждому. Теперь по
невыбранным каналам запрос в Telegram не уходит, по выбранным — с паузами, а во время
ожидания — не уходит ничего.
"""

import asyncio
import logging
import random

import pytest
from telethon import TelegramClient, errors
from telethon._updates.session import Entity, EntityType
from telethon.sessions import MemorySession
from telethon.tl import functions, types

from shturman.tg import client as tg_client
from shturman.tg.client import ChannelUpdatesThrottle, GuardedClient, RequestPolicy, Spacer
from shturman.tg.live import LiveIngest
from shturman.tg.sync import ChatState


def diff_request(channel_id, pts=100):
    return functions.updates.GetChannelDifferenceRequest(
        channel=types.InputChannel(channel_id, 1), filter=types.ChannelMessagesFilterEmpty(),
        pts=pts, limit=100)


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(round(seconds, 3))
        self.now += seconds


def throttle(clock, wanted=None):
    t = ChannelUpdatesThrottle("owner", gap=2.0, clock=clock, sleep=clock.sleep, rng=random.Random(1))
    t.wanted = wanted
    return t


async def test_unselected_channel_never_reaches_telegram():
    clock, sent = Clock(), []

    async def send():
        sent.append(1)

    t = throttle(clock)                                  # фильтр ещё не задан: не выбран никакой
    out = await t.run(diff_request(7, pts=555), send)
    assert isinstance(out, types.updates.ChannelDifferenceEmpty)
    assert out.final is True and out.pts == 555          # номер состояния прежний
    t.wanted = lambda cid: cid == 8
    await t.run(diff_request(7), send)
    assert sent == [] and t.skipped == 2 and clock.slept == []


async def test_filter_error_counts_as_not_selected():
    def broken(_cid):
        raise RuntimeError("x")

    clock = Clock()
    t = throttle(clock, broken)

    async def send():
        raise AssertionError("не должно уйти в Telegram")

    assert isinstance(await t.run(diff_request(1), send), types.updates.ChannelDifferenceEmpty)


async def test_selected_channels_are_spaced_not_burst():
    clock, sent = Clock(), []

    async def send():
        sent.append(clock.now)
        return "ok"

    t = throttle(clock, lambda cid: True)
    for cid in range(5):
        assert await t.run(diff_request(cid), send) == "ok"
    gaps = [b - a for a, b in zip(sent, sent[1:])]
    assert len(sent) == 5 and all(2.0 <= g <= 3.6 for g in gaps)


async def test_flood_wait_stops_all_channel_requests_until_it_ends(caplog):
    clock, sent = Clock(), []

    async def flood():
        sent.append("flood")
        raise errors.FloodWaitError(diff_request(1), capture=30)

    async def ok():
        sent.append("ok")
        return "ok"

    t = throttle(clock, lambda cid: True)
    with caplog.at_level(logging.WARNING, logger="shturman.tg"):
        with pytest.raises(errors.FloodWaitError):
            await t.run(diff_request(1), flood)
    assert "подождать 30 с" in caplog.text
    for cid in range(2, 36):                             # остальная пачка — без запросов
        assert isinstance(await t.run(diff_request(cid), ok), types.updates.ChannelDifferenceEmpty)
    assert sent == ["flood"] and t.skipped == 34
    clock.now += 31.5
    assert await t.run(diff_request(2), ok) == "ok"
    assert sent == ["flood", "ok"]


async def test_spacer_spaces_dialog_pages():
    clock, sent = Clock(), []
    s = Spacer(1.5, clock=clock, sleep=clock.sleep, rng=random.Random(2))

    async def send():
        sent.append(clock.now)

    for _ in range(4):
        await s.run(send)
    assert all(1.5 <= b - a <= 2.7 for a, b in zip(sent, sent[1:]))


# --- настоящий клиент и настоящий MessageBox Telethon ---

@pytest.fixture
def network(monkeypatch):
    calls = []

    async def fake_call(self, sender, request, ordered=False, flood_sleep_threshold=None):
        calls.append(request)
        if isinstance(request, functions.updates.GetChannelDifferenceRequest):
            return types.updates.ChannelDifferenceEmpty(pts=request.pts + 1, final=True)
        return types.messages.DialogsSlice(count=0, dialogs=[], messages=[], chats=[], users=[])

    monkeypatch.setattr(TelegramClient, "_call", fake_call)
    return calls


def make_client():
    return GuardedClient(MemorySession(), 12345, "0123456789abcdef", policy=RequestPolicy("owner"))


async def test_telethon_update_loop_burst_sends_only_selected(network):
    """Повтор того, что делает цикл обновлений Telethon, когда у 35 каналов истёк срок тишины."""
    client = make_client()
    client._shturman_channels._sleep = lambda s: asyncio.sleep(0)   # паузы проверены выше
    box, cache = client._message_box, client._mb_entity_cache
    channels = list(range(5001, 5036))
    for cid in channels:
        box.try_set_channel_state(cid, 100)
        cache.put(Entity(EntityType.MEGAGROUP, cid, 77))
    selected = {5003, 5010}
    client.set_channel_filter(lambda cid: cid in selected)

    for state in box.map.values():
        state.deadline = 0                                # 15 минут тишины прошли
    box.next_deadline = channels[0]
    box.check_deadlines()
    assert set(box.getting_diff_for) == set(channels)
    rounds = 0
    while True:
        get_diff = box.get_channel_difference(cache)
        if not get_diff:
            break
        diff = await client(get_diff)                     # ровно так зовёт client/updates.py
        box.apply_channel_difference(get_diff, diff, cache)
        rounds += 1
    assert rounds == 35
    sent = [r.channel.channel_id for r in network if isinstance(r, functions.updates.GetChannelDifferenceRequest)]
    assert sorted(sent) == sorted(selected)
    # Невыбранные каналы Telethon не забыл и не сдвинул: номер прежний, следующий раз — через 15 минут.
    assert all(box.map[cid].pts == 100 for cid in channels if cid not in selected)
    assert all(box.map[cid].pts == 101 for cid in selected)
    assert not box.getting_diff_for


async def test_dialog_pages_go_through_spacer(network):
    client = make_client()
    seen = []

    async def run(send):
        seen.append(1)
        return await send()

    client._shturman_dialogs.run = run
    await client(functions.messages.GetDialogsRequest(
        offset_date=None, offset_id=0, offset_peer=types.InputPeerEmpty(), limit=100, hash=0))
    assert seen == [1] and isinstance(network[-1], functions.messages.GetDialogsRequest)


async def test_policy_still_checked_before_throttle(network):
    client = GuardedClient(MemorySession(), 12345, "0123456789abcdef", policy=RequestPolicy("owner"))
    with pytest.raises(tg_client.RequestNotAllowed):
        await client(functions.messages.SendMessageRequest(types.InputPeerUser(1, 1), "x"))
    assert network == []


def test_live_index_decides_which_channels_are_wanted():
    live = LiveIngest.__new__(LiveIngest)
    live.index = {("channel", 10): ChatState(1, True), ("channel", 11): ChatState(2, False),
                  ("chat", 12): ChatState(3, True)}
    assert live.wants_channel(10) is True
    assert live.wants_channel(11) is False                # выключен или исключён
    assert live.wants_channel(12) is False                # обычная группа — не канал
    assert live.wants_channel(99) is False                # не выбирали
