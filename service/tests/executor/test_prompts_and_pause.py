"""Бот согласований: просьба «напишите следующим сообщением» (ForceReply) и «Пауза» / «Продолжить».

Текст по просьбе принимается только от привязанного владельца в его личном чате и только пока просьба
ждёт; ответ на другое сообщение бота её не забирает. «Пауза» — сразу; «Продолжить» — только кнопкой
в боте, с подтверждением.
"""

import time

import pytest

from shturman import bridge, halt
from shturman.executor import owner_chat, prompts
from shturman.replies import owner as replies_owner
from exec_fakes import OWNER, OWNER_USER, STRANGER_USER, bind, buttons_of, private, rig  # noqa: F401 — фикстура

calls: list[dict] = []


@bridge.on_callback("tq")
async def _test_prompt(conn, rest, user_id):
    """Кнопка из теста: просит ответ, как «Ответить» под вопросом ассистента."""
    return {"answer": "Напишите ответ следующим сообщением.", "edit_text": None, "remove_buttons": False,
            "prompt": {"kind": "reply", "id": 77, "nonce": "nonce-77-abcdef", "until": time.time() + 600,
                       "text": "Напишите ответ на вопрос ассистента № 77 одним сообщением.",
                       "placeholder": "Ваш ответ ассистенту"}}


@pytest.fixture(autouse=True)
def _answers(monkeypatch):
    calls.clear()

    async def answer(conn, state, task_id, text, user_id, *, nonce=None, window=False):
        calls.append({"task_id": task_id, "text": text, "user_id": user_id, "nonce": nonce, "window": window})
        return {"ok": True, "code": "ok", "text": replies_owner.THANKS}

    monkeypatch.setattr(replies_owner, "answer", answer)


async def ask_prompt(rig) -> int:
    await bind(rig)
    rig.tg.press(bridge.callback_data("tq", "go"))
    await rig.bot.poll_once()
    (force,) = [p for p in rig.tg.sent() if (p.get("reply_markup") or {}).get("force_reply")]
    assert force["reply_markup"]["input_field_placeholder"] == "Ваш ответ ассистенту"
    assert "одним сообщением" in force["text"]
    return rig.tg.last_message_id()


def reply_to(rig, text: str, message_id: int, *, user: dict = OWNER_USER, chat: dict | None = None) -> None:
    rig.tg.push(message={"message_id": 900, "date": int(time.time()), "from": user, "chat": chat or private(user),
                         "text": text, "reply_to_message": {"message_id": message_id}})


@pytest.mark.asyncio
async def test_reply_to_the_prompt_goes_to_the_answer_path_once(rig):
    prompt_id = await ask_prompt(rig)
    reply_to(rig, "В пятницу.", prompt_id)
    await rig.bot.poll_once()
    assert calls == [{"task_id": 77, "text": "В пятницу.", "user_id": OWNER, "nonce": "nonce-77-abcdef", "window": True}]
    assert rig.tg.sent()[-1]["text"] == replies_owner.THANKS
    reply_to(rig, "Ещё раз", prompt_id)          # просьба одноразовая
    await rig.bot.poll_once()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_strangers_groups_other_replies_and_commands_do_not_take_the_prompt(rig):
    prompt_id = await ask_prompt(rig)
    reply_to(rig, "Я не владелец", prompt_id, user=STRANGER_USER)
    reply_to(rig, "В группе", prompt_id, chat={"id": -100500, "type": "supergroup", "title": "Группа"})
    reply_to(rig, "Ответ на другое", prompt_id - 1)
    rig.tg.text("/status")
    await rig.bot.poll_once()
    assert calls == []
    async with rig.state.pool.acquire() as conn:
        assert await conn.fetchval("SELECT value->>'kind' FROM executor_state WHERE key = 'prompt'") == "reply"
    rig.tg.text("Просто текст после — тоже ответ")      # следующее сообщение без ответа — по просьбе
    await rig.bot.poll_once()
    assert [c["text"] for c in calls] == ["Просто текст после — тоже ответ"]


@pytest.mark.asyncio
async def test_expired_prompt_says_the_question_is_closed(rig, monkeypatch):
    prompt_id = await ask_prompt(rig)
    monkeypatch.setattr(prompts, "now", lambda: time.time() + 3600)
    reply_to(rig, "Поздно", prompt_id)
    await rig.bot.poll_once()
    assert calls == [] and rig.tg.sent()[-1]["text"] == replies_owner.CLOSED


@pytest.mark.asyncio
async def test_pause_stops_at_once_and_resume_needs_a_press(rig):
    await bind(rig)
    rig.tg.text("/pause")
    await rig.bot.poll_once()
    message = rig.tg.sent()[-1]
    assert message["text"].startswith("⏸ Ассистент остановлен") and "Отменено:" in message["text"]
    assert "Продолжить…" in buttons_of(message)
    async with rig.state.pool.acquire() as conn:
        assert await halt.is_stopped(conn)
    # «Сейчас» — остановлено первой строкой; в меню вместо «Пауза» — «Продолжить»
    rig.tg.text("/status")
    await rig.bot.poll_once()
    status = rig.tg.sent()[-1]
    assert status["text"].splitlines()[2].startswith("⏸ Остановлено с ")
    assert "Продолжить" in buttons_of(status) and "Пауза" not in buttons_of(status)
    # /resume — только вопрос с кнопками; сам по себе ничего не снимает
    rig.tg.text("/resume")
    await rig.bot.poll_once()
    question = rig.tg.sent()[-1]
    assert question["text"].startswith("Продолжить работу ассистента?") and "Снова заработает:" in question["text"]
    keys = buttons_of(question)
    assert set(keys) == {"Продолжить", "Оставить остановленным"}
    async with rig.state.pool.acquire() as conn:
        assert await halt.is_stopped(conn)
    # чужое нажатие — нет
    rig.tg.press(keys["Продолжить"], user=STRANGER_USER)
    await rig.bot.poll_once()
    async with rig.state.pool.acquire() as conn:
        assert await halt.is_stopped(conn)
    rig.tg.press(keys["Продолжить"])
    await rig.bot.poll_once()
    async with rig.state.pool.acquire() as conn:
        assert not await halt.is_stopped(conn)
        log = [r["action"] for r in await conn.fetch("SELECT action FROM setup_audit ORDER BY id")]
    assert log[-2:] == ["halt.stop", "halt.resume"] or ("halt.stop" in log and log[-1] == "halt.resume")
    # старая кнопка второй раз не действует
    rig.tg.press(keys["Продолжить"])
    await rig.bot.poll_once()


@pytest.mark.asyncio
async def test_stop_is_a_synonym_and_menu_pause_works(rig):
    await bind(rig)
    rig.tg.press(bridge.callback_data(owner_chat.MODULE, "pause"))
    await rig.bot.poll_once()
    async with rig.state.pool.acquire() as conn:
        assert await halt.is_stopped(conn)
        assert await conn.fetchval("SELECT via FROM assistant_halt") == "bot"
    rig.tg.text("/stop")                  # повторно — уже остановлен, ничего не ломает
    await rig.bot.poll_once()
    assert "уже был остановлен" in rig.tg.sent()[-1]["text"]
