"""Свой вариант владельца (Б14, Р-89, Р-105). Теперь — «🎙 Наговорить свой» под «Черновиком ответа»: готовая
запись голосом или наброском — оценка «почти». Просьба написать вариант текстом больше не выдаётся; варианты,
о которых бот попросил до обновления, ещё принимаются и хранятся у строки 30 дней, как раньше."""

import json

from rehearsal_helpers import ANNA, OWNER, answer, cards, chat, enable, item, live, message, model_jobs, press, rv  # noqa: F401

from shturman import bridge
from shturman.dictation import core as dictation
from shturman.rehearsal import core


async def shown(rv) -> dict:
    await enable(rv.conn)
    anna = await chat(rv.conn, rv.owner_acc)
    mid = await message(rv.conn, anna, 10, "Завтра в десять удобно?")
    _, item_id = await live(rv, rv.owner_acc, anna, mid)
    (job,) = await model_jobs(rv.conn)
    await answer(rv.conn, job["id"], {"outcome": "reply", "text": "Да, в десять удобно."})
    (card,) = await cards(rv.conn)
    row = await item(rv.conn, item_id)
    return {"item_id": item_id, "chat": anna, "mid": mid, "nonce": row["nonce"],
            "buttons": {b["text"]: b.get("data") for r in card["buttons"] for b in r}}


async def test_dictated_own_variant_rates_the_draft_almost_without_keeping_its_text(rv):
    s = await shown(rv)
    voice = s["buttons"]["🎙 Наговорить свой"]
    assert voice == f"sh:vr:a:r:{s['item_id']}:{s['nonce']}"
    # голосовая запись прошла весь путь (dictation/): готовая карточка «Записано»
    dictation.set_state(rv.state)
    try:
        did = await rv.conn.fetchval(
            """INSERT INTO dictations (chat_id, message_id, source, nonce, state, wait_until, transcript, asks)
               VALUES ($1, $2, 'rehearsal', 'dn', 'preparing', now(), 'давай в десять у тебя', 1) RETURNING id""",
            s["chat"], s["mid"])
        job = await bridge.request_structured(rv.conn, handler=dictation.HANDLER, instructions="i", input="x",
                                              json_schema={"type": "object"}, schema_name="dictation",
                                              context={"id": did})
        await rv.conn.execute("UPDATE dictations SET job_id = $2 WHERE id = $1", did, job)
        await rv.conn.execute("UPDATE jobs SET status = 'running' WHERE id = $1", job)
        assert await bridge.deliver_result(rv.conn, job, {"parsed": {"text": "Давай в десять у тебя.", "unsure": []},
                                                          "text": "{}", "model": "m"})
    finally:
        dictation.set_state(None)
    row = await item(rv.conn, s["item_id"])
    assert (row["rating"], row["rated_via"]) == ("almost", "voice") and row["owner_variant"] is None
    assert "Давай в десять" not in json.dumps({k: str(v) for k, v in row.items()}, ensure_ascii=False)
    stats = await core.stats(rv.conn)
    assert stats["voice"] == 1 and stats["almost"] == 1


async def test_old_almost_button_saves_the_rating_and_asks_nothing(rv):
    s = await shown(rv)
    out = await press(rv.conn, f"sh:rh:a:{s['item_id']}:{s['nonce']}")
    assert "prompt" not in out and "Свой вариант теперь" in out["answer"]
    assert (await item(rv.conn, s["item_id"]))["rating"] == "almost"


async def test_variant_asked_before_the_update_is_still_kept_and_skip_still_works(rv):
    s = await shown(rv)
    await rv.conn.execute("INSERT INTO executor_state (key, value) VALUES ('prompt', $1::jsonb)",
                          json.dumps({"kind": "variant", "id": s["item_id"], "nonce": s["nonce"], "message_id": 5}))
    done = await press(rv.conn, f"sh:rh:k:{s['item_id']}:{s['nonce']}")
    assert done["answer"] == "Хорошо, без своего варианта."
    assert await rv.conn.fetchval("SELECT count(*) FROM executor_state WHERE key = 'prompt'") == 0
    saved = await core.save_variant(rv.conn, s["item_id"], s["nonce"], "  Да, давайте в десять у вас.  ")
    assert saved["ok"]
    assert (await item(rv.conn, s["item_id"]))["owner_variant"] == "Да, давайте в десять у вас."
    log = await rv.conn.fetchval("SELECT detail FROM setup_audit WHERE action = 'rehearsal.variant'")
    assert log == f"карточка № {s['item_id']}; знаков: 27"
    assert "давайте" not in (await rv.client.get("/api/rehearsal")).text
    assert not (await core.save_variant(rv.conn, s["item_id"], "wrong", "x"))["ok"]
    assert not (await core.save_variant(rv.conn, s["item_id"], s["nonce"], "а" * 2001))["ok"]


async def test_variant_goes_away_with_the_row_after_30_days(rv):
    s = await shown(rv)
    await core.save_variant(rv.conn, s["item_id"], s["nonce"], "Мой вариант")
    await rv.conn.execute("UPDATE rehearsal_items SET created_at = now() - interval '31 days'")
    await rv.mod.sweep()
    assert await rv.conn.fetchval("SELECT count(*) FROM rehearsal_items") == 0
    assert OWNER and ANNA
