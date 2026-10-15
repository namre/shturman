"""Общее для тестов разбора входящих (triage/, Р-92): архив, живые сообщения, ответ «модели», карточки.

Сервис поднимается с модулями разбора и репетиции (кнопка «Подготовить ответ» под карточкой — разовая
репетиция); фоновый обход разбора снят, уведомления (Р-84) тест зовёт сам (`Notifier.on_message`,
`Notifier.flush`) — без гонок по часам. Пачки (packs.py) в тестах выключены: каждое сообщение — своя,
сразу закрытая пачка, и разбор просится прямо в обработчике события; тест пачки задаёт паузу сам.
Модель заменена прямой работой с очередью заданий: тест закрывает задание `llm.structured` своим ответом
через `bridge.deliver_result`, как это сделал бы исполнитель.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "notify"))

from notify_helpers import (ANNA, HELPER, OTHER, OWNER, PETR, chat, configure, entity, mention, message,  # noqa: E402,F401
                            now, payload)

from shturman import authority, bridge, store  # noqa: E402
from shturman.notify import core as notify_core  # noqa: E402
from shturman.triage import core  # noqa: E402
from shturman.triage import settings as ts  # noqa: E402

GROUP = 3001
MODULES = ("shturman.api_core", "shturman.triage.service", "shturman.rehearsal.service")


@pytest_asyncio.fixture
async def tv(make_client, conn, own_bot):
    return await setup_env(make_client, conn)


async def setup_env(make_client, conn, modules=MODULES):
    client, state = await make_client(*modules)
    for task in list(state._tasks):
        if task.get_name() in ("triage-sweeper", "rehearsal-sweeper"):
            task.cancel()          # обход тест зовёт сам
    with authority.owner_context(OWNER, chat_id=OWNER, action="test.fixture.bind"):
        await bridge.set_owner(conn, OWNER, OWNER)
    owner_acc = await store.ensure_account(conn, OWNER, "Владелец", "owner")
    helper_acc = await store.ensure_account(conn, HELPER, "Помощник", "assistant")
    await store.ensure_peer(conn, "user", OWNER, name="Евгений", username="evgenii_e")
    notifier = notify_core.Notifier(state.pool, tz=lambda: "Europe/Moscow")
    state.extras["notify"] = notifier
    await configure(conn, quiet=False)
    mod = state.extras["triage"]
    return SimpleNamespace(client=client, state=state, conn=conn, n=notifier, t=mod, owner_acc=owner_acc,
                           helper_acc=helper_acc)


async def enable(conn, **rules) -> dict:
    """Разбор включён (как после нажатия владельца в боте) и правила."""
    s = ts.merge({**await ts.load(conn), "enabled": True}, ts.validate(rules))
    await ts.save(conn, s)
    return s


async def group(env, tg_id=GROUP, *, name="Корпус 3"):
    return await chat(env.conn, env.owner_acc, tg_id, cls="channel", type_="private_supergroup", name=name)


async def live(env, account_id, chat_id, message_id, **flags):
    """Живое сообщение: оба подписчика события — уведомления и разбор. (вид уведомления, строка разбора)."""
    data = payload(account_id, chat_id, message_id, **flags)
    return await env.n.on_message(data), await env.t.on_message(data)


async def model_jobs(conn) -> list[dict]:
    rows = await conn.fetch("SELECT * FROM jobs WHERE kind = $1 AND handler = $2 ORDER BY id",
                            bridge.LLM_STRUCTURED, core.HANDLER)
    return [{**dict(r), "payload": json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"],
             "context": json.loads(r["context"]) if isinstance(r["context"], str) else r["context"]} for r in rows]


def verdict(needs_reply=True, urgency="normal", want="просит смету по корпусу 3", due="", thread="") -> dict:
    return {"needs_reply": needs_reply, "urgency": urgency, "want": want, "due": due, "thread": thread}


async def answer(conn, job_id: int, parsed: dict | None) -> None:
    """«Модель» вернула ответ на задание разбора."""
    await conn.execute("UPDATE jobs SET status = 'running' WHERE id = $1", job_id)
    assert await bridge.deliver_result(conn, job_id, {"parsed": parsed, "text": json.dumps(parsed or {}), "model": "m"})


async def answer_last(conn, parsed: dict | None) -> None:
    jobs = await model_jobs(conn)
    await answer(conn, jobs[-1]["id"], parsed)


async def fail(conn, job_id: int) -> None:
    await conn.execute("UPDATE jobs SET status = 'running', max_attempts = 1, attempts = 1 WHERE id = $1", job_id)
    await bridge.deliver_failure(conn, job_id, "model: недоступна", retry_in=None)


async def owner_messages(conn, prefix="triage:") -> list[dict]:
    """Сообщения владельцу из разбора (карточки, напоминания, предупреждения) по порядку."""
    rows = await conn.fetch("SELECT payload, dedup_key FROM jobs WHERE kind = $1 AND dedup_key LIKE $2 ORDER BY id",
                            bridge.NOTIFY_OWNER, prefix + "%")
    out = []
    for r in rows:
        p = r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"])
        for row in p.get("buttons") or []:
            for b in row:      # кнопок отправки нет: только кнопки разбора
                assert b["data"].startswith("sh:tr:") and b["text"] in (
                    core.PREPARE, core.REMIND, core.NOT_IMPORTANT, core.MUTE), b
        out.append({**p, "key": r["dedup_key"]})
    return out


async def cards(conn) -> list[dict]:
    return [m for m in await owner_messages(conn) if ":model-failed:" not in m["key"]]


async def notifications(conn) -> list[str]:
    """Прежние уведомления «Вам написали» / «Вас упомянули», вставшие в очередь."""
    rows = await conn.fetch("SELECT payload FROM jobs WHERE kind = $1 AND dedup_key LIKE 'notify:%' ORDER BY id",
                            bridge.NOTIFY_OWNER)
    return [(r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"]))["text"] for r in rows]


async def item(conn, item_id: int) -> dict:
    return dict(await conn.fetchrow("SELECT * FROM triage_items WHERE id = $1", item_id))


async def press(conn, data: str, user: int = OWNER, action: str = "telegram.callback") -> dict:
    if user == OWNER:
        with authority.owner_context(OWNER, chat_id=OWNER, action=action):
            return await bridge.dispatch_callback(conn, data, user)
    return await bridge.dispatch_callback(conn, data, user)


def button(card: dict, text: str) -> str:
    return next(b["data"] for row in card["buttons"] for b in row if b["text"] == text)


def at(minutes: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


__all__ = ["ANNA", "PETR", "OTHER", "OWNER", "HELPER", "GROUP", "chat", "message", "now", "payload", "configure",
           "mention", "entity", "core", "ts", "notify_core"]
