"""Экран «Память ассистента» на странице настройки: страницы о людях, заметки владельца, решения.

Главное, что здесь проверяется: со страницы владелец правит свои заметки и решает предложенное
сразу — тем же кодом, что нажатие в боте согласований, — а внутренний API, доступный ассистенту,
от этого не меняется и по-прежнему ждёт нажатия в боте."""

import json
import re
import sys
from importlib import resources
from pathlib import Path

_PROCESSING = str(Path(__file__).resolve().parents[1] / "processing")
if _PROCESSING not in sys.path:
    sys.path.insert(0, _PROCESSING)

from setup_fakes import stand  # noqa: E402, F401, I001 — фикстура; первым: добавляет пути
from pages_helpers import blocks_of, build_with, commitment, ivan_owes_estimate, log, seed, statement  # noqa: E402

from proc_helpers import chat, say  # noqa: E402

from shturman.processing import facts, pages, pages_service, projects  # noqa: E402
from shturman.setup_page import audit, memory  # noqa: E402

MODULES = ("shturman.api_core", "shturman.executor.service", "shturman.ingest_api", "shturman.tg.service",
           "shturman.processing.service", "shturman.processing.pages_service", "shturman.processing.memory_service",
           "shturman.setup_page.service")
NAMES = ("Иван", "Петров", "Мария", "Сидорова", "Тестов", "смет", "акт сверки", "фасад", "19:00", "Береговой", "Объект",
         "Склад", "директор", "коротко")


def quiet(monkeypatch):
    """Фоновая сборка страниц — только по «будильнику», без долгих пауз."""
    monkeypatch.setattr(pages_service, "POLL_SECONDS", 3600.0)
    monkeypatch.setattr(pages_service, "WAKE_DELAY", 0.05)


async def world(stand, conn, monkeypatch):
    """Вошедший владелец; страница об Иване собрана со сводкой и принятым обязательством."""
    quiet(monkeypatch)
    s = await stand(modules=MODULES)
    await s.page.login(conn)
    w = await seed(conn)
    w.estimate = await ivan_owes_estimate(conn, w)
    await build_with(conn, s.config, lambda job: [
        statement("Ведёт фасады на объекте [корпус 2]", [w.ivan_msgs[0], w.ivan_msgs[2]]),
        statement("Монтаж — в октябре", [w.ivan_msgs[2]], contradiction=True)])
    return s, w


async def audit_rows(conn):
    return [(r["action"], r["outcome"], r["detail"]) for r in
            await conn.fetch("SELECT action, outcome, detail FROM setup_audit WHERE action LIKE 'memory.%' ORDER BY id")]


def no_names(rows) -> None:
    dumped = json.dumps(rows, ensure_ascii=False)
    assert not any(word in dumped for word in NAMES), dumped


# --- разбор блоков -----------------------------------------------------------------------------------

def test_blocks_become_plain_text_without_links():
    item = memory.plain("Сказал \\[важно\\] и \\| ещё [сообщение](msg:5) [сообщение](msg:7) (сказал собеседник)")
    assert item == {"text": "Сказал [важно] и | ещё", "sources": 2, "origin": "сказал собеседник"}
    # экранированная «ссылка» — чужой текст, а не источник
    assert memory.plain("\\[сообщение\\](msg:9)")["sources"] == 0

    summary = memory.summary_items(pages.summary_block(
        [{"text": "Ведёт [фасады]", "sources": [3], "origin": "other"},
         {"text": "Монтаж в октябре", "sources": [4], "origin": "model", "disputed": True}], not_updated=True))
    assert summary == [
        {"text": "Сводка не обновлена: модель не дала пригодного ответа, показана прежняя.", "sources": 0,
         "origin": None, "disputed": False, "note": True},
        {"text": "Ведёт [фасады]", "sources": 1, "origin": "сказал собеседник", "disputed": False, "note": False},
        {"text": "Монтаж в октябре", "sources": 1, "origin": "вывела модель", "disputed": True, "note": False}]
    assert memory.summary_items(pages.NO_SUMMARY)[0]["text"] == "Сводки пока нет."

    table = pages.commitments_block([{"what": pages.md_inline("смета | фасады"), "due": "2026-10-09",
                                      "status": "ждём", "message_id": 3}])
    assert memory.commitment_rows(table) == [{"what": "смета | фасады", "due": "2026-10-09", "status": "ждём"}]
    assert memory.commitment_rows(pages.NO_COMMITMENTS) == []

    line = pages.timeline_line("2026-10-06", "обязательство (Иван → вам): смета", [3, 4], "other", "c1")
    assert memory.timeline_items(line + "\n") == [
        {"day": "2026-10-06", "text": "обязательство (Иван → вам): смета", "sources": 2, "origin": "сказал собеседник"}]


# --- доступ ------------------------------------------------------------------------------------------

async def test_memory_routes_need_a_session_and_internal_tokens_do_not_open_them(stand, conn, monkeypatch):
    s, w = await world(stand, conn, monkeypatch)
    guest = s.browser()
    for path in ("/memory/pages", f"/memory/pages/{w.ivan}", "/memory/pending"):
        assert (await guest.get(path)).status_code == 401, path
        # токен внутреннего API входом на страницу не служит
        assert (await s.api.get("/shturman-setup/api" + path)).status_code in (401, 403), path
    for method, path in (("PUT", f"/memory/pages/{w.ivan}/owner-block"), ("POST", f"/memory/pending/pages/{w.maria}"),
                         ("POST", "/memory/pending/commitments/1")):
        got = await guest.send(method, path, {"text": "взлом", "accept": True})
        assert got.status_code == 401 and got.json()["code"] == "unauthenticated", (method, path)
    # запрос с соседнего порта (дашборд) отвергается даже с ключом сессии
    foreign = await s.page.http.put(f"/shturman-setup/api/memory/pages/{w.ivan}/owner-block", json={"text": "x"},
                                    headers=s.page.headers(Origin="http://test:9119", **{"Sec-Fetch-Site": "same-site"}))
    assert foreign.status_code == 403
    assert await audit_rows(conn) == []
    assert blocks_of((s.config.pages_dir / (await conn.fetchval(
        "SELECT path FROM pages WHERE person_id = $1", w.ivan))).read_text(encoding="utf-8")).owner == "\n"


# --- страницы ----------------------------------------------------------------------------------------

async def test_pages_are_listed_searched_and_shown_as_plain_text(stand, conn, monkeypatch):
    s, w = await world(stand, conn, monkeypatch)
    listed = (await s.page.get("/memory/pages")).json()
    assert [(p["person_id"], p["title"], p["flags"]) for p in listed["pages"]] == [(w.ivan, "Иван Петров", [])]
    assert listed["pages"][0]["updated"]

    by_name = (await s.page.get("/memory/pages", params={"q": "ива"})).json()["pages"]
    assert [(p["person_id"], p["match"]) for p in by_name] == [(w.ivan, "head")]
    by_text = (await s.page.get("/memory/pages", params={"q": "фасадам"})).json()["pages"]
    assert [p["person_id"] for p in by_text] == [w.ivan] and by_text[0]["match"] in ("summary", "commitments", "timeline")
    assert "«" in by_text[0]["snippet"] and "msg:" not in by_text[0]["snippet"]
    assert (await s.page.get("/memory/pages", params={"q": "ничегоподобного"})).json()["pages"] == []

    shown = (await s.page.get(f"/memory/pages/{w.ivan}")).json()
    assert shown["title"] == "Иван Петров" and shown["editable"] and shown["written"] and shown["problem"] is None
    assert shown["owner"] == ""
    assert shown["summary"] == [
        {"text": "Ведёт фасады на объекте [корпус 2]", "sources": 2, "origin": "сказал собеседник",
         "disputed": False, "note": False},
        {"text": "Монтаж — в октябре", "sources": 1, "origin": "сказал собеседник", "disputed": True, "note": False}]
    assert shown["commitments"] == [{"what": "прислать смету по фасадам", "due": "2026-10-09", "status": "ждём"}]
    assert [(t["day"], t["sources"], t["origin"]) for t in shown["timeline"]] == [("2026-10-06", 1, "сказал собеседник")]
    assert "прислать смету по фасадам" in shown["timeline"][0]["text"]
    raw = json.dumps(shown, ensure_ascii=False)
    assert "msg:" not in raw and "<!--" not in raw and "](" not in raw

    assert (await s.page.get(f"/memory/pages/{w.maria}")).status_code == 404      # у Марии страницы нет
    assert (await s.page.get("/memory/pages/999999")).status_code == 404

    # сломанная вручную разметка: страница заморожена, заметки не правятся
    path = s.config.pages_dir / await conn.fetchval("SELECT path FROM pages WHERE person_id = $1", w.ivan)
    path.write_text(path.read_text(encoding="utf-8").replace(pages.MARKERS["timeline"] + "\n", ""), encoding="utf-8")
    frozen = (await s.page.get(f"/memory/pages/{w.ivan}")).json()
    assert frozen["problem"] and not frozen["editable"] and not frozen["written"]
    refused = await s.page.put(f"/memory/pages/{w.ivan}/owner-block", {"text": "ещё"})
    assert refused.status_code == 409 and "не обновляется" in refused.json()["error"]
    assert await audit_rows(conn) == []


async def test_owner_block_is_written_at_once_by_the_owner_path(stand, conn, monkeypatch, own_bot, approvals):
    """Со своим ботом согласований внутренний API ждёт нажатия; страница — нет: это сам владелец.
    Запись — тем же кодом, отдельным коммитом «Правка владельца»."""
    s, w = await world(stand, conn, monkeypatch)
    path = s.config.pages_dir / await conn.fetchval("SELECT path FROM pages WHERE person_id = $1", w.ivan)

    saved = await s.page.put(f"/memory/pages/{w.ivan}/owner-block", {"text": "Не писать после 19:00.\nРешает сам."})
    assert saved.status_code == 200, saved.text
    assert saved.json() == {"ok": True, "changed": True, "saved_to_history": True}
    assert await approvals.pending() == 0                         # карточки в боте нет
    assert blocks_of(path.read_text(encoding="utf-8")).owner == "Не писать после 19:00.\nРешает сам.\n\n"
    assert log(s.config)[0] == ("Владелец", "Правка владельца: 1 страница (из кабинета)")
    assert (await s.page.get(f"/memory/pages/{w.ivan}")).json()["owner"] == "Не писать после 19:00.\nРешает сам."
    again = await s.page.put(f"/memory/pages/{w.ivan}/owner-block", {"text": "Не писать после 19:00.\nРешает сам."})
    assert again.json()["changed"] is False

    # те же отказы, что у внутреннего API, и ни один ничего не записал
    before = path.read_bytes()
    for bad, message in (({"text": "ок\n<!-- timeline: взлом -->\n- строка"}, memory.HAS_MARKER),
                         ({"text": "<!--owner-->"}, memory.HAS_MARKER),
                         ({"text": "я" * 20_001}, memory.TOO_LONG),
                         ({"text": 5}, "поле text: нужна строка"), ({}, "поле text: нужна строка")):
        refused = await s.page.put(f"/memory/pages/{w.ivan}/owner-block", bad)
        assert refused.status_code == 400 and refused.json()["error"] == message, bad
    assert memory.HAS_MARKER == ("В тексте не должно быть меток блоков страницы "
                                 "(<!-- summary …, owner, commitments, decisions, facts, timeline).")
    long_ok = await s.page.put(f"/memory/pages/{w.ivan}/owner-block", {"text": "я" * 20_000})
    assert long_ok.status_code == 200
    assert (await s.page.put(f"/memory/pages/{w.maria}/owner-block", {"text": "x"})).status_code == 404
    assert path.read_bytes() != before
    cleared = await s.page.put(f"/memory/pages/{w.ivan}/owner-block", {"text": ""})
    assert cleared.status_code == 200 and blocks_of(path.read_text(encoding="utf-8")).owner == "\n"

    rows = await audit_rows(conn)
    assert rows == [("memory.owner_block", "ok", f"запись о человеке № {w.ivan}; знаков: 34"),
                    ("memory.owner_block", "ok", f"запись о человеке № {w.ivan}; знаков: 34; без изменений"),
                    ("memory.owner_block", "ok", f"запись о человеке № {w.ivan}; знаков: 20000"),
                    ("memory.owner_block", "ok", f"запись о человеке № {w.ivan}; очищены")]
    no_names(rows)
    key = (await s.page.get("/overview")).json()["audit_key"]
    assert any(r["action"] == "memory.owner_block" for r in key)          # правку заметок не вытеснить из вида

    # внутренний API не изменился: та же правка от держателя токена ждёт нажатия в боте
    waiting = await s.api.put(f"/api/pages/{w.ivan}/owner-block", json={"text": "от ассистента"})
    action = approvals.waiting(waiting)
    assert blocks_of(path.read_text(encoding="utf-8")).owner == "\n"
    assert (await approvals.press(action))["answer"] == "Сделано."
    assert blocks_of(path.read_text(encoding="utf-8")).owner == "от ассистента\n\n"
    # и под префиксом страницы маршрутов внутреннего API нет
    assert (await s.page.get(f"/pages/{w.ivan}")).status_code == 404


async def test_pending_pages_and_commitments_are_decided_from_the_page(stand, conn, monkeypatch, own_bot, approvals):
    s, w = await world(stand, conn, monkeypatch)
    proposed = await commitment(conn, w.maria_chat, w.maria_msgs[0], debtor=w.maria_peer, creditor=w.owner_peer,
                                direction="owed_to_owner", what="подписать акт сверки", accept=False,
                                due_expression="в понедельник", quote="Акт сверки подпишу в понедельник.")
    second = await commitment(conn, w.maria_chat, w.maria_msgs[0], debtor=w.owner_peer, creditor=w.maria_peer,
                              direction="owner_owes", what="прислать реквизиты", accept=False)
    await conn.execute("INSERT INTO page_proposals (person_id, reason) VALUES ($1, '{\"messages\": 25}'::jsonb)", w.maria)

    got = (await s.page.get("/memory/pending")).json()
    assert got["total"] == 3
    assert got["pages"] == [{"person_id": w.maria, "name": "Мария Сидорова", "reason": "сообщений за 30 дн.: 25"}]
    by_id = {c["id"]: c for c in got["commitments"]}
    assert by_id[proposed]["who"] == "Мария Сидорова → вам" and by_id[proposed]["what"] == "подписать акт сверки"
    assert by_id[proposed]["due"] == "Срок: «в понедельник» — дата не определена"
    assert by_id[proposed]["quote"] == "Акт сверки подпишу в понедельник."
    assert re.fullmatch(r"[0-9a-f]{64}", by_id[proposed]["fingerprint"])
    assert "msg:" not in json.dumps(got, ensure_ascii=False)

    # принять — с отпечатком того, что владелец видел
    assert (await s.page.post(f"/memory/pending/commitments/{proposed}", {"accept": True})).status_code == 400
    stale = await s.page.post(f"/memory/pending/commitments/{proposed}", {"accept": True, "fingerprint": "0" * 64})
    assert stale.status_code == 409 and stale.json()["code"] == "changed_meanwhile"
    assert (await s.page.post(f"/memory/pending/commitments/{proposed}", {"accept": "да"})).status_code == 400
    yes = await s.page.post(f"/memory/pending/commitments/{proposed}",
                            {"accept": True, "fingerprint": by_id[proposed]["fingerprint"]})
    assert yes.status_code == 200 and yes.json() == {"ok": True, "accepted": True}
    row = await conn.fetchrow("SELECT status, approved_via FROM commitments WHERE id = $1", proposed)
    assert (row["status"], row["approved_via"]) == ("open", "setup")
    assert await approvals.pending() == 0
    actor = await conn.fetchval("SELECT actor FROM commitment_events WHERE commitment_id = $1 ORDER BY id DESC", proposed)
    assert actor == "owner"
    no = await s.page.post(f"/memory/pending/commitments/{second}", {"accept": False})
    assert no.status_code == 200 and await conn.fetchval("SELECT status FROM commitments WHERE id = $1", second) == "rejected"
    again = await s.page.post(f"/memory/pending/commitments/{second}", {"accept": False})
    assert again.status_code == 409
    assert (await s.page.post("/memory/pending/commitments/999999", {"accept": False})).status_code == 404

    # страница о человеке: согласие — тем же кодом, что кнопка в боте
    assert (await s.page.post(f"/memory/pending/pages/{w.maria}", {})).status_code == 400
    assert (await s.page.post(f"/memory/pending/pages/{w.ivan}", {"accept": True})).status_code == 404
    page_yes = await s.page.post(f"/memory/pending/pages/{w.maria}", {"accept": True})
    assert page_yes.status_code == 200, page_yes.text
    assert await conn.fetchval("SELECT status FROM page_proposals WHERE person_id = $1", w.maria) == "accepted"
    assert await conn.fetchval("SELECT confirmed FROM people WHERE id = $1", w.maria) is True
    assert await conn.fetchval("SELECT 1 FROM pages WHERE person_id = $1", w.maria) == 1
    assert (await s.page.post(f"/memory/pending/pages/{w.maria}", {"accept": False})).status_code == 409
    assert (await s.page.get("/memory/pending")).json() == {"projects": [], "owner_facts": [], "pages": [],
                                                           "commitments": [], "total": 0}

    # отказ заводить страницу
    other = await conn.fetchval("INSERT INTO people (display_name) VALUES ('Олег') RETURNING id")
    await conn.execute("INSERT INTO page_proposals (person_id, reason) VALUES ($1, '{}'::jsonb)", other)
    assert (await s.page.post(f"/memory/pending/pages/{other}", {"accept": False})).status_code == 200
    assert await conn.fetchval("SELECT status FROM page_proposals WHERE person_id = $1", other) == "rejected"
    assert await conn.fetchval("SELECT 1 FROM pages WHERE person_id = $1", other) is None

    rows = await audit_rows(conn)
    assert rows == [("memory.commitment_accept", "ok", f"№ {proposed}"), ("memory.commitment_reject", "ok", f"№ {second}"),
                    ("memory.page_accept", "ok", f"запись о человеке № {w.maria}"),
                    ("memory.page_reject", "ok", f"запись о человеке № {other}")]
    no_names(rows)

    # внутренний API не изменился: согласие завести страницу от держателя токена ждёт нажатия
    third = await conn.fetchval("INSERT INTO people (display_name) VALUES ('Пётр') RETURNING id")
    await conn.execute("INSERT INTO page_proposals (person_id, reason) VALUES ($1, '{}'::jsonb)", third)
    approvals.waiting(await s.api.post(f"/api/pages/proposals/{third}", json={"accept": True}))
    assert await conn.fetchval("SELECT status FROM page_proposals WHERE person_id = $1", third) == "pending"


def test_every_memory_action_is_in_the_audit_list():
    source = (resources.files("shturman.setup_page") / "memory.py").read_text(encoding="utf-8")
    used = set(re.findall(r'"(memory\.[a-z_]+)"', source))
    assert used == {"memory.owner_block", "memory.page_accept", "memory.page_reject",
                    "memory.commitment_accept", "memory.commitment_reject", "memory.project_create",
                    "memory.project_chats", "memory.project_archive", "memory.project_accept", "memory.project_reject",
                    "memory.fact_retract", "memory.owner_fact_accept", "memory.owner_fact_reject",
                    "memory.profile_block"}
    assert used <= set(audit.ACTIONS)
    assert {"memory.owner_block", "memory.profile_block", "memory.owner_fact_accept"} <= audit.IMPORTANT


# --- проекты, профиль, факты ------------------------------------------------------------------------------

async def add_fact(conn, subject_type, message_id, text, *, person_id=None, project_id=None, kind="fact",
                   slot=None, status="active", origin="other", since="2026-10-06"):
    from datetime import date

    fact_id = await conn.fetchval(
        """INSERT INTO facts (subject_type, person_id, project_id, kind, slot, text, text_norm, valid_from, status,
                              origin, source_message_id, source_quote)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $6) RETURNING id""",
        subject_type, person_id, project_id, kind, slot, text, facts.text_norm(text), date.fromisoformat(since),
        status, origin, message_id)
    if status == "active":
        await facts.rechain(conn, subject_type, person_id, project_id, slot)
    return fact_id


async def group(conn, w):
    chat_id = await chat(conn, w.account, -100777, "Объект Береговой: рабочая", type_="private_supergroup", cls="channel")
    msgs = await say(conn, chat_id, [(2001, "Иван Петров", "Решили: фасад — керамогранит."),
                                     (2001, "Иван Петров", "Бюджет фасада 12 млн.")], first_id=50)
    return chat_id, msgs


async def test_projects_are_created_shown_changed_and_archived_from_the_page(stand, conn, monkeypatch, own_bot,
                                                                             approvals):
    s, w = await world(stand, conn, monkeypatch)
    room, said = await group(conn, w)
    chats = (await s.page.get("/memory/chats")).json()["chats"]
    kinds = {c["id"]: (c["title"], c["kind"]) for c in chats}
    assert kinds[room] == ("Объект Береговой: рабочая", "group") and kinds[w.ivan_chat][1] == "personal"

    assert (await s.page.post("/memory/projects", {"title": "  "})).status_code == 400
    assert (await s.page.post("/memory/projects", {"title": "Береговой", "chat_ids": [999999]})).status_code == 404
    assert (await s.page.post("/memory/projects", {"title": "Береговой", "chat_ids": ["x"]})).status_code == 400
    made = await s.page.post("/memory/projects", {"title": "ЖК Береговой", "chat_ids": [room], "aliases": ["Береговой"]})
    assert made.status_code == 200, made.text
    project = made.json()["project"]
    assert (project["title"], project["status"], project["chats"]) == ("ЖК Береговой", "active",
                                                                       ["Объект Береговой: рабочая"])
    assert await approvals.pending() == 0                         # со страницы — без карточки в боте
    again = await s.page.post("/memory/projects", {"title": "жк береговой"})
    assert again.status_code == 409 and again.json()["code"] == "exists"
    pid = project["id"]

    decision = await add_fact(conn, "project", said[0], "фасад — керамогранит", project_id=pid, kind="decision")
    budget = await add_fact(conn, "project", said[1], "12 млн", project_id=pid, slot="бюджет")
    shown = (await s.page.get(f"/memory/projects/{pid}")).json()
    assert shown["aliases"] == ["Береговой"] and shown["chats"] == [
        {"id": room, "title": "Объект Береговой: рабочая", "type": "private_supergroup"}]
    assert [(f["id"], f["slot"], f["text"], f["since"]) for f in shown["facts"]] == [
        (budget, "бюджет", "12 млн", "2026-10-06")]
    assert [(d["id"], d["text"]) for d in shown["decisions"]] == [(decision, "фасад — керамогранит")]
    assert shown["editable"] and shown["owner"] == "" and isinstance(shown["summary"], list)
    assert "msg:" not in json.dumps(shown, ensure_ascii=False)

    changed = await s.page.put(f"/memory/projects/{pid}/chats", {"chat_ids": [room, w.ivan_chat]})
    assert changed.status_code == 200 and changed.json()["changed"] is True
    assert {c["id"] for c in (await s.page.get(f"/memory/projects/{pid}")).json()["chats"]} == {room, w.ivan_chat}
    same = await s.page.put(f"/memory/projects/{pid}/chats", {"chat_ids": [room, w.ivan_chat]})
    assert same.json()["changed"] is False
    assert (await s.page.put(f"/memory/projects/{pid}/chats", {})).status_code == 400

    note = await s.page.put(f"/memory/projects/{pid}/owner-block", {"text": "Главный — Иван."})
    assert note.status_code == 200 and note.json()["changed"] is True
    assert (await s.page.get(f"/memory/projects/{pid}")).json()["owner"] == "Главный — Иван."
    assert log(s.config)[0] == ("Владелец", "Правка владельца: 1 страница (из кабинета)")
    refused = await s.page.put(f"/memory/projects/{pid}/owner-block", {"text": "<!-- facts -->"})
    assert refused.status_code == 400 and refused.json()["error"] == memory.HAS_MARKER

    assert (await s.page.post(f"/memory/facts/{budget}/retract")).status_code == 200
    assert (await s.page.get(f"/memory/projects/{pid}")).json()["facts"] == []
    assert (await s.page.post(f"/memory/facts/{budget}/retract")).status_code == 409
    assert (await s.page.post("/memory/facts/999999/retract")).status_code == 404

    assert (await s.page.post(f"/memory/projects/{pid}/archive")).status_code == 200
    listed = (await s.page.get("/memory/projects")).json()["projects"]
    assert [(p["id"], p["status"]) for p in listed] == [(pid, "archived")]
    assert (await s.page.post(f"/memory/projects/{pid}/archive")).status_code == 409
    assert (await s.page.get("/memory/projects/999999")).status_code == 404
    assert await approvals.pending() == 0

    rows = await audit_rows(conn)
    assert rows == [("memory.project_create", "ok", f"проект № {pid}; чатов: 1; других названий: 1"),
                    ("memory.project_chats", "ok", f"проект № {pid}; добавлено: 1; убрано: 0"),
                    ("memory.owner_block", "ok", f"проект № {pid}; знаков: 15"),
                    ("memory.fact_retract", "ok", f"факт № {budget}"),
                    ("memory.project_archive", "ok", f"проект № {pid}")]
    no_names(rows)

    # внутренний API не изменился: новый проект от держателя токена ждёт нажатия в боте
    approvals.waiting(await s.api.post("/api/projects", json={"title": "Склад на Окружной"}))
    assert await conn.fetchval("SELECT count(*) FROM projects") == 1


async def test_profile_rules_and_facts_about_the_owner(stand, conn, monkeypatch, own_bot, approvals):
    s, w = await world(stand, conn, monkeypatch)
    empty = (await s.page.get("/memory/profile")).json()
    assert (empty["facts"], empty["owner"], empty["editable"], empty["written"]) == ([], "", True, False)

    rules = "Отвечать коротко.\nПо выходным не беспокоить."
    saved = await s.page.put("/memory/profile/owner-block", {"text": rules})
    assert saved.status_code == 200 and saved.json()["changed"] is True
    profile = (await s.page.get("/memory/profile")).json()
    assert profile["owner"] == rules and profile["written"]
    assert log(s.config)[0] == ("Владелец", "Правка владельца: 1 страница (из кабинета)")
    assert await approvals.pending() == 0

    # факт о владельце — только с его «✓», и только в том виде, в каком его показали
    proposed = await add_fact(conn, "owner", w.ivan_msgs[1], "генеральный директор", slot="должность",
                              status="proposed", origin="owner")
    waiting = (await s.page.get("/memory/pending")).json()
    assert waiting["total"] == 1 and len(waiting["owner_facts"]) == 1
    item = waiting["owner_facts"][0]
    assert (item["id"], item["slot"], item["text"], item["since"]) == (proposed, "должность", "генеральный директор",
                                                                     "2026-10-06")
    assert item["quote"] == "генеральный директор" and re.fullmatch(r"[0-9a-f]{64}", item["fingerprint"])
    assert (await s.page.post(f"/memory/pending/owner-facts/{proposed}", {"accept": True})).status_code == 400
    stale = await s.page.post(f"/memory/pending/owner-facts/{proposed}", {"accept": True, "fingerprint": "0" * 64})
    assert stale.status_code == 409 and stale.json()["code"] == "changed_meanwhile"
    assert (await s.page.get("/memory/profile")).json()["facts"] == []
    yes = await s.page.post(f"/memory/pending/owner-facts/{proposed}",
                            {"accept": True, "fingerprint": item["fingerprint"]})
    assert yes.status_code == 200
    row = await conn.fetchrow("SELECT status, approved_via FROM facts WHERE id = $1", proposed)
    assert (row["status"], row["approved_via"]) == ("active", "setup")
    assert [(f["id"], f["slot"], f["text"]) for f in (await s.page.get("/memory/profile")).json()["facts"]] == [
        (proposed, "должность", "генеральный директор")]
    assert (await s.page.post(f"/memory/pending/owner-facts/{proposed}", {"accept": False})).status_code == 409

    other = await add_fact(conn, "owner", w.ivan_msgs[1], "живёт в Астрахани", status="proposed", origin="owner")
    assert (await s.page.post(f"/memory/pending/owner-facts/{other}", {"accept": False})).status_code == 200
    assert await conn.fetchval("SELECT status FROM facts WHERE id = $1", other) == "rejected"
    person_fact = await add_fact(conn, "person", w.ivan_msgs[0], "прораб", person_id=w.ivan, slot="должность")
    assert (await s.page.post(f"/memory/pending/owner-facts/{person_fact}",
                              {"accept": True, "fingerprint": "0" * 64})).status_code == 404

    # «Неверно» у факта профиля и у факта о человеке на его странице
    assert (await s.page.post(f"/memory/facts/{proposed}/retract")).status_code == 200
    assert (await s.page.get("/memory/profile")).json()["facts"] == []
    on_page = (await s.page.get(f"/memory/pages/{w.ivan}")).json()["facts"]
    assert [(f["id"], f["slot"], f["text"], f["origin"]) for f in on_page] == [
        (person_fact, "должность", "прораб", "сказал собеседник")]
    assert (await s.page.post(f"/memory/facts/{person_fact}/retract")).status_code == 200
    assert (await s.page.get(f"/memory/pages/{w.ivan}")).json()["facts"] == []

    rows = await audit_rows(conn)
    assert rows == [("memory.profile_block", "ok", f"профиль; знаков: {len(rules)}"),
                    ("memory.owner_fact_accept", "ok", f"факт № {proposed}"),
                    ("memory.owner_fact_reject", "ok", f"факт № {other}"),
                    ("memory.fact_retract", "ok", f"факт № {proposed}"),
                    ("memory.fact_retract", "ok", f"факт № {person_fact}")]
    no_names(rows)
    key = {r["action"] for r in (await s.page.get("/overview")).json()["audit_key"]}
    assert {"memory.profile_block", "memory.owner_fact_accept"} <= key

    # внутренний API не изменился: правила в профиле от держателя токена ждут нажатия в боте
    approvals.waiting(await s.api.put("/api/owner/profile/owner-block", json={"text": "от ассистента"}))
    assert (await s.page.get("/memory/profile")).json()["owner"] == rules


async def test_proposed_projects_are_decided_from_the_page(stand, conn, monkeypatch, own_bot, approvals):
    s, w = await world(stand, conn, monkeypatch)
    room, _ = await group(conn, w)
    offered = (await projects.create_project(conn, "Объект Береговой", [room], origin="model",
                                             reason={"messages": 25, "chats": [room]}))["project"]["id"]
    second = (await projects.create_project(conn, "Склад на Окружной", origin="model",
                                            reason={"messages": 30}))["project"]["id"]
    got = (await s.page.get("/memory/pending")).json()
    assert got["total"] == 2
    assert got["projects"] == [
        {"id": offered, "title": "Объект Береговой", "reason": "групповой чат: сообщений за 30 дн. — 25",
         "chats": ["Объект Береговой: рабочая"]},
        {"id": second, "title": "Склад на Окружной", "reason": "групповой чат: сообщений за 30 дн. — 30", "chats": []}]
    assert (await s.page.get("/memory/projects")).json()["projects"] == []       # предложенные — не в списке

    assert (await s.page.post(f"/memory/pending/projects/{offered}", {"accept": "да"})).status_code == 400
    yes = await s.page.post(f"/memory/pending/projects/{offered}", {"accept": True})
    assert yes.status_code == 200 and await approvals.pending() == 0
    assert await conn.fetchval("SELECT status FROM projects WHERE id = $1", offered) == "active"
    assert await conn.fetchval("SELECT 1 FROM pages WHERE project_id = $1", offered) == 1
    assert (await s.page.post(f"/memory/pending/projects/{offered}", {"accept": False})).status_code == 409
    assert (await s.page.post(f"/memory/pending/projects/{second}", {"accept": False})).status_code == 200
    assert await conn.fetchval("SELECT status FROM projects WHERE id = $1", second) == "rejected"
    assert (await s.page.post("/memory/pending/projects/999999", {"accept": True})).status_code == 404
    assert [p["id"] for p in (await s.page.get("/memory/projects")).json()["projects"]] == [offered]
    rows = await audit_rows(conn)
    assert rows == [("memory.project_accept", "ok", f"проект № {offered}"),
                    ("memory.project_reject", "ok", f"проект № {second}")]
    no_names(rows)
