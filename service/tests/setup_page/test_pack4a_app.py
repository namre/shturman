"""Приложение «Штурман», пакет 4а (Р-94): блок «Сейчас», экран «Договорённости» и разделы «Согласований».

Что проверяется: «Сейчас» — только числа, состояния и время, без текста переписки и имён; договорённости —
только принятые владельцем, по направлению («Я обещал» / «Мне обещали») и группам срока, без цитат и слов о
сроке, без скрытых защитой, исключённых и неподтверждённых; «Выполнено», «Перенести», «Не так» — сразу, в
контексте владельца из Telegram, с журналом договорённости (`via: app`) и журналом действий страницы без
формулировок; устаревший отпечаток — отказ; «Вернуть» — только сразу после своего действия; карточки
«Согласований» — по разделам.
"""

import json
from datetime import date, datetime, timedelta, timezone

from setup_fakes import ORIGIN, stand  # noqa: F401, I001 — stand — фикстура; первым: добавляет пути
from test_miniapp import App, MODULES, ready, signed  # noqa: F401
import exec_fakes  # noqa: E402

from shturman import authority, bridge, confirm, spheres, store  # noqa: E402
from shturman.processing import agenda, commitments  # noqa: E402
from shturman.records import ChatRecord, MessageRecord  # noqa: E402
from shturman.setup_page import approvals  # noqa: E402

OWNER = exec_fakes.OWNER_USER["id"]
ANNA, PETR, GROUP = 5001, 5002, 777001
QUOTES = ("Смету по складу пришлю до пятницы", "Договор подпишу завтра", "Акт сверки за сентябрь",
          "Секретный отчёт", "Обход в четверг", "Счёт № 77 закрыть", "Это между ними")


async def _messages(conn, chat_id: int, lines: list[tuple[int, str]]) -> list[int]:
    now = datetime.now(timezone.utc)
    base = int(await conn.fetchval("SELECT coalesce(max(tg_message_id), 0) FROM messages WHERE chat_id = $1", chat_id))
    rows = [(chat_id, MessageRecord(
        tg_message_id=base + n + 1, sent_at=now - timedelta(hours=n + 1), kind="message", sender_class="user",
        sender_tg_id=who, sender_name=None, text=text, entities=None, reply_to_tg_id=None, forwarded_from=None,
        edited_at=None, media_type=None, media_path=None, service_action=None), who == OWNER)
        for n, (who, text) in enumerate(lines)]
    await store.upsert_messages(conn, rows, source="session", owner_tg_id=OWNER)
    return [r["id"] for r in await conn.fetch(
        "SELECT id FROM messages WHERE chat_id = $1 AND tg_message_id > $2 ORDER BY tg_message_id", chat_id, base)]


async def _promise(conn, chat_id, msg, debtor, creditor, direction, what, quote, due=None, *,
                   accept=True, expression=None) -> int:
    cid = await conn.fetchval(
        """INSERT INTO commitments (chat_id, source_message_id, debtor_peer_id, creditor_peer_id, direction,
                                    what, source_quote, due_expression, due_reason, due_date)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING id""",
        chat_id, msg, debtor, creditor, direction, what, quote, expression or ("до пятницы" if due else None),
        "ok" if due else "no_deadline", due)
    if accept:
        with authority.owner_context(OWNER, chat_id=OWNER, action="telegram.callback"):
            assert (await commitments.accept(conn, cid, actor="owner"))["ok"]
    return cid


async def seed(conn, today: date) -> dict:
    """Договорённости во всех группах срока и обоих направлениях, плюс то, что показывать нельзя."""
    account = await store.ensure_account(conn, OWNER, "Владелец", "owner")
    me = await store.ensure_peer(conn, "user", OWNER, name="Евгений")
    anna_chat, _ = await store.ensure_chat(conn, account, ChatRecord("user", ANNA, "personal_chat", "Анна Ким"))
    petr_chat, _ = await store.ensure_chat(conn, account, ChatRecord("user", PETR, "personal_chat", "Пётр Смета"))
    group, _ = await store.ensure_chat(conn, account, ChatRecord("channel", GROUP, "private_supergroup", "Стройка"))
    anna = await conn.fetchval("SELECT id FROM peers WHERE class = 'user' AND tg_id = $1", ANNA)
    petr = await conn.fetchval("SELECT id FROM peers WHERE class = 'user' AND tg_id = $1", PETR)
    a = await _messages(conn, anna_chat, [(OWNER, QUOTES[0]), (ANNA, QUOTES[1]), (ANNA, QUOTES[2]), (ANNA, QUOTES[3])])
    p = await _messages(conn, petr_chat, [(OWNER, QUOTES[4]), (PETR, QUOTES[5])])
    g = await _messages(conn, group, [(PETR, QUOTES[6])])
    ids = {
        "overdue_mine": await _promise(conn, anna_chat, a[0], me, anna, "owner_owes", "прислать смету по складу",
                                       QUOTES[0], today - timedelta(days=3)),
        "today_theirs": await _promise(conn, anna_chat, a[1], anna, me, "owed_to_owner", "подписать договор",
                                       QUOTES[1], today),
        "week_mine": await _promise(conn, petr_chat, p[0], me, petr, "owner_owes", "провести обход", QUOTES[4],
                                    today + timedelta(days=3)),
        "later_theirs": await _promise(conn, petr_chat, p[1], petr, me, "owed_to_owner", "закрыть счёт",
                                       QUOTES[5], today + timedelta(days=40)),
        "nodate_theirs": await _promise(conn, anna_chat, a[2], anna, me, "owed_to_owner", "прислать акт сверки",
                                        QUOTES[2], expression="на днях"),
        # не показываются: между другими людьми, ещё не подтверждено, скрыто защитой
        "others": await _promise(conn, group, g[0], petr, anna, "others", "между ними", QUOTES[6], today),
        "proposed": await _promise(conn, petr_chat, p[1], petr, me, "owed_to_owner", "ещё не подтверждено",
                                   QUOTES[5], today, accept=False),
        "hidden": await _promise(conn, anna_chat, a[3], anna, me, "owed_to_owner", "скрытое защитой",
                                 QUOTES[3], today),
    }
    await conn.execute("UPDATE messages SET agent_visible = false, guard_label = 'suspect' WHERE id = $1", a[3])
    work = await spheres.create_sphere(conn, "Работа")
    await spheres.add_chats(conn, work["id"], account, [("user", ANNA)])
    return ids


def _today(s) -> date:
    return approvals.today_of(s.state.config)


async def test_now_is_numbers_states_and_time_only(stand, conn):
    s = await ready(stand, conn)
    today = _today(s)
    await seed(conn, today)
    assert (await s.app.get("/now")).status_code == 401           # без сессии приложения — ничего
    assert (await s.app.sign_in(signed(query_id="now-1"))).status_code == 200
    got = await s.app.get("/now")
    assert got.status_code == 200
    body = got.json()
    assert body["commitments"] == {"owner_owes": {"overdue": 1, "today": 0}, "owed_to_owner": {"overdue": 0, "today": 1},
                                   "overdue": 1, "today": 1, "waiting": 1}
    assert body["sending"] == "off" and body["halt"]["stopped"] is False and body["triage"] is False
    assert body["model_failed_24h"] == {"media": 0, "requests": 0}
    assert body["last_message_at"] and body["today"] == today.isoformat()
    assert isinstance(body["accounts"], list)
    dump = json.dumps(body, ensure_ascii=False)
    for word in (*QUOTES, "Анна", "Пётр", "смету", "подписать договор", "Стройка"):
        assert word not in dump, word
    # ключ страницы приложение не открывает, и наоборот
    assert (await s.page.get("/app/now")).status_code in (401, 403)


async def test_approvals_come_in_sections_with_counts(stand, conn):
    s = await ready(stand, conn)
    today = _today(s)
    ids = await seed(conn, today)
    action = (await confirm.request(conn, "t.miniapp", "Тестовое действие для приложения", {"n": 4}))["action_id"]
    run = await conn.fetchval("INSERT INTO processing_runs (trigger, status) VALUES ('manual', 'done') RETURNING id")
    assert await commitments.build_digests(conn, run_id=run, today=today)
    assert (await s.app.sign_in(signed(query_id="groups-1"))).status_code == 200
    body = (await s.app.get("/approvals")).json()
    groups = {i["key"]: i["group"] for i in body["items"]}
    assert groups[f"action:{action}"] == "permissions"
    assert groups[f"commitment:{ids['proposed']}"] == "commitments"
    assert [g["key"] for g in body["groups"]] == ["answers", "permissions", "commitments", "memory", "guard"]
    assert [g["title"] for g in body["groups"]] == ["Ответы", "Разрешения", "Договорённости", "Память", "Защита"]
    counts = {g["key"]: g["count"] for g in body["groups"]}
    # «Защита»: сводка скрытого (Р-99) — сообщение стенда скрыто защитой и ждёт решения
    assert counts == {"answers": 0, "permissions": 1, "commitments": 1, "memory": 0, "guard": 1}
    # кнопки карточек — прежние
    (item,) = [i for i in body["items"] if i["kind"] == "commitment"]
    assert [a["label"] for a in item["actions"] if not a.get("why")] == ["Верно", "Не так"]
    # «Не так» с причиной (Р-96): причины — отдельные кнопки, приложение показывает их после «Не так»
    assert [a["label"] for a in item["actions"] if a.get("why")] == [
        "не обещание", "не тот человек", "неверно что", "неверный срок", "дубль", "уже сделано"]


def test_every_kind_has_its_section():
    from shturman import words
    cases = {("draft", "Черновик ответа"): "answers", ("reply", words.REPLY_QUESTION): "answers",
             ("reply", words.REPLY_SOURCE): "permissions", ("action", "Подтверждение действия"): "permissions",
             ("commitment", "Договорённость"): "commitments", ("owner_fact", "Факт о вас"): "memory",
             ("page", "Страница памяти"): "memory", ("project", "Проект"): "memory",
             ("hidden", "Скрытое сообщение"): "guard"}
    for (kind, title), group in cases.items():
        assert approvals.group_of(kind, title) == group, kind
    collected = {"items": [], "later": {"hidden": 2, "commitment": 3, "owner_fact": 1, "page": 0, "project": 4}}
    later = {g["key"]: g["later"] for g in approvals.group_counts(collected)}
    assert later == {"answers": 0, "permissions": 0, "commitments": 3, "memory": 5, "guard": 2}


async def test_commitments_two_lists_by_due_without_message_text(stand, conn):
    s = await ready(stand, conn)
    today = _today(s)
    ids = await seed(conn, today)
    assert (await s.app.get("/commitments")).status_code == 401
    assert (await s.app.sign_in(signed(query_id="cm-1"))).status_code == 200
    body = (await s.app.get("/commitments")).json()
    mine, theirs = body["lists"]["owner_owes"], body["lists"]["owed_to_owner"]
    assert [i["id"] for i in mine["overdue"]] == [ids["overdue_mine"]]
    assert [i["id"] for i in mine["week"]] == [ids["week_mine"]]
    assert mine["today"] == mine["later"] == mine["no_date"] == []
    assert [i["id"] for i in theirs["today"]] == [ids["today_theirs"]]
    assert [i["id"] for i in theirs["later"]] == [ids["later_theirs"]]
    assert [i["id"] for i in theirs["no_date"]] == [ids["nodate_theirs"]]
    assert body["waiting"] == 1 and body["truncated"] is False
    shown = {i["id"] for d in body["lists"].values() for g in d.values() for i in g}
    for hidden in ("others", "proposed", "hidden"):
        assert ids[hidden] not in shown, hidden
    item = mine["overdue"][0]
    assert item["who"] == "Анна Ким" and item["what"] == "прислать смету по складу"
    assert item["days"] == -3 and item["chat"] == {"title": "Анна Ким", "kind": "личный чат"}
    assert item["spheres"] == ["Работа"] and len(item["v"]) == agenda.SHOWN
    assert theirs["no_date"][0]["due_vague"] is True and theirs["no_date"][0]["due_words"] is None
    dump = json.dumps(body, ensure_ascii=False)
    for word in (*QUOTES, "до пятницы", "на днях", "скрытое защитой", "между ними", "source", "quote"):
        assert word not in dump, word


async def test_done_move_wrong_apply_at_once_with_journal(stand, conn):
    s = await ready(stand, conn)
    today = _today(s)
    ids = await seed(conn, today)
    assert (await s.app.sign_in(signed(query_id="cm-act-1"))).status_code == 200
    told_before = await conn.fetchval("SELECT count(*) FROM jobs WHERE kind = $1", bridge.NOTIFY_OWNER)

    def item_of(body, cid):
        return next(i for d in body["lists"].values() for g in d.values() for i in g if i["id"] == cid)

    board = (await s.app.get("/commitments")).json()
    first = item_of(board, ids["overdue_mine"])
    # «Выполнено» — сразу, журнал договорённости: владелец, из приложения
    done = await s.app.post(f"/commitments/{first['id']}/done", {"v": first["v"]})
    assert done.status_code == 200 and done.json()["undo"] is True
    assert await conn.fetchval("SELECT status FROM commitments WHERE id = $1", first["id"]) == "done"
    event = await conn.fetchrow("SELECT actor, action, details FROM commitment_events WHERE commitment_id = $1 "
                                "ORDER BY id DESC LIMIT 1", first["id"])
    assert (event["actor"], event["action"], json.loads(event["details"])["via"]) == ("owner", "closed", "app")
    # тот же отпечаток второй раз — договорённости уже нет в списке
    again = await s.app.post(f"/commitments/{first['id']}/done", {"v": first["v"]})
    assert again.status_code == 409 and again.json()["code"] == "gone"
    # «Вернуть» сразу после — да
    back = await s.app.post(f"/commitments/{first['id']}/reopen", {})
    assert back.status_code == 200
    assert await conn.fetchval("SELECT status FROM commitments WHERE id = $1", first["id"]) == "open"
    # «Вернуть» того, что закрыто не отсюда, — нет
    with authority.owner_context(OWNER, chat_id=OWNER, action="telegram.command"):
        assert (await commitments.close(conn, ids["week_mine"], actor="owner"))["ok"]
    refused = await s.app.post(f"/commitments/{ids['week_mine']}/reopen", {})
    assert refused.status_code == 409 and refused.json()["code"] == "too_late"
    assert await conn.fetchval("SELECT status FROM commitments WHERE id = $1", ids["week_mine"]) == "done"

    # «Перенести»: на завтра, через неделю, на дату; прошлое и мусор — отказ словами
    board = (await s.app.get("/commitments")).json()
    item = item_of(board, ids["today_theirs"])
    for bad, code in (({"to": "date", "date": (today - timedelta(days=1)).isoformat()}, "past"),
                      ({"to": "date", "date": "31.12.2026"}, "bad_date"), ({"to": "someday"}, "bad_to"),
                      ({"to": "date", "date": (today + timedelta(days=5000)).isoformat()}, "too_far")):
        answer = await s.app.post(f"/commitments/{item['id']}/move", {"v": item["v"], **bad})
        assert answer.status_code == 400 and answer.json()["code"] == code, bad
    moved = await s.app.post(f"/commitments/{item['id']}/move", {"v": item["v"], "to": "tomorrow"})
    assert moved.status_code == 200 and moved.json()["undo"] is False
    row = await conn.fetchrow("SELECT due_date, due_time, status, due_message_id FROM commitments WHERE id = $1", item["id"])
    assert (row["due_date"], row["status"], row["due_message_id"]) == (today + timedelta(days=1), "open", None)
    event = await conn.fetchrow("SELECT actor, action, details FROM commitment_events WHERE commitment_id = $1 "
                                "ORDER BY id DESC LIMIT 1", item["id"])
    details = json.loads(event["details"])
    assert (event["actor"], event["action"], details["via"]) == ("owner", "rescheduled", "app")
    assert details["to"] == (today + timedelta(days=1)).isoformat()
    # перенесённая договорённость остаётся одобренной владельцем — с новым отпечатком
    assert (await commitments.get_commitment(conn, item["id"], today=today))["owner_approved"] is True
    # прежний отпечаток после переноса — «изменилась»
    stale = await s.app.post(f"/commitments/{item['id']}/done", {"v": item["v"]})
    assert stale.status_code == 409 and stale.json()["code"] == "changed"
    board = (await s.app.get("/commitments")).json()
    item = item_of(board, ids["today_theirs"])
    assert item["group"] == "week" and item["days"] == 1
    picked = (today + timedelta(days=20)).isoformat()
    assert (await s.app.post(f"/commitments/{item['id']}/move",
                             {"v": item["v"], "to": "date", "date": picked})).status_code == 200
    assert await conn.fetchval("SELECT due_date FROM commitments WHERE id = $1", item["id"]) == date.fromisoformat(picked)

    # «Не так»: убрана как неверная, в выборках её больше нет
    board = (await s.app.get("/commitments")).json()
    wrong = item_of(board, ids["later_theirs"])
    assert (await s.app.post(f"/commitments/{wrong['id']}/wrong", {"v": wrong["v"]})).status_code == 200
    assert await conn.fetchval("SELECT status FROM commitments WHERE id = $1", wrong["id"]) == "rejected"
    action = await conn.fetchval("SELECT action FROM commitment_events WHERE commitment_id = $1 ORDER BY id DESC LIMIT 1",
                                 wrong["id"])
    assert action == "marked_wrong"
    board = (await s.app.get("/commitments")).json()
    assert wrong["id"] not in {i["id"] for d in board["lists"].values() for g in d.values() for i in g}

    # скрытое защитой, неподтверждённое и «между другими» отсюда не трогаются
    for hidden in ("hidden", "proposed", "others"):
        fp = await commitments.get_commitment(conn, ids[hidden], today=today)
        v = (fp or {}).get("approval_fingerprint", "x" * 24)[:24]
        for action in ("done", "wrong"):
            answer = await s.app.post(f"/commitments/{ids[hidden]}/{action}", {"v": v})
            assert answer.status_code == 409 and answer.json()["code"] == "gone", (hidden, action)
    assert await conn.fetchval("SELECT status FROM commitments WHERE id = $1", ids["hidden"]) == "open"
    assert await conn.fetchval("SELECT status FROM commitments WHERE id = $1", ids["proposed"]) == "proposed"

    # журнал действий страницы: номер и дата, без формулировок и имён; сообщений в бот нет
    log = [(r["action"], r["outcome"], r["detail"]) for r in await conn.fetch(
        "SELECT action, outcome, detail FROM setup_audit WHERE action LIKE 'app.commitment%' ORDER BY id")]
    assert ("app.commitment_done", "ok", f"договорённость № {ids['overdue_mine']}") in log
    assert ("app.commitment_wrong", "ok", f"договорённость № {ids['later_theirs']}") in log
    assert ("app.commitment_move", "ok", f"договорённость № {ids['today_theirs']}; новый срок "
            f"{today + timedelta(days=1):%d.%m.%Y}") in log
    assert any(a == "app.commitment_reopen" and o != "ok" for a, o, _ in log)
    text = json.dumps(log, ensure_ascii=False)
    for word in ("смет", "подписать договор", "акт сверки", "Анна", "Пётр", *QUOTES):
        assert word not in text, word
    told = await conn.fetchval("SELECT count(*) FROM jobs WHERE kind = $1", bridge.NOTIFY_OWNER)
    assert told == told_before


async def test_agent_cannot_mark_wrong_or_move_by_date(stand, conn):
    """Новые действия — только владельца: без его контекста «Не так» и перенос датой не проходят."""
    s = await ready(stand, conn)
    today = _today(s)
    ids = await seed(conn, today)
    out = await commitments.mark_wrong(conn, ids["week_mine"], actor="owner", today=today)
    assert out["ok"] is False and out["code"] == "approval_required"
    out = await commitments.reschedule_to(conn, ids["week_mine"], today + timedelta(days=2), actor="owner", today=today)
    assert out["ok"] is False and out["code"] == "approval_required"
    assert await conn.fetchval("SELECT status FROM commitments WHERE id = $1", ids["week_mine"]) == "open"
    # и во внутреннем API их нет
    for path in (f"/api/commitments/{ids['week_mine']}/wrong", f"/api/commitments/{ids['week_mine']}/move"):
        assert (await s.api.post(path, json={})).status_code in (401, 403, 404, 405)
