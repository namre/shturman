"""Unix-сокет для обратного прокси (listen.py, Р-76): когда сервис его открывает, когда нет,
и что один сервер uvicorn отвечает и на TCP-порту, и на сокете, запуская приложение один раз."""

import asyncio
import os
import socket
import stat

import httpx
import pytest

from shturman import listen


def make_dir(tmp_path, mode=0o2710):
    d = tmp_path / "run"
    d.mkdir()
    os.chmod(d, mode)
    return d


@pytest.fixture(autouse=True)
def fresh_state():
    listen._set("off")
    yield
    listen._set("off")


@pytest.mark.parametrize("mode", [0o2710, 0o2750])
def test_closed_setgid_directory_is_accepted(tmp_path, mode):
    assert listen.check_dir(make_dir(tmp_path, mode)) is None


@pytest.mark.parametrize("mode", [
    0o0710,   # без setgid сокет получил бы группу сервиса — Caddy не смог бы войти
    0o2700,   # группе нельзя войти — Caddy тоже не сможет
    0o2730,   # группа могла бы подменить сокет
    0o2711,   # остальным можно войти
    0o2714,
    0o2777,
])
def test_directory_with_other_rights_is_refused(tmp_path, mode):
    assert listen.check_dir(make_dir(tmp_path, mode)) == "dir_mode"


def test_missing_directory_and_foreign_owner_are_refused(tmp_path, monkeypatch):
    assert listen.check_dir(tmp_path / "нет") == "dir_missing"
    (tmp_path / "файл").write_text("")
    assert listen.check_dir(tmp_path / "файл") == "dir_missing"
    d = make_dir(tmp_path)
    # Каталог, созданный Docker от root (или кем-то ещё), сервису не годится.
    monkeypatch.setattr(listen.os, "geteuid", lambda: os.stat(d).st_uid + 1)
    assert listen.check_dir(d) == "dir_owner"


def test_socket_is_opened_closed_to_others_and_state_says_so(tmp_path):
    d = make_dir(tmp_path)
    sock = listen.open_proxy_socket(str(d / "proxy.sock"))
    try:
        assert sock is not None and listen.state() == "listening"
        st = os.lstat(d / "proxy.sock")
        assert stat.S_ISSOCK(st.st_mode)
        assert stat.S_IMODE(st.st_mode) == 0o660
        assert st.st_gid == os.stat(d).st_gid       # группа каталога (на сервере — caddy)
    finally:
        sock.close()


def test_stale_socket_is_replaced_but_other_files_are_left_alone(tmp_path):
    d = make_dir(tmp_path)
    old = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old.bind(str(d / "proxy.sock"))
    old.close()
    sock = listen.open_proxy_socket(str(d / "proxy.sock"))
    assert sock is not None and listen.state() == "listening"
    sock.close()

    (d / "proxy.sock").unlink()
    (d / "proxy.sock").write_text("не сокет")
    assert listen.open_proxy_socket(str(d / "proxy.sock")) is None
    assert listen.state() == "not_socket"
    assert (d / "proxy.sock").read_text() == "не сокет"


def test_no_path_means_no_socket(tmp_path):
    assert listen.open_proxy_socket("") is None and listen.state() == "off"


def test_unsuitable_directory_leaves_tcp_only(tmp_path):
    d = make_dir(tmp_path, 0o0755)
    assert listen.open_proxy_socket(str(d / "proxy.sock")) is None
    assert listen.state() == "dir_mode"
    assert not (d / "proxy.sock").exists()


async def test_one_server_answers_on_tcp_and_on_the_socket(tmp_path):
    lifespans = []

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    lifespans.append("start")
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        host = dict(scope["headers"]).get(b"host", b"").decode()
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": host.encode()})

    d = make_dir(tmp_path)
    path = str(d / "proxy.sock")
    server, sockets = listen.make_server(app, host="127.0.0.1", port=0, proxy_socket=path,
                                         log_level="warning", access_log=False)
    assert len(sockets) == 2
    port = sockets[0].getsockname()[1]
    task = asyncio.create_task(server.serve(sockets=sockets))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started
        async with httpx.AsyncClient() as tcp:
            got = await tcp.get(f"http://127.0.0.1:{port}/health")
            assert (got.status_code, got.text) == (200, f"127.0.0.1:{port}")
        # Через сокет имя узла приходит то, что прислал прокси: сервис сверяет его, как и раньше.
        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=path)) as uds:
            got = await uds.get("http://assistant.example.com:8443/shturman-setup/",
                                headers={"Host": "assistant.example.com:8443"})
            assert (got.status_code, got.text) == (200, "assistant.example.com:8443")
        assert lifespans == ["start"]
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
    for s in sockets:
        s.close()
