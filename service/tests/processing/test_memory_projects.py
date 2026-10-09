"""Проекты: заводит владелец или предлагает модель; обязательства, факты и решения проекта."""

from datetime import timedelta

import pytest

from shturman import bridge, confirm
from shturman.processing import commitments, facts, people, projects

from memory_helpers import NOW, buttons, fact, notifications, owner, press_button, run_with
from proc_helpers import OWNER, T0, account, chat, peer_id, say

IVAN, BRIGADE = 2001, 5001


class W:
    pass


async def seed(conn):
    w = W()
    w.account = await account(conn)
    await bridge.set_owner(conn, OWNER, OWNER)
    w.ivan_chat = await chat(conn, w.account, IVAN, "Иван Петров")
    w.group = await chat(conn, w.account, BRIGADE, "Стройка: Северный", type_="private_supergroup", cls="channel")
    w.ivan = await people.ensure_person_for_peer(conn, await peer_id(conn, IVAN))
    await people.confirm_person(conn, w.ivan)
    return w


def digest_texts(jobs, head):
    return [j for j in jobs if j["payload"]["text"].startswith(head)]


async def test_owner_creates_a_project(conn):
    w = await seed(conn)
    with pytest.raises(confirm.Refused) as refused:
        await projects.create_project(conn, "ЖК Северный", [w.group])
    assert refused.value.code == "owner_required"
    with owner():
        out = await projects.create_project(conn, " «ЖК Северный» ", [w.group, w.group], ["Северный", "северный", "ЖК С."],
                                            description="Дом на 120 квартир")
        project = out["project"]
        assert out["created"] is True and project["title"] == "ЖК Северный" and project["status"] == "active"
        assert project["aliases"] == ["Северный", "ЖК С"] and [c["id"] for c in project["chats"]] == [w.group]
        assert project["description"] == "Дом на 120 квартир" and project["page"]["entity_id"] == f"project:{project['id']}"
        decided = await conn.fetchrow("SELECT approved_by, approved_via, origin FROM projects WHERE id = $1", project["id"])
        assert tuple(decided) == (str(OWNER), "telegram", "owner")
        for bad, code in ((("жк северный", []), "exists"), (("Омега", [999999]), "bad_chat"),
                          (("!!!", []), "bad_request"), (("Очень длинное название " * 4, []), "bad_request")):
            with pytest.raises(projects.ProjectsError) as err:
                await projects.create_project(conn, *bad)
            assert err.value.code == code, bad
        await conn.execute("UPDATE chats SET excluded = true WHERE id = $1", w.ivan_chat)
        with pytest.raises(projects.ProjectsError):
            await projects.create_project(conn, "Омега", [w.ivan_chat])     # исключённый чат не годится
    assert [p["title"] for p in await projects.list_projects(conn)] == ["ЖК Северный"]
    assert await projects.list_projects(conn, "rejected") == []
    with pytest.raises(projects.ProjectsError):
        await projects.list_projects(conn, "всё")


async def test_chats_archive_and_commitments_of_a_project(conn):
    w = await seed(conn)
    msgs = await say(conn, w.ivan_chat, [(IVAN, "Иван Петров", "Пришлю смету по Северному к пятнице")])
    commitment_id = await conn.fetchval(
        """INSERT INTO commitments (chat_id, source_message_id, direction, what, source_quote)
           VALUES ($1, $2, 'owed_to_owner', 'прислать смету', 'Пришлю смету') RETURNING id""", w.ivan_chat, msgs[0])
    with owner():
        project_id = (await projects.create_project(conn, "ЖК Северный", [w.group]))["project"]["id"]
    with pytest.raises(confirm.Refused):
        await projects.set_project_chats(conn, project_id, [w.group, w.ivan_chat])
    with owner():
        out = await projects.set_project_chats(conn, project_id, [w.group, w.ivan_chat])
        assert out["added"] == [w.ivan_chat] and out["removed"] == []
        # обязательства из добавленного чата относятся к проекту
        assert await conn.fetchval("SELECT project_id FROM commitments WHERE id = $1", commitment_id) == project_id
        out = await projects.set_project_chats(conn, project_id, [w.group])
        assert out["removed"] == [w.ivan_chat]
        assert await conn.fetchval("SELECT project_id FROM commitments WHERE id = $1", commitment_id) is None
        out = await projects.archive_project(conn, project_id)
        assert out["project"]["status"] == "archived" and out["changed"] is True
        assert (await projects.archive_project(conn, project_id))["changed"] is False
    with pytest.raises(confirm.Refused):
        await projects.archive_project(conn, project_id)
    assert await projects.resolve(conn, "ЖК Северный") is None                      # в архиве — не для новых
    assert await projects.resolve(conn, "ЖК Северный", statuses=("archived",)) == project_id


async def test_project_proposed_by_mentions_in_two_episodes(conn):
    w = await seed(conn)
    await say(conn, w.ivan_chat, [
        (IVAN, "Иван Петров", "По ЖК Северный цена фасада 5 тыс за метр"),
        (IVAN, "Иван Петров", "И ещё по ЖК Северный: бюджет 12 млн"),
    ])
    reply = lambda job: {"commitments": [], "facts": [], "projects": ["ЖК Северный", "Проект Омега"]}  # noqa: E731
    out = await run_with(conn, reply)
    assert out["planned"] == 1
    assert await conn.fetchval("SELECT count(*) FROM project_mentions") == 2                # два сообщения
    assert await conn.fetchval("SELECT count(*) FROM projects") == 0                        # один эпизод — мало
    await say(conn, w.ivan_chat, [(IVAN, "Иван Петров", "Цена по ЖК Северный выросла на 10%")],
              start=T0 + timedelta(days=2), first_id=10)
    await run_with(conn, reply, now=T0 + timedelta(days=2, hours=1))
    project = await conn.fetchrow("SELECT * FROM projects")
    assert (project["title"], project["status"], project["origin"]) == ("ЖК Северный", "proposed", "model")
    reason = projects._loads(project["reason"])
    assert (reason["mentions"], reason["episodes"], reason["chats"]) == (3, 2, [w.ivan_chat])

    sent = digest_texts(await notifications(conn), "Проекты из переписки")
    assert len(sent) == 1 and "1. ЖК Северный — упоминаний за 30 дн.: 3 в 2 разговорах" in sent[0]["payload"]["text"]
    accept, reject = buttons(sent, "pj")
    assert (accept, reject) == (f"sh:pj:a:{project['id']}", f"sh:pj:r:{project['id']}")
    with pytest.raises(confirm.Refused):
        await projects.decide_project_proposal(conn, project["id"], True)
    done = await press_button(conn, accept)
    assert done["answer"] == "Проект заведён." and done["edit_text"] == "Проекты — решено:\n1. ЖК Северный — ✓ заведён"
    assert await conn.fetchval("SELECT status FROM projects WHERE id = $1", project["id"]) == "active"
    assert await conn.fetchval("SELECT entity_id FROM pages WHERE project_id = $1", project["id"]) == f"project:{project['id']}"
    assert (await press_button(conn, reject))["answer"] == "Уже решено."


async def test_rejected_project_is_never_proposed_again(conn):
    w = await seed(conn)
    await say(conn, w.group, [(IVAN, "Иван Петров", f"Сообщение бригады номер {n}", {"at": T0 + timedelta(minutes=n)})
                              for n in range(20)])
    await say(conn, w.group, [(IVAN, "Иван Петров", "ещё одно")], start=T0 - timedelta(days=40), first_id=100)
    out = await projects.propose(conn, now=NOW)
    assert out == {"projects_proposed": 1}
    project = await conn.fetchrow("SELECT * FROM projects")
    assert project["title"] == "Стройка: Северный" and projects._loads(project["reason"])["messages"] == 20
    assert await conn.fetchval("SELECT origin FROM project_chats WHERE project_id = $1", project["id"]) == "model"
    assert await projects.send_digest(conn, run_id=1) == 1
    out = await projects.decide_project_proposal(conn, project["id"], False)       # отказ — и без владельца
    assert out["status"] == "rejected"
    assert await projects.propose(conn, now=NOW) == {"projects_proposed": 0}
    # и упоминания того же названия его не возвращают
    for n in range(4):
        await conn.execute(
            """INSERT INTO project_mentions (title, title_norm, chat_id, message_id, episode)
               SELECT 'Стройка: Северный', $4, $1, id, $2 FROM messages WHERE chat_id = $1
               ORDER BY id LIMIT 1 OFFSET $3""", w.group, f"e{n}", n, projects.norm("«Стройка: Северный»"))
    assert await projects.propose(conn, now=NOW) == {"projects_proposed": 0}
    # а владелец может завести проект с тем же названием сам
    with owner():
        assert (await projects.create_project(conn, "Стройка: Северный"))["project"]["status"] == "active"


async def test_small_or_excluded_group_is_not_proposed(conn):
    w = await seed(conn)
    await say(conn, w.group, [(IVAN, "Иван Петров", f"сообщение {n}", {"at": T0 + timedelta(minutes=n)})
                              for n in range(19)])
    assert await projects.propose(conn, now=NOW) == {"projects_proposed": 0}
    await say(conn, w.group, [(IVAN, "Иван Петров", "двадцатое")], start=T0 + timedelta(minutes=30), first_id=50)
    await conn.execute("UPDATE messages SET agent_visible = false WHERE chat_id = $1 AND tg_message_id = 50", w.group)
    assert await projects.propose(conn, now=NOW) == {"projects_proposed": 0}       # скрытое не считается
    await conn.execute("UPDATE messages SET agent_visible = true WHERE chat_id = $1", w.group)
    await conn.execute("UPDATE chats SET excluded = true WHERE id = $1", w.group)
    assert await projects.propose(conn, now=NOW) == {"projects_proposed": 0}


async def test_extraction_assigns_projects_and_records_project_facts(conn):
    w = await seed(conn)
    with owner():
        project_id = (await projects.create_project(conn, "ЖК Северный", [w.group], ["Северный"]))["project"]["id"]
    await say(conn, w.ivan_chat, [
        (IVAN, "Иван Петров", "По Северному решили: фасад из керамогранита, цена 5 тыс за метр. Пришлю смету завтра."),
    ])
    group_msgs = await say(conn, w.group, [(IVAN, "Иван Петров", "Бригада выйдет в понедельник, сделаю разметку")],
                           first_id=200)
    reply_ivan = {"commitments": [{"message": 1, "source_quote": "Пришлю смету завтра", "what": "прислать смету",
                                   "due_expression": "завтра", "project": "Северный"}],
                  "facts": [fact(1, "фасад из керамогранита", "ПРОЕКТ", "фасад из керамогранита", kind="decision",
                                 project="ЖК Северный"),
                            fact(1, "цена 5 тыс за метр", "ПРОЕКТ", "цена фасада 5 тыс за метр", slot="цена",
                                 project="Северный"),
                            fact(1, "цена 5 тыс за метр", "ПРОЕКТ", "цена 5 тыс", slot="цена", project="Омега")],
                  "projects": ["Северному"]}
    reply_group = {"commitments": [{"message": 1, "source_quote": "сделаю разметку", "what": "сделать разметку"}]}
    await run_with(conn, lambda job: reply_ivan if "Северному" in job["payload"]["input"] else reply_group)
    rows = await conn.fetch("SELECT what, chat_id, project_id FROM commitments ORDER BY id")
    assert [(r["what"], r["project_id"]) for r in rows] == [("прислать смету", project_id),     # по метке модели
                                                             ("сделать разметку", project_id)]  # по чату проекта
    got = await conn.fetch("SELECT kind, slot, text, status FROM facts WHERE project_id = $1 ORDER BY id", project_id)
    assert [tuple(r) for r in got] == [("decision", None, "фасад из керамогранита", "active"),
                                       ("fact", "цена", "цена фасада 5 тыс за метр", "active")]
    assert await conn.fetchval("SELECT count(*) FROM facts") == 2                 # «Омега» — проекта нет
    assert group_msgs
    # проект в чатах указан как действующий: модель видит его название в запросе
    assert await projects.active_titles(conn) == ["ЖК Северный"]


async def test_exclusion_takes_the_chat_out_of_the_project(conn):
    w = await seed(conn)
    with owner():
        project_id = (await projects.create_project(conn, "ЖК Северный", [w.group, w.ivan_chat]))["project"]["id"]
    await conn.execute("UPDATE pages SET dirty = false")
    await conn.execute("UPDATE chats SET excluded = true WHERE id = $1", w.group)
    assert [c["id"] for c in (await projects.get_project(conn, project_id))["chats"]] == [w.ivan_chat]   # не видно сразу
    assert await projects.purge_orphans(conn) == 1
    assert await conn.fetchval("SELECT count(*) FROM project_chats WHERE chat_id = $1", w.group) == 0
    assert await conn.fetchval("SELECT dirty FROM pages WHERE project_id = $1", project_id) is True


async def test_pending_approvals_lists_everything_waiting(conn):
    w = await seed(conn)
    msgs = await say(conn, w.ivan_chat, [(IVAN, "Иван Петров", "Пришлю смету к пятнице"),
                                         (OWNER, "Евгений Тестов", "Я теперь в отпуске до 20 октября")])
    await conn.execute(
        """INSERT INTO commitments (chat_id, source_message_id, direction, what, source_quote, debtor_peer_id)
           VALUES ($1, $2, 'owed_to_owner', 'прислать смету', 'Пришлю смету', $3)""",
        w.ivan_chat, msgs[0], await peer_id(conn, IVAN))
    from shturman.processing import extract
    row = await conn.fetchrow("SELECT * FROM messages WHERE id = $1", msgs[1])
    msg = extract.Msg(id=row["id"], chat_id=row["chat_id"], sent_at=row["sent_at"], sender_peer_id=None,
                      sender_name=None, is_outgoing=True, text=row["text"])
    await facts.record(conn, extract.FactCandidate(msg, "в отпуске", "owner", None, None, None, "в отпуске до 20 октября",
                                                   "fact"), subject_type="owner")
    await projects.create_project(conn, "Омега", origin="model", reason={"mentions": 3, "episodes": 2})
    maria = await people.create_person(conn, "Мария Сидорова")
    await conn.execute("INSERT INTO page_proposals (person_id, reason) VALUES ($1, '{\"messages\": 25}')", maria)
    waiting = await projects.pending_approvals(conn)
    assert set(waiting) == {"projects", "owner_facts", "pages", "commitments"}
    assert [(p["title"], p["text"]) for p in waiting["projects"]] == [("Омега", "упоминаний за 30 дн.: 3 в 2 разговорах")]
    assert [(f["title"], f["text"]) for f in waiting["owner_facts"]] == [("о вас", "в отпуске до 20 октября")]
    assert waiting["owner_facts"][0]["fingerprint"] == facts.fingerprint(await conn.fetchrow("SELECT * FROM facts"))
    assert [(p["person_id"], p["title"], p["text"]) for p in waiting["pages"]] == [(maria, "Мария Сидорова", "messages: 25")]
    assert [(c["text"], c["title"]) for c in waiting["commitments"]] == [("прислать смету", "Иван Петров → вам")]
    assert commitments                                                       # модуль обязательств — источник
