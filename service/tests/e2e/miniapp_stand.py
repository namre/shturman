"""Стенд для браузерной проверки Mini App бота согласований: настоящий сервис, подставной Telegram.

Что настоящее: сервис переписки целиком (все модули, uvicorn, Postgres), страница приложения и её
файлы, проверка данных входа. Что подставное: Bot API (из tests/executor) и сам Telegram-клиент —
данные входа (`initData`) подписывает пульт стенда тем же способом, что Telegram, токеном бота
согласований стенда. В сеть стенд не ходит.

Запуск — README.md в этом каталоге. Стенд пересоздаёт схему `public` в базе из SHTURMAN_TEST_DSN:
база должна быть отдельной тестовой.

При запуске стенд привязывает владельца к боту согласований (как «Запустить» по ссылке) и заводит
то, что ждёт решения: подтверждение действия, предложение завести страницу о человеке (уже ушедшее
сводкой) и черновик ответа «только для просмотра» (отправка на стенде выключена, как по умолчанию).

Пульт (порт сервиса + 1):
  GET  /init-data?who=owner|stranger&age=N   строка initData, подписанная как у Telegram (age — сколько секунд назад)
  GET  /state                                что стало с заведённым, журнал действий, вызовы Bot API
  GET  /framer                               чужая страница, которая пытается открыть приложение в рамке
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

HERE = Path(__file__).resolve().parent
for extra in (HERE.parent, HERE.parent / "executor"):
    sys.path.insert(0, str(extra))

import asyncpg  # noqa: E402
import uvicorn  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

import exec_fakes  # noqa: E402
from shturman import confirm, db, store  # noqa: E402
from shturman.app import build_app  # noqa: E402
from shturman.config import Config  # noqa: E402
from shturman.executor import binding  # noqa: E402
from shturman.executor import service as executor_service  # noqa: E402
from shturman.outbox import drafts  # noqa: E402
from shturman.records import ChatRecord, MessageRecord  # noqa: E402
from shturman.setup_page import initdata  # noqa: E402

DSN = os.environ.get("SHTURMAN_TEST_DSN", "")
PORT = int(os.environ.get("E2E_PORT", "8775"))
ORIGIN = f"http://localhost:{PORT}"
TOKEN = exec_fakes.TOKEN


@confirm.applier("t.e2e")
async def _apply(conn, payload):
    return "Сделано на стенде."


def signed(user: dict, age: int) -> str:
    fields = {"query_id": f"AAE{int(time.time() * 1000)}", "auth_date": str(int(time.time()) - age),
              "user": json.dumps({"id": user["id"], "first_name": user["first_name"]}, ensure_ascii=False,
                                 separators=(",", ":"))}
    fields["hash"] = initdata.sign(fields, TOKEN)
    return urlencode(fields)


FRAMER = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Чужой сайт</title></head>
<body><h1>Чужой сайт</h1><iframe id="f" src="{ORIGIN}/shturman-setup/app/" width="400" height="300"></iframe></body></html>"""


async def main() -> None:
    if not DSN:
        sys.exit("нужна переменная SHTURMAN_TEST_DSN с отдельной тестовой базой")
    conn = await asyncpg.connect(DSN)
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await db.migrate(conn)

    data_dir = Path(os.environ.get("E2E_DATA") or tempfile.mkdtemp(prefix="shturman-miniapp-"))
    config = Config(
        dsn=DSN, api_token="e2e-api-token-0123456789abcdef0123456789",
        mcp_token="e2e-mcp-token-0123456789abcdef0123456789", host="127.0.0.1", port=PORT, data_dir=data_dir,
        allowed_hosts=(f"127.0.0.1:{PORT}", f"localhost:{PORT}"), bot_token=TOKEN, timezone="Europe/Moscow")
    telegram = exec_fakes.FakeTelegram()
    executor_service.TEST_OVERRIDES.update(bot_transport=telegram.transport(), poll=0, idle=0.2)
    gate = build_app(config, migrate=False)
    seeded: dict = {}

    async def init_data(request):
        who = request.query_params.get("who", "owner")
        user = exec_fakes.OWNER_USER if who == "owner" else exec_fakes.STRANGER_USER
        return PlainTextResponse(signed(user, int(request.query_params.get("age", "0"))))

    async def state_view(request):
        return JSONResponse({
            "action": await conn.fetchval("SELECT status FROM pending_actions WHERE id = $1", seeded["action"]),
            "page": await conn.fetchval("SELECT status FROM page_proposals WHERE person_id = $1", seeded["person"]),
            "draft": await conn.fetchval("SELECT status FROM outbox_drafts WHERE id = $1", seeded["draft"]),
            "sessions": await conn.fetchval("SELECT count(*) FROM miniapp_sessions"),
            "audit": [dict(r) for r in await conn.fetch(
                "SELECT action, outcome, detail FROM setup_audit WHERE action LIKE 'app.%' ORDER BY id")],
            "bot_methods": [name for name, _ in telegram.requests if name != "getUpdates"][-30:],
        })

    async def framer(request):
        return HTMLResponse(FRAMER)

    control = Starlette(routes=[Route("/init-data", init_data), Route("/state", state_view), Route("/framer", framer)])

    async with contextlib.AsyncExitStack() as stack:
        await stack.enter_async_context(gate.inner.router.lifespan_context(gate.inner))
        app = gate.inner.state.shturman
        await exec_fakes.until(lambda: getattr(app.extras["executor"].bot, "identity", None))
        code, _ = await binding.create_code(conn)
        telegram.text(f"/start {code}", user=exec_fakes.OWNER_USER)
        await exec_fakes.until(lambda: binding.bound_owner(conn, exec_fakes.BOT_ID))

        seeded["action"] = (await confirm.request(conn, "t.e2e", "Включить чтение чата «Стенд»", {"n": 1}))["action_id"]
        seeded["person"] = await conn.fetchval("INSERT INTO people (display_name) VALUES ('Мария Сидорова') RETURNING id")
        await conn.execute("""INSERT INTO page_proposals (person_id, reason, batch, pos, notified_at)
                              VALUES ($1, '{"messages": 30}', 'pg1', 1, now())""", seeded["person"])
        account = await store.ensure_account(conn, 1500, "Помощник", "assistant")
        chat_id, _ = await store.ensure_chat(conn, account, ChatRecord("user", 2001, "personal_chat", "Иван Петров"))
        record = MessageRecord(tg_message_id=1, sent_at=datetime.now(timezone.utc) - timedelta(days=30), kind="message",
                               sender_class="user", sender_tg_id=1000, sender_name="Я", text="Здравствуйте!",
                               entities=None, reply_to_tg_id=None, forwarded_from=None, edited_at=None,
                               media_type=None, media_path=None, service_action=None)
        await store.upsert_messages(conn, [(chat_id, record, True)], source="session", owner_tg_id=1000)
        seeded["draft"] = (await drafts.create(conn, app, chat_id=chat_id, prepare_only=True,
                                               text="Добрый день! Смету пришлю в пятницу.\nС уважением."))["draft_id"]

        servers = [
            uvicorn.Server(uvicorn.Config(gate, host="127.0.0.1", port=PORT, log_level="warning",
                                          access_log=False, proxy_headers=False, server_header=False, lifespan="off")),
            uvicorn.Server(uvicorn.Config(control, host="127.0.0.1", port=PORT + 1, log_level="warning",
                                          access_log=False)),
        ]
        print(json.dumps({"ready": True, "app": f"{ORIGIN}/shturman-setup/app/",
                          "control": f"http://127.0.0.1:{PORT + 1}", **seeded}), flush=True)
        await asyncio.gather(*(server.serve() for server in servers))
    executor_service.TEST_OVERRIDES.clear()
    await conn.close()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
