"""Свой вариант владельца после «Почти — поправил бы» (Б14, Р-89): бот просит написать, как ответили
бы вы; текст хранится у строки репетиции 30 дней, виден в приложении и в статистике — числом."""

import json

from rehearsal_helpers import ANNA, OWNER, answer, cards, chat, enable, item, live, message, model_jobs, press, rv  # noqa: F401

from shturman.rehearsal import core


async def shown(rv) -> dict:
    await enable(rv.conn)
    anna = await chat(rv.conn, rv.owner_acc)
    mid = await message(rv.conn, anna, 10, "Завтра в десять удобно?")
    _, item_id = await live(rv, rv.owner_acc, anna, mid)
    (job,) = await model_jobs(rv.conn)
    await answer(rv.conn, job["id"], {"outcome": "reply", "text": "Да, в десять удобно."})
    (card,) = await cards(rv.conn)
    return {"item_id": item_id, "buttons": {b["text"]: b["data"] for row in card["buttons"] for b in row}}


async def test_almost_asks_for_owner_variant_and_it_is_kept_without_text_in_logs(rv):
    s = await shown(rv)
    out = await press(rv.conn, s["buttons"]["Почти — поправил бы"])
    prompt = out["prompt"]
    assert prompt["kind"] == "variant" and prompt["id"] == s["item_id"] and "как ответили бы вы" in prompt["text"]
    assert [row[0]["text"] for row in out["buttons"]][-1] == "Не писать свой вариант"
    row = await item(rv.conn, s["item_id"])
    saved = await core.save_variant(rv.conn, s["item_id"], prompt["nonce"], "  Да, давайте в десять у вас.  ")
    assert saved["ok"] and "приложении" in saved["text"]
    row = await item(rv.conn, s["item_id"])
    assert row["owner_variant"] == "Да, давайте в десять у вас." and row["rating"] == "almost"
    stats = await core.stats(rv.conn)
    assert stats["variants"] == 1 and stats["almost"] == 1
    # в журнале — номер и число знаков, без текста; во внутреннем API — только числа
    log = await rv.conn.fetchval("SELECT detail FROM setup_audit WHERE action = 'rehearsal.variant'")
    assert log == f"карточка № {s['item_id']}; знаков: 27"
    api = (await rv.client.get("/api/rehearsal")).text
    assert "давайте" not in api
    # чужая метка и пустой текст — не записываются
    assert not (await core.save_variant(rv.conn, s["item_id"], "wrong", "x"))["ok"]
    assert not (await core.save_variant(rv.conn, s["item_id"], prompt["nonce"], "   "))["ok"]
    assert not (await core.save_variant(rv.conn, s["item_id"], prompt["nonce"], "а" * 2001))["ok"]


async def test_skip_button_drops_the_prompt_and_keeps_the_rating(rv):
    s = await shown(rv)
    out = await press(rv.conn, s["buttons"]["Почти — поправил бы"])
    await rv.conn.execute("INSERT INTO executor_state (key, value) VALUES ('prompt', $1::jsonb)",
                          json.dumps({**out["prompt"], "message_id": 5}))
    skip = next(b["data"] for row in out["buttons"] for b in row if b["text"] == "Не писать свой вариант")
    done = await press(rv.conn, skip)
    assert done["answer"] == "Хорошо, без своего варианта."
    assert [row[0]["text"] for row in done["buttons"]] == ["Годится", "✓ Почти — поправил бы", "Не годится"]
    assert await rv.conn.fetchval("SELECT count(*) FROM executor_state WHERE key = 'prompt'") == 0
    assert (await item(rv.conn, s["item_id"]))["rating"] == "almost"


async def test_variant_goes_away_with_the_row_after_30_days(rv):
    s = await shown(rv)
    out = await press(rv.conn, s["buttons"]["Почти — поправил бы"])
    await core.save_variant(rv.conn, s["item_id"], out["prompt"]["nonce"], "Мой вариант")
    await rv.conn.execute("UPDATE rehearsal_items SET created_at = now() - interval '31 days'")
    await rv.mod.sweep()
    assert await rv.conn.fetchval("SELECT count(*) FROM rehearsal_items") == 0
    assert await rv.conn.fetchval("SELECT count(*) FROM rehearsal_items WHERE owner_variant IS NOT NULL") == 0
    assert OWNER and ANNA
