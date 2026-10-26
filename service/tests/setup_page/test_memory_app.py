"""Приложение «Штурман», экран «Память» (шаг 6 записки «держать, подкладывать, сигналить»): что действует,
лента изменений, «на дату», «Поправить» и «Не так» — сразу, в контексте владельца из Telegram, с журналом без
текста и сообщением владельцу в бот; устаревший отпечаток — отказ."""

from datetime import datetime, timedelta, timezone

from setup_fakes import ORIGIN, stand  # noqa: F401, I001 — stand — фикстура; первым: добавляет пути
from test_miniapp import App, MODULES, ready, signed  # noqa: F401
from test_pack4a_app import OWNER, _messages
import exec_fakes  # noqa: E402

from shturman import authority, store  # noqa: E402
from shturman.processing import facts, projects  # noqa: E402
from shturman.records import ChatRecord  # noqa: E402

GROUP = 777002


async def world(conn):
    acc = await store.ensure_account(conn, OWNER, "Владелец", "owner")
    chat_id, _ = await store.ensure_chat(conn, acc, ChatRecord("channel", GROUP, "private_supergroup", "Северный: стройка"))
    mid, = await _messages(conn, chat_id, [(OWNER, "По Северному бюджет электрики 4 млн")])
    with authority.owner_context(OWNER, chat_id=OWNER, action="test.memory"):
        project = (await projects.create_project(conn, "ЖК Северный", [chat_id]))["project"]["id"]
    fact_id = await conn.fetchval(
        """INSERT INTO facts (subject_type, project_id, kind, slot, text, text_norm, valid_from, status, origin,
                              source_message_id, source_quote, basis, chained, applied_at, created_at)
           VALUES ('project', $1, 'fact', 'бюджет', 'бюджет электрики 40 млн', 'бюджет электрики 40 млн',
                   '2026-10-06', 'active', 'owner', $2, 'бюджет электрики 4 млн', 'owner', false,
                   '2026-10-06T12:00:00Z', '2026-10-06T12:00:00Z') RETURNING id""", project, mid)
    return project, fact_id


async def test_memory_screen_shows_current_history_and_dates(stand, conn):
    s = await ready(stand, conn)
    project, fact_id = await world(conn)
    assert (await s.app.sign_in(signed(query_id="mem-1"))).status_code == 200
    listed = (await s.app.get("/memory")).json()
    assert [p["title"] for p in listed["projects"]] == ["ЖК Северный"] and listed["owner"]["title"] == "Вы"
    assert (await s.app.get("/memory", params={"q": "южный"})).json()["projects"] == []
    view = (await s.app.get(f"/memory/project/{project}")).json()
    row, = view["current"]
    assert row["text"] == "бюджет электрики 40 млн" and row["basis"] == "сказали вы" and row["chat"] == "Северный: стройка"
    assert row["quote"] == "бюджет электрики 4 млн" and view["editable"] is True
    assert [h["state_text"] for h in view["history"]] == ["действует"]
    before = (await s.app.get(f"/memory/project/{project}", params={"as_of": "2026-10-05"})).json()
    assert before["current"] == [] and before["editable"] is False
    known = (await s.app.get(f"/memory/project/{project}", params={"known_at": "2026-10-07"})).json()
    assert [r["id"] for r in known["current"]] == [fact_id]
    assert (await s.app.get(f"/memory/project/{project}", params={"as_of": "x"})).status_code == 400
    assert (await s.app.get("/memory/project/999")).status_code == 404
    # без входа в приложение — ничего
    assert (await App(s.api._transport, ORIGIN).get(f"/memory/project/{project}")).status_code == 401


async def test_correct_and_wrong_from_the_app(stand, conn):
    s = await ready(stand, conn)
    project, fact_id = await world(conn)
    assert (await s.app.sign_in(signed(query_id="mem-2"))).status_code == 200
    row, = (await s.app.get(f"/memory/project/{project}")).json()["current"]
    stale = await s.app.post(f"/memory/facts/{fact_id}/correct", {"text": "бюджет электрики 4 млн", "v": "0" * 16})
    assert stale.status_code == 409
    out = await s.app.post(f"/memory/facts/{fact_id}/correct", {"text": "бюджет электрики 4 млн", "v": row["v"]})
    assert out.status_code == 200 and out.json()["answer"].startswith("Поправил")
    view = (await s.app.get(f"/memory/project/{project}")).json()
    new, = view["current"]
    assert new["text"] == "бюджет электрики 4 млн" and new["correction"] is True and new["basis"] == "сказали вы"
    assert [h["state_text"] for h in view["history"]] == ["действует", "исправлено"]
    old = await conn.fetchrow("SELECT status, superseded_by FROM facts WHERE id = $1", fact_id)
    assert (old["status"], old["superseded_by"]) == ("retracted", new["id"])
    # «что ассистент знал» до исправления — прежняя запись
    day_before = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    known = (await s.app.get(f"/memory/project/{project}", params={"known_at": day_before})).json()
    assert [r["text"] for r in known["current"]] == ["бюджет электрики 40 млн"]
    # тот же текст — отказ; «Не так» по исправленному
    assert (await s.app.post(f"/memory/facts/{new['id']}/correct", {"text": "Бюджет  электрики 4 млн", "v": new["v"]})).status_code == 400
    assert (await s.app.post(f"/memory/facts/{new['id']}/wrong", {"v": new["v"]})).status_code == 200
    assert (await s.app.get(f"/memory/project/{project}")).json()["current"] == []
    audit = [(r["action"], r["detail"]) for r in await conn.fetch(
        "SELECT action, detail FROM setup_audit WHERE action LIKE 'app.fact%' AND outcome = 'ok' ORDER BY id")]
    assert audit == [("app.fact_correct", f"факт № {fact_id} → № {new['id']}"), ("app.fact_wrong", f"факт № {new['id']}")]
    assert not any("млн" in d for _, d in audit)
    told = lambda: [m["text"] for m in s.telegram.sent() if "в памяти" in m["text"]]  # noqa: E731
    await exec_fakes.until(lambda: len(told()) == 2)
    assert all("млн" not in t for t in told())


async def test_owner_profile_correction_keeps_the_approval(conn):
    """Факт профиля, исправленный владельцем, остаётся одобренным им: его ассистент читает как слова владельца."""
    acc = await store.ensure_account(conn, OWNER, "Владелец", "owner")
    chat_id, _ = await store.ensure_chat(conn, acc, ChatRecord("user", 5009, "personal_chat", "Коллега"))
    mid, = await _messages(conn, chat_id, [(OWNER, "Я отвечаю за стройку на Северном")])
    fact_id = await conn.fetchval(
        """INSERT INTO facts (subject_type, kind, slot, text, text_norm, valid_from, status, origin,
                              source_message_id, source_quote, basis, applied_at)
           VALUES ('owner', 'fact', 'роль', 'отвечает за стройку', 'отвечает за стройку', '2026-10-06', 'active', 'owner',
                   $1, 'Я отвечаю за стройку на Северном', 'approved', now()) RETURNING id""", mid)
    with authority.owner_context(OWNER, chat_id=OWNER, action="test.memory"):
        out = await facts.correct_fact(conn, fact_id, "отвечает за стройку ЖК Северный")
    assert out["fact"]["owner_approved"] is True and out["fact"]["change_kind"] == "correction"
