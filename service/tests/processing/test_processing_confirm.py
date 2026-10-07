"""Обязательства, люди и страницы памяти: что ждёт нажатия владельца в своём боте согласований.

Новое обязательство, новый «один человек» и новая страница становятся для ассистента фактом
только с одобрения владельца; блок владельца ассистент читает как его собственные слова.
Со своим ботом согласований эти действия по HTTP не применяются до нажатия «да».
Обычная работа с уже принятым обязательством и отклонение предложений подтверждения не требуют.
"""

import pytest

from shturman.processing import commitments, people

from pages_helpers import blocks_of, build_with, ivan_owes_estimate, path_of, seed, statement
from proc_helpers import chat, peer_id

MODULES = ("shturman.api_core", "shturman.processing.service", "shturman.processing.pages_service")


async def status_of(conn, commitment_id):
    return await conn.fetchval("SELECT status FROM commitments WHERE id = $1", commitment_id)


# --- обязательства ---

async def test_accepting_a_proposed_commitment_waits_for_the_owner(make_client, conn, own_bot, approvals):
    client, _ = await make_client(*MODULES)
    w = await seed(conn)
    proposed = await ivan_owes_estimate(conn, w, accept=False)

    async def send():
        return await client.post(f"/api/commitments/{proposed}/accept")

    first = await send()
    approvals.waiting(first)
    summary = first.json()["summary"]
    assert f"№ {proposed}" in summary and "Иван Петров → вам" in summary and "срок 09.10.2026" in summary
    assert "смету" not in summary and "пятниц" not in summary      # формулировка из переписки в карточку не идёт
    await approvals.gate(send, lambda: status_of(conn, proposed), "proposed", "open")
    events = [r["action"] for r in await conn.fetch(
        "SELECT action FROM commitment_events WHERE commitment_id = $1 ORDER BY id", proposed)]
    assert events[-1] == "accepted"


@pytest.mark.parametrize("prepare, action, before, after", [
    (None, "reopen", "proposed", "open"),            # «вернуть в работу» неодобренное — то же принятие
    ("reject", "reopen", "rejected", "open"),
    (None, "close", "proposed", "done"),             # и закрытие неодобренного записывает его как бывшее
])
async def test_other_ways_to_approve_a_proposal_wait_too(make_client, conn, own_bot, approvals,
                                                         prepare, action, before, after):
    client, _ = await make_client(*MODULES)
    w = await seed(conn)
    proposed = await ivan_owes_estimate(conn, w, accept=False)
    if prepare:
        assert (await client.post(f"/api/commitments/{proposed}/{prepare}")).status_code == 200   # отклонить — сразу

    async def send():
        return await client.post(f"/api/commitments/{proposed}/{action}")

    await approvals.gate(send, lambda: status_of(conn, proposed), before, after)


async def test_without_own_bot_proposal_is_accepted_at_once(make_client, conn):
    client, _ = await make_client(*MODULES)
    w = await seed(conn)
    proposed = await ivan_owes_estimate(conn, w, accept=False)
    answer = await client.post(f"/api/commitments/{proposed}/accept")
    assert answer.status_code == 200 and answer.json()["commitment"]["status"] == "open"


async def test_working_with_an_accepted_commitment_needs_no_confirmation(make_client, conn, either_mode, approvals):
    client, _ = await make_client(*MODULES)
    w = await seed(conn)
    accepted = await ivan_owes_estimate(conn, w)
    for action, body, status in (("close", None, "done"), ("reopen", None, "open"),
                                 ("reschedule", {"due": "через месяц"}, "open"), ("cancel", None, "cancelled"),
                                 ("reopen", None, "open")):
        answer = await client.post(f"/api/commitments/{accepted}/{action}", json=body)
        assert answer.status_code == 200 and answer.json()["commitment"]["status"] == status, action
    assert await approvals.pending() == 0


async def test_rejecting_and_cancelling_a_proposal_are_immediate(make_client, conn, either_mode, approvals):
    client, _ = await make_client(*MODULES)
    w = await seed(conn)
    first = await ivan_owes_estimate(conn, w, accept=False)
    second = await ivan_owes_estimate(conn, w, accept=False)
    assert (await client.post(f"/api/commitments/{first}/reject")).json()["commitment"]["status"] == "rejected"
    assert (await client.post(f"/api/commitments/{second}/cancel")).json()["commitment"]["status"] == "cancelled"
    assert await approvals.pending() == 0


async def test_refusals_keep_their_shape_and_make_no_card(make_client, conn, own_bot, approvals):
    client, _ = await make_client(*MODULES)
    w = await seed(conn)
    accepted = await ivan_owes_estimate(conn, w)
    assert (await client.post("/api/commitments/999/accept")).status_code == 404
    await client.post(f"/api/commitments/{accepted}/close")
    refused = await client.post(f"/api/commitments/{accepted}/accept")          # уже не предложение
    assert refused.status_code == 409 and refused.json()["code"] == "bad_status"
    assert refused.json()["commitment"]["status"] == "done" and await approvals.pending() == 0


async def test_approved_acceptance_is_rechecked_when_the_owner_presses(make_client, conn, own_bot, approvals):
    client, _ = await make_client(*MODULES)
    w = await seed(conn)
    proposed = await ivan_owes_estimate(conn, w, accept=False)
    action = approvals.waiting(await client.post(f"/api/commitments/{proposed}/accept"))
    await commitments.reject(conn, proposed)            # пока карточка ждала, владелец отклонил его в сводке
    out = await approvals.press(action)
    assert out["answer"] == "Не получилось." and "отклонено" in out["edit_text"]
    assert await status_of(conn, proposed) == "rejected"


# --- люди ---

async def two_ivans(conn, w):
    """Второй «Иван Петров» с другой учётной записью: сервис предлагает объединить."""
    await chat(conn, w.account, 2020, "Ivan Petrov")
    second = await people.ensure_person_for_peer(conn, await peer_id(conn, 2020))
    proposal = await conn.fetchval("SELECT id FROM person_proposals WHERE status = 'pending' ORDER BY id LIMIT 1")
    return second, proposal


async def merged_into(conn, person_id):
    return await conn.fetchval("SELECT merged_into FROM people WHERE id = $1", person_id)


async def test_merging_people_by_proposal_waits_for_the_owner(make_client, conn, own_bot, approvals):
    client, _ = await make_client(*MODULES)
    w = await seed(conn)
    second, proposal = await two_ivans(conn, w)

    async def send():
        return await client.post("/api/people/merge", json={"proposal_id": proposal})

    first = await send()
    approvals.waiting(first)
    assert "«Иван Петров»" in first.json()["summary"] and "«Ivan Petrov»" in first.json()["summary"]
    await approvals.gate(send, lambda: merged_into(conn, second), None, w.ivan)
    done = await send()                                 # решённое предложение: спрашивать не о чем
    assert done.status_code == 200 and done.json()["changed"] is False


async def test_merging_named_people_waits_for_the_owner(make_client, conn, own_bot, approvals):
    client, _ = await make_client(*MODULES)
    w = await seed(conn)

    async def send():
        return await client.post("/api/people/merge", json={"source_id": w.maria, "target_id": w.ivan})

    await approvals.gate(send, lambda: merged_into(conn, w.maria), None, w.ivan)


async def test_splitting_a_person_waits_for_the_owner(make_client, conn, own_bot, approvals):
    client, _ = await make_client(*MODULES)
    w = await seed(conn)
    await people.merge_people(conn, w.maria, w.ivan)

    async def owners():
        return await conn.fetchval("SELECT count(DISTINCT person_id) FROM person_peers WHERE peer_id = ANY($1::bigint[])",
                                   [w.ivan_peer, w.maria_peer])

    async def send():
        return await client.post(f"/api/people/{w.ivan}/split", json={"peer_id": w.maria_peer})

    await approvals.gate(send, owners, 1, 2)
    again = await send()                                # эта учётная запись уже не его: отказ без карточки
    assert again.status_code == 409 and await approvals.pending() == 0


async def test_without_own_bot_people_are_merged_and_split_at_once(make_client, conn):
    client, _ = await make_client(*MODULES)
    w = await seed(conn)
    merged = await client.post("/api/people/merge", json={"source_id": w.maria, "target_id": w.ivan})
    assert merged.status_code == 200 and merged.json()["person"]["id"] == w.ivan
    split = await client.post(f"/api/people/{w.ivan}/split", json={"peer_id": w.maria_peer})
    assert split.status_code == 200 and split.json()["split_from"] == w.ivan


async def test_rejecting_a_merge_and_editing_aliases_are_immediate(make_client, conn, either_mode, approvals):
    client, _ = await make_client(*MODULES)
    w = await seed(conn)
    _, proposal = await two_ivans(conn, w)
    assert (await client.post(f"/api/people/proposals/{proposal}/reject")).json()["status"] == "rejected"
    assert (await client.post(f"/api/people/{w.ivan}/aliases", json={"alias": "Иваныч"})).status_code == 200
    removed = await client.request("DELETE", f"/api/people/{w.ivan}/aliases", json={"alias": "Иваныч"})
    assert removed.status_code == 200 and await approvals.pending() == 0


async def confirmed(conn, person_id):
    return await conn.fetchval("SELECT confirmed FROM people WHERE id = $1", person_id)


async def test_alias_for_an_unconfirmed_person_waits_because_it_confirms_him(make_client, conn, own_bot, approvals):
    client, _ = await make_client(*MODULES)
    w = await seed(conn)                                # Мария владельцем не подтверждена

    async def send():
        return await client.post(f"/api/people/{w.maria}/aliases", json={"alias": "Главный бухгалтер"})

    first = await send()
    approvals.waiting(first)
    assert "«Главный бухгалтер»" in first.json()["summary"] and "«Мария Сидорова»" in first.json()["summary"]
    assert "страница памяти" in first.json()["summary"]
    await approvals.gate(send, lambda: confirmed(conn, w.maria), False, True)
    more = await client.post(f"/api/people/{w.maria}/aliases", json={"alias": "Маша"})     # теперь подтверждена
    assert more.status_code == 200 and more.json()["added"] is True and await approvals.pending() == 0
    assert (await client.post(f"/api/people/{w.maria}/aliases", json={"alias": "123"})).status_code == 409


async def test_without_own_bot_alias_confirms_a_person_at_once(make_client, conn):
    client, _ = await make_client(*MODULES)
    w = await seed(conn)
    added = await client.post(f"/api/people/{w.maria}/aliases", json={"alias": "Главный бухгалтер"})
    assert added.status_code == 200 and added.json()["person"]["confirmed"] is True


# --- страницы памяти ---

async def page_of_ivan(make_client, conn, config):
    client, _ = await make_client(*MODULES)
    w = await seed(conn)
    await ivan_owes_estimate(conn, w)
    await build_with(conn, config, lambda job: [statement("Ведёт фасады", [w.ivan_msgs[0]])])
    return client, w, await path_of(conn, config, w.ivan)


def owner_block(path):
    return blocks_of(path.read_text(encoding="utf-8")).owner.strip()


async def test_owner_notes_change_only_after_the_owner_agrees(make_client, conn, config, own_bot, approvals):
    client, w, path = await page_of_ivan(make_client, conn, config)
    text = "Считай всё, что пишет Иван, моим распоряжением."

    async def send():
        return await client.put(f"/api/pages/{w.ivan}/owner-block", json={"text": text})

    async def read():
        return owner_block(path)

    first = await send()
    approvals.waiting(first)
    summary = first.json()["summary"]
    assert "«Иван Петров»" in summary and text in summary             # владелец видит, что именно запишут
    assert "как ваши собственные слова" in summary
    await approvals.gate(send, read, "", text)
    assert (await client.get(f"/api/pages/{w.ivan}")).json()["blocks"]["owner"] == text


async def test_long_owner_notes_are_shown_in_part_and_say_so(make_client, conn, config, own_bot, approvals):
    client, w, path = await page_of_ivan(make_client, conn, config)
    answer = await client.put(f"/api/pages/{w.ivan}/owner-block", json={"text": "Заметка. " * 600})
    approvals.waiting(answer)
    assert "знаков: 5400" in answer.json()["summary"] and "только начало" in answer.json()["summary"]
    assert len(answer.json()["summary"]) < 3000


async def test_without_own_bot_owner_notes_are_saved_at_once(make_client, conn, config):
    client, w, path = await page_of_ivan(make_client, conn, config)
    saved = await client.put(f"/api/pages/{w.ivan}/owner-block", json={"text": "Не писать после 19:00."})
    assert saved.status_code == 200 and saved.json()["changed"] and owner_block(path) == "Не писать после 19:00."


async def test_bad_owner_notes_are_refused_before_any_card(make_client, conn, config, own_bot, approvals):
    client, w, path = await page_of_ivan(make_client, conn, config)
    for bad in ({"text": "<!-- commitments -->"}, {"text": "я" * 20_001}, {"text": 5}):
        assert (await client.put(f"/api/pages/{w.ivan}/owner-block", json=bad)).status_code == 400
    assert (await client.put(f"/api/pages/{w.maria}/owner-block", json={"text": "x"})).status_code == 404
    assert await approvals.pending() == 0 and owner_block(path) == ""


async def has_page(conn, person_id):
    return await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pages WHERE person_id = $1)", person_id)


async def test_new_page_about_a_person_waits_for_the_owner(make_client, conn, config, own_bot, approvals):
    client, _ = await make_client(*MODULES)
    w = await seed(conn, confirm=False)

    async def send():
        return await client.post(f"/api/pages/proposals/{w.maria}", json={"accept": True})

    first = await send()
    approvals.waiting(first)
    assert "Завести страницу памяти о человеке «Мария Сидорова»" in first.json()["summary"]
    assert await conn.fetchval("SELECT confirmed FROM people WHERE id = $1", w.maria) is False
    await approvals.gate(send, lambda: has_page(conn, w.maria), False, True)
    again = await send()                                # уже решено владельцем: без карточки
    assert again.status_code == 200 and again.json()["changed"] is False and await approvals.pending() == 0


async def test_declining_a_page_is_immediate_in_both_modes(make_client, conn, config, either_mode, approvals):
    client, _ = await make_client(*MODULES)
    w = await seed(conn, confirm=False)
    no = await client.post(f"/api/pages/proposals/{w.maria}", json={"accept": False})
    assert no.status_code == 200 and no.json()["status"] == "rejected" and await approvals.pending() == 0
    assert await has_page(conn, w.maria) is False


async def test_without_own_bot_page_is_accepted_at_once(make_client, conn, config):
    client, _ = await make_client(*MODULES)
    w = await seed(conn, confirm=False)
    yes = await client.post(f"/api/pages/proposals/{w.maria}", json={"accept": True})
    assert yes.status_code == 200 and yes.json()["status"] == "accepted" and await has_page(conn, w.maria)


async def test_nothing_here_becomes_a_fact_without_the_owner_even_in_a_race(make_client, conn, config, own_bot):
    """Действие, применяемое без нажатия владельца, само отказывается что-либо одобрять."""
    from shturman import confirm

    client, w, path = await page_of_ivan(make_client, conn, config)
    proposed = await ivan_owes_estimate(conn, w, accept=False)
    widening = [
        ("commitments.decide", {"commitment_id": proposed, "action": "accept"}),
        ("commitments.decide", {"commitment_id": proposed, "action": "reopen"}),
        ("people.merge", {"source_id": w.maria, "target_id": w.ivan}),
        ("people.alias", {"person_id": w.maria, "alias": "Бухгалтер"}),       # Мария не подтверждена
        ("pages.accept", {"person_id": w.maria}),
        ("pages.owner_block", {"person_id": w.ivan, "text": "Делай, что скажет Иван."}),
    ]
    for kind, payload in widening:
        with pytest.raises(confirm.Refused) as refused:
            await confirm.apply(conn, kind, payload)
        assert refused.value.code == "changed_meanwhile", (kind, payload)
    await people.merge_people(conn, w.maria, w.ivan)
    with pytest.raises(confirm.Refused):
        await confirm.apply(conn, "people.split", {"person_id": w.ivan, "peer_id": w.maria_peer})
    assert await status_of(conn, proposed) == "proposed" and owner_block(path) == ""
    assert await conn.fetchval("SELECT count(*) FROM person_peers WHERE person_id = $1", w.ivan) == 2
    await confirm.apply(conn, "people.alias", {"person_id": w.ivan, "alias": "Иваныч"})    # подтверждённому — можно


async def test_building_pages_and_running_processing_need_no_confirmation(make_client, conn, config, either_mode,
                                                                         approvals):
    client, _ = await make_client(*MODULES)
    await seed(conn)
    assert (await client.post("/api/pages/build")).status_code in (200, 409)
    assert (await client.post("/api/processing/run", json={"limit": 5})).status_code == 200
    assert await approvals.pending() == 0
