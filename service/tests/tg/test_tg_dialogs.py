"""Список диалогов аккаунта: загрузка в фоне, продолжение после «подождите», хранение в базе (Р-81).

Что было (опыт с настоящим `iter_dialogs` Telethon и подменённой сетью): список грузится страницами по
100 с паузой 1,5–2,7 с; 4 000 чатов — 41 запрос и больше минуты, а запрос страницы ждал всё это время
(Cloudflare рвёт его через 100 с). FLOOD_WAIT на середине выбрасывал всё загруженное.

Здесь проверяется:
  * настоящий клиент Telethon (`GuardedClient`, сеть подменена на уровне отправителя): 4 000 диалогов,
    FLOOD_WAIT 25 с на 12-й странице — ожидание и та же страница снова; ни одна полученная страница не
    запрашивается повторно, ничего не теряется; ключи доступа и состояние каналов попадают в Telethon;
  * запрос экрана не ждёт загрузку; вторая «Обновить» во время загрузки вторую не начинает; пока список
    грузится, видно уже загруженное и «N из ~M»;
  * после перезапуска сервиса список берётся из базы, в Telegram за ним не ходят;
  * диалог, пропавший из Telegram, уходит из списка, но не из архива; выход из аккаунта и удаление
    аккаунта из архива убирают список.
"""

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from telethon import errors
from telethon.client import users as telethon_users
from telethon.sessions import MemorySession
from telethon.tl import functions, types

from shturman import authority
from shturman.events import Events
from shturman.tg import dialogs as dl
from shturman.tg import sync
from shturman.tg.client import GuardedClient, RequestPolicy, Spacer
from shturman.tg.manager import DIALOGS_TTL, TgManager

from tg_fakes import (C_NEWS, G_FAMILY, IVAN, SELF_ID, U_IVAN, U_MARIA, World, dialogs_loaded, msg, pool,  # noqa: F401
                      start_account, tg_config, user, wait_for)

BASE = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


# --- настоящий клиент Telethon, подменённая сеть ------------------------------------------------------

class Clock:
    """Виртуальное время: паузы не ждут, а сдвигают часы."""

    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += max(0.0, seconds)

    async def pacer_sleep(self, seconds, stop):
        await self.sleep(seconds)
        return stop.is_set()


class Telegram:
    """«Telegram» за отправителем Telethon: список из `n` диалогов (каждый десятый — канал) страницами,
    как `messages.getDialogs`: после диалога `offset_peer`, последнее сообщение — всё раньше по списку."""

    def __init__(self, n: int, *, flood_at: int = 0, seconds: int = 25):
        self.n, self.flood_at, self.seconds = n, flood_at, seconds
        self.entities = [types.Channel(id=500_000 + i, title=f"Канал {i}", photo=types.ChatPhotoEmpty(), date=BASE,
                                       access_hash=11 * i + 1, broadcast=True) if i % 10 == 9
                         else types.User(id=100_000 + i, first_name="Клиент", last_name=str(i), access_hash=7 * i + 1)
                         for i in range(n)]
        self.index = {self.key(e): i for i, e in enumerate(self.entities)}
        self.asked: list[int] = []        # с какого места просили каждую страницу, в том числе отказанную

    @staticmethod
    def key(entity):
        return ("channel" if isinstance(entity, types.Channel) else "user", entity.id)

    def respond(self, request):
        assert isinstance(request, functions.messages.GetDialogsRequest), type(request).__name__
        peer = request.offset_peer
        if isinstance(peer, types.InputPeerEmpty):
            start = 0
        else:
            key = ("channel", peer.channel_id) if isinstance(peer, types.InputPeerChannel) else ("user", peer.user_id)
            start = self.index[key] + 1
        self.asked.append(start)
        if self.flood_at and len(self.asked) == self.flood_at:
            raise errors.FloodWaitError(request, capture=self.seconds)
        chunk = self.entities[start:start + request.limit]
        dialogs, messages = [], []
        for i, entity in enumerate(chunk, start):
            p = (types.PeerChannel(entity.id) if isinstance(entity, types.Channel) else types.PeerUser(entity.id))
            dialogs.append(types.Dialog(
                peer=p, top_message=1000 + i, read_inbox_max_id=0, read_outbox_max_id=0, unread_count=0,
                unread_mentions_count=0, unread_reactions_count=0, unread_poll_votes_count=0,
                notify_settings=types.PeerNotifySettings(), pts=70 + i if isinstance(entity, types.Channel) else None,
                folder_id=1 if i % 7 == 3 else None))
            messages.append(types.Message(id=1000 + i, peer_id=p, message="", date=BASE - timedelta(minutes=i)))
        return types.messages.DialogsSlice(
            count=self.n, dialogs=dialogs, messages=messages,
            chats=[e for e in chunk if isinstance(e, types.Channel)], users=[e for e in chunk if isinstance(e, types.User)])


def real_client(telegram: Telegram, clock: Clock, monkeypatch) -> GuardedClient:
    """`GuardedClient` с настоящим `_call` Telethon: сеть подменена на уровне отправителя, поэтому
    учёт FLOOD_WAIT Telethon и запись ключей доступа в сессию — настоящие."""
    # Те же настройки ожидания, что у настоящей фабрики (client.py): ждёт сервис, а не библиотека.
    client = GuardedClient(MemorySession(), 12345, "0123456789abcdef", policy=RequestPolicy("owner"),
                           flood_sleep_threshold=0, request_retries=3, raise_last_call_error=True)
    client._shturman_dialogs = Spacer(1.5, clock=clock, sleep=clock.sleep, rng=random.Random(2))

    def send(request, ordered=False):
        future = asyncio.get_running_loop().create_future()
        try:
            future.set_result(telegram.respond(request))
        except Exception as exc:  # noqa: BLE001 — так отвечает отправитель Telethon
            future.set_exception(exc)
        return future

    client._sender.send = send
    # Свои часы Telethon для «эта страница ещё под FLOOD_WAIT» — тоже виртуальные.
    monkeypatch.setattr(telethon_users, "time", SimpleNamespace(time=lambda: 1_700_000_000 + clock.now))
    return client


async def fetch(client, clock, *, stop=None):
    pacer = sync.Pacer(3.0, clock=clock, sleep=clock.pacer_sleep, jitter=True, rng=random.Random(1))
    pages, waits = [], []

    async def on_page(rows, expected, done):
        pages.append((rows, expected, done))

    async def on_wait(seconds):
        waits.append(seconds)

    await dl.fetch_all(client, self_id=SELF_ID, pacer=pacer, stop=stop or asyncio.Event(),
                       on_page=on_page, on_wait=on_wait, retry_wait=0.01)
    return pages, waits, pacer


async def test_flood_wait_on_page_12_of_4000_continues_from_the_same_page(monkeypatch):
    telegram, clock = Telegram(4000, flood_at=12, seconds=25), Clock()
    client = real_client(telegram, clock, monkeypatch)
    pages, waits, pacer = await fetch(client, clock)

    assert waits == [25]
    # 40 страниц, пустая 41-я (так Telethon узнаёт конец, когда диалогов ровно 4 000) и один повтор 12-й
    # (с места 1100) — уже полученные страницы снова не запрашивались
    assert len(telegram.asked) == 42
    assert telegram.asked == [*range(0, 1200, 100), *range(1100, 4100, 100)]
    rows = [r for page_rows, _, _ in pages for r in page_rows]
    assert len(pages) == 41 and [done for _, _, done in pages] == [False] * 40 + [True] and pages[-1][0] == []
    assert [r.key for r in rows] == [Telegram.key(e) for e in telegram.entities]       # ничего не потеряно, порядок тот же
    assert {expected for _, expected, _ in pages} == {4000}
    # ожидание — не меньше названного Telegram; паузы между страницами — 1,5–2,7 с
    assert any(s >= 26 for s in clock.slept)
    gaps = [s for s in clock.slept if s < 26]
    assert len(gaps) >= 39 and all(0 < s <= 2.7 + 1e-9 for s in gaps)
    assert clock.now - 1000.0 < 26 + 41 * 2.7
    # сведения для будущей группировки: последнее сообщение, архив, вид
    assert rows[0].top_message == 1000 and rows[0].last_message_at == BASE and rows[3].folder_id == 1
    assert rows[9].chat.type == "private_channel" and rows[0].chat.type == "personal_chat"
    # то, что делал iter_dialogs: ключи доступа — в памяти Telethon и в сессии, состояние каналов — в потоке обновлений
    assert client._mb_entity_cache.get(100_000 + 3999 - 1).hash == 7 * 3998 + 1
    assert client.session.get_input_entity(100_000 + 5).access_hash == 7 * 5 + 1
    assert client._message_box.map[500_000 + 19].pts == 70 + 19
    assert pacer.flood_remaining() == 0


async def test_pages_without_flood_and_history_waits_behind_the_list(monkeypatch):
    telegram, clock = Telegram(250), Clock()
    client = real_client(telegram, clock, monkeypatch)
    pages, waits, pacer = await fetch(client, clock)
    assert waits == [] and telegram.asked == [0, 100, 200] and sum(len(p[0]) for p in pages) == 250
    # каждая страница отодвигает следующий запрос истории: список и история не идут вперемешку
    assert pacer._next_at > clock.now


async def test_stop_interrupts_the_wait_and_nothing_more_is_asked(monkeypatch):
    telegram, clock = Telegram(400, flood_at=2, seconds=3000), Clock()
    client = real_client(telegram, clock, monkeypatch)
    stop = asyncio.Event()

    async def stopping(seconds, stop_event):
        stop_event.set()
        return True

    pacer = sync.Pacer(3.0, clock=clock, sleep=stopping)
    with pytest.raises(sync.Stopped):
        await dl.fetch_all(client, self_id=SELF_ID, pacer=pacer, stop=stop, on_page=_noop_page, on_wait=_noop_wait)
    assert telegram.asked == [0, 100]


async def _noop_page(rows, expected, done):
    return None


async def _noop_wait(seconds):
    return None


async def test_connection_failure_retries_the_same_page(monkeypatch):
    telegram, clock = Telegram(250), Clock()
    client = real_client(telegram, clock, monkeypatch)
    respond, broken = telegram.respond, [2]

    def flaky(request):
        if len(telegram.asked) + 1 in broken:
            broken.remove(len(telegram.asked) + 1)
            telegram.asked.append(-1)
            raise ConnectionError("обрыв")
        return respond(request)

    telegram.respond = flaky
    pages, waits, _ = await fetch(client, clock)
    assert telegram.asked == [0, -1, 100, 200] and sum(len(p[0]) for p in pages) == 250


# --- менеджер: фоновая загрузка, база ----------------------------------------------------------------

@pytest.fixture
async def stage(pool, conn, config):  # noqa: F811
    world = World()
    world.dialogs = [U_IVAN, U_MARIA, G_FAMILY, C_NEWS] + [user(20_000 + i, "Клиент", str(i)) for i in range(246)]
    world.add(msg(1, ("user", IVAN), "привет", sender=IVAN))
    managers = []

    def make():
        manager = TgManager(tg_config(config), pool, Events(), client_factory=world.factory, pacing=0.0)
        managers.append(manager)
        return manager

    st = SimpleNamespace(world=world, make=make, pool=pool)
    try:
        yield st
    finally:
        for manager in managers:
            await manager.stop()


class Gate:
    """Telegram «думает»: страница списка отдаётся только по разрешению."""

    def __init__(self, world):
        self.world, self.open, self.served = world, asyncio.Event(), 0
        original = world.factory

        def factory(*args):
            client = original(*args)
            answer = client._GetDialogsRequest

            async def slow(request):
                await self.open.wait()
                self.served += 1
                return await answer(request)

            client._GetDialogsRequest = slow
            return client

        world.factory = factory


async def test_refresh_answers_at_once_while_the_list_loads_and_a_second_one_starts_nothing(stage):
    gate = Gate(stage.world)
    manager = stage.make()
    rt = await start_account(manager, stage.world, "owner")
    account = rt.account_id

    first = await asyncio.wait_for(manager.list_dialogs(account, refresh=True), 1)   # Telegram молчит — ответ всё равно сразу
    assert first["load"]["running"] is True and first["load"]["state"] == "loading"
    assert first["items"] == [] and first["loaded"] is False
    task = rt.dialogs_load.task
    await wait_for(lambda: len(rt.client.of(functions.messages.GetDialogsRequest)) == 1)   # Telegram спрошен
    for _ in range(3):
        again = await asyncio.wait_for(manager.list_dialogs(account, refresh=True), 1)
        assert again["load"]["running"] is True
    assert rt.dialogs_load.task is task                                       # вторая загрузка не начата
    assert len(rt.client.of(functions.messages.GetDialogsRequest)) == 1

    # первая страница пришла — видно уже загруженное и «N из ~M»
    gate.open.set()
    await wait_for(lambda: gate.served >= 1)
    gate.open.clear()
    await wait_for(lambda: rt.dialogs_load.loaded == 100)
    partial = await manager.list_dialogs(account, limit=500)
    assert partial["load"] == {"state": "loading", "running": True, "loaded": 100, "expected": 250, "wait_seconds": None,
                               "updated_at": None, "error": None}
    assert partial["total"] == 100 and partial["loaded"] is True and partial["age"] is None
    gate.open.set()
    await dialogs_loaded(manager, account, refresh=False)
    done = await manager.list_dialogs(account, limit=500)
    assert done["total"] == 250 and done["load"]["state"] == "done" and done["age"] == 0
    assert stage.world.dialog_pages == [0, 100, 200]


async def test_list_survives_a_restart_without_asking_telegram(stage, conn):
    manager = stage.make()
    rt = await start_account(manager, stage.world, "owner")
    await dialogs_loaded(manager, rt.account_id)
    stored = await conn.fetchval("SELECT count(*) FROM tg_dialogs")
    assert stored == 250
    row = await conn.fetchrow("SELECT * FROM tg_dialogs WHERE tg_id = $1", IVAN)
    assert (row["title"], row["username"], row["chat_type"], row["top_message"], row["position"], row["folder_id"]) == (
        "Иван Петров", "ivan_p", "personal_chat", 1, 0, 0)
    assert row["search"] == "иван петров\nivan_p" and row["last_message_at"] is not None
    await manager.stop()

    calls = stage.world.dialog_calls
    again = stage.make()
    rt = await start_account(again, stage.world, "owner")
    shown = await again.list_dialogs(rt.account_id, limit=10, query="ИВАН")      # как страница настройки
    assert [i["title"] for i in shown["items"]] == ["Иван Петров"] and shown["load"]["state"] == "done"
    assert shown["load"]["running"] is False and shown["age"] is not None and shown["age"] < DIALOGS_TTL
    everything = await again.list_dialogs(rt.account_id, limit=500, cached=True)   # как приложение
    assert everything["total"] == 250
    assert stage.world.dialog_calls == calls and rt.dialogs_load.task is None      # в Telegram не ходили
    # включить чат можно сразу — он есть в списке из базы
    out = await again.set_sync(rt.account_id, enabled=True, chats=[("user", IVAN)])
    assert out == [{"peer_class": "user", "tg_id": IVAN, "enabled": True}]
    # устаревший список страница обновляет сама — в фоне, не задерживая ответ
    await conn.execute("UPDATE tg_dialog_lists SET completed_at = now() - make_interval(secs => $1)", DIALOGS_TTL + 5)
    stale = await again.list_dialogs(rt.account_id, limit=5)
    assert stale["load"]["state"] in ("loading", "done") and stale["total"] == 250   # загрузка уже начата
    await dialogs_loaded(again, rt.account_id, refresh=False)
    assert stage.world.dialog_calls == calls + 3


async def test_vanished_dialog_leaves_the_list_but_not_the_archive(stage, conn):
    manager = stage.make()
    rt = await start_account(manager, stage.world, "owner")
    await dialogs_loaded(manager, rt.account_id)
    await manager.set_sync(rt.account_id, enabled=True, chats=[("user", IVAN)])
    await wait_for(lambda: rt.history.idle and not rt.wake.is_set())
    assert await conn.fetchval("SELECT count(*) FROM messages") == 1
    stage.world.dialogs.remove(U_IVAN)                     # чат удалили в Telegram
    stage.world.dialogs.append(user(30_000, "Новый", "Собеседник"))
    await conn.execute("UPDATE tg_dialog_lists SET completed_at = now() - interval '1 hour'")
    await dialogs_loaded(manager, rt.account_id)
    keys = {(r["peer_class"], r["tg_id"]) for r in await conn.fetch("SELECT peer_class, tg_id FROM tg_dialogs")}
    assert ("user", IVAN) not in keys and ("user", 30_000) in keys and len(keys) == 250
    # в архиве чат и его сообщения остались
    assert await conn.fetchval("SELECT count(*) FROM messages") == 1
    assert await conn.fetchval("SELECT count(*) FROM chats WHERE title = 'Иван Петров'") == 1


async def test_partial_load_failure_keeps_what_was_there(stage, conn):
    manager = stage.make()
    rt = await start_account(manager, stage.world, "owner")
    await dialogs_loaded(manager, rt.account_id)
    stage.world.dialogs.remove(U_MARIA)
    stage.world.dialog_fail = {stage.world.dialog_calls + 2: errors.AuthKeyUnregisteredError(None)}
    assert manager.start_dialogs(rt)
    await asyncio.wait_for(rt.dialogs_load.done.wait(), 5)
    shown = await manager.list_dialogs(rt.account_id, limit=500, cached=True)
    assert shown["load"]["state"] == "failed" and "войдите в аккаунт заново" in shown["load"]["error"]
    # загрузка не дошла до конца — ничего не убрано, прежний список на месте
    assert shown["total"] == 250 and ("user", 2002) in {(i["peer_class"], i["tg_id"]) for i in shown["items"]}


async def test_logout_and_forget_remove_the_list(stage, conn, caplog):
    caplog.set_level(logging.INFO)
    manager = stage.make()
    rt = await start_account(manager, stage.world, "owner")
    account = rt.account_id
    await dialogs_loaded(manager, account)
    assert await conn.fetchval("SELECT count(*) FROM tg_dialogs WHERE account_id = $1", account) == 250
    await manager.logout(account)
    assert await conn.fetchval("SELECT count(*) FROM tg_dialogs") == 0
    assert await conn.fetchval("SELECT count(*) FROM tg_dialog_lists") == 0
    assert await conn.fetchval("SELECT count(*) FROM accounts") == 1             # архив остаётся
    # удаление аккаунта из архива уносит список каскадом (если он почему-то остался)
    await conn.execute("INSERT INTO tg_dialog_lists (account_id, load_id) VALUES ($1, 1)", account)
    await conn.execute(
        """INSERT INTO tg_dialogs (account_id, peer_class, tg_id, chat_type, title, position, load_id)
           VALUES ($1, 'user', 1, 'personal_chat', 'x', 0, 1)""", account)
    async with conn.transaction():
        await manager.forget(conn, account)
    assert await conn.fetchval("SELECT count(*) FROM tg_dialogs") == 0
    assert await conn.fetchval("SELECT count(*) FROM tg_dialog_lists") == 0
    # в журнале — числа, без названий
    assert "Иван" not in caplog.text and "Клиент" not in caplog.text


async def test_flood_through_the_manager_is_one_log_line_and_the_list_completes(stage, conn, caplog):
    caplog.set_level(logging.WARNING)
    manager = stage.make()
    rt = await start_account(manager, stage.world, "owner")

    async def nap(delay, stop):
        await asyncio.sleep(0.01)
        return stop.is_set()

    rt.pacer._sleep = nap
    stage.world.dialog_fail = {stage.world.dialog_calls + 2: errors.FloodWaitError(None, capture=25)}
    assert manager.start_dialogs(rt)
    await wait_for(lambda: rt.dialogs_load.waits == 1)
    shown = await manager.list_dialogs(rt.account_id, limit=500, cached=True)
    assert shown["load"]["state"] == "waiting" and 25 <= shown["load"]["wait_seconds"] <= 27
    assert shown["load"]["loaded"] == 100 and shown["total"] == 100           # уже загруженное не пропало
    rt.pacer._flood_at = 0.0
    await dialogs_loaded(manager, rt.account_id, refresh=False)
    assert stage.world.dialog_pages == [0, 100, 200]
    lines = [r.getMessage() for r in caplog.records if "список чатов" in r.getMessage()]
    assert len(lines) == 1 and "подождать 25 с" in lines[0] and "продолжу с той же страницы" in lines[0]


async def test_history_gap_fill_waits_for_the_running_load_instead_of_starting_another(stage):
    manager = stage.make()
    rt = await start_account(manager, stage.world, "owner")
    assert manager.start_dialogs(rt)
    task = rt.dialogs_load.task
    tops = await manager._tops(rt)
    assert rt.dialogs_load.task is task and tops[("user", IVAN)] == 1 and len(tops) == 250
    calls = stage.world.dialog_calls
    assert await manager._tops(rt) == tops and stage.world.dialog_calls == calls     # свежий — без нового запроса


async def test_http_request_for_the_list_is_not_held_while_telegram_thinks(make_client, config):
    """Запрос экрана (здесь — маршрут внутреннего API, тот же `list_dialogs`) отвечает, пока Telegram
    молчит; раньше он ждал весь список, а прокси перед страницей рвёт запрос через 100 с."""
    world = World()
    world.authorized = False
    world.dialogs = [U_IVAN, U_MARIA] + [user(20_000 + i, "Клиент", str(i)) for i in range(150)]
    gate = Gate(world)
    client, state = await make_client("shturman.api_core", "shturman.tg.service", cfg=tg_config(config))
    assert (await client.put("/api/owner", json={"user_id": SELF_ID, "chat_id": SELF_ID})).status_code == 200
    manager = state.extras["tg"]
    manager.client_factory, manager.pacing = world.factory, 0.0
    with authority.setup_context("test-dialogs", action="/api/tg/login"):
        started = await client.post("/api/tg/login", json={"role": "owner", "confirm_owner": True})
    assert started.status_code == 200, started.text
    world.last.scan.set_result(world.me)
    await wait_for(lambda: "owner" in manager.runtimes and manager.runtimes["owner"].status == "running")
    account = manager.runtimes["owner"].account_id

    got = await asyncio.wait_for(client.get(f"/api/tg/accounts/{account}/dialogs?refresh=1&limit=5"), 2)
    assert got.status_code == 200 and got.json()["load"]["running"] is True and got.json()["items"] == []
    asked = lambda: len(world.last.of(functions.messages.GetDialogsRequest))  # noqa: E731
    await wait_for(lambda: asked() == 1)                  # загрузка спросила Telegram и ждёт ответа
    task = manager.runtimes["owner"].dialogs_load.task
    again = await asyncio.wait_for(client.get(f"/api/tg/accounts/{account}/dialogs?refresh=1&limit=5"), 2)
    assert again.status_code == 200 and again.json()["load"]["running"] is True
    await asyncio.sleep(0.05)
    assert asked() == 1 and manager.runtimes["owner"].dialogs_load.task is task   # вторая загрузка не начата
    gate.open.set()
    await dialogs_loaded(manager, account, refresh=False)
    done = (await client.get(f"/api/tg/accounts/{account}/dialogs?limit=5")).json()
    assert done["total"] == 152 and done["load"]["state"] == "done" and world.dialog_pages == [0, 100]
