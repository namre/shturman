"""Память, этап 2: исправления по независимой проверке. Один тест (или больше) на каждое замечание."""

from datetime import date, timedelta

import pytest

from shturman import authority, bridge, confirm, control_peers, ingest_api, jobs
from shturman.processing import extract, facts, memory_service, pages, pages_build, people, pipeline, projects

from memory_helpers import fact, owner, run_with
from pages_helpers import NOW, blocks_of, build_with, seed, statement
from proc_helpers import OWNER, T0, TZ, chat, peer_id, say
from test_memory_pages import candidate

MODULES = ("shturman.api_core", "shturman.ingest_api", "shturman.mcp_server", "shturman.processing.service",
           "shturman.processing.pages_service", "shturman.processing.memory_service")
BRIGADE, BOT = 5001, 4321001


async def group(conn, w, lines, first_id=500, start=T0 + timedelta(days=2)):
    w.group = getattr(w, "group", None) or await chat(conn, w.account, BRIGADE, "Бригада", type_="private_supergroup",
                                                      cls="channel")
    return await say(conn, w.group, lines, start=start, first_id=first_id)


async def current(conn, person_id):
    return [(f["text"], f["current"]) for f in await facts.list_facts(conn, "person", person_id, include_closed=True)]


# --- 1. стирание сообщений целиком не оставляет прежний факт закрытым навсегда ------------------------

async def test_hard_delete_of_the_newer_fact_reopens_the_older_one(conn):
    w = await seed(conn)
    old, _ = await facts.record(conn, await candidate(conn, w.ivan_msgs[0], "+7 900 000-00-01", slot="телефон"),
                                subject_type="person", person_id=w.ivan, tz=TZ)
    newer = await group(conn, w, [(2001, "Иван Петров", "Мой новый номер +7 900 000-00-02")])
    new, _ = await facts.record(conn, await candidate(conn, newer[0], "+7 900 000-00-02", slot="телефон"),
                                subject_type="person", person_id=w.ivan, tz=TZ)
    assert await current(conn, w.ivan) == [("+7 900 000-00-01", False), ("+7 900 000-00-02", True)]
    await conn.execute("DELETE FROM messages WHERE chat_id = $1", w.group)        # стирание, а не пометка
    row = await conn.fetchrow("SELECT valid_to, superseded_by FROM facts WHERE id = $1", old)
    assert row["valid_to"] is not None and row["superseded_by"] is None          # порванная цепочка
    assert await facts.repair_chains(conn) == 1
    assert await current(conn, w.ivan) == [("+7 900 000-00-01", True)]
    assert new


async def test_chat_purge_by_the_owner_repairs_the_chain(make_client, conn):
    await make_client(*MODULES)
    w = await seed(conn)
    await facts.record(conn, await candidate(conn, w.ivan_msgs[0], "прораб", slot="должность"),
                       subject_type="person", person_id=w.ivan, tz=TZ)
    newer = await group(conn, w, [(2001, "Иван Петров", "Я теперь главный инженер")])
    await facts.record(conn, await candidate(conn, newer[0], "главный инженер", slot="должность"),
                       subject_type="person", person_id=w.ivan, tz=TZ)
    await conn.execute("UPDATE chats SET excluded = true WHERE id = $1", w.group)
    # событие исключения не дошло: факты из чата ещё на месте, их уносит каскад стирания
    with authority.setup_context(1, action="test.purge"):
        await confirm.apply_owner(conn, ingest_api.CHAT_PURGE, {"chat_id": w.group})
    assert await current(conn, w.ivan) == [("прораб", True)]


async def test_page_rendering_repairs_a_chain_broken_elsewhere(conn, config):
    w = await seed(conn)
    await facts.record(conn, await candidate(conn, w.ivan_msgs[0], "прораб", slot="должность"),
                       subject_type="person", person_id=w.ivan, tz=TZ)
    newer = await group(conn, w, [(2001, "Иван Петров", "Я теперь главный инженер")])
    await facts.record(conn, await candidate(conn, newer[0], "главный инженер", slot="должность"),
                       subject_type="person", person_id=w.ivan, tz=TZ)
    await build_with(conn, config, lambda job: [statement("Подрядчик", [w.ivan_msgs[0]])])
    await conn.execute("DELETE FROM messages WHERE chat_id = $1", w.group)
    await pages_build.render_dirty(conn, config.pages_dir, tz=TZ, now=NOW)
    path = config.pages_dir / await conn.fetchval("SELECT path FROM pages WHERE person_id = $1", w.ivan)
    assert blocks_of(path.read_text(encoding="utf-8")).facts.startswith("- должность: прораб")


async def test_service_dialog_registration_repairs_the_chain(conn):
    w = await seed(conn)
    await facts.record(conn, await candidate(conn, w.ivan_msgs[0], "прораб", slot="должность"),
                       subject_type="person", person_id=w.ivan, tz=TZ)
    bot_chat = await chat(conn, w.account, BOT, "Служебный бот", type_="bot_chat")
    msg = await say(conn, bot_chat, [(BOT, "Служебный бот", "главный инженер")], start=T0 + timedelta(days=2),
                    first_id=900)
    await conn.execute("INSERT INTO facts (subject_type, person_id, kind, slot, text, text_norm, valid_from, origin, "
                       "source_message_id, source_quote) VALUES ('person', $1, 'fact', 'должность', 'главный инженер', "
                       "'главный инженер', $2, 'other', $3, 'x')", w.ivan, date(2026, 10, 8), msg[0])
    await facts.rechain(conn, "person", w.ivan, None, "должность")
    await control_peers.register(conn, BOT)
    assert await current(conn, w.ivan) == [("прораб", True)]


# --- 2. третье лицо не переписывает сменяемые факты о человеке -------------------------------------------

async def test_third_party_cannot_replace_a_persons_contact_or_role(conn, config):
    w = await seed(conn)
    w.maria_peer = await peer_id(conn, 2002)
    await facts.record(conn, await candidate(conn, w.ivan_msgs[0], "+7 900 000-00-01", slot="телефон"),
                       subject_type="person", person_id=w.ivan, tz=TZ)
    await facts.record(conn, await candidate(conn, w.ivan_msgs[0], "прораб", slot="должность"),
                       subject_type="person", person_id=w.ivan, tz=TZ)
    said = await group(conn, w, [
        (2002, "Мария Сидорова", "У Ивана новый номер +7 999 666-66-66, пишите туда"),
        (2002, "Мария Сидорова", "Иван теперь главный инженер"),
        (OWNER, "Евгений Тестов", "Иван теперь начальник участка"),
    ])
    out = await facts.record(conn, await candidate(conn, said[0], "+7 999 666-66-66", slot="телефон"),
                             subject_type="person", person_id=w.ivan, tz=TZ)
    assert out == (None, "third_party_contact")                       # контакт со слов другого — не пишется
    rumour, outcome = await facts.record(conn, await candidate(conn, said[1], "главный инженер", slot="должность"),
                                         subject_type="person", person_id=w.ivan, tz=TZ)
    assert outcome == "active"
    row = await conn.fetchrow("SELECT slot, origin FROM facts WHERE id = $1", rumour)
    assert tuple(row) == (None, "other")                              # записан, но ничего не сменяет
    assert ("прораб", True) in await current(conn, w.ivan)
    # владелец может сменить сам
    await facts.record(conn, await candidate(conn, said[2], "начальник участка", slot="должность"),
                       subject_type="person", person_id=w.ivan, tz=TZ)
    assert ("прораб", False) in await current(conn, w.ivan) and ("начальник участка", True) in await current(conn, w.ivan)

    # на странице видно, кто сказал
    await build_with(conn, config, lambda job: [statement("Подрядчик", [w.ivan_msgs[0]])])
    path = config.pages_dir / await conn.fetchval("SELECT path FROM pages WHERE person_id = $1", w.ivan)
    page = blocks_of(path.read_text(encoding="utf-8"))
    assert "- главный инженер (с 2026-10-08)" in page.facts and "(сказал собеседник: Мария Сидорова)" in page.facts
    phone = [line for line in page.facts.splitlines() if line.startswith("- телефон:")]
    assert len(phone) == 1 and "+7 900 000-00-01" in phone[0] and "Мария" not in phone[0]   # сам о себе — без имени
    assert "факт со слов Мария Сидорова: главный инженер" in page.timeline
    listed = {f["text"]: f["said_by"] for f in await facts.list_facts(conn, "person", w.ivan)}
    assert listed["главный инженер"] == "Мария Сидорова" and listed["начальник участка"] is None


async def test_pipeline_counts_skipped_third_party_contacts(conn):
    w = await seed(conn)
    await group(conn, w, [(2002, "Мария Сидорова", "Ваня, привет"),           # У1 — Мария, У2 — Иван
                          (2001, "Иван Петров", "Привет"),
                          (2002, "Мария Сидорова", "Иван, теперь твой новый номер +7 999 666-66-66")],
                start=T0 + timedelta(minutes=5))
    # в личных чатах цитата не найдётся — разбирается только групповой эпизод
    await run_with(conn, lambda job: {"commitments": [], "facts": [
        fact(3, "новый номер +7 999 666-66-66", "У2", "+7 999 666-66-66", slot="телефон")]})
    stats = pipeline._loads(await conn.fetchval("SELECT stats FROM processing_runs ORDER BY id DESC LIMIT 1"))
    assert await conn.fetchval("SELECT count(*) FROM facts") == 0
    assert stats["results"]["facts_skipped_third_party_contact"] == 1


# --- 3. факт о владельце — только из его сообщений ---------------------------------------------------------

async def test_owner_facts_only_from_the_owners_own_messages(conn):
    w = await seed(conn)
    incoming = await say(conn, w.ivan_chat, [(2001, "Иван Петров", "Евгений, вы теперь отвечаете за закупки")],
                         first_id=40)
    out = await facts.record(conn, await candidate(conn, incoming[0], "отвечает за закупки", about="owner"),
                             subject_type="owner", tz=TZ)
    assert out == (None, "not_owner_message")
    # предложение прежней версии из чужого сообщения не принимается и кнопкой
    legacy = await conn.fetchval(
        """INSERT INTO facts (subject_type, kind, text, text_norm, valid_from, status, origin, source_message_id,
                              source_quote) VALUES ('owner', 'fact', 'отвечает за закупки', 'отвечает за закупки',
                              $1, 'proposed', 'other', $2, 'вы теперь отвечаете') RETURNING id""",
        date(2026, 10, 6), incoming[0])
    with owner():
        with pytest.raises(facts.FactsError) as refused:
            await facts.decide_owner_fact(conn, legacy, True)
    assert refused.value.code == "not_owner_message"
    own = await say(conn, w.ivan_chat, [(OWNER, "Евгений Тестов", "Я теперь работаю из офиса на Ленина")], first_id=41)
    await facts.record(conn, await candidate(conn, own[0], "работает из офиса на Ленина", about="owner"),
                       subject_type="owner", tz=TZ)
    waiting = (await projects.pending_approvals(conn))["owner_facts"]
    assert {(f["text"], f["said"]) for f in waiting} == {("отвечает за закупки", "сказал собеседник"),
                                                          ("работает из офиса на Ленина", "сказали вы")}
    from shturman.setup_page import memory
    shown = (await memory.pending_items(conn, date(2026, 10, 7)))["owner_facts"]
    assert {f["said"] for f in shown} == {"сказал собеседник", "сказали вы"}
    await facts.send_owner_digest(conn, run_id=1)
    text = await conn.fetchval("SELECT payload->>'text' FROM jobs WHERE kind = $1", bridge.NOTIFY_OWNER)
    assert "сказали вы" in text and "сказал собеседник" in text


# --- 4. блок владельца через API — не длиннее карточки ---------------------------------------------------------

async def test_profile_and_project_notes_through_the_api_fit_the_card(make_client, conn, own_bot, approvals):
    client, _ = await make_client(*MODULES)
    w = await seed(conn)
    with owner():
        project_id = (await projects.create_project(conn, "ЖК Северный", [w.ivan_chat]))["project"]["id"]
    for path in ("/api/owner/profile/owner-block", f"/api/projects/{project_id}/owner-block"):
        long = await client.put(path, json={"text": "Правило. " * 250})
        assert long.status_code == 400 and long.json()["code"] == "too_long_for_card", path
        assert "«Память»" in long.json()["error"]
        exact = ("Правило. " * 400)[:pages_build_card()].rstrip()
        action = approvals.waiting(await client.put(path, json={"text": exact}))
        assert exact in await approvals.card(action) and "только начало" not in await approvals.card(action)
    assert await approvals.pending() == 2


def pages_build_card():
    from shturman.processing import pages_service
    return pages_service.PREVIEW


async def test_setup_page_path_keeps_the_long_limit(make_client, conn, config, own_bot, approvals):
    await make_client(*MODULES)
    await seed(conn)
    text = "Правило номер один. " * 900                                  # 18 000 знаков
    with authority.setup_context(1, action="test.owner_block"):
        out = await confirm.apply_owner(conn, "pages.owner_block", {"entity_id": pages.OWNER_ENTITY, "text": text})
    assert out["status"] == "applied" and await approvals.pending() == 0
    assert len(blocks_of((config.pages_dir / pages.OWNER_PATH).read_text(encoding="utf-8")).owner) > 17_000


# --- 5. карточка «завести проект» показывает всё; чужие названия не принимаются ------------------------------

async def test_create_card_shows_description_and_every_alias(make_client, conn, own_bot, approvals):
    client, _ = await make_client(*MODULES)
    w = await seed(conn)
    aliases = [f"Корпус {n}" for n in range(1, 13)]
    action = approvals.waiting(await client.post("/api/projects", json={
        "title": "ЖК Северный", "chat_ids": [w.ivan_chat], "aliases": aliases, "description": "Дом на 120 квартир"}))
    card = await approvals.card(action)
    assert all(f"«{a}»" in card for a in aliases) and "Описание: «Дом на 120 квартир»" in card
    too_long = await client.post("/api/projects", json={"title": "Омега", "description": "д" * 301})
    assert too_long.status_code == 400
    await approvals.press(action)
    project = (await client.get("/api/projects")).json()["projects"][0]
    assert len(project["aliases"]) == 12
    assert (await client.get(f"/api/projects/{project['id']}")).json()["description"] == "Дом на 120 квартир"


async def test_alias_taken_by_another_project_is_refused(make_client, conn, own_bot, approvals):
    client, _ = await make_client(*MODULES)
    await seed(conn)
    with owner():
        await projects.create_project(conn, "ЖК Северный", aliases=["Северный"])
    await projects.create_project(conn, "Омега", origin="model")
    for body in ({"title": "Северный"}, {"title": "Южный", "aliases": ["Северный"]},
                 {"title": "Южный", "aliases": ["жк северный"]}, {"title": "Южный", "aliases": ["Омега"]}):
        refused = await client.post("/api/projects", json=body)
        assert refused.status_code == 409 and refused.json()["code"] == "name_taken", body
    assert await approvals.pending() == 0
    with owner():                                                       # и на странице настройки
        with pytest.raises(projects.ProjectsError) as err:
            await projects.create_project(conn, "Южный", aliases=["Северный"])
        assert err.value.code == "name_taken"
        # свой же предложенный проект заводится вместе со своими названиями
        assert (await projects.create_project(conn, "Омега", aliases=["Омега-2"]))["project"]["status"] == "active"


# --- 6. изменение чатов применяется разницей ---------------------------------------------------------------

async def test_late_approval_of_chats_does_not_undo_the_owners_changes(make_client, conn, own_bot, approvals):
    client, _ = await make_client(*MODULES)
    w = await seed(conn)
    third = await chat(conn, w.account, 2003, "Пётр")
    with owner():
        project_id = (await projects.create_project(conn, "ЖК Северный", [w.ivan_chat]))["project"]["id"]
    action = approvals.waiting(await client.post(f"/api/projects/{project_id}/chats", json={"add": [w.maria_chat]}))
    card = await approvals.card(action)
    assert "добавить «Мария Сидорова»" in card and "Иван" not in card.split("ЖК Северный")[1]
    # пока карточка ждала, владелец сам добавил Петра и убрал Ивана на странице настройки
    with authority.setup_context(1, action="test.chats"):
        await confirm.apply_owner(conn, memory_service.PROJECT_CHATS,
                                  {"project_id": project_id, "chat_ids": [third]})
    await approvals.press(action)
    chats = {c["id"] for c in (await projects.get_project(conn, project_id))["chats"]}
    assert chats == {third, w.maria_chat}                                # Пётр остался, Иван не вернулся
    payload = await conn.fetchval("SELECT payload FROM pending_actions WHERE id = $1", action)
    assert "chat_ids" not in str(payload)


# --- 7. служебный диалог снимает и предложения по упоминаниям ------------------------------------------------

async def test_service_dialog_drops_proposals_from_its_mentions_and_reason(conn):
    w = await seed(conn)
    bot_chat = await chat(conn, w.account, BOT, "Служебный бот", type_="bot_chat")
    msg = await say(conn, bot_chat, [(BOT, "Служебный бот", "Проект Омега: код 1234")], first_id=900)
    await conn.execute("INSERT INTO project_mentions (title, title_norm, chat_id, message_id, episode) "
                       "VALUES ('Омега', 'омега', $1, $2, 'e')", bot_chat, msg[0])
    by_mention = (await projects.create_project(conn, "Омега", origin="model",
                                                reason={"mentions": 3, "episodes": 2}))["project"]["id"]
    by_reason = (await projects.create_project(conn, "Альфа", origin="model",
                                               reason={"mentions": 3, "episodes": 2, "chats": [bot_chat]}))["project"]["id"]
    kept = (await projects.create_project(conn, "Бета", origin="model", reason={"messages": 25}))["project"]["id"]
    assert await projects.send_digest(conn, run_id=7) == 3
    digest = await conn.fetchval("SELECT id FROM jobs WHERE context->>'batch' = 'pj7'")
    await control_peers.register(conn, BOT)
    assert sorted(r["id"] for r in await conn.fetch("SELECT id FROM projects")) == [kept]
    assert await conn.fetchval("SELECT payload FROM jobs WHERE id = $1", digest) == "{}"
    assert by_mention and by_reason


# --- 8. обещания при упоре в предел — раньше фактов -----------------------------------------------------------

async def test_with_a_small_cap_promise_episodes_are_planned_first(conn):
    w = await seed(conn)                     # переписка с Иваном и Марией: в обеих есть обещания
    facts_only = await chat(conn, w.account, 2004, "Сергей")
    await say(conn, facts_only, [(2004, "Сергей", "Цена выросла до 15 тыс за метр")], start=T0 - timedelta(hours=3),
              first_id=1)
    out = await pipeline.plan_run(conn, tz=TZ, now=NOW, limit=2)
    jobs_ = await jobs.claim(conn, [bridge.LLM_STRUCTURED], worker="t", limit=10)
    inputs = [j["payload"]["input"] for j in jobs_]
    assert out["planned"] == 2 and out["cap_reached"] is True and out["fact_episodes_skipped"] == 1
    assert not any("15 тыс" in text for text in inputs)                 # эпизод только с фактом уступил место
    assert any("Пришлю смету" in text for text in inputs) and any("Акт сверки" in text for text in inputs)


async def test_promises_beyond_the_cap_are_deferred_not_lost(conn):
    w = await seed(conn)
    out = await pipeline.plan_run(conn, tz=TZ, now=NOW, limit=1)
    assert out["planned"] == 1 and out["more"] is True and out["fact_episodes_skipped"] == 0
    assert w


# --- 9. снова действующий факт теряет строку «больше не действует» ----------------------------------------------

async def test_reopened_fact_loses_its_closed_line_and_can_close_again(conn, config):
    w = await seed(conn)
    old, _ = await facts.record(conn, await candidate(conn, w.ivan_msgs[0], "прораб", slot="должность"),
                                subject_type="person", person_id=w.ivan, tz=TZ)
    later = await say(conn, w.ivan_chat, [(2001, "Иван Петров", "Я теперь главный инженер")],
                      start=T0 + timedelta(days=2), first_id=50)
    new, _ = await facts.record(conn, await candidate(conn, later[0], "главный инженер", slot="должность"),
                                subject_type="person", person_id=w.ivan, tz=TZ)
    await build_with(conn, config, lambda job: [statement("Подрядчик", [w.ivan_msgs[0]])])
    path = config.pages_dir / await conn.fetchval("SELECT path FROM pages WHERE person_id = $1", w.ivan)
    assert f"<!-- id:fz{old} -->" in path.read_text(encoding="utf-8")
    with owner():
        await facts.retract_fact(conn, new)
    await pages_build.render_dirty(conn, config.pages_dir, tz=TZ, now=NOW)
    page = blocks_of(path.read_text(encoding="utf-8"))
    assert f"id:fz{old} " not in page.timeline and page.facts.startswith("- должность: прораб")
    assert f"<!-- id:fz{new} -->" in page.timeline                       # «отмечено неверным» остаётся
    assert not await conn.fetchval("SELECT 1 FROM page_entries WHERE key = $1", f"fz{old}")
    # новая смена — строка «больше не действует» дописывается заново, с новой датой
    again = await say(conn, w.ivan_chat, [(2001, "Иван Петров", "Я теперь начальник участка")],
                      start=T0 + timedelta(days=5), first_id=60)
    await facts.record(conn, await candidate(conn, again[0], "начальник участка", slot="должность"),
                       subject_type="person", person_id=w.ivan, tz=TZ)
    await pages_build.render_dirty(conn, config.pages_dir, tz=TZ, now=NOW)
    closed = [line for line in path.read_text(encoding="utf-8").splitlines() if f"id:fz{old} " in line]
    assert len(closed) == 1 and closed[0].startswith("- 2026-10-11 — больше не действует: должность: прораб")


# --- 10. секреты, отклонённое предложение, разделение людей ------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Код от домофона 1234", "домофонный код 4567", "password: qwerty", "PIN 1234", "пин-код 0000",
    "Код от сейфа 9876", "Номер карты 4276 1234 5678 9012",
])
def test_secrets_are_never_facts(text):
    episode = extract.Episode(1, [extract.Msg(id=1, chat_id=1, sent_at=T0, sender_peer_id=7, sender_name="Иван",
                                              is_outgoing=False, text=text)])
    found, dropped = extract.validate_facts(
        {"facts": [fact(1, text, "У1", text, slot="телефон")]}, episode, extract.speaker_labels(episode))
    assert found == [] and dropped["sensitive"] == 1, text


async def test_accepting_a_rejected_proposal_is_refused_without_a_card(make_client, conn, own_bot, approvals):
    client, _ = await make_client(*MODULES)
    await seed(conn)
    project_id = (await projects.create_project(conn, "Омега", origin="model"))["project"]["id"]
    await projects.decide_project_proposal(conn, project_id, False)
    refused = await client.post(f"/api/projects/proposals/{project_id}", json={"accept": True})
    assert refused.status_code == 409 and refused.json()["code"] == "bad_status"
    assert await approvals.pending() == 0


async def test_split_moves_the_facts_of_the_separated_account(conn):
    w = await seed(conn)
    second = await chat(conn, w.account, 2009, "Иван (рабочий)")
    msgs = await say(conn, second, [(2009, "Иван (рабочий)", "Я теперь в «Бете»")], first_id=70)
    second_peer = await peer_id(conn, 2009)
    await conn.execute("INSERT INTO person_peers (peer_id, person_id) VALUES ($1, $2)", second_peer, w.ivan)
    stays, _ = await facts.record(conn, await candidate(conn, w.ivan_msgs[0], "прораб", slot="должность"),
                                  subject_type="person", person_id=w.ivan, tz=TZ)
    moves, _ = await facts.record(conn, await candidate(conn, msgs[0], "работает в «Бете»", slot="компания"),
                                  subject_type="person", person_id=w.ivan, tz=TZ)
    with owner():
        out = await people.split_person(conn, w.ivan, second_peer)
    assert await conn.fetchval("SELECT person_id FROM facts WHERE id = $1", moves) == out["person_id"]
    assert await conn.fetchval("SELECT person_id FROM facts WHERE id = $1", stays) == w.ivan
