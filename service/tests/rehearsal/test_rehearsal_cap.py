"""Дневной предел репетиции из бота: кнопки «Поднять до …» под «предел исчерпан» и команды
`/rehearsal cap N`, `/rehearsal pause S` (дополнение к Р-85 и Р-89).

Главное:
  * под «предел исчерпан» — только значения больше нынешнего: 50 и 100, от 100 — 200 и 300, на 300 — ничего;
  * нажатие владельца в личном чате бота согласований — его решение: сразу, журнал «бот, кнопка», ответ
    «Предел — N в сутки. Вернуть: /rehearsal cap M»; чужое нажатие, нажатие из приложения, устаревшая
    и подделанная кнопка предел не меняют;
  * у «Подготовить ответ» после подъёма просьба повторяется сама;
  * команды проверяют диапазон и отвечают словами, как правильно.
"""

import json

import pytest

from shturman import authority, bridge
from shturman.executor import owner_chat
from shturman.notify import buttons as nb

from rehearsal_helpers import (ANNA, OWNER, PETR, chat, core, enable, item, live, message,  # noqa: F401
                               model_jobs, nothing_sent, press, rs, rv)


def keys(payload: dict) -> dict[str, str]:
    return {b["text"]: b["data"] for row in payload.get("buttons") or [] for b in row}


async def cap_messages(conn) -> list[dict]:
    rows = await conn.fetch("SELECT payload FROM jobs WHERE kind = $1 AND payload->>'text' LIKE '%дневной предел%' "
                            "ORDER BY id", bridge.NOTIFY_OWNER)
    return [r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"]) for r in rows]


async def journal(conn) -> list[str]:
    return [r["detail"] for r in await conn.fetch(
        "SELECT detail FROM setup_audit WHERE action = 'rehearsal.settings' ORDER BY id")]


@pytest.mark.parametrize("cap,expected", [(1, [50, 100]), (30, [50, 100]), (50, [100]), (99, [100]),
                                          (100, [200, 300]), (150, [200, 300]), (250, [300]), (300, [])])
def test_raise_options_are_only_bigger_values(cap, expected):
    assert core.raise_options(cap) == expected
    labels = [b["text"] for row in core.cap_buttons(cap) for b in row]
    assert labels == [f"Поднять до {v}" for v in expected]
    for row in core.cap_buttons(cap, message_id=123456789012):
        for b in row:
            assert b["data"].startswith("sh:rh:c:") and len(b["data"].encode()) <= bridge.CALLBACK_DATA_LIMIT
    if not expected:
        assert "наибольший предел" in core.cap_text(cap) and "кнопкой" not in core.cap_text(cap)


async def _exhaust(rv, cap=1):
    """Предел cap исчерпан фоновой репетицией: первое сообщение готовится, второе — «предел исчерпан»."""
    await enable(rv.conn, people=(ANNA, PETR), daily_cap=cap)
    anna = await chat(rv.conn, rv.owner_acc, ANNA, name="Анна Ким")
    petr = await chat(rv.conn, rv.owner_acc, PETR, name="Пётр")
    _, first = await live(rv, rv.owner_acc, anna, await message(rv.conn, anna, 1, "Первый вопрос"))
    assert (await item(rv.conn, first))["state"] == "preparing"
    mid = await message(rv.conn, petr, 1, "Второй вопрос", sender=PETR, sender_name="Пётр")
    _, second = await live(rv, rv.owner_acc, petr, mid)
    assert (await item(rv.conn, second))["reason"] == "daily_cap"
    [told] = await cap_messages(rv.conn)
    return told


async def test_cap_message_has_raise_buttons_and_the_owners_press_applies_at_once(rv):
    told = await _exhaust(rv)
    assert "дневной предел — 1 карточка — исчерпан" in told["text"]
    assert "/rehearsal cap ЧИСЛО" in told["text"] and "вкладке «Репетиция» приложения" in told["text"]
    assert "Настройках Штурмана" not in told["text"]
    buttons = keys(told)
    assert list(buttons) == ["Поднять до 50", "Поднять до 100"]
    assert buttons["Поднять до 50"] == "sh:rh:c:50:1"

    # чужое нажатие и нажатие из приложения предел не меняют
    assert (await press(rv.conn, buttons["Поднять до 50"], user=OWNER + 1))["answer"] == "Кнопка недоступна."
    with authority.miniapp_context(OWNER, chat_id=OWNER):
        out = await bridge.dispatch_callback(rv.conn, buttons["Поднять до 50"], OWNER)
    assert "только нажатием в боте" in out["answer"]
    # подделанные данные: не из списка кнопок, не числа
    for forged in ("sh:rh:c:300:1", "sh:rh:c:5:1", "sh:rh:c:x:1", "sh:rh:c:50", "sh:rh:c:50:1:2:3"):
        assert (await press(rv.conn, forged))["answer"] == "Кнопка недоступна.", forged
    assert (await rs.load(rv.conn))["daily_cap"] == 1
    assert await journal(rv.conn) == []

    out = await press(rv.conn, buttons["Поднять до 50"])
    assert out["answer"] == "Предел — 50 в сутки." and out["remove_buttons"] is True
    assert out["reply"]["text"] == "Предел — 50 в сутки. Вернуть: /rehearsal cap 1"
    assert (await rs.load(rv.conn))["daily_cap"] == 50
    assert await rv.conn.fetchval("SELECT count(*) FROM pending_actions") == 0      # без карточки «Да»
    [detail] = await journal(rv.conn)
    assert "в сутки: 50" in detail and detail.endswith("через: бот, кнопка «Поднять до …»")
    # старая кнопка после смены предела не срабатывает
    again = await press(rv.conn, buttons["Поднять до 100"])
    assert again["answer"] == "Предел уже изменён: сейчас 50 в сутки." and again["remove_buttons"] is True
    assert (await rs.load(rv.conn))["daily_cap"] == 50
    await nothing_sent(rv)


async def test_at_100_and_above_the_buttons_are_200_and_300_and_at_300_none(rv):
    told = await _exhaust(rv, cap=1)
    await rs.save(rv.conn, {**await rs.load(rv.conn), "daily_cap": 100})
    assert [b["text"] for row in core.cap_buttons(100) for b in row] == ["Поднять до 200", "Поднять до 300"]
    out = await press(rv.conn, "sh:rh:c:300:100")
    assert out["reply"]["text"] == "Предел — 300 в сутки. Вернуть: /rehearsal cap 100"
    assert core.cap_buttons(300) == [] and told


async def test_prepare_reply_at_the_cap_offers_raise_and_repeats_the_request(rv):
    await enable(rv.conn, people=(), daily_cap=1, enabled=False)     # «Подготовить ответ» — и без списка
    anna = await chat(rv.conn, rv.owner_acc, ANNA, name="Анна Ким")
    petr = await chat(rv.conn, rv.owner_acc, PETR, name="Пётр")
    first = await message(rv.conn, anna, 1, "Когда смета?")
    assert (await core.request_once(rv.state, first))["ok"] is True
    second = await message(rv.conn, petr, 1, "Подпишешь акт?", sender=PETR, sender_name="Пётр")
    out = await nb.prepare_reply(rv.conn, second, personal=True)
    assert out["answer"] == "Не получилось."
    assert out["reply"]["text"].startswith("Дневной предел репетиции — 1 карточка — исчерпан.")
    assert "Настройках Штурмана" not in out["reply"]["text"]
    labels = dict(out["reply"]["buttons"][0])
    assert list(labels) == ["Поднять до 50", "Поднять до 100"]
    assert labels["Поднять до 50"] == f"sh:rh:c:50:1:{second}"
    assert len(await model_jobs(rv.conn)) == 1

    pressed = await press(rv.conn, labels["Поднять до 50"])
    assert pressed["reply"]["text"].startswith("Предел — 50 в сутки. Вернуть: /rehearsal cap 1\n")
    assert "Готовлю, что ответил бы ассистент" in pressed["reply"]["text"]
    jobs = await model_jobs(rv.conn)
    assert len(jobs) == 2 and jobs[-1]["context"]["item_id"] == await rv.conn.fetchval(
        "SELECT id FROM rehearsal_items WHERE trigger_message_id = $1", second)
    await nothing_sent(rv)


async def _command(conn, state, text):
    with authority.owner_context(OWNER, chat_id=OWNER, action="telegram.command"):
        return await owner_chat.handle_command(conn, state, text, OWNER)


async def test_cap_and_pause_commands_apply_at_once_and_say_how_to_undo(rv):
    shown = (await _command(rv.conn, rv.state, "/rehearsal"))["text"]
    assert "Предел — 30 карточек в сутки" in shown and "/rehearsal cap ЧИСЛО" in shown
    out = await _command(rv.conn, rv.state, "/rehearsal cap 50")
    assert out["text"] == "Предел — 50 в сутки. Вернуть: /rehearsal cap 30"
    out = await _command(rv.conn, rv.state, "/rehearsal pause 0")
    assert out["text"] == "Пауза между карточками — 0 с. Вернуть: /rehearsal pause 5"
    s = await rs.load(rv.conn)
    assert (s["daily_cap"], s["pause_seconds"]) == (50, 0)
    assert "так и было" in (await _command(rv.conn, rv.state, "/rehearsal cap 50"))["text"]
    details = await journal(rv.conn)
    assert len(details) == 2 and all(d.endswith("через: бот, команда") for d in details)
    assert await rv.conn.fetchval("SELECT count(*) FROM pending_actions") == 0
    # ниже — тоже сразу
    assert (await _command(rv.conn, rv.state, "/rehearsal cap 1"))["text"].endswith("/rehearsal cap 50")


@pytest.mark.parametrize("command,words", [
    ("/rehearsal cap 0", "от 1 до 300"), ("/rehearsal cap 301", "от 1 до 300"), ("/rehearsal cap abc", "от 1 до 300"),
    ("/rehearsal cap -5", "от 1 до 300"), ("/rehearsal cap 5.5", "от 1 до 300"), ("/rehearsal cap ５０", "от 1 до 300"),
    ("/rehearsal pause 3601", "от 0 до 3600"), ("/rehearsal pause x", "от 0 до 3600"),
    ("/rehearsal cap", "/rehearsal cap 50"), ("/rehearsal cap 5 6", "/rehearsal cap 50")])
async def test_cap_and_pause_commands_refuse_bad_values_in_words(rv, command, words):
    with pytest.raises(ValueError) as exc:
        await _command(rv.conn, rv.state, command)
    assert words in str(exc.value)
    s = await rs.load(rv.conn)
    assert (s["daily_cap"], s["pause_seconds"]) == (30, 5)


async def test_cap_command_needs_the_verified_owner(rv):
    with pytest.raises(PermissionError):
        await owner_chat.handle_command(rv.conn, rv.state, "/rehearsal cap 300", OWNER)
    assert (await rs.load(rv.conn))["daily_cap"] == 30


async def test_help_and_advanced_mention_the_cap(rv):
    assert "/rehearsal cap 50" in (await _command(rv.conn, rv.state, "/help"))["text"]
    advanced = (await _command(rv.conn, rv.state, "/advanced"))["text"]
    assert "/rehearsal cap ЧИСЛО" in advanced and "/rehearsal pause СЕКУНДЫ" in advanced
