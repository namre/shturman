"""Маршруты /api/tg/… на сервисе, поднятом как в работе, с подставным клиентом Telegram."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

from telethon import errors

from shturman import authority, bridge, store
from shturman.tg.client import session_path
from shturman.tg.manager import TgManager

from tg_fakes import (C_NEWS, C_SUPER, G_FAMILY, GROUP, HELPER, HELPER_ID, IVAN, ME, PASSWORD, SELF_ID, U_BOT,
                      U_IVAN, U_MARIA, U_TELEGRAM, World, msg, tg_config, wait_for)

MODULES = ("shturman.api_core", "shturman.tg.service")


async def service(make_client, config, world, *, owner=SELF_ID, sending=True):
    client, state = await make_client(*MODULES, cfg=tg_config(config, sending=sending))
    if owner is not None:      # управляющий чат привязан: сервис знает, кто владелец
        assert (await client.put("/api/owner", json={"user_id": owner, "chat_id": owner})).status_code == 200
    manager = state.extras["tg"]
    assert isinstance(manager, TgManager)
    assert manager.runtimes == {}          # на диске сессий нет — настоящий клиент не создавался
    manager.client_factory = world.factory
    manager.pacing = 0.0
    return client, state, manager


async def setup_request(client, method, path, **kwargs):
    """Simulate the setup handler's authenticated session for one chosen request."""
    with authority.setup_context("test-tg-owner-session", action=path):
        return await client.request(method, path, **kwargs)


async def login(client, world, role="assistant", **extra):
    started = (await setup_request(client, "POST", "/api/tg/login", json={"role": role, **extra})).json()
    world.last.scan.set_result(world.me)
    for _ in range(200):
        status = (await client.get(f"/api/tg/login/{started['login_id']}")).json()
        if status["status"] != "pending":
            break
        await asyncio.sleep(0.01)
    return started, status


async def test_module_is_idle_and_says_so_without_app_keys(make_client):
    client, state = await make_client(*MODULES)
    manager = state.extras["tg"]
    assert manager.configured is False and manager.runtimes == {}
    assert manager.can_send(1) is False
    for method, path in (("GET", "/api/tg/accounts"), ("POST", "/api/tg/login"),
                         ("GET", "/api/tg/login/abc"), ("GET", "/api/tg/accounts/1/dialogs"),
                         ("POST", "/api/tg/accounts/1/logout"), ("GET", "/api/tg/accounts/1/sync")):
        response = await client.request(method, path, json={"role": "assistant"} if method == "POST" else None)
        assert response.status_code == 503
        # Основной путь — страница настройки переписки, шаг 1; переменные окружения — запасной.
        assert "TELEGRAM_API_ID" in response.json()["error"] and "шаг 1" in response.json()["error"]
    assert (await client.get("/api/tg/accounts", headers={"Authorization": "Bearer nope"})).status_code == 401


async def test_qr_login_over_http_then_account_is_listed(make_client, config):
    world = World(HELPER)
    world.authorized = False
    client, state, manager = await service(make_client, config, world)
    assert (await client.get("/api/tg/accounts")).json() == {"accounts": []}
    assert (await setup_request(client, "POST", "/api/tg/login", json={"role": "admin"})).status_code == 400

    started = (await setup_request(client, "POST", "/api/tg/login", json={"role": "assistant"})).json()
    assert started["status"] == "pending" and started["qr_svg"].startswith("<svg")
    assert started["link"].startswith("tg://login?token=") and started["expires_at"]
    assert world.last.policy.login is True and world.last.role == "assistant"
    # повторное открытие экрана входа отменяет прежний код
    again = (await setup_request(client, "POST", "/api/tg/login", json={"role": "assistant"})).json()
    assert again["login_id"] != started["login_id"]
    assert (await client.get(f"/api/tg/login/{started['login_id']}")).json()["status"] == "cancelled"

    world.last.scan.set_result(HELPER)
    await wait_for(lambda: manager.flows[again["login_id"]].done)
    done = (await client.get(f"/api/tg/login/{again['login_id']}")).json()
    assert done["status"] == "completed" and "qr_svg" not in done and "link" not in done
    await wait_for(lambda: manager.runtimes["assistant"].status == "running")
    assert world.last.policy.login is False              # запросы входа больше не проходят

    listing = (await client.get("/api/tg/accounts")).json()["accounts"]
    assert len(listing) == 1
    account = listing[0]
    assert (account["role"], account["status"], account["can_send"], account["paused"]) == \
           ("assistant", "running", True, False)
    assert account["account_id"] == done["account_id"] and account["label"] == "Помощник"
    assert set(account) == {"role", "account_id", "tg_user_id", "label", "status", "error", "paused",
                            "can_send", "auto_personal", "auto_groups", "backfill_months", "logged_in_at"}
    assert account["backfill_months"] == 12
    assert session_path(manager.config, "assistant").exists()
    # второй вход в занятую роль — отказ
    busy = await setup_request(client, "POST", "/api/tg/login", json={"role": "assistant"})
    assert busy.status_code == 409 and "выйдите" in busy.json()["error"]
    assert (await client.get("/api/tg/login/nope")).status_code == 404


async def test_two_factor_login_over_http_never_echoes_password(make_client, config, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    world = World(HELPER)
    world.authorized, world.password = False, PASSWORD
    client, state, manager = await service(make_client, config, world)
    started = (await setup_request(client, "POST", "/api/tg/login", json={"role": "assistant"})).json()
    url = f"/api/tg/login/{started['login_id']}"
    early = await client.post(f"{url}/password", json={"password": PASSWORD})
    assert early.status_code == 409                       # пароль ещё не требуется
    world.last.scan.set_exception(errors.SessionPasswordNeededError(None))
    await wait_for(lambda: manager.flows[started["login_id"]].status == "password_required")
    status = (await client.get(url)).json()
    assert status["status"] == "password_required" and status["hint"] == "кличка кота"
    assert (await client.post(f"{url}/password", json={})).status_code == 400
    wrong = (await client.post(f"{url}/password", json={"password": "не тот"})).json()
    assert wrong["status"] == "password_required" and wrong["error"] and wrong["attempts_left"] == 2
    right = await client.post(f"{url}/password", json={"password": PASSWORD})
    assert right.json()["status"] == "completed"
    for response in (wrong, right.json(), (await client.get(url)).json(),
                     (await client.get("/api/tg/accounts")).json()):
        assert PASSWORD not in json.dumps(response, ensure_ascii=False)
    assert PASSWORD not in caplog.text and "QRTOKEN" not in caplog.text
    async with state.pool.acquire() as conn:
        dump = await conn.fetchval(
            "SELECT string_agg(t::text, ' ') FROM (SELECT * FROM tg_sessions) t") or ""
        assert PASSWORD not in dump


async def test_cancel_and_failed_start_clean_up_session_file(make_client, config):
    world = World(HELPER)
    world.authorized = False
    client, state, manager = await service(make_client, config, world)
    path = session_path(manager.config, "assistant")
    started = (await setup_request(client, "POST", "/api/tg/login", json={"role": "assistant"})).json()
    assert path.exists()
    cancelled = (await client.post(f"/api/tg/login/{started['login_id']}/cancel")).json()
    assert cancelled["status"] == "cancelled" and not path.exists() and not world.last.connected
    assert (await client.get("/api/tg/accounts")).json() == {"accounts": []}
    world.connect_error = ConnectionError("нет сети")
    failed = await setup_request(client, "POST", "/api/tg/login", json={"role": "assistant"})
    assert failed.status_code == 502 and not path.exists()
    world.connect_error = errors.FloodWaitError(None, 600)
    assert (await setup_request(client, "POST", "/api/tg/login", json={"role": "assistant"})).status_code == 429
    world.connect_error = None
    assert (await setup_request(client, "POST", "/api/tg/login", json={"role": "assistant"})).status_code == 200   # блокировки отпущены


async def test_owner_login_needs_explicit_confirmation_and_stays_read_only(make_client, config):
    world = World(ME)
    world.authorized = False
    client, state, manager = await service(make_client, config, world)
    refused = await setup_request(client, "POST", "/api/tg/login", json={"role": "owner"})
    assert refused.status_code == 400 and "confirm_owner" in refused.json()["error"]
    assert world.clients == []
    _, status = await login(client, world, "owner", confirm_owner=True)
    assert status["status"] == "completed"
    await wait_for(lambda: manager.runtimes["owner"].status == "running")
    account = (await client.get("/api/tg/accounts")).json()["accounts"][0]
    assert (account["role"], account["can_send"]) == ("owner", False)
    assert world.last.policy.can_send is False


async def test_owner_account_is_rejected_as_assistant_and_its_session_is_terminated(make_client, config, conn):
    await bridge.set_owner(conn, SELF_ID, SELF_ID)          # владелец управляющего чата известен
    world = World(ME)                                       # код отсканировали основным аккаунтом
    world.authorized = False
    client, state, manager = await service(make_client, config, world, owner=None)
    _, status = await login(client, world, "assistant")
    assert status["status"] == "failed" and "основной аккаунт" in status["error"]
    assert world.last.logged_out is True                    # сессия не осталась висеть в Telegram
    assert not session_path(manager.config, "assistant").exists()
    assert manager.runtimes == {} and (await client.get("/api/tg/accounts")).json() == {"accounts": []}
    assert await conn.fetchval("SELECT count(*) FROM accounts") == 0
    # то же — если аккаунт уже есть в архиве как основной (например, из экспорта)
    await conn.execute("DELETE FROM settings")
    await store.ensure_account(conn, SELF_ID, "Владелец", "owner")
    _, status = await login(client, world, "assistant")
    assert status["status"] == "failed" and world.last.logged_out is True


async def test_dialogs_selection_sync_and_status(make_client, config):
    world = World(ME)
    world.authorized = False
    world.dialogs = [U_IVAN, U_MARIA, U_BOT, U_TELEGRAM, G_FAMILY, C_SUPER, C_NEWS]
    world.add(*[msg(i, ("user", IVAN), f"личное {i}") for i in range(1, 121)])
    world.add(*[msg(i, ("chat", GROUP), f"семья {i}", sender=2002) for i in range(200, 205)])
    world.add(*[msg(i, ("channel", 4001), f"пост {i}", post=True) for i in range(1, 31)])
    client, state, manager = await service(make_client, config, world)
    _, status = await login(client, world, "owner", confirm_owner=True)
    account_id = status["account_id"]
    base = f"/api/tg/accounts/{account_id}"
    await wait_for(lambda: manager.runtimes["owner"].status == "running")

    page = (await client.get(f"{base}/dialogs?limit=3")).json()
    assert page["total"] == 7 and page["offset"] == 0 and len(page["items"]) == 3
    assert page["items"][0] == {"peer_class": "user", "tg_id": IVAN, "type": "personal_chat",
                                "title": "Иван Петров", "username": "ivan_p", "enabled": False,
                                "excluded": False, "message_estimate": None}
    rest = (await client.get(f"{base}/dialogs?offset=3&limit=100")).json()["items"]
    by_id = {i["tg_id"]: i for i in page["items"] + rest}
    assert by_id[777000]["excluded"] is True                 # служебный чат Telegram
    assert by_id[4001]["message_estimate"] == 30 and by_id[4001]["type"] == "public_channel"
    groups = (await client.get(f"{base}/dialogs?type=private_group")).json()
    assert [i["tg_id"] for i in groups["items"]] == [GROUP] and groups["total"] == 1
    assert (await client.get(f"{base}/dialogs?type=nonsense")).status_code == 400
    assert (await client.get(f"{base}/dialogs?limit=many")).status_code == 400

    # до выбора в архиве нет ни чатов, ни сообщений
    assert (await client.get("/api/status")).json()["chats"] == 0
    empty = (await client.get(f"{base}/sync")).json()
    assert empty["counts"]["enabled"] == 0 and empty["chats"] == []

    assert (await setup_request(client, "POST", f"{base}/sync", json={"enabled": True})).status_code == 400
    assert (await setup_request(client, "POST", f"{base}/sync", json={"chats": [{"peer_class": "user", "tg_id": IVAN}]})).status_code == 400
    assert (await setup_request(client, "POST", f"{base}/sync", json={"enabled": True, "chats": [{"peer_class": "x", "tg_id": 1}]})).status_code == 400
    one = (await setup_request(client, "POST", f"{base}/sync", json={
        "enabled": True, "chats": [{"peer_class": "user", "tg_id": IVAN}, {"peer_class": "user", "tg_id": 424242}]})).json()
    assert one["chats"][0] == {"peer_class": "user", "tg_id": IVAN, "enabled": True}
    assert one["chats"][1]["enabled"] is False and "не найден" in one["chats"][1]["error"]
    bulk = (await setup_request(client, "POST", f"{base}/sync", json={"enabled": True, "types": ["private_group", "public_channel"]})).json()
    assert sorted(c["tg_id"] for c in bulk["chats"] if c["enabled"]) == [GROUP, 4001]

    rt = manager.runtimes["owner"]
    await wait_for(lambda: rt.history.idle and not rt.wake.is_set())
    status = (await client.get(f"{base}/sync")).json()
    assert status["status"] == "running" and status["flood_wait_until"] is None
    assert status["counts"] == {"enabled": 3, "backfill_done": 3, "backfill_pending": 0, "access_lost": 0,
                                "errors": 0, "messages": 155}
    ivan = next(c for c in status["chats"] if c["tg_id"] == IVAN)
    assert (ivan["backfill_before"], ivan["backfill_done"], ivan["forward_id"], ivan["messages"]) == (1, True, 120, 120)
    since = datetime.fromisoformat(ivan["backfill_since"])      # глубина по умолчанию — 12 месяцев
    assert timedelta(days=360) < datetime.now(timezone.utc) - since < timedelta(days=370)
    assert "title" not in ivan and "личное" not in json.dumps(status, ensure_ascii=False)   # только счётчики и курсоры
    listed = (await client.get(f"{base}/dialogs")).json()["items"]
    assert [i["tg_id"] for i in listed if i["enabled"]] == [IVAN, GROUP, 4001]

    off = (await setup_request(client, "POST", f"{base}/sync", json={"enabled": False, "types": ["public_channel"]})).json()
    assert off["chats"] == [{"peer_class": "channel", "tg_id": 4001, "enabled": False}]
    assert (await client.get(f"{base}/sync")).json()["counts"]["enabled"] == 2
    assert (await client.get("/api/status")).json()["messages"] == 155     # сохранённое остаётся

    options = await setup_request(client, "PUT", f"{base}/options", json={"auto_personal": True})
    assert options.status_code == 200
    assert (await setup_request(client, "PUT", f"{base}/options", json={"auto_groups": "да"})).status_code == 400
    account = (await client.get("/api/tg/accounts")).json()["accounts"][0]
    assert (account["auto_personal"], account["auto_groups"]) == (True, False)
    assert (await client.get("/api/tg/accounts/999/sync")).status_code == 404


async def test_pause_resume_and_logout(make_client, config):
    world = World(HELPER)
    world.authorized = False
    world.dialogs = [U_IVAN]
    client, state, manager = await service(make_client, config, world)
    _, status = await login(client, world, "assistant")
    account_id = status["account_id"]
    base = f"/api/tg/accounts/{account_id}"
    path = session_path(manager.config, "assistant")
    await wait_for(lambda: manager.runtimes["assistant"].status == "running")
    first_client = world.last

    assert (await client.post(f"{base}/pause")).json() == {"ok": True}
    assert not first_client.connected and path.exists()      # отключён, но сессия цела
    account = (await client.get("/api/tg/accounts")).json()["accounts"][0]
    assert (account["status"], account["paused"], account["can_send"]) == ("paused", True, False)
    assert (await client.get(f"{base}/dialogs")).status_code == 409
    assert (await setup_request(client, "POST", "/api/tg/login", json={"role": "assistant"})).status_code == 409
    await manager.start()                                    # перезапуск сервиса паузу не снимает
    assert "assistant" not in manager.runtimes

    world.authorized = True                                  # сессия на диске действительна
    assert (await setup_request(client, "POST", f"{base}/resume")).json() == {"ok": True}
    await wait_for(lambda: manager.runtimes["assistant"].status == "running")
    assert world.last is not first_client and manager.can_send(account_id) is True

    out = (await client.post(f"{base}/logout")).json()
    assert out == {"ok": True, "terminated": True}
    assert world.last.logged_out is True and not path.exists()
    assert (await client.get("/api/tg/accounts")).json() == {"accounts": []}
    assert manager.can_send(account_id) is False
    assert (await client.post(f"{base}/logout")).status_code == 404
    async with state.pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM accounts") == 1    # архив и аккаунт остаются
    # после выхода роль свободна для нового входа
    world.authorized = False
    _, again = await login(client, world, "assistant")
    assert again["status"] == "completed" and again["account_id"] == account_id


async def test_logout_of_paused_account_terminates_session(make_client, config):
    world = World(HELPER)
    world.authorized = False
    client, state, manager = await service(make_client, config, world)
    _, status = await login(client, world, "assistant")
    base = f"/api/tg/accounts/{status['account_id']}"
    await wait_for(lambda: manager.runtimes["assistant"].status == "running")
    await client.post(f"{base}/pause")
    world.authorized = True
    out = (await client.post(f"{base}/logout")).json()
    assert out == {"ok": True, "terminated": True} and world.last.logged_out is True
    assert not session_path(manager.config, "assistant").exists()


async def test_login_replaces_revoked_session_left_on_disk(make_client, config):
    world = World(HELPER)
    world.authorized = False                 # сессию отозвали из приложения Telegram
    client, state, manager = await service(make_client, config, world)
    path = session_path(manager.config, "assistant")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"old")
    await manager.start()
    rt = manager.runtimes["assistant"]
    await wait_for(lambda: rt.task.done())
    listing = (await client.get("/api/tg/accounts")).json()["accounts"]
    assert [(a["role"], a["status"], a["account_id"]) for a in listing] == [("assistant", "unauthorized", None)]
    assert "заново" in listing[0]["error"]
    _, status = await login(client, world, "assistant")
    assert status["status"] == "completed"
    await wait_for(lambda: manager.runtimes["assistant"].status == "running")
    assert path.read_bytes() == b""          # прежний файл убран, создан новый


async def test_service_shutdown_disconnects_sessions(make_client, config):
    world = World(HELPER)
    world.authorized = False
    client, state, manager = await service(make_client, config, world)
    await login(client, world, "assistant")
    await wait_for(lambda: manager.runtimes["assistant"].status == "running")
    running = world.last
    pending = (await setup_request(client, "POST", "/api/tg/login", json={"role": "owner", "confirm_owner": True})).json()
    flow = manager.flows[pending["login_id"]]
    await manager.stop()
    assert not running.connected and flow.status == "cancelled"
    assert session_path(manager.config, "assistant").exists()        # сессия помощника цела
    assert not session_path(manager.config, "owner").exists()        # недоделанный вход убран


# --- основной аккаунт не становится помощником; главный выключатель; глубина истории ---

async def test_assistant_login_is_refused_until_owner_is_known(make_client, config, conn):
    """Пока сервис не знает владельца, он не отличит его аккаунт от помощника — вход не начинается."""
    world = World(ME)
    world.authorized = False
    client, state, manager = await service(make_client, config, world, owner=None)
    refused = await setup_request(client, "POST", "/api/tg/login", json={"role": "assistant"})
    assert refused.status_code == 409
    # Отказ называет оба пути: основной — подключить свой аккаунт на странице настройки (шаг 2),
    # запасной — привязка к боту. Мастера Hermes он не упоминает: сервис не знает, установлен ли Hermes.
    text = refused.json()["error"]
    assert "подключите основной аккаунт" in text and "шаг 2" in text and "привяжите" in text
    assert "мастер" not in text.lower() and "Hermes" not in text
    assert world.clients == [] and manager.flows == {}       # ни клиента, ни кода
    assert not session_path(manager.config, "assistant").exists()
    # вход основного аккаунта (только чтение) от этого не зависит
    assert (await setup_request(client, "POST", "/api/tg/login", json={"role": "owner", "confirm_owner": True})).status_code == 200
    await manager.cancel_login(next(iter(manager.flows)))
    # владелец известен по архиву (экспорт загружен) — этого достаточно
    await store.ensure_account(conn, SELF_ID, "Владелец", "owner")
    world.me = HELPER
    _, status = await login(client, world, "assistant")
    assert status["status"] == "completed"


async def test_owner_becoming_known_stops_assistant_session_of_the_same_account(make_client, config, conn):
    """Владельцем управляющего чата стал аккаунт, уже подключённый как помощник."""
    world = World(HELPER)
    world.authorized = False
    client, state, manager = await service(make_client, config, world)       # владелец — другой аккаунт
    _, status = await login(client, world, "assistant")
    account_id = status["account_id"]
    await wait_for(lambda: manager.runtimes["assistant"].status == "running")
    assert manager.can_send(account_id) is True
    session = world.last

    # тот же владелец привязан повторно — ничего не происходит
    await client.put("/api/owner", json={"user_id": SELF_ID, "chat_id": SELF_ID})
    assert manager.can_send(account_id) is True

    await client.put("/api/owner", json={"user_id": HELPER_ID, "chat_id": HELPER_ID})
    assert manager.can_send(account_id) is False             # сразу, не дожидаясь отключения
    await wait_for(lambda: not session.connected)
    account = (await client.get("/api/tg/accounts")).json()["accounts"][0]
    assert (account["status"], account["paused"], account["can_send"]) == ("paused", True, False)
    assert "помощник" in account["error"]
    jobs = (await client.post("/api/jobs/claim", json={"kinds": ["notify.owner"]})).json()["jobs"]
    assert len(jobs) == 1 and "основным аккаунтом" in jobs[0]["payload"]["text"]

    # снять паузу нельзя: запуск с диска сверяет аккаунт с владельцем и отвергает сессию
    world.authorized = True
    assert (await setup_request(client, "POST", f"/api/tg/accounts/{account_id}/resume")).status_code == 200
    rt = manager.runtimes["assistant"]
    await wait_for(lambda: rt.task.done())
    assert rt.status == "failed" and "основной аккаунт" in rt.error.lower()
    assert manager.can_send(account_id) is False
    from shturman.tg import gateway
    import pytest
    with pytest.raises((gateway.AccountUnavailable, gateway.SendForbidden)):
        await manager.send_text(account_id, "user", IVAN, "не должно уйти")
    assert not any(type(r).__name__ == "SendMessageRequest" for c in world.clients for r in c.requests)
    # остаётся выйти
    assert (await client.post(f"/api/tg/accounts/{account_id}/logout")).json()["ok"] is True


async def test_master_switch_off_means_nobody_sends(make_client, config):
    import pytest
    from telethon.tl import functions, types
    from shturman.tg import gateway
    from shturman.tg.client import RequestNotAllowed

    world = World(HELPER)
    world.authorized = False
    client, state, manager = await service(make_client, config, world, sending=False)
    _, status = await login(client, world, "assistant")
    account_id = status["account_id"]
    await wait_for(lambda: manager.runtimes["assistant"].status == "running")
    account = (await client.get("/api/tg/accounts")).json()["accounts"][0]
    assert (account["role"], account["status"], account["can_send"]) == ("assistant", "running", False)
    assert manager.can_send(account_id) is False
    before = list(world.last.requests)
    with pytest.raises(gateway.SendForbidden, match="SHTURMAN_SENDING"):
        await manager.send_text(account_id, "user", IVAN, "привет")
    await manager.set_typing(account_id, "user", IVAN, True)
    assert world.last.requests == before                      # к клиенту не обращались вовсе
    # и сам клиент отправку не пропустит, если до него кто-то доберётся
    with pytest.raises(RequestNotAllowed):
        await world.last(functions.messages.SendMessageRequest(types.InputPeerUser(IVAN, 1), "в обход"))
    with pytest.raises(RequestNotAllowed):
        await world.last(functions.messages.SetTypingRequest(types.InputPeerUser(IVAN, 1),
                                                             types.SendMessageTypingAction()))


async def test_backfill_depth_per_chat_and_account_default(make_client, config):
    world = World(ME)
    world.authorized = False
    world.dialogs = [U_IVAN, G_FAMILY, C_NEWS]
    now = datetime.now(timezone.utc)
    # канал: по сообщению в день за 500 дней, №500 — сегодняшнее
    world.add(*[msg(i, ("channel", 4001), f"пост {i}", post=True, at=now - timedelta(days=500 - i))
                for i in range(1, 501)])
    world.add(*[msg(i, ("user", IVAN), f"личное {i}", at=now - timedelta(days=800 - i)) for i in range(1, 11)])
    world.add(*[msg(100 + i, ("chat", GROUP), f"семья {i}", sender=2002, at=now - timedelta(days=900 - i))
                for i in range(1, 6)])
    client, state, manager = await service(make_client, config, world)
    _, status = await login(client, world, "owner", confirm_owner=True)
    base = f"/api/tg/accounts/{status['account_id']}"
    await wait_for(lambda: manager.runtimes["owner"].status == "running")
    rt = manager.runtimes["owner"]

    async def settled():
        await wait_for(lambda: rt.history.idle and not rt.wake.is_set())
        chats = (await client.get(f"{base}/sync")).json()["chats"]
        return {c["tg_id"]: c for c in chats}

    channel = [{"peer_class": "channel", "tg_id": 4001}]
    # 1) глубина не названа — по настройке аккаунта: 12 месяцев
    await setup_request(client, "POST", f"{base}/sync", json={"enabled": True, "chats": channel})
    chats = await settled()
    assert 360 <= chats[4001]["messages"] <= 370 and chats[4001]["backfill_done"] is True
    oldest = chats[4001]["backfill_before"]
    assert oldest == 500 - chats[4001]["messages"] + 1        # курсор — на самом старом из взятого
    history = [r for r in world.last.requests if type(r).__name__ == "GetHistoryRequest"]
    assert len(history) == 4                                  # 400 сообщений просмотрено, не все 500

    # 2) явная дата ближе прежней — уже загруженное остаётся, новых запросов нет
    recent = (now - timedelta(days=30)).date().isoformat()
    await setup_request(client, "POST", f"{base}/sync", json={"enabled": True, "chats": channel, "since": recent})
    chats = await settled()
    assert chats[4001]["backfill_since"].startswith(recent) and chats[4001]["backfill_before"] == oldest
    assert len([r for r in world.last.requests if type(r).__name__ == "GetHistoryRequest"]) == 4

    # 3) null — вся история: загрузка продолжается ровно с курсора
    await setup_request(client, "POST", f"{base}/sync", json={"enabled": True, "chats": channel, "since": None})
    chats = await settled()
    assert chats[4001]["backfill_since"] is None and chats[4001]["messages"] == 500
    assert chats[4001]["backfill_before"] == 1 and chats[4001]["backfill_done"] is True
    deeper = [r for r in world.last.requests if type(r).__name__ == "GetHistoryRequest"][4:]
    assert deeper[0].offset_id == oldest and len(deeper) == 2

    # 4) чат целиком старше границы: ничего не взято, но чат включён и новые сообщения идут
    await setup_request(client, "POST", f"{base}/sync", json={"enabled": True, "chats": [{"peer_class": "user", "tg_id": IVAN}]})
    chats = await settled()
    assert (chats[IVAN]["messages"], chats[IVAN]["backfill_done"], chats[IVAN]["enabled"]) == (0, True, True)

    # 5) настройка аккаунта: «вся история» для чатов, включаемых после этого
    assert (await setup_request(client, "PUT", f"{base}/options", json={"backfill_months": 0})).status_code == 400
    assert (await setup_request(client, "PUT", f"{base}/options", json={"backfill_months": "год"})).status_code == 400
    assert (await setup_request(client, "PUT", f"{base}/options", json={"backfill_months": None})).status_code == 200
    assert (await client.get("/api/tg/accounts")).json()["accounts"][0]["backfill_months"] is None
    await setup_request(client, "POST", f"{base}/sync", json={"enabled": True, "types": ["private_group"]})
    chats = await settled()
    assert chats[GROUP]["backfill_since"] is None and chats[GROUP]["messages"] == 5
    assert chats[IVAN]["messages"] == 0                       # уже включённых настройка не касается
    assert (await setup_request(client, "PUT", f"{base}/options", json={"backfill_months": 6})).status_code == 200
    assert (await setup_request(client, "PUT", f"{base}/options", json={"auto_groups": False})).status_code == 200
    assert (await client.get("/api/tg/accounts")).json()["accounts"][0]["backfill_months"] == 6

    for bad in ("вчера", 5, "2090-01-01", "1999-01-01"):
        response = await setup_request(client, "POST", f"{base}/sync", json={"enabled": True, "chats": channel, "since": bad})
        assert response.status_code == 400


async def test_agent_token_cannot_start_owner_login_without_independent_authority(make_client, config):
    world = World(ME)
    world.authorized = False
    client, state, manager = await service(make_client, config, world)
    refused = await client.post("/api/tg/login", json={"role": "owner", "confirm_owner": True})
    assert (refused.status_code, refused.json()["code"]) == (409, "owner_unknown")
    assert world.clients == [] and manager.flows == {}
    assert not session_path(manager.config, "owner").exists()
