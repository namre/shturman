"""Вид страницы проекта (Р-112, разбор памяти пилота: проект-«свалка»).

* модель называет упоминание проектом, организацией или подразделением; ответ без вида — как раньше;
* предложение получает вид, которым модель чаще называла упоминания, — и так же подписано владельцу;
* статус, срок, бюджет, цена и площадь работы к организации и подразделению по ключу не пишутся;
* вид заведённого меняет только владелец; страница перерисовывается с новым видом."""

from datetime import timedelta

import pytest

from shturman import confirm
from shturman.processing import extract, pages, pipeline, projects

from memory_helpers import buttons, fact, notifications, owner, press_button, run_with
from pages_helpers import build
from proc_helpers import OWNER, T0, say
from test_memory_projects import IVAN, digest_texts, seed

EP = extract.Episode(chat_id=1, messages=[extract.Msg(
    id=1, chat_id=1, sent_at=T0, sender_peer_id=None, sender_name=None, is_outgoing=False,
    text="ГК Альфа и отдел снабжения ведут закупку электрики для ЖК Северный")])


def test_mentions_carry_the_kind_the_model_named():
    found, dropped = extract.validate_projects({"projects": [
        "ЖК Северный",                                          # строка — как до 0.0.17
        {"title": "ГК Альфа", "kind": "organization"},
        {"title": "отдел снабжения", "kind": "unit"},
        {"title": "закупку электрики", "kind": "strange"},      # незнакомый вид — не назван
        {"kind": "project"},                                     # без названия
        {"title": "Омега", "kind": "project"},                   # нет в тексте
    ]}, EP)
    assert [(m.title, m.kind) for m in found] == [
        ("ЖК Северный", None), ("ГК Альфа", "organization"), ("отдел снабжения", "unit"), ("закупку электрики", None)]
    assert dropped == {"malformed": 1, "ungrounded": 1, "over_limit": 0}


def test_prompt_lists_organizations_apart_from_projects():
    text = extract.build_extract_input(EP, extract.speaker_labels(EP), None, chat_kind="личный чат",
                                       projects=["ЖК Северный"], organizations=["ГК Альфа", "  "])
    assert "Проекты владельца: ЖК Северный." in text
    assert "Организации и подразделения владельца: ГК Альфа." in text
    assert "organization" in extract.EXTRACT_INSTRUCTIONS and "organization" in extract.EXTRACT_SCHEMA["description"]


async def test_organization_is_proposed_and_signed_as_such(conn):
    w = await seed(conn)
    await say(conn, w.ivan_chat, [(IVAN, "Иван Петров", "ГК Альфа прислала договор"),
                                  (IVAN, "Иван Петров", "В ГК Альфа сменился директор")])
    reply = lambda job: {"commitments": [], "facts": [],  # noqa: E731
                         "projects": [{"title": "ГК Альфа", "kind": "organization"}]}
    await run_with(conn, reply)
    await say(conn, w.ivan_chat, [(IVAN, "Иван Петров", "ГК Альфа ждёт оплату 120 тыс до пятницы")], start=T0 + timedelta(days=2), first_id=10)
    await run_with(conn, lambda job: {"commitments": [], "facts": [], "projects": ["ГК Альфа"]},   # без вида — тоже
                   now=T0 + timedelta(days=2, hours=1))
    assert sorted(r["kind"] or "-" for r in await conn.fetch("SELECT kind FROM project_mentions")) == [
        "-", "organization", "organization"]
    project = await conn.fetchrow("SELECT id, kind, status FROM projects")
    assert (project["kind"], project["status"]) == ("organization", "proposed")

    sent = digest_texts(await notifications(conn), "Проекты, организации и подразделения из переписки")
    assert len(sent) == 1 and "1. организация: ГК Альфа — упоминаний" in sent[0]["payload"]["text"]
    labels = [b["text"] for row in sent[0]["payload"]["buttons"] for b in row]
    assert labels == ["1 — завести организацию", "1 — не нужно"]
    waiting = await projects.awaiting_owner(conn)
    assert waiting["items"][0]["text"].startswith("организация: ГК Альфа")
    accept, _ = buttons(sent, "pj")
    assert (await press_button(conn, accept))["answer"] == "Организация заведена."
    assert await projects.active_titles(conn) == []
    assert await projects.active_titles(conn, kinds=("organization", "unit")) == ["ГК Альфа"]


async def test_work_values_are_not_keyed_on_an_organization(conn):
    """Статус и бюджет работы, которые модель отнесла к компании, пишутся без ключа и ничего не сменяют;
    адрес компании — её значение."""
    w = await seed(conn)
    with owner():
        org = (await projects.create_project(conn, "ГК Альфа", kind="organization"))["project"]["id"]
    await say(conn, w.ivan_chat, [(OWNER, "Евгений Тестов", "ГК Альфа: закупка электрики на согласовании, бюджет 3 млн. "
                                                          "Офис ГК Альфа теперь на Ленина 5")])
    quote_status, quote_budget, quote_address = "закупка электрики на согласовании", "бюджет 3 млн", "теперь на Ленина 5"
    await run_with(conn, lambda job: {"commitments": [], "facts": [
        fact(1, quote_status, "ПРОЕКТ", "Закупка электрики на согласовании", slot="статус", project="ГК Альфа",
             slot_detail="закупка электрики"),
        fact(1, quote_budget, "ПРОЕКТ", "Бюджет 3 млн", slot="бюджет", project="ГК Альфа"),
        fact(1, quote_address, "ПРОЕКТ", "Офис на Ленина 5", slot="адрес", project="ГК Альфа")]})
    rows = await conn.fetch("SELECT text, slot, slot_detail FROM facts WHERE project_id = $1 ORDER BY id", org)
    assert [tuple(r) for r in rows] == [("Закупка электрики на согласовании", None, None), ("Бюджет 3 млн", None, None),
                                        ("Офис на Ленина 5", "адрес", None)]
    stats = pipeline._loads(await conn.fetchval("SELECT stats FROM processing_runs ORDER BY id DESC LIMIT 1"))
    assert stats["results"]["facts_org_work_unkeyed"] == 2


async def test_only_the_owner_changes_the_kind_and_the_page_shows_it(conn, config):
    w = await seed(conn)
    with owner():
        pid = (await projects.create_project(conn, "ГК Альфа", [w.group]))["project"]["id"]
    assert (await projects.get_project(conn, pid))["kind"] == "project"
    with pytest.raises(confirm.Refused) as refused:
        await projects.set_project_kind(conn, pid, "organization")
    assert refused.value.code == "owner_required"
    with owner():
        with pytest.raises(projects.ProjectsError) as bad:
            await projects.set_project_kind(conn, pid, "company")
        assert bad.value.code == "bad_kind"
        out = await projects.set_project_kind(conn, pid, "organization")
        assert out["changed"] is True and out["project"]["kind"] == "organization"
        assert (await projects.set_project_kind(conn, pid, "organization"))["changed"] is False
    await build(conn, config)
    path = await conn.fetchval("SELECT path FROM pages WHERE project_id = $1", pid)
    page = pages.parse(pages.read_page(config.pages_dir, path))
    assert (page.type, page.kind) == ("project", "organization")
    assert "kind: organization\n" in pages.render(page)
    with owner():
        await projects.set_project_kind(conn, pid, "project")
    await build(conn, config)
    text = pages.read_page(config.pages_dir, path)
    assert "kind:" not in text and pages.parse(text).kind == ""
