"""Исключения чатов и импорт выгрузки: что ждёт нажатия владельца в своём боте согласований.

Исключить чат — ужесточение: применяется сразу в любом режиме. Вернуть чат, стереть его сообщения
и импортировать выгрузку — после решения владельца в независимом канале. Без своего
бота внутренний API не получает права расширять доступ; владелец может сделать это
на аутентифицированной странице настройки.
"""

import asyncio
import json

import pytest

from shturman import authority, bridge, confirm, events
from shturman.importer import import_export

from conftest import IVAN, OWNER, as_file

MODULES = ("shturman.api_core", "shturman.ingest_api")


async def setup(make_client, conn, sample_export):
    client, state = await make_client(*MODULES)
    await bridge.set_owner(conn, OWNER, OWNER)
    await import_export(conn, as_file(sample_export))
    chat_id = await conn.fetchval(
        "SELECT c.id FROM chats c JOIN peers p ON p.id = c.peer_id WHERE p.tg_id = $1", IVAN)
    seen = []

    async def on_excluded(payload):
        seen.append(payload)

    state.events.subscribe(events.CHAT_EXCLUDED, on_excluded)
    return client, state, chat_id, seen


async def excluded(conn, chat_id):
    return await conn.fetchval("SELECT excluded FROM chats WHERE id = $1", chat_id)


async def messages(conn, chat_id):
    return await conn.fetchval("SELECT count(*) FROM messages WHERE chat_id = $1", chat_id)


# --- исключение и возврат чата ---

async def test_excluding_a_chat_is_immediate_in_both_modes(make_client, conn, sample_export, either_mode, approvals):
    client, state, chat_id, seen = await setup(make_client, conn, sample_export)
    r = await client.put(f"/api/chats/{chat_id}/excluded", json={"excluded": True})
    assert r.status_code == 200 and r.json() == {"id": chat_id, "excluded": True, "purged": 0}
    assert await excluded(conn, chat_id) is True and await approvals.pending() == 0
    await state.events.drain()
    assert seen == [{"chat_id": chat_id, "purged": False}]      # остальные модули узнают сразу


async def test_returning_a_chat_waits_for_the_owner(make_client, conn, sample_export, own_bot, approvals):
    client, state, chat_id, _ = await setup(make_client, conn, sample_export)
    await conn.execute("UPDATE chats SET excluded = true WHERE id = $1", chat_id)

    async def send():
        return await client.put(f"/api/chats/{chat_id}/excluded", json={"excluded": False})

    await approvals.gate(send, lambda: excluded(conn, chat_id), True, False)
    summary = await conn.fetchval("SELECT summary FROM pending_actions ORDER BY id DESC LIMIT 1")
    assert "Вернуть чат «Иван Петров» в архив" in summary and "смету" not in summary


async def test_without_own_bot_api_cannot_return_a_chat(make_client, conn, sample_export):
    client, _, chat_id, _ = await setup(make_client, conn, sample_export)
    await conn.execute("UPDATE chats SET excluded = true WHERE id = $1", chat_id)
    r = await client.put(f"/api/chats/{chat_id}/excluded", json={"excluded": False})
    assert (r.status_code, r.json()["code"]) == (409, "owner_unknown")
    assert await excluded(conn, chat_id) is True


async def test_authenticated_setup_owner_can_return_a_chat_without_bot(make_client, conn, sample_export):
    _, _, chat_id, _ = await setup(make_client, conn, sample_export)
    await conn.execute("UPDATE chats SET excluded = true WHERE id = $1", chat_id)
    with pytest.raises(confirm.Refused, match="владелец"):
        await confirm.apply_owner(conn, "archive.chat_include", {"chat_id": chat_id})
    assert await excluded(conn, chat_id) is True
    # The setup handler establishes this context after verifying its own session.
    with authority.setup_context("authenticated-test-session", action="archive.chat_include"):
        out = await confirm.apply_owner(conn, "archive.chat_include", {"chat_id": chat_id})
    assert out["status"] == "applied" and await excluded(conn, chat_id) is False


async def test_returning_a_chat_that_is_not_excluded_asks_nothing(make_client, conn, sample_export, own_bot, approvals):
    client, _, chat_id, _ = await setup(make_client, conn, sample_export)
    r = await client.put(f"/api/chats/{chat_id}/excluded", json={"excluded": False})
    assert r.status_code == 200 and await approvals.pending() == 0


async def test_service_chat_is_refused_without_a_card(make_client, conn, sample_export, own_bot, approvals):
    client, _, _, _ = await setup(make_client, conn, sample_export)
    from shturman import store
    from shturman.records import ChatRecord

    account = await conn.fetchval("SELECT id FROM accounts WHERE tg_user_id = $1", OWNER)
    locked, _ = await store.ensure_chat(conn, account, ChatRecord("user", 777000, "personal_chat", "Telegram"))
    r = await client.put(f"/api/chats/{locked}/excluded", json={"excluded": False})
    assert (r.status_code, r.json()["code"]) == (409, "locked") and await approvals.pending() == 0


# --- стирание сообщений исключённого чата ---

async def test_purge_waits_for_the_owner_while_the_chat_is_closed_at_once(make_client, conn, sample_export,
                                                                         own_bot, approvals):
    client, state, chat_id, seen = await setup(make_client, conn, sample_export)

    async def send():
        return await client.put(f"/api/chats/{chat_id}/excluded", json={"excluded": True, "purge": True})

    first = await send()
    approvals.waiting(first)
    assert first.json()["applied_now"] == {"id": chat_id, "excluded": True}
    assert await excluded(conn, chat_id) is True          # кран закрыт сразу
    await state.events.drain()
    assert seen == [{"chat_id": chat_id, "purged": False}]
    summary = first.json()["summary"]
    assert "«Иван Петров»" in summary and "сообщений: 6" in summary and "Вернуть стёртое нельзя" in summary
    assert "смету" not in summary                          # текста сообщений в карточке нет

    await approvals.gate(send, lambda: messages(conn, chat_id), 6, 0)
    await state.events.drain()
    assert seen[-1] == {"chat_id": chat_id, "purged": True}
    assert await conn.fetchval("SELECT count(*) FROM messages") == 2      # остальные чаты не тронуты


async def test_without_own_bot_purge_is_refused_but_exclusion_applies(make_client, conn, sample_export):
    client, _, chat_id, _ = await setup(make_client, conn, sample_export)
    r = await client.put(f"/api/chats/{chat_id}/excluded", json={"excluded": True, "purge": True})
    assert (r.status_code, r.json()["code"]) == (409, "owner_unknown")
    assert await excluded(conn, chat_id) is True and await messages(conn, chat_id) == 6


async def test_authenticated_setup_owner_can_purge_excluded_messages(make_client, conn, sample_export):
    _, _, chat_id, _ = await setup(make_client, conn, sample_export)
    await conn.execute("UPDATE chats SET excluded = true WHERE id = $1", chat_id)
    with authority.setup_context("authenticated-test-session", action="archive.chat_purge"):
        out = await confirm.apply_owner(conn, "archive.chat_purge", {"chat_id": chat_id})
    assert out["status"] == "applied" and out["result"] == 6
    assert await messages(conn, chat_id) == 0


async def test_purging_an_empty_chat_asks_nothing(make_client, conn, sample_export, either_mode, approvals):
    client, _, _, _ = await setup(make_client, conn, sample_export)
    empty = await conn.fetchval("SELECT c.id FROM chats c JOIN peers p ON p.id = c.peer_id WHERE p.tg_id = 2999")
    r = await client.put(f"/api/chats/{empty}/excluded", json={"excluded": True, "purge": True})
    assert r.status_code == 200 and r.json() == {"id": empty, "excluded": True, "purged": 0}
    assert await approvals.pending() == 0


async def test_approved_purge_is_refused_if_the_chat_came_back_meanwhile(make_client, conn, sample_export,
                                                                        own_bot, approvals):
    client, _, chat_id, _ = await setup(make_client, conn, sample_export)
    action = approvals.waiting(
        await client.put(f"/api/chats/{chat_id}/excluded", json={"excluded": True, "purge": True}))
    await conn.execute("UPDATE chats SET excluded = false WHERE id = $1", chat_id)
    out = await approvals.press(action)
    assert out["answer"] == "Не получилось." and "больше не исключён" in out["edit_text"]
    assert await messages(conn, chat_id) == 6 and await approvals.status(action) == "failed"


async def test_tightening_path_cannot_return_a_chat_or_erase_messages(make_client, conn, sample_export, own_bot):
    """Действие, применяемое без нажатия владельца, само проверяет под блокировкой, что ничего не
    расширяет и не стирает: состояние могло измениться после того, как маршрут его прочитал."""
    client, _, chat_id, _ = await setup(make_client, conn, sample_export)
    await conn.execute("UPDATE chats SET excluded = true WHERE id = $1", chat_id)
    for kind in ("archive.chat_include", "archive.chat_purge"):
        with pytest.raises(confirm.Refused) as refused:
            await confirm.apply(conn, kind, {"chat_id": chat_id})
        assert refused.value.code == "changed_meanwhile"
    with pytest.raises(confirm.Refused) as refused:
        await confirm.apply(conn, "archive.import_run", {"import_id": "a" * 32, "exclude": [], "owner_id": None})
    assert refused.value.code == "changed_meanwhile"
    assert await excluded(conn, chat_id) is True and await messages(conn, chat_id) == 6


# --- импорт выгрузки ---

async def upload(client, obj) -> str:
    r = await client.post("/api/imports", content=json.dumps(obj, ensure_ascii=False).encode("utf-8"))
    assert r.status_code == 201, r.text
    return r.json()["import_id"]


async def wait_state(client, import_id, *states, timeout=10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        body = (await client.get(f"/api/imports/{import_id}")).json()
        if body["state"] in states:
            return body
        assert asyncio.get_running_loop().time() < deadline, body
        await asyncio.sleep(0.02)


async def test_import_waits_for_the_owner(make_client, conn, sample_export, own_bot, approvals):
    client, _ = await make_client(*MODULES)
    await bridge.set_owner(conn, OWNER, OWNER)
    import_id = await upload(client, sample_export)
    scan = (await client.get(f"/api/imports/{import_id}/scan")).json()

    async def send():
        return await client.post(f"/api/imports/{import_id}/run", json={"exclude": ["chat:3001"]})

    async def stored():
        state = (await client.get(f"/api/imports/{import_id}")).json()["state"]
        if state == "running":
            state = (await wait_state(client, import_id, "done", "failed"))["state"]
        return state, await conn.fetchval("SELECT count(*) FROM messages")

    first = await send()
    approvals.waiting(first)
    summary = first.json()["summary"]
    assert f"чатов: 4, сообщений: {scan['total_messages']}" in summary
    assert "Евгений Тестов" in summary and "Не принимать чатов: 1" in summary
    assert "Иван Петров" not in summary and "смету" not in summary     # ни названий чатов, ни текста
    await approvals.gate(send, stored, ("uploaded", 0), ("done", 7))


async def test_without_own_bot_api_cannot_start_import(make_client, conn, sample_export):
    client, _ = await make_client(*MODULES)
    import_id = await upload(client, sample_export)
    r = await client.post(f"/api/imports/{import_id}/run", json={})
    assert (r.status_code, r.json()["code"]) == (409, "owner_unknown")
    assert (await client.get(f"/api/imports/{import_id}")).json()["state"] == "uploaded"
    assert await conn.fetchval("SELECT count(*) FROM messages") == 0


async def test_authenticated_setup_owner_can_start_import_without_bot(make_client, conn, sample_export):
    client, _ = await make_client(*MODULES)
    import_id = await upload(client, sample_export)
    payload = {"import_id": import_id, "exclude": [], "owner_id": None}
    with pytest.raises(confirm.Refused, match="владелец"):
        await confirm.apply_owner(conn, "archive.import_run", payload)
    with authority.setup_context("authenticated-test-session", action="archive.import_run"):
        out = await confirm.apply_owner(conn, "archive.import_run", payload)
    assert out["status"] == "applied" and out["result"]["state"] == "running"
    assert (await wait_state(client, import_id, "done", "failed"))["state"] == "done"
    assert await conn.fetchval("SELECT count(*) FROM messages") == 8


async def test_approved_import_is_refused_if_the_upload_is_gone(make_client, conn, sample_export, own_bot, approvals):
    client, _ = await make_client(*MODULES)
    await bridge.set_owner(conn, OWNER, OWNER)
    import_id = await upload(client, sample_export)
    action = approvals.waiting(await client.post(f"/api/imports/{import_id}/run", json={}))
    # удалить загрузку можно сразу, без карточки: это только убирает файл с сервера
    assert (await client.delete(f"/api/imports/{import_id}")).json()["deleted"] is True
    out = await approvals.press(action)
    assert out["answer"] == "Не получилось." and "загрузка не найдена" in out["edit_text"]
    assert await conn.fetchval("SELECT count(*) FROM messages") == 0


async def test_deleting_an_upload_is_immediate_in_both_modes(make_client, conn, sample_export, either_mode, approvals):
    client, _ = await make_client(*MODULES)
    await bridge.set_owner(conn, OWNER, OWNER)
    import_id = await upload(client, sample_export)
    assert (await client.delete(f"/api/imports/{import_id}")).json() == {"deleted": True, "was_running": False}
    assert (await client.get(f"/api/imports/{import_id}")).status_code == 404 and await approvals.pending() == 0


async def test_bad_import_request_is_refused_before_any_card(make_client, conn, sample_export, own_bot, approvals):
    client, _ = await make_client(*MODULES)
    await bridge.set_owner(conn, OWNER, OWNER)
    import_id = await upload(client, sample_export)
    assert (await client.post(f"/api/imports/{import_id}/run", json={"exclude": ["кто-то"]})).status_code == 400
    assert (await client.post(f"/api/imports/{'a' * 32}/run", json={})).status_code == 404
    assert await approvals.pending() == 0

