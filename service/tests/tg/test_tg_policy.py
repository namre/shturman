"""Перечень разрешённых запросов: основной аккаунт владельца — только чтение."""

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
]
READS = [
    functions.updates.GetStateRequest(),
    functions.updates.GetDifferenceRequest(pts=1, date=None, qts=0),
    functions.updates.GetChannelDifferenceRequest(CHANNEL, types.ChannelMessagesFilterEmpty(), 1, 100),
    functions.users.GetUsersRequest([types.InputUserSelf()]),
    functions.messages.GetDialogsRequest(None, 0, types.InputPeerEmpty(), 100, 0),
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
        "messages.GetMessagesRequest", "channels.GetMessagesRequest", "functions.PingRequest",
        "auth.LogOutRequest",
    }
    login = {"auth.ExportLoginTokenRequest", "auth.ImportLoginTokenRequest",
             "account.GetPasswordRequest", "auth.CheckPasswordRequest"}
    send = {"messages.SendMessageRequest", "messages.SetTypingRequest"}
    assert _allowed(RequestPolicy("owner")) == read
    assert _allowed(RequestPolicy("owner", login=True)) == read | login
    assert _allowed(RequestPolicy("assistant")) == read | send
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
