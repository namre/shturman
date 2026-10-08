"""Вход на страницу настройки: одноразовая ссылка, код от бота согласований, сессия."""

import asyncio
import json
import logging

import pytest

from shturman.setup_page import auth, service as setup_service

from setup_fakes import API, ORIGIN, bind_owner, save_bot, stand  # noqa: F401, I001 — stand — фикстура; первым: добавляет пути
from exec_fakes import OWNER, STRANGER_USER, until


# --- ссылка входа ---

async def test_link_logs_in_once_and_only_while_fresh(stand, conn):
    s = await stand()
    token, expires_at = await auth.create_link(conn)
    assert auth.well_formed(token) and len(token) >= 43
    # в базе — только хеш
    stored = await conn.fetchval("SELECT string_agg(t::text, ' ') FROM setup_links t")
    assert token not in stored and auth.token_hash(token) in stored
    ttl = await conn.fetchval("SELECT extract(epoch FROM $1::timestamptz - now())", expires_at)
    assert 29 * 60 < ttl <= 30 * 60

    first = await s.page.http.post(API + "/login/link", json={"token": token}, headers=s.page.headers())
    assert first.status_code == 200 and set(first.json()) == {"ok", "key", "expires_at"}
    assert "set-cookie" not in first.headers
    again = await s.browser().http.post(API + "/login/link", json={"token": token}, headers=s.page.headers())
    assert again.status_code == 401 and again.json()["code"] == "link_invalid"
    assert "set-cookie" not in again.headers

    stale, _ = await auth.create_link(conn)
    await conn.execute("UPDATE setup_links SET expires_at = now() - interval '1 second'")
    late = await s.browser().http.post(API + "/login/link", json={"token": stale}, headers=s.page.headers())
    assert late.status_code == 401


async def test_new_link_cancels_the_previous_one(stand, conn):
    s = await stand()
    old, _ = await auth.create_link(conn)
    new, _ = await auth.create_link(conn)
    assert await conn.fetchval("SELECT count(*) FROM setup_links") == 1
    other = s.browser()
    assert (await other.http.post(API + "/login/link", json={"token": old}, headers=other.headers())).status_code == 401
    assert (await other.http.post(API + "/login/link", json={"token": new}, headers=other.headers())).status_code == 200


@pytest.mark.parametrize("token", ["", "short", None, 5, ["x"], "a" * 43 + "!", "я" * 43, "a" * 200])
async def test_malformed_link_values_are_refused_without_touching_the_database(stand, conn, token):
    s = await stand()
    response = await s.page.http.post(API + "/login/link", json={"token": token}, headers=s.page.headers())
    assert response.status_code == 401 and "key" not in response.json()
    assert await conn.fetchval("SELECT count(*) FROM setup_sessions") == 0


async def test_wrong_links_never_lock_the_owner_out(stand, conn):
    """256 бит не перебрать, поэтому отказов сколько угодно — а верная ссылка проходит всё равно."""
    s = await stand()
    good, _ = await auth.create_link(conn)
    wrong = [s.page.http.post(API + "/login/link", json={"token": auth.new_token()}, headers=s.page.headers())
             for _ in range(40)]
    assert {r.status_code for r in await asyncio.gather(*wrong)} == {401}
    assert (await s.page.http.post(API + "/login/link", json={"token": good}, headers=s.page.headers())).status_code == 200
    # журнал действий отказами не заливается: не чаще одной записи в минуту
    assert await conn.fetchval("SELECT count(*) FROM setup_audit WHERE action = 'login.failed'") == 1


async def test_refusals_are_slow_but_do_not_queue_up(stand, conn, monkeypatch):
    s = await stand()
    monkeypatch.setattr(setup_service, "FAIL_DELAY", 0.3)
    monkeypatch.setattr(setup_service, "FAIL_SLOTS", 2)
    page = s.state.extras["setup_page"]
    page.slow = asyncio.Semaphore(2)
    loop = asyncio.get_running_loop()
    started = loop.time()
    one = await s.page.http.post(API + "/login/link", json={"token": auth.new_token()}, headers=s.page.headers())
    assert one.status_code == 401 and loop.time() - started >= 0.28
    started = loop.time()
    many = [s.page.http.post(API + "/login/link", json={"token": auth.new_token()}, headers=s.page.headers())
            for _ in range(12)]
    await asyncio.gather(*many)
    assert loop.time() - started < 1.5          # двенадцать отказов не выстроились в очередь по 0,3 с
    good, _ = await auth.create_link(conn)
    started = loop.time()
    assert (await s.page.http.post(API + "/login/link", json={"token": good}, headers=s.page.headers())).status_code == 200
    assert loop.time() - started < 0.25         # верный вход не задерживается


# --- сессия ---

async def test_session_key_goes_in_a_header_and_a_wrong_or_missing_key_is_a_guest(stand, conn):
    """Подробно — в test_security_review.py; здесь — что без заголовка и с чужим значением входа нет."""
    s = await stand()
    await s.page.login(conn)
    assert (await s.page.get("/state")).status_code == 200
    info = (await s.page.get("/session")).json()
    assert info["authenticated"] is True and info["via"] == "link" and s.page.key not in str(info)
    for value in ("", "x", auth.new_token(), s.page.key[:-1], s.page.key + "a", auth.token_hash(s.page.key)):
        got = await s.page.http.get(API + "/state", headers={"Sec-Fetch-Site": "same-origin", "X-Shturman-Session": value})
        assert got.status_code == 401 and got.json()["code"] == "unauthenticated"
    # заголовки прокси ничего не решают
    got = await s.page.http.get(API + "/state", headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "127.0.0.1"})
    assert got.status_code == 401


async def test_logout_ends_this_session_and_logout_all_ends_every_session(stand, conn):
    s = await stand()
    await s.page.login(conn)
    second = s.browser()
    await second.login(conn)        # вход по новой ссылке завершает прежние сессии: увели одну — ссылка с сервера её отзывает
    assert (await s.page.get("/state")).status_code == 401
    assert (await second.get("/state")).status_code == 200

    out = await second.post("/logout")
    assert out.status_code == 200 and "set-cookie" not in out.headers
    assert (await second.get("/state")).status_code == 401

    a, b = s.browser(), s.browser()
    await a.login(conn)
    b.key, _ = await auth.create_session(conn, "code")
    assert (await b.get("/state")).status_code == 200
    pending, _ = await auth.create_link(conn)
    assert (await a.post("/logout-all")).json()["sessions"] == 2
    assert (await a.get("/state")).status_code == 401 and (await b.get("/state")).status_code == 401
    # невостребованная ссылка тоже отменена
    assert (await a.http.post(API + "/login/link", json={"token": pending}, headers=s.browser().headers())).status_code == 401


async def test_cli_prints_only_the_path_and_keeps_the_value_out_of_the_notes(stand, conn, monkeypatch, capsys):
    from shturman import cli
    from conftest import DSN

    s = await stand()
    monkeypatch.setenv("SHTURMAN_DSN", DSN)
    monkeypatch.delenv("SHTURMAN_SETUP_ORIGIN", raising=False)
    monkeypatch.delenv("SHTURMAN_DASHBOARD_ORIGIN", raising=False)
    monkeypatch.setenv("SHTURMAN_PORT", "8765")
    await cli._setup_link(False)
    out, err = capsys.readouterr()
    path = out.strip()
    assert out.count("\n") == 1 and path.startswith("/shturman-setup/#")
    token = path.split("#")[1]
    assert auth.well_formed(token) and token not in err
    assert "30 минут" in err and "http://127.0.0.1:8765" in err and "туннель" in err
    assert (await s.page.http.post(API + "/login/link", json={"token": token}, headers=s.page.headers())).status_code == 200

    monkeypatch.setenv("SHTURMAN_SETUP_ORIGIN", "https://assistant.example.com/")
    await cli._setup_link(True)
    out, err = capsys.readouterr()
    assert out.strip().startswith("https://assistant.example.com/shturman-setup/#") and "туннель" not in err

    # другой порт того же имени — обычный случай: адрес печатается с портом
    monkeypatch.setenv("SHTURMAN_SETUP_ORIGIN", "https://assistant.example.com:8443")
    monkeypatch.setenv("SHTURMAN_DASHBOARD_ORIGIN", "https://assistant.example.com")
    await cli._setup_link(True)
    out, err = capsys.readouterr()
    assert out.strip().startswith("https://assistant.example.com:8443/shturman-setup/#") and "ВНИМАНИЕ" not in err
    # адрес страницы совпал с адресом дашборда: снаружи её нет, ссылка — на локальный адрес, и об этом сказано
    monkeypatch.setenv("SHTURMAN_DASHBOARD_ORIGIN", "https://Assistant.example.com:8443/")
    await cli._setup_link(True)
    out, err = capsys.readouterr()
    assert out.strip().startswith("http://127.0.0.1:8765/shturman-setup/#")
    assert "совпадает с адресом дашборда" in err and "туннель" in err
    monkeypatch.delenv("SHTURMAN_DASHBOARD_ORIGIN")

    await cli._setup_logout_all()
    assert "Завершено сессий страницы настройки: 1" in capsys.readouterr().out
    assert await conn.fetchval("SELECT count(*) FROM setup_sessions WHERE revoked_at IS NULL") == 0
    assert await conn.fetchval("SELECT count(*) FROM setup_links WHERE used_at IS NULL") == 0


def test_cli_knows_the_commands():
    import subprocess
    import sys

    done = subprocess.run([sys.executable, "-m", "shturman.cli", "--help"], capture_output=True, text=True)
    assert "setup-link" in done.stdout and "setup-logout-all" in done.stdout


# --- код от бота согласований ---

async def owner_bound(stand, conn):
    s = await stand()
    await s.page.login(conn)
    assert (await save_bot(s)).status_code == 200
    await until(lambda: s.state.extras["executor"].bot and s.state.extras["executor"].bot.identity)
    await bind_owner(s)
    return s


def lock_notices(s) -> list[dict]:
    return [p for p in s.telegram.calls("sendMessage") if "неверный код" in p["text"]]


def codes(s) -> list[str]:
    return ["".join(ch for ch in p["text"].split("\n")[0] if ch.isdigit())
            for p in s.telegram.calls("sendMessage") if p["text"].startswith("Код входа")]


async def test_without_a_bound_owner_there_is_no_code_login(stand, conn):
    s = await stand()
    guest = s.browser()
    assert (await guest.get("/session")).json() == {"authenticated": False, "code_login": False}
    assert (await guest.post("/login/code/request")).json() == {"result": "no_owner"}
    assert (await guest.post("/login/code", {"code": "12345678"})).json()["code"] == "none"
    # бот есть, но владелец к нему не привязан — тоже некому
    await s.page.login(conn)
    await save_bot(s)
    assert (await guest.post("/login/code/request")).json() == {"result": "no_owner"}
    assert s.telegram.calls("sendMessage") == []


async def test_code_from_the_bot_logs_in_once(stand, conn, caplog):
    caplog.set_level(logging.DEBUG)
    s = await owner_bound(stand, conn)
    guest = s.browser()
    assert (await guest.get("/session")).json()["code_login"] is True
    assert (await guest.post("/login/code/request")).json() == {"result": "sent"}
    (code,) = codes(s)
    assert len(code) == 8
    sent = [p for p in s.telegram.calls("sendMessage") if p["text"].startswith("Код входа")][0]
    assert sent["chat_id"] == OWNER and "5 минут" in sent["text"]
    # действующий код новым не заменяется, и второе сообщение владельцу не уходит
    assert (await guest.post("/login/code/request")).json() == {"result": "reused"}
    assert len(codes(s)) == 1
    # в базе кода нет — только его подпись ключом, которого в базе нет
    dump = await conn.fetchval("SELECT string_agg(t::text, ' ') FROM setup_state t")
    assert code not in dump and "digest" in dump
    assert code not in caplog.text

    spaced = f"{code[:4]} {code[4:]}"
    done = await guest.post("/login/code", {"code": spaced})
    assert done.status_code == 200 and "set-cookie" not in done.headers
    guest.key = done.json()["key"]
    assert (await guest.get("/session")).json()["via"] == "code"
    assert (await s.page.get("/state")).status_code == 200           # вход по коду чужих сессий не завершает
    assert (await s.browser().post("/login/code", {"code": code})).json()["code"] == "none"   # код одноразовый
    await until(lambda: any("по коду из этого чата" in p["text"] for p in s.telegram.calls("sendMessage")))


async def test_five_wrong_codes_close_code_login_and_the_lock_grows(stand, conn, monkeypatch):
    s = await owner_bound(stand, conn)
    clock = [1_800_000_000.0]
    monkeypatch.setattr(auth, "now", lambda: clock[0])
    guest = s.browser()
    assert (await guest.post("/login/code/request")).json() == {"result": "sent"}
    good = codes(s)[-1]
    wrong = "0" * 8 if good != "0" * 8 else "1" * 8
    for _ in range(4):
        assert (await guest.post("/login/code", {"code": wrong})).json()["code"] == "wrong"
    fifth = await guest.post("/login/code", {"code": wrong})
    assert fifth.status_code == 401 and fifth.json()["code"] == "locked"
    await until(lambda: any("неверный код" in p["text"] for p in s.telegram.calls("sendMessage")))
    # верный код во время блокировки не принимается, новый не выдаётся
    assert (await guest.post("/login/code", {"code": good})).json()["code"] == "locked"
    assert (await guest.post("/login/code/request")).json() == {"result": "locked"}
    clock[0] += 61
    assert (await guest.post("/login/code", {"code": good})).json()["code"] == "none"   # прежний код погашен
    # счётчик новым кодом не обнуляется: вторая блокировка длиннее первой
    assert (await guest.post("/login/code/request")).json() == {"result": "sent"}
    for _ in range(5):
        await guest.post("/login/code", {"code": wrong})
    clock[0] += 61
    assert (await guest.post("/login/code/request")).json() == {"result": "locked"}
    clock[0] += 5 * 60
    assert (await guest.post("/login/code/request")).json() == {"result": "sent"}
    # вход по ссылке снимает блокировку: владелец доказал, что сервер его
    for _ in range(5):
        await guest.post("/login/code", {"code": wrong})
    assert (await guest.post("/login/code/request")).json() == {"result": "locked"}
    await s.browser().login(conn)
    assert (await guest.post("/login/code/request")).json() == {"result": "sent"}
    assert await conn.fetchval("SELECT count(*) FROM setup_audit WHERE action = 'login.code_locked'") == 3
    # В журнале — все три блокировки, а сообщение владельцу о них ушло одно: следующее не раньше,
    # чем через период блокировки (и не чаще раза в 15 минут, пока блокировки короткие).
    await asyncio.sleep(0.05)
    assert len(lock_notices(s)) == 1
    # Вход по ссылке всё сбросил: новая блокировка — новое сообщение, и снова только одно.
    for _ in range(5):
        await guest.post("/login/code", {"code": wrong})
    await until(lambda: len(lock_notices(s)) == 2)
    clock[0] += 61
    assert (await guest.post("/login/code/request")).json() == {"result": "sent"}
    for _ in range(5):
        await guest.post("/login/code", {"code": wrong})
    assert await conn.fetchval("SELECT count(*) FROM setup_audit WHERE action = 'login.code_locked'") == 5
    await asyncio.sleep(0.05)
    assert len(lock_notices(s)) == 2


async def test_lock_notice_is_decided_under_the_same_lock_as_the_lock_itself(conn, monkeypatch):
    """`verify_code`: locked_now — раз за период; повторная блокировка в том же периоде — locked_again."""
    clock = [1_800_000_000.0]
    monkeypatch.setattr(auth, "now", lambda: clock[0])
    key, sent = b"k" * 32, []

    async def send(code):
        sent.append(code)

    async def lock() -> str:
        assert await auth.request_code(conn, key, send) == "sent"
        wrong = "0" * 8 if sent[-1] != "0" * 8 else "1" * 8
        results = [await auth.verify_code(conn, key, wrong) for _ in range(5)]
        assert results[:4] == ["wrong"] * 4
        return results[4]

    assert await lock() == "locked_now"
    assert await auth.verify_code(conn, key, "12345678") == "locked"
    clock[0] += 61
    assert await lock() == "locked_again"            # через минуту: вторая блокировка, сообщение уже было
    clock[0] += 5 * 60 + 1
    assert await lock() == "locked_again"
    clock[0] += 15 * 60 + 1
    assert await lock() == "locked_now"              # прошло больше 15 минут с прошлого сообщения
    clock[0] += 60 * 60 + 1
    assert await lock() == "locked_now"              # блокировки уже часовые: одно сообщение на каждую
    clock[0] += 30 * 60
    assert await auth.verify_code(conn, key, "12345678") == "locked"


async def test_code_expires_and_failed_delivery_is_not_repeated_at_once(stand, conn, monkeypatch):
    from exec_fakes import refusal

    s = await owner_bound(stand, conn)
    clock = [1_800_000_000.0]
    monkeypatch.setattr(auth, "now", lambda: clock[0])
    guest = s.browser()
    s.telegram.script["sendMessage"] = [refusal(403, "Forbidden: bot was blocked by the user")]
    assert (await guest.post("/login/code/request")).json() == {"result": "send_failed"}
    assert (await guest.post("/login/code/request")).json() == {"result": "wait"}
    clock[0] += 61
    assert (await guest.post("/login/code/request")).json() == {"result": "sent"}
    code = codes(s)[-1]
    clock[0] += 5 * 60 + 1
    assert (await guest.post("/login/code", {"code": code})).json()["code"] == "expired"
    assert (await guest.post("/login/code/request")).json() == {"result": "sent"}


async def test_only_the_owner_bound_to_this_bot_gets_the_code(stand, conn):
    """Запись о владельце из общей таблицы (её мог оставить плагин) кодом не пользуется."""
    s = await stand()
    await s.page.login(conn)
    await save_bot(s)
    await conn.execute("INSERT INTO settings (key, value) VALUES ('owner', $1::jsonb)",
                       json.dumps({"user_id": STRANGER_USER["id"], "chat_id": STRANGER_USER["id"]}))
    guest = s.browser()
    assert (await guest.post("/login/code/request")).json() == {"result": "no_owner"}
    assert s.telegram.calls("sendMessage") == []
    assert ORIGIN  # имя узла теста — то же, под которым страница считает запрос своим
