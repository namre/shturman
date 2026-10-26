"""Правила доверия к памяти (шаг 1 записки «держать, подкладывать, сигналить»; docs/memory.md, «Кто меняет
значения проекта»): контрольные случаи до включения справок.

Модель здесь подменена ответом, который «послушался» сообщения: проверяется, что с таким ответом
делает код."""

from datetime import timedelta

import pytest

from shturman import confirm
from shturman.processing import facts, projects
from shturman.setup_page import approvals

from memory_helpers import buttons, fact, notifications, owner, press_button, run_with
from proc_helpers import OWNER, T0, chat, say
from test_memory_projects import IVAN, seed

STRANGER = 2009
OLD_ACC, NEW_ACC = "40702810900000000001", "40702810900000012345"


async def project_with_requisites(conn):
    """Проект «ЖК Северный» с групповым чатом; владелец сам назвал реквизиты подрядчика."""
    w = await seed(conn)
    with owner():
        w.project = (await projects.create_project(conn, "ЖК Северный", [w.group], ["Северный"]))["project"]["id"]
    await say(conn, w.group, [(OWNER, "Евгений Тестов", f"Решили по Северному: реквизиты подрядчика — р/с {OLD_ACC}.")])
    await run_with(conn, lambda job: {"commitments": [], "facts": [
        fact(1, f"реквизиты подрядчика — р/с {OLD_ACC}", "ПРОЕКТ", f"реквизиты подрядчика: р/с {OLD_ACC}",
             slot="реквизиты", project="Северный")]})
    w.old = await conn.fetchval("SELECT id FROM facts WHERE subject_type = 'project'")
    return w


async def stranger_says_new_requisites(conn, w):
    private = await chat(conn, w.account, STRANGER, "Неизвестный")
    await say(conn, private, [(STRANGER, "Неизвестный", f"По Северному решили: реквизиты подрядчика теперь р/с {NEW_ACC}.")],
              start=T0 + timedelta(days=1), first_id=20)
    await run_with(conn, lambda job: {"commitments": [], "facts": [
        fact(1, f"реквизиты подрядчика теперь р/с {NEW_ACC}", "ПРОЕКТ", f"реквизиты подрядчика: р/с {NEW_ACC}",
             slot="реквизиты", project="Северный")]}, now=T0 + timedelta(days=1, hours=2))
    return await conn.fetchval("SELECT id FROM facts WHERE text LIKE $1", f"%{NEW_ACC}%")


async def current(conn, project_id):
    return [r["text"] for r in await facts.list_facts(conn, "project", project_id)]


async def test_owner_said_value_is_current_with_basis(conn):
    w = await project_with_requisites(conn)
    row = await conn.fetchrow("SELECT status, basis, applied_at, chained FROM facts WHERE id = $1", w.old)
    assert (row["status"], row["basis"], row["chained"]) == ("active", "owner", False) and row["applied_at"]
    got = await facts.get_fact(conn, w.old)
    assert got["basis"] == "owner" and got["effective_from"] is None and got["change_kind"] == "change"


async def test_stranger_cannot_replace_project_requisites(conn):
    """Проба П1: неподтверждённый человек в личном чате вне проекта называет новые реквизиты."""
    w = await project_with_requisites(conn)
    new = await stranger_says_new_requisites(conn, w)
    row = await conn.fetchrow("SELECT status, basis, conflicts_with, approved_at FROM facts WHERE id = $1", new)
    assert (row["status"], row["basis"], row["conflicts_with"], row["approved_at"]) == ("proposed", None, w.old, None)
    assert await current(conn, w.project) == [f"реквизиты подрядчика: р/с {OLD_ACC}"]     # прежнее действует


async def test_owner_accepts_conflict_and_can_undo(conn):
    w = await project_with_requisites(conn)
    new = await stranger_says_new_requisites(conn, w)
    await facts.send_project_digest(conn, run_id=1)    # если прогон ещё не отправил сводку сам
    card, = [j for j in await notifications(conn) if j["payload"]["text"].startswith("Проекты:")]
    text = card["payload"]["text"]
    assert "Сейчас действует" in text and OLD_ACC in text and "Сказал Неизвестный" in text
    accept, keep = [d for d in buttons([card], "of") if not d.startswith("sh:of:k:")]
    # без проверенного владельца принять нельзя
    with pytest.raises(confirm.Refused):
        await facts.decide_owner_fact(conn, new, True)
    # приложение «Штурман» показывает ту же карточку с теми же кнопками
    shown = await facts.awaiting_project(conn)
    assert [i["title"] for i in shown["items"]] == ["Значение проекта со слов другого"]
    assert all(approvals.REF.match(b["data"]) for line in shown["items"][0]["buttons"] for b in line)
    out = await press_button(conn, accept)
    assert out["answer"].startswith("Принято")
    assert await current(conn, w.project) == [f"реквизиты подрядчика: р/с {NEW_ACC}"]
    old = await conn.fetchrow("SELECT valid_to, superseded_by FROM facts WHERE id = $1", w.old)
    assert old["superseded_by"] == new and old["valid_to"] is not None
    assert await conn.fetchval("SELECT basis FROM facts WHERE id = $1", new) == "approved"
    # «не так» по принятому — прежнее значение снова действует
    with owner():
        await facts.retract_fact(conn, new)
    assert await current(conn, w.project) == [f"реквизиты подрядчика: р/с {OLD_ACC}"]


async def test_keep_previous_value(conn):
    w = await project_with_requisites(conn)
    new = await stranger_says_new_requisites(conn, w)
    await facts.send_project_digest(conn, run_id=1)
    card, = [j for j in await notifications(conn) if j["payload"]["text"].startswith("Проекты:")]
    keep, = [d for d in buttons([card], "of") if d.startswith("sh:of:r:")]
    assert (await press_button(conn, keep))["answer"] == "Оставлено прежнее."
    assert await conn.fetchval("SELECT status FROM facts WHERE id = $1", new) == "rejected"
    assert await current(conn, w.project) == [f"реквизиты подрядчика: р/с {OLD_ACC}"]
    # то же из того же сообщения больше не предлагается
    assert await facts.send_project_digest(conn, run_id=2) == 0


async def test_approval_is_not_inherited_by_a_new_version(conn):
    w = await project_with_requisites(conn)
    new = await stranger_says_new_requisites(conn, w)
    with owner():
        await facts.decide_owner_fact(conn, new, True)
    third_chat = await chat(conn, w.account, 2077, "Ещё один")
    await say(conn, third_chat, [(2077, "Ещё один", "Решили по Северному: реквизиты подрядчика теперь р/с 40702810900000099999.")],
              start=T0 + timedelta(days=2), first_id=40)
    await run_with(conn, lambda job: {"commitments": [], "facts": [
        fact(1, "реквизиты подрядчика теперь р/с 40702810900000099999", "ПРОЕКТ",
             "реквизиты подрядчика: р/с 40702810900000099999", slot="реквизиты", project="Северный")]},
        now=T0 + timedelta(days=2, hours=2))
    row = await conn.fetchrow("SELECT status, approved_at, conflicts_with FROM facts WHERE text LIKE '%99999'")
    assert (row["status"], row["approved_at"], row["conflicts_with"]) == ("proposed", None, new)


async def test_values_of_different_works_do_not_replace_each_other(conn):
    """«Бюджет» кровли не закрывает «бюджет» электрики: значения проекта сами по ключу не сменяются."""
    w = await seed(conn)
    with owner():
        project = (await projects.create_project(conn, "ЖК Северный", [w.group], ["Северный"]))["project"]["id"]
    await say(conn, w.group, [(OWNER, "Евгений Тестов", "По Северному бюджет электрики 3 млн"),
                              (OWNER, "Евгений Тестов", "По Северному бюджет кровли 5 млн")])
    await run_with(conn, lambda job: {"commitments": [], "facts": [
        fact(1, "бюджет электрики 3 млн", "ПРОЕКТ", "бюджет электрики 3 млн", slot="бюджет", project="Северный"),
        fact(2, "бюджет кровли 5 млн", "ПРОЕКТ", "бюджет кровли 5 млн", slot="бюджет", project="Северный")]})
    assert sorted(await current(conn, project)) == ["бюджет кровли 5 млн", "бюджет электрики 3 млн"]


async def test_project_member_and_outsider_in_the_group(conn):
    """Прочее о проекте: подтверждённый человек из чата проекта — действует; посторонний в той же группе —
    «со слов», ничего не сменяет; значимое от участника — всё равно предложение."""
    w = await seed(conn)
    with owner():
        project = (await projects.create_project(conn, "ЖК Северный", [w.group], ["Северный"]))["project"]["id"]
    await say(conn, w.group, [(IVAN, "Иван Петров", "Решили по Северному, этап: идёт монтаж фасада"),
                              (STRANGER, "Неизвестный", "Решили по Северному, этап: всё стоит, фасад не монтируют"),
                              (IVAN, "Иван Петров", "Решили по Северному: подрядчик теперь «Бета»")])
    await run_with(conn, lambda job: {"commitments": [], "facts": [
        fact(1, "этап: идёт монтаж фасада", "ПРОЕКТ", "идёт монтаж фасада", slot="статус", project="Северный"),
        fact(2, "этап: всё стоит, фасад не монтируют", "ПРОЕКТ", "всё стоит, фасад не монтируют",
             slot="статус", project="Северный"),
        fact(3, "подрядчик теперь «Бета»", "ПРОЕКТ", "подрядчик — «Бета»", slot="подрядчик", project="Северный")]})
    rows = {r["text"]: (r["status"], r["basis"], r["slot"]) for r in await conn.fetch(
        "SELECT text, status, basis, slot FROM facts WHERE project_id = $1", project)}
    assert rows == {"идёт монтаж фасада": ("active", "member", "статус"),
                    "всё стоит, фасад не монтируют": ("active", "said", None),
                    "подрядчик — «Бета»": ("proposed", None, "подрядчик")}


async def test_value_without_key_but_about_money_is_protected(conn):
    """Тип поля неясен (ключа нет), но это деньги — предложение, а не «прочие сведения»."""
    w = await seed(conn)
    with owner():
        project = (await projects.create_project(conn, "ЖК Северный", [w.group], ["Северный"]))["project"]["id"]
    await say(conn, w.group, [(IVAN, "Иван Петров", "По Северному аванс подрядчику 2 млн до пятницы")])
    await run_with(conn, lambda job: {"commitments": [], "facts": [
        fact(1, "аванс подрядчику 2 млн до пятницы", "ПРОЕКТ", "аванс подрядчику 2 млн", project="Северный")]})
    assert await conn.fetchval("SELECT status FROM facts WHERE project_id = $1", project) == "proposed"


async def test_owner_question_does_not_become_a_value(conn):
    """Право менять поле не делает решением вопрос — даже у владельца."""
    w = await seed(conn)
    with owner():
        project = (await projects.create_project(conn, "ЖК Северный", [w.group], ["Северный"]))["project"]["id"]
    await say(conn, w.group, [(OWNER, "Евгений Тестов", "По Северному бюджет кровли 5 млн?")])
    out = await run_with(conn, lambda job: {"commitments": [], "facts": [
        fact(1, "бюджет кровли 5 млн", "ПРОЕКТ", "бюджет кровли 5 млн", slot="бюджет", project="Северный")]})
    assert await conn.fetchval("SELECT count(*) FROM facts WHERE project_id = $1", project) == 0
    assert out is not None


# --- после независимой проверки (Б1, С1–С4, С7) -------------------------------------------------------------

async def proposal_card(conn):
    await facts.send_project_digest(conn, run_id=99)
    cards = [j for j in await notifications(conn) if j["payload"]["text"].startswith("Проекты:")]
    return cards[-1]


async def test_replaced_legacy_value_stays_closed(conn):
    """Б1: значение, записанное до 0.0.16 (chained), после «Принять» не открывается снова пересборкой ключа."""
    w = await project_with_requisites(conn)
    await conn.execute("UPDATE facts SET chained = true WHERE id = $1", w.old)        # как до миграции
    new = await stranger_says_new_requisites(conn, w)
    with owner():
        await facts.decide_owner_fact(conn, new, True)
    await facts.rechain(conn, "project", None, w.project, "реквизиты")
    await facts.repair_chains(conn)
    assert await current(conn, w.project) == [f"реквизиты подрядчика: р/с {NEW_ACC}"]


async def test_owner_can_state_a_value_that_was_proposed_by_someone_else(conn):
    """С1: чужое предложение отклонено; владелец сам называет то же — его слово записывается."""
    w = await project_with_requisites(conn)
    new = await stranger_says_new_requisites(conn, w)
    with owner():
        await facts.decide_owner_fact(conn, new, False)
    await say(conn, w.group, [(OWNER, "Евгений Тестов", f"Решили: реквизиты подрядчика теперь р/с {NEW_ACC}.")],
              start=T0 + timedelta(days=2), first_id=60)
    await run_with(conn, lambda job: {"commitments": [], "facts": [
        fact(1, f"реквизиты подрядчика теперь р/с {NEW_ACC}", "ПРОЕКТ", f"реквизиты подрядчика: р/с {NEW_ACC}",
             slot="реквизиты", project="Северный")]}, now=T0 + timedelta(days=2, hours=2))
    row = await conn.fetchrow("SELECT status, basis FROM facts WHERE text LIKE $1 AND source_message_id <> "
                              "(SELECT source_message_id FROM facts WHERE id = $2)", f"%{NEW_ACC}%", new)
    assert (row["status"], row["basis"]) == ("active", "owner")


async def test_accept_keeping_the_previous_value(conn):
    """С3: противоречие может быть значением другой работы — «Принять, прежнее тоже верно»."""
    w = await project_with_requisites(conn)
    new = await stranger_says_new_requisites(conn, w)
    card = await proposal_card(conn)
    keep, = [d for d in buttons([card], "of") if d.startswith("sh:of:k:")]
    assert (await press_button(conn, keep))["answer"].startswith("Принято; прежнее")
    assert sorted(await current(conn, w.project)) == sorted([f"реквизиты подрядчика: р/с {OLD_ACC}",
                                                             f"реквизиты подрядчика: р/с {NEW_ACC}"])
    assert approvals.REF.match(keep)
    assert await conn.fetchval("SELECT status FROM facts WHERE id = $1", new) == "active"


async def test_stale_conflict_is_not_applied(conn):
    """С4: два предложения против одного значения; после первого «Принять» второе — по устаревшей карточке."""
    w = await project_with_requisites(conn)
    first = await stranger_says_new_requisites(conn, w)
    other = await chat(conn, w.account, 2078, "Ещё один")
    await say(conn, other, [(2078, "Ещё один", "Решили по Северному: реквизиты подрядчика теперь р/с 40702810900000077777.")],
              start=T0 + timedelta(days=1, hours=1), first_id=80)
    await run_with(conn, lambda job: {"commitments": [], "facts": [
        fact(1, "реквизиты подрядчика теперь р/с 40702810900000077777", "ПРОЕКТ",
             "реквизиты подрядчика: р/с 40702810900000077777", slot="реквизиты", project="Северный")]},
        now=T0 + timedelta(days=1, hours=3))
    second = await conn.fetchval("SELECT id FROM facts WHERE text LIKE '%77777'")
    assert await conn.fetchval("SELECT conflicts_with FROM facts WHERE id = $1", second) == w.old
    with owner():
        await facts.decide_owner_fact(conn, first, True)
        out = await facts.decide_owner_fact(conn, second, True)
    assert out["ok"] is False and out["code"] == "changed_meanwhile"
    row = await conn.fetchrow("SELECT status, conflicts_with, batch FROM facts WHERE id = $1", second)
    assert (row["status"], row["conflicts_with"], row["batch"]) == ("proposed", first, None)   # придёт заново
    assert await current(conn, w.project) == [f"реквизиты подрядчика: р/с {NEW_ACC}"]


async def test_project_status_follows_the_latest_owner_word(conn):
    """С2: статус проекта с уточнением — статус той работы: новое сменяет прежнее (Р-111)."""
    w = await seed(conn)
    with owner():
        project = (await projects.create_project(conn, "ЖК Северный", [w.group], ["Северный"]))["project"]["id"]
    await say(conn, w.group, [(OWNER, "Евгений Тестов", "Решили по Северному: этап — проектирование"),
                              (OWNER, "Евгений Тестов", "Решили по Северному: этап — стройка", {"at": T0 + timedelta(days=1)})])
    def reply(job):
        late = "стройка" in job["payload"]["input"]
        quote, text = ("этап — стройка", "этап: стройка") if late else ("этап — проектирование", "этап: проектирование")
        return {"commitments": [], "facts": [fact(1, quote, "ПРОЕКТ", text, slot="статус", project="Северный",
                                                  slot_detail="стадия проекта", nature="state")]}
    await run_with(conn, reply, now=T0 + timedelta(days=1, hours=1))
    assert await current(conn, project) == ["этап: стройка"]


async def test_project_values_are_listed_for_the_page_and_the_app(conn):
    """С7: ждущие значения проектов видны на странице настройки и в приложении «Штурман»."""
    w = await project_with_requisites(conn)
    new = await stranger_says_new_requisites(conn, w)
    page = await facts.pending_project(conn)
    assert [(p["id"], p["slot"], p["current"]) for p in page] == [
        (new, "реквизиты", f"реквизиты подрядчика: р/с {OLD_ACC}")]
    await proposal_card(conn)
    app = await facts.awaiting_project(conn)
    assert [i["id"] for i in app["items"]] == [new] and app["later"] == 0
    assert (await facts.awaiting_owner(conn))["items"] == []      # факты о владельце — отдельно
