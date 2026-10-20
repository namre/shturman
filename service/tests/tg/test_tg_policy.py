"""Перечень разрешённых запросов: основной аккаунт владельца — чтение и одна запись: облачный черновик в поле
ввода чата по нажатию владельца «В чат» (Р-105), только взведённым и только простым текстом."""

import inspect
import pkgutil
import importlib

import pytest
from telethon import TelegramClient
from telethon.sessions import MemorySession
from telethon.tl import functions, types
from telethon.tl.tlobject import TLRequest

from shturman.tg import client as tg_client
from shturman.tg.client import GuardedClient, RequestNotAllowed, RequestPolicy

PEER = types.InputPeerUser(2001, 1)
CHANNEL = types.InputChannel(4001, 1)

WRITES = [
    functions.messages.SendMessageRequest(PEER, "привет"),
    functions.messages.SendMediaRequest(PEER, types.InputMediaEmpty(), "x"),
    functions.messages.ForwardMessagesRequest(PEER, [1], PEER),
    functions.messages.EditMessageRequest(PEER, 1, message="x"),
    functions.messages.DeleteMessagesRequest([1]),
    functions.channels.DeleteMessagesRequest(CHANNEL, [1]),
    functions.messages.DeleteHistoryRequest(PEER, 0),
    functions.messages.ReadHistoryRequest(PEER, 5),
    functions.channels.ReadHistoryRequest(CHANNEL, 5),
    functions.messages.ReadMentionsRequest(PEER),
    functions.messages.SetTypingRequest(PEER, types.SendMessageTypingAction()),
    functions.messages.SendReactionRequest(PEER, 1),
    functions.account.UpdateStatusRequest(offline=False),
    functions.account.UpdateProfileRequest(first_name="x"),
    functions.contacts.BlockRequest(PEER),
    functions.channels.JoinChannelRequest(CHANNEL),
    functions.channels.LeaveChannelRequest(CHANNEL),
    functions.auth.SendCodeRequest("+70000000000", 1, "x", types.CodeSettings()),
    functions.auth.AcceptLoginTokenRequest(b"token"),
    functions.account.DeleteAccountRequest("x"),
    # папки Telegram читаются, но не меняются (Р-82)
    functions.messages.UpdateDialogFilterRequest(2),
    functions.messages.UpdateDialogFiltersOrderRequest([2]),
]
READS = [
    functions.updates.GetStateRequest(),
    functions.updates.GetDifferenceRequest(pts=1, date=None, qts=0),
    functions.updates.GetChannelDifferenceRequest(CHANNEL, types.ChannelMessagesFilterEmpty(), 1, 100),
    functions.users.GetUsersRequest([types.InputUserSelf()]),
    functions.messages.GetDialogsRequest(None, 0, types.InputPeerEmpty(), 100, 0),
    functions.messages.GetDialogFiltersRequest(),        # папки Telegram — только их описание (Р-82)
    functions.messages.GetHistoryRequest(PEER, 0, None, 0, 100, 0, 0, 0),
    functions.messages.GetMessagesRequest([types.InputMessageID(1)]),
    functions.channels.GetMessagesRequest(CHANNEL, [types.InputMessageID(1)]),
    functions.help.GetConfigRequest(),
    functions.auth.LogOutRequest(),
]
LOGIN = [
    functions.auth.ExportLoginTokenRequest(1, "x", []),
    functions.auth.ImportLoginTokenRequest(b"t"),
    functions.account.GetPasswordRequest(),
]


@pytest.mark.parametrize("request_", WRITES, ids=lambda r: type(r).__name__)
def test_owner_role_refuses_everything_that_writes(request_):
    policy = RequestPolicy("owner")
    with pytest.raises(RequestNotAllowed) as exc:
        policy.check(request_)
    assert exc.value.role == "owner" and type(request_).__name__ in str(exc.value)


@pytest.mark.parametrize("request_", READS, ids=lambda r: type(r).__name__)
def test_reads_and_telethon_internals_pass_for_both_roles(request_):
    RequestPolicy("owner").check(request_)
    RequestPolicy("assistant").check(request_)


def test_assistant_may_send_text_and_typing_and_nothing_else():
    policy = RequestPolicy("assistant")
    policy.check(functions.messages.SendMessageRequest(PEER, "привет"))
    policy.check(functions.messages.SetTypingRequest(PEER, types.SendMessageTypingAction()))
    for request_ in WRITES[1:10] + WRITES[11:]:
        with pytest.raises(RequestNotAllowed):
            policy.check(request_)


@pytest.mark.parametrize("sending", [True, False])
@pytest.mark.parametrize("login", [True, False])
def test_owner_role_never_sends_whatever_the_switches(sending, login):
    """Репетиция ответов (Р-85) работает на основном аккаунте: он остаётся только на чтение при любом
    положении выключателей — главного выключателя отправки (`SHTURMAN_SENDING`), подготовки без
    доставки и входа. Ни одного запроса отправки, «печатает…», правки, удаления, отметки прочитанным."""
    policy = RequestPolicy("owner", sending=sending, login=login)
    assert policy.can_send is False
    for request_ in [*WRITES, *(cls.__new__(cls) for cls in tg_client.SEND_REQUESTS)]:
        with pytest.raises(RequestNotAllowed):
            policy.check(request_)
    # перечень основного аккаунта — чтение (и вход, пока он идёт) и ничего больше
    read = {f"{c.__module__.rsplit('.', 1)[-1]}.{c.__name__}" for c in tg_client.READ_REQUESTS}
    login_names = {f"{c.__module__.rsplit('.', 1)[-1]}.{c.__name__}" for c in tg_client.LOGIN_REQUESTS}
    assert _allowed(policy) == read | (login_names if login else set())
    assert not _allowed(policy) & {"messages.SendMessageRequest", "messages.SetTypingRequest",
                                   "messages.SendMediaRequest", "messages.ForwardMessagesRequest"}


def test_login_requests_pass_only_while_logging_in():
    for role in ("owner", "assistant"):
        policy = RequestPolicy(role, login=True)
        for request_ in LOGIN:
            policy.check(request_)
        policy.login = False
        for request_ in LOGIN:
            with pytest.raises(RequestNotAllowed):
                policy.check(request_)
    # входа по коду из SMS нет вообще
    with pytest.raises(RequestNotAllowed):
        RequestPolicy("assistant", login=True).check(
            functions.auth.SignInRequest("+70000000000", "hash", "12345"))


def test_wrappers_and_batches_are_checked_by_content():
    policy = RequestPolicy("owner")
    policy.check(functions.InvokeWithoutUpdatesRequest(functions.updates.GetStateRequest()))
    policy.check([functions.updates.GetStateRequest(), functions.help.GetConfigRequest()])
    hidden = functions.InvokeWithoutUpdatesRequest(functions.InvokeAfterMsgRequest(
        1, functions.messages.SendMessageRequest(PEER, "спрятано")))
    with pytest.raises(RequestNotAllowed) as exc:
        policy.check(hidden)
    assert "SendMessageRequest" in str(exc.value)
    with pytest.raises(RequestNotAllowed):
        policy.check([functions.updates.GetStateRequest(), functions.messages.ReadHistoryRequest(PEER, 1)])
    # неизвестная обёртка не раскрывается и не проходит
    with pytest.raises(RequestNotAllowed):
        policy.check(functions.InvokeWithTakeoutRequest(1, functions.updates.GetStateRequest()))
    with pytest.raises(RequestNotAllowed):
        policy.check("не запрос")


def _all_requests():
    found = set()
    for info in pkgutil.iter_modules(functions.__path__):
        module = importlib.import_module(f"{functions.__name__}.{info.name}")
        found.update(c for _, c in inspect.getmembers(module, inspect.isclass)
                     if issubclass(c, TLRequest) and c is not TLRequest)
    found.update(c for _, c in inspect.getmembers(functions, inspect.isclass)
                 if issubclass(c, TLRequest) and c is not TLRequest)
    return found


def _allowed(policy):
    out = set()
    for cls in _all_requests():
        probe = cls.__new__(cls)      # перечень смотрит только на класс запроса
        if cls not in tg_client._WRAPPERS and policy.allows(probe):
            out.add(f"{cls.__module__.rsplit('.', 1)[-1]}.{cls.__name__}")
    return out


def test_allowlist_is_exactly_this_and_default_is_deny():
    """Снимок перечня: расширить его незаметно нельзя — этот тест придётся править явно."""
    assert len(_all_requests()) > 500
    read = {
        "help.GetConfigRequest", "updates.GetStateRequest", "updates.GetDifferenceRequest",
        "updates.GetChannelDifferenceRequest", "users.GetUsersRequest", "messages.GetChatsRequest",
        "channels.GetChannelsRequest", "messages.GetDialogsRequest", "messages.GetHistoryRequest",
        # папки Telegram: только чтение их описания, для раскладки «Папки» и сфер (Р-82). Изменить
        # папку (UpdateDialogFilter, UpdateDialogFiltersOrder) по-прежнему нельзя — тест ниже
        "messages.GetDialogFiltersRequest",
        "messages.GetMessagesRequest", "channels.GetMessagesRequest", "functions.PingRequest",
        "auth.LogOutRequest",
        # облачный черновик одного чата — прочитать перед «В чат», чтобы не затереть текст владельца (Р-105)
        "messages.GetPeerDialogsRequest",
        # скачивание голосового для расшифровки (voice/): файл и перенос авторизации в его дата-центр
        "upload.GetFileRequest", "auth.ExportAuthorizationRequest", "auth.ImportAuthorizationRequest",
    }
    login = {"auth.ExportLoginTokenRequest", "auth.ImportLoginTokenRequest",
             "account.GetPasswordRequest", "auth.CheckPasswordRequest"}
    send = {"messages.SendMessageRequest", "messages.SetTypingRequest"}
    assert _allowed(RequestPolicy("owner")) == read
    assert _allowed(RequestPolicy("owner", login=True)) == read | login
    assert _allowed(RequestPolicy("assistant")) == read | send
    # «В чат» (Р-105): облачный черновик — только «взведённым» на этот самый объект запроса; взвод на чужой
    # объект не открывает ни одного класса — перечень не расширяется незаметно
    with tg_client.arming(object()):
        assert _allowed(RequestPolicy("owner")) == read
    with pytest.raises(ValueError):
        RequestPolicy("admin")


# --- настоящий клиент Telethon: запрет срабатывает до сети ---

@pytest.fixture
def sent(monkeypatch):
    """Перехватывает место, где Telethon отдаёт запрос в сеть."""
    calls = []

    async def fake_call(self, sender, request, ordered=False, flood_sleep_threshold=None):
        calls.append(request)
        return None

    monkeypatch.setattr(TelegramClient, "_call", fake_call)
    return calls


def make(role, **kw):
    return GuardedClient(MemorySession(), 12345, "0123456789abcdef", policy=RequestPolicy(role, **kw))


async def test_real_client_refuses_before_any_network_for_owner(sent):
    client = make("owner")
    for request_ in WRITES:
        with pytest.raises(RequestNotAllowed):
            await client(request_)
    with pytest.raises(RequestNotAllowed):
        await client._call(None, functions.messages.SendMessageRequest(PEER, "в обход __call__"))
    assert sent == []
    await client(functions.messages.GetHistoryRequest(PEER, 0, None, 0, 100, 0, 0, 0))
    assert [type(r).__name__ for r in sent] == ["GetHistoryRequest"]


async def test_high_level_telethon_methods_cannot_bypass_the_list(sent):
    owner = make("owner")
    with pytest.raises(RequestNotAllowed):
        await owner.send_message(PEER, "привет")
    with pytest.raises(RequestNotAllowed):
        await owner.send_read_acknowledge(PEER, max_id=5)
    with pytest.raises(RequestNotAllowed):
        await owner.delete_messages(PEER, [1])
    assert sent == []
    assistant = make("assistant")
    with pytest.raises(RequestNotAllowed):
        await assistant.send_read_acknowledge(PEER, max_id=5)   # помощник тоже ничего не отмечает прочитанным
    await assistant(functions.messages.SendMessageRequest(PEER, "привет"))
    assert [type(r).__name__ for r in sent] == ["SendMessageRequest"]


async def test_reconnect_is_reported_upwards(monkeypatch):
    seen = []

    async def no_probe(self):
        return None

    monkeypatch.setattr(TelegramClient, "_handle_auto_reconnect", no_probe)
    client = GuardedClient(MemorySession(), 12345, "0123456789abcdef", policy=RequestPolicy("owner"),
                           on_reconnect=lambda: seen.append(1))
    await client._handle_auto_reconnect()
    assert seen == [1]


def test_factory_builds_guarded_client_on_private_file_session(tmp_path, config):
    import dataclasses

    from telethon.sessions import SQLiteSession

    with pytest.raises(tg_client.NotConfigured):
        tg_client.make_client_factory(config)("owner", tmp_path / "x.session", RequestPolicy("owner"), None)
    cfg = dataclasses.replace(config, tg_api_id=12345, tg_api_hash="0123456789abcdef")
    path = tg_client.session_path(cfg, "owner")
    policy = RequestPolicy("owner")
    client = tg_client.make_client_factory(cfg)("owner", path, policy, None)
    try:
        assert isinstance(client, GuardedClient) and client.policy is policy
        assert isinstance(client.session, SQLiteSession)      # файловая: хранит состояние обновлений
        assert client.flood_sleep_threshold == 0 and client._catch_up is True and client._no_updates is False
        assert path.name == "owner.session" and oct(path.stat().st_mode & 0o777) == "0o600"
        assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
        client.session.save()
        assert oct(path.stat().st_mode & 0o777) == "0o600"
    finally:
        client.session.close()
    with pytest.raises(ValueError):
        tg_client.session_path(cfg, "../etc/passwd")


def test_session_file_and_directory_are_private(tmp_path):
    path = tmp_path / "sessions" / "owner.session"
    tg_client.prepare_session_file(path)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
    (tmp_path / "sessions" / "owner.session-journal").touch()
    tg_client.remove_session_file(path)
    assert list(path.parent.iterdir()) == []


def test_proxy_is_never_silently_bypassed():
    assert tg_client.parse_proxy("") is None
    with pytest.raises(tg_client.NotConfigured):
        tg_client.parse_proxy("ftp://proxy:1")
    try:
        import python_socks  # noqa: F401
    except ImportError:
        with pytest.raises(tg_client.NotConfigured, match="python-socks"):
            tg_client.parse_proxy("socks5://user:p%40ss@proxy.local:1080")
    else:
        assert tg_client.parse_proxy("socks5://user:p%40ss@proxy.local:1080") == {
            "proxy_type": "socks5", "addr": "proxy.local", "port": 1080,
            "username": "user", "password": "p@ss", "rdns": True}


def test_no_role_can_mint_mini_app_sign_in_data():
    """Запросы Mini App (*WebView*) дают клиенту подписанные данные входа в приложение бота.
    С сессией основного аккаунта сервис выпустил бы данные входа владельца в приложение «Штурман»
    и нажимал бы кнопки бота согласований за него (docs/miniapp.md). Ни в одном перечне их нет,
    ни у одной роли и ни на каком этапе (вход, отправка) они не проходят."""
    web_view = set()
    for _, name, _ in pkgutil.iter_modules(functions.__path__):
        module = importlib.import_module(f"{functions.__name__}.{name}")
        for cls_name, cls in inspect.getmembers(module, inspect.isclass):
            if "webview" in cls_name.lower() and issubclass(cls, TLRequest):
                web_view.add(cls)
    names = {cls.__name__ for cls in web_view}
    assert {"RequestWebViewRequest", "RequestSimpleWebViewRequest", "ProlongWebViewRequest"} <= names, names
    allowed = tg_client.READ_REQUESTS | tg_client.LOGIN_REQUESTS | tg_client.SEND_REQUESTS
    assert not any("webview" in cls.__name__.lower() for cls in allowed)
    for role in tg_client.ROLES:
        for login in (False, True):
            policy = RequestPolicy(role, login=login, sending=True)
            for cls in web_view:
                request = cls.__new__(cls)
                assert not policy.allows(request), (role, login, cls.__name__)
                assert not policy.allows(functions.InvokeWithoutUpdatesRequest(request)), (role, cls.__name__)


# --- «В чат» (Р-105): облачный черновик основного аккаунта ---

def _draft(**kw):
    args = {"peer": PEER, "message": "Да, в четверг в десять.", "no_webpage": True}
    args.update(kw)
    return functions.messages.SaveDraftRequest(**args)


def test_save_draft_passes_only_armed_on_this_very_request_only_for_owner():
    """Мутационный якорь `owner_write_is_savedraft_only`: единственная запись основного аккаунта — облачный
    черновик, и только «взведённым» на этот самый объект запроса (`arming(request)` — его взводит только
    `TgManager.put_draft`)."""
    owner = RequestPolicy("owner")
    request = _draft()
    with pytest.raises(RequestNotAllowed):
        owner.check(request)                                   # не взведён — отказ
    with tg_client.arming(request):
        owner.check(request)
        owner.check(functions.InvokeWithoutUpdatesRequest(request))
        with pytest.raises(RequestNotAllowed):
            owner.check(_draft())                              # такой же, но другой объект — отказ
        for request_ in WRITES:                                # взвод не открывает ничего другого
            with pytest.raises(RequestNotAllowed):
                owner.check(request_)
        for policy in (RequestPolicy("assistant"), RequestPolicy("assistant", sending=True),
                       RequestPolicy("assistant", login=True)):
            with pytest.raises(RequestNotAllowed):
                policy.check(request)                          # помощнику черновики не нужны
        # вход в аккаунт идёт — взведённый черновик проходит, невзведённый — нет, как без входа
        RequestPolicy("owner", login=True).check(request)
        with pytest.raises(RequestNotAllowed):
            RequestPolicy("owner", login=True).check(_draft())
    with pytest.raises(RequestNotAllowed):
        owner.check(request)                                   # взвод кончился вместе с блоком
    with pytest.raises(RequestNotAllowed):
        RequestPolicy("owner", login=True).check(request)


async def test_arming_is_one_shot_and_not_inherited_by_background_tasks(sent):
    """Взвод тратится на первой отдаче запроса в сеть; фоновая задача, запущенная внутри взведённого
    вызова (подключение Telethon, переход в другой дата-центр), взвод не наследует."""
    import asyncio

    from shturman import authority
    owner = make("owner")
    request = _draft()
    with tg_client.arming(request):
        background = authority.background_context()
        await owner(request)
        with pytest.raises(RequestNotAllowed):
            await owner(request)                               # второй раз тот же объект — отказ
    assert [type(r).__name__ for r in sent] == ["SaveDraftRequest"]
    second = _draft()
    with tg_client.arming(second):
        task_ctx = authority.background_context()
    assert task_ctx.run(tg_client._armed.get) is None and background.run(tg_client._armed.get) is None
    with tg_client.arming(second):
        loop_task = asyncio.get_running_loop().create_task(owner(second), context=authority.background_context())
        with pytest.raises(RequestNotAllowed):
            await loop_task
    assert len(sent) == 1


@pytest.mark.parametrize("shape", [
    {"no_webpage": False}, {"no_webpage": None}, {"invert_media": True},
    {"entities": [types.MessageEntityTextUrl(0, 2, "https://example.com")]},
    {"media": types.InputMediaEmpty()}, {"effect": 5104841245755180586},
    {"message": "я" * 4097}, {"message": None},
    # пустой черновик стёр бы поле ввода владельца — как и «невидимый»
    {"message": ""}, {"message": "   \n\t"}, {"message": "​‍⁠"}, {"message": " 　"},
    # только собеседник-человек: не канал, не группа, не «Избранное»
    {"peer": types.InputPeerChannel(4001, 1)}, {"peer": types.InputPeerChat(3001)}, {"peer": types.InputPeerSelf()},
    # ответ — только на сообщение того же чата, без цитаты, темы, истории и другого собеседника
    {"reply_to": types.InputReplyToStory(peer=PEER, story_id=5)},
    {"reply_to": types.InputReplyToMessage(reply_to_msg_id=7, quote_text="цитата")},
    {"reply_to": types.InputReplyToMessage(reply_to_msg_id=7, reply_to_peer_id=types.InputPeerUser(2002, 1))},
    {"reply_to": types.InputReplyToMessage(reply_to_msg_id=7, top_msg_id=3)},
    {"reply_to": types.InputReplyToMessage(reply_to_msg_id=0)},
], ids=lambda shape: next(iter(shape)) + ":" + repr(next(iter(shape.values())))[:24])
def test_save_draft_shape_is_visible_plain_text_to_a_person(shape):
    """Мутационный якорь `savedraft_shape`."""
    request = _draft(**shape)
    with tg_client.arming(request):
        with pytest.raises(RequestNotAllowed):
            RequestPolicy("owner").check(request)
    assert tg_client.DRAFT_TEXT_LIMIT == 4096
    for good in (_draft(message="я" * 4096), _draft(reply_to=types.InputReplyToMessage(reply_to_msg_id=7)),
                 _draft(message="Ок 👍")):
        with tg_client.arming(good):
            RequestPolicy("owner").check(good)


async def test_real_client_save_draft_only_armed(sent):
    owner = make("owner")
    request = _draft()
    with pytest.raises(RequestNotAllowed):
        await owner(request)
    with pytest.raises(RequestNotAllowed):
        await owner._call(None, request)                       # в обход __call__ — тоже проверка
    assert sent == []
    with tg_client.arming(request):
        await owner(request)
    assert [type(r).__name__ for r in sent] == ["SaveDraftRequest"]


# --- TgManager.put_draft на настоящем клиенте Telethon (сеть подменена) ---

def _manager(client, role="owner", pacer=None):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from shturman.tg import sync
    from shturman.tg.manager import TgManager
    mgr = TgManager(SimpleNamespace(sending=False), None, SimpleNamespace(subscribe=lambda *a: None))
    mgr._role_of = AsyncMock(return_value=role)
    runtime = SimpleNamespace(client=client, pacer=pacer or sync.Pacer(0.0), stop=None)
    mgr._running = lambda _: runtime
    mgr.can_put = lambda _: True
    return mgr


def _field_client(monkeypatch, fields, *, save=None, read_delay=0.0):
    """Настоящий GuardedClient основного аккаунта; сеть — словарь «поле ввода чата»."""
    import asyncio
    calls = []

    async def fake_call(self, sender, request, ordered=False, flood_sleep_threshold=None):
        calls.append(request)
        if isinstance(request, functions.messages.GetPeerDialogsRequest):
            if read_delay:
                await asyncio.sleep(read_delay)
            text = fields.get("anna", "")
            draft = types.DraftMessage(date=None, message=text) if text else types.DraftMessageEmpty()
            dialog = types.Dialog(peer=types.PeerUser(2001), top_message=1, read_inbox_max_id=0,
                                  read_outbox_max_id=0, unread_count=0, unread_mentions_count=0,
                                  unread_reactions_count=0, unread_poll_votes_count=0,
                                  notify_settings=types.PeerNotifySettings(), draft=draft)
            return types.messages.PeerDialogs(dialogs=[dialog], messages=[], chats=[], users=[],
                                              state=types.updates.State(1, 1, None, 1, 1))
        if isinstance(request, functions.messages.SaveDraftRequest):
            if save is not None:
                return await save(request)
            fields["anna"] = request.message
            return True
        raise AssertionError(type(request).__name__)

    monkeypatch.setattr(TelegramClient, "_call", fake_call)
    client = make("owner")
    client.is_connected = lambda: True

    async def entity(peer):
        return PEER
    client.get_input_entity = entity
    return client, calls


async def test_put_draft_reads_then_writes_and_trusts_the_answer(monkeypatch):
    from shturman import authority
    fields: dict = {}
    client, calls = _field_client(monkeypatch, fields)
    mgr = _manager(client)
    with authority.owner_context(1, chat_id=1, action="telegram.callback"):
        assert await mgr.put_draft(5, "user", 2001, "Да, в четверг.", reply_to_tg_id=7) == "placed"
    # прочитать поле → записать; второго чтения нет — итог по ответу Telegram на запись
    assert [type(r).__name__ for r in calls] == ["GetPeerDialogsRequest", "SaveDraftRequest"]
    save = calls[1]
    assert save.no_webpage is True and save.entities is None and save.media is None
    assert save.reply_to.reply_to_msg_id == 7
    with authority.owner_context(1, chat_id=1, action="telegram.miniapp"):
        assert await mgr.put_draft(5, "user", 2001, "Да, в четверг.") == "already"
    assert len(calls) == 3                                      # только чтение, без записи


async def test_put_draft_never_overwrites_the_owners_text(monkeypatch):
    """Мутационный якорь `put_never_overwrites_owner_text` (на уровне клиента)."""
    from shturman import authority
    from shturman.tg.gateway import DraftBusy
    fields = {"anna": "мой текст"}
    client, calls = _field_client(monkeypatch, fields)
    mgr = _manager(client)
    with authority.owner_context(1, chat_id=1, action="telegram.callback"):
        with pytest.raises(DraftBusy) as exc:
            await mgr.put_draft(5, "user", 2001, "Да, в четверг.")
    assert fields["anna"] == "мой текст" and "мой текст" not in str(exc.value)
    assert [type(r).__name__ for r in calls] == ["GetPeerDialogsRequest"]


async def test_put_draft_refuses_without_press_or_off_the_main_account(monkeypatch):
    """Мутационный якорь `put_requires_owner_press` (на уровне клиента): без нажатия, у помощника, в группе —
    отказ до любого обращения к сети."""
    from shturman import authority
    from shturman.replies.put import Refused
    from shturman.tg.gateway import SendForbidden
    fields: dict = {}
    client, calls = _field_client(monkeypatch, fields)
    with pytest.raises(Refused):
        await _manager(client).put_draft(5, "user", 2001, "Текст")
    with authority.owner_context(1, chat_id=1, action="telegram.command"):
        with pytest.raises(Refused):
            await _manager(client).put_draft(5, "user", 2001, "Текст")
    with authority.owner_context(1, chat_id=1, action="telegram.callback"):
        with pytest.raises(SendForbidden):
            await _manager(client, role="assistant").put_draft(5, "user", 2001, "Текст")
        with pytest.raises(SendForbidden):
            await _manager(client).put_draft(5, "channel", 2001, "Текст")
    assert calls == [] and fields == {}


async def test_put_draft_offline_reconnecting_flood_or_slow_read_writes_nothing(monkeypatch):
    """До записи — только то, что безопасно прервать: не на связи, переподключается, Telegram велел ждать,
    чтение поля не ответило вовремя — отказ, запись в очередь Telethon не ставится."""
    from types import SimpleNamespace

    from shturman import authority
    from shturman.tg import sync
    from shturman.tg.gateway import AccountUnavailable, FloodWait
    fields: dict = {}
    client, calls = _field_client(monkeypatch, fields)
    with authority.owner_context(1, chat_id=1, action="telegram.callback"):
        client.is_connected = lambda: False
        with pytest.raises(AccountUnavailable):
            await _manager(client).put_draft(5, "user", 2001, "Текст")
        client.is_connected = lambda: True
        real_sender = client._sender
        client._sender = SimpleNamespace(_reconnecting=True)
        with pytest.raises(AccountUnavailable):
            await _manager(client).put_draft(5, "user", 2001, "Текст")
        client._sender = real_sender
        pacer = sync.Pacer(0.0)
        pacer.flood(30)
        with pytest.raises(FloodWait):
            await _manager(client, pacer=pacer).put_draft(5, "user", 2001, "Текст")
        assert calls == []
        slow, slow_calls = _field_client(monkeypatch, fields, read_delay=1.0)
        with pytest.raises(AccountUnavailable):
            await _manager(slow).put_draft(5, "user", 2001, "Текст", read_timeout=0.1)
    assert [type(r).__name__ for r in slow_calls] == ["GetPeerDialogsRequest"]   # записи не было
    assert fields == {}


async def test_put_draft_never_cancels_a_queued_write_and_reports_unknown_on_a_broken_link(monkeypatch):
    """Поставленную запись не прерывают по времени: медленный ответ Telegram дожидается. Связь оборвалась
    после отправки — «неизвестно» (`PutUnknown`), а не тихая повторная запись."""
    import asyncio

    from shturman import authority
    from shturman.tg.gateway import PutUnknown
    fields: dict = {}

    async def slow_save(request):
        await asyncio.sleep(0.3)                                   # дольше, чем чтению разрешено ждать
        fields["anna"] = request.message
        return True
    client, calls = _field_client(monkeypatch, fields, save=slow_save)
    with authority.owner_context(1, chat_id=1, action="telegram.callback"):
        assert await _manager(client).put_draft(5, "user", 2001, "Текст", read_timeout=0.1) == "placed"
    assert fields["anna"] == "Текст"

    async def broken(request):
        raise ConnectionError("связь оборвалась")
    fields.clear()
    client, calls = _field_client(monkeypatch, fields, save=broken)
    with authority.owner_context(1, chat_id=1, action="telegram.callback"):
        with pytest.raises(PutUnknown):
            await _manager(client).put_draft(5, "user", 2001, "Текст")
    assert [type(r).__name__ for r in calls] == ["GetPeerDialogsRequest", "SaveDraftRequest"]


async def test_put_draft_with_the_real_human_like_pacer_answers_fast(monkeypatch):
    """Пауз «как у человека» перед записью нет: с настоящим `Pacer` (интервал 3 с, разброс, редкие перерывы),
    даже сразу после другого запроса, нажатие укладывается в доли секунды."""
    import time as _time

    from shturman import authority
    from shturman.tg import sync
    fields: dict = {}
    client, calls = _field_client(monkeypatch, fields)
    pacer = sync.Pacer(3.0, jitter=True)
    pacer.note_request()                                           # только что был другой запрос
    waited = []

    async def no_wait(*args, **kwargs):
        waited.append(args)
    pacer.wait = no_wait                                           # пауза «как у человека» не зовётся вовсе
    mgr = _manager(client, pacer=pacer)
    started = _time.monotonic()
    with authority.owner_context(1, chat_id=1, action="telegram.callback"):
        for n in range(3):
            fields.clear()
            assert await mgr.put_draft(5, "user", 2001, f"Текст {n}") == "placed"
    assert waited == []
    assert _time.monotonic() - started < 3.0                       # с запасом: один интервал пейсера — 3 с


async def test_put_draft_stops_right_before_the_write(monkeypatch):
    from shturman import authority
    from shturman.tg.gateway import Halted
    fields: dict = {}
    client, calls = _field_client(monkeypatch, fields)

    async def stopped():
        return False
    with authority.owner_context(1, chat_id=1, action="telegram.callback"):
        with pytest.raises(Halted):
            await _manager(client).put_draft(5, "user", 2001, "Текст", before_write=stopped)
    assert [type(r).__name__ for r in calls] == ["GetPeerDialogsRequest"] and fields == {}


async def test_disconnect_while_the_write_is_in_flight_is_unknown_not_a_cancelled_task(monkeypatch):
    """`client.disconnect()` гасит ожидания Telethon: запись в пути — исход неизвестен (`PutUnknown`), а сама
    задача, которая нажатие разбирает, отменённой не становится."""
    import asyncio

    from shturman import authority
    from shturman.tg.gateway import PutUnknown
    fields: dict = {}
    pending = asyncio.get_running_loop().create_future()

    async def in_flight(request):
        return await pending
    client, calls = _field_client(monkeypatch, fields, save=in_flight)

    async def press():
        with authority.owner_context(1, chat_id=1, action="telegram.callback"):
            return await _manager(client).put_draft(5, "user", 2001, "Текст")
    task = asyncio.create_task(press())
    for _ in range(200):
        await asyncio.sleep(0.01)
        if pending._callbacks:
            break
    pending.cancel()
    with pytest.raises(PutUnknown):
        await task
    # а настоящая отмена задачи (остановка сервиса) идёт дальше как отмена
    pending2 = asyncio.get_running_loop().create_future()

    async def in_flight2(request):
        return await pending2
    client2, _ = _field_client(monkeypatch, {}, save=in_flight2)

    async def press2():
        with authority.owner_context(1, chat_id=1, action="telegram.callback"):
            return await _manager(client2).put_draft(5, "user", 2001, "Текст")
    task2 = asyncio.create_task(press2())
    for _ in range(200):
        await asyncio.sleep(0.01)
        if pending2._callbacks:
            break
    task2.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task2


@pytest.mark.parametrize("error,expected", [
    ("ServerError", "PutUnknown"), ("TimedOutError", "PutUnknown"), ("PeerIdInvalidError", "DraftRefused")])
async def test_server_and_timeout_errors_on_the_write_are_unknown_not_refused(monkeypatch, error, expected):
    """-500 и -503: по документации Telegram запрос мог быть выполнен — «неизвестно» (отметка «кладу» остаётся),
    а не «не положен». Настоящий отказ (например, неверный собеседник) — «не положен»."""
    from telethon import errors

    from shturman import authority
    from shturman.tg import gateway
    cls = getattr(errors, error)

    async def failing(request):
        if error == "PeerIdInvalidError":
            raise cls(request=request)
        raise cls(request=request, message=error.upper())
    client, calls = _field_client(monkeypatch, {}, save=failing)
    with authority.owner_context(1, chat_id=1, action="telegram.callback"):
        with pytest.raises(getattr(gateway, expected)):
            await _manager(client).put_draft(5, "user", 2001, "Текст")
