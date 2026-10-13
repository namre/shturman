"""Бот с изменённым именем: присоединение к паре по коду подтверждения от создателя (relayd 0.4.1).

Telegram — подставной HTTP-сервер (fake_telegram.py): у каждого управляемого бота своя очередь
обновлений, запросы — настоящим tg_call. Только стандартная библиотека и pytest.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import relayd  # noqa: E402
from fake_telegram import MANAGER_TOKEN  # noqa: E402
from test_botpair import OTHER, OWNER, api, manager, ready, settings, tg  # noqa: E402,F401 — фикстуры
from test_relayd import call  # noqa: E402


def renamed(manager, tg, owner=OWNER, name="shturman_erman_bot"):
    """Пара начата, но владелец изменил имя бота в окне создания: бот пришёл без пары."""
    out = manager.start(None)
    bot_id = tg.create_managed_bot(name, "Согласования", owner)
    manager.poll_once(0)
    return out, bot_id


def test_renamed_bot_waits_unmatched_with_its_public_name_in_the_log(manager, tg, caplog):
    caplog.set_level(logging.INFO)
    ready(manager)
    out, bot_id = renamed(manager, tg)
    assert re.fullmatch(r"[a-z2-9]{12}", out["claim"]) and out["claim"] not in out["link"]
    assert set(manager.unmatched) == {bot_id} and manager.pairs[out["pair_id"]].bot_id is None
    assert "@shturman_erman_bot" in caplog.text and tg.token_of(bot_id) not in caplog.text
    assert out["claim"] not in caplog.text and out["secret"] not in caplog.text
    assert not any(m == "setManagedBotAccessSettings" for m, _ in tg.calls)   # чужой пока бот не трогаем


def test_claim_from_the_creator_attaches_the_renamed_bot(manager, tg, caplog):
    caplog.set_level(logging.INFO)
    ready(manager)
    out, bot_id = renamed(manager, tg, owner={**OWNER, "username": "evgeny_t"})
    assert manager.claim_once() == 1                                    # бот опрошен: сообщений ещё нет
    assert manager.fetch(out["pair_id"], out["secret"])[1]["status"] == "waiting"
    tg.send_to_bot(bot_id, OWNER, "/start " + out["claim"].upper())    # регистр не важен
    manager.clock_obj.now += manager.s.claim_poll_every
    manager.claim_once()
    assert bot_id not in manager.unmatched and manager.counters["claimed"] == 1
    assert tg.managed[bot_id]["restricted"] is True
    gets = [p for b, m, p in tg.bot_calls if b == bot_id and m == "getUpdates"]
    assert all("allowed_updates" not in p and p["timeout"] == 0 for p in gets)   # перечень обновлений бота не меняем
    assert tg.bot_updates[bot_id] == []                                  # сообщение с кодом подтверждено
    assert [p["text"] for b, m, p in tg.bot_calls if m == "sendMessage"] == [relayd.CLAIMED_TEXT]
    status, body, _ = manager.fetch(out["pair_id"], out["secret"])
    assert status == 200 and body["claimed"] is True and body["bot"]["username"] == "shturman_erman_bot"
    assert body["creator"] == {"id": OWNER["id"], "username": "evgeny_t", "name": "Евгений Тестов"}
    assert body["token"] == tg.token_of(bot_id)
    calls = len(tg.bot_calls)
    manager.clock_obj.now += 10
    manager.claim_once()
    assert len(tg.bot_calls) == calls                                    # после присоединения бота не опрашиваем
    assert tg.token_of(bot_id) not in caplog.text and out["claim"] not in caplog.text


def test_claim_needs_the_creator_the_exact_command_and_a_live_pair(manager, tg):
    ready(manager)
    out, bot_id = renamed(manager, tg)
    for user, text in ((OTHER, "/start " + out["claim"]),              # верный код, но не от создателя
                       (OWNER, "/start " + "z" * 12),                   # неверный код
                       (OWNER, out["claim"]),                           # без /start
                       (OWNER, "/start " + out["claim"] + " ещё"),       # не ровно команда
                       (OWNER, "/start " + out["secret"][:12])):       # кусок секрета пары — не код
        tg.send_to_bot(bot_id, user, text)
    tg.send_to_bot(bot_id, OWNER, "/start " + out["claim"], chat_type="group")
    manager.claim_once()
    assert bot_id in manager.unmatched and manager.counters["claimed"] == 0
    assert manager.pairs[out["pair_id"]].claim_failures == 2            # не от создателя; не в личном чате
    assert manager.unmatched[bot_id].wrong == 3                         # от создателя: «/start» с чужим кодом


def test_bot_created_before_the_pair_started_cannot_be_claimed(manager, tg):
    """Злоумышленник с собственной парой не присоединит чужой бот, созданный раньше его пары, — даже
    если владельца уговорили отправить своему боту чужой код."""
    ready(manager)
    victim, bot_id = renamed(manager, tg)
    manager.clock_obj.now += 5
    attacker = manager.start(None)
    tg.send_to_bot(bot_id, OWNER, "/start " + attacker["claim"])
    manager.claim_once()
    assert manager.pairs[attacker["pair_id"]].bot_id is None and manager.pairs[attacker["pair_id"]].claim_failures == 1
    assert manager.fetch(attacker["pair_id"], attacker["secret"])[1]["status"] == "waiting"


def test_claim_failures_lock_the_pair_code(manager, tg):
    ready(manager)
    out, bot_id = renamed(manager, tg)
    for _ in range(relayd.Settings.claim_pair_fail_max):
        tg.send_to_bot(bot_id, OTHER, "/start " + out["claim"])
    tg.send_to_bot(bot_id, OWNER, "/start " + out["claim"])             # теперь и от создателя — поздно
    manager.claim_once()
    assert manager.pairs[out["pair_id"]].bot_id is None


def test_too_many_wrong_codes_from_the_creator_stop_listening(manager, tg):
    ready(manager)
    out, bot_id = renamed(manager, tg)
    for i in range(relayd.Settings.claim_wrong_max):
        tg.send_to_bot(bot_id, OWNER, f"/start {'q' * 11}{i % 10 + 2 if i < 8 else 'z'}")
    manager.claim_once()
    assert bot_id not in manager.unmatched


def test_unmatched_bots_are_kept_only_while_pairs_wait_and_within_limits(manager, tg):
    ready(manager)
    tg.create_managed_bot("lonely_bot", "x", OWNER)                     # пар нет вовсе — не запоминаем
    manager.poll_once(0)
    assert manager.unmatched == {}
    out = manager.start(None)
    manager.s.unmatched_max = 2
    ids = []
    for i in range(3):
        ids.append(tg.create_managed_bot(f"renamed{i}_bot", "x", OWNER))
        manager.poll_once(0)
        manager.clock_obj.now += 1
    assert set(manager.unmatched) == set(ids[1:])                       # самый старый вытеснен
    calls = len(tg.calls) + len(tg.bot_calls)
    assert manager.cancel(out["pair_id"], out["secret"])                # пар больше нет — ботов не опрашиваем
    assert manager.claim_once() == 0 and len(tg.calls) + len(tg.bot_calls) == calls
    manager.start(None)
    manager.clock_obj.now += manager.s.unmatched_ttl + 1
    assert manager.claim_once() == 0 and manager.unmatched == {}


def test_bot_polled_by_someone_else_is_left_alone(manager, tg):
    ready(manager)
    out, bot_id = renamed(manager, tg)
    manager.claim_once()                                                 # токен бота уже получен
    tg.fail["getUpdates"] = [409]
    manager.clock_obj.now += manager.s.claim_poll_every
    manager.claim_once()
    assert bot_id not in manager.unmatched


def test_owner_change_of_an_unmatched_bot_drops_it(manager, tg):
    ready(manager)
    out, bot_id = renamed(manager, tg)
    tg.change_owner(bot_id, OTHER)
    manager.poll_once(0)
    assert bot_id not in manager.unmatched


def test_http_cancel_removes_the_pair(api, manager):
    ready(manager)
    out = json.loads(call(api, "POST", "/api/botpair/start", {})[1])
    assert call(api, "POST", f"/api/botpair/{out['pair_id']}/cancel", {"secret": "D" * 43})[0] == 404
    assert call(api, "POST", f"/api/botpair/{out['pair_id']}/cancel", {"secret": out["secret"]})[0] == 200
    assert call(api, "POST", f"/api/botpair/{out['pair_id']}/token", {"secret": out["secret"]})[0] == 404


def test_transient_poll_errors_are_quiet_and_lose_nothing(settings, tg, caplog):
    """Обрыв долгого опроса — info, а не предупреждение; бот, созданный во время обрывов, не теряется."""
    caplog.set_level(logging.INFO)
    m = relayd.Manager(settings)
    relayd.write_manager_token(settings.manager_path, MANAGER_TOKEN)
    calls = {"n": 0}
    real = relayd.tg_call

    def flaky(base, token, method, params=None, timeout=15):
        if method == "getUpdates" and token == MANAGER_TOKEN and calls["n"] < 2:
            calls["n"] += 1
            raise relayd.TelegramError("reset")
        return real(base, token, method, params, timeout)

    m._tg = flaky
    relayd.write_manager_token(settings.manager_path, MANAGER_TOKEN)
    m.reload()
    m.check_identity()
    out = m.start(None)
    tg.create_managed_bot(out["username"], "x", OWNER)                  # обновление ждёт, пока опрос обрывается
    stop = threading.Event()
    t = threading.Thread(target=m.run, args=(stop,), daemon=True)
    t.start()
    deadline = time.time() + 15
    while m.pairs[out["pair_id"]].bot_id is None:
        assert time.time() < deadline
        time.sleep(0.02)
    stop.set()
    t.join(5)
    assert calls["n"] == 2 and "запрос обновлений прервался (reset)" in caplog.text
    assert not any(r.levelno >= logging.WARNING and "опрос" in r.getMessage() for r in caplog.records)


def test_network_problems_are_named():
    import http.client
    import socket
    import urllib.error
    assert relayd._net_problem(urllib.error.URLError(socket.timeout())) == "timeout"
    assert relayd._net_problem(http.client.RemoteDisconnected("x")) == "reset"
    assert relayd._net_problem(urllib.error.URLError(socket.gaierror())) == "dns"
    assert relayd._net_problem(urllib.error.URLError(ConnectionRefusedError())) == "connect"


def test_bench_service_with_claim_thread(settings, tg):
    """Служба как в serve: HTTP, опрос менеджера и поток ботов без пары; «экземпляр» — только по HTTP."""
    settings.manager_poll = 1
    settings.claim_poll_every = 0.3
    relayd.write_manager_token(settings.manager_path, MANAGER_TOKEN)
    _, srv = relayd.make_servers(settings, ("127.0.0.1", 0), ("127.0.0.1", 0))
    stop = threading.Event()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=srv.manager.run, args=(stop,), daemon=True).start()
    threading.Thread(target=srv.manager.claim_loop, args=(stop,), daemon=True).start()
    try:
        deadline = time.time() + 15
        while not json.loads(call(srv, "GET", "/api/health")[1])["botpair"]:
            assert time.time() < deadline
            time.sleep(0.05)
        out = json.loads(call(srv, "POST", "/api/botpair/start", {})[1])
        bot_id = tg.create_managed_bot("shturman_erman_bot", "Согласования", OWNER)
        while bot_id not in srv.manager.unmatched:
            assert time.time() < deadline
            time.sleep(0.05)
        tg.send_to_bot(bot_id, OWNER, "/start " + out["claim"])
        path = f"/api/botpair/{out['pair_id']}/token"
        while True:
            status, body, _ = call(srv, "POST", path, {"secret": out["secret"]})
            if status == 200:
                break
            assert status == 202 and time.time() < deadline
            time.sleep(0.05)
        got = json.loads(body)
        assert got["claimed"] is True and got["bot"]["username"] == "shturman_erman_bot"
        assert got["token"] == tg.token_of(bot_id) and got["restricted"] is True
    finally:
        stop.set()
        srv.shutdown()
        srv.server_close()
