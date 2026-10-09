"""Датированные факты: запись из ответа модели, смена по ключу, профиль владельца с одобрением."""

from datetime import date, timedelta

import pytest

from shturman import authority, bridge, confirm, jobs
from shturman.processing import facts, pages, pages_build, people, pipeline

from memory_helpers import NOW, buttons, fact, notifications, owner, plan, press_button, run_with
from proc_helpers import OWNER, T0, TZ, account, chat, peer_id, say

IVAN, MARIA = 2001, 2002


class W:
    pass


async def seed(conn):
    w = W()
    w.account = await account(conn)
    await bridge.set_owner(conn, OWNER, OWNER)
    w.ivan_chat = await chat(conn, w.account, IVAN, "Иван Петров")
    w.maria_chat = await chat(conn, w.account, MARIA, "Мария Сидорова")
    w.ivan_msgs = await say(conn, w.ivan_chat, [
        (IVAN, "Иван Петров", "Я теперь директор по развитию в «Альфе»."),
        (OWNER, "Евгений Тестов", "Поздравляю! Я теперь работаю из офиса на Ленина."),
    ])
    w.maria_msgs = await say(conn, w.maria_chat, [(MARIA, "Мария Сидорова", "Мой новый номер +7 999 000-11-22")])
    w.ivan = await people.ensure_person_for_peer(conn, await peer_id(conn, IVAN))
    w.maria = await people.ensure_person_for_peer(conn, await peer_id(conn, MARIA))
    await people.confirm_person(conn, w.ivan)
    return w


def first_answer(job):
    text = job["payload"]["input"]
    if "Иван Петров" in text:
        return {"commitments": [], "facts": [
            fact(1, "Я теперь директор по развитию в «Альфе»", "У1", "директор по развитию в «Альфе»",
                 slot="должность"),
            fact(2, "Я теперь работаю из офиса на Ленина", "ВЛАДЕЛЕЦ", "работает из офиса на Ленина",
                 slot="адрес"),
        ], "projects": []}
    return {"commitments": [], "facts": [
        fact(1, "Мой новый номер +7 999 000-11-22", "У1", "+7 999 000-11-22", slot="телефон")]}


async def facts_of(conn):
    return [dict(r) for r in await conn.fetch("SELECT * FROM facts ORDER BY id")]


async def test_extraction_writes_person_facts_and_proposes_owner_facts(conn):
    w = await seed(conn)
    out = await run_with(conn, first_answer)
    assert out["planned"] == 2
    rows = await facts_of(conn)
    assert [(r["subject_type"], r["person_id"], r["slot"], r["status"], r["origin"], r["source_message_id"])
            for r in rows] == [
        ("person", w.ivan, "должность", "active", "other", w.ivan_msgs[0]),
        ("owner", None, "адрес", "proposed", "owner", w.ivan_msgs[1]),
    ]                                             # о неподтверждённой Марии факты не пишутся
    assert rows[0]["valid_from"] == date(2026, 10, 6) and rows[0]["valid_to"] is None
    stats = pipeline._loads(await conn.fetchval("SELECT stats FROM processing_runs ORDER BY id DESC LIMIT 1"))
    assert stats["results"]["facts_recorded"] == 1 and stats["results"]["owner_facts_proposed"] == 1
    assert stats["results"]["facts_without_subject"] == 1 and stats["owner_facts_shown"] == 1

    # профиль меняется только с одобрения: сообщение с кнопками и отпечатком содержания
    sent = await notifications(conn)
    digest = [j for j in sent if "Профиль: запомнить" in j["payload"]["text"]]
    assert len(digest) == 1 and "Профиль: запомнить это о вас?" in digest[0]["payload"]["text"]
    assert "адрес: работает из офиса на Ленина (с 2026-10-06; сказали вы)" in digest[0]["payload"]["text"]
    accept, reject = buttons(digest, "of")
    fp = facts.fingerprint(await conn.fetchrow("SELECT * FROM facts WHERE subject_type = 'owner'"))
    assert accept == f"sh:of:a:{rows[1]['id']}:{fp[:24]}" and reject == f"sh:of:r:{rows[1]['id']}"
    assert await conn.fetchval("SELECT count(*) FROM pages WHERE entity_type = 'owner'") == 0

    # подменённая кнопка (другой отпечаток) не принимает факт
    stale = await press_button(conn, f"sh:of:a:{rows[1]['id']}:{'0' * 24}")
    assert stale["answer"].startswith("Карточка устарела")
    done = await press_button(conn, accept)
    assert done["answer"] == "Запомнено." and done["remove_buttons"] is True
    assert done["edit_text"].startswith("Профиль — решено:\n1. работает из офиса на Ленина — ✓ запомнено")
    row = await conn.fetchrow("SELECT * FROM facts WHERE id = $1", rows[1]["id"])
    assert row["status"] == "active" and facts.approved(row) and row["approved_via"] == "telegram"
    assert (await press_button(conn, accept))["answer"].startswith("Уже решено")
    # одобренный факт заводит страницу профиля
    assert await conn.fetchval("SELECT entity_id FROM pages WHERE entity_type = 'owner'") == pages.OWNER_ENTITY
    assert [f["text"] for f in await facts.list_facts(conn, "owner")] == ["работает из офиса на Ленина"]


async def test_owner_fact_needs_the_verified_owner(conn):
    w = await seed(conn)
    await run_with(conn, first_answer)
    fact_id = await conn.fetchval("SELECT id FROM facts WHERE subject_type = 'owner'")
    with pytest.raises(confirm.Refused) as refused:
        await facts.decide_owner_fact(conn, fact_id, True)
    assert refused.value.status == 403 and refused.value.code == "owner_required"
    with owner():
        with pytest.raises(facts.FactsError) as changed:
            await facts.decide_owner_fact(conn, fact_id, True, expected_fingerprint="0" * 64)
        assert changed.value.code == "changed_meanwhile"
    # отказаться можно и без владельца: это ничего не добавляет
    out = await facts.decide_owner_fact(conn, fact_id, False)
    assert out["status"] == "rejected" and out["changed"] is True
    # отклонённое не предлагается снова, даже из нового сообщения с тем же текстом
    later = await say(conn, w.ivan_chat, [(OWNER, "Евгений Тестов", "Я теперь работаю из офиса на Ленина, напоминаю.")],
                      start=T0 + timedelta(days=1), first_id=10)
    await run_with(conn, lambda job: {"commitments": [], "facts": [
        fact(1, "Я теперь работаю из офиса на Ленина", "ВЛАДЕЛЕЦ", "работает из офиса на Ленина", slot="адрес")]},
        now=NOW + timedelta(days=1))
    assert await conn.fetchval("SELECT count(*) FROM facts WHERE subject_type = 'owner'") == 1
    assert later


async def test_new_value_supersedes_the_old_and_order_is_by_date(conn):
    w = await seed(conn)
    await run_with(conn, first_answer)
    first = await conn.fetchval("SELECT id FROM facts WHERE person_id = $1", w.ivan)
    msgs = await say(conn, w.ivan_chat, [
        (IVAN, "Иван Петров", "Новости: я теперь коммерческий директор в «Альфе»."),
    ], start=T0 + timedelta(days=3), first_id=20)
    await run_with(conn, lambda job: {"commitments": [], "facts": [
        fact(1, "я теперь коммерческий директор в «Альфе»", "У1", "коммерческий директор в «Альфе»",
             slot="должность")]}, now=T0 + timedelta(days=3, hours=1))
    rows = await conn.fetch("SELECT * FROM facts WHERE person_id = $1 ORDER BY id", w.ivan)
    old, new = rows
    assert (old["valid_to"], old["superseded_by"]) == (date(2026, 10, 9), new["id"])     # противоречие помечено
    assert (new["valid_to"], new["superseded_by"], new["source_message_id"]) == (None, None, msgs[0])
    assert [f["text"] for f in await facts.list_facts(conn, "person", w.ivan)] == ["коммерческий директор в «Альфе»"]
    closed = await facts.list_facts(conn, "person", w.ivan, include_closed=True)
    assert [(f["id"], f["current"], f["valid_to"]) for f in closed] == [
        (first, False, "2026-10-09"), (new["id"], True, None)]

    # тот же текст ещё раз — дубль, не пишется
    await say(conn, w.ivan_chat, [(IVAN, "Иван Петров", "Повторю: я теперь коммерческий директор в «Альфе».")],
              start=T0 + timedelta(days=4), first_id=30)
    await run_with(conn, lambda job: {"commitments": [], "facts": [
        fact(1, "я теперь коммерческий директор в «Альфе»", "У1", "Коммерческий директор в «Альфе».",
             slot="должность")]}, now=T0 + timedelta(days=4, hours=1))
    assert await conn.fetchval("SELECT count(*) FROM facts WHERE person_id = $1", w.ivan) == 2

    # сообщение из прошлого, разобранное позже (поздняя расшифровка), встаёт в цепочку по дате
    past = await say(conn, w.ivan_chat, [(IVAN, "Иван Петров", "Я теперь заместитель директора в «Альфе».")],
                     start=T0 + timedelta(days=1), first_id=40)
    candidate = (await _candidates(conn, past[0], "заместитель директора в «Альфе»", "должность"))[0]
    fact_id, outcome = await facts.record(conn, candidate, subject_type="person", person_id=w.ivan, tz=TZ)
    assert outcome == "active"
    chain = await conn.fetch(
        "SELECT id, valid_from, valid_to, superseded_by FROM facts WHERE person_id = $1 ORDER BY valid_from", w.ivan)
    assert [(r["id"], r["valid_to"], r["superseded_by"]) for r in chain] == [
        (first, date(2026, 10, 7), fact_id), (fact_id, date(2026, 10, 9), new["id"]), (new["id"], None, None)]


async def _candidates(conn, message_id, text, slot):
    from shturman.processing import extract
    row = await conn.fetchrow("SELECT * FROM messages WHERE id = $1", message_id)
    msg = extract.Msg(id=row["id"], chat_id=row["chat_id"], sent_at=row["sent_at"],
                      sender_peer_id=row["sender_peer_id"], sender_name=row["sender_name"],
                      is_outgoing=row["is_outgoing"], text=row["text"])
    return [extract.FactCandidate(message=msg, quote=row["text"][:20], about="peer", speaker_key=None,
                                  project=None, slot=slot, text=text, kind="fact")]


async def test_retract_by_the_owner_restores_the_previous_value(conn):
    w = await seed(conn)
    await run_with(conn, first_answer)
    await say(conn, w.ivan_chat, [(IVAN, "Иван Петров", "Я теперь коммерческий директор.")],
              start=T0 + timedelta(days=3), first_id=20)
    await run_with(conn, lambda job: {"commitments": [], "facts": [
        fact(1, "Я теперь коммерческий директор", "У1", "коммерческий директор", slot="должность")]},
        now=T0 + timedelta(days=3, hours=1))
    old, new = [r["id"] for r in await conn.fetch("SELECT id FROM facts WHERE person_id = $1 ORDER BY id", w.ivan)]
    with pytest.raises(confirm.Refused):
        await facts.retract_fact(conn, new)                 # ассистент сам не отменяет факт
    with owner():
        out = await facts.retract_fact(conn, new)
        assert out["changed"] is True and out["fact"]["status"] == "retracted"
        assert (await facts.retract_fact(conn, new))["changed"] is False
    current = await facts.list_facts(conn, "person", w.ivan)
    assert [(f["id"], f["current"]) for f in current] == [(old, True)]
    assert await conn.fetchval("SELECT dirty FROM pages WHERE person_id = $1", w.ivan) in (True, None)


async def test_deleted_or_excluded_source_takes_the_fact_away(conn):
    w = await seed(conn)
    await run_with(conn, first_answer)
    await say(conn, w.ivan_chat, [(IVAN, "Иван Петров", "Я теперь коммерческий директор.")],
              start=T0 + timedelta(days=3), first_id=20)
    await run_with(conn, lambda job: {"commitments": [], "facts": [
        fact(1, "Я теперь коммерческий директор", "У1", "коммерческий директор", slot="должность")]},
        now=T0 + timedelta(days=3, hours=1))
    old, new = await conn.fetch("SELECT id, source_message_id FROM facts WHERE person_id = $1 ORDER BY id", w.ivan)

    # скрытое защитой от внедрённых инструкций — не видно, но и не стёрто
    await conn.execute("UPDATE messages SET agent_visible = false WHERE id = $1", new["source_message_id"])
    assert await facts.list_facts(conn, "person", w.ivan) == []          # прежний закрыт датой, новый скрыт
    assert await facts.get_fact(conn, new["id"]) is None
    await conn.execute("UPDATE messages SET agent_visible = true WHERE id = $1", new["source_message_id"])

    # удалено сообщение — факт уходит, прежний снова действует
    await conn.execute("UPDATE messages SET deleted_at = now() WHERE id = $1", new["source_message_id"])
    assert await facts.purge_for_messages(conn, [new["source_message_id"]]) == 1
    assert [(f["id"], f["current"]) for f in await facts.list_facts(conn, "person", w.ivan)] == [(old["id"], True)]

    # чат исключён — уходят все его факты (обход на случай пропущенного события)
    await conn.execute("UPDATE chats SET excluded = true WHERE id = $1", w.ivan_chat)
    assert await facts.purge_orphans(conn) == 2       # факт Ивана и предложенный факт владельца
    assert await facts_of(conn) == []


async def test_undelivered_profile_message_is_resent_a_limited_number_of_times(conn):
    await seed(conn)
    await run_with(conn, first_answer)
    for attempt in range(1, facts.DIGEST_MAX_SENDS + 1):
        job = [j for j in await notifications(conn) if "Профиль: запомнить" in j["payload"]["text"]]
        assert len(job) == 1, attempt
        assert await bridge.deliver_failure(conn, job[0]["id"], "chat not found", retry_in=None) == "failed"
        row = await conn.fetchrow("SELECT batch, digest_attempts FROM facts WHERE subject_type = 'owner'")
        assert row["digest_attempts"] == attempt
        assert (row["batch"] is None) is (attempt < facts.DIGEST_MAX_SENDS)
        await pipeline.finish_run(conn, await conn.fetchval(
            "INSERT INTO processing_runs (trigger) VALUES ('manual') RETURNING id"))
    assert [j for j in await notifications(conn) if "Профиль: запомнить" in j["payload"]["text"]] == []
    # в перечне «ждёт решения» он остаётся
    from shturman.processing import projects
    assert len((await projects.pending_approvals(conn))["owner_facts"]) == 1


async def test_merging_people_moves_their_facts(conn):
    w = await seed(conn)
    await run_with(conn, first_answer)
    await people.confirm_person(conn, w.maria)
    await say(conn, w.maria_chat, [(MARIA, "Мария Сидорова", "Я теперь главный бухгалтер.")],
              start=T0 + timedelta(days=1), first_id=10)
    await run_with(conn, lambda job: {"commitments": [], "facts": [
        fact(1, "Я теперь главный бухгалтер", "У1", "главный бухгалтер", slot="должность")]},
        now=T0 + timedelta(days=1, hours=1))
    maria_fact = await conn.fetchval("SELECT id FROM facts WHERE person_id = $1", w.maria)
    with owner():
        await people.merge_people(conn, w.maria, w.ivan)
    rows = await conn.fetch("SELECT id, valid_to FROM facts WHERE person_id = $1 ORDER BY valid_from, id", w.ivan)
    assert [r["id"] for r in rows][-1] == maria_fact and rows[0]["valid_to"] == date(2026, 10, 7)
    assert await facts.list_facts(conn, "person", w.maria) != []        # влитая запись ведёт к новой


async def test_old_answer_without_facts_still_works(conn):
    await seed(conn)
    await run_with(conn, lambda job: {"commitments": []})
    assert await facts_of(conn) == []
    assert await conn.fetchval("SELECT count(*) FROM processing_requests WHERE state = 'done'") == 2
    assert jobs  # модуль очереди используется подставным исполнителем
