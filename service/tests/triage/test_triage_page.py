"""Разбор входящих на странице «Настройки Штурмана» (Р-90): включение — только нажатием в боте, выключение
и правила — сразу; оценка объёма; журнал без имён.

Главное:
  * включить со страницы нельзя: она присылает карточку в бот; в приложении «Да» под ней не принимается;
    функция применения сама отказывает всему, кроме нажатия в боте; внутреннего API у настроек нет;
  * выключение — сразу, и то, что ещё разбиралось, приходит обычным уведомлением;
  * правила (важные, тихие, сферы, срок) — сразу, в журнал — только числа.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "setup_page"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "executor"))

from setup_fakes import API, ORIGIN, bind_owner, stand  # noqa: E402,F401
import exec_fakes  # noqa: E402

from shturman import authority, bridge, confirm, halt, store  # noqa: E402
from shturman.records import ChatRecord  # noqa: E402
from shturman.triage import service as triage_service  # noqa: E402
from shturman.triage import settings as ts  # noqa: E402

from test_miniapp import App, TOKEN, signed  # noqa: E402

MODULES = ("shturman.api_core", "shturman.executor.service", "shturman.ingest_api", "shturman.tg.service",
           "shturman.outbox.service", "shturman.notify.service", "shturman.triage.service",
           "shturman.setup_page.service")
OWNER = exec_fakes.OWNER_USER["id"]
ANNA = 5001


async def ready(stand, conn):
    s = await stand(modules=MODULES, bot_token=TOKEN)
    await s.page.login(conn)
    await bind_owner(s)
    s.app = App(s.api._transport, ORIGIN)
    s.acc = await store.ensure_account(conn, OWNER, "Владелец", "owner")
    return s


async def enabled(conn) -> bool:
    return (await ts.load(conn))["enabled"]


async def test_enabling_is_only_a_press_in_the_bot(stand, conn):
    s = await ready(stand, conn)
    view = (await s.page.get("/triage")).json()
    assert view["settings"]["enabled"] is False and view["pending"] is None and view["own_bot"] is True
    assert view["settings"]["remind_minutes"] == 120 and "провайдеру вашей модели" in view["what_goes"][0]
    # прямо со страницы — нет
    refused = await s.page.put("/triage", {"enabled": True})
    assert refused.status_code == 400 and refused.json()["code"] == "bot_only"
    assert not await enabled(conn)
    # просьба — карточка в боте, само ничего не включает
    asked = await s.page.post("/triage/enable")
    assert asked.status_code == 202 and asked.json()["status"] == "pending_confirmation"
    action = asked.json()["action_id"]
    assert not await enabled(conn) and (await s.page.get("/triage")).json()["pending"]["action_id"] == action
    card = await conn.fetchval("SELECT summary FROM pending_actions WHERE id = $1", action)
    assert card.startswith("Включить разбор входящих.") and "Что уходит модели" in card and "Отправки нет" in card
    nonce = await conn.fetchval("SELECT nonce FROM pending_actions WHERE id = $1", action)
    # страница (вход страницы) применить сама не может, даже в обход карточки
    with authority.setup_context("test-session"):
        with pytest.raises(confirm.Refused) as exc:
            await confirm.apply_owner(conn, triage_service.ENABLE, {"enable": True})
    assert exc.value.code == "bot_only" and not await enabled(conn)
    # в приложении «Да» не принимается
    assert (await s.app.sign_in(signed(query_id="triage-1"))).status_code == 200
    items = (await s.app.get("/approvals")).json()["items"]
    (item,) = [i for i in items if i["kind"] == "action" and i["id"] == action]
    assert item["actions"] == []
    assert (await s.app.post("/approvals/decide", {"ref": f"sh:cf:y:{action}:{nonce}"})).status_code == 409
    assert not await enabled(conn)
    # нажатие в самом боте — включает
    with authority.owner_context(OWNER, chat_id=OWNER, action="telegram.callback"):
        done = await bridge.dispatch_callback(conn, f"sh:cf:y:{action}:{nonce}", OWNER)
    assert done["answer"] == "Сделано." and "Разбор входящих включён" in done["edit_text"]
    assert await enabled(conn)
    log = [(r["action"], r["detail"]) for r in await conn.fetch("SELECT action, detail FROM setup_audit "
                                                               "WHERE action LIKE 'triage.%' ORDER BY id")]
    assert [a for a, _ in log] == ["triage.enable_request", "triage.enable"]
    # через внутренний API (ассистент, дашборд Hermes) — ни включить, ни поменять правила
    for method, path in (("PUT", "/api/triage"), ("POST", "/api/triage/enable"), ("GET", "/api/triage")):
        assert (await s.api.request(method, path, json={"enabled": True})).status_code in (404, 405)


async def test_enabling_while_stopped_is_refused(stand, conn):
    s = await ready(stand, conn)
    await s.page.post("/halt", {"reading": False})
    refused = await s.page.post("/triage/enable")
    assert refused.status_code == 423 and not await enabled(conn)
    async with s.state.pool.acquire() as c:
        assert await halt.is_stopped(c)


async def test_rules_and_switching_off_apply_at_once_and_the_log_has_no_names(stand, conn):
    s = await ready(stand, conn)
    await ts.save(conn, {**await ts.load(conn), "enabled": True})
    chat_id, _ = await store.ensure_chat(conn, s.acc, ChatRecord("user", ANNA, "personal_chat", "Анна Ким"))
    sphere = await conn.fetchval("INSERT INTO spheres (title, title_norm) VALUES ('Корпус 3', 'корпус 3') RETURNING id")
    out = await s.page.put("/triage", {"important_people": [ANNA], "important_spheres": [sphere], "remind_minutes": 60})
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["settings"]["important_people"] == [ANNA] and body["settings"]["remind_minutes"] == 60
    assert body["people"] == [{"tg_id": ANNA, "name": "Анна Ким", "username": None, "level": "important"}]
    # тихой — та же Анна: прежняя отметка снимается
    body = (await s.page.put("/triage", {"quiet_people": [ANNA]})).json()
    assert body["settings"]["quiet_people"] == [ANNA] and body["settings"]["important_people"] == []
    assert (await s.page.put("/triage", {"important_spheres": [999]})).status_code == 400
    assert (await s.page.put("/triage", {"remind_minutes": 7})).status_code == 400
    # выключить — сразу
    off = (await s.page.put("/triage", {"enabled": False})).json()
    assert off["settings"]["enabled"] is False
    log = await conn.fetch("SELECT action, detail FROM setup_audit WHERE action LIKE 'triage.%' ORDER BY id")
    assert [r["action"] for r in log] == ["triage.settings", "triage.settings", "triage.disable"]
    assert all("Анна" not in r["detail"] and str(ANNA) not in r["detail"] for r in log)
    assert chat_id


async def test_estimate_counts_last_week_without_text(stand, conn):
    from datetime import datetime, timedelta, timezone

    from shturman.records import MessageRecord
    s = await ready(stand, conn)
    chat_id, _ = await store.ensure_chat(conn, s.acc, ChatRecord("user", ANNA, "personal_chat", "Анна Ким"))
    base = datetime.now(timezone.utc) - timedelta(days=1)
    records = []
    for n, (minutes, outgoing) in enumerate([(0, False), (1, False), (30, True), (31, False), (200, False)], start=1):
        records.append((chat_id, MessageRecord(
            tg_message_id=n, sent_at=base + timedelta(minutes=minutes), kind="message", sender_class="user",
            sender_tg_id=OWNER if outgoing else ANNA, sender_name="x", text="тайный текст", entities=None,
            reply_to_tg_id=None, forwarded_from=None, edited_at=None, media_type=None, media_path=None,
            service_action=None), outgoing))
    await store.upsert_messages(conn, records, source="session", owner_tg_id=OWNER)
    est = (await s.page.get("/triage")).json()["estimate"]
    assert est["incoming_7d"] == 4 and est["batches_7d"] == 3        # 0+1 мин — одна пачка, после ответа — новая
    assert est["tokens_per_day"] > 0 and "тайный" not in str(est)
