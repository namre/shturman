"""Бот-менеджер и создание бота согласований в одно нажатие (relayd 0.4.0, Р-77).

Telegram — подставной HTTP-сервер (fake_telegram.py), запросы к нему идут настоящим tg_call через
urllib; API посредника — настоящий HTTP-сервер relayd. Только стандартная библиотека и pytest.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import stat
import sys
import threading
import time
from pathlib import Path
from urllib.parse import unquote

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import relayd  # noqa: E402
from fake_telegram import MANAGER_TOKEN, PLAIN_TOKEN, FakeTelegram  # noqa: E402
from test_relayd import call, invite  # noqa: E402

OWNER = {"id": 1000, "is_bot": False, "first_name": "Евгений", "last_name": "Тестов"}
OTHER = {"id": 6666, "is_bot": False, "first_name": "Некто"}


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def tg():
    fake = FakeTelegram()
    base = fake.start()
    fake.base = base
    yield fake
    fake.stop()


@pytest.fixture()
def settings(tmp_path, tg):
    ca = tmp_path / "frps.crt"
    ca.write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n")
    return relayd.Settings(db=str(tmp_path / "relay.db"), domain="sh.example.test", ca_file=str(ca), frp_token="t",
                           tg_api=tg.base, manager_poll=0, join_fail_total=1000)


@pytest.fixture()
def manager(settings):
    clock = Clock()
    m = relayd.Manager(settings, clock=clock)
    m.clock_obj = clock
    return m


def ready(m: relayd.Manager, token: str = MANAGER_TOKEN) -> relayd.Manager:
    relayd.write_manager_token(m.s.manager_path, token)
    assert m.reload()
    m.check_identity()
    return m


# --- токен бота-менеджера ---------------------------------------------------------------------------

def test_without_token_there_is_no_pairing(manager):
    manager.reload()
    assert manager.problem == "no_token" and not manager.ready
    with pytest.raises(relayd.PairError) as e:
        manager.start(None)
    assert e.value.status == 503 and e.value.code == "manager_unavailable"


def test_token_file_is_private_and_reloaded_when_changed(manager):
    ready(manager)
    path = manager.s.manager_path
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert manager.ready and manager.username == "shturman_manager_bot"
    assert not manager.reload()                                  # файл не менялся
    assert MANAGER_TOKEN not in repr(manager)
    relayd.write_manager_token(path, None)
    assert manager.reload() and not manager.ready and manager.problem == "no_token"


def test_bot_without_management_mode_is_not_a_manager(manager):
    ready(manager, PLAIN_TOKEN)
    assert not manager.ready and manager.problem == "not_manager"


# --- пары -------------------------------------------------------------------------------------------

def test_start_gives_link_without_secret(manager):
    ready(manager)
    out = manager.start("Мои согласования")
    assert re.fullmatch(r"shturman_[a-z2-9]{10}_bot", out["username"]) and 5 <= len(out["username"]) <= 32
    assert out["link"] == (f"https://t.me/newbot/shturman_manager_bot/{out['username']}"
                           "?name=%D0%9C%D0%BE%D0%B8%20%D1%81%D0%BE%D0%B3%D0%BB%D0%B0%D1%81%D0%BE%D0%B2%D0%B0%D0%BD%D0%B8%D1%8F")
    assert unquote(out["link"].split("name=")[1]) == "Мои согласования"
    assert len(out["secret"]) >= 43 and out["secret"] not in out["link"]       # 256 бит, в ссылку не входит
    pair = manager.pairs[out["pair_id"]]
    assert pair.secret_hash == relayd.secret_hash(out["secret"]) and out["secret"] not in repr(pair)
    assert out["ttl"] == 900 and out["expires_at"] == int(manager.clock_obj.now + 900)
    assert manager.start(None)["name"] == "Согласования"
    for bad in ("x" * 65, 42, "a\x00b"):
        with pytest.raises(relayd.PairError):
            manager.start(bad)


def lifecycle(manager, tg, owner=OWNER, username=None):
    out = manager.start(None)
    bot_id = tg.create_managed_bot(username or out["username"], "Согласования", owner)
    manager.poll_once(0)
    return out, bot_id


def test_full_lifecycle_token_once_and_access_restricted(manager, tg, caplog):
    caplog.set_level(logging.INFO)
    ready(manager)
    out = manager.start(None)
    status, body, failed = manager.fetch(out["pair_id"], out["secret"])
    assert (status, body["status"], failed) == (202, "waiting", False)
    bot_id = tg.create_managed_bot(out["username"], "Согласования", OWNER)
    manager.poll_once(0)
    assert tg.allowed == ["managed_bot"]                         # только эти обновления
    assert ("setManagedBotAccessSettings", {"user_id": bot_id, "is_access_restricted": True}) in tg.calls
    assert tg.managed[bot_id]["restricted"] is True
    # Чужой секрет — тот же ответ, что неизвестная пара.
    assert manager.fetch(out["pair_id"], "A" * 43) == manager.fetch("zzzzzzzzzz", "A" * 43)
    assert manager.fetch(out["pair_id"], "A" * 43)[0] == 404
    status, body, failed = manager.fetch(out["pair_id"], out["secret"])
    assert status == 200 and not failed
    assert body["token"] == tg.token_of(bot_id) and body["bot"] == {"id": bot_id, "username": out["username"], "name": "Согласования"}
    assert body["creator"] == {"id": OWNER["id"], "username": None, "name": "Евгений Тестов"} and body["restricted"] is True
    assert manager.fetch(out["pair_id"], out["secret"])[0] == 404           # второй раз — нет
    assert out["pair_id"] not in manager.pairs
    logs = caplog.text
    assert tg.token_of(bot_id) not in logs and out["secret"] not in logs and MANAGER_TOKEN not in logs


def test_token_is_taken_from_telegram_at_delivery(manager, tg):
    ready(manager)
    out, bot_id = lifecycle(manager, tg)
    assert not any(m == "getManagedBotToken" for m, _ in tg.calls)          # до выдачи токен не запрашивался
    tg.fail["getManagedBotToken"] = [500]
    assert manager.fetch(out["pair_id"], out["secret"])[0] == 502            # Telegram не ответил — пара жива
    status, body, _ = manager.fetch(out["pair_id"], out["secret"])
    assert status == 200 and body["token"] == tg.token_of(bot_id)


def test_token_waits_until_access_is_settled(manager, tg, monkeypatch):
    ready(manager)
    monkeypatch.setattr(manager, "_restrict", lambda pair: None)          # закрытие доступа ещё не закончилось
    out, _ = lifecycle(manager, tg)
    assert manager.fetch(out["pair_id"], out["secret"])[1]["status"] == "waiting"
    manager.pairs[out["pair_id"]].restricted = True
    assert manager.fetch(out["pair_id"], out["secret"])[0] == 200


def test_creator_username_is_delivered(manager, tg):
    ready(manager)
    out, _ = lifecycle(manager, tg, owner={**OWNER, "username": "evgeny_t"})
    assert manager.fetch(out["pair_id"], out["secret"])[1]["creator"]["username"] == "evgeny_t"


def test_access_settings_failure_is_reported(manager, tg):
    ready(manager)
    tg.fail["setManagedBotAccessSettings"] = [400]
    out, _ = lifecycle(manager, tg)
    assert manager.fetch(out["pair_id"], out["secret"])[1]["restricted"] is False


def test_username_edited_in_creation_window_is_not_matched(manager, tg):
    ready(manager)
    out, _ = lifecycle(manager, tg, username="ivan_my_own_name_bot")
    assert manager.counters["unmatched"] == 1
    assert manager.fetch(out["pair_id"], out["secret"])[1]["status"] == "waiting"
    assert not any(m == "setManagedBotAccessSettings" for m, _ in tg.calls)  # чужого бота не трогаем


def test_username_match_ignores_case(manager, tg):
    ready(manager)
    out, _ = lifecycle(manager, tg, username=None)
    out2 = manager.start(None)
    tg.create_managed_bot(out2["username"].upper().replace("_BOT", "_bot"), "x", OWNER)
    manager.poll_once(0)
    assert manager.fetch(out2["pair_id"], out2["secret"])[0] == 200


def test_owner_change_before_delivery_is_a_conflict(manager, tg):
    ready(manager)
    out, bot_id = lifecycle(manager, tg)
    tg.change_owner(bot_id, OTHER)
    manager.poll_once(0)
    status, body, _ = manager.fetch(out["pair_id"], out["secret"])
    assert status == 409 and body["error"] == "conflict"
    assert manager.fetch(out["pair_id"], out["secret"])[0] == 404


def test_pair_expires_and_late_bot_is_unmatched(manager, tg):
    ready(manager)
    out = manager.start(None)
    manager.clock_obj.now += 901
    tg.create_managed_bot(out["username"], "x", OWNER)
    manager.poll_once(0)
    assert manager.counters["unmatched"] == 1
    assert manager.fetch(out["pair_id"], out["secret"])[0] == 404


def test_created_bot_gets_grace_time_for_delivery(manager, tg):
    ready(manager)
    out = manager.start(None)
    manager.clock_obj.now += 890                     # бот создан за 10 секунд до конца срока пары
    tg.create_managed_bot(out["username"], "x", OWNER)
    manager.poll_once(0)
    manager.clock_obj.now += 60
    assert manager.fetch(out["pair_id"], out["secret"])[0] == 200


def test_pending_pairs_are_capped(manager):
    ready(manager)
    manager.s.botpair_max_pending = 2
    manager.start(None)
    manager.start(None)
    with pytest.raises(relayd.PairError) as e:
        manager.start(None)
    assert e.value.code == "busy"
    manager.clock_obj.now += 901
    manager.start(None)                               # истёкшие не считаются


def test_garbage_updates_are_ignored(manager, tg):
    ready(manager)
    out = manager.start(None)
    for upd in ({"managed_bot": "x"}, {"managed_bot": {"bot": {"id": "1"}, "user": OWNER}},
                {"managed_bot": {"bot": {"id": 5, "username": out["username"]}}}, {"message": {}}):
        manager.handle_update(upd)
    assert manager.pairs[out["pair_id"]].bot_id is None


# --- HTTP посредника ----------------------------------------------------------------------------------

@pytest.fixture()
def api(settings, manager):
    _, srv = relayd.make_servers(settings, ("127.0.0.1", 0), ("127.0.0.1", 0), manager=manager)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def test_http_health_tells_whether_pairing_works(api, manager):
    assert json.loads(call(api, "GET", "/api/health")[1])["botpair"] is False
    ready(manager)
    assert json.loads(call(api, "GET", "/api/health")[1]) == {"ok": True, "version": "0.4.1", "botpair": True}


def test_http_pair_round_trip(api, manager, tg):
    ready(manager)
    status, body, hdr = call(api, "POST", "/api/botpair/start", {"name": "Пульт"}, {"X-Forwarded-For": "203.0.113.7"})
    assert status == 200 and hdr["Cache-Control"] == "no-store"
    out = json.loads(body)
    path = f"/api/botpair/{out['pair_id']}/token"
    assert json.loads(call(api, "POST", path, {"secret": out["secret"]})[1])["status"] == "waiting"
    bot_id = tg.create_managed_bot(out["username"], "Пульт", OWNER)
    manager.poll_once(0)
    status, body, _ = call(api, "POST", path, {"secret": out["secret"]})
    assert status == 200 and json.loads(body)["token"] == tg.token_of(bot_id)
    assert call(api, "POST", path, {"secret": out["secret"]})[0] == 404
    assert call(api, "POST", "/api/botpair/../token", {"secret": out["secret"]})[0] == 404
    assert call(api, "GET", path)[0] == 404                       # секрет — только телом POST


def test_http_start_is_rate_limited_per_address(api, manager):
    ready(manager)
    api.botpair_start = relayd.Window(2, 900)
    for _ in range(2):
        assert call(api, "POST", "/api/botpair/start", {}, {"X-Forwarded-For": "198.51.100.1"})[0] == 200
    assert call(api, "POST", "/api/botpair/start", {}, {"X-Forwarded-For": "198.51.100.1"})[0] == 429
    assert call(api, "POST", "/api/botpair/start", {}, {"X-Forwarded-For": "198.51.100.2"})[0] == 200


def test_http_wrong_secrets_do_not_stop_delivery_for_others(api, manager):
    """Секрет — 256 бит: перебирать его бессмысленно, а общий предел неудачных позволил бы постороннему
    остановить выдачу всем. Поэтому у выдачи — только предел на адрес."""
    ready(manager)
    api.failures = relayd.Window(3, 60)
    out = json.loads(call(api, "POST", "/api/botpair/start", {})[1])
    for i in range(5):
        assert call(api, "POST", f"/api/botpair/{out['pair_id']}/token", {"secret": "B" * 43},
                    {"X-Forwarded-For": f"192.0.2.{i}"})[0] == 404
    assert call(api, "POST", f"/api/botpair/{out['pair_id']}/token", {"secret": out["secret"]})[0] == 202
    assert not api.failures.blocked("*")


def test_pending_pairs_are_capped_per_network(api, manager):
    """Одна сеть (IPv6 /48, IPv4 /24) не займёт общий предел ожидающих пар, перебирая адреса."""
    ready(manager)
    for i in range(5):
        assert call(api, "POST", "/api/botpair/start", {}, {"X-Forwarded-For": f"2001:db8:1:{i}::1"})[0] == 200
    status, body, _ = call(api, "POST", "/api/botpair/start", {}, {"X-Forwarded-For": "2001:db8:1:99::1"})
    assert status == 429 and json.loads(body)["error"] == "rate_limited"
    assert call(api, "POST", "/api/botpair/start", {}, {"X-Forwarded-For": "2001:db8:2::1"})[0] == 200
    assert relayd.net_bucket("203.0.113.77") == "203.0.113.0/24" == relayd.net_bucket("::ffff:203.0.113.5")


def test_http_invite_can_be_required(settings, manager, tg):
    settings.botpair_require_invite = True
    _, srv = relayd.make_servers(settings, ("127.0.0.1", 0), ("127.0.0.1", 0), manager=manager)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        ready(manager)
        status, body, _ = call(srv, "POST", "/api/botpair/start", {})
        assert status == 403 and json.loads(body)["error"] == "invite_required"
        code = invite(relayd.open_db(settings.db))
        assert call(srv, "POST", "/api/botpair/start", {"code": code})[0] == 200
        assert call(srv, "POST", "/api/botpair/start", {"code": code})[0] == 200     # код не расходуется
    finally:
        srv.shutdown()
        srv.server_close()


def test_manager_setup_page_and_link(api, manager, settings, capsys):
    assert relayd.main(["--db", settings.db, "manager-link", "--minutes", "5"]) == 0
    url = capsys.readouterr().out.strip()
    assert url.startswith("https://relay.sh.namre.app/api/manager/setup#")   # служба ещё не записала свой адрес
    secret = url.split("#", 1)[1]
    status, body, hdr = call(api, "GET", "/api/manager/setup")
    assert status == 200 and secret.encode() not in body
    assert "script-src 'self'" in hdr["Content-Security-Policy"] and hdr["X-Frame-Options"] == "DENY"
    assert call(api, "GET", "/api/manager/setup.js")[0] == 200
    # Бот без Bot Management Mode — отказ, ссылка остаётся.
    status, body, _ = call(api, "POST", "/api/manager/setup", {"secret": secret, "token": PLAIN_TOKEN})
    assert status == 422 and json.loads(body)["error"] == "not_manager"
    assert call(api, "POST", "/api/manager/setup", {"secret": "C" * 43, "token": MANAGER_TOKEN})[0] == 403
    status, body, _ = call(api, "POST", "/api/manager/setup", {"secret": secret, "token": MANAGER_TOKEN})
    assert status == 200 and json.loads(body) == {"ok": True, "username": "shturman_manager_bot"}
    assert MANAGER_TOKEN.encode() not in body
    assert relayd.read_manager_token(settings.manager_path) == MANAGER_TOKEN
    assert stat.S_IMODE(os.stat(settings.manager_path).st_mode) == 0o600
    manager.check_identity()
    assert manager.ready
    assert call(api, "POST", "/api/manager/setup", {"secret": secret, "token": MANAGER_TOKEN})[0] == 403   # одноразовая


def test_new_manager_link_cancels_old_one(settings, capsys):
    relayd.main(["--db", settings.db, "manager-link"])
    first = capsys.readouterr().out.strip().split("#")[1]
    relayd.main(["--db", settings.db, "manager-link"])
    conn = relayd.open_db(settings.db)
    assert conn.execute("SELECT count(*) FROM manager_links WHERE secret_hash = ?",
                        (relayd.secret_hash(first, "manager-link"),)).fetchone()[0] == 0


def test_manager_token_from_stdin(settings, tg, monkeypatch, capsys):
    monkeypatch.setenv("RELAY_TELEGRAM_API", tg.base)
    monkeypatch.setattr(sys, "stdin", io.StringIO(PLAIN_TOKEN + "\n"))
    assert relayd.main(["--db", settings.db, "manager-token"]) == 1          # не менеджер — не записан
    assert relayd.read_manager_token(settings.manager_path) is None
    monkeypatch.setattr(sys, "stdin", io.StringIO(MANAGER_TOKEN + "\n"))
    assert relayd.main(["--db", settings.db, "manager-token"]) == 0
    out = capsys.readouterr()
    assert MANAGER_TOKEN not in out.out + out.err
    assert relayd.read_manager_token(settings.manager_path) == MANAGER_TOKEN
    assert relayd.main(["--db", settings.db, "manager-status", "--api-port", "1"]) == 0
    out = capsys.readouterr().out
    assert "@shturman_manager_bot" in out and "включён" in out and MANAGER_TOKEN not in out
    assert relayd.main(["--db", settings.db, "manager-token", "--remove"]) == 0
    assert not os.path.exists(settings.manager_path)


def test_caddy_of_the_relay_passes_botpair_and_manager_setup():
    caddyfile = (Path(__file__).resolve().parents[1] / "Caddyfile").read_text(encoding="utf-8")
    line = next(l for l in caddyfile.splitlines() if l.strip().startswith("@api path"))
    assert set(line.split()[2:]) == {"/api/join", "/api/check", "/api/health", "/api/ca", "/api/botpair/*",
                                     "/api/manager/setup", "/api/manager/setup.js"}


# --- стенд: служба целиком ------------------------------------------------------------------------------

def test_bench_service_with_poll_thread(settings, tg):
    """Служба как в serve: HTTP-сервер и поток опроса бота-менеджера; «экземпляр» ходит только по HTTP."""
    settings.manager_poll = 1
    relayd.write_manager_token(settings.manager_path, MANAGER_TOKEN)
    _, srv = relayd.make_servers(settings, ("127.0.0.1", 0), ("127.0.0.1", 0))
    stop = threading.Event()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=srv.manager.run, args=(stop,), daemon=True).start()
    try:
        deadline = time.time() + 10
        while not json.loads(call(srv, "GET", "/api/health")[1])["botpair"]:
            assert time.time() < deadline
            time.sleep(0.05)
        out = json.loads(call(srv, "POST", "/api/botpair/start", {"name": "Согласования"})[1])
        bot_id = tg.create_managed_bot(out["username"], "Согласования", OWNER)
        path = f"/api/botpair/{out['pair_id']}/token"
        while True:
            status, body, _ = call(srv, "POST", path, {"secret": out["secret"]})
            if status == 200:
                break
            assert status == 202 and time.time() < deadline
            time.sleep(0.05)
        got = json.loads(body)
        assert got["token"] == tg.token_of(bot_id) and got["creator"]["id"] == OWNER["id"] and got["restricted"] is True
    finally:
        stop.set()
        srv.shutdown()
        srv.server_close()
