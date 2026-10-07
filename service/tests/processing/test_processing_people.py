"""Реестр людей: упоминание -> человек, отображаемое имя -> запись, слияние только через владельца."""

import pytest

from shturman import store
from shturman.processing import people

from proc_helpers import OWNER, account, chat, peer_id

IVAN_P, IVAN_S, NATASHA, SASHA, PETR, OLEG_K, ANNA_P = 2001, 2002, 2003, 2004, 2005, 2006, 2007


async def registry(conn):
    """Пять человек из прототипа и двое для ловушек с родом фамилии."""
    account_id = await account(conn)
    ids = {}
    for tg_id, name, aliases in [
        (IVAN_P, "Иван Петров", ["Иван Иванович"]),
        (IVAN_S, "Иван Сидоров", ["Иван Сергеевич"]),
        (NATASHA, "Наташа Кузнецова", ["Наталья Сергеевна"]),
        (SASHA, "Sasha E", ["Александр Михайлович Эрман"]),
        (PETR, "Пётр Петренко | ГК Фасад", ["Пётр Павлович"]),
        (OLEG_K, "Олег Кузнецов", []),
        (ANNA_P, "Анна Петрова", []),
    ]:
        await chat(conn, account_id, tg_id, name)
        person_id = await people.ensure_person_for_peer(conn, await peer_id(conn, tg_id))
        for alias in aliases:
            await people.add_alias(conn, person_id, alias)
        ids[tg_id] = person_id
    return account_id, ids


async def resolved(conn, mention, **kw):
    out = await people.resolve_mention(conn, mention, **kw)
    return out["status"], [c["person_id"] for c in out["candidates"]]


async def test_mentions_resolve_through_case_forms(conn):
    _, ids = await registry(conn)
    assert await resolved(conn, "Ивану Иванычу") == ("match", [ids[IVAN_P]])
    assert await resolved(conn, "Иван Иванычу") == ("match", [ids[IVAN_P]])
    assert await resolved(conn, "Сидорову Ивану") == ("match", [ids[IVAN_S]])
    assert await resolved(conn, "Наталье Сергеевне") == ("match", [ids[NATASHA]])
    assert await resolved(conn, "с Натальей Кузнецовой") == ("match", [ids[NATASHA]])
    assert await resolved(conn, "Сан Михалычу") == ("match", [ids[SASHA]])
    assert await resolved(conn, "Эрману") == ("match", [ids[SASHA]])
    assert await resolved(conn, "Петру Палычу") == ("match", [ids[PETR]])
    assert await resolved(conn, "с Петренко") == ("match", [ids[PETR]])
    assert await resolved(conn, "Диме") == ("none", [])
    assert await resolved(conn, "") == ("none", [])


async def test_same_first_name_is_ambiguous_not_guessed(conn):
    _, ids = await registry(conn)
    status, found = await resolved(conn, "Ване")
    assert status == "ambiguous" and set(found) == {ids[IVAN_P], ids[IVAN_S]}
    status, found = await resolved(conn, "Иван")
    assert status == "ambiguous" and set(found) == {ids[IVAN_P], ids[IVAN_S]}


async def test_feminine_surname_does_not_match_a_man(conn):
    """«Кузнецовой» морфология сводит к «Кузнецов»; указатель форм этого не допускает."""
    _, ids = await registry(conn)
    assert await resolved(conn, "Кузнецовой") == ("match", [ids[NATASHA]])
    assert await resolved(conn, "Кузнецовым") == ("match", [ids[OLEG_K]])
    # «Петрову» — дательный от «Петров» и винительный от «Петрова»: выбрать нельзя
    status, found = await resolved(conn, "Петрову")
    assert status == "ambiguous" and set(found) == {ids[IVAN_P], ids[ANNA_P]}
    assert await resolved(conn, "Петровой") == ("match", [ids[ANNA_P]])


async def test_unknown_surname_is_not_reported_as_match(conn):
    _, ids = await registry(conn)
    await people.merge_people(conn, ids[IVAN_S], ids[IVAN_P])  # остался один Иван
    assert await resolved(conn, "Ивану") == ("match", [ids[IVAN_P]])
    assert await resolved(conn, "Ивану Смирнову") == ("partial", [ids[IVAN_P]])
    assert await resolved(conn, "передай Ивану") == ("match", [ids[IVAN_P]])


async def test_chat_participants_break_a_tie(conn):
    account_id, ids = await registry(conn)
    sidorov_chat = await conn.fetchval(
        "SELECT c.id FROM chats c JOIN peers p ON p.id = c.peer_id WHERE p.tg_id = $1", IVAN_S)
    out = await people.resolve_mention(conn, "Ване", chat_id=sidorov_chat)
    assert out["status"] == "match" and out["candidates"][0]["person_id"] == ids[IVAN_S]
    assert out["candidates"][0]["in_chat"] is True


async def test_owner_alias_and_username(conn):
    account_id, ids = await registry(conn)
    await chat(conn, account_id, 2010, "Михаил Орлов", username="orlov_m")
    orlov = await people.ensure_person_for_peer(conn, await peer_id(conn, 2010))
    assert await resolved(conn, "@orlov_m") == ("match", [orlov])
    await people.add_alias(conn, ids[PETR], "Петрович с Фасада")
    await people.add_alias(conn, ids[PETR], "Шеф")
    assert await resolved(conn, "Петрович с Фасада") == ("match", [ids[PETR]])
    assert await resolved(conn, "шефу") == ("match", [ids[PETR]])
    assert (await people.remove_alias(conn, ids[PETR], "Шеф"))["removed"] == 1
    assert await resolved(conn, "шефу") == ("none", [])
    # имя из Telegram так не убрать
    assert (await people.remove_alias(conn, ids[PETR], "Пётр Петренко | ГК Фасад"))["removed"] == 0


async def named(conn, display):
    out = await people.match_display_name(conn, display)
    return out["status"], [c["person_id"] for c in out["candidates"]]


async def test_display_names_match_across_scripts_and_decorations(conn):
    _, ids = await registry(conn)
    assert await named(conn, "Пётр Петренко | ГК Фасад") == ("match", [ids[PETR]])
    assert await named(conn, "Петр Петренко (Фасад)") == ("match", [ids[PETR]])
    assert await named(conn, "Aleksandr Erman") == ("match", [ids[SASHA]])
    assert await named(conn, "Александр Эрман") == ("match", [ids[SASHA]])
    assert await named(conn, "Natasha Kuznetsova") == ("match", [ids[NATASHA]])
    assert await named(conn, "Кузнецова Наталья") == ("match", [ids[NATASHA]])
    assert await named(conn, "Ivan Petrov") == ("match", [ids[IVAN_P]])
    assert await named(conn, "Иван Петренко") == ("none", [])
    assert await named(conn, "Dmitry") == ("none", [])
    assert await named(conn, "🔥🔥🔥") == ("none", [])
    status, found = await named(conn, "Иван")
    assert status == "ambiguous" and set(found) == {ids[IVAN_P], ids[IVAN_S]}
    score = (await people.match_display_name(conn, "Aleksandr Erman"))["candidates"][0]["score"]
    assert score >= 85


async def test_second_account_becomes_a_proposal_never_a_merge(conn):
    account_id, ids = await registry(conn)
    await chat(conn, account_id, 2020, "Ivan Petrov")
    second = await people.ensure_person_for_peer(conn, await peer_id(conn, 2020))
    assert second not in ids.values()                       # отдельная запись, не слияние
    assert await people.ensure_person_for_peer(conn, await peer_id(conn, 2020)) == second  # повтор — та же
    proposals = await people.list_proposals(conn)
    assert [(p["person"]["id"], p["other"]["id"]) for p in proposals] == [(second, ids[IVAN_P])]
    assert proposals[0]["score"] >= 85
    # до решения владельца упоминание по-прежнему неоднозначно между записями
    status, found = await resolved(conn, "Петрову Ивану")
    assert status == "ambiguous" and set(found) == {second, ids[IVAN_P]}

    out = await people.decide_proposal(conn, proposals[0]["id"], accept=True)
    assert out["person_id"] == ids[IVAN_P]
    merged = await people.get_person(conn, second)          # старый идентификатор ведёт к новой записи
    assert merged["id"] == ids[IVAN_P] and merged["confirmed"] is True
    assert {p["tg_id"] for p in merged["peers"]} == {IVAN_P, 2020}
    assert await resolved(conn, "Петрову Ивану") == ("match", [ids[IVAN_P]])
    assert await people.list_proposals(conn) == []
    assert (await people.decide_proposal(conn, proposals[0]["id"], accept=False))["changed"] is False

    # владелец передумал: отделяет вторую учётную запись обратно
    split = await people.split_person(conn, ids[IVAN_P], await peer_id(conn, 2020))
    assert {p["tg_id"] for p in (await people.get_person(conn, split["person_id"]))["peers"]} == {2020}
    assert {p["tg_id"] for p in (await people.get_person(conn, ids[IVAN_P]))["peers"]} == {IVAN_P}
    # и эту пару больше не предлагаем
    assert await people.list_proposals(conn) == []
    with pytest.raises(people.PeopleError):
        await people.split_person(conn, ids[IVAN_P], await peer_id(conn, IVAN_P))


async def test_rejected_proposal_does_not_come_back(conn):
    account_id, ids = await registry(conn)
    await chat(conn, account_id, 2020, "Ivan Petrov")
    second = await people.ensure_person_for_peer(conn, await peer_id(conn, 2020))
    proposal = (await people.list_proposals(conn))[0]
    await people.decide_proposal(conn, proposal["id"], accept=False)
    assert await people._propose_merges(conn, second, "Ivan Petrov") == 0
    assert await people.list_proposals(conn) == []
    assert (await people.get_person(conn, second))["id"] == second


async def test_single_first_name_never_proposes_a_merge(conn):
    account_id, ids = await registry(conn)
    await people.merge_people(conn, ids[IVAN_S], ids[IVAN_P])
    await chat(conn, account_id, 2030, "Иван")
    await people.ensure_person_for_peer(conn, await peer_id(conn, 2030))
    assert await people.list_proposals(conn) == []


async def test_bots_service_accounts_and_groups_are_not_people(conn):
    account_id = await account(conn)
    await chat(conn, account_id, 5001, "Помощник", type_="bot_chat")
    await chat(conn, account_id, 777000, "Telegram")
    await chat(conn, account_id, 3001, "Семья", type_="private_group", cls="chat")
    assert await people.ensure_person_for_peer(conn, await peer_id(conn, 5001)) is None
    assert await people.ensure_person_for_peer(conn, await peer_id(conn, 777000)) is None
    assert await people.ensure_person_for_peer(conn, await peer_id(conn, 3001, "chat")) is None
    assert await conn.fetchval("SELECT count(*) FROM people") == 0


async def test_owner_is_one_person_and_sync_covers_personal_chats(conn):
    account_id = await account(conn)
    await chat(conn, account_id, IVAN_P, "Иван Петров")
    await chat(conn, account_id, 2040, "Секрет", exclude=True)
    await chat(conn, account_id, 3001, "Семья", type_="private_group", cls="chat")
    owner = await people.ensure_person_for_peer(conn, await peer_id(conn, OWNER))
    assert (await people.get_person(conn, owner))["is_owner"] is True
    assert await people.sync_people(conn) == {"created": 1, "renamed": 0}
    assert await people.sync_people(conn) == {"created": 0, "renamed": 0}
    # человек сменил имя в Telegram: новое имя добавляется, прежнее остаётся
    await store.ensure_peer(conn, "user", IVAN_P, name="Иван Петров (Фасад-Строй)", refresh=True)
    assert await people.sync_people(conn) == {"created": 0, "renamed": 1}
    ivan = await people.get_person(conn, await people.person_for_peer(conn, await peer_id(conn, IVAN_P)))
    assert len([a for a in ivan["aliases"] if a["origin"] == "telegram"]) == 2
    assert ivan["confirmed"] is False


async def test_forms_are_rebuilt_from_registry_only(conn):
    _, ids = await registry(conn)
    before = await conn.fetchval("SELECT count(*) FROM person_forms")
    await conn.execute("DELETE FROM person_forms")
    assert await resolved(conn, "Ивану Иванычу") == ("none", [])
    await people.rebuild_forms(conn)
    assert await conn.fetchval("SELECT count(*) FROM person_forms") == before
    assert await resolved(conn, "Ивану Иванычу") == ("match", [ids[IVAN_P]])


def test_fold_and_name_parsing():
    assert people.fold("Пётр Михайлович") == people.fold("петр михаилович")
    assert people.fold("Aleksandr Erman") == people.fold("Александр Эрман")
    assert people.fold("Dmitry") == people.fold("Дмитрий")
    assert people.fold("Evgeniy") == people.fold("Евгений")
    parsed = people.parse_name("Петров Иван Иванович | ООО Ромашка")
    assert (parsed.first, parsed.middle, parsed.last, parsed.gender) == ("Иван", "Иванович", "Петров", "m")
    parsed = people.parse_name("Natalia Kuznetsova")
    assert (parsed.first, parsed.last, parsed.gender) == ("Наталия", "Кузнецова", "f")
    assert people.parse_name("🔥").parts == 0
    assert people.parse_name(None).parts == 0
    forms = people.name_forms(people.ParsedName("Иван", "Иванович", "Петров", "m"))
    assert {("ване", "first"), ("иванычу", "middle"), ("петрову", "last")} <= forms
    assert ("петровои", "last") not in forms   # женская форма у мужчины не строится
