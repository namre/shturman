"""Пульт стендов для пакета 4б ревизии удобства (Р-95): «История», «Ответы и доверие», расписание сводок.

Подключается к обоим стендам (`stand.py` — страница, `miniapp_stand.py` — приложение):

  POST /pack4b/seed     два личных чата основного аккаунта («Анна Ким», «Борис Ли») с сообщением; Анна —
                        в доверенных (нажатием в боте — строка «Истории»); просьба ассистента сделать Бориса
                        доверенным (ждёт в боте); разрешение прочитать переписку для ответа и доступ внешней
                        программы к архиву; ответ плагина Hermes о сводках (08:27 и пт 17:47) — без этого блока
                        сводок нет, как в режиме без Hermes
  POST /pack4b/plugin   плагин Hermes забирает задания расписания и отвечает, что применил выбранное
  POST /pack4b/say?text=…  владелец пишет боту согласований (/sources, /trusted …)
  GET  /pack4b/seen     что стало: доверенные, ждущие карточки `owner.widen`, действующие разрешения,
                        выбор и совпадение сводок, строки «Истории», журнал
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from starlette.responses import JSONResponse
from starlette.routing import Route

import exec_fakes
from shturman import authority, bridge, confirm, history, jobs, store, summaries
from shturman.outbox import service as outbox
from shturman.records import ChatRecord, MessageRecord

ANNA, BORIS = 5101, 5102


async def _owner_account(conn) -> int:
    found = await conn.fetchval("SELECT id FROM accounts WHERE role = 'owner' ORDER BY id LIMIT 1")
    return found or await store.ensure_account(conn, exec_fakes.OWNER, "Владелец", "owner")


async def _chat(conn, account: int, tg_id: int, name: str, minutes_ago: int) -> int:
    chat, _ = await store.ensure_chat(conn, account, ChatRecord("user", tg_id, "personal_chat", name))
    record = MessageRecord(tg_message_id=7000 + tg_id % 100, sent_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
                           kind="message", sender_class="user", sender_tg_id=tg_id, sender_name=name, text="Добрый день!",
                           entities=None, reply_to_tg_id=None, forwarded_from=None, edited_at=None, media_type=None,
                           media_path=None, service_action=None)
    await store.upsert_messages(conn, [(chat, record, False)], source="session", owner_tg_id=exec_fakes.OWNER)
    return chat


def _hermes(desired: dict | None) -> dict:
    m = (desired or {}).get("morning-brief") or {"enabled": True, "time": "08:27"}
    w = (desired or {}).get("weekly-review") or {"enabled": True, "time": "17:47", "day": 5}
    days = ("вс", "пн", "вт", "ср", "чт", "пт", "сб")
    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
    return {"timezone": "Europe/Moscow", "jobs": {
        "morning-brief": {"exists": True, "enabled": m["enabled"], "time": m["time"],
                          **({"next_run_at": tomorrow.isoformat(),
                              "next_local": f"{days[(tomorrow.weekday() + 1) % 7]} {tomorrow:%d.%m} {m['time']}"}
                             if m["enabled"] else {})},
        "weekly-review": {"exists": True, "enabled": w["enabled"], "time": w["time"], "day": w["day"],
                          **({"next_local": f"{days[w['day']]} {w['time']}"} if w["enabled"] else {})}}}


async def seed(conn) -> dict:
    account = await _owner_account(conn)
    anna = await _chat(conn, account, ANNA, "Анна Ким", 5)
    boris = await _chat(conn, account, BORIS, "Борис Ли", 30)
    with authority.owner_context(exec_fakes.OWNER, chat_id=exec_fakes.OWNER, action="telegram.command"):
        await confirm.apply_owner(conn, outbox.TRUSTED_ADD, {"tg_user_id": ANNA, "note": None})
    asked = await confirm.request(conn, outbox.TRUSTED_ADD,
                                  "Добавить в доверенные: Борис Ли. Если автоответ включён, ассистент будет отвечать этому "
                                  "человеку сам, без вашего согласования.", {"tg_user_id": BORIS, "note": None})
    grant = await conn.fetchval(
        """INSERT INTO source_grants (task_id, target_chat_id, kind, request, mode, owner_id, expires_at)
           VALUES (NULL, $1, 'chat', '{"query": "смета по фасадам"}', 'read', $2, now() + interval '30 days')
           RETURNING id""", anna, exec_fakes.OWNER)
    await conn.execute("""INSERT INTO remote_mcp_clients (id, name, redirect_uris) VALUES ('e2e-claude', 'Claude', '[]')
                          ON CONFLICT DO NOTHING""")
    mcp = await conn.fetchval("""INSERT INTO remote_mcp_grants (client_id, owner_id, resource, expires_at)
                                 VALUES ('e2e-claude', $1, 'archive', now() + interval '30 days') RETURNING id""",
                              exec_fakes.OWNER)
    await summaries._reported(conn, {"id": 0}, {**_hermes(None), "applied": False, "revision": 0})
    return {"anna": anna, "boris": boris, "grant": grant, "mcp": mcp, "asked": asked.get("action_id")}


async def plugin(conn) -> dict:
    """Плагин Hermes: забрать задания `hermes.cron` и ответить так, будто выбранное применено."""
    claimed = sorted(await jobs.claim(conn, [bridge.HERMES_CRON], worker="e2e-plugin", limit=10), key=lambda j: j["id"])
    for job in claimed:
        payload = job["payload"]
        result = {**_hermes(payload.get("desired")), "applied": bool(payload.get("apply")),
                  "revision": payload.get("revision")}
        await bridge.deliver_result(conn, job["id"], result, executor="plugin")
    return {"claimed": len(claimed)}


async def seen(conn) -> dict:
    view = await summaries.view(conn)
    return {
        "trusted": sorted(r["tg_user_id"] for r in await conn.fetch("SELECT tg_user_id FROM outbox_trusted")),
        "widen_pending": await conn.fetchval(
            "SELECT count(*) FROM pending_actions WHERE kind = 'owner.widen' AND status = 'pending'"),
        "grants_active": await conn.fetchval("SELECT count(*) FROM source_grants WHERE revoked_at IS NULL"),
        "mcp_active": await conn.fetchval("SELECT count(*) FROM remote_mcp_grants WHERE revoked_at IS NULL"),
        "summaries": {"chosen": view["chosen"], "state": view["sync"]["state"], "choice": view["choice"]},
        "history": [{"title": e["title"], "via": e["via"], "undone": e["undone"]} for e in await history.entries(conn)],
        "audit": [dict(r) for r in await conn.fetch(
            """SELECT action, outcome, detail FROM setup_audit
               WHERE action IN ('replies.change', 'app.replies', 'app.replies_request', 'history.revert',
                                'history.revert_request', 'grant.revoke', 'app.revoke', 'app.history_revert',
                                'summaries.schedule') ORDER BY id""")],
    }


def routes(conn, telegram) -> list[Route]:
    async def seed_route(request):
        try:
            return JSONResponse(await seed(conn))
        except Exception:  # noqa: BLE001 — стенд: причину — в ответ
            import traceback
            return JSONResponse({"error": traceback.format_exc()}, status_code=500)

    async def plugin_route(request):
        return JSONResponse(await plugin(conn))

    async def say_route(request):
        telegram.text(request.query_params["text"])
        await asyncio.sleep(0.6)
        return JSONResponse({"ok": True})

    async def seen_route(request):
        return JSONResponse(json.loads(json.dumps(await seen(conn), default=str)))

    return [Route("/pack4b/seed", seed_route, methods=["POST"]), Route("/pack4b/plugin", plugin_route, methods=["POST"]),
            Route("/pack4b/say", say_route, methods=["POST"]), Route("/pack4b/seen", seen_route)]
