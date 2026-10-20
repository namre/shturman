"""Разрешения владельца на действия сопровождения (Р-100, Д-29): кнопка в боте вместо фразы в терминале.

Сценарии: запрос → карточка в боте → нажатие владельца → скрипт продолжает (расходует разрешение один раз);
отклонение; истечение; чужой нажимает — отказ; приложение «Штурман» и внутренний API разрешение не выдают;
сервис лежит — путь напрямую через Bot API (подставной Telegram), владелец — из базы или копии сервиса.
"""

import dataclasses
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio

from shturman import authority, bridge, confirm, ops_consent as oc, ops_consent_cli as cli
from shturman.executor.bot import Bot
from shturman.executor.botapi import BotApi
from shturman.executor.worker import Worker

from exec_fakes import (
    BOT_ID, OWNER, OWNER_USER, STRANGER_USER, TOKEN, Clock, FakeTelegram, bind, buttons_of, no_sleep, private,
    refusal,
)

SHA = "a" * 40
UPDATE = {"action": "update", "target": SHA,
          "details": {"from": "0.0.8", "to": "v0.0.9-rc.2", "migrations": "1 (0041_ops_consents.sql)", "scope": "data"}}


@pytest_asyncio.fixture
async def ops_rig(make_client, conn, config):
    """Сервис со своим ботом и маршрутами разрешений; опрос и исполнитель — по шагам из теста."""
    cfg = dataclasses.replace(config, bot_token=TOKEN)
    client, state = await make_client("shturman.api_core", "shturman.ops_consent", cfg=cfg)
    bridge.set_builtin({bridge.NOTIFY_OWNER, bridge.NOTIFY_EDIT})
    tg = FakeTelegram()
    api = BotApi(TOKEN, transport=tg.transport())
    bot = Bot(state, api, poll=0, clock=Clock(), sleep=no_sleep)
    worker = Worker(state, api=api, bot=bot, sleep=no_sleep)
    bot.wake = worker.wake
    await bot.ensure_identity()
    try:
        yield SimpleNamespace(client=client, state=state, conn=conn, tg=tg, api=api, bot=bot, worker=worker, config=cfg)
    finally:
        bridge.set_builtin(())
        await api.aclose()


async def ask(rig, body=None):
    response = await rig.client.post("/api/ops/consents", json=body or UPDATE)
    return response.status_code, response.json()


def cards(rig) -> list[dict]:
    return [p for p in rig.tg.sent() if any(v.startswith("sh:cf:") for v in buttons_of(p).values())]


async def card(rig) -> dict:
    """Карточка дошла до владельца: отправленное ботом сообщение с кнопками."""
    await rig.worker.run_once("bot")
    sent = cards(rig)
    assert sent, "карточка не отправлена"
    return sent[-1]


async def press(rig, sent: dict, label: str, *, user=OWNER_USER, chat=None):
    rig.tg.press(buttons_of(sent)[label], user=user, message_id=rig.tg.last_message_id(), chat=chat)
    await rig.bot.poll_once()


async def use(rig, cid, target=SHA, action="update"):
    response = await rig.client.post(f"/api/ops/consents/{cid}/use", json={"action": action, "target": target})
    return response.status_code, response.json()


async def test_request_card_press_and_one_time_use(ops_rig):
    rig = ops_rig
    await bind(rig)
    code, out = await ask(rig)
    assert code == 202 and out["status"] == "pending" and out["title"]
    sent = await card(rig)
    assert set(buttons_of(sent)) == {"Разрешить", "Отклонить"}
    assert "0.0.8 → v0.0.9-rc.2" in sent["text"] and "ВМЕСТЕ С БАЗОЙ" in sent["text"]
    assert "команда сопровождения" in sent["text"] and "{" not in sent["text"]
    assert sent["chat_id"] == OWNER
    # Тот же запрос ещё раз — та же строка, новой карточки нет.
    code2, again = await ask(rig)
    assert again["consent_id"] == out["consent_id"] and code2 == 202
    await rig.worker.run_once("bot")
    assert len(cards(rig)) == 1
    await press(rig, sent, "Разрешить")
    edited = rig.tg.calls("editMessageText")[-1]["text"]
    assert edited.startswith("Разрешено:")
    code, granted = await ask(rig)
    assert code == 200 and granted["status"] == "granted" and granted["valid_until"]
    # Чужая цель — не израсходовать; своя — один раз.
    assert (await use(rig, out["consent_id"], target="b" * 40))[0] == 409
    assert (await use(rig, out["consent_id"], action="rollback"))[0] == 409
    code, used = await use(rig, out["consent_id"])
    assert code == 200 and used["status"] == "used"
    assert (await use(rig, out["consent_id"]))[0] == 409
    # Следующий запрос с той же целью — новая карточка.
    code, fresh = await ask(rig)
    assert code == 202 and fresh["consent_id"] != out["consent_id"]


async def test_rejection_and_expiry(ops_rig):
    rig = ops_rig
    await bind(rig)
    _, out = await ask(rig)
    sent = await card(rig)
    await press(rig, sent, "Отклонить")
    state = (await rig.client.get(f"/api/ops/consents/{out['consent_id']}")).json()
    assert state["status"] == "rejected"
    assert (await use(rig, out["consent_id"]))[0] == 409
    # Ждущий запрос с вышедшим сроком — expired, нажатие уже не выдаёт.
    _, out2 = await ask(rig)
    sent2 = await card(rig)
    await rig.conn.execute("UPDATE pending_actions SET expires_at = now() - interval '1 minute'")
    await rig.conn.execute("UPDATE ops_consents SET expires_at = now() - interval '1 minute'")
    assert (await rig.client.get(f"/api/ops/consents/{out2['consent_id']}")).json()["status"] == "expired"
    await press(rig, sent2, "Разрешить")
    assert await rig.conn.fetchval("SELECT status FROM ops_consents WHERE id = $1", out2["consent_id"]) == "pending"
    # Выданное, но просроченное разрешение — не израсходовать.
    _, out3 = await ask(rig)
    sent3 = await card(rig)
    await press(rig, sent3, "Разрешить")
    await rig.conn.execute("UPDATE ops_consents SET valid_until = now() - interval '1 minute' WHERE id = $1",
                           out3["consent_id"])
    assert (await rig.client.get(f"/api/ops/consents/{out3['consent_id']}")).json()["status"] == "expired"
    assert (await use(rig, out3["consent_id"]))[0] == 409


async def test_stranger_or_foreign_chat_press_is_refused(ops_rig):
    rig = ops_rig
    await bind(rig)
    _, out = await ask(rig)
    sent = await card(rig)
    await press(rig, sent, "Разрешить", user=STRANGER_USER)
    await press(rig, sent, "Разрешить", user=STRANGER_USER, chat=private(STRANGER_USER))
    await press(rig, sent, "Разрешить", chat={"id": -100500, "type": "supergroup", "title": "Группа"})
    assert {a["text"] for a in rig.tg.calls("answerCallbackQuery")} == {"Кнопка недоступна."}
    assert (await rig.client.get(f"/api/ops/consents/{out['consent_id']}")).json()["status"] == "pending"


async def test_no_route_or_owner_channel_grants_without_a_bot_press(ops_rig):
    """Агент через внутренний API (`shturman call`) и владелец в приложении или на странице — не выдают."""
    rig = ops_rig
    await bind(rig)
    _, out = await ask(rig)
    await card(rig)
    cid, action_id = out["consent_id"], await rig.conn.fetchval("SELECT action_id FROM ops_consents")
    # Ни одного маршрута, который ставит granted: попытки по очевидным адресам.
    for path, body in ((f"/api/ops/consents/{cid}", {"status": "granted"}),
                       (f"/api/ops/consents/{cid}/grant", {}),
                       (f"/api/confirmations/{action_id}", {"choice": "y"}),
                       ("/api/callbacks/telegram", {"data": "sh:cf:y:1:x", "from_user_id": OWNER})):
        response = await rig.client.post(path, json=body)
        assert response.status_code in (403, 404, 405), (path, response.status_code)
    # Функция применения без нажатия под карточкой — отказ: и «как ужесточение», и от имени владельца.
    with pytest.raises(confirm.Refused):
        await confirm.apply(rig.conn, oc.KIND, {"consent_id": cid})
    with authority.setup_context("s1"), pytest.raises(confirm.Refused):
        await confirm.apply_owner(rig.conn, oc.KIND, {"consent_id": cid})
    # Даже в контексте владельца из его чата с ботом — но не нажатием под этой карточкой (например, команда
    # бота, которая просит confirm.request): без нажатия разрешения нет.
    with authority.owner_context(OWNER, chat_id=OWNER, action="telegram.callback"), pytest.raises(confirm.Refused):
        await confirm.request(rig.conn, oc.KIND, "Обновление", {"consent_id": cid})
    # Нажатие «Да» из приложения «Штурман» (Mini App) — не принимается: только в самом боте.
    nonce = await rig.conn.fetchval("SELECT nonce FROM pending_actions WHERE id = $1", action_id)
    with authority.miniapp_context(OWNER, chat_id=OWNER):
        out_app = await bridge.dispatch_callback(rig.conn, f"sh:cf:y:{action_id}:{nonce}", OWNER)
    assert "только в боте" in out_app["answer"]
    # Список «ждёт решения» в приложении показывает карточку без кнопок.
    listed = await confirm.awaiting_owner(rig.conn)
    assert listed and listed[0]["buttons"] == []
    assert await rig.conn.fetchval("SELECT status FROM ops_consents WHERE id = $1", cid) == "pending"


async def test_input_is_checked_and_card_needs_an_owner(ops_rig):
    rig = ops_rig
    for bad in ({"action": "drop_db", "target": SHA},
                {"action": "update", "target": "v0.0.9; rm -rf"},
                {"action": "update", "target": SHA, "details": {"evil": "x"}},
                {"action": "update", "target": SHA, "details": {"to": 5}},
                {"action": "guard_threshold", "target": "0.2"}):
        response = await rig.client.post("/api/ops/consents", json=bad)
        assert response.status_code == 400, bad
    code, out = await ask(rig)                     # владелец не привязан — подтвердить некому
    assert code == 409 and out["code"] == "owner_unknown"
    assert await rig.conn.fetchval("SELECT count(*) FROM ops_consents") == 0


def test_every_action_has_a_plain_card():
    samples = {"update": SHA, "restore": "20261016T081500Z:all:keep", "restore_nocopy": "20261016T081500Z",
               "rollback": SHA, "no_backup": "0123456789abcdef", "guard_threshold": "0.97",
               "backups_delete": "5:0123456789abcdef"}
    assert set(samples) == set(oc.ACTIONS)
    for action, target in samples.items():
        text = oc.summary(action, target, oc.check(action, target, {}))
        assert len(text) > 40 and "{" not in text and "один раз" in text, action


# --- команда ops-consent: ожидание через сервис (подставные ответы API) ---

def scripted(*answers):
    calls, queue = [], list(answers)

    def call(method, path, payload):
        calls.append((method, path))
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    return call, calls


def pending(**extra):
    return {"consent_id": 7, "status": "pending", "title": "обновление", "expires_at": "2026-10-17T12:00:00+00:00", **extra}


def test_cli_waits_then_uses_the_grant(capsys):
    call, calls = scripted((202, pending()), (200, pending()), (200, pending(status="granted")), (200, {"status": "used"}))
    assert cli.ask_service("update", SHA, {}, wait=60, keep=False, call=call, sleep=lambda s: None) == oc.GRANTED
    printed = capsys.readouterr().out
    assert "Ждём подтверждения владельца в боте согласований" in printed and "Разрешено владельцем" in printed
    assert calls[-1] == ("POST", "/api/ops/consents/7/use")


def test_cli_keep_does_not_use_and_timeout_leaves_request(capsys):
    call, calls = scripted((200, pending(status="granted", valid_until="2026-10-17T13:00:00+00:00")))
    assert cli.ask_service("update", SHA, {}, wait=0, keep=True, call=call) == oc.GRANTED
    assert all(not p.endswith("/use") for _, p in calls)
    ticks = iter([0.0, 0.0, 100.0, 100.0])
    call, _ = scripted((202, pending()), (200, pending()))
    assert cli.ask_service("update", SHA, {}, wait=10, keep=False, call=call, sleep=lambda s: None,
                           clock=lambda: next(ticks)) == oc.WAITING
    assert "запустите ту же команду снова" in capsys.readouterr().out


@pytest.mark.parametrize("answer, code", [
    ((200, pending(status="rejected")), oc.DENIED),
    ((200, pending(status="expired")), oc.DENIED),
    ((409, {"code": "owner_unknown", "error": "некому"}), oc.NO_BOT),
    (cli.ServiceDown("x"), oc.NO_SERVICE),
    ((400, {"error": "плохо"}), 2),
])
def test_cli_outcomes(answer, code):
    call, _ = scripted(answer)
    assert cli.ask_service("update", SHA, {}, wait=0, keep=False, call=call) == code


# --- путь без сервиса: напрямую через Bot API ---

OWNER_REC = {"user_id": OWNER, "chat_id": OWNER}


async def direct(tg, *, wait=30.0, presses=(), every=0.0):
    api = BotApi(TOKEN, transport=tg.transport())
    out: list[str] = []
    ticks = {"n": 0}

    def clock():
        return float(ticks["n"])

    async def sleep(_):
        ticks["n"] += 1
        if presses:
            fn = presses[min(ticks["n"] - 1, len(presses) - 1)]
            if fn:
                fn()
    try:
        code = await oc.direct_ask(api, OWNER_REC, "restore", "20261016T081500Z:all:keep", {}, wait=wait,
                                   say=out.append, tz="Europe/Moscow", clock=clock, sleep=sleep)
    finally:
        await api.aclose()
    return code, out


def ours(tg) -> dict:
    return buttons_of(tg.sent()[-1])


async def test_direct_owner_press_grants_without_confirming_other_updates():
    tg = FakeTelegram()
    tg.text("сообщение боту, пока сервис лежит")                 # должно остаться сервису
    tg.push(business_message={"message_id": 9, "chat": {"id": 5, "type": "private"}})

    def owner_presses():
        tg.press(ours(tg)["Разрешить"], message_id=tg.last_message_id())
    code, out = await direct(tg, presses=[owner_presses])
    assert code == oc.GRANTED
    assert any("Ждём подтверждения владельца" in line for line in out)
    sent = tg.sent()[-1]
    assert sent["chat_id"] == OWNER and set(buttons_of(sent)) == {"Разрешить", "Отклонить"}
    assert "Сервис «Штурмана» сейчас не работает" in sent["text"]
    preview = sent.get("link_preview_options")
    assert (json.loads(preview) if isinstance(preview, str) else preview) == {"is_disabled": True}   # Д-39
    assert all(not b.startswith("sh:") for b in buttons_of(sent).values())    # сервис такие кнопки не примет
    polls = tg.calls("getUpdates")
    assert polls and all("offset" not in p and "allowed_updates" not in p for p in polls)
    assert len(tg.updates) == 3                                   # ничего не подтверждено и не забыто
    assert tg.calls("editMessageText")[-1]["text"].startswith("Разрешено:")


async def test_direct_stranger_press_is_refused_and_rejection_denies():
    tg = FakeTelegram()

    def stranger():
        tg.press(ours(tg)["Разрешить"], user=STRANGER_USER, chat=private(STRANGER_USER),
                 message_id=tg.last_message_id(), query_id="q-stranger")

    def stranger_in_owner_chat():   # подделка вида «нажал в чате владельца»: проверяется и нажавший
        tg.press(ours(tg)["Разрешить"], user=STRANGER_USER, message_id=tg.last_message_id(), query_id="q-forged")

    def owner_in_group():
        tg.press(ours(tg)["Разрешить"], chat={"id": -1001, "type": "supergroup", "title": "Г"},
                 message_id=tg.last_message_id(), query_id="q-group")

    def owner_rejects():
        tg.press(ours(tg)["Отклонить"], message_id=tg.last_message_id(), query_id="q-no")
    code, _ = await direct(tg, presses=[stranger, stranger_in_owner_chat, owner_in_group, owner_rejects])
    assert code == oc.DENIED
    answers = {a["callback_query_id"]: a["text"] for a in tg.calls("answerCallbackQuery")}
    assert answers == {"q-stranger": "Кнопка недоступна.", "q-forged": "Кнопка недоступна.",
                       "q-group": "Кнопка недоступна.", "q-no": "Отклонено."}


async def test_direct_old_card_or_forged_data_does_not_grant():
    tg = FakeTelegram()

    def forged():
        tg.press("oc:y:guessed-nonce", message_id=tg.last_message_id())
        tg.press(ours(tg)["Разрешить"], message_id=tg.last_message_id() - 1)   # не та карточка
    code, out = await direct(tg, wait=3, presses=[forged, None])
    assert code == oc.WAITING and "не ответил" in out[-1]
    assert tg.calls("editMessageText")[-1]["text"].startswith("Срок вышел")


async def test_direct_refuses_when_someone_else_polls_the_bot():
    tg = FakeTelegram()
    tg.script["getUpdates"] = [refusal(409, "Conflict: terminated by other getUpdates request")]
    with pytest.raises(oc.DirectRefused) as caught:
        await direct(tg)
    assert caught.value.code == oc.CONFLICT and tg.sent() == []


async def test_direct_full_backlog_is_not_skipped():
    tg = FakeTelegram()
    for i in range(100):
        tg.text(f"m{i}")
    with pytest.raises(oc.DirectRefused) as caught:
        await direct(tg)
    assert caught.value.code == oc.CONFLICT and len(tg.updates) == 100


# --- кто владелец без сервиса ---

def test_owner_copy_roundtrip(tmp_path):
    oc.remember_owner(tmp_path, BOT_ID, OWNER_REC)
    path = oc.owner_path(tmp_path)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oc.read_owner_copy(tmp_path, BOT_ID) == OWNER_REC
    assert oc.read_owner_copy(tmp_path, BOT_ID + 1) is None       # копия о другом боте
    path.write_text(json.dumps({"bot_id": BOT_ID, "user_id": OWNER, "chat_id": -5}))
    assert oc.read_owner_copy(tmp_path, BOT_ID) is None           # не личный чат
    oc.remember_owner(tmp_path, BOT_ID, None)
    assert not path.exists()


async def test_bot_keeps_the_owner_copy_and_direct_path_reads_owner_from_db(ops_rig):
    rig = ops_rig
    await bind(rig)
    await rig.bot.sync_menu()
    assert oc.read_owner_copy(rig.config.data_dir, BOT_ID) == OWNER_REC
    owner, source = await cli.load_owner(rig.config, BOT_ID)
    assert owner == OWNER_REC and source == "база"
    # База недоступна — копия сервиса.
    broken = dataclasses.replace(rig.config, dsn="postgresql://nobody@127.0.0.1:1/none")
    owner, source = await cli.load_owner(broken, BOT_ID)
    assert owner == OWNER_REC and source == "копия сервиса"
    # База говорит «владельца нет» — она главнее копии.
    await bridge.clear_owner(rig.conn)
    owner, source = await cli.load_owner(rig.config, BOT_ID)
    assert owner is None and source == "база"


async def test_cli_direct_without_bot_token_says_so(monkeypatch, capsys):
    monkeypatch.setenv("SHTURMAN_DSN", "postgresql://nobody@127.0.0.1:1/none")
    monkeypatch.setenv("SHTURMAN_API_TOKEN", "a" * 40)
    monkeypatch.setenv("SHTURMAN_MCP_TOKEN", "b" * 40)
    monkeypatch.setenv("SHTURMAN_OPERATOR_TOKEN", "c" * 40)
    monkeypatch.delenv("SHTURMAN_BOT_TOKEN", raising=False)
    monkeypatch.setenv("SHTURMAN_DATA_DIR", "/nonexistent-shturman")
    assert await cli.ask_direct("update", SHA, {}, wait=0) == oc.NO_BOT
    assert "не настроен" in capsys.readouterr().out

