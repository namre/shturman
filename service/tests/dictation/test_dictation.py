"""Ответ голосом, записанный грамотно (Р-103): от нажатия «Ответить голосом» до карточки «Записано».

Главное:
  * голос принимается только от привязанного владельца в его личном чате с ботом — чужое голосовое не
    скачивается и не расшифровывается (мутационный якорь `dictation_voice_owner_only`);
  * собеседнику не уходит ничего: ни черновика, ни задания отправки, ни сообщения через бизнес-режим;
  * без расшифровки голосовых — понятный отказ, голос не скачивается; набросок текстом работает;
  * ожидание истекает; «Остановить всё» снимает ожидание и не даёт записать;
  * число или имя, которых не было в сказанном, — «проверьте»;
  * модель получает сказанное, образцы стиля владельца и справочник имён — и ничего лишнего.
Telegram подставной (`exec_fakes.FakeTelegram`), контейнер распознавания — подставной объект, модель —
прямой работой с очередью заданий.
"""

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "executor"))

from exec_fakes import OWNER, OWNER_USER, STRANGER, STRANGER_USER, bind, private, rig, until  # noqa: E402,F401

from shturman import authority, bridge, store  # noqa: E402
from shturman.dictation import core as dictation  # noqa: E402
from shturman.executor import prompts  # noqa: E402
from shturman.records import ChatRecord, MessageRecord  # noqa: E402
from shturman.voice.asr import AsrUnavailable, Transcript  # noqa: E402

ANNA = 2001


class FakeAsr:
    """Подставной контейнер распознавания: помнит, сколько раз его звали и с какими байтами."""

    def __init__(self, text: str = "ну короче в четверг ок но договор после юриста") -> None:
        self.text, self.calls, self.fail = text, [], None

    async def transcribe(self, audio: bytes) -> Transcript:
        self.calls.append(audio)
        if self.fail is not None:
            raise self.fail
        return Transcript(text=self.text, seconds=12.0)


def asr_on(rig, text: str | None = None) -> FakeAsr:
    asr = FakeAsr(text) if text is not None else FakeAsr()
    rig.state.extras["voice"] = SimpleNamespace(asr=asr)
    return asr


async def message(conn, chat_id, tg_id, text, *, outgoing=False, age=0.0) -> int:
    at = datetime.now(timezone.utc) - timedelta(seconds=age)
    record = MessageRecord(
        tg_message_id=tg_id, sent_at=at, kind="message", sender_class="user",
        sender_tg_id=OWNER if outgoing else ANNA, sender_name="Я" if outgoing else "Анна Ким", text=text,
        entities=None, reply_to_tg_id=None, forwarded_from=None, edited_at=None, media_type=None, media_path=None,
        service_action=None, telegram_entities=None, is_forwarded=False, telegram_via_bot=False,
        telegram_sender_bot=False)
    result = await store.upsert_messages(conn, [(chat_id, record, outgoing)], source="session", owner_tg_id=OWNER)
    return (result.new_ids or result.known_ids)[0]


@pytest_asyncio.fixture
async def dv(rig):
    """Бот с привязанным владельцем; чат с Анной в архиве; уведомление о её сообщении с меткой."""
    await bind(rig)
    dictation.set_state(rig.state)
    account = await store.ensure_account(rig.conn, OWNER, "Владелец", "owner")
    chat_id, _ = await store.ensure_chat(rig.conn, account, ChatRecord("user", ANNA, "personal_chat", "Анна Ким",
                                                                        username="anna_kim"))
    await message(rig.conn, chat_id, 1, "Привет, Анна! Смету пришлю завтра.", outgoing=True, age=3600)
    await message(rig.conn, chat_id, 2, "Ок, спасибо!", outgoing=True, age=1800)
    mid = await message(rig.conn, chat_id, 3, "Когда подпишем договор?", age=60)
    item = await rig.conn.fetchval(
        """INSERT INTO notify_items (message_id, chat_id, kind, due_at, state, nonce)
           VALUES ($1, $2, 'personal', now(), 'sent', 'nonce-anna-1') RETURNING id""", mid, chat_id)
    yield SimpleNamespace(**vars(rig), chat=chat_id, mid=mid, item=item,
                          data=f"sh:vr:a:n:{item}:nonce-anna-1")
    dictation.set_state(None)


def force_prompts(tg) -> list[dict]:
    return [p for p in tg.sent() if (p.get("reply_markup") or {}).get("force_reply")]


async def press(dv, data: str, *, user: dict = OWNER_USER, message_id: int = 501) -> None:
    dv.tg.press(data, user=user, message_id=message_id)
    await dv.bot.poll_once()


async def model_jobs(conn) -> list[dict]:
    rows = await conn.fetch("SELECT * FROM jobs WHERE kind = $1 AND handler = $2 ORDER BY id",
                            bridge.LLM_STRUCTURED, dictation.HANDLER)
    return [{**dict(r), "payload": json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"],
             "context": json.loads(r["context"]) if isinstance(r["context"], str) else r["context"]} for r in rows]


async def answer(conn, job_id: int, parsed) -> None:
    await conn.execute("UPDATE jobs SET status = 'running' WHERE id = $1", job_id)
    assert await bridge.deliver_result(conn, job_id, {"parsed": parsed, "text": json.dumps(parsed), "model": "m"})


async def cards(conn) -> list[dict]:
    rows = await conn.fetch("SELECT payload FROM jobs WHERE kind = $1 AND handler = $2 ORDER BY id",
                            bridge.NOTIFY_OWNER, dictation.CARD_HANDLER)
    return [r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"]) for r in rows]


async def row(conn) -> dict:
    found = await conn.fetchrow("SELECT * FROM dictations ORDER BY id DESC LIMIT 1")
    return dict(found) if found is not None else {}


async def nothing_sent(dv) -> None:
    """Собеседнику не уходит ничего: ни черновика, ни задания отправки, ни сообщения бизнес-режима."""
    conn = dv.conn
    assert await conn.fetchval("SELECT count(*) FROM outbox_drafts") == 0
    assert await conn.fetchval("SELECT count(*) FROM jobs WHERE kind = $1", bridge.BUSINESS_SEND) == 0
    assert dv.tg.sent(business=True) == []
    assert all(p["chat_id"] == OWNER for p in dv.tg.calls("sendMessage"))


async def say_voice(dv, file_id="VOICE1", *, reply_to=None, user=OWNER_USER, audio=b"OggS-voice", **kw) -> None:
    dv.tg.files[file_id] = audio
    dv.tg.voice(file_id, reply_to=reply_to, user=user, **kw)
    await dv.bot.poll_once()


# --- весь путь ---------------------------------------------------------------------------------------

async def test_voice_reply_from_press_to_card_and_nothing_is_sent(dv):
    asr = asr_on(dv)
    await press(dv, dv.data)
    [ask] = force_prompts(dv.tg)
    assert ask["text"].startswith("Запишите голосовое в ответ на это сообщение (до 10 минут).")
    assert (await row(dv.conn))["state"] == "waiting"
    await say_voice(dv, reply_to=dv.tg.last_message_id())
    await until(lambda: model_jobs(dv.conn))
    assert asr.calls == [b"OggS-voice"]                                # скачано своим ботом и расшифровано
    assert [p["path"] for name, p in dv.tg.requests if name == "file"] == ["voice/VOICE1.oga"]
    [job] = await model_jobs(dv.conn)
    sent = job["payload"]["input"]
    assert "в четверг ок но договор после юриста" in sent              # сказанное
    assert "Смету пришлю завтра" in sent and "Ок, спасибо!" in sent    # образцы стиля — свои сообщения владельца
    assert "Когда подпишем договор?" not in sent                       # чужое сообщение модели не нужно
    assert "Анна Ким" in sent                                          # справочник написания
    rules = job["payload"]["instructions"]
    assert "Ничего не добавляй от себя" in rules and "«постараюсь» не превращай в «сделаю»" in rules
    assert (await row(dv.conn))["state"] == "preparing"

    await answer(dv.conn, job["id"], {"text": "В четверг — ок. Договор — после юриста.", "unsure": []})
    [card] = await cards(dv.conn)
    text = card["text"]
    assert text.startswith("🎙 Ваш ответ записан — ничего не отправлено")
    assert "Кому: Анна Ким" in text and "Ответ на: «Когда подпишем договор?»" in text
    assert "Вы сказали" in text and "в четверг ок но договор после юриста" in text
    assert "Записано" in text and "В четверг — ок. Договор — после юриста." in text
    assert "Проверьте" not in text
    kinds = {e["type"] for e in card["entities"]}
    assert kinds == {"expandable_blockquote", "pre"}
    pre = next(e for e in card["entities"] if e["type"] == "pre")
    utf16 = text.encode("utf-16-le")
    assert utf16[pre["offset"] * 2:(pre["offset"] + pre["length"]) * 2].decode("utf-16-le") == \
        "В четверг — ок. Договор — после юриста."
    labels = [[b["text"] for b in r] for r in card["buttons"]]
    assert labels == [["Скопировать"], ["Поправить", "Наговорить заново"], ["Открыть чат"]]
    assert card["buttons"][0][0]["copy"] == "В четверг — ок. Договор — после юриста."
    assert card["buttons"][2][0]["url"] == "https://t.me/anna_kim"
    assert (await row(dv.conn))["state"] == "shown"
    # ответ модели прочитан — его копии в задании не остаётся
    assert await dv.conn.fetchval("SELECT payload::text FROM jobs WHERE id = $1", job["id"]) == "{}"
    # исполнитель передаёт оформление и кнопки Telegram как есть
    await dv.worker.run_once("bot")
    sent_card = dv.tg.calls("sendMessage")[-1]
    assert sent_card["entities"] == card["entities"] and "parse_mode" not in sent_card
    keyboard = sent_card["reply_markup"]["inline_keyboard"]
    assert keyboard[0][0] == {"text": "Скопировать", "copy_text": {"text": "В четверг — ок. Договор — после юриста."}}
    assert keyboard[2][0] == {"text": "Открыть чат", "url": "https://t.me/anna_kim"}
    await nothing_sent(dv)


async def test_voice_is_accepted_only_from_the_owner(dv):
    """Мутационный якорь: голос принимается только от владельца. Чужое голосовое — даже с верной
    просьбой в руках — не скачивается и не расшифровывается."""
    asr = asr_on(dv)
    await press(dv, dv.data)
    async with dv.state.pool.acquire() as conn:
        prompt = await prompts.take(conn, reply_to=None, kinds=dictation.PROMPT_KINDS)
    fetched = []

    async def fetch(file_id, max_bytes):
        fetched.append(file_id)
        return b"voice"

    out = await dictation.accept_voice(dv.state, prompt, {"file_id": "V", "duration": 3},
                                       user_id=STRANGER, chat_id=STRANGER, fetch=fetch)
    assert out is None and fetched == [] and asr.calls == []
    assert (await row(dv.conn))["state"] == "waiting" and await model_jobs(dv.conn) == []
    # и через бота: посторонний в своём чате с ботом — бот молчит, файл не запрашивается
    await dictation._rearm(dv.conn, prompt)
    before = len(dv.tg.calls("sendMessage"))
    await say_voice(dv, "V2", user=STRANGER_USER)
    assert dv.tg.calls("getFile") == [] and asr.calls == [] and len(dv.tg.calls("sendMessage")) == before
    # владелец в группе — тоже нет
    await say_voice(dv, "V3", chat={"id": -100500, "type": "supergroup", "title": "Группа"})
    assert dv.tg.calls("getFile") == [] and asr.calls == []


async def test_voice_without_a_waiting_prompt_is_not_downloaded(dv):
    asr = asr_on(dv)
    await say_voice(dv, "LOOSE")
    assert dv.tg.calls("getFile") == [] and asr.calls == []
    assert dv.tg.calls("sendMessage")[-1]["text"] == dictation.NOT_WAITING


async def test_press_only_from_the_bot_and_only_by_the_owner(dv):
    await press(dv, dv.data, user=STRANGER_USER)
    assert await row(dv.conn) == {} and force_prompts(dv.tg) == []
    # из приложения (Mini App) голос не записать: кнопка не нажимается
    with authority.miniapp_context(OWNER, chat_id=OWNER):
        out = await bridge.dispatch_callback(dv.conn, dv.data, OWNER)
    assert "только в боте" in out["answer"] and await row(dv.conn) == {}
    # чужая метка карточки — устаревшая кнопка
    await press(dv, f"sh:vr:a:n:{dv.item}:nonce-wrong")
    assert await row(dv.conn) == {}


# --- без расшифровки, истёкшее ожидание, остановка, предел --------------------------------------------------

async def test_without_asr_voice_is_refused_plainly_and_text_sketch_still_works(dv):
    dv.state.extras.pop("voice", None)
    await press(dv, dv.data)
    [ask] = force_prompts(dv.tg)
    assert "расшифровка голосовых выключена" in ask["text"] and "./ops/asr.sh on" in ask["text"]
    prompt_id = dv.tg.last_message_id()
    await say_voice(dv, reply_to=prompt_id)
    assert dv.tg.calls("getFile") == []                    # голос даже не скачивается
    assert dv.tg.calls("sendMessage")[-1]["text"] == dictation.NO_ASR
    # набросок текстом — тот же путь
    dv.tg.push(message={"message_id": 901, "date": int(time.time()), "from": OWNER_USER, "chat": private(OWNER_USER),
                        "text": "ответь, что в четверг ок, но договор после юриста",
                        "reply_to_message": {"message_id": prompt_id}})
    await dv.bot.poll_once()
    [job] = await model_jobs(dv.conn)
    assert "договор после юриста" in job["payload"]["input"]
    assert (await row(dv.conn))["input_kind"] == "text"
    await answer(dv.conn, job["id"], {"text": "В четверг — ок, договор — после юриста.", "unsure": []})
    [card] = await cards(dv.conn)
    assert "Ваш набросок" in card["text"]
    await nothing_sent(dv)


async def test_waiting_expires(dv, monkeypatch):
    asr = asr_on(dv)
    await press(dv, dv.data)
    prompt_id = dv.tg.last_message_id()
    monkeypatch.setattr(prompts, "now", lambda: time.time() + 3600)
    monkeypatch.setattr(dictation, "_now_fn", lambda: datetime.now(timezone.utc) + timedelta(hours=1))
    await say_voice(dv, reply_to=prompt_id)
    assert dv.tg.calls("sendMessage")[-1]["text"] == dictation.EXPIRED
    assert dv.tg.calls("getFile") == [] and asr.calls == []
    assert (await row(dv.conn))["state"] == "expired"


async def test_sweep_expires_waiting_and_forgets_old_rows(dv, monkeypatch):
    await press(dv, dv.data)
    await dv.conn.execute("UPDATE dictations SET wait_until = now() - interval '1 minute'")
    assert (await dictation.sweep(dv.conn))["expired"] == 1
    await dv.conn.execute("UPDATE dictations SET created_at = now() - interval '25 hours', transcript = 'сказано'")
    assert (await dictation.sweep(dv.conn))["removed"] == 1
    assert await dv.conn.fetchval("SELECT count(*) FROM dictations") == 0


async def test_stop_everything_cancels_waiting_and_preparing(dv):
    from shturman import halt
    asr_on(dv)
    await press(dv, dv.data)
    await say_voice(dv, reply_to=dv.tg.last_message_id())
    await until(lambda: model_jobs(dv.conn))
    [job] = await model_jobs(dv.conn)
    await halt.stop(dv.state, via="bot")
    assert (await row(dv.conn))["state"] == "stopped"
    await answer(dv.conn, job["id"], {"text": "В четверг — ок.", "unsure": []})
    assert await cards(dv.conn) == []                    # записи нет: ассистент остановлен
    # и новое нажатие при остановке — понятный отказ, без ожидания
    await press(dv, dv.data)
    assert dictation.STOPPED in dv.tg.calls("sendMessage")[-1]["text"]
    assert await dv.conn.fetchval("SELECT count(*) FROM dictations WHERE state = 'waiting'") == 0


async def test_daily_cap(dv):
    await dv.conn.execute(
        """INSERT INTO dictations (chat_id, source, nonce, state, wait_until, asks)
           VALUES ($1, 'notify', 'n', 'shown', now(), $2)""", dv.chat, dictation.DAILY_CAP)
    await press(dv, dv.data)
    assert "предел" in dv.tg.calls("sendMessage")[-1]["text"]
    assert force_prompts(dv.tg) == []


async def test_asr_problems_keep_the_prompt_waiting(dv):
    asr = asr_on(dv)
    asr.fail = AsrUnavailable("down")
    await press(dv, dv.data)
    await say_voice(dv, reply_to=dv.tg.last_message_id())
    await until(lambda: "не отвечает" in dv.tg.calls("sendMessage")[-1]["text"])
    assert (await row(dv.conn))["state"] == "waiting"
    asr.fail = None
    await say_voice(dv, "VOICE2")                         # следующим сообщением, без ответа — просьба ещё ждёт
    await until(lambda: model_jobs(dv.conn))
    # слишком длинное голосовое не скачивается вовсе
    await press(dv, dv.data)
    await say_voice(dv, "LONG", duration=dictation.MAX_SECONDS + 1, reply_to=dv.tg.last_message_id())
    assert "длиннее" in dv.tg.calls("sendMessage")[-1]["text"]
    assert [p["file_id"] for p in dv.tg.calls("getFile")] == ["VOICE1", "VOICE2"]


# --- проверка итога, поправка, заново -------------------------------------------------------------------

async def test_model_added_number_and_name_are_flagged(dv):
    asr_on(dv, "да буду в четверг после обеда")
    await press(dv, dv.data)
    await say_voice(dv, reply_to=dv.tg.last_message_id())
    await until(lambda: model_jobs(dv.conn))
    [job] = await model_jobs(dv.conn)
    await answer(dv.conn, job["id"], {"text": "Да, буду в пятницу в 16:00. Петров подтвердит.",
                                      "unsure": ["после обеда"]})
    [card] = await cards(dv.conn)
    assert "⚠ Проверьте — этого не было в сказанном: «пятницу», «16:00», «Петров»." in card["text"]
    assert "Модель не уверена: «после обеда»." in card["text"]
    assert (await row(dv.conn))["flags"] == ["пятницу", "16:00", "Петров"]


def test_check_accepts_what_was_said_and_names_what_was_not():
    heard = "привет иван в четверг в пятнадцать тридцать ок сумма двадцать пять тысяч пятнадцатого октября"
    assert dictation.check("Привет, Иван! В четверг в 15:30 — ок. Сумма — 25 000. 15 октября.", heard) == []
    assert dictation.check("Привет! Встреча 16 октября в 12:00, адрес example.com, пишите @boss.", "привет встреча") == \
        ["16", "октября", "12:00", "example.com", "@boss"]
    assert dictation.check("Постараюсь к вечеру.", "постараюсь к вечеру") == []
    assert dictation.spoken_numbers("двести тридцать пять") >= {200, 30, 5, 235}


async def test_fix_and_again(dv):
    asr_on(dv)
    await press(dv, dv.data)
    await say_voice(dv, reply_to=dv.tg.last_message_id())
    await until(lambda: model_jobs(dv.conn))
    [job] = await model_jobs(dv.conn)
    await answer(dv.conn, job["id"], {"text": "В четверг — ок. Договор — после юриста.", "unsure": []})
    [card] = await cards(dv.conn)
    fix = card["buttons"][1][0]["data"]
    await press(dv, fix, message_id=777)
    ask = force_prompts(dv.tg)[-1]
    assert ask["text"].startswith("Что поправить?")
    # поправка текстом — ответом на карточку (сообщение с кнопкой)
    dv.tg.push(message={"message_id": 902, "date": int(time.time()), "from": OWNER_USER, "chat": private(OWNER_USER),
                        "text": "не в четверг, а в пятницу", "reply_to_message": {"message_id": 777}})
    await dv.bot.poll_once()
    jobs_ = await model_jobs(dv.conn)
    assert len(jobs_) == 2
    second = jobs_[-1]["payload"]["input"]
    assert "Прежний вариант записи" in second and "В четверг — ок." in second
    assert "Поправка владельца" in second and "не в четверг, а в пятницу" in second
    await answer(dv.conn, jobs_[-1]["id"], {"text": "В пятницу — ок. Договор — после юриста.", "unsure": []})
    second_card = (await cards(dv.conn))[-1]
    assert "Поправка: «не в четверг, а в пятницу»" in second_card["text"]
    assert "Проверьте" not in second_card["text"]          # «пятницу» есть в поправке
    # «Наговорить заново» — та же строка снова ждёт голосовое, прежняя запись забыта
    await press(dv, card["buttons"][1][1]["data"])
    again = await row(dv.conn)
    assert again["state"] == "waiting" and again["result"] is None and again["transcript"] is None
    await nothing_sent(dv)


async def test_long_voice_card_fits_telegram_and_keeps_the_whole_record(dv):
    long_heard = " ".join(["ну значит по смете по фасаду мы ещё раз всё проверим и пришлём"] * 60)
    asr_on(dv, long_heard)
    await press(dv, dv.data)
    await say_voice(dv, reply_to=dv.tg.last_message_id())
    await until(lambda: model_jobs(dv.conn))
    [job] = await model_jobs(dv.conn)
    record = "По смете по фасаду мы ещё раз всё проверим и пришлём. " * 70
    await answer(dv.conn, job["id"], {"text": record, "unsure": []})
    parts = await cards(dv.conn)
    assert len(parts) >= 2
    for part in parts:
        assert len(part["text"].encode("utf-16-le")) // 2 <= bridge.MESSAGE_LIMIT
    pres = []
    for part in parts:
        raw = part["text"].encode("utf-16-le")
        pres += [raw[e["offset"] * 2:(e["offset"] + e["length"]) * 2].decode("utf-16-le")
                 for e in part["entities"] if e["type"] == "pre"]
    assert " ".join(pres).replace("  ", " ").split() == record.split()        # записанное — целиком
    assert parts[-1]["buttons"] and all(not p.get("buttons") for p in parts[:-1])
    assert all(b["text"] != "Скопировать" for r in parts[-1]["buttons"] for b in r)   # длиннее 256 знаков


# --- «В чат» (Р-105, часть 2) ------------------------------------------------------------------------------

async def _put_card(dv):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outbox"))
    from outbox_helpers import FakeTg
    tg = FakeTg()
    account = await dv.conn.fetchval("SELECT id FROM accounts WHERE role = 'owner'")
    tg.puttable.add(account)
    dv.state.extras["tg"] = tg
    asr_on(dv)
    await press(dv, dv.data)
    await say_voice(dv, reply_to=dv.tg.last_message_id())
    await until(lambda: model_jobs(dv.conn))
    [job] = await model_jobs(dv.conn)
    await answer(dv.conn, job["id"], {"text": "В четверг — ок. Договор — после юриста.", "unsure": []})
    [card] = await cards(dv.conn)
    return tg, account, card


async def test_put_lays_the_record_into_the_input_field_and_sends_nothing(dv):
    tg, account, card = await _put_card(dv)
    labels = [[b["text"] for b in r] for r in card["buttons"]]
    assert labels == [["В чат"], ["Скопировать"], ["Поправить", "Наговорить заново"], ["Открыть чат"]]
    assert "«В чат» положит текст в поле ввода чата" in card["text"]
    record = await row(dv.conn)
    await press(dv, f"sh:vr:p:{record['id']}:{record['nonce']}")
    assert tg.fields[(account, ANNA)] == "В четверг — ок. Договор — после юриста."
    assert tg.put_calls[-1]["reply_to_tg_id"] == 3
    assert (await row(dv.conn))["placed_at"] is not None
    answered = dv.tg.calls("answerCallbackQuery")[-1]
    assert answered["text"].startswith("Положил в поле ввода")
    assert await dv.conn.fetchval("SELECT detail FROM setup_audit WHERE action = 'dictation.put'") == \
        f"запись № {record['id']}; итог: placed"
    # повторное нажатие — без второго обращения к полю
    await press(dv, f"sh:vr:p:{record['id']}:{record['nonce']}")
    assert len(tg.put_calls) == 1
    assert tg.sent == [] and tg.calls == 0
    await nothing_sent(dv)


async def test_put_keeps_the_owners_own_text_and_does_not_store_it(dv, caplog):
    import logging
    tg, account, card = await _put_card(dv)
    tg.fields[(account, ANNA)] = "Свой недописанный черновик"
    record = await row(dv.conn)
    with caplog.at_level(logging.DEBUG):
        await press(dv, f"sh:vr:p:{record['id']}:{record['nonce']}")
    assert tg.fields[(account, ANNA)] == "Свой недописанный черновик"
    assert (await row(dv.conn))["placed_at"] is None
    assert "уже есть текст — не трогаю" in dv.tg.calls("answerCallbackQuery")[-1]["text"]
    for table in ("setup_audit", "jobs", "dictations"):
        assert await dv.conn.fetchval(f"SELECT count(*) FROM {table} t WHERE t::text LIKE '%недописанный%'") == 0
    assert "недописанный" not in caplog.text


async def test_no_put_without_the_main_account_session(dv):
    """Сессии основного аккаунта нет — «В чат» под «Записано» не появляется, остаётся «Скопировать»."""
    dv.state.extras.pop("tg", None)
    asr_on(dv)
    await press(dv, dv.data)
    await say_voice(dv, reply_to=dv.tg.last_message_id())
    await until(lambda: model_jobs(dv.conn))
    [job] = await model_jobs(dv.conn)
    await answer(dv.conn, job["id"], {"text": "В четверг — ок.", "unsure": []})
    [card] = await cards(dv.conn)
    assert [[b["text"] for b in r] for r in card["buttons"]][0] == ["Скопировать"]
    assert "Отсюда ничего не уходит" in card["text"]


async def test_disconnect_during_put_answers_unknown_and_the_bot_keeps_working(dv):
    """Аккаунт отключают (пауза, остановка, повторный вход), пока запись «В чат» в пути: Telethon гасит свои
    ожидания — до нажатия это доходит отменой чужого ожидания, а не отменой задачи бота. Владелец получает
    «не удалось узнать», отметка «кладу» остаётся, а опрос бота продолжается."""
    import asyncio
    tg, account, card = await _put_card(dv)
    record = await row(dv.conn)
    loop = asyncio.get_running_loop()
    pending = loop.create_future()

    async def in_flight(*args, **kwargs):
        await pending                                   # как ожидание ответа Telethon на saveDraft
    tg.put_draft = in_flight
    pressing = asyncio.create_task(press(dv, f"sh:vr:p:{record['id']}:{record['nonce']}"))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if pending._callbacks:                          # нажатие дошло до записи
            break
    pending.cancel()                                    # client.disconnect() гасит ожидание извне
    await pressing                                      # опрос не упал отменой
    assert not pressing.cancelled()
    answered = dv.tg.calls("answerCallbackQuery")[-1]["text"]
    assert answered.startswith("Не удалось узнать")
    assert (await row(dv.conn))["put_started_at"] is not None and (await row(dv.conn))["placed_at"] is None
    # бот работает дальше: следующее нажатие разобрано — «уже кладу», второй записи нет
    await press(dv, f"sh:vr:p:{record['id']}:{record['nonce']}")
    assert dv.tg.calls("answerCallbackQuery")[-1]["text"].startswith("Уже кладу")


async def test_poll_survives_a_cancelled_wait_inside_one_update(dv, monkeypatch):
    """Защита на уровне опроса: чужая отмена при разборе одного обновления — как сбой этого обновления."""
    import asyncio
    original = dv.bot.handle_update
    calls = []

    async def once_cancelled(update):
        calls.append(update)
        if len(calls) == 1:
            raise asyncio.CancelledError()
        return await original(update)
    monkeypatch.setattr(dv.bot, "handle_update", once_cancelled)
    dv.tg.text("/status")
    dv.tg.text("/status")
    await dv.bot.poll_once()                            # не бросает
    assert len(calls) == 2 and dv.bot.counters["update_errors"] >= 1
