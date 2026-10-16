"""Пакет 4б ревизии удобства (Р-95): «История» с «Отозвать» и «Вернуть как было», «Ответы и доверие» без JSON,
утренняя сводка без терминала — на странице «Настройки Штурмана» и в приложении «Штурман».

Главное, что проверяется:
  * страница — сам владелец: и ослабление, и сужение сразу; приложение — сужение сразу, ослабление только
    карточкой в бот, «Да» под которой в приложении не принимается (`owner.widen` в `confirm.BOT_ONLY`);
  * «Вернуть как было»: ужесточение — сразу, ослабление — только карточкой в бот (и со страницы тоже);
    вернуть можно только последнее изменение;
  * «Отозвать» разрешение — сразу и со страницы, и из приложения, и кнопкой в боте;
  * в «Истории» нет текста переписки и черновиков;
  * расписание сводок: выбор → задание `hermes.cron` → ответ плагина → совпадает или расхождение; без
    ответа плагина (режим без Hermes) блока нет; внутреннего API у выбора нет.
"""

import json

from setup_fakes import API, bind_owner, stand  # noqa: F401, I001 — stand — фикстура; первым: добавляет пути
from test_miniapp import App, signed  # noqa: E402
import exec_fakes  # noqa: E402

from shturman import authority, bridge, history, store, summaries  # noqa: E402
from shturman.records import ChatRecord  # noqa: E402

OWNER = exec_fakes.OWNER_USER["id"]
TOKEN = exec_fakes.TOKEN
ANNA, BORIS = 5001, 5002
SECRET = "СЕКРЕТНЫЙ-ТЕКСТ-ЧЕРНОВИКА"
MODULES = ("shturman.api_core", "shturman.executor.service", "shturman.ingest_api", "shturman.tg.service",
           "shturman.outbox.service", "shturman.notify.service", "shturman.rehearsal.service",
           "shturman.triage.service", "shturman.summaries", "shturman.setup_page.service")


async def ready(stand, conn, **changes):
    s = await stand(modules=MODULES, bot_token=TOKEN, **changes)
    await s.page.login(conn)
    await bind_owner(s)
    s.app = App(s.api._transport, "http://test")
    s.acc = await store.ensure_account(conn, OWNER, "Владелец", "owner")
    s.anna, _ = await store.ensure_chat(conn, s.acc, ChatRecord("user", ANNA, "personal_chat", "Анна Ким"))
    s.boris, _ = await store.ensure_chat(conn, s.acc, ChatRecord("user", BORIS, "personal_chat", "Борис Ли"))
    return s


async def bot_messages(s) -> list[str]:
    rows = await s.conn.fetch("SELECT payload FROM jobs WHERE kind = $1 ORDER BY id", bridge.NOTIFY_OWNER)
    return [(r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"])).get("text") or ""
            for r in rows]


async def press(conn, action_id: int, *, app: bool = False) -> dict:
    """Нажатие «Да, сделать» под карточкой: в самом боте или в приложении (контекст приложения)."""
    nonce = await conn.fetchval("SELECT nonce FROM pending_actions WHERE id = $1", action_id)
    action = "telegram.miniapp" if app else "telegram.callback"
    with authority.owner_context(OWNER, chat_id=OWNER, action=action):
        return await bridge.dispatch_callback(conn, f"sh:cf:y:{action_id}:{nonce}", OWNER)


async def trusted(conn) -> set[int]:
    return {r["tg_user_id"] for r in await conn.fetch("SELECT tg_user_id FROM outbox_trusted")}


async def test_page_widens_at_once_app_only_asks_the_bot(stand, conn):
    s = await ready(stand, conn)
    found = (await s.page.get("/replies/people", params={"q": "анна"})).json()["items"]
    assert found == [{"tg_id": ANNA, "name": "Анна Ким", "username": None, "trusted": False}]
    # страница: сам владелец — сразу, без карточки
    r = await s.page.post("/replies/trusted", {"tg_user_id": ANNA, "note": "жена"})
    assert r.status_code == 200, r.text
    assert [t["name"] for t in r.json()["trusted"]] == ["Анна Ким"] and r.json()["trusted"][0]["note"] == "жена"
    assert await conn.fetchval("SELECT count(*) FROM pending_actions") == 0
    (entry,) = await history.entries(conn)
    assert entry["title"] == "Анна Ким — в доверенных." and entry["via"] == "страница" and entry["can_revert"]
    # приложение: расширение — только карточка в бот, ничего не применено
    assert (await s.app.sign_in(signed(query_id="p4b-1"))).status_code == 200
    asked = await s.app.post("/replies/trusted", {"tg_user_id": BORIS})
    assert asked.status_code == 202 and asked.json()["status"] == "pending_confirmation"
    assert await trusted(conn) == {ANNA}
    action = asked.json()["action_id"]
    assert await conn.fetchval("SELECT kind FROM pending_actions WHERE id = $1", action) == "owner.widen"
    summary = await conn.fetchval("SELECT summary FROM pending_actions WHERE id = $1", action)
    assert "Борис Ли" in summary and "в приложении «Штурман»" in summary and "{" not in summary
    # в «Согласованиях» приложения — без кнопок; нажатие «Да» в приложении не проходит
    items = (await s.app.get("/approvals")).json()["items"]
    (card,) = [i for i in items if i["kind"] == "action" and i["id"] == action]
    assert card["actions"] == []
    refused = await press(conn, action, app=True)
    assert "только в боте" in refused["answer"] and await trusted(conn) == {ANNA}
    # нажатие в самом боте — применяет та же функция, со строкой «Истории»
    done = await press(conn, action)
    assert done["answer"] == "Сделано." and await trusted(conn) == {ANNA, BORIS}
    assert (await history.entries(conn))[0]["via"] == "бот, «Да» под карточкой"
    # сужение из приложения — сразу, и владельцу уходит сообщение
    r = await s.app.delete(f"/replies/trusted/{ANNA}")
    assert r.status_code == 200 and await trusted(conn) == {BORIS}
    assert any("Из списка доверенных убран" in t for t in await bot_messages(s))
    log = [(r["action"], r["detail"]) for r in await conn.fetch(
        "SELECT action, detail FROM setup_audit WHERE action LIKE '%replies%' ORDER BY id")]
    assert log == [("replies.change", "что: добавлен доверенный; через: страница"),
                   ("app.replies_request", "что: добавлен доверенный; через: приложение"),
                   ("app.replies", "что: убран доверенный; через: приложение")]
    assert "Анна" not in json.dumps(log, ensure_ascii=False) and str(ANNA) not in json.dumps(log)


async def test_owner_widen_applies_only_under_a_press_in_the_bot(stand, conn):
    s = await ready(stand, conn)  # noqa: F841
    from shturman import confirm
    payload = {"kind": "outbox.trusted_add", "payload": {"tg_user_id": ANNA, "note": None}, "from": "app"}
    for context in (authority.setup_context(1), authority.owner_context(OWNER, chat_id=OWNER, action="telegram.command"),
                    authority.miniapp_context(OWNER, chat_id=OWNER)):
        with context:
            try:
                await confirm.apply_owner(conn, "owner.widen", payload)
            except confirm.Refused as exc:
                assert exc.code == "bot_only"
            else:
                raise AssertionError("owner.widen применилось без нажатия в боте")
    assert await trusted(conn) == set()
    # под нажатием — только виды из перечня
    action = (await confirm.request_card(conn, "owner.widen", "Тестовая просьба расширить настройки владельца.",
                                         {"kind": "halt.resume", "payload": {}, "from": "app"}))["action_id"]
    assert (await press(conn, action))["answer"] == "Не получилось."


async def test_groups_drafting_addressing_autoreply_from_the_page_and_the_app(stand, conn):
    s = await ready(stand, conn, sending=False, prepare_only=True)
    helper = await store.ensure_account(conn, 9000, "Помощник", "assistant")
    group, _ = await store.ensure_chat(conn, helper, ChatRecord("chat", 7001, "private_group", "Корпус 3"))
    groups = (await s.page.get("/replies/chats", params={"groups": "1"})).json()["items"]
    assert [g["title"] for g in groups] == ["Корпус 3"]
    assert (await s.page.post("/replies/groups", {"chat_id": group})).status_code == 200
    assert (await s.page.put("/replies/addressing", {"aliases": ["Штурман"]})).status_code == 200
    assert (await s.page.put(f"/replies/drafting/{s.anna}", {"value": "deny"})).status_code == 200
    assert (await s.page.put(f"/replies/autoreply/{helper}", {"enabled": True})).status_code == 200
    view = (await s.page.get("/replies")).json()
    assert view["groups"] == [{"chat_id": group, "topic_tg_id": None, "title": "Корпус 3", "account": "Помощник"}]
    assert view["addressing"] == ["Штурман"] and view["drafting"]["chats"][0]["value"] == "deny"
    assert [a["enabled"] for a in view["autoreply"]["accounts"] if a["account_id"] == helper] == [True]
    # приложение: сузить — сразу; расширить — карточка
    assert (await s.app.sign_in(signed(query_id="p4b-2"))).status_code == 200
    assert (await s.app.put("/replies/addressing", {"aliases": []})).status_code == 200
    asked = await s.app.put("/replies/addressing", {"aliases": ["Штурман", "Ассистент"]})
    assert asked.status_code == 202
    assert (await s.app.put(f"/replies/drafting/{s.anna}", {"value": "allow"})).status_code == 202
    assert (await s.app.delete(f"/replies/groups/{group}")).status_code == 200
    assert (await s.app.put(f"/replies/autoreply/{helper}", {"enabled": False})).status_code == 200
    after = (await s.page.get("/replies")).json()
    assert after["addressing"] == [] and after["groups"] == [] and after["drafting"]["chats"][0]["value"] == "deny"
    assert (await s.app.post("/replies/groups", {"chat_id": group})).status_code == 202
    assert (await s.app.put(f"/replies/autoreply/{helper}", {"enabled": True})).status_code == 202
    kinds = [json.loads(r["payload"])["kind"] for r in await conn.fetch(
        "SELECT payload::text AS payload FROM pending_actions WHERE kind = 'owner.widen' ORDER BY id")]
    assert kinds == ["outbox.addressing", "outbox.chat_drafting", "outbox.group_scope", "outbox.autoreply"]
    # группа аккаунта владельца — не для ответов
    r = await s.page.post("/replies/groups", {"chat_id": s.anna})
    assert r.status_code == 400 and "помощника" in r.json()["error"]


async def test_revert_tightening_at_once_widening_only_in_the_bot(stand, conn):
    s = await ready(stand, conn)
    await s.page.post("/replies/trusted", {"tg_user_id": ANNA})
    (added,) = await history.entries(conn)
    r = await s.page.post(f"/history/{added['id']}/revert")
    assert r.status_code == 200 and r.json()["status"] == "applied" and await trusted(conn) == set()
    entries = await history.entries(conn)
    removed = entries[0]
    assert removed["title"] == "Анна Ким — больше не в доверенных." and removed["can_revert"]
    assert [e["undone"] for e in entries] == [False, True]
    again = await s.page.post(f"/history/{added['id']}/revert")
    assert again.status_code == 409
    # вернуть удаление — это расширение: и со страницы только карточка
    r = await s.page.post(f"/history/{removed['id']}/revert")
    assert r.status_code == 202 and await trusted(conn) == set()
    action = r.json()["action_id"]
    assert (await press(conn, action, app=True))["answer"].startswith("Подтвердить можно только")
    assert await trusted(conn) == set()
    assert (await press(conn, action))["answer"] == "Сделано." and await trusted(conn) == {ANNA}
    assert (await history.entries(conn))[1]["undone"] is True
    # из приложения то же: вернуть добавление (ужесточение) — сразу
    assert (await s.app.sign_in(signed(query_id="p4b-3"))).status_code == 200
    latest = (await s.app.get("/history", params={"tab": "decisions"})).json()
    change = [i for d in latest["days_list"] for i in d["items"] if i["kind"] == "change"][0]
    assert change["actions"] == [{"ref": f"revert:{(await history.entries(conn))[0]['id']}", "label": "Вернуть как было"}]
    r = await s.app.post(f"/history/{change['actions'][0]['ref'].split(':')[1]}/revert")
    assert r.status_code == 200 and await trusted(conn) == set()
    assert any("«Вернуть как было»" in t for t in await bot_messages(s))


async def test_grants_are_revoked_at_once_from_page_and_app(stand, conn):
    s = await ready(stand, conn)
    first = await conn.fetchval(
        """INSERT INTO source_grants (task_id, target_chat_id, kind, request, mode, owner_id, expires_at)
           VALUES (NULL, $1, 'chat', '{"query": "смета"}', 'read', $2, now() + interval '30 days') RETURNING id""",
        s.anna, OWNER)
    second = await conn.fetchval(
        """INSERT INTO source_grants (task_id, target_chat_id, kind, request, mode, owner_id, expires_at)
           VALUES (NULL, $1, 'memory', '{}', 'disclose', $2, now() + interval '1 day') RETURNING id""", s.boris, OWNER)
    await conn.execute("INSERT INTO remote_mcp_clients (id, name, redirect_uris) VALUES ('c1', 'Claude', '[]')")
    mcp = await conn.fetchval("""INSERT INTO remote_mcp_grants (client_id, owner_id, resource, expires_at)
                                 VALUES ('c1', $1, 'r', now() + interval '30 days') RETURNING id""", OWNER)
    view = (await s.page.get("/history", params={"tab": "decisions"})).json()
    refs = [g["ref"] for g in view["grants"]]
    assert refs == [f"source:{second}", f"source:{first}", f"mcp:{mcp}"]
    titles = [g["title"] for g in view["grants"]]
    assert titles[1] == "Прочитать переписку по запросу «смета» для ответа в «Анна Ким»"
    assert titles[2] == "Внешняя программа «Claude» читает архив"
    assert (await s.page.post(f"/history/grants/source/{first}/revoke")).status_code == 200
    assert (await s.page.post(f"/history/grants/source/{first}/revoke")).status_code == 404
    assert (await s.app.sign_in(signed(query_id="p4b-4"))).status_code == 200
    assert (await s.app.post(f"/history/grants/source/{second}/revoke")).status_code == 200
    assert (await s.app.post(f"/history/grants/mcp/{mcp}/revoke")).status_code == 200
    assert await conn.fetchval("SELECT count(*) FROM source_grants WHERE revoked_at IS NULL") == 0
    assert await conn.fetchval("SELECT revoked_at IS NOT NULL FROM remote_mcp_grants WHERE id = $1", mcp)
    assert any(t.startswith("Из приложения «Штурман» отозвано доступ к архиву") for t in await bot_messages(s))
    left = (await s.app.get("/history", params={"tab": "decisions"})).json()
    assert left["grants"] == []
    words = [i["title"] for d in left["days_list"] for i in d["items"] if i["kind"] == "grant"]
    assert "Отозвано разрешение прочитать переписку для ответа в «Анна Ким»" in words


async def test_assistant_tab_and_logins_have_no_message_text(stand, conn):
    s = await ready(stand, conn)
    for start, steps, origin in (("pending", ["rejected"], "agent"), ("approved", ["sending", "sent"], "autoreply")):
        draft = await conn.fetchval(
            """INSERT INTO outbox_drafts (account_id, chat_id, channel, text, text_hash, origin, nonce, status, expires_at)
               VALUES ($1, $2, 'business', $3, 'h', $4, 'n', $5, now() + interval '1 day') RETURNING id""",
            s.acc, s.anna, SECRET + start, origin, start)
        for status in steps:
            await conn.execute("UPDATE outbox_drafts SET status = $2, claimed_at = now() WHERE id = $1", draft, status)
    body = (await s.page.get("/history", params={"tab": "assistant"})).json()
    items = [i for d in body["days_list"] for i in d["items"]]
    assert [(i["title"], i["status"]) for i in items] == [
        ("Автоответ доверенному в «Анна Ким»", "отправлен сам (вы включили автоответ)"),
        ("Черновик ответа в «Анна Ким»", "вы отклонили")]
    assert SECRET not in json.dumps(body, ensure_ascii=False)
    logins = (await s.page.get("/history", params={"tab": "logins"})).json()
    assert [i["title"] for d in logins["days_list"] for i in d["items"]] == ["Вход по ссылке"]
    assert logins["log"] and (await s.page.get("/history", params={"tab": "nope"})).status_code == 400


async def _plugin(s, result: dict) -> dict:
    """Плагин Hermes: забирает задание расписания и отвечает тем, что в Hermes (через внутренний API моста)."""
    claimed = (await s.api.post("/api/jobs/claim", json={"kinds": [bridge.HERMES_CRON], "limit": 5})).json()["jobs"]
    assert claimed, "задания расписания нет"
    for job in claimed:
        r = await s.api.post(f"/api/jobs/{job['id']}/complete",
                             json={"result": {**result, "revision": job["payload"]["revision"],
                                              "applied": job["payload"]["apply"]}})
        assert r.status_code == 200
    return claimed[-1]["payload"]


def _hermes(morning="07:30", weekly_on=True, morning_next="пт 16.10 07:30") -> dict:
    return {"timezone": "Europe/Moscow", "jobs": {
        "morning-brief": {"exists": True, "enabled": True, "time": morning, "expr": "x",
                          "next_run_at": "2026-10-16T07:30:00+03:00", "next_local": morning_next},
        "weekly-review": {"exists": True, "enabled": weekly_on, "time": "17:47", "day": 5}}}


async def test_summary_schedule_goes_to_hermes_and_back(stand, conn):
    s = await ready(stand, conn)
    first = (await s.page.get("/summaries")).json()
    assert first["available"] is False and first["chosen"] is False and first["sync"]["state"] == "unset"
    # без ответа плагина (режим без Hermes) — блока нет; «посмотреть» при запуске ставится, но никто не берёт
    await summaries.tick(conn, boot=True)
    payload = await _plugin(s, _hermes(morning="08:27"))
    assert payload == {"apply": False, "revision": 0}
    seen = (await s.page.get("/summaries")).json()
    assert seen["available"] is True and seen["choice"]["morning-brief"] == {"enabled": True, "time": "08:27"}
    r = await s.page.put("/summaries", {"morning-brief": {"enabled": True, "time": "07:30"}})
    assert r.status_code == 200 and r.json()["sync"]["state"] == "pending"
    payload = await _plugin(s, _hermes())
    assert payload["apply"] is True and payload["desired"]["morning-brief"] == {"enabled": True, "time": "07:30"}
    assert payload["desired"]["weekly-review"] == {"enabled": True, "time": "17:47", "day": 5}
    view = (await s.page.get("/summaries")).json()
    assert view["sync"] == {"state": "in_sync", "differs": []}
    assert view["hermes"]["morning-brief"]["next_local"] == "пт 16.10 07:30" and view["timezone"] == "Europe/Moscow"
    status = (await s.api.get("/api/status")).json()
    assert status["summaries_state"] == "in_sync" and status["summaries_next_morning"] == "пт 16.10 07:30"
    # правка в самом Hermes — расхождение, не перезапись
    await summaries.request_refresh(conn, key="t-1")
    payload = await _plugin(s, _hermes(morning="09:00"))
    assert payload["apply"] is False
    assert (await s.page.get("/summaries")).json()["sync"] == {"state": "drift", "differs": ["morning-brief"]}
    # приложение — сразу, с журналом и сообщением в бот
    assert (await s.app.sign_in(signed(query_id="p4b-5"))).status_code == 200
    r = await s.app.put("/summaries", {"weekly-review": {"enabled": False}})
    assert r.status_code == 200 and r.json()["choice"]["weekly-review"]["enabled"] is False
    assert any("изменено расписание сводок" in t for t in await bot_messages(s))
    log = [r["detail"] for r in await conn.fetch("SELECT detail FROM setup_audit WHERE action = 'summaries.schedule'")]
    assert log[-1] == "утро: 07:30 вкл; неделя: пт 17:47 выкл; через: приложение"
    # «История» — вернуть сразу
    (latest, *_) = [e for e in await history.entries(conn) if e["area"] == "summaries"]
    assert latest["title"] == "Обзор недели: выключен." and latest["can_revert"]
    assert (await s.app.post(f"/history/{latest['id']}/revert")).status_code == 200
    assert (await summaries.desired(conn))["weekly-review"]["enabled"] is True
    # внутреннего API у выбора нет
    assert (await s.api.put("/api/summaries", json={})).status_code in (404, 405)
    bad = await s.page.put("/summaries", {"morning-brief": {"time": "7:30"}})
    assert bad.status_code == 400 and "ЧЧ:ММ" in bad.json()["error"]


async def test_plugin_error_and_hidden_jobs_are_reported_without_extra_fields(stand, conn):
    s = await ready(stand, conn)
    await s.page.put("/summaries", {"morning-brief": {"enabled": True, "time": "06:00"}})
    await _plugin(s, {"jobs": {"morning-brief": {"exists": False, "prompt": "чужое"}}, "error": "no_home_channel",
                      "timezone": "Europe/Moscow"})
    actual = await summaries.actual(conn)
    assert actual["jobs"]["morning-brief"] == {"exists": False, "enabled": False}
    view = (await s.page.get("/summaries")).json()
    assert view["sync"]["state"] == "error" and "некуда приходить" in view["sync"]["error_text"]


async def test_late_answer_to_an_older_job_does_not_overwrite_a_newer_one(stand, conn):
    await summaries._reported(conn, {"id": 7}, {**_hermes(morning="07:30"), "applied": True, "revision": 2})
    await summaries._reported(conn, {"id": 5}, {**_hermes(morning="09:00"), "applied": False, "revision": 1})
    actual = await summaries.actual(conn)
    assert actual["job"] == 7 and actual["revision"] == 2 and actual["jobs"]["morning-brief"]["time"] == "07:30"
    # «только посмотреть» не сдвигает применённую версию выбора
    await summaries._reported(conn, {"id": 8}, {**_hermes(morning="09:00"), "applied": False, "revision": 5})
    actual = await summaries.actual(conn)
    assert actual["job"] == 8 and actual["revision"] == 2 and actual["jobs"]["morning-brief"]["time"] == "09:00"
