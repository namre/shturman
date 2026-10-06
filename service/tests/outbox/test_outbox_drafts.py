"""Черновики и согласование: от создания до отправки по обоим каналам и все отказы."""

import asyncio

import asyncpg
import pytest

from shturman import bridge
from shturman.outbox import drafts, policy
from shturman.tg.gateway import AccountUnavailable, FloodWait, SendForbidden

from outbox_helpers import (  # noqa: F401 - env — фикстура
    IVAN, MARIA, OWNER, STRANGER, add_chat, add_message, business, button, draft_row, env,
    new_draft, owner_messages, press, settle, take, texts,
)


async def approved(env, chat_id, text="Добрый день! Смету пришлю в пятницу.", **extra):
    """Создаёт черновик, «доставляет» карточку и возвращает (номер, карточки)."""
    response = await new_draft(env, chat_id, text, **extra)
    assert response.status_code == 200, response.text
    return response.json()["draft_id"], await owner_messages(env.conn)


# --- полный путь ---

async def test_session_draft_is_sent_only_after_owner_tap(env):
    chat = await add_chat(env.conn, env.helper_acc)
    draft_id, cards = await approved(env, chat)
    card = texts(cards)
    assert "Иван Петров (личный чат)" in card and "от имени помощника" in card
    assert card.endswith("Добрый день! Смету пришлю в пятницу.")
    await settle(env)
    assert env.tg.sent == [] and (await draft_row(env.conn, draft_id))["status"] == "pending"

    out = await press(env, button(cards, "Отправить"))
    assert out["answer"] == "Принято, отправляю." and out["remove_buttons"] is True
    assert "принят, отправляется" in out["edit_text"]
    await settle(env)

    assert [(m["account_id"], m["peer_class"], m["tg_id"], m["text"]) for m in env.tg.sent] == [
        (env.helper_acc, "user", IVAN, "Добрый день! Смету пришлю в пятницу.")]
    row = await draft_row(env.conn, draft_id)
    assert row["status"] == "sent" and list(row["sent_tg_message_ids"]) == [env.tg.sent[0]["id"]]
    assert list(row["card_message_ids"]) != []
    note = texts(await owner_messages(env.conn))
    assert "Отправлено: Иван Петров" in note


async def test_business_draft_goes_through_plugin_job_in_owner_voice(env):
    chat = await add_chat(env.conn, env.owner_acc)
    incoming = await add_message(env.conn, chat, 41, "Когда будет смета?")
    await business(env.conn, env.owner_acc)
    draft_id, cards = await approved(env, chat, reply_to_message_id=incoming)
    card = texts(cards)
    assert "от вашего имени" in card and "В ответ на: «Когда будет смета?»" in card

    await press(env, button(cards, "Отправить"))
    await settle(env)
    sends = await take(env.conn, bridge.BUSINESS_SEND)
    assert [j["payload"] for j in sends] == [{
        "business_connection_id": "bc-1", "chat_id": IVAN,
        "text": "Добрый день! Смету пришлю в пятницу.", "reply_to_message_id": 41}]
    assert (await draft_row(env.conn, draft_id))["status"] == "sending"
    assert await bridge.deliver_result(env.conn, sends[0]["id"], {"message_id": 555}) is True
    row = await draft_row(env.conn, draft_id)
    assert row["status"] == "sent" and list(row["sent_tg_message_ids"]) == [555]
    assert env.tg.calls == 0   # сессия в отправке от имени владельца не участвует


async def test_double_tap_sends_once(env):
    chat = await add_chat(env.conn, env.helper_acc)
    draft_id, cards = await approved(env, chat)
    data = button(cards, "Отправить")
    env.tg.delay = 0.05
    answers = await asyncio.gather(*[press(env, data) for _ in range(4)])
    await settle(env)
    await press(env, data)
    await settle(env)
    assert sorted(a["answer"] for a in answers).count("Принято, отправляю.") == 1
    assert env.tg.calls == 1 and len(env.tg.sent) == 1
    assert (await draft_row(env.conn, draft_id))["status"] == "sent"


# --- кто и чем может нажать ---

async def test_forged_and_foreign_buttons_are_refused(env):
    chat = await add_chat(env.conn, env.helper_acc)
    draft_id, cards = await approved(env, chat)
    data = button(cards, "Отправить")
    nonce = data.rsplit(":", 1)[1]
    forged = [
        f"sh:ob:s:{draft_id}:{'x' * len(nonce)}",   # метка не та
        f"sh:ob:s:{draft_id}:",                       # без метки
        f"sh:ob:s:{draft_id + 1}:{nonce}",            # метка от другого черновика
        f"sh:ob:x:{draft_id}:{nonce}",                # неизвестное действие
        f"sh:ob:s:{draft_id}:{nonce}extra",
    ]
    for item in forged:
        assert (await press(env, item))["answer"] == "Кнопка недоступна."
    # верная кнопка, но нажал не владелец — и через HTTP, и напрямую через мост
    assert (await press(env, data, user=STRANGER))["answer"] == "Кнопка недоступна."
    assert (await bridge.dispatch_callback(env.conn, data, STRANGER))["answer"] == "Кнопка недоступна."
    await settle(env)
    assert env.tg.calls == 0 and (await draft_row(env.conn, draft_id))["status"] == "pending"
    assert len(data.encode()) <= 64
    assert len(bridge.callback_data("ob", f"s:{2**62}:{nonce}").encode()) <= 64


async def test_reject_and_cancel_send_nothing(env):
    chat = await add_chat(env.conn, env.helper_acc)
    first, cards = await approved(env, chat)
    out = await press(env, button(cards, "Отклонить"))
    assert out["answer"] == "Отклонено, ничего не отправлено." and "отклонён" in out["edit_text"]
    assert (await press(env, button(cards, "Отправить")))["answer"] == "Этот черновик уже отклонён."

    second, cards = await approved(env, chat, "Другой текст")
    assert (await env.client.post(f"/api/outbox/drafts/{second}/cancel")).json()["cancelled"] is True
    assert (await env.client.post(f"/api/outbox/drafts/{second}/cancel")).status_code == 409
    assert (await env.client.post("/api/outbox/drafts/999/cancel")).status_code == 404
    await press(env, button(cards, "Отправить"))
    await settle(env)
    assert env.tg.calls == 0
    assert [(await draft_row(env.conn, i))["status"] for i in (first, second)] == ["rejected", "rejected"]


async def test_expired_draft_is_closed_by_worker_and_cannot_be_sent(env):
    chat = await add_chat(env.conn, env.helper_acc)
    one, cards_one = await approved(env, chat)
    await env.conn.execute("UPDATE outbox_drafts SET expires_at = now() - interval '1 second' WHERE id = $1", one)
    # кнопку нажали раньше, чем прошла уборка: срок проверяется и здесь
    out = await press(env, button(cards_one, "Отправить"))
    assert out["answer"].startswith("Срок черновика истёк") and out["remove_buttons"] is True

    two, cards_two = await approved(env, chat, "Второй текст")
    await env.conn.execute("UPDATE outbox_drafts SET expires_at = now() - interval '1 second' WHERE id = $1", two)
    assert (await drafts.sweep(env.mod))["expired"] == 1
    assert (await press(env, button(cards_two, "Отправить")))["answer"].startswith("Срок черновика истёк")
    await settle(env)
    assert env.tg.calls == 0
    assert [(await draft_row(env.conn, i))["status"] for i in (one, two)] == ["expired", "expired"]


async def test_new_draft_supersedes_previous_one_for_the_same_chat(env):
    ivan = await add_chat(env.conn, env.helper_acc)
    maria = await add_chat(env.conn, env.helper_acc, MARIA, name="Мария")
    old, old_cards = await approved(env, ivan, "Первый вариант")
    other, _ = await approved(env, maria, "Марии — отдельно")
    new, new_cards = await approved(env, ivan, "Второй вариант")
    assert [(await draft_row(env.conn, i))["status"] for i in (old, other, new)] == [
        "superseded", "pending", "pending"]
    out = await press(env, button(old_cards, "Отправить"))
    assert "заменён новым" in out["answer"] and out["remove_buttons"] is True
    await settle(env)
    assert env.tg.calls == 0
    await press(env, button(new_cards, "Отправить"))
    await settle(env)
    assert [m["text"] for m in env.tg.sent] == ["Второй вариант"]


async def test_idempotency_key_returns_the_same_draft(env):
    chat = await add_chat(env.conn, env.helper_acc)
    first = (await new_draft(env, chat, "Текст", idempotency_key="key-000001")).json()
    again = (await new_draft(env, chat, "Текст", idempotency_key="key-000001")).json()
    assert again["draft_id"] == first["draft_id"] and again["replayed"] is True
    assert len(await owner_messages(env.conn)) == 1          # карточка одна
    conflict = await new_draft(env, chat, "Совсем другой текст", idempotency_key="key-000001")
    assert conflict.status_code == 409 and conflict.json()["reason"] == "idempotency_conflict"
    # без ключа тот же текст в тот же чат — тоже не вторая карточка
    plain = (await new_draft(env, chat, "Текст")).json()
    assert plain["draft_id"] == first["draft_id"] and plain["duplicate"] is True
    assert await env.conn.fetchval("SELECT count(*) FROM outbox_drafts") == 1
    # ключ отправленного черновика отправку не повторяет
    await env.conn.execute("UPDATE outbox_drafts SET status = 'rejected' WHERE id = $1", first["draft_id"])
    assert (await new_draft(env, chat, "Текст", idempotency_key="key-000001")).json()["status"] == "rejected"


# --- исходы отправки ---

async def test_unknown_outcome_is_never_retried(env):
    chat = await add_chat(env.conn, env.helper_acc)
    draft_id, cards = await approved(env, chat)
    env.tg.fail = [asyncio.TimeoutError()]
    await press(env, button(cards, "Отправить"))
    await settle(env)
    row = await draft_row(env.conn, draft_id)
    assert row["status"] == "outcome_unknown" and env.tg.calls == 1
    note = texts(await owner_messages(env.conn))
    assert "Не удалось узнать, дошло ли сообщение" in note and "повторно не отправится" in note
    # ни уборка, ни отправщик, ни повторное нажатие к отправке не возвращаются
    await drafts.sweep(env.mod)
    await settle(env)
    assert "Проверьте чат" in (await press(env, button(cards, "Отправить")))["answer"]
    await settle(env)
    assert env.tg.calls == 1 and env.tg.sent == []
    # тот же текст, пока исход неизвестен, считается повтором
    again = await new_draft(env, chat)
    assert again.status_code == 429 and again.json()["reason"] == "duplicate_text"


async def test_flood_wait_is_reported_and_blocks_the_account_until_it_passes(env):
    chat = await add_chat(env.conn, env.helper_acc)
    one, cards = await approved(env, chat, "Первое")
    env.tg.fail = [FloodWait(120)]
    await press(env, button(cards, "Отправить"))
    await settle(env)
    row = await draft_row(env.conn, one)
    assert (row["status"], row["error_code"]) == ("failed", "flood_wait") and env.tg.calls == 1
    assert "Telegram просит подождать ещё 120 с" in texts(await owner_messages(env.conn))

    two, cards = await approved(env, chat, "Второе")
    out = await press(env, button(cards, "Отправить"))
    assert out["answer"].startswith("Пока нельзя: Telegram просит подождать") and out["remove_buttons"] is False
    await settle(env)
    assert env.tg.calls == 1 and (await draft_row(env.conn, two))["status"] == "pending"

    await env.conn.execute("UPDATE outbox_accounts SET blocked_until = now() - interval '1 second'")
    await press(env, button(cards, "Отправить"))
    await settle(env)
    assert [m["text"] for m in env.tg.sent] == ["Второе"]


@pytest.mark.parametrize("error,code", [(SendForbidden(), "send_forbidden"),
                                        (AccountUnavailable(), "session_unavailable")])
async def test_definite_refusal_from_gateway_is_a_plain_failure(env, error, code):
    chat = await add_chat(env.conn, env.helper_acc)
    draft_id, cards = await approved(env, chat)
    env.tg.fail = [error]
    await press(env, button(cards, "Отправить"))
    await settle(env)
    row = await draft_row(env.conn, draft_id)
    assert (row["status"], row["error_code"]) == ("failed", code) and env.tg.sent == []
    assert "Не отправлено" in texts(await owner_messages(env.conn))


async def test_long_text_is_split_by_paragraphs_and_card_shows_all_of_it(env):
    chat = await add_chat(env.conn, env.helper_acc)
    incoming = await add_message(env.conn, chat, 7, "Пришлите подробности")
    paragraphs = [f"Абзац {i}. " + "слово " * 180 + f"конец{i}." for i in range(8)]
    text = "\n\n".join(paragraphs)
    draft_id, cards = await approved(env, chat, text, reply_to_message_id=incoming)
    assert len(cards) > 1 and all(len(c["payload"]["text"]) <= 4096 for c in cards)
    assert [bool(c["payload"]["buttons"]) for c in cards] == [False] * (len(cards) - 1) + [True]
    shown = texts(cards)
    assert all(f"конец{i}." in shown and f"Абзац {i}." in shown for i in range(8))
    assert "часть 1 из" in shown and "уйдёт 3 сообщениями подряд" in shown

    await press(env, button(cards, "Отправить"))
    await settle(env)
    parts = [m["text"] for m in env.tg.sent]
    assert len(parts) == 3 and all(len(p) <= 3500 for p in parts)
    assert "\n\n".join(parts) == text                       # разрез только по границам абзацев
    assert [m["reply_to_tg_id"] for m in env.tg.sent] == [7, None, None]
    row = await draft_row(env.conn, draft_id)
    assert row["status"] == "sent" and len(row["sent_tg_message_ids"]) == 3 and row["parts_sent"] == 3

    too_long = await new_draft(env, chat, "я " * 9000)
    assert too_long.status_code == 422 and too_long.json()["reason"] == "text_too_long"
    assert (await new_draft(env, chat, "я" * 20001)).status_code == 422


async def test_part_failure_is_reported_as_partial_and_not_continued(env):
    chat = await add_chat(env.conn, env.helper_acc)
    text = "\n\n".join("слово " * 400 for _ in range(3))
    draft_id, cards = await approved(env, chat, text)
    env.tg.fail = [None, FloodWait(30)]
    await press(env, button(cards, "Отправить"))
    await settle(env)
    row = await draft_row(env.conn, draft_id)
    assert (row["status"], row["error_code"], row["parts_sent"]) == ("failed", "partial", 1)
    assert env.tg.calls == 2 and "только часть сообщения: 1 из" in texts(await owner_messages(env.conn))


async def test_business_text_over_4096_is_refused_at_creation(env):
    chat = await add_chat(env.conn, env.owner_acc)
    await add_message(env.conn, chat, 1, "Вопрос")
    await business(env.conn, env.owner_acc)
    response = await new_draft(env, chat, "ж" * 4097)
    assert response.status_code == 422 and response.json()["reason"] == "text_too_long_business"
    assert "4096" in response.json()["error"]
    assert (await new_draft(env, chat, "ж" * 4096)).status_code == 200


async def test_business_outcomes_from_plugin(env):
    chat = await add_chat(env.conn, env.owner_acc)
    await add_message(env.conn, chat, 1, "Вопрос")
    await business(env.conn, env.owner_acc)

    async def sending(text):
        draft_id, cards = await approved(env, chat, text)
        await press(env, button(cards, "Отправить"))
        await settle(env)
        return draft_id, (await take(env.conn, bridge.BUSINESS_SEND))[0]["id"]

    # плагин точно знает, что не ушло
    one, job = await sending("Первое")
    await bridge.deliver_failure(env.conn, job, "not_sent: Bad Request: chat not found", retry_in=60)
    assert (await draft_row(env.conn, one))["status"] == "failed"
    # любая другая ошибка — «неизвестно»; повторной попытки у задания нет
    two, job = await sending("Второе")
    assert await bridge.deliver_failure(env.conn, job, "timed out", retry_in=60) == "failed"
    assert (await draft_row(env.conn, two))["status"] == "outcome_unknown"
    # закрытие без номера сообщения доставку не подтверждает
    three, job = await sending("Третье")
    await bridge.deliver_result(env.conn, job, {"message_id": None})
    assert (await draft_row(env.conn, three))["status"] == "outcome_unknown"
    assert await take(env.conn, bridge.BUSINESS_SEND) == []


async def test_business_job_nobody_took_is_withdrawn_and_stuck_one_becomes_unknown(env):
    chat = await add_chat(env.conn, env.owner_acc)
    await add_message(env.conn, chat, 1, "Вопрос")
    await business(env.conn, env.owner_acc)
    one, cards = await approved(env, chat, "Первое")
    await press(env, button(cards, "Отправить"))
    await settle(env)
    assert (await drafts.sweep(env.mod))["not_sent"] == 0          # ещё не поздно
    await env.conn.execute("UPDATE outbox_drafts SET claimed_at = now() - interval '10 minutes' WHERE id = $1", one)
    assert (await drafts.sweep(env.mod))["not_sent"] == 1
    row = await draft_row(env.conn, one)
    assert (row["status"], row["error_code"]) == ("failed", "executor_absent")
    assert await take(env.conn, bridge.BUSINESS_SEND) == []          # задание снято и позже не уйдёт

    two, cards = await approved(env, chat, "Второе")
    await press(env, button(cards, "Отправить"))
    await settle(env)
    job = (await take(env.conn, bridge.BUSINESS_SEND))[0]["id"]      # плагин взял и пропал
    await env.conn.execute("UPDATE outbox_drafts SET claimed_at = now() - interval '10 minutes' WHERE id = $1", two)
    assert (await drafts.sweep(env.mod))["unknown"] == 1
    assert (await draft_row(env.conn, two))["status"] == "outcome_unknown"
    await owner_messages(env.conn)
    # ответ всё-таки пришёл: подтверждение записывается, второй отправки нет
    await bridge.deliver_result(env.conn, job, {"message_id": 777})
    row = await draft_row(env.conn, two)
    assert row["status"] == "sent" and list(row["sent_tg_message_ids"]) == [777]
    assert "Пришло подтверждение" in texts(await owner_messages(env.conn))


async def test_interrupted_and_late_work_is_closed_without_sending(env):
    chat = await add_chat(env.conn, env.helper_acc)
    # сервис упал посреди отправки: исход неизвестен
    one, _ = await approved(env, chat, "Первое")
    await env.conn.execute("UPDATE outbox_drafts SET status = 'approved', approved_at = now() WHERE id = $1", one)
    await env.conn.execute("UPDATE outbox_drafts SET status = 'sending', claimed_at = now() WHERE id = $1", one)
    assert (await drafts.sweep(env.mod))["unknown"] == 1
    # согласовано давно, а отправить вовремя не успели: с опозданием не уходит
    two, _ = await approved(env, chat, "Второе")
    await env.conn.execute(
        "UPDATE outbox_drafts SET status = 'approved', approved_at = now() - interval '1 hour' WHERE id = $1", two)
    await settle(env)
    rows = [await draft_row(env.conn, i) for i in (one, two)]
    assert [(r["status"], r["error_code"]) for r in rows] == [
        ("outcome_unknown", "outcome_unknown"), ("failed", "stale")]
    assert env.tg.calls == 0


# --- правила ---

async def test_excluded_blocked_and_forbidden_chats_cannot_be_addressed(env):
    excluded = await add_chat(env.conn, env.helper_acc, MARIA, name="Мария", exclude=True)
    service = await add_chat(env.conn, env.helper_acc, 777000, name="Telegram")
    botfather = await add_chat(env.conn, env.helper_acc, 93372553, name="BotFather", username="BotFather")
    for chat, reason in ((excluded, "chat_excluded"), (service, "chat_blocked"), (botfather, "chat_blocked")):
        response = await new_draft(env, chat)
        assert response.status_code == 409 and response.json()["reason"] == reason
    assert (await new_draft(env, 424242)).status_code == 404

    ivan = await add_chat(env.conn, env.helper_acc)
    assert (await env.client.put(f"/api/outbox/chats/{ivan}", json={"drafting": "deny"})).json()["can_draft"] is False
    assert (await new_draft(env, ivan)).json()["reason"] == "drafting_forbidden"
    await env.client.put(f"/api/outbox/chats/{ivan}", json={"drafting": "default"})
    # запрет по умолчанию для аккаунта и разрешение для одного чата
    await env.client.put("/api/outbox/policy", json={"account_id": env.helper_acc, "drafting_default": "deny"})
    assert (await new_draft(env, ivan)).json()["reason"] == "drafting_forbidden"
    await env.client.put(f"/api/outbox/chats/{ivan}", json={"drafting": "allow"})
    draft_id, cards = await approved(env, ivan)

    # чат исключили уже после создания черновика: нажатие ничего не отправит
    await env.conn.execute("UPDATE chats SET excluded = true WHERE id = $1", ivan)
    out = await press(env, button(cards, "Отправить"))
    assert out["answer"].startswith("Не отправлено") and "исключён" in out["edit_text"]
    await settle(env)
    row = await draft_row(env.conn, draft_id)
    assert (row["status"], row["error_code"]) == ("failed", "chat_excluded") and env.tg.calls == 0
    assert await env.conn.fetchval("SELECT count(*) FROM outbox_drafts") == 1


async def test_owner_account_is_never_a_session_channel(env):
    env.tg.sendable.add(env.owner_acc)          # даже если шлюз по ошибке ответит «можно»
    chat = await add_chat(env.conn, env.owner_acc)
    await add_message(env.conn, chat, 1, "Вопрос")
    forced = await new_draft(env, chat, channel="session")
    assert forced.status_code == 409 and forced.json()["reason"] == "owner_read_only"
    # без бизнес-бота у владельца канала отправки нет вовсе
    auto = await new_draft(env, chat)
    assert auto.status_code == 409 and auto.json()["reason"] == "business_unavailable"
    await business(env.conn, env.owner_acc, can_reply=False)
    assert (await new_draft(env, chat)).json()["reason"] == "business_unavailable"
    # и наоборот: чат помощника через бизнес-бота не идёт
    helper_chat = await add_chat(env.conn, env.helper_acc, MARIA, name="Мария")
    assert (await new_draft(env, helper_chat, channel="business")).json()["reason"] == "business_not_for_assistant"
    assert (await new_draft(env, helper_chat, channel="email")).status_code == 400
    # обход проверки создания (строка в базе руками) упирается в ту же проверку перед отправкой
    await env.conn.execute(
        """INSERT INTO outbox_drafts (account_id, chat_id, channel, text, text_hash, origin, nonce, status, expires_at)
           VALUES ($1, $2, 'session', 'x', 'h', 'agent', 'n', 'pending', now() + interval '1 hour')""",
        env.owner_acc, chat)
    await env.conn.execute("UPDATE outbox_drafts SET status = 'approved', approved_at = now()")
    await settle(env)
    row = await env.conn.fetchrow("SELECT status, error_code FROM outbox_drafts")
    assert tuple(row) == ("failed", "owner_read_only") and env.tg.calls == 0


async def test_business_reply_only_within_24_hours_of_last_incoming(env):
    chat = await add_chat(env.conn, env.owner_acc)
    await business(env.conn, env.owner_acc)
    assert (await new_draft(env, chat)).json()["reason"] == "business_window_closed"   # входящих нет вовсе
    await add_message(env.conn, chat, 1, "Старый вопрос", age=25 * 3600)
    await add_message(env.conn, chat, 2, "Свой ответ", sender=OWNER, outgoing=True, age=60)
    late = await new_draft(env, chat)
    assert late.status_code == 409 and late.json()["reason"] == "business_window_closed"
    # свежее входящее открывает окно
    fresh = await add_message(env.conn, chat, 3, "Новый вопрос", age=3600)
    draft_id, cards = await approved(env, chat)
    # окно закрылось, пока владелец думал: нажатие не отправляет
    await env.conn.execute("UPDATE messages SET sent_at = now() - interval '24 hours' WHERE id = $1", fresh)
    out = await press(env, button(cards, "Отправить"))
    assert "в течение суток" in out["edit_text"] and out["remove_buttons"] is True
    await settle(env)
    assert (await draft_row(env.conn, draft_id))["error_code"] == "business_window_closed"
    assert await take(env.conn, bridge.BUSINESS_SEND) == []
    # группы и боты через бизнес-бота недоступны
    group = await add_chat(env.conn, env.owner_acc, 3001, cls="chat", type_="private_group", name="Семья")
    assert (await new_draft(env, group)).json()["reason"] == "business_private_only"


async def test_session_channel_needs_running_assistant_session(env):
    chat = await add_chat(env.conn, env.helper_acc)
    draft_id, cards = await approved(env, chat)
    env.tg.sendable.clear()                      # сессия пропала после создания
    out = await press(env, button(cards, "Отправить"))
    assert out["answer"].startswith("Пока нельзя") and out["remove_buttons"] is False
    assert (await draft_row(env.conn, draft_id))["status"] == "pending"
    assert (await new_draft(env, chat, "Новый")).json()["reason"] == "session_unavailable"
    del env.state.extras["tg"]                   # модуль сессий не настроен вовсе
    assert (await new_draft(env, chat, "Новый")).json()["reason"] == "session_not_configured"
    assert env.tg.calls == 0


async def test_limits_and_duplicates(env):
    chat = await add_chat(env.conn, env.helper_acc)
    await env.client.put("/api/outbox/policy", json={"chat_window_max": 2, "duplicate_window_seconds": 300})
    for text in ("Раз", "Два"):
        _, cards = await approved(env, chat, text)
        await press(env, button(cards, "Отправить"))
        await settle(env)
    # повтор только что отправленного текста — отказ уже при создании (и при другом регистре)
    repeat = await new_draft(env, chat, "  раз ")
    assert repeat.status_code == 429 and repeat.json()["reason"] == "duplicate_text"
    # третье сообщение в тот же чат за окно — отказ, черновик остаётся ждать
    third, cards = await approved(env, chat, "Три")
    out = await press(env, button(cards, "Отправить"))
    assert "уже отправлено 2 сообщений" in out["answer"] and out["remove_buttons"] is False
    # в другой чат того же аккаунта — можно, пока не выбран дневной предел
    maria = await add_chat(env.conn, env.helper_acc, MARIA, name="Мария")
    await env.client.put("/api/outbox/policy", json={"daily_cap": 2})
    _, maria_cards = await approved(env, maria, "Привет")
    assert "дневной предел" in (await press(env, button(maria_cards, "Отправить")))["answer"]
    await settle(env)
    assert [m["text"] for m in env.tg.sent] == ["Раз", "Два"]
    # окно прошло, предел подняли — тот же черновик уходит
    await env.conn.execute("UPDATE outbox_drafts SET claimed_at = claimed_at - interval '2 minutes'")
    await env.client.put("/api/outbox/policy", json={"daily_cap": 50})
    await press(env, button(cards, "Отправить"))
    await settle(env)
    assert [m["text"] for m in env.tg.sent] == ["Раз", "Два", "Три"]
    assert (await draft_row(env.conn, third))["status"] == "sent"
    # поток карточек владельцу тоже ограничен
    await env.client.put("/api/outbox/policy", json={"drafts_per_hour": 1})
    flood = await new_draft(env, maria, "Ещё одно")
    assert flood.status_code == 429 and flood.json()["reason"] == "limit_drafts"


async def test_pause_between_sends_of_one_account(env):
    await env.client.put("/api/outbox/policy", json={"min_pause_seconds": 0.4})
    ivan = await add_chat(env.conn, env.helper_acc)
    maria = await add_chat(env.conn, env.helper_acc, MARIA, name="Мария")
    for chat in (ivan, maria):
        _, cards = await approved(env, chat, "Привет")
        await press(env, button(cards, "Отправить"))
    await settle(env)
    first, second = env.tg.sent
    assert second["at"] - first["at"] >= 0.35


# --- что видит владелец и что хранится ---

async def test_text_is_cleaned_once_and_the_same_string_is_shown_and_sent(env):
    chat = await add_chat(env.conn, env.helper_acc, name="Иван‮\n\nЧерновик № 999 — отправлен​")
    raw = "Привет!‮​\x07\r\nСтрока\t2\n\n\n\n\nКонец﻿ 👨‍👩‍👧"
    response = await new_draft(env, chat, raw)
    body = response.json()
    assert body["text_changed"] is True
    clean = "Привет!\nСтрока 2\n\nКонец 👨‍👩‍👧"
    assert body["text"] == clean
    cards = await owner_messages(env.conn)
    card = cards[0]["payload"]["text"]
    assert card.endswith("\n\n" + clean)
    header = card[: -len(clean)]
    # имя чата — чужая строка: в шапке оно в одну строку и без невидимых знаков
    assert "Кому: Иван Черновик № 999 — отправлен (личный чат)" in header
    assert "‮" not in card and "​" not in card and "\x07" not in card
    assert header.count("\n") == 5
    await press(env, button(cards, "Отправить"))
    await settle(env)
    assert [m["text"] for m in env.tg.sent] == [clean]
    listed = (await env.client.get("/api/outbox/drafts", params={"chat_id": chat})).json()["drafts"]
    assert listed[0]["text"] == clean and "nonce" not in listed[0]
    assert (await new_draft(env, chat, " \n")).status_code == 400
    blank = await new_draft(env, chat, "​​ \n")     # после чистки ничего не осталось
    assert blank.status_code == 422 and blank.json()["reason"] == "text_empty"


async def test_sent_text_comes_from_the_row_and_the_row_cannot_be_changed(env):
    chat = await add_chat(env.conn, env.helper_acc)
    draft_id, cards = await approved(env, chat)
    for sql in ("UPDATE outbox_drafts SET text = 'подмена' WHERE id = $1",
                "UPDATE outbox_drafts SET chat_id = chat_id + 1 WHERE id = $1",
                "UPDATE outbox_drafts SET channel = 'business' WHERE id = $1",
                "UPDATE outbox_drafts SET status = 'sending' WHERE id = $1",      # мимо согласования
                "UPDATE outbox_drafts SET status = 'sent' WHERE id = $1"):
        with pytest.raises(asyncpg.RaiseError):
            await env.conn.execute(sql, draft_id)
    with pytest.raises(asyncpg.RaiseError):
        await env.conn.execute(
            """INSERT INTO outbox_drafts (account_id, chat_id, channel, text, text_hash, origin, nonce, status, expires_at)
               VALUES ($1, $2, 'session', 'x', 'h', 'agent', 'n', 'approved', now())""", env.helper_acc, chat)
    await press(env, button(cards, "Отправить"))
    await settle(env)
    with pytest.raises(asyncpg.RaiseError):
        await env.conn.execute("UPDATE outbox_drafts SET status = 'approved' WHERE id = $1", draft_id)
    assert [m["text"] for m in env.tg.sent] == ["Добрый день! Смету пришлю в пятницу."]


async def test_no_owner_no_drafts_and_listing_filters(env):
    chat = await add_chat(env.conn, env.helper_acc)
    await env.conn.execute("DELETE FROM settings WHERE key = 'owner'")
    assert (await new_draft(env, chat)).json()["reason"] == "owner_unknown"
    await bridge.set_owner(env.conn, OWNER, OWNER)
    draft_id, _ = await approved(env, chat)
    listing = await env.client.get("/api/outbox/drafts", params={"status": "pending"})
    assert [d["draft_id"] for d in listing.json()["drafts"]] == [draft_id]
    assert (await env.client.get("/api/outbox/drafts", params={"status": "sent"})).json()["drafts"] == []
    assert (await env.client.get("/api/outbox/drafts", params={"status": "nope"})).status_code == 400
    assert (await env.client.get("/api/outbox/drafts", params={"chat_id": "x"})).status_code == 400
    bad = await env.client.post("/api/outbox/drafts", json={"chat_id": "1", "text": "т"})
    assert bad.status_code == 400
    assert (await new_draft(env, chat, reply_to_message_id=987654)).json()["reason"] == "reply_not_found"


async def test_policy_route_clamps_values_and_tells_owner(env):
    view = (await env.client.get("/api/outbox/policy")).json()
    assert view["policy"]["drafting_default"] == "allow" and view["limits"]["daily_cap"]["max"] == 1000
    changed = await env.client.put("/api/outbox/policy", json={"daily_cap": 10**9, "chat_window_max": 0})
    assert changed.json()["policy"]["daily_cap"] == 1000 and changed.json()["policy"]["chat_window_max"] == 1
    assert "daily_cap" in texts(await owner_messages(env.conn))
    assert (await env.client.put("/api/outbox/policy", json={"rm": 1})).status_code == 400
    assert (await env.client.put("/api/outbox/policy", json={"daily_cap": "много"})).status_code == 400
    assert (await env.client.put("/api/outbox/policy", json={"drafting_default": "да"})).status_code == 400
    assert (await env.client.put("/api/outbox/chats/999", json={"drafting": "deny"})).status_code == 404
    # испорченное значение в базе не ломает правила: берётся умолчание
    await env.conn.execute("UPDATE settings SET value = '{\"daily_cap\": \"x\", \"min_pause_seconds\": -5}' "
                           "WHERE key = 'outbox.policy'")
    rules = await policy.load(env.conn)
    assert rules["daily_cap"] == 400 and rules["min_pause_seconds"] == 0
