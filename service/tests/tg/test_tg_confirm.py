"""Аккаунты Telegram: что ждёт нажатия владельца в своём боте согласований.

Вход в аккаунт, снятие с паузы, включение чатов и настройки «брать больше» расширяют то, что
сервис читает, — со своим ботом они применяются только после «да» владельца. Выход, пауза,
отмена входа, выключение чатов и более мелкая история применяются сразу в любом режиме.
"""

from shturman import authority, bridge, confirm
from shturman.tg.client import session_path
from shturman.tg.manager import TgManager

from tg_fakes import (C_NEWS, G_FAMILY, GROUP, HELPER, IVAN, MARIA, ME, SELF_ID, U_IVAN, U_MARIA, World, msg,
                      tg_config, wait_for)

MODULES = ("shturman.api_core", "shturman.tg.service")


async def service(make_client, config, conn, world):
    client, state = await make_client(*MODULES, cfg=tg_config(config))
    await bridge.set_owner(conn, SELF_ID, SELF_ID)       # владелец привязан: карточки есть кому показать
    manager = state.extras["tg"]
    assert isinstance(manager, TgManager)
    manager.client_factory = world.factory
    manager.pacing = 0.0
    return client, state, manager


async def login(client, world, approvals, role="assistant", **extra):
    """Fixture login: bot approval or explicitly authenticated setup authority."""
    body = {"role": role, **extra}
    started = await client.post("/api/tg/login", json=body)
    if started.status_code == 202:
        assert (await approvals.press(approvals.waiting(started)))["answer"] == "Сделано."
        started = await client.post("/api/tg/login", json=body)
    elif started.status_code == 409 and started.json().get("code") == "owner_unknown":
        with authority.setup_context("test-login", action="fixture.login"):
            await confirm.apply_owner(approvals.conn, "tg.login", body)
        started = await client.post("/api/tg/login", json=body)
    assert started.status_code == 200, started.text
    world.last.scan.set_result(world.me)
    status = {}
    for _ in range(300):
        status = (await client.get(f"/api/tg/login/{started.json()['login_id']}")).json()
        if status["status"] != "pending":
            break
        import asyncio
        await asyncio.sleep(0.01)
    assert status["status"] == "completed", status
    return status["account_id"]


async def running(manager, slot):
    await wait_for(lambda: slot in manager.runtimes and manager.runtimes[slot].status == "running")


# --- вход ---

async def test_login_starts_only_after_the_owner_allows_it(make_client, config, conn, own_bot, approvals):
    world = World(HELPER)
    world.authorized = False
    client, state, manager = await service(make_client, config, conn, world)

    asked = await client.post("/api/tg/login", json={"role": "assistant"})
    action = approvals.waiting(asked)
    assert "в роли помощника" in asked.json()["summary"] and "tg://" not in await approvals.card(action)
    assert world.clients == [] and not session_path(manager.config, "assistant").exists()   # входа ещё нет
    assert "qr_svg" not in asked.json() and "link" not in asked.json()

    # отказ: вход не начинается, а новый запрос — снова вопрос владельцу
    assert (await approvals.press(action, yes=False))["answer"] == "Отклонено."
    again = await client.post("/api/tg/login", json={"role": "assistant"})
    second = approvals.waiting(again)
    assert second != action and world.clients == []

    # срок вышел — тоже ничего
    await approvals.lapse(second)
    assert (await approvals.press(second))["answer"] == "Срок вышел."
    third = approvals.waiting(await client.post("/api/tg/login", json={"role": "assistant"}))
    assert world.clients == []

    # «да» само вход не начинает: оно разрешает начать его тем же запросом
    done = await approvals.press(third)
    assert done["answer"] == "Сделано." and "минут" in done["edit_text"] and world.clients == []
    started = await client.post("/api/tg/login", json={"role": "assistant"})
    assert started.status_code == 200 and started.json()["status"] == "pending"
    assert started.json()["link"].startswith("tg://login?token=")
    # разрешение одноразовое: следующий запрос — снова к владельцу
    approvals.waiting(await client.post("/api/tg/login", json={"role": "assistant"}))
    # отменить начатый вход можно сразу
    cancelled = await client.post(f"/api/tg/login/{started.json()['login_id']}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"


async def test_permission_is_for_one_role_only(make_client, config, conn, own_bot, approvals):
    world = World(ME)
    world.authorized = False
    client, state, manager = await service(make_client, config, conn, world)
    action = approvals.waiting(await client.post("/api/tg/login", json={"role": "assistant"}))
    await approvals.press(action)
    owner = await client.post("/api/tg/login", json={"role": "owner", "confirm_owner": True})
    approvals.waiting(owner)
    assert "основной аккаунт" in owner.json()["summary"] and world.clients == []


async def test_bad_login_request_is_refused_before_any_card(make_client, config, conn, own_bot, approvals):
    world = World(HELPER)
    world.authorized = False
    client, state, manager = await service(make_client, config, conn, world)
    assert (await client.post("/api/tg/login", json={"role": "admin"})).status_code == 400
    assert (await client.post("/api/tg/login", json={"role": "owner"})).status_code == 400   # без confirm_owner
    assert await approvals.pending() == 0


async def test_without_own_bot_agent_cannot_start_login(make_client, config, conn):
    world = World(HELPER)
    world.authorized = False
    client, state, manager = await service(make_client, config, conn, world)
    started = await client.post("/api/tg/login", json={"role": "assistant"})
    assert started.status_code == 409 and started.json()["code"] == "owner_unknown"
    assert world.clients == []


# --- пауза, снятие с паузы, выход ---

async def paused(conn, account_id):
    return await conn.fetchval("SELECT paused FROM tg_sessions WHERE account_id = $1", account_id)


async def test_resume_waits_for_the_owner_and_pause_does_not(make_client, config, conn, own_bot, approvals):
    world = World(HELPER)
    world.authorized = False
    client, state, manager = await service(make_client, config, conn, world)
    account_id = await login(client, world, approvals)
    await running(manager, "assistant")
    base = f"/api/tg/accounts/{account_id}"

    assert (await client.post(f"{base}/pause")).json() == {"ok": True}      # пауза — сразу
    assert await paused(conn, account_id) is True and "assistant" not in manager.runtimes
    world.authorized = True

    async def send():
        return await client.post(f"{base}/resume")

    await approvals.gate(send, lambda: paused(conn, account_id), True, False)
    await running(manager, "assistant")
    summary = await conn.fetchval("SELECT summary FROM pending_actions ORDER BY id DESC LIMIT 1")
    assert "Снять с паузы аккаунт «Помощник» (аккаунт-помощник)" in summary
    # аккаунт не на паузе: повторный запрос ничего не расширяет и карточки не создаёт
    assert (await client.post(f"{base}/resume")).json() == {"ok": True} and await approvals.pending() == 0


async def test_pause_and_logout_are_immediate_in_both_modes(make_client, config, conn, either_mode, approvals):
    world = World(HELPER)
    world.authorized = False
    client, state, manager = await service(make_client, config, conn, world)
    account_id = await login(client, world, approvals)
    await running(manager, "assistant")
    base = f"/api/tg/accounts/{account_id}"
    assert (await client.post(f"{base}/pause")).json() == {"ok": True}
    assert await paused(conn, account_id) is True
    out = await client.post(f"{base}/logout")
    assert out.status_code == 200 and out.json()["ok"] is True
    assert not session_path(manager.config, "assistant").exists() and await approvals.pending() == 0
    assert await conn.fetchval("SELECT count(*) FROM tg_sessions") == 0


async def test_without_own_bot_agent_cannot_resume(make_client, config, conn, approvals):
    world = World(HELPER)
    world.authorized = False
    client, state, manager = await service(make_client, config, conn, world)
    account_id = await login(client, world, approvals)
    await running(manager, "assistant")
    await client.post(f"/api/tg/accounts/{account_id}/pause")
    world.authorized = True
    refused = await client.post(f"/api/tg/accounts/{account_id}/resume")
    assert refused.status_code == 409 and refused.json()["code"] == "owner_unknown"
    assert await paused(conn, account_id) is True


async def test_reconnect_without_the_owner_never_lifts_a_pause(make_client, config, conn, own_bot, approvals):
    """Маршрут счёл аккаунт «не на паузе» и применяет переподключение без владельца, а паузу
    поставили в тот же миг: пауза остаётся, аккаунт не подключается."""
    import pytest

    from shturman import confirm

    world = World(HELPER)
    world.authorized = False
    client, state, manager = await service(make_client, config, conn, world)
    account_id = await login(client, world, approvals)
    await running(manager, "assistant")
    await manager.pause(account_id)
    world.authorized = True
    with pytest.raises(confirm.Refused) as refused:
        await confirm.apply(conn, "tg.resume", {"account_id": account_id})
    assert "на паузе" in refused.value.message
    assert await paused(conn, account_id) is True and "assistant" not in manager.runtimes


# --- выбор чатов ---

async def test_tightening_path_cannot_open_more_of_the_account(make_client, config, conn, own_bot, approvals):
    import pytest

    from shturman import confirm

    client, manager, account_id, base = await owner_account(make_client, config, conn, approvals)
    await conn.execute("UPDATE tg_sessions SET backfill_months = 6 WHERE account_id = $1", account_id)
    widening = [
        ("tg.login", {"role": "assistant"}),
        ("tg.options", {"account_id": account_id, "auto_personal": True}),
        ("tg.options", {"account_id": account_id, "auto_groups": True, "backfill_months": 3}),
        ("tg.options", {"account_id": account_id, "backfill_months": 12}),      # глубже, чем стало
        ("tg.options", {"account_id": account_id, "backfill_months": None}),
        ("tg.sync", {"account_id": account_id, "enabled": True, "chats": [["user", IVAN]], "types": [],
                     "since": "account_default"}),
    ]
    for kind, payload in widening:
        with pytest.raises(confirm.Refused) as refused:
            await confirm.apply(conn, kind, payload)
        assert refused.value.code == "changed_meanwhile", (kind, payload)
    assert await option(conn, account_id, "backfill_months") == 6
    assert await option(conn, account_id, "auto_personal") is False and await enabled(conn, account_id) == []
    assert (await client.post("/api/tg/login", json={"role": "assistant"})).status_code == 202   # разрешения нет
    await confirm.apply(conn, "tg.options", {"account_id": account_id, "backfill_months": 3})   # мельче — можно
    assert await option(conn, account_id, "backfill_months") == 3


async def owner_account(make_client, config, conn, approvals):
    world = World(ME)
    world.authorized = False
    world.dialogs = [U_IVAN, U_MARIA, G_FAMILY, C_NEWS]
    world.add(*[msg(i, ("user", IVAN), f"личное {i}") for i in range(1, 6)])
    world.add(*[msg(i, ("chat", GROUP), f"семья {i}", sender=MARIA) for i in range(200, 203)])
    client, state, manager = await service(make_client, config, conn, world)
    account_id = await login(client, world, approvals, "owner", confirm_owner=True)
    await running(manager, "owner")
    return client, manager, account_id, f"/api/tg/accounts/{account_id}"


async def enabled(conn, account_id):
    return sorted(r["tg_id"] for r in await conn.fetch(
        "SELECT tg_id FROM tg_sync_chats WHERE account_id = $1 AND enabled", account_id))


async def test_enabling_chats_waits_for_the_owner(make_client, config, conn, own_bot, approvals):
    client, manager, account_id, base = await owner_account(make_client, config, conn, approvals)

    async def send():
        return await client.post(f"{base}/sync", json={
            "enabled": True, "chats": [{"peer_class": "user", "tg_id": IVAN}]})

    first = await send()
    approvals.waiting(first)
    summary = first.json()["summary"]
    assert "«Иван Петров»" in summary and "последние 12 мес." in summary and "личное" not in summary
    assert (await client.get("/api/status")).json()["chats"] == 0           # в архиве по-прежнему пусто
    await approvals.gate(send, lambda: enabled(conn, account_id), [], [IVAN])

    # уже включённый чат — не расширение: без карточки
    again = await send()
    assert again.status_code == 200 and again.json()["chats"][0]["enabled"] is True
    assert await approvals.pending() == 0
    # а тот же чат «со всей историей» — расширение
    approvals.waiting(await client.post(f"{base}/sync", json={
        "enabled": True, "chats": [{"peer_class": "user", "tg_id": IVAN}], "since": None}))


async def test_enabling_whole_kinds_of_chats_waits_too(make_client, config, conn, own_bot, approvals):
    client, manager, account_id, base = await owner_account(make_client, config, conn, approvals)

    async def send():
        return await client.post(f"{base}/sync", json={"enabled": True, "types": ["private_group"]})

    first = await send()
    approvals.waiting(first)
    assert "закрытые группы — сейчас таких 1" in first.json()["summary"]
    await approvals.gate(send, lambda: enabled(conn, account_id), [], [GROUP])


async def test_disabling_chats_is_immediate_in_both_modes(make_client, config, conn, either_mode, approvals):
    client, manager, account_id, base = await owner_account(make_client, config, conn, approvals)
    on = await client.post(f"{base}/sync", json={"enabled": True, "chats": [{"peer_class": "user", "tg_id": IVAN}]})
    if either_mode:
        await approvals.press(approvals.waiting(on))
    else:
        assert on.status_code == 409 and on.json()["code"] == "owner_unknown"
        with authority.setup_context("test-sync", action="fixture.enable_chat"):
            await confirm.apply_owner(conn, "tg.sync", {
                "account_id": account_id, "enabled": True, "chats": [["user", IVAN]],
                "types": [], "since": "account_default"})
    assert await enabled(conn, account_id) == [IVAN]
    off = await client.post(f"{base}/sync", json={"enabled": False, "chats": [{"peer_class": "user", "tg_id": IVAN}]})
    assert off.status_code == 200 and off.json()["chats"] == [{"peer_class": "user", "tg_id": IVAN, "enabled": False}]
    assert await enabled(conn, account_id) == [] and await approvals.pending() == 0


# --- настройки аккаунта ---

async def option(conn, account_id, key):
    return await conn.fetchval(f"SELECT {key} FROM tg_sessions WHERE account_id = $1", account_id)


async def test_taking_new_chats_automatically_waits_for_the_owner(make_client, config, conn, own_bot, approvals):
    client, manager, account_id, base = await owner_account(make_client, config, conn, approvals)

    async def send():
        return await client.put(f"{base}/options", json={"auto_personal": True})

    await approvals.gate(send, lambda: option(conn, account_id, "auto_personal"), False, True)
    off = await client.put(f"{base}/options", json={"auto_personal": False})        # выключить — сразу
    assert off.status_code == 200 and await option(conn, account_id, "auto_personal") is False


async def test_deeper_history_waits_and_shallower_does_not(make_client, config, conn, own_bot, approvals):
    client, manager, account_id, base = await owner_account(make_client, config, conn, approvals)
    less = await client.put(f"{base}/options", json={"backfill_months": 6})
    assert less.status_code == 200 and await option(conn, account_id, "backfill_months") == 6

    async def more():
        return await client.put(f"{base}/options", json={"backfill_months": 24})

    await approvals.gate(more, lambda: option(conn, account_id, "backfill_months"), 6, 24)

    async def everything():
        return await client.put(f"{base}/options", json={"backfill_months": None})

    await approvals.gate(everything, lambda: option(conn, account_id, "backfill_months"), 24, None)
    back = await client.put(f"{base}/options", json={"backfill_months": 12})        # от «всей истории» к году
    assert back.status_code == 200 and await option(conn, account_id, "backfill_months") == 12


async def test_mixed_options_tighten_now_and_ask_about_the_rest(make_client, config, conn, own_bot, approvals):
    client, manager, account_id, base = await owner_account(make_client, config, conn, approvals)
    answer = await client.put(f"{base}/options", json={"auto_groups": True, "backfill_months": 3})
    action = approvals.waiting(answer)
    assert answer.json()["applied_now"] == {"backfill_months": 3}
    assert await option(conn, account_id, "backfill_months") == 3
    assert await option(conn, account_id, "auto_groups") is False
    assert "новые группы и каналы" in answer.json()["summary"]
    await approvals.press(action)
    assert await option(conn, account_id, "auto_groups") is True


async def test_without_own_bot_agent_cannot_expand_options(make_client, config, conn, approvals):
    client, manager, account_id, base = await owner_account(make_client, config, conn, approvals)
    answer = await client.put(f"{base}/options", json={"auto_personal": True, "backfill_months": None})
    assert answer.status_code == 409 and answer.json()["code"] == "owner_unknown"
    assert await option(conn, account_id, "auto_personal") is False
    assert await option(conn, account_id, "backfill_months") == 12

