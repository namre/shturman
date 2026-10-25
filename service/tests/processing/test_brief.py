"""Справка при контакте и сигнал о просрочке (`brief/`): шаги 2–3 записки «держать, подкладывать,
сигналить». Модель подменена ответом; проверяется, что, кому и как доставляет код."""

from datetime import timedelta

import pytest

from shturman import bridge, jobs
from shturman.brief import core as brief
from shturman.processing import commitments, people

from memory_helpers import notifications, owner, run_with
from proc_helpers import OWNER, T0, TZ, account, buttons_of, chat, peer_id, press, say

IVAN, PETR, BRIGADE = 2001, 2002, 5001
WHAT = "прислать смету по фасадам"
SUM_TEXT = "Пришлю смету по фасадам на 3 млн завтра"


@pytest.fixture(autouse=True)
def own_bot():
    bridge.set_builtin({bridge.NOTIFY_OWNER, bridge.NOTIFY_EDIT})
    yield
    bridge.set_builtin(())


class W:
    pass


async def world(conn, *, accept=True):
    """Иван обещал владельцу смету «завтра» (7 октября); владелец принял, если accept."""
    w = W()
    w.account = await account(conn)
    await bridge.set_owner(conn, OWNER, OWNER)
    w.ivan_chat = await chat(conn, w.account, IVAN, "Иван Петров")
    w.petr_chat = await chat(conn, w.account, PETR, "Пётр Сидоров")
    w.group = await chat(conn, w.account, BRIGADE, "Стройка: Северный", type_="private_supergroup", cls="channel")
    await say(conn, w.ivan_chat, [(IVAN, "Иван Петров", SUM_TEXT)])
    await run_with(conn, lambda job: {"commitments": [{"message": 1, "source_quote": SUM_TEXT, "what": WHAT,
                                                       "due_expression": "завтра"}]})
    w.commitment = await conn.fetchval("SELECT id FROM commitments")
    if accept:
        with owner():
            assert (await commitments.accept(conn, w.commitment))["ok"]
    await notifications(conn)          # сводки прогона — не предмет этих тестов
    await sent(conn)
    return w


async def sent(conn):
    """Сообщения владельцу через свой бот сервиса, в порядке постановки."""
    got = await jobs.claim(conn, [bridge.NOTIFY_OWNER], worker="test", limit=50,
                           executor=bridge.executor_for(bridge.NOTIFY_OWNER))
    return sorted(got, key=lambda j: j["id"])


async def incoming(conn, chat_id, sender, name, text, at, first_id):
    mid, = await say(conn, chat_id, [(sender, name, text, {"at": at})], first_id=first_id)
    return mid


async def briefs_sent(conn):
    return [j for j in await sent(conn) if j["payload"]["text"].startswith("Пишет ")]


async def test_contact_brings_a_brief_without_sums_or_quotes(conn):
    w = await world(conn)
    at = T0 + timedelta(hours=2)
    mid = await incoming(conn, w.ivan_chat, IVAN, "Иван Петров", "Добрый день, по фасадам вопрос", at, 10)
    brief_id = await brief.on_message(conn, mid, tz=TZ, now=at + timedelta(minutes=1))
    assert brief_id is not None
    card, = await briefs_sent(conn)
    text = card["payload"]["text"]
    assert text.startswith("Пишет Иван Петров. Открытых договорённостей: 1 (обещано вам: 1).")
    assert "Видно только то, что есть в читаемых чатах" in text and "разобраны по" in text
    # безопасная доставка: в уведомлении ни формулировки, ни суммы, ни цитаты
    assert "смет" not in text and "3 млн" not in text and SUM_TEXT not in text
    assert [d for _, d in buttons_of(card)] == [f"sh:br:s:{brief_id}", f"sh:br:z:{brief_id}"]


async def test_not_more_often_than_every_four_hours(conn):
    w = await world(conn)
    at = T0 + timedelta(hours=2)
    first = await incoming(conn, w.ivan_chat, IVAN, "Иван Петров", "Привет", at, 10)
    assert await brief.on_message(conn, first, tz=TZ, now=at) is not None
    again = await incoming(conn, w.ivan_chat, IVAN, "Иван Петров", "Ещё вопрос", at + timedelta(hours=1), 11)
    assert await brief.on_message(conn, again, tz=TZ, now=at + timedelta(hours=1)) is None
    later = await incoming(conn, w.ivan_chat, IVAN, "Иван Петров", "И ещё", at + timedelta(hours=4, minutes=5), 12)
    assert await brief.on_message(conn, later, tz=TZ, now=at + timedelta(hours=4, minutes=5)) is not None


async def test_no_brief_without_open_commitments_or_for_outgoing_hidden_and_old(conn):
    w = await world(conn)
    at = T0 + timedelta(hours=2)
    petr = await incoming(conn, w.petr_chat, PETR, "Пётр Сидоров", "Привет", at, 10)
    assert await brief.on_message(conn, petr, tz=TZ, now=at) is None          # с Петром договорённостей нет
    mine = await incoming(conn, w.ivan_chat, OWNER, "Евгений Тестов", "Жду смету", at, 11)
    assert await brief.on_message(conn, mine, tz=TZ, now=at) is None          # своё сообщение
    hidden = await incoming(conn, w.ivan_chat, IVAN, "Иван Петров", "Привет", at, 12)
    await conn.execute("UPDATE messages SET agent_visible = false WHERE id = $1", hidden)
    assert await brief.on_message(conn, hidden, tz=TZ, now=at) is None        # скрыто защитой
    old = await incoming(conn, w.ivan_chat, IVAN, "Иван Петров", "Привет", at, 13)
    assert await brief.on_message(conn, old, tz=TZ, now=at + timedelta(hours=4)) is None   # не свежее
    assert await briefs_sent(conn) == []


async def test_quiet_hours_halt_and_no_bot(conn):
    w = await world(conn)
    night = T0.replace(hour=21)        # 00:00 по Москве — тихие часы по умолчанию (23:00–08:00)
    mid = await incoming(conn, w.ivan_chat, IVAN, "Иван Петров", "Не спите?", night, 10)
    assert await brief.on_message(conn, mid, tz=TZ, now=night) is None
    day = T0 + timedelta(hours=2)
    mid = await incoming(conn, w.ivan_chat, IVAN, "Иван Петров", "Добрый день", day, 11)
    bridge.set_builtin(())
    assert await brief.on_message(conn, mid, tz=TZ, now=day) is None          # без своего бота — некуда писать
    bridge.set_builtin({bridge.NOTIFY_OWNER, bridge.NOTIFY_EDIT})
    await conn.execute("UPDATE assistant_halt SET stopped = true WHERE id = 1")
    assert await brief.on_message(conn, mid, tz=TZ, now=day) is None          # «Остановить всё»


async def test_group_message_from_the_same_person(conn):
    w = await world(conn)
    at = T0 + timedelta(hours=2)
    mid = await incoming(conn, w.group, IVAN, "Иван Петров", "Бригада на месте", at, 50)
    assert await brief.on_message(conn, mid, tz=TZ, now=at) is not None


async def test_unconfirmed_commitment_is_shown_with_accept(conn):
    w = await world(conn, accept=False)
    at = T0 + timedelta(hours=2)
    mid = await incoming(conn, w.ivan_chat, IVAN, "Иван Петров", "Добрый день", at, 10)
    brief_id = await brief.on_message(conn, mid, tz=TZ, now=at)
    card, = await briefs_sent(conn)
    assert "Не подтверждено вами: 1." in card["payload"]["text"]
    shown = await press(conn, f"sh:br:s:{brief_id}")
    reply = shown["reply"]
    assert WHAT in reply["text"] and "не подтверждено вами" in reply["text"]
    accept, wrong = [b["data"] for b in reply["buttons"][0]]
    assert accept.startswith(f"sh:br:a:{w.commitment}:") and wrong == f"sh:br:w:{w.commitment}"
    assert (await press(conn, accept))["answer"] == "Принято."
    assert await conn.fetchval("SELECT status FROM commitments WHERE id = $1", w.commitment) == "open"
    assert (await press(conn, accept))["answer"] == "Уже решено."


async def test_show_done_wrong_and_snooze(conn):
    w = await world(conn)
    at = T0 + timedelta(hours=2)
    mid = await incoming(conn, w.ivan_chat, IVAN, "Иван Петров", "Добрый день", at, 10)
    brief_id = await brief.on_message(conn, mid, tz=TZ, now=at)
    reply = (await press(conn, f"sh:br:s:{brief_id}"))["reply"]
    done, wrong, remind_ = [b["data"] for b in reply["buttons"][0]]
    assert remind_ == f"sh:br:n:{w.commitment}"            # «напомнить» — у обещанного вам (`brief/remind.py`)
    assert reply["buttons"][-1][0]["data"] == f"sh:br:z:{brief_id}"
    assert (await press(conn, done))["answer"] == "Отмечено: выполнено."
    event = await conn.fetchrow("SELECT actor, action, details FROM commitment_events WHERE commitment_id = $1 "
                                "ORDER BY id DESC LIMIT 1", w.commitment)
    assert (event["actor"], event["action"]) == ("owner", "closed")
    assert (await press(conn, f"sh:br:s:{brief_id}"))["answer"] == "Открытых договорённостей с ним уже нет."
    # «Отложить на сутки»: даже через 5 часов справки нет
    with owner():
        await commitments.reopen(conn, w.commitment)
    assert (await press(conn, f"sh:br:z:{brief_id}"))["answer"].startswith("Справок по этому человеку не будет")
    later = at + timedelta(hours=5)
    mid = await incoming(conn, w.ivan_chat, IVAN, "Иван Петров", "Ещё вопрос", later, 11)
    assert await brief.on_message(conn, mid, tz=TZ, now=later) is None
    assert wrong == f"sh:br:w:{w.commitment}"


async def test_buttons_need_the_owner(conn):
    w = await world(conn)
    at = T0 + timedelta(hours=2)
    mid = await incoming(conn, w.ivan_chat, IVAN, "Иван Петров", "Добрый день", at, 10)
    brief_id = await brief.on_message(conn, mid, tz=TZ, now=at)
    out = await bridge.dispatch_callback(conn, f"sh:br:d:{w.commitment}", 999)     # не владелец
    assert out["answer"] == "Кнопка недоступна."
    assert await conn.fetchval("SELECT status FROM commitments WHERE id = $1", w.commitment) == "open"
    assert brief_id


# --- просрочка -------------------------------------------------------------------------------------------

async def test_overdue_signal_once_per_due_without_sums(conn):
    w = await world(conn)
    today = (T0 + timedelta(days=3)).date()           # срок 7 октября прошёл
    assert await brief.send_overdue(conn, today=today, label="t1") == 1
    signal, = [j for j in await sent(conn) if j["payload"]["text"].startswith("Срок прошёл")]
    text = signal["payload"]["text"]
    assert "Иван Петров" in text and "смет" not in text and "3 млн" not in text
    assert "возможно, сделано вне них" in text
    assert await brief.send_overdue(conn, today=today, label="t2") == 0            # второй раз — нет
    # перенесли срок, и он снова прошёл — сигнал снова возможен
    await conn.execute("UPDATE commitments SET due_date = $2 WHERE id = $1", w.commitment, today - timedelta(days=1))
    assert await brief.send_overdue(conn, today=today, label="t3") == 1
    shown = await press(conn, f"sh:br:o:{today:%Y%m%d}")
    assert WHAT in shown["reply"]["text"] and "подтверждение выполнения в читаемых чатах не найдено" in shown["reply"]["text"]


async def test_overdue_needs_acceptance_and_a_defined_date(conn):
    w = await world(conn, accept=False)
    today = (T0 + timedelta(days=3)).date()
    assert await brief.send_overdue(conn, today=today, label="t1") == 0             # не принято владельцем
    with owner():
        await commitments.accept(conn, w.commitment)
    for reason in ("vague", "immediate", "event", "ambiguous_period"):
        await conn.execute("UPDATE commitments SET due_reason = $2 WHERE id = $1", w.commitment, reason)
        assert await brief.send_overdue(conn, today=today, label=f"t-{reason}") == 0, reason
    await conn.execute("UPDATE commitments SET due_reason = 'ok' WHERE id = $1", w.commitment)
    assert await brief.send_overdue(conn, today=(T0 + timedelta(days=1)).date(), label="t-due-day") == 0  # день срока
    assert await brief.send_overdue(conn, today=today, label="t-ok") == 1


async def test_subject_merges_all_accounts_of_a_person(conn):
    w = await world(conn)
    second = await chat(conn, w.account, 2077, "Иван (второй номер)")
    ivan = await people.person_for_peer(conn, await peer_id(conn, IVAN))
    await conn.execute("INSERT INTO person_peers (person_id, peer_id) VALUES ($1, $2)",
                       ivan, await peer_id(conn, 2077))
    at = T0 + timedelta(hours=2)
    mid = await incoming(conn, second, 2077, "Иван", "Это мой второй номер", at, 70)
    assert await brief.on_message(conn, mid, tz=TZ, now=at) is not None


# --- после независимой проверки (С8, С9) ----------------------------------------------------------------

async def test_muted_and_forwarded_messages_do_not_bring_a_brief(conn):
    from shturman.notify import settings as ns
    w = await world(conn)
    at = T0 + timedelta(hours=2)
    forwarded = await incoming(conn, w.ivan_chat, IVAN, "Иван Петров", "Переслано", at, 10)
    await conn.execute("UPDATE messages SET is_forwarded = true WHERE id = $1", forwarded)
    assert await brief.on_message(conn, forwarded, tz=TZ, now=at) is None
    s = await ns.load(conn)
    await ns.save(conn, {**s, "muted_chats": [w.ivan_chat]})
    mid = await incoming(conn, w.ivan_chat, IVAN, "Иван Петров", "Добрый день", at, 11)
    assert await brief.on_message(conn, mid, tz=TZ, now=at) is None          # «Не сообщать об этом чате»


async def test_overdue_signal_returns_when_not_delivered(conn):
    w = await world(conn)
    today = (T0 + timedelta(days=3)).date()
    assert await brief.send_overdue(conn, today=today, label="t1") == 1
    job, = [j for j in await sent(conn) if j["payload"]["text"].startswith("Срок прошёл")]
    await bridge.deliver_failure(conn, job["id"], "бот недоступен", retry_in=None,
                                 executor=bridge.executor_for(bridge.NOTIFY_OWNER))
    assert await conn.fetchval("SELECT overdue_signaled_due FROM commitments WHERE id = $1", w.commitment) is None
    assert await brief.send_overdue(conn, today=today, label="t2") == 1


async def test_no_overdue_signal_when_stopped(conn):
    await world(conn)
    await conn.execute("UPDATE assistant_halt SET stopped = true WHERE id = 1")
    assert await brief.send_overdue(conn, today=(T0 + timedelta(days=3)).date(), label="t1") == 0
