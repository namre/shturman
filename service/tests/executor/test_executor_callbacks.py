"""Кнопки в боте согласований: нажатие приходит от Telegram и принимается только от владельца."""

from shturman import bridge

from exec_fakes import (  # noqa: F401 — rig — фикстура
    OWNER, OWNER_USER, STRANGER_USER, bind, private, refusal, rig,
)

MARKUP = {"inline_keyboard": [[{"text": "Да", "callback_data": "sh:tcb:yes"},
                               {"text": "Нет", "callback_data": "sh:tcb:no"}]]}


def handler(reply):
    """Регистрирует разбор кнопок `sh:tcb:*` и запоминает, кто что нажал."""
    seen = []

    @bridge.on_callback("tcb")
    async def pressed(conn, rest, user_id):
        seen.append((rest, user_id))
        return reply(rest) if callable(reply) else reply

    return seen


async def press(rig, data, **kwargs):
    rig.tg.press(data, markup=MARKUP, **kwargs)
    await rig.bot.poll_once()


async def test_owner_press_is_dispatched_with_the_id_telegram_reported(rig):
    seen = handler({"answer": "Принято.", "edit_text": "Черновик принят", "remove_buttons": True})
    await bind(rig)
    await press(rig, "sh:tcb:yes", message_id=777, query_id="q-1")
    assert seen == [("yes", OWNER)]
    assert rig.tg.calls("answerCallbackQuery") == [{"callback_query_id": "q-1", "text": "Принято."}]
    edit = rig.tg.calls("editMessageText")
    assert edit == [{"chat_id": OWNER, "message_id": 777, "text": "Черновик принят",
                     "link_preview_options": {"is_disabled": True}}]          # без клавиатуры и без разметки
    assert rig.bot.counters["callbacks"] == 1


async def test_stranger_foreign_prefix_and_foreign_chat_are_refused_without_dispatch(rig):
    seen = handler({"answer": "Принято."})
    await bind(rig)
    group = {"id": -100500, "type": "supergroup", "title": "Группа"}
    await press(rig, "sh:tcb:yes", user=STRANGER_USER)                      # нажал не владелец
    await press(rig, "sh:tcb:yes", user=STRANGER_USER, chat=private(STRANGER_USER))
    await press(rig, "ea:approve:1")                                        # кнопка не сервиса
    await press(rig, "bd:send:1")
    await press(rig, "sh:tcb:yes", chat=group)                              # владелец, но не в своём чате с ботом
    await press(rig, "sh:tcb:yes", chat=private(STRANGER_USER))
    rig.tg.push(callback_query={"id": "inline", "from": OWNER_USER, "inline_message_id": "x", "data": "sh:tcb:yes"})
    await rig.bot.poll_once()
    assert seen == []
    answers = rig.tg.calls("answerCallbackQuery")
    assert len(answers) == 7 and {a["text"] for a in answers} == {"Кнопка недоступна."}
    assert rig.tg.calls("editMessageText") == [] and rig.tg.calls("editMessageReplyMarkup") == []
    assert rig.bot.counters["callbacks_refused"] == 7


async def test_nobody_can_press_before_the_owner_is_bound(rig):
    seen = handler({"answer": "Принято."})
    await bridge.set_owner(rig.conn, OWNER, OWNER)      # запись есть, но привязки через бота не было
    await press(rig, "sh:tcb:yes")
    assert seen == [] and rig.tg.calls("answerCallbackQuery")[0]["text"] == "Кнопка недоступна."


async def test_keyboard_is_kept_removed_or_left_alone_as_the_module_asked(rig):
    replies = {
        "keep": {"answer": "Обновлено.", "edit_text": "Новый текст", "remove_buttons": False},
        "strip": {"answer": "Готово.", "edit_text": None, "remove_buttons": True},
        "none": {"answer": "Понял.", "edit_text": None, "remove_buttons": False},
    }
    handler(lambda rest: replies[rest])
    await bind(rig)
    await press(rig, "sh:tcb:keep", message_id=10)
    await press(rig, "sh:tcb:strip", message_id=11)
    await press(rig, "sh:tcb:none", message_id=12)
    assert rig.tg.calls("editMessageText") == [{
        "chat_id": OWNER, "message_id": 10, "text": "Новый текст", "reply_markup": MARKUP,
        "link_preview_options": {"is_disabled": True}}]
    assert rig.tg.calls("editMessageReplyMarkup") == [{
        "chat_id": OWNER, "message_id": 11, "reply_markup": {"inline_keyboard": []}}]
    assert [a["text"] for a in rig.tg.calls("answerCallbackQuery")] == ["Обновлено.", "Готово.", "Понял."]


async def test_long_edit_text_is_cut_by_telegram_units(rig):
    handler({"answer": "x" * 500, "edit_text": "🙂" * 3000, "remove_buttons": True})
    await bind(rig)
    await press(rig, "sh:tcb:yes")
    text = rig.tg.calls("editMessageText")[0]["text"]
    assert bridge.utf16_len(text) <= 4096 and text.endswith("…")
    assert len(rig.tg.calls("answerCallbackQuery")[0]["text"]) <= 200


async def test_failed_handler_or_failed_edit_does_not_stop_the_bot(rig):
    def boom(rest):
        raise RuntimeError("Секретный текст в ошибке")

    seen = handler(boom)
    await bind(rig)
    await press(rig, "sh:tcb:yes")
    assert rig.tg.calls("answerCallbackQuery")[-1]["text"] == "Не получилось. Попробуйте ещё раз."

    bridge.on_callback("tcb")(lambda conn, rest, user: _ok())
    rig.tg.script["editMessageText"] = [refusal(400, "Bad Request: message is not modified")]
    rig.tg.script["answerCallbackQuery"] = [refusal(400, "Bad Request: query is too old")]
    await press(rig, "sh:tcb:yes")
    await press(rig, "sh:tcb:yes")                      # следующее нажатие разбирается как обычно
    assert rig.bot.counters["callbacks"] == 2 and rig.bot.offset == rig.tg._update_id + 1
    assert len(seen) == 1


async def _ok():
    return {"answer": "Сделано.", "edit_text": "Итог", "remove_buttons": True}
