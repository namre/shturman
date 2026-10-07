"""Привязка владельца через бота согласований: одноразовая ссылка, молчание для посторонних."""

import logging

from shturman import bridge
from shturman.executor import binding, commands

from exec_fakes import (  # noqa: F401 — rig — фикстура
    BOT_ID, BOT_NAME, IVAN_USER, OWNER, OWNER_USER, STRANGER, STRANGER_USER, bind, rig,
)


async def owner_id(rig):
    owner = await binding.bound_owner(rig.conn, BOT_ID)
    return owner["user_id"] if owner else None


async def start(rig, code, user=OWNER_USER, chat=None):
    rig.tg.text(f"/start {code}", user=user, chat=chat)
    await rig.bot.poll_once()


async def test_person_who_opens_the_link_becomes_the_owner(rig, caplog):
    caplog.set_level(logging.DEBUG)
    code, expires_at = await binding.create_code(rig.conn)
    assert len(code) >= 32 and binding.deep_link(BOT_NAME, code) == f"https://t.me/{BOT_NAME}?start={code}"
    stored = await rig.conn.fetchrow("SELECT code_hash, expires_at - created_at AS ttl FROM executor_bind_codes")
    assert stored["code_hash"] != code and len(stored["code_hash"]) == 64      # в базе только хеш
    assert 14 * 60 < stored["ttl"].total_seconds() <= 15 * 60

    await start(rig, code)
    assert await bridge.get_owner(rig.conn) == {"user_id": OWNER, "chat_id": OWNER}
    assert await owner_id(rig) == OWNER
    said = rig.tg.sent()
    assert len(said) == 1 and said[0]["chat_id"] == OWNER and said[0]["text"].startswith("Готово: вы привязаны")
    assert code not in caplog.text and str(OWNER) not in caplog.text          # ни кода, ни идентификатора


async def test_code_works_once_and_a_new_code_cancels_the_old_one(rig):
    code = await bind(rig)
    await start(rig, code, user=STRANGER_USER)                # тот же код второй раз
    assert await owner_id(rig) == OWNER

    old, _ = await binding.create_code(rig.conn)
    new, _ = await binding.create_code(rig.conn)
    assert await rig.conn.fetchval("SELECT count(*) FROM executor_bind_codes") == 1
    await start(rig, old, user=STRANGER_USER)
    assert await owner_id(rig) == OWNER
    await start(rig, new, user=STRANGER_USER)
    assert await owner_id(rig) == STRANGER


async def test_expired_wrong_and_malformed_codes_get_no_reply_at_all(rig):
    code, _ = await binding.create_code(rig.conn)
    await rig.conn.execute("UPDATE executor_bind_codes SET expires_at = now() - interval '1 second'")
    await start(rig, code)
    await start(rig, binding.new_code())
    await start(rig, "короткий")
    await start(rig, "x' OR 1=1 --")
    rig.tg.text("/start")                       # без кода, владельца ещё нет
    rig.tg.text("Привет, ты кто?", user=STRANGER_USER)
    await rig.bot.poll_once()
    assert await bridge.get_owner(rig.conn) is None
    assert rig.tg.calls("sendMessage") == []    # посторонний не узнаёт даже, что бот существует
    assert rig.bot.counters["bind_wrong"] == 4


async def test_flood_of_wrong_codes_pauses_binding_then_it_resumes(rig):
    for _ in range(5):
        await start(rig, binding.new_code(), user=STRANGER_USER)
    assert rig.bot.flood.locked()
    good, _ = await binding.create_code(rig.conn)
    await start(rig, good)                      # даже верный код сейчас не принимается…
    assert await bridge.get_owner(rig.conn) is None and rig.tg.calls("sendMessage") == []
    assert await rig.conn.fetchval("SELECT used_at IS NULL FROM executor_bind_codes") is True   # …и не сгорает
    status = (await rig.client.get("/api/status")).json()
    assert status["owner_known"] is False

    rig.clock.tick(binding.LOCKOUT + 1)
    await start(rig, good)
    assert await owner_id(rig) == OWNER


async def test_link_opened_in_a_group_binds_nobody_and_burns_the_code(rig):
    code, _ = await binding.create_code(rig.conn)
    group = {"id": -100500, "type": "supergroup", "title": "Рабочий чат"}
    await start(rig, code, chat=group)
    assert await bridge.get_owner(rig.conn) is None and rig.tg.calls("sendMessage") == []
    await start(rig, code)                      # код видели посторонние — он больше не действует
    assert await bridge.get_owner(rig.conn) is None


async def test_rebinding_replaces_the_owner_and_tells_other_modules(rig):
    seen = []

    @bridge.on_owner_change
    async def changed(conn, new_user_id):
        seen.append(new_user_id)

    try:
        await bind(rig)
        assert seen == []                       # первая привязка сменой не считается
        await bind(rig)                         # тот же владелец ещё раз — ничего не меняется
        assert seen == []
        await bind(rig, IVAN_USER)
        assert seen == [IVAN_USER["id"]]
        assert (await bridge.get_owner(rig.conn))["user_id"] == IVAN_USER["id"]
    finally:
        bridge._owner_change_handlers.remove(changed)


async def test_owner_set_before_the_bot_existed_is_not_trusted_until_he_binds(rig):
    """Запись о владельце, оставшаяся со времён плагина, могла быть подменена ассистентом."""
    await bridge.set_owner(rig.conn, STRANGER, STRANGER)
    assert await owner_id(rig) is None
    await bridge.notify_owner(rig.conn, "Карточка")
    await rig.worker.run_once("bot")
    assert rig.tg.calls("sendMessage") == []
    job = await rig.conn.fetchrow("SELECT status, error FROM jobs")
    assert job["status"] == "queued" and "не привязан" in job["error"]


async def test_owner_free_text_gets_one_short_hint_per_hour(rig):
    await bind(rig)
    before = len(rig.tg.sent())
    for text in ("привет", "что ты умеешь?", "/start", "/help"):
        rig.tg.text(text)
    rig.tg.text("а мне ответишь?", user=STRANGER_USER)
    await rig.bot.poll_once()
    hints = rig.tg.sent()[before:]
    assert len(hints) == 1 and hints[0]["text"].startswith("Это бот согласований Штурмана")
    assert hints[0]["chat_id"] == OWNER

    rig.clock.tick(3601)
    rig.tg.text("ещё вопрос")
    await rig.bot.poll_once()
    assert len(rig.tg.sent()) == before + 2


async def test_bot_bind_command_prints_a_working_link(rig, monkeypatch, capsys):
    from conftest import DSN

    monkeypatch.setenv("SHTURMAN_BOT_TOKEN", "задан")
    await commands.bot_bind(DSN)
    out = capsys.readouterr().out
    link = next(line.strip() for line in out.splitlines() if "https://t.me/" in line)
    assert link.startswith(f"https://t.me/{BOT_NAME}?start=") and "15 минут" in out
    await start(rig, link.split("start=")[1])
    assert await owner_id(rig) == OWNER

    await commands.bot_bind(DSN)                # повторная команда предупреждает о смене владельца
    assert "Владелец уже привязан" in capsys.readouterr().out
