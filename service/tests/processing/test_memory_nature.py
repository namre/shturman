"""Вид сведения и уточнение ключа (Р-111, разбор памяти пилота 26.10.2026).

* событие («подключил», «торги закрыты») — только в хронологию, действующим не бывает;
* положение дел («сейчас», «пока», «на 29.09», статус без уточнения) — действует, пока свежее
  (`facts.OBSERVATION_DAYS`), потом только в хронологии;
* о владельце — только свойство: его действия и положение дел в профиль не предлагаются;
* ключ с уточнением («статус (закупка электрики)»): значения сменяют друг друга только при совпадении
  уточнения; статус проекта без уточнения ничего не сменяет;
* проверки памяти для `doctor` — только числа.

Данные тестов датированы T0 (06.10.2026): положение дел оттуда уже устарело. Свежее — сдвигом даты
на сегодня."""

from datetime import timedelta

from shturman.processing import extract, facts, memory_health, pages_build, projects

from memory_helpers import fact, owner, run_with
from proc_helpers import OWNER, T0, say
from test_memory_projects import IVAN, seed

EP = extract.Episode(chat_id=1, messages=[extract.Msg(
    id=1, chat_id=1, sent_at=T0, sender_peer_id=None, sender_name=None, is_outgoing=True,
    text="Сейчас LiteLLM работает, торги по кровле закрыли, бюджет кровли 9 млн")])


def candidates(*items):
    found, _ = extract.validate_facts({"facts": list(items)}, EP, {("owner",): extract.OWNER_LABEL})
    return found


def test_nature_and_detail_are_checked_by_code():
    by_model, moment, event, detail, long_detail, decision = candidates(
        fact(1, "бюджет кровли 9 млн", "ПРОЕКТ", "Бюджет кровли 9 млн", slot="бюджет", project="X",
             slot_detail=" «кровля» ", nature="strange"),
        fact(1, "Сейчас LiteLLM работает", "ПРОЕКТ", "Сейчас LiteLLM работает", project="X", nature="state"),
        fact(1, "торги по кровле закрыли", "ПРОЕКТ", "Торги по кровле закрыты", slot="статус", project="X",
             slot_detail="кровля", nature="event"),
        fact(1, "LiteLLM работает, торги", "ПРОЕКТ", "LiteLLM работает", project="X", slot_detail="кровля"),
        fact(1, "бюджет кровли 9 млн", "ПРОЕКТ", "Кровля 9 млн", slot="бюджет", project="X", slot_detail="к" * 61),
        fact(1, "торги по кровле закрыли", "ПРОЕКТ", "Закрыли торги по кровле", project="X", kind="decision",
             nature="event"))
    assert (by_model.nature, by_model.slot, by_model.slot_detail) == ("state", "бюджет", "кровля")
    assert moment.nature == "observation"                           # примета момента — положение дел
    assert (event.nature, event.slot, event.slot_detail) == ("event", None, None)   # событие ничего не сменяет
    assert detail.slot_detail is None                               # уточнение — только при ключе
    assert long_detail.slot_detail is None
    assert decision.nature == "state"


async def test_owner_profile_takes_only_lasting_facts(conn):
    w = await seed(conn)
    await say(conn, w.ivan_chat, [(OWNER, "Евгений Тестов", "Подключил сервис к агенту. Сейчас я в Екатеринбурге. Теперь "
                                                          "я руководитель лаборатории.")])
    await run_with(conn, lambda job: {"commitments": [], "facts": [
        fact(1, "Подключил сервис к агенту", "ВЛАДЕЛЕЦ", "Подключил сервис к агенту", nature="event"),
        fact(1, "Сейчас я в Екатеринбурге", "ВЛАДЕЛЕЦ", "Сейчас в Екатеринбурге", slot="город"),
        fact(1, "я руководитель лаборатории", "ВЛАДЕЛЕЦ", "Руководитель лаборатории", slot="должность",
             nature="state")]})
    rows = await conn.fetch("SELECT text, status, nature FROM facts WHERE subject_type = 'owner'")
    assert [(r["text"], r["status"], r["nature"]) for r in rows] == [("Руководитель лаборатории", "proposed", "state")]


async def project(conn, w):
    with owner():
        return (await projects.create_project(conn, "ЖК Северный", [w.group], ["Северный"]))["project"]["id"]


async def test_status_without_detail_changes_nothing_and_ages_out(conn):
    """Разные работы одного проекта: статус без уточнения не закрывает прежний; оба — положение дел и со
    временем уходят в хронологию (T0 — давно)."""
    w = await seed(conn)
    pid = await project(conn, w)
    await say(conn, w.group, [(OWNER, "Евгений Тестов", "Решили по Северному: сервис видеовстреч снова работает"),
                              (OWNER, "Евгений Тестов", "Решили по Северному: торги по электрике закрыты без победителя",
                               {"at": T0 + timedelta(days=1)})])

    def reply(job):       # оба варианта под номером 1: не найденная в сообщении цитата отсеивается
        return {"commitments": [], "facts": [
            fact(1, quote, "ПРОЕКТ", quote.capitalize(), slot="статус", project="Северный", nature="state")
            for quote in ("торги по электрике закрыты без победителя", "сервис видеовстреч снова работает")]}
    await run_with(conn, reply, now=T0 + timedelta(days=1, hours=1))
    rows = await conn.fetch("SELECT id, valid_to, superseded_by, chained, nature FROM facts ORDER BY id")
    assert len(rows) == 2
    assert all((r["valid_to"], r["superseded_by"], r["chained"], r["nature"]) == (None, None, False, "observation")
               for r in rows)
    assert await facts.list_facts(conn, "project", pid) == []           # устарели — только хронология
    states = {h["state"] for h in await facts.history(conn, "project", pid)}
    assert states == {"stale"}
    # свежее положение дел действует и на странице — «на дату»
    await conn.execute("UPDATE facts SET valid_from = CURRENT_DATE WHERE id = $1", rows[1]["id"])
    current = await facts.list_facts(conn, "project", pid)
    assert [(f["id"], f["nature"], f["current"]) for f in current] == [(rows[1]["id"], "observation", True)]
    entries, now_facts, _ = await pages_build._facts_from_base(conn, "project", pid)
    assert [f["id"] for f in now_facts] == [rows[1]["id"]]
    assert sum(e.text.startswith("положение дел") for e in entries) == 2


async def test_status_with_detail_chains_only_within_the_same_work(conn):
    w = await seed(conn)
    pid = await project(conn, w)
    await say(conn, w.group, [
        (OWNER, "Евгений Тестов", "Решили по Северному: электрика — объявили торги"),
        (OWNER, "Евгений Тестов", "Решили по Северному: кровля — подрядчик выбран", {"at": T0 + timedelta(hours=1)}),
        (OWNER, "Евгений Тестов", "Решили по Северному: электрика — торги закрыты", {"at": T0 + timedelta(days=1)})])
    said = [("электрика — объявили торги", "электрика"), ("кровля — подрядчик выбран", "кровля"),
            ("электрика — торги закрыты", "Электрика")]

    def reply(job):       # каждое сообщение — свой эпизод; чужая цитата отсеивается проверкой
        return {"commitments": [], "facts": [
            fact(1, quote, "ПРОЕКТ", quote, slot="статус", slot_detail=detail, project="Северный", nature="state")
            for quote, detail in said]}
    await run_with(conn, reply, now=T0 + timedelta(days=1, hours=1))
    rows = {r["text"]: r for r in await conn.fetch("SELECT * FROM facts")}
    assert rows["электрика — объявили торги"]["superseded_by"] == rows["электрика — торги закрыты"]["id"]
    assert rows["кровля — подрядчик выбран"]["valid_to"] is None        # другая работа — не закрыта
    current = await facts.list_facts(conn, "project", pid)
    assert sorted((f["slot_label"], f["text"]) for f in current) == [
        ("статус (Электрика)", "электрика — торги закрыты"), ("статус (кровля)", "кровля — подрядчик выбран")]


async def test_project_budget_conflicts_only_within_the_same_work(conn):
    """Чужое значение проекта ждёт решения владельца; противоречием считается только та же работа."""
    w = await seed(conn)
    pid = await project(conn, w)
    await say(conn, w.group, [(OWNER, "Евгений Тестов", "Решили по Северному: бюджет кровли 9 млн")])
    await run_with(conn, lambda job: {"commitments": [], "facts": [
        fact(1, "бюджет кровли 9 млн", "ПРОЕКТ", "Бюджет кровли 9 млн", slot="бюджет", slot_detail="кровля",
             project="Северный")]})
    roof = await conn.fetchval("SELECT id FROM facts")
    await say(conn, w.group, [(IVAN, "Иван Петров", "Решили по Северному: бюджет электрики 51 млн"),
                              (IVAN, "Иван Петров", "Решили по Северному: бюджет кровли теперь 10 млн")],
              start=T0 + timedelta(days=1), first_id=10)

    def reply(job):
        return {"commitments": [], "facts": [
            fact(1, "бюджет электрики 51 млн", "ПРОЕКТ", "Бюджет электрики 51 млн", slot="бюджет",
                 slot_detail="электрика", project="Северный"),
            fact(2, "бюджет кровли теперь 10 млн", "ПРОЕКТ", "Бюджет кровли 10 млн", slot="бюджет",
                 slot_detail="Кровля", project="Северный")]}
    await run_with(conn, reply, now=T0 + timedelta(days=1, hours=2))
    got = {r["text"]: r["conflicts_with"] for r in await conn.fetch("SELECT text, conflicts_with FROM facts WHERE status = 'proposed'")}
    assert got == {"Бюджет электрики 51 млн": None, "Бюджет кровли 10 млн": roof}
    assert [f["id"] for f in await facts.list_facts(conn, "project", pid)] == [roof]


async def test_person_event_goes_to_the_timeline_only(conn):
    w = await seed(conn)
    await say(conn, w.ivan_chat, [(IVAN, "Иван Петров", "Перенёс заглушку на новый адрес, я теперь директор")])
    await run_with(conn, lambda job: {"commitments": [], "facts": [
        fact(1, "Перенёс заглушку на новый адрес", "У1", "Перенёс заглушку на новый адрес", nature="event"),
        fact(1, "я теперь директор", "У1", "Директор", slot="должность", nature="state")]})
    current = await facts.list_facts(conn, "person", w.ivan)
    assert [f["text"] for f in current] == ["Директор"]
    event = await conn.fetchrow("SELECT * FROM facts WHERE nature = 'event'")
    assert (event["status"], event["slot"]) == ("active", None)
    entries, now_facts, _ = await pages_build._facts_from_base(conn, "person", w.ivan)
    assert [e.text for e in entries if e.text.startswith("событие")] == ["событие: Перенёс заглушку на новый адрес"]
    assert await facts.facts_in_force(conn, "person", w.ivan, T0.date()) and all(
        f["nature"] != "event" for f in await facts.facts_in_force(conn, "person", w.ivan, T0.date()))
    with owner():
        try:
            await facts.correct_fact(conn, event["id"], "Перенёс заглушку")
            raise AssertionError("событие не поправляется как действующее")
        except facts.FactsError as exc:
            assert exc.code == "not_current"


async def test_memory_health_counts(conn):
    w = await seed(conn)
    pid = await project(conn, w)
    with owner():
        await projects.create_project(conn, "Пустой проект", [])
    await say(conn, w.group, [(OWNER, "Евгений Тестов", "Решили по Северному: в litellm доступны модели openrouter и локальные")])
    await say(conn, w.ivan_chat, [(OWNER, "Евгений Тестов", "Решили по Северному: в litellm доступны модели из openrouter и локальные")],
              first_id=30)
    await run_with(conn, lambda job: {"commitments": [], "facts": [fact(
        1, "в litellm доступны модели" + (" из" if "из openrouter" in job["payload"]["input"] else ""), "ПРОЕКТ",
        "В litellm доступны модели" + (" из" if "из openrouter" in job["payload"]["input"] else "")
        + " openrouter и локальные", project="Северный", nature="state")]})
    got = await memory_health.overview(conn)
    assert got["memory_broken"] == 0 and got["memory_facts_current"] == 2
    assert got["memory_paraphrases"] == 1 and got["memory_project_outside"] == 1 and got["memory_empty_projects"] == 1
    # нарушенная ссылка: «сменено» указывает на отклонённый факт
    a, b = [r["id"] for r in await conn.fetch("SELECT id FROM facts ORDER BY id")]
    await conn.execute("UPDATE facts SET status = 'rejected' WHERE id = $1", b)
    await conn.execute("UPDATE facts SET superseded_by = $2, valid_to = valid_from WHERE id = $1", a, b)
    assert (await memory_health.overview(conn))["memory_broken"] == 1
    assert pid


async def test_stale_observation_refreshes_its_page(conn):
    w = await seed(conn)
    pid = await project(conn, w)
    await say(conn, w.group, [(OWNER, "Евгений Тестов", "Решили по Северному: пока сервис не работает")])
    await run_with(conn, lambda job: {"commitments": [], "facts": [
        fact(1, "пока сервис не работает", "ПРОЕКТ", "Пока сервис не работает", project="Северный")]})
    await conn.execute("UPDATE pages SET dirty = false, built_at = now() - interval '30 days' WHERE project_id = $1", pid)
    days = facts.OBSERVATION_DAYS + 1
    await conn.execute(f"UPDATE facts SET valid_from = CURRENT_DATE - {days}")
    assert await pages_build.mark_stale_observations(conn) == 1
    await conn.execute("UPDATE pages SET dirty = false, built_at = now() WHERE project_id = $1", pid)
    assert await pages_build.mark_stale_observations(conn) == 0         # уже собрана после — повтора нет
