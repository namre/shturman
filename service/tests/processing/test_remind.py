"""Напоминание исполнителю (`brief/remind.py`): кнопка «Напомнить» под договорённостью «вам обещали».

Главное: кнопка есть только у принятой открытой договорённости, обещанной владельцу, и только у людей из
круга настройки; текст собирает код по шаблону; «В чат» кладёт его в поле ввода личного чата основного
аккаунта и только по нажатию владельца; сервис ничего не отправляет; без личного чата — «Скопировать» и
подсказка, куда отправить (чат аккаунта помощника, группа)."""

import contextlib
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from shturman import authority, bridge, store
from shturman.brief import core as brief
from shturman.brief import remind
from shturman.processing import commitments

from memory_helpers import notifications, owner, run_with
from proc_helpers import OWNER, T0, TZ, account, chat, say

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outbox"))
from outbox_helpers import FakeTg  # noqa: E402

IVAN, PETR, BRIGADE, HELPER = 2001, 2002, 5001, 7001
WHAT = "прислать смету по фасадам"
SAID = "Пришлю смету по фасадам завтра"


@pytest.fixture(autouse=True)
def own_bot():
    bridge.set_builtin({bridge.NOTIFY_OWNER, bridge.NOTIFY_EDIT})
    yield
    bridge.set_builtin(())
    remind.set_state(None)


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    @contextlib.asynccontextmanager
    async def acquire(self):
        yield self.conn


def rig(conn, account_id):
    tg = FakeTg()
    tg.puttable.add(account_id)
    remind.set_state(SimpleNamespace(extras={"tg": tg}, pool=_Pool(conn)))
    return tg


async def tap(conn, data, *, action="telegram.callback"):
    """Нажатие владельца в его боте согласований (как его подтверждает бот)."""
    owner_ = await bridge.get_owner(conn)
    with authority.owner_context(OWNER, chat_id=owner_["chat_id"], action=action):
        return await bridge.dispatch_callback(conn, data, OWNER)


async def promise(conn, chat_id, sender, name, said=SAID, *, accept=True, what=WHAT, first_id=1):
    """Человек обещал владельцу в этом чате; владелец принял, если accept. Номер договорённости."""
    await say(conn, chat_id, [(sender, name, said)], first_id=first_id)
    await run_with(conn, lambda job: {"commitments": [{"message": 1, "source_quote": said, "what": what,
                                                       "due_expression": "завтра",
                                                       "recipient": "ВЛАДЕЛЕЦ"}]})
    cid = await conn.fetchval("SELECT max(id) FROM commitments")
    if accept:
        with owner():
            assert (await commitments.accept(conn, cid))["ok"]
    await notifications(conn)
    return cid


async def world(conn):
    w = SimpleNamespace()
    w.account = await account(conn)
    await bridge.set_owner(conn, OWNER, OWNER)
    w.ivan_chat = await chat(conn, w.account, IVAN, "Иван Петров")
    w.commitment = await promise(conn, w.ivan_chat, IVAN, "Иван Петров")
    return w


async def overdue_details(conn, today):
    return (await tap(conn, f"sh:br:o:{today:%Y%m%d}"))["reply"]


TODAY = (T0 + timedelta(days=3)).date()      # срок 7 октября прошёл


async def test_remind_button_builds_a_template_and_put_lays_it_into_the_field(conn):
    w = await world(conn)
    tg = rig(conn, w.account)
    shown = await overdue_details(conn, TODAY)
    labels = [b["text"] for b in shown["buttons"][0]]
    assert labels == ["1 — выполнено", "1 — не так", "1 — напомнить"]
    data = shown["buttons"][0][2]["data"]
    assert data == f"sh:br:n:{w.commitment}"
    card = (await tap(conn, data))["reply"]
    text = await conn.fetchval("SELECT text FROM reminders")
    assert text == f"Добрый день! Напоминаю: {WHAT} — срок был 07.10. Как с этим?"
    assert card["text"].startswith("Напоминание — Иван Петров:") and text in card["text"]
    assert "«В чат» положит текст в поле ввода вашего личного чата с ним" in card["text"]
    assert "Сервис ничего не отправляет" in card["text"]
    put, copy = card["buttons"]
    assert put[0]["text"] == "В чат" and copy == [{"text": "Скопировать", "copy": text}]
    # повторное «Напомнить» в течение суток — то же напоминание, без второй строки
    again = (await tap(conn, data))["reply"]
    assert again["buttons"][0][0]["data"] == put[0]["data"]
    assert await conn.fetchval("SELECT count(*) FROM reminders") == 1
    out = await tap(conn, put[0]["data"])
    assert out["answer"].startswith("Положил в поле ввода")
    assert tg.fields[(w.account, IVAN)] == text
    assert tg.sent == [] and tg.calls == 0                         # ничего не отправлено
    assert await conn.fetchval("SELECT placed_at IS NOT NULL FROM reminders")
    rid = await conn.fetchval("SELECT id FROM reminders")
    assert await conn.fetchval("SELECT detail FROM setup_audit WHERE action = 'remind.put'") == \
        f"напоминание № {rid}; итог: placed"
    assert WHAT not in await conn.fetchval("SELECT string_agg(detail, ' ') FROM setup_audit")
    # второй раз в поле не кладётся
    assert (await tap(conn, put[0]["data"]))["answer"] == "Этот текст уже лежит в поле ввода."
    assert len(tg.put_calls) == 1


async def test_put_only_from_the_owners_press_and_not_when_stopped(conn):
    w = await world(conn)
    tg = rig(conn, w.account)
    card = (await tap(conn, f"sh:br:n:{w.commitment}"))["reply"]
    put = card["buttons"][0][0]["data"]
    assert (await bridge.dispatch_callback(conn, put, 999))["answer"] == "Кнопка недоступна."      # не владелец
    assert (await tap(conn, put, action="test.callback"))["answer"] == "Кнопка недоступна."      # не нажатие в боте
    assert tg.put_calls == []
    await conn.execute("UPDATE assistant_halt SET stopped = true WHERE id = 1")
    assert "остановлен" in (await tap(conn, put))["answer"]
    assert (await tap(conn, f"sh:br:n:{w.commitment}"))["answer"] == "Ассистент остановлен."
    assert tg.put_calls == [] and tg.fields == {}


async def test_only_accepted_open_promises_to_the_owner_get_the_button(conn):
    w = await world(conn)
    petr = await chat(conn, w.account, PETR, "Пётр Сидоров")
    unconfirmed = await promise(conn, petr, PETR, "Пётр Сидоров", "Пришлю график работ завтра",
                                accept=False, what="прислать график работ", first_id=20)
    items = await brief.open_with(conn, [await conn.fetchval("SELECT id FROM peers WHERE tg_id = $1", PETR)],
                                  today=TODAY)
    assert [i["id"] for i in items] == [unconfirmed]
    assert await remind.offered(conn, items) == set()
    assert "Напомнить по этой договорённости нельзя" in (await tap(conn, f"sh:br:n:{unconfirmed}"))["answer"]
    with owner():
        await commitments.close(conn, w.commitment, actor="owner")
    assert "нельзя" in (await tap(conn, f"sh:br:n:{w.commitment}"))["answer"]
    assert await conn.fetchval("SELECT count(*) FROM reminders") == 0


async def test_chosen_people_and_groups_and_off(conn):
    w = await world(conn)
    group = await chat(conn, w.account, BRIGADE, "Стройка: Северный", type_="private_supergroup", cls="channel")
    petr_promise = await promise(conn, group, PETR, "Пётр Сидоров", "Пришлю график работ завтра",
                                 what="прислать график работ", first_id=30)
    both = [await commitments.get_commitment(conn, c, today=TODAY) for c in (w.commitment, petr_promise)]
    assert await remind.offered(conn, both) == {w.commitment, petr_promise}        # по умолчанию — всем
    await remind.save(conn, {"who": "chosen", "people": [IVAN]})
    assert await remind.offered(conn, both) == {w.commitment}
    await remind.save(conn, {"people": [], "groups": [group]})
    assert await remind.offered(conn, both) == {petr_promise}
    await remind.save(conn, {"enabled": False})
    assert await remind.offered(conn, both) == set()
    shown = await overdue_details(conn, TODAY)
    assert all(not b["text"].endswith("напомнить") for row in shown["buttons"] for b in row)
    with pytest.raises(ValueError):
        remind.validate({"who": "everyone"})
    with pytest.raises(ValueError):
        remind.validate({"people": ["Иван"]})
    assert remind.audit_detail(await remind.load(conn)) == "предлагать: нет; кому: выбранным; людей: 0; групп: 1"


async def test_group_promise_without_a_personal_chat_is_copied_with_the_name(conn):
    a = await account(conn)
    await bridge.set_owner(conn, OWNER, OWNER)
    group = await chat(conn, a, BRIGADE, "Стройка: Северный", type_="private_supergroup", cls="channel")
    cid = await promise(conn, group, PETR, "Пётр Сидоров", "Пришлю график работ завтра", what="прислать график работ")
    card = (await tap(conn, f"sh:br:n:{cid}"))["reply"]
    row = await conn.fetchrow("SELECT * FROM reminders")
    assert (row["route"], row["chat_id"]) == ("group", group)
    assert row["text"] == "Пётр, напоминаю: прислать график работ — срок был 07.10. Как с этим?"
    assert "Договорились в группе — «Стройка: Северный». Скопируйте текст" in card["text"]
    assert [b["text"] for r in card["buttons"] for b in r] == ["Скопировать"]
    out = await tap(conn, f"sh:br:p:{row['id']}:{row['nonce']}")                  # «В чат» нет вовсе
    assert out["answer"].startswith("Это напоминание уже недоступно")


async def test_helper_account_chat_is_named_and_copied(conn):
    a = await account(conn)
    await bridge.set_owner(conn, OWNER, OWNER)
    helper = await store.ensure_account(conn, HELPER, "Помощник", "assistant")
    group = await chat(conn, helper, BRIGADE, "Стройка: Северный", type_="private_supergroup", cls="channel")
    cid = await promise(conn, group, PETR, "Пётр Сидоров", "Пришлю график работ завтра", what="прислать график работ")
    card = (await tap(conn, f"sh:br:n:{cid}"))["reply"]
    assert await conn.fetchval("SELECT route FROM reminders") == "helper"
    assert "есть чат аккаунта помощника — «Стройка: Северный». Скопируйте текст и отправьте оттуда." in card["text"]
    # появился личный чат основного аккаунта с ним — следующее напоминание (через сутки) кладётся «В чат»
    await chat(conn, a, PETR, "Пётр Сидоров")
    await conn.execute("UPDATE reminders SET created_at = created_at - interval '25 hours'")
    assert await remind.prune(conn) == 1
    card = (await tap(conn, f"sh:br:n:{cid}"))["reply"]
    assert await conn.fetchval("SELECT route FROM reminders") == "put"
    assert card["buttons"][0][0]["text"] == "В чат"


async def test_cap_exclusion_and_owner_change_remove_reminders(conn):
    w = await world(conn)
    rig(conn, w.account)
    await conn.execute(
        "INSERT INTO reminders (commitment_id, chat_id, route, nonce, text) "
        "SELECT $1, $2, 'none', 'x', 't' FROM generate_series(1, $3)", w.commitment, w.ivan_chat, remind.DAILY_CAP)
    await conn.execute("UPDATE reminders SET created_at = now() - interval '23 hours'")
    # по этой договорённости напоминание за сутки уже есть — оно и показывается; новая — упирается в предел
    petr = await chat(conn, w.account, PETR, "Пётр Сидоров")
    other = await promise(conn, petr, PETR, "Пётр Сидоров", "Пришлю график работ завтра",
                          what="прислать график работ", first_id=40)
    assert (await tap(conn, f"sh:br:n:{other}"))["answer"] == f"Напоминаний — не больше {remind.DAILY_CAP} в сутки. Попробуйте позже."
    assert await remind.drop_chat(conn, w.ivan_chat) == remind.DAILY_CAP
    await tap(conn, f"sh:br:n:{other}")
    assert await conn.fetchval("SELECT count(*) FROM reminders") == 1
    await bridge.set_owner(conn, OWNER + 1, OWNER + 1)
    assert await conn.fetchval("SELECT count(*) FROM reminders") == 0


async def test_put_counts_toward_the_hourly_cap_of_the_input_field(conn):
    w = await world(conn)
    rig(conn, w.account)
    from shturman.replies import put as put_rules
    await conn.execute(
        "INSERT INTO reminders (commitment_id, chat_id, route, nonce, text, put_started_at) "
        "SELECT $1, $2, 'put', 'x', 't', now() FROM generate_series(1, $3)", w.commitment, w.ivan_chat,
        put_rules.HOURLY_CAP)
    from shturman.outbox import policy
    assert await put_rules.check(conn, await policy.target(conn, w.ivan_chat), "текст") == "cap"
