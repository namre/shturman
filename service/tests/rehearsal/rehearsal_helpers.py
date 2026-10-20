"""Общее для тестов репетиции ответов (rehearsal/, Р-85): архив, живые сообщения, ответ «модели».

Сервис поднимается с модулями отправки, уведомлений и репетиции. Ожидание пачки (`debounce_seconds`)
в тестах нулевое — подготовка идёт прямо в обработчике события, без таймеров; уведомления тест зовёт
сам (`Notifier.on_message`, `Notifier.flush`), как подписчик и фоновый обход, — без гонок по часам.
Модель заменена прямой работой с очередью заданий: тест берёт задание `llm.structured` и закрывает
его своим ответом через `bridge.deliver_result`, как это сделал бы исполнитель.
"""

import dataclasses
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "notify"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outbox"))

from notify_helpers import ANNA, HELPER, OTHER, OWNER, PETR, chat, configure, message, now, payload  # noqa: E402,F401
from outbox_helpers import FakeTg  # noqa: E402

from shturman import authority, bridge, jobs, store  # noqa: E402
from shturman.notify import core as notify_core  # noqa: E402
from shturman.outbox import autoreply, policy  # noqa: E402
from shturman.rehearsal import core  # noqa: E402
from shturman.rehearsal import settings as rs  # noqa: E402

MODULES = ("shturman.api_core", "shturman.outbox.service", "shturman.rehearsal.service")


@pytest_asyncio.fixture
async def rv(make_client, conn, config, own_bot):
    """Сервис с репетицией и своим ботом согласований; основной аккаунт владельца, помощник, уведомления."""
    cfg = dataclasses.replace(config, send_daily_hard_cap=1000)
    client, state = await make_client(*MODULES, cfg=cfg)
    tg = FakeTg()
    state.extras["tg"] = tg
    with authority.owner_context(OWNER, chat_id=OWNER, action="test.fixture.bind"):
        await bridge.set_owner(conn, OWNER, OWNER)
    owner_acc = await store.ensure_account(conn, OWNER, "Владелец", "owner")
    helper_acc = await store.ensure_account(conn, HELPER, "Помощник", "assistant")
    notifier = notify_core.Notifier(state.pool, tz=lambda: "Europe/Moscow")
    state.extras["notify"] = notifier
    await configure(conn, quiet=False)
    with authority.owner_context(OWNER, chat_id=OWNER, action="test.fixture.autoreply"):
        await autoreply.update(conn, {"debounce_seconds": 0, "pause_seconds": 0})
    return SimpleNamespace(client=client, state=state, conn=conn, tg=tg, n=notifier, mod=state.extras["rehearsal"],
                           owner_acc=owner_acc, helper_acc=helper_acc)


async def enable(conn, people=(ANNA,), **extra) -> dict:
    """Владелец включает репетицию (как со страницы): сразу, без карточки. Паузы в тестах нет, если
    её не задать явно."""
    s = {**await rs.load(conn), "enabled": True, "people": sorted(people), "pause_seconds": 0, **extra}
    await rs.save(conn, s)
    return s


async def live(env, account_id, chat_id, message_id, **flags):
    """Живое сообщение: сначала уведомления (Р-84), потом репетиция — оба подписчика события."""
    data = payload(account_id, chat_id, message_id, **flags)
    kind = await env.n.on_message(data)
    item = await env.mod.on_message(data)
    return kind, item


async def model_jobs(conn) -> list[dict]:
    rows = await conn.fetch("SELECT * FROM jobs WHERE kind = $1 AND handler = $2 ORDER BY id",
                            bridge.LLM_STRUCTURED, core.HANDLER)
    return [{**dict(r), "payload": json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"],
             "context": json.loads(r["context"]) if isinstance(r["context"], str) else r["context"]} for r in rows]


async def answer(conn, job_id: int, parsed: dict | None) -> None:
    """«Модель» вернула ответ на задание репетиции."""
    await conn.execute("UPDATE jobs SET status = 'running' WHERE id = $1", job_id)
    assert await bridge.deliver_result(conn, job_id, {"parsed": parsed, "text": json.dumps(parsed or {}), "model": "m"})


async def fail(conn, job_id: int) -> None:
    await conn.execute("UPDATE jobs SET status = 'running', max_attempts = 1, attempts = 1 WHERE id = $1", job_id)
    await bridge.deliver_failure(conn, job_id, "model: недоступна", retry_in=None)


async def cards(conn) -> list[dict]:
    """Карточки репетиции, вставшие в очередь сообщений владельцу."""
    rows = await conn.fetch("SELECT payload, context, id FROM jobs WHERE kind = $1 AND handler = $2 ORDER BY id",
                            bridge.NOTIFY_OWNER, core.CARD_HANDLER)
    out = []
    for r in rows:
        p = r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"])
        out.append({"id": r["id"], **p})
    return out


async def notifications(conn) -> list[str]:
    rows = await conn.fetch("SELECT payload FROM jobs WHERE kind = $1 AND dedup_key LIKE 'notify:%' ORDER BY id",
                            bridge.NOTIFY_OWNER)
    return [(r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"]))["text"] for r in rows]


async def item(conn, item_id: int) -> dict:
    return dict(await conn.fetchrow("SELECT * FROM rehearsal_items WHERE id = $1", item_id))


async def press(conn, data: str, user: int = OWNER) -> dict:
    """Нажатие кнопки под карточкой — тем же путём, что от бота согласований."""
    if user == OWNER:
        with authority.owner_context(OWNER, chat_id=OWNER, action="telegram.callback"):
            return await bridge.dispatch_callback(conn, data, user)
    return await bridge.dispatch_callback(conn, data, user)


async def nothing_sent(env) -> None:
    """Главное свойство черновика ответа (Р-85, Р-105): без нажатия владельца ничего не согласовано и не
    отправлено — только ждущий черновик сервиса; ни задачи ответа, ни задания отправки, ни обращения к
    отправке Telegram."""
    conn = env.conn
    assert await conn.fetchval("SELECT count(*) FROM outbox_drafts WHERE origin <> 'suggest' "
                               "OR status IN ('approved', 'sending', 'sent', 'outcome_unknown')") == 0
    assert await conn.fetchval("SELECT count(*) FROM reply_tasks") == 0
    assert await conn.fetchval("SELECT count(*) FROM jobs WHERE kind = $1", bridge.BUSINESS_SEND) == 0
    assert env.tg.calls == 0 and env.tg.sent == [] and env.tg.typing == []


__all__ = ["ANNA", "PETR", "OTHER", "OWNER", "HELPER", "chat", "message", "now", "payload", "configure", "jobs",
           "policy", "core", "rs"]
