"""Привязка бота кодом, если владелец изменил адрес бота в окне создания (relayd 0.5.0, Р-78).

Бот без пары («сирота») запоминается и закрывается; код пары, присланный боту-менеджеру его
создателем, привязывает бота к паре. Telegram — подставной HTTP-сервер (fake_telegram.py).
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import relayd  # noqa: E402
from fake_telegram import MANAGER_TOKEN  # noqa: E402
from test_botpair import OTHER, OWNER, api, manager, ready, settings, tg  # noqa: E402,F401 — фикстуры
from test_relayd import call  # noqa: E402

RENAMED = "evgeny_own_name_bot"
OWNER_TG = {**OWNER, "username": "evgeny_t"}


def renamed(manager, tg, owner=OWNER, username=RENAMED) -> int:
    """Владелец открыл ссылку создания, изменил адрес бота и нажал «Создать»."""
    bot_id = tg.create_managed_bot(username, "Согласования", owner)
    manager.poll_once(0)
    return bot_id


def say(manager, tg, user, text) -> str | None:
    """Пользователь пишет боту-менеджеру; ответ менеджера (или None — промолчал)."""
    before = len(tg.replies())
    tg.send_to_manager(user, text)
    manager.poll_once(0)
    got = tg.replies()
    assert len(got) - before in (0, 1)
    return got[-1] if len(got) > before else None


def test_start_gives_claim_code_and_link(manager):
    ready(manager)
    out = manager.start(None)
    assert re.fullmatch(r"[a-z2-9]{16}", out["claim_code"]) and len(out["claim_code"]) <= 64
    assert out["claim_link"] == f"https://t.me/shturman_manager_bot?start={out['claim_code']}"
    assert out["claim_code"] not in out["link"] and out["claim_code"] != out["secret"]
    pair = manager.pairs[out["pair_id"]]
    assert pair.claim_hash == relayd.secret_hash(out["claim_code"], "claim")
    assert out["claim_code"] not in repr(pair) and pair.started == manager.clock_obj.now
    assert manager.start(None)["claim_code"] != out["claim_code"]


def test_bot_with_edited_address_is_remembered_and_closed_without_token(manager, tg):
    ready(manager)
    out = manager.start(None)
    bot_id = renamed(manager, tg)
    orphan = manager.orphans[bot_id]
    assert (orphan.bot_username, orphan.creator_id, orphan.restricted) == (RENAMED, OWNER["id"], True)
    assert set(vars(orphan)) == {"bot_id", "bot_username", "bot_name", "creator_id", "seen", "restricted"}   # токена нет
    assert ("setManagedBotAccessSettings", {"user_id": bot_id, "is_access_restricted": True}) in tg.calls
    assert not any(m == "getManagedBotToken" for m, _ in tg.calls)
    assert manager.fetch(out["pair_id"], out["secret"])[1]["status"] == "waiting"


def test_code_from_the_creator_binds_the_bot(manager, tg, caplog):
    caplog.set_level(logging.DEBUG)
    ready(manager)
    out = manager.start("Пульт")
    bot_id = renamed(manager, tg, owner=OWNER_TG)
    reply = say(manager, tg, OWNER_TG, f"/start {out['claim_code']}")
    assert reply.startswith(f"Готово: бот @{RENAMED} привязан") and "страницу настройки" in reply
    assert bot_id not in manager.orphans and manager.counters["claimed"] == 1
    status, body, _ = manager.fetch(out["pair_id"], out["secret"])
    assert status == 200 and body["token"] == tg.token_of(bot_id) and body["claimed"] is True
    assert body["bot"]["id"] == bot_id and body["bot"]["username"] == RENAMED
    assert body["creator"] == {"id": OWNER["id"], "username": "evgeny_t", "name": "Евгений Тестов"}
    assert body["restricted"] is True
    # Ни токена, ни кода, ни секрета пары — ни в журнале, ни в ответах менеджера в чат.
    text = caplog.text + json.dumps(tg.replies(), ensure_ascii=False)
    for secret in (tg.token_of(bot_id), out["claim_code"], out["secret"], MANAGER_TOKEN):
        assert secret not in text
    assert out["pair_id"] in caplog.text


def test_bot_found_by_address_says_not_claimed(manager, tg):
    ready(manager)
    out = manager.start(None)
    tg.create_managed_bot(out["username"], "x", OWNER)
    manager.poll_once(0)
    assert manager.fetch(out["pair_id"], out["secret"])[1]["claimed"] is False


def test_code_from_someone_else_gives_nothing(manager, tg):
    """Код подсмотрели: посторонний присылает его менеджеру. Бот владельца ему не достаётся."""
    ready(manager)
    out = manager.start(None)
    bot_id = renamed(manager, tg)
    reply = say(manager, tg, OTHER, f"/start {out['claim_code']}")
    assert reply == relayd.CLAIM_TEXTS["no_bot"] and RENAMED not in reply
    assert bot_id in manager.orphans
    assert manager.fetch(out["pair_id"], out["secret"])[1]["status"] == "waiting"
    # Создатель — тот же код — привязывает.
    assert say(manager, tg, OWNER, out["claim_code"]).startswith("Готово")


def test_residual_risk_owner_persuaded_to_send_someone_elses_code(manager, tg):
    """Остаточный риск (Р-78): если создатель бота сам пришлёт чужой код, его бот достанется чужой паре.
    Чужой экземпляр получит бота, а страница этого экземпляра покажет создателя — и спросит «Это ваш
    аккаунт?». Здесь — проверка, что именно так и выходит: создатель в выдаче — тот, кто прислал код."""
    ready(manager)
    victim_bot = renamed(manager, tg, owner=OWNER)
    stranger = manager.start(None)
    assert say(manager, tg, OWNER, stranger["claim_code"]).startswith("Готово")
    body = manager.fetch(stranger["pair_id"], stranger["secret"])[1]
    assert body["bot"]["id"] == victim_bot and body["creator"]["id"] == OWNER["id"]


def test_already_bound_pair(manager, tg):
    ready(manager)
    out = manager.start(None)
    tg.create_managed_bot(out["username"], "x", OWNER)                  # узнан по адресу
    manager.poll_once(0)
    assert say(manager, tg, OWNER, out["claim_code"]) == relayd.CLAIM_TEXTS["already"].format(bot=out["username"])
    # Посторонний с тем же кодом об этом боте ничего не узнаёт.
    reply = say(manager, tg, OTHER, out["claim_code"])
    assert reply == relayd.CLAIM_TEXTS["no_bot"] and out["username"] not in reply


def test_expired_or_wrong_code(manager, tg):
    ready(manager)
    out = manager.start(None)
    bot_id = renamed(manager, tg)
    assert say(manager, tg, OWNER, "a" * 16) == relayd.CLAIM_TEXTS["unknown_code"]
    manager.clock_obj.now += 901                                         # пара истекла
    assert say(manager, tg, OWNER, out["claim_code"]) == relayd.CLAIM_TEXTS["unknown_code"]
    assert bot_id in manager.orphans                                     # бот помнится дальше
    # Новая пара — новый код — тот же бот.
    again = manager.start(None)
    assert say(manager, tg, OWNER, again["claim_code"]).startswith("Готово")
    assert manager.fetch(again["pair_id"], again["secret"])[1]["bot"]["id"] == bot_id


def test_no_bot_of_this_user(manager, tg):
    ready(manager)
    out = manager.start(None)
    assert say(manager, tg, OWNER, out["claim_code"]) == relayd.CLAIM_TEXTS["no_bot"]


def test_several_orphans_newest_created_after_start_wins(manager, tg):
    ready(manager)
    clock = manager.clock_obj
    old = renamed(manager, tg, username="old_one_bot")
    clock.now += 60
    older_newest = renamed(manager, tg, username="old_two_bot")
    clock.now += 60
    out = manager.start(None)
    clock.now += 10
    after_1 = renamed(manager, tg, username="new_one_bot")
    clock.now += 10
    after_2 = renamed(manager, tg, username="new_two_bot")
    renamed(manager, tg, owner=OTHER, username="strangers_bot")         # чужой, ещё свежее
    clock.now += 10
    assert say(manager, tg, OWNER, out["claim_code"]).startswith("Готово: бот @new_two_bot")
    assert manager.fetch(out["pair_id"], out["secret"])[1]["bot"]["id"] == after_2
    assert {after_1, old, older_newest} <= set(manager.orphans)
    # Если созданных после начала пары нет — самый свежий из прежних.
    for bid in (after_1,):
        del manager.orphans[bid]
    out2 = manager.start(None)
    assert say(manager, tg, OWNER, out2["claim_code"]).startswith("Готово: бот @old_two_bot")


def test_orphans_are_forgotten_after_a_day(manager, tg):
    ready(manager)
    bot_id = renamed(manager, tg)
    manager.clock_obj.now += 24 * 3600 + 1
    out = manager.start(None)
    assert say(manager, tg, OWNER, out["claim_code"]) == relayd.CLAIM_TEXTS["no_bot"]
    assert bot_id not in manager.orphans


def test_orphans_are_capped(manager, tg):
    ready(manager)
    manager.s.botpair_orphans_per_creator = 2
    manager.s.botpair_max_orphans = 3
    clock = manager.clock_obj
    first = renamed(manager, tg, username="a1_bot")
    clock.now += 1
    renamed(manager, tg, username="a2_bot")
    clock.now += 1
    renamed(manager, tg, username="a3_bot")                              # у создателя — не больше двух
    assert first not in manager.orphans and len(manager.orphans) == 2
    clock.now += 1
    renamed(manager, tg, owner=OTHER, username="b1_bot")
    clock.now += 1
    renamed(manager, tg, owner={**OTHER, "id": 7777}, username="c1_bot")  # всего — не больше трёх
    assert len(manager.orphans) == 3
    assert sorted(o.bot_username for o in manager.orphans.values()) == ["a3_bot", "b1_bot", "c1_bot"]


def test_orphan_with_changed_owner_is_forgotten(manager, tg):
    ready(manager)
    bot_id = renamed(manager, tg)
    tg.change_owner(bot_id, OTHER)
    manager.poll_once(0)
    assert bot_id not in manager.orphans and manager.counters["conflicts"] == 1
    out = manager.start(None)
    assert say(manager, tg, OTHER, out["claim_code"]) == relayd.CLAIM_TEXTS["no_bot"]


def test_delivered_bot_does_not_come_back_as_orphan(manager, tg):
    """Позже владелец меняет токен в @BotFather — приходит managed_bot о том же боте. Он уже выдан."""
    ready(manager)
    out = manager.start(None)
    bot_id = tg.create_managed_bot(out["username"], "x", OWNER)
    manager.poll_once(0)
    assert manager.fetch(out["pair_id"], out["secret"])[0] == 200
    tg.change_owner(bot_id, OWNER)                                       # то же обновление, что при смене токена
    manager.poll_once(0)
    assert manager.orphans == {}


def test_unknown_messages_get_short_help_and_other_chats_are_ignored(manager, tg):
    ready(manager)
    out = manager.start(None)
    renamed(manager, tg)
    for text in ("/start", "привет", "/help", "/start a b"):
        assert say(manager, tg, OWNER, text) == relayd.CLAIM_TEXTS["help"]
    # Код можно ввести и руками: регистр и пробелы не важны.
    assert say(manager, tg, OWNER, "  " + out["claim_code"].upper() + " ").startswith("Готово")
    # Группы, боты и чужой номер чата — молчание.
    other = manager.start(None)
    before = len(tg.replies())
    tg.send_to_manager(OWNER, f"/start {other['claim_code']}", chat_type="group")
    manager.handle_update({"message": {"from": {**OWNER, "is_bot": True}, "chat": {"id": OWNER["id"], "type": "private"},
                                       "text": other["claim_code"]}})
    manager.handle_update({"message": {"from": OWNER, "chat": {"id": 1, "type": "private"}, "text": other["claim_code"]}})
    manager.handle_update({"message": "x"})
    manager.poll_once(0)
    assert len(tg.replies()) == before


def test_attempts_are_limited_per_user_and_in_total(manager, tg):
    ready(manager)
    manager.claim_per_user = relayd.Window(3, 900)
    manager.claim_told = relayd.Window(1, 900)
    out = manager.start(None)
    renamed(manager, tg)
    for _ in range(3):
        assert say(manager, tg, OWNER, "b" * 16) == relayd.CLAIM_TEXTS["unknown_code"]
    assert say(manager, tg, OWNER, out["claim_code"]) == relayd.CLAIM_TEXTS["too_many"]   # верный код — тоже нет
    assert say(manager, tg, OWNER, out["claim_code"]) is None                              # дальше — молчание
    manager.clock_obj.now += 901                                                           # окно прошло — снова отвечает
    assert say(manager, tg, OWNER, out["claim_code"]) == relayd.CLAIM_TEXTS["unknown_code"]   # а пара за это время истекла
    # Общий предел неудачных кодов: перебор с многих аккаунтов останавливает всех на минуту.
    manager.claim_per_user = relayd.Window(100, 900)
    manager.claim_fails = relayd.Window(2, 60)
    fresh = manager.start(None)
    renamed(manager, tg, username="another_bot")
    for i in range(2):
        assert say(manager, tg, {**OTHER, "id": 7000 + i}, "c" * 16) == relayd.CLAIM_TEXTS["unknown_code"]
    assert say(manager, tg, OWNER, fresh["claim_code"]) == relayd.CLAIM_TEXTS["busy"]
    manager.clock_obj.now += 61
    assert say(manager, tg, OWNER, fresh["claim_code"]).startswith("Готово")


def test_claim_code_parsing():
    code = "abcdefghjkmnpqrs"
    assert relayd.claim_code_of(f"/start {code}") == code
    assert relayd.claim_code_of(code.upper()) == code
    for bad in (None, 5, "", "/start", f"/start {code} x", f"/other {code}", code[:-1], code + "a", "x" * 300,
                "abcdefghjkmnpqr1"):                                    # «1» — не из алфавита
        assert relayd.claim_code_of(bad) is None, bad


def test_http_start_gives_claim_link(api, manager):
    ready(manager)
    status, body, _ = call(api, "POST", "/api/botpair/start", {})
    out = json.loads(body)
    assert status == 200 and out["claim_link"] == f"https://t.me/{out['manager']}?start={out['claim_code']}"


def test_bench_claim_with_poll_thread(settings, tg, caplog):
    """Служба как в serve: поток опроса принимает и managed_bot, и сообщение с кодом; «экземпляр» — по HTTP."""
    caplog.set_level(logging.DEBUG)
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
        out = json.loads(call(srv, "POST", "/api/botpair/start", {})[1])
        bot_id = tg.create_managed_bot(RENAMED, "Согласования", OWNER)
        while bot_id not in srv.manager.orphans:
            assert time.time() < deadline
            time.sleep(0.05)
        tg.send_to_manager(OWNER, f"/start {out['claim_code']}")
        path = f"/api/botpair/{out['pair_id']}/token"
        while True:
            status, body, _ = call(srv, "POST", path, {"secret": out["secret"]})
            if status == 200:
                break
            assert status == 202 and time.time() < deadline
            time.sleep(0.05)
        got = json.loads(body)
        assert got["claimed"] is True and got["bot"]["username"] == RENAMED and got["token"] == tg.token_of(bot_id)
        assert tg.replies(OWNER["id"])[-1].startswith(f"Готово: бот @{RENAMED}")
        assert out["claim_code"] not in caplog.text and tg.token_of(bot_id) not in caplog.text
    finally:
        stop.set()
        srv.shutdown()
        srv.server_close()
