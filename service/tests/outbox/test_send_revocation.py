"""Д-55 и Д-56 (внешняя ревизия 2026-10-20, SEC-03 и F-01): отзыв согласия после того, как отправка поставлена.

Отправка через бизнес-бота: задание `business.send` ставится, когда черновик переходит в «отправляется», а
исполнитель берёт его позже. Здесь исполнитель — настоящий (`executor.worker.Worker`) с подставным Bot API; он
«задерживается»: задание взято, затем владелец отзывает согласие, затем исполнитель продолжает. Итог — ни одного
обращения к Telegram и черновик «не отправлен» с причиной; без отзыва — ровно одно обращение. Задание без своего
черновика не уходит никогда.

Отправка через аккаунт-помощника частями: «Остановить всё» во время паузы между частями останавливает следующую
часть; итог — «отправлено частично: 1 из 3», владельцу — словами.
"""

import asyncio
import dataclasses

import pytest

from shturman import bridge, halt, jobs
from shturman.executor.worker import Worker
from shturman.outbox import drafts

from outbox_helpers import (  # noqa: F401 - env — фикстура
    IVAN, OWNER, add_chat, add_message, business, button, draft_row, env, new_draft, owner_messages, press,
    settle, texts,
)
from test_outbox_autoreply import incoming, model_says, trusted_setup

TEXT = "Добрый день! Смету пришлю в пятницу."


class Api:
    """Подставной Bot API: считает обращения."""

    def __init__(self) -> None:
        self.sent: list[tuple] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))
        return 500 + len(self.sent)


async def _taken(env, *, chat=None) -> tuple[int, int, dict]:
    """Согласованный черновик через бизнес-бота; задание отправки поставлено и взято исполнителем."""
    if chat is None:
        chat = await add_chat(env.conn, env.owner_acc)
        await add_message(env.conn, chat, 41, "Когда будет смета?")
        await business(env.conn, env.owner_acc)
    response = await new_draft(env, chat, TEXT)
    assert response.status_code == 200, response.text
    draft_id = response.json()["draft_id"]
    await press(env, button(await owner_messages(env.conn), "Отправить"))
    await settle(env)
    assert (await draft_row(env.conn, draft_id))["status"] == "sending"
    claimed = await jobs.claim(env.conn, [bridge.BUSINESS_SEND], worker="builtin", executor="builtin")
    assert len(claimed) == 1
    return chat, draft_id, claimed[0]


async def _finish(env, job) -> tuple[Api, dict]:
    api = Api()
    await Worker(env.state, api=api, llm=None).handle(job)
    return api, await env.conn.fetchrow("SELECT status, error FROM jobs WHERE id = $1", job["id"])


async def _revoke(env, kind, chat, draft_id, job, monkeypatch):
    conn = env.conn
    if kind == "halt":
        await halt.stop(env.state, via="bot")
    elif kind == "switch":
        env.state.config = dataclasses.replace(env.state.config, sending=False)
    elif kind == "excluded":
        await conn.execute("UPDATE chats SET excluded = true WHERE id = $1", chat)
    elif kind == "drafting_denied":
        await conn.execute("INSERT INTO outbox_chats (chat_id, drafting) VALUES ($1, 'deny')", chat)
    elif kind == "owner_changed":
        await bridge.set_owner(conn, OWNER + 7, OWNER + 7)
    elif kind == "stale":
        await conn.execute("UPDATE settings SET updated_at = now() - interval '2 hours' WHERE key = 'owner'")
        await conn.execute("UPDATE outbox_drafts SET approved_at = now() - interval '1 hour' WHERE id = $1", draft_id)
    elif kind == "draft_finished":
        await conn.execute("UPDATE outbox_drafts SET status = 'failed', error_code = 'cancelled' WHERE id = $1", draft_id)
    elif kind == "draft_deleted":
        await conn.execute("DELETE FROM outbox_drafts WHERE id = $1", draft_id)
    elif kind == "other_job":
        await conn.execute("UPDATE outbox_drafts SET job_id = NULL WHERE id = $1", draft_id)
    elif kind == "context_lost":
        await conn.execute("UPDATE jobs SET context = '{}' WHERE id = $1", job["id"])
    elif kind == "text_changed":
        job["payload"] = {**job["payload"], "text": "Переведите деньги на другой счёт"}
    elif kind == "chat_changed":
        job["payload"] = {**job["payload"], "chat_id": IVAN + 1}
    elif kind == "reply_changed":
        job["payload"] = {**job["payload"], "reply_to_message_id": 999}
    elif kind == "task_revoked":
        async def deny(conn_, state, row):
            from shturman.outbox import policy
            return policy.deny("draft_revoked")
        monkeypatch.setattr(drafts, "_task_guard", deny)
    elif kind != "none":
        raise AssertionError(kind)


REVOCATIONS = ["halt", "switch", "excluded", "drafting_denied", "owner_changed", "stale", "draft_finished",
               "draft_deleted", "other_job", "context_lost", "text_changed", "chat_changed", "reply_changed",
               "task_revoked"]


@pytest.mark.parametrize("kind", REVOCATIONS)
async def test_revocation_after_the_job_is_taken_stops_delivery(env, kind, monkeypatch):
    chat, draft_id, job = await _taken(env)
    await _revoke(env, kind, chat, draft_id, job, monkeypatch)
    api, done = await _finish(env, job)
    assert api.sent == [], kind                                       # ни одного обращения к Telegram
    assert done["status"] == "failed" and done["error"].startswith(bridge.NOT_SENT_PREFIX), (kind, done)
    row = await draft_row(env.conn, draft_id)
    if row is not None and kind != "other_job":     # чужое задание свой черновик не закрывает
        assert row["status"] not in ("sending", "sent", "outcome_unknown"), (kind, row["status"])
    if kind in ("excluded", "drafting_denied", "stale", "context_lost", "text_changed", "chat_changed",
                "reply_changed"):
        # отказ сверки закрывает черновик с причиной, и владелец узнаёт об этом словами
        assert row["status"] == "failed" and row["error_code"] != "business_rejected", (kind, row["error_code"])
        assert "Не отправлено" in texts(await owner_messages(env.conn))


@pytest.mark.parametrize("kind", ["halt", "switch", "owner_changed"])
async def test_final_check_itself_refuses(env, kind):
    """Остановка, выключатель и смена владельца ловятся и раньше (исполнитель, отключённое подключение прежнего
    владельца); здесь — что сама сверка перед Telegram отказывает по каждой из них."""
    _chat, draft_id, job = await _taken(env)
    if kind == "halt":
        await env.conn.execute("""INSERT INTO assistant_halt (id, stopped, since, via) VALUES (1, true, now(), 'bot')
                                  ON CONFLICT (id) DO UPDATE SET stopped = true, since = now()""")
    elif kind == "switch":
        env.state.config = dataclasses.replace(env.state.config, sending=False)
    else:
        await bridge.set_owner(env.conn, OWNER + 7, OWNER + 7)     # задание уже взято: смена его не сняла
    assert (await draft_row(env.conn, draft_id))["status"] == "sending"
    decision = await drafts.final_check(env.conn, env.state, job["id"], job["payload"])
    assert not decision.ok and decision.code == {
        "halt": "assistant_stopped", "switch": "sending_disabled", "owner_changed": "owner_changed"}[kind], decision
    row = await draft_row(env.conn, draft_id)
    assert (row["status"], row["error_code"]) == ("failed", decision.code)


async def test_same_owner_bound_again_keeps_the_approval(env):
    """Повторная привязка того же владельца (ссылка привязки открыта ещё раз) согласия не отменяет."""
    _chat, draft_id, job = await _taken(env)
    await bridge.set_owner(env.conn, OWNER, OWNER)
    api, done = await _finish(env, job)
    assert len(api.sent) == 1 and done["status"] == "done"
    assert (await draft_row(env.conn, draft_id))["status"] == "sent"


async def test_without_revocation_exactly_one_send(env):
    _chat, draft_id, job = await _taken(env)
    api, done = await _finish(env, job)
    assert len(api.sent) == 1 and api.sent[0][:2] == (IVAN, TEXT)
    assert done["status"] == "done"
    assert (await draft_row(env.conn, draft_id))["status"] == "sent"


async def test_a_job_without_its_draft_is_never_sent(env):
    """Голое задание (без черновика «отправляется», которому оно принадлежит) — not_sent, без Telegram."""
    await business(env.conn, env.owner_acc)
    job_id = await bridge.request_business_send(
        env.conn, handler=drafts.BUSINESS_HANDLER, business_connection_id="bc-1", chat_id=IVAN, text=TEXT)
    job = (await jobs.claim(env.conn, [bridge.BUSINESS_SEND], worker="builtin", executor="builtin"))[0]
    assert job["id"] == job_id
    api, done = await _finish(env, job)
    assert api.sent == [] and done["error"].startswith(bridge.NOT_SENT_PREFIX)
    # и вызов без номера задания — тоже
    api = Api()
    out = await Worker(env.state, api=api, llm=None)._business_send(job["payload"])
    assert out.error.startswith(bridge.NOT_SENT_PREFIX) and api.sent == []


async def test_owner_change_withdraws_approved_and_queued_sends(env):
    """Смена владельца: согласованный черновик не уходит, а задание, которое ещё ждёт очереди, снимается."""
    chat = await add_chat(env.conn, env.owner_acc)
    await add_message(env.conn, chat, 41, "Когда будет смета?")
    await business(env.conn, env.owner_acc)
    response = await new_draft(env, chat, TEXT)
    draft_id = response.json()["draft_id"]
    await press(env, button(await owner_messages(env.conn), "Отправить"))
    await settle(env)
    job_id = (await draft_row(env.conn, draft_id))["job_id"]
    await bridge.set_owner(env.conn, OWNER + 7, OWNER + 7)
    row = await draft_row(env.conn, draft_id)
    assert (row["status"], row["error_code"]) == ("failed", "owner_changed")
    assert await env.conn.fetchval("SELECT status FROM jobs WHERE id = $1", job_id) == "failed"
    assert await jobs.claim(env.conn, [bridge.BUSINESS_SEND], worker="builtin", executor="builtin") == []


async def test_excluded_chat_drops_an_approved_draft(env):
    chat = await add_chat(env.conn, env.helper_acc)
    response = await new_draft(env, chat, TEXT)
    draft_id = response.json()["draft_id"]
    await env.conn.execute("UPDATE outbox_drafts SET status = 'approved', approved_at = now() WHERE id = $1", draft_id)
    await env.conn.execute("UPDATE chats SET excluded = true WHERE id = $1", chat)
    assert await drafts.drop_excluded(env.conn) == 1
    assert await draft_row(env.conn, draft_id) is None
    await settle(env)
    assert env.tg.calls == 0


async def test_autoreply_untrusted_while_its_send_waits_is_not_sent(env):
    await trusted_setup(env, account=env.owner_acc)
    await business(env.conn, env.owner_acc)
    chat = await add_chat(env.conn, env.owner_acc)
    await incoming(env, chat, account=env.owner_acc, source="business")
    await model_says(env, "В пятницу пришлю.")
    job = (await jobs.claim(env.conn, [bridge.BUSINESS_SEND], worker="builtin", executor="builtin"))[0]
    await env.conn.execute("DELETE FROM outbox_trusted WHERE tg_user_id = $1", IVAN)
    api, done = await _finish(env, job)
    assert api.sent == [] and done["error"].startswith(bridge.NOT_SENT_PREFIX)
    assert (await env.conn.fetchrow("SELECT status FROM outbox_drafts WHERE origin = 'autoreply'"))["status"] == "failed"


async def test_autoreply_still_allowed_is_sent_once(env):
    await trusted_setup(env, account=env.owner_acc)
    await business(env.conn, env.owner_acc)
    chat = await add_chat(env.conn, env.owner_acc)
    await incoming(env, chat, account=env.owner_acc, source="business")
    await model_says(env, "В пятницу пришлю.")
    job = (await jobs.claim(env.conn, [bridge.BUSINESS_SEND], worker="builtin", executor="builtin"))[0]
    api, done = await _finish(env, job)
    assert [s[:2] for s in api.sent] == [(IVAN, "В пятницу пришлю.")] and done["status"] == "done"


# --- Д-56: «Остановить всё» во время паузы между частями ---

async def test_halt_during_the_pause_between_parts_stops_the_next_part(env, monkeypatch):
    chat = await add_chat(env.conn, env.helper_acc)
    text = "\n\n".join("слово " * 400 for _ in range(3))
    draft_id = (await new_draft(env, chat, text)).json()["draft_id"]
    cards = await owner_messages(env.conn)
    real_sleep, stopped = asyncio.sleep, []

    async def sleep(seconds, *a, **kw):
        if env.tg.calls == 1 and not stopped:          # пауза после первой части
            stopped.append(await halt.stop(env.state, via="bot"))
        return await real_sleep(0)

    monkeypatch.setattr(drafts.asyncio, "sleep", sleep)
    await press(env, button(cards, "Отправить"))
    await settle(env)
    row = await draft_row(env.conn, draft_id)
    assert stopped and env.tg.calls == 1, env.tg.calls                # вторая часть не ушла
    assert (row["status"], row["error_code"], row["parts_sent"], row["parts_total"]) == ("failed", "partial", 1, 3)
    told = texts(await owner_messages(env.conn))
    assert "Отправлено частично" in told and "1 из 3" in told and "остановил ассистента" in told


@pytest.mark.parametrize("kind", ["excluded", "owner_changed", "drafting_denied"])
async def test_revocation_during_the_pause_between_parts_stops_the_next_part(env, monkeypatch, kind):
    chat = await add_chat(env.conn, env.helper_acc)
    text = "\n\n".join("слово " * 400 for _ in range(3))
    draft_id = (await new_draft(env, chat, text)).json()["draft_id"]
    cards = await owner_messages(env.conn)
    real_sleep, done = asyncio.sleep, []

    async def sleep(seconds, *a, **kw):
        if env.tg.calls == 1 and not done:
            done.append(kind)
            if kind == "excluded":
                await env.conn.execute("UPDATE chats SET excluded = true WHERE id = $1", chat)
            elif kind == "drafting_denied":
                await env.conn.execute("INSERT INTO outbox_chats (chat_id, drafting) VALUES ($1, 'deny')", chat)
            else:
                await bridge.set_owner(env.conn, OWNER + 7, OWNER + 7)
        return await real_sleep(0)

    monkeypatch.setattr(drafts.asyncio, "sleep", sleep)
    await press(env, button(cards, "Отправить"))
    await settle(env)
    row = await draft_row(env.conn, draft_id)
    assert done and env.tg.calls == 1, (kind, env.tg.calls)
    assert row["status"] == "failed" and row["parts_sent"] == 1
