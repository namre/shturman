"""«В чат» (Р-105, часть 2): текст ответа — в поле ввода личного чата основного аккаунта, по нажатию владельца.

Главное:
  * «В чат» есть, только когда «Отправить» нет, и только у «Черновика ответа» сервиса и «Записано» в личном
    чате основного аккаунта при своём боте и сессии на связи; у черновика ассистента и в чате помощника — нет;
  * кладёт только нажатие проверенного владельца — в боте или в приложении; фоновая работа, команда и страница —
    отказ (мутационный якорь `put_requires_owner_press`);
  * поле ввода с текстом владельца не перезаписывается, а сам этот текст нигде не сохраняется и не пишется в
    журнал (мутационный якорь `put_never_overwrites_owner_text`);
  * ничего не отправляется: ни одного вызова отправки, ни задания `business.send`;
  * при остановке ассистента — отказ, в поле ничего не кладётся.
Telegram — подставной шлюз (`outbox_helpers.FakeTg`); путь до настоящего клиента Telethon — `tests/tg/test_tg_policy.py`.
"""

import logging

import pytest

from shturman import authority, bridge
from shturman.outbox import drafts
from shturman.replies import put

from rehearsal_helpers import (ANNA, OWNER, answer, cards, chat, enable, item, live, message, model_jobs,  # noqa: F401
                               nothing_sent, press, rv)

OWN_TEXT = "Мой незаконченный ответ про аванс"


async def _card(rv, text="Да, в четверг в десять."):
    await enable(rv.conn)
    rv.tg.puttable.add(rv.owner_acc)
    anna = await chat(rv.conn, rv.owner_acc, ANNA, name="Анна Ким", username="anna_kim")
    mid = await message(rv.conn, anna, 7, "Встречаемся в четверг?")
    _, item_id = await live(rv, rv.owner_acc, anna, mid)
    (job,) = await model_jobs(rv.conn)
    await answer(rv.conn, job["id"], {"outcome": "reply", "text": text})
    (card,) = await cards(rv.conn)
    draft = await rv.conn.fetchrow("SELECT * FROM outbox_drafts WHERE origin = 'suggest'")
    return anna, item_id, card, draft


def _data(card, prefix):
    return [b["data"] for row in card["buttons"] for b in row if b.get("data", "").startswith(prefix)]


async def test_put_in_chat_lays_the_text_into_the_input_field_and_sends_nothing(rv):
    anna, item_id, card, draft = await _card(rv)
    assert [[b["text"] for b in r] for r in card["buttons"]][0] == ["В чат"]
    assert not _data(card, "sh:ob:s:")                 # «Отправить» и «В чат» вместе не бывают
    assert "«В чат» положит текст в поле ввода этого чата" in card["text"]
    [data] = _data(card, "sh:ob:p:")
    # карточка доставлена владельцу (как это записывает бот после отправки)
    await rv.conn.execute("""UPDATE outbox_drafts SET card_messages = '{"1": 555}'::jsonb WHERE id = $1""", draft["id"])
    out = await press(rv.conn, data)
    assert out["answer"].startswith("Положил в поле ввода")
    key = (rv.owner_acc, ANNA)
    assert rv.tg.fields[key] == "Да, в четверг в десять."
    assert rv.tg.put_calls[-1]["reply_to_tg_id"] == 7
    row = await rv.conn.fetchrow("SELECT * FROM outbox_drafts WHERE id = $1", draft["id"])
    assert row["status"] == "placed"
    rated = await item(rv.conn, item_id)
    assert (rated["rating"], rated["rated_via"]) == ("good", "put")
    log = await rv.conn.fetchval("SELECT detail FROM setup_audit WHERE action = 'draft.put'")
    assert log == f"черновик № {draft['id']}; итог: placed"
    # карточка — с итогом и без кнопок
    edit = await rv.conn.fetchrow("SELECT payload FROM jobs WHERE kind = $1 ORDER BY id DESC LIMIT 1", bridge.NOTIFY_EDIT)
    import json
    payload = edit["payload"] if isinstance(edit["payload"], dict) else json.loads(edit["payload"])
    assert "положен в поле ввода чата, отправляете вы" in payload["text"] and payload.get("remove_buttons")
    # повторное нажатие — «Уже недоступно», второго обращения к полю нет
    calls = len(rv.tg.put_calls)
    await press(rv.conn, data)
    assert len(rv.tg.put_calls) == calls
    assert rv.tg.sent == [] and await rv.conn.fetchval(
        "SELECT count(*) FROM jobs WHERE kind = $1", bridge.BUSINESS_SEND) == 0


async def test_owner_text_in_the_field_is_never_overwritten_nor_kept(rv, caplog):
    """Мутационный якорь: в поле ввода — свой текст владельца: «В чат» его не трогает; сам этот текст нигде не
    сохраняется — ни в базе, ни в журнале действий, ни в журнале сервиса."""
    anna, item_id, card, draft = await _card(rv)
    rv.tg.fields[(rv.owner_acc, ANNA)] = OWN_TEXT
    with caplog.at_level(logging.DEBUG):
        out = await press(rv.conn, _data(card, "sh:ob:p:")[0])
    assert "уже есть текст — не трогаю" in out["answer"] or "уже есть текст — не трогаю" in (out.get("reply") or {}).get("text", "")
    assert rv.tg.fields[(rv.owner_acc, ANNA)] == OWN_TEXT
    assert await rv.conn.fetchval("SELECT status FROM outbox_drafts WHERE id = $1", draft["id"]) == "pending"
    for table in ("setup_audit", "jobs", "outbox_drafts", "rehearsal_items", "history_events"):
        exists = await rv.conn.fetchval("SELECT to_regclass($1) IS NOT NULL", table)
        if exists:
            assert await rv.conn.fetchval(f"SELECT count(*) FROM {table} t WHERE t::text LIKE '%незаконченный%'") == 0
    assert "незаконченный" not in caplog.text


async def test_same_text_already_in_the_field_is_fine(rv):
    anna, item_id, card, draft = await _card(rv)
    rv.tg.fields[(rv.owner_acc, ANNA)] = "Да, в четверг в десять."
    out = await press(rv.conn, _data(card, "sh:ob:p:")[0])
    assert "уже лежит" in out["answer"]
    assert await rv.conn.fetchval("SELECT status FROM outbox_drafts WHERE id = $1", draft["id"]) == "placed"


async def test_put_needs_the_owners_press(rv):
    """Мутационный якорь: положить текст может только нажатие владельца — не фоновая работа, не команда."""
    anna, item_id, card, draft = await _card(rv)
    from shturman.outbox import policy
    tgt = await policy.target(rv.conn, anna)
    with pytest.raises(put.Refused):
        await put.put(rv.state, tgt, "Текст", reply_to_tg_id=None)            # фоновая работа
    with authority.owner_context(OWNER, chat_id=OWNER, action="telegram.command"):
        with pytest.raises(put.Refused):
            await put.put(rv.state, tgt, "Текст", reply_to_tg_id=None)        # команда — не нажатие
    assert rv.tg.put_calls == []
    with authority.miniapp_context(OWNER, chat_id=OWNER):
        assert (await put.put(rv.state, tgt, "Текст", reply_to_tg_id=None)).placed   # приложение — нажатие


async def test_put_is_refused_when_stopped_or_the_session_is_down(rv):
    anna, item_id, card, draft = await _card(rv)
    data = _data(card, "sh:ob:p:")[0]
    await rv.conn.execute("UPDATE assistant_halt SET stopped = true")
    out = await press(rv.conn, data)
    assert "остановлен" in out["answer"] and rv.tg.put_calls == []
    await rv.conn.execute("UPDATE assistant_halt SET stopped = false")
    from shturman.tg.gateway import AccountUnavailable
    rv.tg.put_fail.append(AccountUnavailable("нет сессии"))
    out = await press(rv.conn, data)
    assert "не на связи" in out["answer"]
    assert await rv.conn.fetchval("SELECT status FROM outbox_drafts WHERE id = $1", draft["id"]) == "pending"
    out = await press(rv.conn, data)                   # снова на связи — нажатие проходит
    assert out["answer"].startswith("Положил")


async def test_send_wins_when_business_is_usable_and_agent_drafts_get_no_put(rv):
    import dataclasses
    await rv.conn.execute("INSERT INTO business_connections (id, account_id, can_reply, enabled, via) "
                          "VALUES ('bc-owner', $1, true, true, 'service')", rv.owner_acc)
    rv.state.config = dataclasses.replace(rv.state.config, sending=True)
    rv.state.extras["outbox"].state.config = rv.state.config
    anna, item_id, card, draft = await _card(rv)
    assert _data(card, "sh:ob:s:") and not _data(card, "sh:ob:p:")
    # черновик ассистента: «В чат» нет, и нажатие с его данными ничего не кладёт (отложенное решение)
    await drafts.finish(rv.conn, draft, None, "rejected", tell_owner=False)
    agent = await drafts.create(rv.conn, rv.state, chat_id=anna, text="Буду в десять.")
    row = await rv.conn.fetchrow("SELECT * FROM outbox_drafts WHERE id = $1", agent["draft_id"])
    out = await press(rv.conn, f"sh:ob:p:{row['id']}:{row['nonce']}")
    assert out["answer"] == "Кнопка недоступна." and rv.tg.put_calls == []


async def test_stop_between_the_press_and_the_write_puts_nothing(rv):
    """«Остановить всё» проверяется ещё раз прямо перед записью в Telegram (`put.put`, `before_write`)."""
    anna, item_id, card, draft = await _card(rv)
    data = _data(card, "sh:ob:p:")[0]
    original = rv.tg.put_draft

    async def stop_then_put(*args, **kwargs):
        await rv.conn.execute("UPDATE assistant_halt SET stopped = true")      # владелец нажал «Остановить всё»
        return await original(*args, **kwargs)
    rv.tg.put_draft = stop_then_put
    out = await press(rv.conn, data)
    assert "остановлен" in out["answer"]
    assert (rv.owner_acc, ANNA) not in rv.tg.fields
    assert await rv.conn.fetchval("SELECT status FROM outbox_drafts WHERE id = $1", draft["id"]) == "pending"


async def test_unknown_outcome_keeps_the_mark_so_a_second_press_does_not_write_again(rv):
    from shturman.tg.gateway import PutUnknown
    anna, item_id, card, draft = await _card(rv)
    data = _data(card, "sh:ob:p:")[0]
    rv.tg.put_fail.append(PutUnknown("ConnectionError"))
    out = await press(rv.conn, data)
    assert "ещё может появиться" in out["answer"] or "ещё может появиться" in out["reply"]["text"]
    assert await rv.conn.fetchval("SELECT put_started_at IS NOT NULL FROM outbox_drafts WHERE id = $1", draft["id"])
    calls = len(rv.tg.put_calls)
    again = await press(rv.conn, data)
    assert again["answer"].startswith("Уже кладу") and len(rv.tg.put_calls) == calls
    # через минуту — снова можно; текст на деле лёг — «уже лежит», черновик закрыт
    await rv.conn.execute("UPDATE outbox_drafts SET put_started_at = now() - interval '2 minutes' WHERE id = $1",
                          draft["id"])
    rv.tg.fields[(rv.owner_acc, ANNA)] = "Да, в четверг в десять."
    assert "уже лежит" in (await press(rv.conn, data))["answer"]
    assert await rv.conn.fetchval("SELECT status FROM outbox_drafts WHERE id = $1", draft["id"]) == "placed"


async def test_hourly_cap_is_exactly_sixty_and_excluded_chat_gets_no_put(rv):
    anna, item_id, card, draft = await _card(rv)
    data = _data(card, "sh:ob:p:")[0]
    await rv.conn.execute(
        "INSERT INTO dictations (chat_id, source, nonce, wait_until, put_started_at) "
        "SELECT $1, 'notify', 'n' || g, now(), now() - interval '10 minutes' FROM generate_series(1, $2) g",
        anna, put.HOURLY_CAP)
    assert "не чаще" in (await press(rv.conn, data))["answer"]          # 61-го нет
    await rv.conn.execute("DELETE FROM dictations")
    from shturman.outbox import policy
    from shturman.replies import delivery
    await rv.conn.execute("UPDATE chats SET excluded = true WHERE id = $1", anna)
    tgt = await policy.target(rv.conn, anna)
    assert delivery.why_no_put(await delivery.facts(rv.conn, rv.state, tgt, text="Да.")) == "not_private"


async def test_real_client_path_keeps_the_owners_text_out_of_everything(rv, caplog, monkeypatch):
    """То же, что выше, но через настоящий `TgManager.put_draft` и клиент Telethon (`GuardedClient`) с
    подменённой сетью: текст владельца в поле ввода не перезаписан, запись не ушла, а сам текст не попал ни в
    базу (в том числе журнал действий и задания), ни в журнал сервиса."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from telethon import TelegramClient
    from telethon.sessions import MemorySession
    from telethon.tl import functions, types

    from shturman.tg import sync
    from shturman.tg.client import GuardedClient, RequestPolicy
    from shturman.tg.manager import TgManager
    anna, item_id, card, draft = await _card(rv)
    requests = []

    async def network(self, sender, request, ordered=False, flood_sleep_threshold=None):
        requests.append(type(request).__name__)
        if isinstance(request, functions.messages.GetPeerDialogsRequest):
            dialog = types.Dialog(peer=types.PeerUser(ANNA), top_message=1, read_inbox_max_id=0,
                                  read_outbox_max_id=0, unread_count=0, unread_mentions_count=0,
                                  unread_reactions_count=0, unread_poll_votes_count=0,
                                  notify_settings=types.PeerNotifySettings(),
                                  draft=types.DraftMessage(date=None, message=OWN_TEXT))
            return types.messages.PeerDialogs(dialogs=[dialog], messages=[], chats=[], users=[],
                                              state=types.updates.State(1, 1, None, 1, 1))
        raise AssertionError(type(request).__name__)
    monkeypatch.setattr(TelegramClient, "_call", network)
    client = GuardedClient(MemorySession(), 12345, "0123456789abcdef", policy=RequestPolicy("owner"))
    client.is_connected = lambda: True
    client.get_input_entity = AsyncMock(return_value=types.InputPeerUser(ANNA, 1))
    mgr = TgManager(SimpleNamespace(sending=False), None, SimpleNamespace(subscribe=lambda *a: None))
    mgr._role_of = AsyncMock(return_value="owner")
    mgr._running = lambda _: SimpleNamespace(client=client, pacer=sync.Pacer(3.0, jitter=True), stop=None)
    mgr.can_put = lambda _: True
    rv.state.extras["tg"] = mgr
    with caplog.at_level(logging.DEBUG):
        out = await press(rv.conn, _data(card, "sh:ob:p:")[0])
    assert "уже есть текст — не трогаю" in out["answer"]
    assert requests == ["GetPeerDialogsRequest"]                   # записи не было
    assert await rv.conn.fetchval("SELECT status FROM outbox_drafts WHERE id = $1", draft["id"]) == "pending"
    tables = [r["table_name"] for r in await rv.conn.fetch(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")]
    for table in tables:
        assert await rv.conn.fetchval(f'SELECT count(*) FROM "{table}" t WHERE t::text LIKE $1',
                                      "%незаконченный%") == 0, table
    assert "незаконченный" not in caplog.text
    assert await rv.conn.fetchval("SELECT detail FROM setup_audit WHERE action = 'draft.put'") == \
        f"черновик № {draft['id']}; итог: busy"
