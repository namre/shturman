"""Полнота и время памяти (шаг 5 записки «держать, подкладывать, сигналить»): «что действовало на дату»,
«что ассистент знал на момент», отдельный счёт потерь правила «о ком» и более широкий отбор до модели."""

from datetime import date, datetime, timedelta, timezone

from shturman.processing import extract, facts, people

from memory_helpers import fact, notifications, owner, press_button, buttons, run_with
from proc_helpers import T0, chat, peer_id, say
from test_memory_projects import IVAN, seed
from test_memory_trust import NEW_ACC, OLD_ACC, project_with_requisites, stranger_says_new_requisites

D0 = T0.date()                     # 6 октября: владелец назвал реквизиты
D1 = D0 + timedelta(days=1)        # 7 октября: посторонний назвал другие


def texts(items):
    return sorted((i["text"], i["state_at"]) for i in items)


async def at(conn, when):
    """Время записи и применения фактов — как будто прогоны шли в эти моменты (в тестах всё «сейчас»)."""
    for fact_id, created in when.items():
        await conn.execute("UPDATE facts SET created_at = $2::timestamptz, applied_at = CASE WHEN applied_at IS NULL THEN NULL "
                           "ELSE $2::timestamptz END WHERE id = $1", fact_id, created)


async def test_known_at_and_in_force_around_an_accepted_conflict(conn):
    w = await project_with_requisites(conn)
    new = await stranger_says_new_requisites(conn, w)
    t_old = datetime(2026, 10, 6, 13, tzinfo=timezone.utc)
    t_new = datetime(2026, 10, 7, 15, tzinfo=timezone.utc)
    await at(conn, {w.old: t_old, new: t_new})
    old_text, new_text = f"реквизиты подрядчика: р/с {OLD_ACC}", f"реквизиты подрядчика: р/с {NEW_ACC}"
    # до решения владельца: знал прежнее как действующее и новое как предложенное
    assert texts(await facts.facts_known_at(conn, "project", w.project, t_new + timedelta(hours=1))) == \
        sorted([(old_text, "active"), (new_text, "proposed")])
    assert texts(await facts.facts_known_at(conn, "project", w.project, t_old - timedelta(hours=1))) == []
    # владелец принимает новое
    await facts.send_project_digest(conn, run_id=1)
    card, = [j for j in await notifications(conn) if j["payload"]["text"].startswith("Проекты:")]
    accept = [d for d in buttons([card], "of") if d.startswith("sh:of:a:")][0]
    assert (await press_button(conn, accept))["answer"].startswith("Принято")
    accepted = datetime(2026, 10, 9, 10, tzinfo=timezone.utc)
    await conn.execute("UPDATE facts SET applied_at = $2, decided_at = $2 WHERE id = $1", new, accepted)
    # то, что ассистент знал 8 октября, решение 9 октября задним числом не переписывает
    assert texts(await facts.facts_known_at(conn, "project", w.project, accepted - timedelta(hours=1))) == \
        sorted([(old_text, "active"), (new_text, "proposed")])
    assert texts(await facts.facts_known_at(conn, "project", w.project, accepted + timedelta(hours=1))) == \
        [(new_text, "active")]
    # действовало: 6-го — прежнее; с 7-го (дата сообщения о новых) — новое
    assert texts(await facts.facts_in_force(conn, "project", w.project, D0)) == [(old_text, "active")]
    assert texts(await facts.facts_in_force(conn, "project", w.project, D1)) == [(new_text, "active")]
    assert await facts.facts_in_force(conn, "project", w.project, D0 - timedelta(days=1)) == []


async def test_chained_person_slot_by_date_and_by_knowledge(conn):
    w = await seed(conn)
    await say(conn, w.ivan_chat, [(IVAN, "Иван Петров", "Я теперь главный инженер на Северном.")])
    await run_with(conn, lambda job: {"commitments": [], "facts": [
        fact(1, "Я теперь главный инженер", "У1", "главный инженер", slot="должность")]})
    await say(conn, w.ivan_chat, [(IVAN, "Иван Петров", "С сегодняшнего дня я руковожу проектом Северный.")],
              start=T0 + timedelta(days=3), first_id=10)
    await run_with(conn, lambda job: {"commitments": [], "facts": [
        fact(1, "я руковожу проектом Северный", "У1", "руководитель проекта Северный", slot="должность")]},
        now=T0 + timedelta(days=3, hours=2))
    first, second = [r["id"] for r in await conn.fetch("SELECT id FROM facts ORDER BY id")]
    t1, t2 = datetime(2026, 10, 6, 13, tzinfo=timezone.utc), datetime(2026, 10, 9, 14, tzinfo=timezone.utc)
    await at(conn, {first: t1, second: t2})
    assert texts(await facts.facts_known_at(conn, "person", w.ivan, t1 + timedelta(days=1))) == \
        [("главный инженер", "active")]
    assert texts(await facts.facts_known_at(conn, "person", w.ivan, t2 + timedelta(hours=1))) == \
        [("руководитель проекта Северный", "active")]
    assert texts(await facts.facts_in_force(conn, "person", w.ivan, date(2026, 10, 8))) == [("главный инженер", "active")]
    assert texts(await facts.facts_in_force(conn, "person", w.ivan, date(2026, 10, 9))) == \
        [("руководитель проекта Северный", "active")]
    # отмеченное владельцем неверным: до отметки ассистент считал его верным, после — нет, и «действовало» — никогда
    with owner():
        await facts.retract_fact(conn, second)
    retracted = datetime(2026, 10, 10, 9, tzinfo=timezone.utc)
    await conn.execute("UPDATE facts SET decided_at = $2 WHERE id = $1", second, retracted)
    assert texts(await facts.facts_known_at(conn, "person", w.ivan, retracted - timedelta(hours=1))) == \
        [("руководитель проекта Северный", "active")]
    assert texts(await facts.facts_known_at(conn, "person", w.ivan, retracted + timedelta(hours=1))) == \
        [("главный инженер", "active")]
    assert texts(await facts.facts_in_force(conn, "person", w.ivan, date(2026, 10, 9))) == [("главный инженер", "active")]


async def test_facts_api_takes_a_date(conn, make_client):
    w = await project_with_requisites(conn)
    from test_memory_service import MODULES
    client, _ = await make_client(*MODULES)
    r = await client.get("/api/facts", params={"subject": f"project:{w.project}", "as_of": D0.isoformat()})
    assert r.status_code == 200 and r.json()["as_of"] == D0.isoformat()
    assert [f["state_at"] for f in r.json()["facts"]] == ["active"]
    r = await client.get("/api/facts", params={"subject": f"project:{w.project}", "known_at": "2000-01-01"})
    assert r.status_code == 200 and r.json()["facts"] == []
    assert (await client.get("/api/facts", params={"subject": f"project:{w.project}", "as_of": "вчера"})).status_code == 400
    assert (await client.get("/api/facts", params={"subject": f"project:{w.project}", "as_of": D0.isoformat(),
                                                   "known_at": D0.isoformat()})).status_code == 400


async def test_subject_losses_are_counted_for_people_and_projects_apart(conn):
    w = await seed(conn)
    stranger = await chat(conn, w.account, 2077, "Незнакомец")
    await say(conn, stranger, [(2077, "Незнакомец", "Я теперь работаю в другой компании, новый телефон скину.")])
    await say(conn, w.group, [(2078, "Пётр Сидоров", "По Лесному бюджет теперь 12 млн."),
                              (2078, "Пётр Сидоров", "Бюджет на отделку тоже вырос до 3 млн.")], first_id=5)
    await people.ensure_person_for_peer(conn, await peer_id(conn, 2077))

    def reply(job):
        text = job["payload"]["input"]
        if "Незнакомец" in text:
            return {"commitments": [], "facts": [fact(1, "Я теперь работаю в другой компании", "У1",
                                                      "работает в другой компании", slot="место работы")]}
        return {"commitments": [], "facts": [
            fact(1, "бюджет теперь 12 млн", "ПРОЕКТ", "бюджет 12 млн", slot="бюджет", project="Лесной"),
            fact(2, "Бюджет на отделку тоже вырос до 3 млн", "ПРОЕКТ", "бюджет на отделку 3 млн", slot="бюджет")]}

    await run_with(conn, reply)
    stats = await conn.fetchval("SELECT stats FROM processing_runs ORDER BY id DESC LIMIT 1")
    import json
    results = (json.loads(stats) if isinstance(stats, str) else stats)["results"]
    assert results["facts_without_subject"] == 2
    assert results["facts_without_subject_person_unconfirmed"] == 1
    assert results["facts_without_subject_project_inactive"] == 1
    assert results["facts_dropped_bad_subject"] == 1          # «о проекте» без имени проекта отсеивает проверка


def test_wider_fact_signal_catches_moves_roles_and_changes():
    for text in ("Прораб на объекте теперь другой", "сдачу перенесли на март", "Электрику отдаём новому подрядчику",
                 "Сидоров отвечает за кровлю", "Вместо Козлова будет Смирнов", "Договор подписали вчера",
                 "Срок продлили до конца ноября"):
        assert extract._FACT_RE.search(extract._plain(text)), text
    for text in ("привет, как дела", "скинь фото", "ок", "спасибо, понял"):
        assert not extract._FACT_RE.search(extract._plain(text)), text
