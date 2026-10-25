"""Шаг 7 записки «держать, подкладывать, сигналить»: справка перед встречей и сигнал «срок изменился».

Справка перед встречей — принятая договорённость о созвоне или встрече со временем: за `MEETING_AHEAD` до неё
владелец получает справку о человеке (имя, время и числа, без формулировок), одну на договорённость и её время.
Попытка переноса принятой договорённости — карточка «Похоже, срок перенесён»: действующий срок остаётся, пока
владелец не подтвердит."""

from datetime import date, datetime, timedelta, timezone

import pytest

from shturman import bridge
from shturman.brief import core as brief
from shturman.processing import commitments

from memory_helpers import notifications, owner, run_with
from proc_helpers import OWNER, T0, TZ, account, chat, say
from test_brief import IVAN, sent

CALL = "Созвонимся завтра в 15:00 по фасадам"


@pytest.fixture(autouse=True)
def own_bot():
    bridge.set_builtin({bridge.NOTIFY_OWNER, bridge.NOTIFY_EDIT})
    yield
    bridge.set_builtin(())


async def call_tomorrow(conn, *, what="созвониться по фасадам", accept=True):
    acc = await account(conn)
    await bridge.set_owner(conn, OWNER, OWNER)
    ivan_chat = await chat(conn, acc, IVAN, "Иван Петров")
    await say(conn, ivan_chat, [(IVAN, "Иван Петров", CALL)])
    await run_with(conn, lambda job: {"commitments": [{"message": 1, "source_quote": CALL, "what": what,
                                                       "due_expression": "завтра в 15:00"}]})
    cid = await conn.fetchval("SELECT id FROM commitments")
    if accept:
        with owner():
            assert (await commitments.accept(conn, cid))["ok"]
    await notifications(conn)
    await sent(conn)
    return cid


AT = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)      # 15:00 по Москве


async def meeting_notes(conn):
    return [j for j in await sent(conn) if j["payload"]["text"].startswith("В 15:00 — созвон")]


async def test_brief_before_a_call_once_per_time(conn):
    cid = await call_tomorrow(conn)
    assert await conn.fetchval("SELECT due_time::text FROM commitments WHERE id = $1", cid) == "15:00:00"
    # за два часа — рано; за 30 минут — справка
    assert await brief.send_before_meetings(conn, tz=TZ, now=AT - timedelta(hours=2)) == 0
    assert await brief.send_before_meetings(conn, tz=TZ, now=AT - timedelta(minutes=30)) == 1
    note, = await meeting_notes(conn)
    text = note["payload"]["text"]
    assert text.startswith("В 15:00 — созвон или встреча: Иван Петров.") and "Открытых договорённостей: 1" in text and "фасад" not in text
    assert [b["data"] for row in note["payload"]["buttons"] for b in row][0].startswith("sh:br:s:")
    # второй обход — ничего; встреча прошла — ничего
    assert await brief.send_before_meetings(conn, tz=TZ, now=AT - timedelta(minutes=20)) == 0
    assert await brief.send_before_meetings(conn, tz=TZ, now=AT + timedelta(minutes=5)) == 0
    # перенесли на 17:00 — справка снова возможна
    await conn.execute("UPDATE commitments SET due_time = '17:00' WHERE id = $1", cid)
    assert await brief.send_before_meetings(conn, tz=TZ, now=AT + timedelta(hours=1, minutes=40)) == 1


async def test_no_brief_for_unaccepted_non_meetings_quiet_hours_and_stop(conn):
    await call_tomorrow(conn, accept=False)
    assert await brief.send_before_meetings(conn, tz=TZ, now=AT - timedelta(minutes=30)) == 0
    await conn.execute("DELETE FROM commitments")
    await conn.execute("DELETE FROM messages")
    cid = await call_tomorrow(conn, what="прислать смету по фасадам")
    assert await brief.send_before_meetings(conn, tz=TZ, now=AT - timedelta(minutes=30)) == 0
    await conn.execute("UPDATE commitments SET what = 'встреча на объекте' WHERE id = $1", cid)
    await conn.execute("UPDATE assistant_halt SET stopped = true WHERE id = 1")
    assert await brief.send_before_meetings(conn, tz=TZ, now=AT - timedelta(minutes=30)) == 0
    await conn.execute("UPDATE assistant_halt SET stopped = false WHERE id = 1")
    assert await brief.send_before_meetings(conn, tz=TZ, now=AT - timedelta(minutes=30)) == 1


def test_meeting_words():
    for what in ("созвониться по фасадам", "встреча на объекте", "позвонить подрядчику", "провести обход с заказчиком",
                 "совещание по графику", "Звонок в 15:00"):
        assert brief.is_meeting({"what": what}), what
    for what in ("прислать смету", "оплатить счёт", "подписать договор"):
        assert not brief.is_meeting({"what": what}), what


async def test_old_briefs_are_pruned(conn):
    await call_tomorrow(conn)
    assert await brief.send_before_meetings(conn, tz=TZ, now=AT - timedelta(minutes=30)) == 1
    assert await brief.prune_briefs(conn, now=AT + brief.BRIEFS_KEEP + timedelta(minutes=1)) == 1


async def test_reschedule_attempt_card_says_the_current_due_stays(conn):
    cid = await call_tomorrow(conn, what="прислать смету по фасадам")
    item = await commitments.get_commitment(conn, cid, today=T0.date())
    change = {"kind": "rescheduled", "new_due_date": date(2026, 10, 9), "new_due_time": None,
              "evidence_quote": "Смету пришлю не завтра, а в пятницу"}
    text = commitments._render_change(change, item, T0.date())
    assert text.startswith("Похоже, срок перенесён на ")
    assert "\nПока вы не подтвердите, действует прежний. Срок: " in text
    # у ещё не принятой договорённости этой строки нет: прежнего действующего срока нет
    assert "действует прежний" not in commitments._render_change(change, {**item, "status": "proposed"}, T0.date())


async def test_contact_brief_in_a_group_read_by_the_helper_account_but_not_in_its_private_chats(conn):
    from shturman import store
    from test_brief import briefs_sent, incoming
    await call_tomorrow(conn, what="прислать смету по фасадам")
    helper = await store.ensure_account(conn, 7001, "Помощник", "assistant")
    group = await chat(conn, helper, 5001, "Стройка: Северный", type_="private_supergroup", cls="channel")
    private = await chat(conn, helper, IVAN, "Иван Петров")
    at = T0 + timedelta(hours=2)
    mid = await incoming(conn, private, IVAN, "Иван Петров", "Добрый день", at, 20)
    assert await brief.on_message(conn, mid, tz=TZ, now=at) is None
    mid = await incoming(conn, group, IVAN, "Иван Петров", "По фасадам вопрос", at, 21)
    assert await brief.on_message(conn, mid, tz=TZ, now=at) is not None
    assert len(await briefs_sent(conn)) == 1
