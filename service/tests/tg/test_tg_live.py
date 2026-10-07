"""Сессия целиком на подставном клиенте: запуск с диска, живые сообщения, шлюз отправки."""

import asyncio
import logging
from datetime import timedelta

import pytest
from telethon import errors
from telethon.tl import functions, types

from shturman import bridge
from shturman import events as ev
from shturman import store
from shturman.events import Events
from shturman.records import ChatRecord
from shturman.tg import gateway, sync
from shturman.tg.client import RequestNotAllowed, session_path
from shturman.tg.lock import SessionLock
from shturman.tg.manager import TgManager

from conftest import DSN
from tg_fakes import (C_NEWS, C_SUPER, CHANNEL, ENTITIES, G_FAMILY, GROUP, HELPER, IVAN, MARIA, ME,
                      SELF_ID, SUPER, T0, U_BOT, U_IVAN, U_MARIA, U_TELEGRAM, World, msg, now_msg,
                      pool, start_account, tg_config, user, wait_for)  # noqa: F401 — pool это фикстура

IVAN_KEY, GROUP_KEY, NEWS_KEY, SUPER_KEY = ("user", IVAN), ("chat", GROUP), ("channel", CHANNEL), ("channel", SUPER)
SECRET_TEXT = "секретный текст сообщения 4815162342"


class Stage:
    def __init__(self, manager, world, events):
        self.manager, self.world, self.events = manager, world, events
        self.live, self.deleted = [], []
        self.rt = None

        async def on_live(payload):
            self.live.append(payload)

        async def on_deleted(payload):
            self.deleted.append(payload)

        events.subscribe(ev.MESSAGE_LIVE, on_live)
        events.subscribe(ev.MESSAGES_DELETED, on_deleted)

    async def start(self, slot="owner"):
        self.rt = await start_account(self.manager, self.world, slot)
        return self.rt

    @property
    def client(self):
        return self.rt.client

    @property
    def account_id(self):
        return self.rt.account_id

    async def enable(self, *keys):
        result = await self.manager.set_sync(self.account_id, enabled=True, chats=list(keys))
        await self.idle()
        return result

    async def idle(self):
        """Ждёт, пока фоновая работа с историей всё доделает."""
        await wait_for(lambda: self.rt.history is not None and self.rt.history.idle
                       and not self.rt.wake.is_set())
        await self.events.drain()

    async def emit(self, update, entities=ENTITIES):
        await self.client.emit(update, entities)
        await self.events.drain()

    async def rows(self, sql="SELECT tg_message_id FROM messages ORDER BY tg_message_id", *args):
        async with self.manager.pool.acquire() as conn:
            return await conn.fetch(sql, *args)

    async def ids(self, where="true"):
        return [r["tg_message_id"] for r in await self.rows(
            f"SELECT tg_message_id FROM messages WHERE {where} ORDER BY tg_message_id")]


@pytest.fixture
async def stage(pool, conn, config):  # noqa: F811
    world = World()
    world.dialogs = [U_IVAN, U_MARIA, U_BOT, U_TELEGRAM, G_FAMILY, C_SUPER, C_NEWS, ME]
    events = Events()
    manager = TgManager(tg_config(config), pool, events, client_factory=world.factory, pacing=0.0)
    manager._retry_first = 0.02
    st = Stage(manager, world, events)
    try:
        yield st
    finally:
        await manager.stop()


def new_message(message):
    cls = types.UpdateNewChannelMessage if isinstance(message.peer_id, types.PeerChannel) else types.UpdateNewMessage
    return cls(message, 1, 1)


def edited(message):
    cls = types.UpdateEditChannelMessage if isinstance(message.peer_id, types.PeerChannel) else types.UpdateEditMessage
    return cls(message, 1, 1)


# --- запуск ---

async def test_session_on_disk_starts_with_locks_and_registers_account(stage):
    rt = await stage.start()
    assert rt.status == "running" and rt.self_id == SELF_ID
    accounts = await stage.manager.list_accounts()
    assert [(a["role"], a["status"], a["tg_user_id"], a["label"], a["can_send"]) for a in accounts] == \
           [("owner", "running", SELF_ID, "Евгений Тестов", False)]
    # блокировки взяты: второй экземпляр на эту сессию не встанет
    other = SessionLock(session_path(stage.manager.config, "owner"), DSN)
    with pytest.raises(Exception, match="занят"):
        await other.acquire("slot:owner")
    assert stage.client.policy.login is False and stage.client.catch_ups >= 1
    await stage.manager.stop()
    assert not stage.world.last.connected
    await other.acquire("slot:owner")           # после остановки блокировки сняты
    await other.release()


async def test_locked_session_is_not_started(stage):
    path = session_path(stage.manager.config, "owner")
    held = SessionLock(path, DSN)
    await held.acquire("slot:owner")
    rt = await stage.start()
    assert rt.status == "locked" and stage.world.clients == []     # клиент даже не создан
    assert (await stage.manager.list_accounts())[0]["status"] == "locked"
    await held.release()


async def test_revoked_session_stops_without_retries(stage):
    stage.world.authorized = False
    rt = await stage.start()
    await wait_for(lambda: rt.task.done())
    assert rt.status == "unauthorized" and len(stage.world.clients) == 1
    assert rt.lock is None                      # блокировки отпущены


async def test_duplicated_auth_key_is_fatal(stage):
    stage.world.connect_error = errors.AuthKeyDuplicatedError(None)
    rt = await stage.start()
    await wait_for(lambda: rt.task.done())
    assert rt.status == "failed" and "AuthKeyDuplicatedError" in rt.error
    await asyncio.sleep(0.1)
    assert len(stage.world.clients) == 1        # ни одного повторного подключения


async def test_network_failure_is_retried_and_dropped_connection_reconnects(stage):
    stage.world.connect_error = ConnectionError("нет сети")
    rt = await stage.start()
    assert rt.status == "error"
    stage.world.connect_error = None
    await wait_for(lambda: rt.status == "running")
    stage.world.add(msg(1, IVAN_KEY, "до разрыва"))
    await stage.enable(IVAN_KEY)
    catch_ups = stage.client.catch_ups
    stage.world.add(msg(2, IVAN_KEY, "пришло, пока связи не было"))
    stage.client.drop()                         # Telethon исчерпал попытки переподключения
    await wait_for(lambda: rt.status == "running" and stage.client.connected)
    await wait_for(lambda: stage.client.catch_ups > catch_ups)
    await stage.idle()
    assert await stage.ids() == [1, 2]          # пропущенное дочитано из истории
    assert stage.live == []                     # и событий «новое сообщение» по нему нет
    assert len(stage.client.handlers) == 1      # обработчики не задвоились


async def test_auto_reconnect_triggers_catch_up_and_gap_fill(stage):
    stage.world.add(msg(1, IVAN_KEY, "раз"))
    await stage.start()
    await stage.enable(IVAN_KEY)
    before = stage.client.catch_ups
    stage.world.add(msg(2, IVAN_KEY, "два"))
    stage.client.on_reconnect()                 # Telethon переподключился сам
    await wait_for(lambda: stage.client.catch_ups > before)
    await stage.idle()
    assert await stage.ids() == [1, 2]


async def test_session_of_another_account_is_refused(stage, conn):
    rt = await stage.start()
    await stage.manager.stop()
    stage.manager.runtimes.clear()
    stage.world.me = user(SELF_ID + 1, "Кто-то")        # файл подменили
    rt = await stage.start()
    await wait_for(lambda: rt.task.done())
    assert rt.status == "failed" and stage.live == []


# --- живые сообщения ---

async def test_live_messages_are_stored_only_for_selected_chats(stage):
    await stage.start()
    await stage.emit(new_message(msg(1, IVAN_KEY, SECRET_TEXT)))
    assert await stage.ids() == [] and stage.live == []       # чат не выбран — ничего
    async with stage.manager.pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM chats") == 0       # даже названия
    await stage.enable(IVAN_KEY)
    await stage.emit(new_message(msg(2, IVAN_KEY, "привет")))
    await stage.emit(types.UpdateShortMessage(id=3, user_id=IVAN, message="коротко", pts=1, pts_count=1,
                                              date=T0, out=True))
    await stage.emit(new_message(msg(4, GROUP_KEY, "в невыбранной группе", sender=MARIA)))
    rows = await stage.rows("SELECT tg_message_id, is_outgoing, sources, sender_name FROM messages ORDER BY 1")
    assert [(r["tg_message_id"], r["is_outgoing"], r["sources"]) for r in rows] == \
           [(2, False, ["session"]), (3, True, ["session"])]
    assert rows[0]["sender_name"] == "Иван Петров"
    chat_id = (await stage.manager.sync_status(stage.account_id))["chats"][0]["chat_id"]
    assert [(p["chat_id"], p["source"], p["outgoing"], p["edited"], p["via_bot"]) for p in stage.live] == \
           [(chat_id, "session", False, False, False), (chat_id, "session", True, False, False)]
    assert all(p["account_id"] == stage.account_id for p in stage.live)
    ids = {r["id"] for r in await stage.rows("SELECT id FROM messages")}
    assert {p["message_id"] for p in stage.live} == ids       # идентификаторы строк архива


async def test_service_messages_and_via_bot_flag(stage):
    await stage.start()
    await stage.enable(GROUP_KEY)
    service = types.MessageService(id=5, peer_id=types.PeerChat(GROUP), date=T0,
                                   from_id=types.PeerUser(MARIA), action=types.MessageActionChatAddUser([IVAN]))
    await stage.emit(new_message(service))
    await stage.emit(new_message(msg(6, GROUP_KEY, "прогноз", sender=MARIA, via_bot_id=2100)))
    await stage.emit(types.UpdateShortChatMessage(id=7, from_id=MARIA, chat_id=GROUP, message="коротко",
                                                  pts=1, pts_count=1, date=T0))
    rows = await stage.rows("SELECT tg_message_id, kind, service_action FROM messages ORDER BY 1")
    assert [tuple(r) for r in rows] == [(5, "service", "invite_members"), (6, "message", None), (7, "message", None)]
    assert [p["via_bot"] for p in stage.live] == [False, True, False]


async def test_edit_versions_text_and_reaction_does_not(stage):
    await stage.start()
    await stage.enable(IVAN_KEY)
    await stage.emit(new_message(msg(1, IVAN_KEY, "к пятнице")))
    stage.live.clear()
    # реакция: Telegram присылает «изменение» без смены текста
    await stage.emit(edited(msg(1, IVAN_KEY, "к пятнице", edit_date=T0 + timedelta(hours=1), edit_hide=True)))
    assert stage.live == []
    assert (await stage.rows("SELECT count(*) AS n FROM message_versions"))[0]["n"] == 0
    await stage.emit(edited(msg(1, IVAN_KEY, "к понедельнику", edit_date=T0 + timedelta(hours=2))))
    row = (await stage.rows("SELECT text, edited_at FROM messages"))[0]
    assert row["text"] == "к понедельнику" and row["edited_at"] == T0 + timedelta(hours=2)
    versions = await stage.rows("SELECT text FROM message_versions")
    assert [v["text"] for v in versions] == ["к пятнице"]
    assert [(p["edited"], p["outgoing"]) for p in stage.live] == [(True, False)]
    # правка сообщения, которого в архиве ещё не было
    stage.live.clear()
    await stage.emit(edited(msg(9, IVAN_KEY, "старое, но изменённое", edit_date=T0 + timedelta(hours=3))))
    assert await stage.ids() == [1, 9] and [p["edited"] for p in stage.live] == [True]


async def test_deletes_with_and_without_chat(stage):
    await stage.start()
    await stage.enable(IVAN_KEY, GROUP_KEY, NEWS_KEY)
    await stage.emit(new_message(msg(10, IVAN_KEY, "личное")))
    await stage.emit(new_message(msg(11, IVAN_KEY, "ещё")))
    await stage.emit(new_message(msg(12, GROUP_KEY, "в группе", sender=MARIA)))
    await stage.emit(new_message(msg(10, NEWS_KEY, "пост канала с тем же номером", post=True)))
    await stage.emit(new_message(msg(11, NEWS_KEY, "второй пост", post=True)))
    # личные чаты и обычные группы: удаление приходит без чата
    await stage.emit(types.UpdateDeleteMessages(messages=[10, 12, 999], pts=1, pts_count=1))
    deleted = await stage.rows(
        """SELECT p.class, m.tg_message_id FROM messages m JOIN chats c ON c.id = m.chat_id
           JOIN peers p ON p.id = c.peer_id WHERE m.deleted_at IS NOT NULL ORDER BY 1, 2""")
    assert [tuple(r) for r in deleted] == [("chat", 12), ("user", 10)]      # пост канала №10 не тронут
    assert len(stage.deleted) == 1 and len(stage.deleted[0]["message_ids"]) == 2
    # канал: удаление приходит с каналом
    await stage.emit(types.UpdateDeleteChannelMessages(channel_id=CHANNEL, messages=[11], pts=1, pts_count=1))
    assert await stage.ids("deleted_at IS NOT NULL") == [10, 11, 12]
    assert len(stage.deleted) == 2
    # удаление в невыбранном канале не трогает ничего
    await stage.emit(types.UpdateDeleteChannelMessages(channel_id=SUPER, messages=[10], pts=1, pts_count=1))
    assert len(stage.deleted) == 2
    assert (await stage.rows("SELECT text FROM messages WHERE tg_message_id = 12"))[0]["text"] == "в группе"


async def test_excluded_and_disabled_chats_drop_live_updates(stage):
    await stage.start()
    await stage.enable(IVAN_KEY, GROUP_KEY)
    async with stage.manager.pool.acquire() as conn:
        await conn.execute(
            "UPDATE chats SET excluded = true WHERE peer_id = (SELECT id FROM peers WHERE class = 'user' AND tg_id = $1)",
            IVAN)
    await stage.emit(new_message(msg(1, IVAN_KEY, SECRET_TEXT)))
    assert await stage.ids() == [] and stage.live == []
    await stage.manager.set_sync(stage.account_id, enabled=False, chats=[GROUP_KEY])
    await stage.emit(new_message(msg(2, GROUP_KEY, "после выключения", sender=MARIA)))
    assert await stage.ids() == [] and stage.live == []
    # служебный чат Telegram включить нельзя; сообщения из него не сохраняются
    result = await stage.manager.set_sync(stage.account_id, enabled=True, chats=[("user", 777000)])
    assert result[0]["enabled"] is False and result[0]["excluded"] is True
    await stage.emit(new_message(msg(3, ("user", 777000), "Login code: 12345")))
    assert await stage.ids() == []


async def test_auto_enable_takes_only_chats_that_appeared_later(stage):
    stage.world.dialogs = [U_IVAN, G_FAMILY]            # эти чаты уже были
    await stage.start()
    newcomer = user(2500, "Новый", "Собеседник")
    await stage.emit(new_message(msg(1, ("user", 2500), "здравствуйте")), [newcomer, ME])
    assert await stage.ids() == []                      # по умолчанию настройка выключена
    await stage.manager.set_options(stage.account_id, auto_personal=True, auto_groups=None)
    await stage.emit(new_message(msg(2, IVAN_KEY, "старый чат")))
    await stage.emit(new_message(msg(3, ("user", 2500), "новый чат")), [newcomer, ME])
    await stage.emit(new_message(msg(4, ("user", 2100), "от бота")))             # бот — не личный чат
    await stage.emit(new_message(msg(5, SUPER_KEY, "новая группа", sender=MARIA)))  # группы не включали
    await stage.idle()
    assert await stage.ids() == [3]
    status = await stage.manager.sync_status(stage.account_id)
    assert [(c["tg_id"], c["auto_enabled"]) for c in status["chats"]] == [(2500, True)]
    # владелец выключил чат — настройка его обратно не включит
    await stage.manager.set_sync(stage.account_id, enabled=False, chats=[("user", 2500)])
    await stage.emit(new_message(msg(6, ("user", 2500), "ещё")), [newcomer, ME])
    assert await stage.ids() == [3]
    await stage.manager.set_options(stage.account_id, auto_personal=None, auto_groups=True)
    await stage.emit(new_message(msg(7, SUPER_KEY, "закрытая супергруппа", sender=MARIA)))
    await stage.emit(new_message(msg(8, NEWS_KEY, "канал", post=True)))          # каналы сами не включаются
    await stage.idle()
    assert await stage.ids() == [3, 7]


async def test_live_chat_title_is_refreshed(stage):
    await stage.start()
    await stage.enable(IVAN_KEY)
    renamed = user(IVAN, "Иван", "Петров-Водкин", username="ivan_new")
    await stage.emit(new_message(msg(1, IVAN_KEY, "я сменил имя")), [renamed, ME])
    row = (await stage.rows("SELECT name, username FROM peers WHERE tg_id = $1", IVAN))[0]
    assert (row["name"], row["username"]) == ("Иван Петров-Водкин", "ivan_new")


async def test_nothing_secret_or_textual_is_logged(stage, caplog):
    caplog.set_level(logging.DEBUG)
    stage.world.add(msg(1, IVAN_KEY, SECRET_TEXT))
    await stage.start()
    await stage.enable(IVAN_KEY)
    await stage.emit(new_message(msg(2, IVAN_KEY, SECRET_TEXT + " вживую")))
    await stage.emit(types.UpdateDeleteMessages(messages=[2], pts=1, pts_count=1))

    async def broken(*a, **kw):
        raise RuntimeError(SECRET_TEXT)

    stage.rt.live.save = broken
    await stage.emit(new_message(msg(3, IVAN_KEY, SECRET_TEXT)))     # сбой обработки тоже не выдаёт текст
    assert await stage.ids() == [1, 2]
    assert "не обработано" in caplog.text and SECRET_TEXT not in caplog.text and "4815162342" not in caplog.text


async def test_owner_role_reads_with_allowed_requests_only(stage):
    """Весь путь чтения под ролью owner: подставной клиент сверяет каждый запрос с перечнем."""
    stage.world.add(*[now_msg(i, IVAN_KEY, f"м{i}") for i in range(1, 6)])
    stage.world.add(*[now_msg(i, NEWS_KEY, f"п{i}", post=True) for i in range(1, 4)])
    await stage.start()
    await stage.manager.list_dialogs(stage.account_id)
    await stage.enable(IVAN_KEY, NEWS_KEY)
    await stage.rt.history.gap_fill_pass()
    stage.world.remove(IVAN_KEY, 2)
    async with stage.manager.pool.acquire() as conn:
        await conn.execute("UPDATE tg_sync_chats SET reconciled_at = NULL")
    assert await stage.rt.history.reconcile_pass() == 1
    names = {type(r).__name__ for r in stage.client.requests}
    assert names == {"GetUsersRequest", "GetDialogsRequest", "GetHistoryRequest", "GetMessagesRequest"}
    assert not any("Read" in n or "Send" in n or "Typing" in n for n in names)


async def test_real_telethon_dispatch_reaches_the_handler(pool, conn, monkeypatch):  # noqa: F811
    """Настоящий клиент Telethon (без сети): его раздача обновлений доводит до обработчика и
    служебное сообщение, и «короткое» обновление — то, что events.NewMessage пропустил бы."""
    from telethon import utils
    from telethon.sessions import MemorySession

    from shturman.tg.client import GuardedClient, RequestPolicy
    from shturman.tg.live import LiveIngest
    from shturman.tg.normalize import chat_record

    account_id = await store.ensure_account(conn, SELF_ID, "Владелец")
    chat_id, _ = await sync.enable_chat(conn, account_id, chat_record(G_FAMILY, self_id=SELF_ID))
    ivan_chat, _ = await sync.enable_chat(conn, account_id, chat_record(U_IVAN, self_id=SELF_ID))
    events = Events()
    client = GuardedClient(MemorySession(), 12345, "0123456789abcdef", policy=RequestPolicy("owner"))
    client._mb_entity_cache.set_self_user(SELF_ID, False, 1)      # «кто я» известно — в сеть не пойдёт
    live = LiveIngest(pool=pool, events=events, account_id=account_id, self_id=SELF_ID, wake=asyncio.Event())
    await live.reload()
    live.register(client)
    updates = [
        types.UpdateNewMessage(types.MessageService(
            id=5, peer_id=types.PeerChat(GROUP), date=T0, from_id=types.PeerUser(MARIA),
            action=types.MessageActionChatAddUser([IVAN])), 1, 1),
        types.UpdateShortMessage(id=6, user_id=IVAN, message="коротко", pts=2, pts_count=1, date=T0),
        types.UpdateEditMessage(msg(6, IVAN_KEY, "коротко, но иначе", edit_date=T0 + timedelta(minutes=1)), 3, 1),
        types.UpdateDeleteMessages(messages=[5], pts=4, pts_count=1),
        types.UpdateReadHistoryInbox(peer=types.PeerUser(IVAN), max_id=6, still_unread_count=0, pts=5, pts_count=1),
    ]
    for update in updates:
        update._entities = {utils.get_peer_id(e): e for e in ENTITIES}
        await client._dispatch_update(update)
    rows = await conn.fetch("SELECT tg_message_id, kind, text, deleted_at IS NOT NULL AS gone FROM messages ORDER BY 1")
    assert [tuple(r) for r in rows] == [(5, "service", "", True), (6, "message", "коротко, но иначе", False)]
    assert await conn.fetchval("SELECT count(*) FROM message_versions") == 1


# --- шлюз отправки ---

async def test_owner_account_cannot_send_by_any_path(stage):
    await stage.start("owner")
    await stage.enable(IVAN_KEY)
    assert stage.manager.can_send(stage.account_id) is False
    requests_before = list(stage.client.requests)
    with pytest.raises(gateway.SendForbidden):
        await stage.manager.send_text(stage.account_id, "user", IVAN, "привет от владельца")
    await stage.manager.set_typing(stage.account_id, "user", IVAN, True)      # молча ничего не делает
    assert stage.client.requests == requests_before                           # к Telegram не ушло ничего
    # и даже если соседний модуль добрался бы до клиента — перечень роли не пропустит
    peer = types.InputPeerUser(IVAN, 1)
    for request in (functions.messages.SendMessageRequest(peer, "в обход шлюза"),
                    functions.messages.ReadHistoryRequest(peer, 5),
                    functions.messages.DeleteMessagesRequest([1]),
                    functions.messages.SetTypingRequest(peer, types.SendMessageTypingAction())):
        with pytest.raises(RequestNotAllowed):
            await stage.client(request)
    assert stage.client.requests == requests_before and stage.live == []
    # остановленный аккаунт владельца — тоже «нельзя», а не «недоступен»
    account_id = stage.account_id
    await stage.manager.stop()
    with pytest.raises(gateway.SendForbidden):
        await stage.manager.send_text(account_id, "user", IVAN, "привет")


async def test_assistant_sends_text_through_gateway(stage):
    stage.world.me = HELPER
    await stage.start("assistant")
    assert stage.manager.can_send(stage.account_id) is True
    await stage.enable(IVAN_KEY)
    message_id = await stage.manager.send_text(stage.account_id, "user", IVAN, "Добрый день!", reply_to_tg_id=7)
    await stage.events.drain()
    sent = stage.client.of(functions.messages.SendMessageRequest)
    assert len(sent) == 1 and sent[0].message == "Добрый день!" and sent[0].reply_to.reply_to_msg_id == 7
    assert sent[0].entities is None and sent[0].peer.user_id == IVAN
    row = (await stage.rows("SELECT tg_message_id, text, is_outgoing, reply_to_tg_id, sources FROM messages"))[0]
    assert tuple(row) == (message_id, "Добрый день!", True, 7, ["session"])
    assert [(p["outgoing"], p["source"]) for p in stage.live] == [(True, "session")]
    # в невыбранный чат отправить можно, но в архив это не попадает
    stage.live.clear()
    second = await stage.manager.send_text(stage.account_id, "channel", SUPER, "В группу")
    assert second > message_id and await stage.ids() == [message_id] and stage.live == []
    # «печатает…»
    await stage.manager.set_typing(stage.account_id, "user", IVAN, True)
    await stage.manager.set_typing(stage.account_id, "user", IVAN, False)
    actions = [type(r.action).__name__ for r in stage.client.of(functions.messages.SetTypingRequest)]
    assert actions == ["SendMessageTypingAction", "SendMessageCancelAction"]
    # прочитанным ничего не отмечено
    assert not any("Read" in type(r).__name__ for r in stage.client.requests)


async def test_gateway_errors(stage):
    stage.world.me = HELPER
    await stage.start("assistant")
    account_id = stage.account_id
    stage.world.fail[functions.messages.SendMessageRequest] = [errors.FloodWaitError(None, 33)]
    with pytest.raises(gateway.FloodWait) as exc:
        await stage.manager.send_text(account_id, "user", IVAN, "раз")
    assert exc.value.seconds == 33
    assert len(stage.client.of(functions.messages.SendMessageRequest)) == 1       # сам не повторяет
    with pytest.raises(ValueError):
        await stage.manager.send_text(account_id, "user", IVAN, "я" * 4097)
    with pytest.raises(ValueError):
        await stage.manager.send_text(account_id, "user", IVAN, "   ")
    stage.world.fail[functions.messages.SetTypingRequest] = [errors.RpcCallFailError(None)]
    await stage.manager.set_typing(account_id, "user", IVAN, True)                # ошибка проглочена
    with pytest.raises(gateway.SendForbidden):
        await stage.manager.send_text(987654, "user", IVAN, "неизвестный аккаунт")
    await stage.manager.pause(account_id)
    assert stage.manager.can_send(account_id) is False
    with pytest.raises(gateway.AccountUnavailable):
        await stage.manager.send_text(account_id, "user", IVAN, "на паузе")


async def test_owner_account_cannot_be_started_in_assistant_slot(stage, conn):
    """Основной аккаунт не становится помощником ни через архив, ни через управляющий чат."""
    await store.ensure_account(conn, SELF_ID, "Владелец", "owner")      # известен архиву как owner
    rt = await stage.start("assistant")                                 # а сессия лежит в слоте помощника
    await wait_for(lambda: rt.task.done())
    assert rt.status == "failed" and "основной аккаунт" in rt.error
    assert stage.manager.can_send(1) is False
    await stage.manager.stop()
    stage.manager.runtimes.clear()
    await conn.execute("DELETE FROM accounts")
    await bridge.set_owner(conn, SELF_ID, SELF_ID)                      # владелец управляющего чата
    rt = await stage.start("assistant")
    await wait_for(lambda: rt.task.done())
    assert rt.status == "failed" and "основной аккаунт" in rt.error.lower()
    assert await conn.fetchval("SELECT count(*) FROM accounts") == 0


async def test_import_and_session_share_rows(stage, conn):
    """Чат уже загружен из экспорта; сессия дополняет те же строки."""
    account_id = await store.ensure_account(conn, SELF_ID, "Владелец")
    chat_id, _ = await store.ensure_chat(conn, account_id, ChatRecord("user", IVAN, "personal_chat", "Иван Петров"))
    from shturman.tg.normalize import index_entities, message_record
    old = msg(1, IVAN_KEY, "из экспорта")
    await store.upsert_messages(conn, [(chat_id, message_record(old, index_entities(ENTITIES), self_id=SELF_ID))],
                                source="import", owner_tg_id=SELF_ID)
    stage.world.add(old, msg(2, IVAN_KEY, "новее экспорта"))
    await stage.start()
    assert stage.account_id == account_id
    await stage.enable(IVAN_KEY)
    rows = await stage.rows("SELECT tg_message_id, sources FROM messages ORDER BY 1")
    assert [tuple(r) for r in rows] == [(1, ["import", "session"]), (2, ["session"])]
    assert (await stage.rows("SELECT count(*) AS n FROM message_versions"))[0]["n"] == 0
    assert isinstance(sync.SWEEP_EVERY, int)


# --- реакции, исключение чата ---

async def test_reaction_never_marks_message_edited_and_real_edit_still_applies(stage):
    """Последовательность: новое → реакция → настоящая правка → реакция."""
    await stage.start()
    await stage.enable(IVAN_KEY)
    hour = lambda n: T0 + timedelta(hours=n)  # noqa: E731

    async def row():
        r = (await stage.rows("SELECT text, edited_at FROM messages WHERE tg_message_id = 1"))[0]
        versions = [v["text"] for v in await stage.rows("SELECT text FROM message_versions ORDER BY id")]
        return r["text"], r["edited_at"], versions

    await stage.emit(new_message(msg(1, IVAN_KEY, "к пятнице")))
    stage.live.clear()
    await stage.emit(edited(msg(1, IVAN_KEY, "к пятнице", edit_date=hour(1), edit_hide=True)))       # реакция
    assert await row() == ("к пятнице", None, []) and stage.live == []
    await stage.emit(edited(msg(1, IVAN_KEY, "к понедельнику", edit_date=hour(2))))                  # правка
    assert await row() == ("к понедельнику", hour(2), ["к пятнице"])
    assert [(p["edited"], p["outgoing"]) for p in stage.live] == [(True, False)]
    stage.live.clear()
    await stage.emit(edited(msg(1, IVAN_KEY, "к понедельнику", edit_date=hour(3), edit_hide=True)))  # реакция
    assert await row() == ("к понедельнику", hour(2), ["к пятнице"]) and stage.live == []
    await stage.emit(edited(msg(1, IVAN_KEY, "ко вторнику", edit_date=hour(4))))                     # ещё правка
    assert await row() == ("ко вторнику", hour(4), ["к пятнице", "к понедельнику"])
    assert len(stage.live) == 1
    # реакция на сообщение, которого в архиве не было: запись появляется без отметки и без события
    stage.live.clear()
    await stage.emit(edited(msg(7, IVAN_KEY, "старое", edit_date=hour(5), edit_hide=True)))
    assert tuple((await stage.rows("SELECT text, edited_at FROM messages WHERE tg_message_id = 7"))[0]) == ("старое", None)
    assert stage.live == []


async def test_missed_real_edit_hidden_behind_reaction_is_still_applied(stage):
    """Правку пропустили (сервис не работал), потом пришла реакция: текст в ней уже новый."""
    await stage.start()
    await stage.enable(IVAN_KEY)
    await stage.emit(new_message(msg(1, IVAN_KEY, "к пятнице")))
    stage.live.clear()
    await stage.emit(edited(msg(1, IVAN_KEY, "к понедельнику", edit_date=T0 + timedelta(hours=3), edit_hide=True)))
    row = (await stage.rows("SELECT text, edited_at FROM messages"))[0]
    assert (row["text"], row["edited_at"]) == ("к понедельнику", T0 + timedelta(hours=3))
    assert [v["text"] for v in await stage.rows("SELECT text FROM message_versions")] == ["к пятнице"]
    assert [p["edited"] for p in stage.live] == [True]
    # то же при чтении истории: в архиве старый текст, в истории — новый под скрытой правкой
    stage.world.add(msg(1, IVAN_KEY, "к среде", edit_date=T0 + timedelta(hours=6), edit_hide=True),
                    msg(2, IVAN_KEY, "с реакцией", edit_date=T0 + timedelta(hours=6), edit_hide=True))
    async with stage.manager.pool.acquire() as conn:
        await conn.execute("UPDATE tg_sync_chats SET backfill_done = false, backfill_before = NULL")
    stage.live.clear()
    await stage.rt.history.backfill_all()
    rows = await stage.rows("SELECT tg_message_id, text, edited_at FROM messages ORDER BY 1")
    assert [tuple(r) for r in rows] == [(1, "к среде", T0 + timedelta(hours=6)), (2, "с реакцией", None)]
    assert stage.live == []


async def test_excluding_chat_stops_its_sync_at_once(stage):
    stage.world.add(*[msg(i, NEWS_KEY, f"пост {i}", post=True) for i in range(1, 251)])
    stage.world.add(msg(1, IVAN_KEY, "личное"))
    await stage.start()
    await stage.enable(IVAN_KEY)
    # канал включён, первая страница истории уже запрошена — и тут владелец исключает чат
    real = stage.client._GetHistoryRequest
    gate, entered = asyncio.Event(), asyncio.Event()

    async def slow(request):
        if getattr(request.peer, "channel_id", None) == CHANNEL:
            entered.set()
            await gate.wait()
        return await real(request)

    stage.client._GetHistoryRequest = slow
    await stage.manager.set_sync(stage.account_id, enabled=True, chats=[NEWS_KEY])
    await asyncio.wait_for(entered.wait(), 5)
    chat_id = (await stage.rows(
        "SELECT chat_id FROM tg_sync_chats WHERE peer_class = 'channel' AND tg_id = $1", CHANNEL))[0]["chat_id"]
    async with stage.manager.pool.acquire() as conn:
        await conn.execute("UPDATE chats SET excluded = true WHERE id = $1", chat_id)
    stage.events.publish(ev.CHAT_EXCLUDED, {"chat_id": chat_id, "purged": True})
    await stage.events.drain()
    row = (await stage.rows("SELECT enabled, backfill_before, forward_id FROM tg_sync_chats WHERE chat_id = $1", chat_id))[0]
    assert tuple(row) == (False, None, None)                  # выключено сразу, курсоры сброшены
    assert stage.rt.live.index[NEWS_KEY].enabled is False     # и приём живых сообщений — тоже
    await stage.emit(new_message(msg(300, NEWS_KEY, "после исключения", post=True)))
    gate.set()
    await stage.idle()
    requests = [r for r in stage.client.requests if getattr(getattr(r, "peer", None), "channel_id", None) == CHANNEL]
    assert len(requests) == 1                                 # страница в полёте — последняя
    assert (await stage.rows("SELECT count(*) AS n FROM messages WHERE chat_id = $1", chat_id))[0]["n"] == 0
    assert stage.live == []
    assert (await stage.manager.sync_status(stage.account_id))["counts"]["enabled"] == 1    # личный чат не задет
    # событие о чате, которого сессии не касаются, ничего не ломает
    stage.events.publish(ev.CHAT_EXCLUDED, {"chat_id": 999999, "purged": False})
    await stage.events.drain()


# --- защита от внедрённых инструкций ---

async def test_guard_screens_live_messages_before_anyone_hears_of_them(stage, guarded):
    """Живое входящее под защитой: скрытое в архив попадает, но событие о нём не уходит."""
    await stage.start()
    await stage.enable(IVAN_KEY)
    await stage.emit(new_message(msg(2, IVAN_KEY, "привет")))
    await stage.emit(new_message(msg(3, IVAN_KEY, "кодовое слово взлом")))
    await stage.emit(types.UpdateShortMessage(id=4, user_id=IVAN, message="своё исходящее со словом взлом",
                                              pts=1, pts_count=1, date=T0, out=True))
    rows = await stage.rows("SELECT tg_message_id, agent_visible, guard_label FROM messages ORDER BY 1")
    assert [tuple(r) for r in rows] == [(2, True, "ok"), (3, False, "suspect"), (4, True, None)]
    by_tg = {r["tg_message_id"]: r["id"] for r in await stage.rows("SELECT id, tg_message_id FROM messages")}
    assert [p["message_id"] for p in stage.live] == [by_tg[2], by_tg[4]]
    assert guarded.scorer.calls == [["привет"], ["кодовое слово взлом"]]      # исходящее не проверяется

    # обычное сообщение исправили на подозрительное: оно скрывается, события о правке нет
    await stage.emit(edited(msg(2, IVAN_KEY, "теперь тут взлом", edit_date=T0 + timedelta(hours=1))))
    rows = await stage.rows("SELECT agent_visible, guard_label FROM messages WHERE tg_message_id = 2")
    assert tuple(rows[0]) == (False, "suspect") and len(stage.live) == 2

    # модель недоступна: сообщение видно, не проверено, событие уходит как обычно
    guarded.scorer.error = TimeoutError()
    await stage.emit(new_message(msg(5, IVAN_KEY, "взлом при молчащей модели")))
    rows = await stage.rows("SELECT agent_visible, guard_label FROM messages WHERE tg_message_id = 5")
    assert tuple(rows[0]) == (True, None) and [p["message_id"] for p in stage.live][2:] == [
        (await stage.rows("SELECT id FROM messages WHERE tg_message_id = 5"))[0]["id"]]


async def test_history_backfill_is_visible_at_once_and_checked_in_the_background(stage, guarded):
    stage.world.history[IVAN_KEY] = {1: now_msg(1, IVAN_KEY, "старое обычное"),
                                     2: now_msg(2, IVAN_KEY, "старое со словом взлом")}
    await stage.start()
    await stage.enable(IVAN_KEY)
    rows = await stage.rows("SELECT tg_message_id, agent_visible, guard_label FROM messages ORDER BY 1")
    assert [tuple(r) for r in rows] == [(1, True, None), (2, True, None)]     # догрузка никого не ждёт
    assert guarded.scorer.calls == [] and stage.live == []
    await guarded.guard.sweep()
    rows = await stage.rows("SELECT tg_message_id, agent_visible, guard_label FROM messages ORDER BY 1")
    assert [tuple(r) for r in rows] == [(1, True, "ok"), (2, False, "suspect")]
