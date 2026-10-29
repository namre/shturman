"""Связи страниц (пакет 0.0.17): человек относится к проекту, если писал в его чатах, участвует в его
договорённости или сказал о нём действующий факт. На странице проекта — его люди, на странице
человека — его проекты; в обе стороны по одним правилам и только по видимому."""

from datetime import date

from shturman.processing import facts, pages, pages_build, people, projects

from memory_helpers import owner
from pages_helpers import NOW, PETR, build, commitment, seed
from proc_helpers import chat, peer_id, say


async def add_fact(conn, project_id, message_id, text):
    return await conn.fetchval(
        """INSERT INTO facts (subject_type, project_id, kind, text, text_norm, valid_from, status, origin,
                              source_message_id, source_quote)
           VALUES ('project', $1, 'fact', $2, $3, $4, 'active', 'other', $5, $2) RETURNING id""",
        project_id, text, facts.text_norm(text), date(2026, 10, 6), message_id)


async def world(conn):
    w = await seed(conn)
    w.group = await chat(conn, w.account, -100555, "Стройка: Северный", type_="private_supergroup", cls="channel")
    await say(conn, w.group, [(PETR, "Пётр Смирнов", "Бригада на месте, начинаем разметку.")], first_id=300)
    w.petr = await people.ensure_person_for_peer(conn, await peer_id(conn, PETR))
    await people.confirm_person(conn, w.maria)
    with owner():
        w.project = (await projects.create_project(conn, "ЖК Северный", [w.group]))["project"]["id"]
        w.other = (await projects.create_project(conn, "ГК Альфа", kind="organization"))["project"]["id"]
    # Иван обещал смету по проекту в личном чате; Мария сказала о проекте факт
    w.estimate = await commitment(conn, w.ivan_chat, w.ivan_msgs[0], debtor=w.ivan_peer, creditor=w.owner_peer,
                                  direction="owed_to_owner", what="прислать смету по фасадам")
    await conn.execute("UPDATE commitments SET project_id = $2 WHERE id = $1", w.estimate, w.project)
    w.fact = await add_fact(conn, w.project, w.maria_msgs[0], "Акт сверки подпишут в понедельник")
    return w


async def test_project_people_come_from_chats_commitments_and_facts(conn):
    w = await world(conn)
    linked = {p["name"]: p["via"] for p in await projects.participants(conn, w.project)}
    assert linked == {"Пётр Смирнов": ["chat"], "Иван Петров": ["commitment"], "Мария Сидорова": ["fact"]}
    assert [p["name"] for p in await projects.participants(conn, w.project)][0] == "Пётр Смирнов"  # пишет в чатах
    full = await projects.get_project(conn, w.project)
    assert {p["person_id"] for p in full["people"]} == {w.petr, w.ivan, w.maria}
    assert (await projects.projects_of_person(conn, w.ivan)) == [
        {"project_id": w.project, "title": "ЖК Северный", "kind": "project", "status": "active", "via": ["commitment"]}]
    assert await projects.participants(conn, w.other) == []


async def test_links_follow_only_what_is_visible(conn):
    w = await world(conn)
    await conn.execute("UPDATE chats SET excluded = true WHERE id = $1", w.group)               # исключён чат
    await conn.execute("UPDATE messages SET agent_visible = false WHERE id = $1", w.maria_msgs[0])  # скрыто
    await conn.execute("UPDATE commitments SET status = 'rejected' WHERE id = $1", w.estimate)  # «не так»
    assert await projects.participants(conn, w.project) == []
    for person in (w.ivan, w.maria, w.petr):
        assert await projects.projects_of_person(conn, person) == []


async def test_person_page_lists_projects_and_project_page_its_people(conn, config):
    w = await world(conn)
    await pages_build.ensure_page(conn, w.ivan)            # страница подтверждённого человека
    await build(conn, config, now=NOW)
    person_path = await conn.fetchval("SELECT path FROM pages WHERE person_id = $1", w.ivan)
    project_path = await conn.fetchval("SELECT path FROM pages WHERE project_id = $1", w.project)
    person = pages.parse(pages.read_page(config.pages_dir, person_path))
    project = pages.parse(pages.read_page(config.pages_dir, project_path))
    assert person.projects == ["ЖК Северный"]
    assert set(project.participants) == {"Пётр Смирнов", "Иван Петров", "Мария Сидорова"}


def test_projects_line_round_trips_in_the_front_matter():
    page = pages.Page(entity_id="person:7", type="person", aliases=["Иван"], projects=["ЖК Северный", "ГК «Альфа»"],
                      updated="2026-10-06", title="Иван", owner="\n", commitments=pages.NO_COMMITMENTS)
    text = pages.render(page)
    assert '\nprojects: [ЖК Северный, "ГК «Альфа»"]\nupdated: 2026-10-06\n' in text
    assert pages.parse(text).projects == ["ЖК Северный", "ГК «Альфа»"]
    page.projects = []
    assert "projects:" not in pages.render(page)
