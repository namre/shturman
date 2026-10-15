"""Тексты бота согласований для владельца без жаргона (Р-86, «Пакет 1» ревизии удобства).

Проверяется то, что видит человек: в `/start`, `/menu`, `/help` нет JSON и номеров-параметров,
пустые списки названы пустыми, «Сейчас» называет способ работы и аккаунты, «Настройки» — словами,
сырой JSON — только по `/config json`. Данные кнопок — прежние.
"""
import dataclasses
import json

import pytest

from shturman import words
from shturman.executor import owner_chat
from shturman.outbox import drafts
from shturman.outbox.policy import Target
from exec_fakes import bind, rig  # noqa: F401 — фикстура

JARGON = ("JSON", "CHAT_ID", "USER_ID", "ACCOUNT_ID", "GRANT_ID", "SCOPE_ID", "DRAFT_ID", "on|off", "{", "[]")


async def ask(rig, text: str) -> dict:
    before = len(rig.tg.sent())
    rig.tg.text(text)
    await rig.bot.poll_once()
    sent = rig.tg.sent()[before:]
    assert sent, text
    return sent[-1]


def labels(message: dict) -> list[list[tuple[str, str]]]:
    rows = (message.get("reply_markup") or {}).get("inline_keyboard") or []
    return [[(b["text"], b.get("callback_data") or "web_app:" + b["web_app"]["url"]) for b in row] for row in rows]


@pytest.mark.asyncio
async def test_start_menu_help_are_short_plain_words_with_buttons(rig):
    await bind(rig)
    for command in ("/start", "/menu", "/help"):
        message = await ask(rig, command)
        text = message["text"]
        assert not [w for w in JARGON if w in text], (command, text)
        assert labels(message) == [[("Сейчас", "sh:oc:status"), ("Согласования", "sh:oc:approvals")],
                                   [("Пауза", "sh:oc:pause"), ("Помощь", "sh:oc:help"), ("Ещё", "sh:oc:more")]], command
    start = (await ask(rig, "/start"))["text"]
    assert len(start.splitlines()) <= 3
    # полный список команд — запасной путь — остался, но отдельной командой
    advanced = (await ask(rig, "/advanced"))["text"]
    assert "/sourcepolicy JSON" in advanced and "/trust НОМЕР_В_TELEGRAM" in advanced and "/config json" in advanced


@pytest.mark.asyncio
async def test_approvals_button_opens_the_app_when_it_has_an_address(rig):
    await bind(rig)
    rig.state.config = dataclasses.replace(rig.state.config, setup_origin="https://shturman.example:8443")
    message = await ask(rig, "/menu")
    assert labels(message)[0][1] == ("Согласования", "web_app:https://shturman.example:8443/shturman-setup/app/")
    assert "Кнопка «Штурман» внизу чата" in message["text"]


@pytest.mark.asyncio
async def test_empty_lists_are_called_empty_not_shown_as_json(rig):
    await bind(rig)
    expected = {
        "/sources": "сейчас ничего не разрешено",
        "/groups": "сейчас ничего не разрешено",
        "/mcpaccess": "сейчас ни одной",
        "/drafts": "сейчас ни один не ждёт решения",
        "/replytasks": "пока не было ни одного",
    }
    for command, phrase in expected.items():
        text = (await ask(rig, command))["text"]
        assert phrase in text, (command, text)
        assert "[]" not in text and "{" not in text and '"' not in text, (command, text)


@pytest.mark.asyncio
async def test_menu_buttons_reply_in_words_and_old_buttons_still_work(rig):
    await bind(rig)
    for data, start in [("sh:oc:config", "Настройки"), ("sh:oc:sources", "Источники и разрешения"),
                        ("sh:oc:groups", "Группы и темы для ответов"), ("sh:oc:replytasks", "Вопросы ассистента"),
                        ("sh:oc:approvals", "Согласования"), ("sh:oc:more", "Ещё")]:
        before = len(rig.tg.sent())
        rig.tg.press(data)
        await rig.bot.poll_once()
        reply = rig.tg.sent()[before:]
        assert len(reply) == 1 and reply[0]["text"].startswith(start), (data, reply)
        assert "{" not in reply[0]["text"], data
    assert rig.tg.calls("answerCallbackQuery")[-1]["text"] == "Готово."
    rig.tg.press("sh:oc:nosuch")
    await rig.bot.poll_once()
    assert rig.tg.calls("answerCallbackQuery")[-1]["text"] == "Кнопка недоступна."


@pytest.mark.asyncio
async def test_status_names_the_way_of_work_accounts_and_what_waits(rig):
    await bind(rig)
    await rig.conn.execute("""INSERT INTO setup_state (key, value) VALUES ('scenario', '{"scenario": "staff"}')""")
    text = (await ask(rig, "/status"))["text"]
    assert f"Способ работы: {words.MODE_STAFF}" in text
    assert "Аккаунты" in text and "Последнее сообщение в архиве" in text and "Модель:" in text
    assert "Защита от внедрённых инструкций" in text and "Расшифровка голосовых" in text
    assert "На согласовании: ничего." in text and "Отправка:" in text
    # свой режим — в своём способе, чужой — одной строкой, почему здесь недоступен
    assert f"{words.DRAFTS_MODE}: выключены" in text and words.REHEARSAL_ELSEWHERE in text
    await rig.conn.execute("""UPDATE setup_state SET value = '{"scenario": "own"}' WHERE key = 'scenario'""")
    text = (await ask(rig, "/status"))["text"]
    assert f"Способ работы: {words.MODE_OWN}" in text and words.DRAFTS_ELSEWHERE in text and "Репетиция:" in text


@pytest.mark.asyncio
async def test_settings_are_words_and_json_only_on_request(rig):
    await bind(rig)
    text = (await ask(rig, "/config"))["text"]
    assert text.startswith("Настройки") and "{" not in text and "Отправка:" in text and "/config json" in text
    raw = (await ask(rig, "/config json"))["text"]
    view = json.loads(raw[raw.index("{"):])
    assert "policy" in view and "autoreply" in view


@pytest.mark.asyncio
async def test_changes_say_what_changed_and_how_to_undo(rig):
    await bind(rig)
    text = (await ask(rig, '/policy {"daily_cap": 17}'))["text"]
    assert "Правила отправки изменены" in text and "станет 17" in text and "Вернуть: /policy" in text
    assert "положительный числовой идентификатор" not in (await ask(rig, "/trust abc"))["text"]
    assert "JSON-объект" not in (await ask(rig, "/policy x"))["text"]
    prepared = (await ask(rig, "/prepare on"))["text"]
    assert "ничего не отправляя" in prepared and "/prepare off" in prepared


def _target(chat_type: str, title: str, role: str) -> Target:
    return Target(chat_id=1, account_id=1, account_role=role, account_label="Помощник", chat_type=chat_type,
                  title=title, excluded=False, peer_id=1, peer_class="user" if chat_type == "personal_chat" else "chat",
                  tg_id=2001, name=title, username=None, is_bot=False)


def test_draft_card_starts_with_recipient_and_whose_name():
    personal = _target("personal_chat", "Иван Петров", "owner")
    group = _target("private_supergroup", "Корпус 3", "assistant")
    assert drafts.lead({"channel": "business"}, personal) == "Ответ: Иван Петров (личный чат) · от вашего имени"
    assert drafts.lead({"channel": "account"}, group) == "Ответ в группу «Корпус 3» · от помощника"


def test_draft_that_cannot_be_sent_has_one_button_with_the_reject_data():
    row = {"id": 7, "nonce": "abcdefgh", "prepare_only": True}
    assert drafts._buttons(row) == [[{"text": "Понятно", "data": "sh:ob:r:7:abcdefgh"}]]
    row["prepare_only"] = False
    assert [b["text"] for b in drafts._buttons(row)[0]] == ["Отправить", "Отклонить"]


def test_menu_callback_data_did_not_change():
    """Кнопки прежних версий («Статус», «Настройки», «Задания ответов», «Источники», «Группы и темы»,
    «Помощь») под старыми сообщениями продолжают работать."""
    for old in ("status", "config", "replytasks", "sources", "groups", "help"):
        assert old in owner_chat.MENU_COMMANDS
