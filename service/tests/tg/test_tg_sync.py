"""Чтение истории: загрузка вглубь, дозагрузка вперёд, сверка удалений, ограничения Telegram."""

import asyncio
from datetime import timedelta

import pytest
from telethon import errors
from telethon.tl import functions

from shturman import events as ev
from shturman import store
from shturman.events import Events
from shturman.tg import normalize, sync
from shturman.tg.client import RequestPolicy
from shturman.tg.sync import HistorySync, Pacer, Stopped

from tg_fakes import (C_NEWS, CHANNEL, G_FAMILY, GROUP, IVAN, MARIA, SELF_ID, T0, U_IVAN, U_MARIA, U_TELEGRAM,
                      FakeClient, World, msg, now_msg, pool)  # noqa: F401 — pool это фикстура

IVAN_KEY, GROUP_KEY, NEWS_KEY = ("user", IVAN), ("chat", GROUP), ("channel", CHANNEL)
History = functions.messages.GetHistoryRequest


class Clock:
    """Часы и сон для Pacer: время идёт только когда кто-то «спит»."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds, stop):
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)
        return stop.is_set()


class Rig:
    def __init__(self, pool, world, account_id, *, interval=0.0):
        self.world, self.pool, self.account_id = world, pool, account_id
        self.clock = Clock()
        self.events = Events()
        self.live, self.deleted = [], []

        async def on_live(payload):
            self.live.append(payload)

        async def on_deleted(payload):
            self.deleted.append(payload)

        self.events.subscribe(ev.MESSAGE_LIVE, on_live)
        self.events.subscribe(ev.MESSAGES_DELETED, on_deleted)
        self.stop = asyncio.Event()
        self.client = FakeClient(world, "owner", RequestPolicy("owner"))
        self.client.connected = True
        self.pacer = Pacer(interval, clock=self.clock, sleep=self.clock.sleep)
        self.sync = self.new_sync()

    def new_sync(self):
        return HistorySync(client=self.client, pool=self.pool, events=self.events, account_id=self.account_id,
                           self_id=SELF_ID, pacer=self.pacer, stop=self.stop)

    async def enable(self, entity):
        async with self.pool.acquire() as conn:
            return await sync.enable_chat(conn, self.account_id, normalize.chat_record(entity, self_id=SELF_ID))

    async def row(self, key):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM tg_sync_chats WHERE account_id = $1 AND peer_class = $2 AND tg_id = $3",
                self.account_id, *key)

    async def ids(self, chat_id, extra=""):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT tg_message_id FROM messages WHERE chat_id = $1 {extra} ORDER BY tg_message_id", chat_id)
        return [r["tg_message_id"] for r in rows]

    def offsets(self):
        return [(r.offset_id, r.add_offset) for r in self.client.of(History)]


@pytest.fixture
async def rig(pool, conn):  # noqa: F811
    account_id = await store.ensure_account(conn, SELF_ID, "Владелец")
    return Rig(pool, World(), account_id)


def fill(world, key, first, last, **kw):
    world.add(*[msg(i, key, f"сообщение {i}", **kw) for i in range(first, last + 1)])


# --- выбор чатов ---

async def test_nothing_is_read_until_owner_selects_a_chat(rig):
    fill(rig.world, IVAN_KEY, 1, 30)
    fill(rig.world, GROUP_KEY, 31, 40, sender=MARIA)
    await rig.sync.backfill_all()
    assert await rig.sync.gap_fill_pass() is False
    assert rig.client.requests == []           # ни одного запроса к Telegram
    chat_id, enabled = await rig.enable(U_IVAN)
    assert enabled
    await rig.sync.backfill_all()
    assert await rig.ids(chat_id) == list(range(1, 31))
    assert {r.peer.user_id for r in rig.client.of(History)} == {IVAN}    # группу никто не трогал
    assert rig.live == []


async def test_service_chat_with_login_codes_cannot_be_enabled(rig):
    rig.world.add(msg(1, ("user", 777000), "Login code: 12345"))
    chat_id, enabled = await rig.enable(U_TELEGRAM)
    assert enabled is False
    await rig.sync.backfill_all()
    assert rig.client.requests == [] and await rig.ids(chat_id) == []


async def test_excluded_chat_is_skipped_even_if_it_was_enabled(rig):
    fill(rig.world, IVAN_KEY, 1, 5)
    chat_id, _ = await rig.enable(U_IVAN)
    async with rig.pool.acquire() as conn:
        await conn.execute("UPDATE chats SET excluded = true WHERE id = $1", chat_id)
    await rig.sync.backfill_all()
    await rig.sync.gap_fill_pass()
    assert rig.client.requests == [] and await rig.ids(chat_id) == []


# --- загрузка вглубь ---

async def test_backfill_goes_newest_to_oldest_in_pages_of_100(rig):
    fill(rig.world, IVAN_KEY, 1, 250)
    chat_id, _ = await rig.enable(U_IVAN)
    await rig.sync.backfill_all()
    assert all(r.limit == 100 for r in rig.client.of(History))
    assert rig.offsets() == [(0, 0), (151, 0), (51, 0)]
    assert await rig.ids(chat_id) == list(range(1, 251))
    row = await rig.row(IVAN_KEY)
    assert (row["backfill_before"], row["backfill_done"], row["forward_id"]) == (1, True, 250)
    assert rig.live == [] and rig.deleted == []        # история событий не порождает
    async with rig.pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM messages WHERE sources = ARRAY['session']") == 250
        assert await conn.fetchval("SELECT count(*) FROM messages WHERE media_path IS NOT NULL") == 0
    # повторный проход ничего не запрашивает
    before = len(rig.client.requests)
    await rig.sync.backfill_all()
    assert len(rig.client.requests) == before


async def test_backfill_ends_on_empty_page_when_history_does_not_start_at_one(rig):
    fill(rig.world, NEWS_KEY, 10, 120, post=True)
    chat_id, _ = await rig.enable(C_NEWS)
    await rig.sync.backfill_all()
    assert rig.offsets() == [(0, 0), (21, 0), (10, 0)]     # последняя страница пустая
    row = await rig.row(NEWS_KEY)
    assert (row["backfill_before"], row["backfill_done"]) == (10, True)
    assert await rig.ids(chat_id) == list(range(10, 121))


async def test_backfill_resumes_exactly_after_interruption(rig):
    fill(rig.world, NEWS_KEY, 1, 350, post=True)
    chat_id, _ = await rig.enable(C_NEWS)
    await rig.sync.backfill_round()
    await rig.sync.backfill_round()
    assert (await rig.row(NEWS_KEY))["backfill_before"] == 151
    # обрыв: процесс умер. Новый процесс — новый клиент и новый объект работы.
    rig.client = FakeClient(rig.world, "owner", RequestPolicy("owner"))
    rig.client.connected = True
    resumed = rig.new_sync()
    await resumed.backfill_all()
    assert rig.offsets()[0] == (151, 0)                # ровно с места, без повтора страниц
    assert rig.offsets() == [(151, 0), (51, 0)]            # дошли до сообщения №1 — старше ничего нет
    assert await rig.ids(chat_id) == list(range(1, 351))
    assert (await rig.row(NEWS_KEY))["backfill_done"] is True


async def test_page_and_cursor_are_one_transaction(rig, monkeypatch):
    fill(rig.world, IVAN_KEY, 1, 150)
    chat_id, _ = await rig.enable(U_IVAN)
    real = store.upsert_messages
    calls = []

    async def failing(conn, rows, **kw):
        result = await real(conn, rows, **kw)
        calls.append(1)
        if len(calls) == 2:
            raise ConnectionError("база пропала посреди страницы")
        return result

    monkeypatch.setattr(store, "upsert_messages", failing)
    await rig.sync.backfill_round()
    await rig.sync.backfill_round()                     # вторая страница не записалась
    row = await rig.row(IVAN_KEY)
    assert row["backfill_before"] == 51 and row["last_error"] == "ConnectionError"
    assert await rig.ids(chat_id) == list(range(51, 151))     # и сообщений второй страницы нет
    await rig.sync.backfill_all()
    assert await rig.ids(chat_id) == list(range(1, 151))


async def test_flood_wait_is_obeyed_and_not_retried_earlier(rig):
    fill(rig.world, IVAN_KEY, 1, 150)
    await rig.enable(U_IVAN)
    rig.pacer.interval = 3.0
    rig.world.fail[History] = [errors.FloodWaitError(None, 47)]
    times = []
    real = rig.client._GetHistoryRequest

    async def stamped(request):
        times.append(rig.clock.now)
        return await real(request)

    rig.client._GetHistoryRequest = stamped
    started = rig.clock.now
    await rig.sync.backfill_all()
    # первый запрос получил FLOOD_WAIT_47; следующий ушёл не раньше, чем через 47 секунд
    assert times[0] - started >= 47
    assert max(rig.clock.sleeps) >= 47
    assert rig.pacer.flood_until is None            # ожидание закончилось
    assert len(rig.client.of(History)) == 3         # отказанный + две страницы
    assert times[1] - times[0] >= 3.0               # и обычная пауза между запросами


async def test_pacing_between_requests(rig):
    fill(rig.world, NEWS_KEY, 1, 350, post=True)
    await rig.enable(C_NEWS)
    rig.pacer.interval = 3.0
    started = rig.clock.now
    await rig.sync.backfill_all()
    assert len(rig.client.of(History)) == 4
    assert rig.clock.now - started >= 9.0           # не больше 10 запросов за 30 секунд


async def test_lost_access_marks_chat_and_stops_asking(rig):
    fill(rig.world, NEWS_KEY, 1, 250, post=True)
    fill(rig.world, IVAN_KEY, 1, 5)
    await rig.enable(C_NEWS)
    ivan_chat, _ = await rig.enable(U_IVAN)
    rig.world.lost[NEWS_KEY] = errors.ChannelPrivateError(None)
    await rig.sync.backfill_all()
    row = await rig.row(NEWS_KEY)
    assert row["access_lost_at"] is not None and row["access_lost_reason"] == "ChannelPrivateError"
    assert await rig.ids(ivan_chat) == [1, 2, 3, 4, 5]          # остальные чаты продолжают
    before = len(rig.client.requests)
    await rig.sync.backfill_all()
    await rig.sync.gap_fill_pass()
    await rig.sync.reconcile_pass()
    assert [r for r in rig.client.requests[before:] if getattr(r, "peer", None) is not None
            and getattr(r.peer, "channel_id", None) == CHANNEL] == []
    # владелец включил чат заново — отметка снята
    await rig.enable(C_NEWS)
    assert (await rig.row(NEWS_KEY))["access_lost_at"] is None


async def test_cursor_that_does_not_move_is_rejected(rig):
    fill(rig.world, IVAN_KEY, 1, 250)
    chat_id, _ = await rig.enable(U_IVAN)
    await rig.sync.backfill_round()
    real = rig.client._GetHistoryRequest

    async def stuck(request):       # Telegram отдаёт ту же страницу, что и в прошлый раз
        request.offset_id = 0
        return await real(request)

    rig.client._GetHistoryRequest = stuck
    assert await rig.sync.backfill_round() == 0
    row = await rig.row(IVAN_KEY)
    assert (row["backfill_before"], row["last_error"], row["backfill_done"]) == (151, "cursor_stuck", False)
    before = len(rig.client.requests)
    await rig.sync.backfill_all()                     # и по кругу не ходит
    assert len(rig.client.requests) == before


async def test_unknown_entity_is_postponed_not_fatal(rig):
    fill(rig.world, IVAN_KEY, 1, 5)
    await rig.enable(U_IVAN)
    rig.world.unknown.add(IVAN_KEY)
    await rig.sync.backfill_all()
    assert (await rig.row(IVAN_KEY))["last_error"] == "entity_unknown" and rig.client.requests == []


async def test_page_fetched_for_chat_disabled_meanwhile_is_discarded(rig):
    fill(rig.world, IVAN_KEY, 1, 150)
    chat_id, _ = await rig.enable(U_IVAN)
    await rig.sync.backfill_round()
    real = rig.client._GetHistoryRequest

    async def disabled_during_request(request):
        async with rig.pool.acquire() as conn:
            await sync.disable_chat(conn, rig.account_id, IVAN_KEY)
        return await real(request)

    rig.client._GetHistoryRequest = disabled_during_request
    await rig.sync.backfill_round()
    assert await rig.ids(chat_id) == list(range(51, 151))      # вторая страница не записана
    assert (await rig.row(IVAN_KEY))["backfill_before"] == 51  # и курсор не сдвинут


async def test_dead_session_stops_the_work(rig):
    fill(rig.world, IVAN_KEY, 1, 5)
    await rig.enable(U_IVAN)
    rig.world.fail[History] = [errors.AuthKeyDuplicatedError(None)]
    with pytest.raises(errors.AuthKeyDuplicatedError):
        await rig.sync.backfill_all()
    assert len(rig.client.of(History)) == 1           # повторов нет


async def test_messages_of_another_chat_in_a_page_are_not_written(rig):
    rig.world.add(msg(1, IVAN_KEY, "своё"))
    rig.world.history[IVAN_KEY][2] = msg(2, ("user", MARIA), "чужое")
    chat_id, _ = await rig.enable(U_IVAN)
    await rig.sync.backfill_all()
    assert await rig.ids(chat_id) == [1]


# --- дозагрузка вперёд ---

async def test_gap_fill_reads_forward_from_own_cursor(rig):
    fill(rig.world, NEWS_KEY, 1, 120, post=True)
    chat_id, _ = await rig.enable(C_NEWS)
    await rig.sync.backfill_all()
    assert (await rig.row(NEWS_KEY))["forward_id"] == 120
    rig.client.requests.clear()
    fill(rig.world, NEWS_KEY, 121, 350, post=True)     # пришло, пока сервис не работал
    assert await rig.sync.gap_fill_pass() is False
    assert rig.offsets() == [(121, -100), (221, -100), (321, -100)]
    assert await rig.ids(chat_id) == list(range(1, 351))
    assert (await rig.row(NEWS_KEY))["forward_id"] == 350
    assert rig.live == []                               # дозагрузка событий не порождает
    rig.client.requests.clear()
    await rig.sync.gap_fill_pass()
    assert rig.offsets() == [(351, -100)]               # нового нет — один запрос и всё


async def test_gap_fill_waits_for_backfill_start_and_covers_empty_chat(rig):
    await rig.enable(U_IVAN)
    await rig.sync.gap_fill_pass()
    assert rig.client.requests == []                    # курсора ещё нет — сначала загрузка вглубь
    await rig.sync.backfill_all()
    row = await rig.row(IVAN_KEY)
    assert (row["backfill_done"], row["forward_id"]) == (True, 0)
    fill(rig.world, IVAN_KEY, 5, 7)
    await rig.sync.gap_fill_pass()
    chat_id = row["chat_id"]
    assert await rig.ids(chat_id) == [5, 6, 7]


async def test_gap_fill_skips_chats_without_news_using_dialog_list(rig):
    for entity, key in ((U_IVAN, IVAN_KEY), (U_MARIA, ("user", MARIA)), (G_FAMILY, GROUP_KEY), (C_NEWS, NEWS_KEY)):
        fill(rig.world, key, 1, 3) if key[0] != "channel" else fill(rig.world, key, 1, 3, post=True)
        await rig.enable(entity)
    await rig.sync.backfill_all()
    rig.client.requests.clear()
    fill(rig.world, NEWS_KEY, 4, 6, post=True)

    async def tops():
        return {IVAN_KEY: 3, ("user", MARIA): 3, GROUP_KEY: 3, NEWS_KEY: 6}

    await rig.sync.gap_fill_pass(tops)
    assert [r.peer.channel_id for r in rig.client.of(History)] == [CHANNEL]

    async def broken():
        raise ConnectionError()

    rig.client.requests.clear()
    await rig.sync.gap_fill_pass(broken)                # список недоступен — опрос по одному
    assert len(rig.client.of(History)) == 4


async def test_gap_fill_picks_up_edits_made_while_offline(rig):
    rig.world.add(msg(1, IVAN_KEY, "к пятнице"))
    chat_id, _ = await rig.enable(U_IVAN)
    await rig.sync.backfill_all()
    rig.world.add(msg(2, IVAN_KEY, "нет, к понедельнику", edit_date=T0 + timedelta(hours=3)))
    await rig.sync.gap_fill_pass()
    assert await rig.ids(chat_id) == [1, 2]


# --- сверка удалений ---

async def test_reconciliation_marks_messages_deleted_while_offline(rig):
    rig.world.add(*[now_msg(i, IVAN_KEY, f"личное {i}") for i in (1, 2, 3)])
    rig.world.add(*[now_msg(i, NEWS_KEY, f"пост {i}", post=True) for i in (1, 2, 3)])
    ivan_chat, _ = await rig.enable(U_IVAN)
    news_chat, _ = await rig.enable(C_NEWS)
    await rig.sync.backfill_all()
    rig.world.remove(IVAN_KEY, 2)
    rig.world.remove(NEWS_KEY, 1, 3)
    rig.client.requests.clear()
    assert await rig.sync.reconcile_pass() == 3
    await rig.events.drain()
    assert await rig.ids(ivan_chat, "AND deleted_at IS NOT NULL") == [2]
    assert await rig.ids(news_chat, "AND deleted_at IS NOT NULL") == [1, 3]
    assert sorted(len(p["message_ids"]) for p in rig.deleted) == [1, 2]
    kinds = sorted(type(r).__module__.rsplit(".", 1)[-1] for r in rig.client.requests)
    assert kinds == ["channels", "messages"]            # канал — своим запросом, с указанием канала
    assert (await rig.row(IVAN_KEY))["reconciled_at"] is not None
    rig.client.requests.clear()
    assert await rig.sync.reconcile_pass() == 0         # только что сверено — повторно не спрашиваем
    assert rig.client.requests == []


async def test_reconciliation_ignores_old_chats_and_never_deletes_on_error(rig):
    fill(rig.world, IVAN_KEY, 1, 3)                      # сообщения 2026-09-12: чат давно не активен
    rig.world.add(now_msg(10, GROUP_KEY, "свежее", sender=MARIA))
    ivan_chat, _ = await rig.enable(U_IVAN)
    group_chat, _ = await rig.enable(G_FAMILY)
    await rig.sync.backfill_all()
    rig.client.requests.clear()
    rig.world.fail[functions.messages.GetMessagesRequest] = [errors.RpcCallFailError(None)]
    assert await rig.sync.reconcile_pass() == 0
    assert len(rig.client.requests) == 1                 # старый чат не спрашивали вовсе
    assert await rig.ids(group_chat, "AND deleted_at IS NOT NULL") == []
    assert (await rig.row(GROUP_KEY))["last_error"] == "RpcCallFailError"


# --- остановка ---

async def test_waiting_is_interrupted_by_shutdown(pool, conn):  # noqa: F811
    account_id = await store.ensure_account(conn, SELF_ID, "Владелец")
    rig = Rig(pool, World(), account_id)
    fill(rig.world, IVAN_KEY, 1, 150)
    await rig.enable(U_IVAN)
    rig.pacer = Pacer(3600.0)                            # настоящие часы и настоящий сон
    rig.sync = rig.new_sync()
    task = asyncio.create_task(rig.sync.backfill_all())
    await asyncio.sleep(0.05)
    assert len(rig.client.of(History)) == 1              # вторая страница ждёт паузу в час
    rig.stop.set()
    with pytest.raises(Stopped):
        await asyncio.wait_for(task, 2)
    assert len(rig.client.of(History)) == 1


async def test_run_loop_catches_up_fills_gaps_and_backfills(rig):
    fill(rig.world, IVAN_KEY, 1, 120)
    chat_id, _ = await rig.enable(U_IVAN)
    wake, reconnected = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(rig.sync.run(wake, reconnected))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if (await rig.row(IVAN_KEY))["backfill_done"]:
            break
    assert await rig.ids(chat_id) == list(range(1, 121)) and rig.client.catch_ups == 1
    fill(rig.world, IVAN_KEY, 121, 130)                  # пропущено за время разрыва
    reconnected.set()
    wake.set()
    for _ in range(200):
        await asyncio.sleep(0.01)
        if len(await rig.ids(chat_id)) == 130:
            break
    assert await rig.ids(chat_id) == list(range(1, 131))
    assert rig.client.catch_ups == 2 and rig.live == []
    rig.stop.set()
    wake.set()
    await asyncio.wait_for(task, 2)
