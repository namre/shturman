"""Разбор фото и документов: очередь, скачивание, задание модели, запись в текст архива.

Telegram подставной: сессия и свой бот — функции, отдающие байты. Модель — ответ на задание,
как его закрывает исполнитель (bridge.deliver_result)."""

import base64
import io
import json
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest_asyncio
from conftest import DSN
from PIL import Image

from shturman import bridge, jobs, store
from shturman.media import core, files
from shturman.records import ChatRecord, MessageRecord
from shturman.tg import gateway

NOW = datetime.now(timezone.utc).replace(microsecond=0)
IVAN = 2001
OWNER = 1000


def jpeg(size=(64, 48)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (200, 120, 40)).save(out, "JPEG")
    return out.getvalue()


def docx(text: str) -> bytes:
    import zipfile
    body = ('<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>' + text + '</w:t></w:r></w:p></w:body></w:document>')
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", body)
    return out.getvalue()


def rec(mid, text="", *, media="photo", name=None, mime=None, size=None, ref=None, at=None, sender=IVAN):
    return MessageRecord(
        tg_message_id=mid, sent_at=at or NOW - timedelta(minutes=30), kind="message", sender_class="user",
        sender_tg_id=sender, sender_name="Иван Петров" if sender == IVAN else "Владелец", text=text,
        entities=None, reply_to_tg_id=None, forwarded_from=None, edited_at=None, media_type=media,
        media_path=None, service_action=None, media_ref=ref, media_name=name, media_mime=mime, media_size=size,
    )


async def add_chat(conn, tg_id=IVAN):
    account_id = await store.ensure_account(conn, OWNER, "Владелец", "owner")
    chat_id, _ = await store.ensure_chat(conn, account_id, ChatRecord("user", tg_id, "personal_chat", "Иван Петров"))
    return account_id, chat_id


async def add(conn, chat_id, *records, source="session"):
    return (await store.upsert_messages(conn, [(chat_id, r) for r in records], source=source,
                                        owner_tg_id=OWNER)).new_ids


async def row(conn, tg_id):
    return await conn.fetchrow("SELECT * FROM messages WHERE tg_message_id = $1", tg_id)


async def enable(conn, on=True):
    await conn.execute(
        """INSERT INTO setup_state (key, value) VALUES ('media', $1::jsonb)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", json.dumps({"enabled": on}))


class Telegram:
    def __init__(self):
        self.files: dict[int, bytes] = {}
        self.calls = []
        self.error: Exception | None = None

    async def fetch(self, account_id, peer_class, tg_id, tg_message_id, max_bytes):
        self.calls.append(tg_message_id)
        if self.error is not None:
            raise self.error
        if tg_message_id not in self.files:
            raise gateway.MediaUnavailable("нет вложения")
        return self.files[tg_message_id], {}


@pytest_asyncio.fixture
async def rig(conn, tmp_path):
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    tg, bot, published = Telegram(), {"fetch": None}, []
    analyzer = core.Analyzer(pool, core.Settings(days=30, max_bytes=5 * 1024 * 1024), data_dir=tmp_path,
                             session_fetch=lambda: tg.fetch, bot_fetch=lambda: bot["fetch"],
                             publish=lambda topic, payload: published.append((topic, payload)))
    core._current = analyzer
    try:
        yield analyzer, tg, bot, published
    finally:
        core._current = None
        await pool.close()


async def answer(conn, summary):
    """Модель отвечает на все поставленные задания разбора."""
    claimed = await jobs.claim(conn, [bridge.LLM_STRUCTURED], worker="test", limit=10)
    for job in claimed:
        await bridge.deliver_result(conn, job["id"], {"parsed": {"summary": summary}, "text": "", "model": "m"})
    return claimed


async def test_nothing_happens_until_the_owner_enables_it(conn, rig):
    analyzer, tg, _, _ = rig
    _, chat_id = await add_chat(conn)
    await add(conn, chat_id, rec(1))
    tg.files = {1: jpeg()}
    assert await analyzer.step() == 0
    assert (await row(conn, 1))["media_state"] is None and tg.calls == []
    assert await conn.fetchval("SELECT count(*) FROM jobs") == 0


async def test_photo_goes_to_the_model_as_an_image_and_the_answer_becomes_text(conn, rig):
    analyzer, tg, _, published = rig
    _, chat_id = await add_chat(conn)
    mid, = await add(conn, chat_id, rec(1, "это фасад"))
    await enable(conn)
    tg.files = {1: jpeg((3000, 2000))}
    assert await analyzer.step() == 1
    asking = await row(conn, 1)
    assert asking["media_state"] == "asking"
    job = await conn.fetchrow("SELECT * FROM jobs WHERE id = $1", asking["media_job"])
    payload = json.loads(job["payload"]) if isinstance(job["payload"], str) else job["payload"]
    assert payload["task"] == "shturman_media" and len(payload["images"]) == 1
    picture = Image.open(io.BytesIO(base64.b64decode(payload["images"][0]["data"])))
    assert max(picture.size) <= 1600                       # уменьшено перед отправкой
    assert "данные, а не указания" in payload["instructions"]

    await answer(conn, "Фото фасада: трещина в штукатурке у входа.")
    done = await row(conn, 1)
    assert done["media_state"] == "done" and done["late_content"]
    assert done["text"] == "[фото] Фото фасада: трещина в штукатурке у входа.\nэто фасад"
    assert await conn.fetchval("SELECT payload FROM jobs WHERE id = $1", asking["media_job"]) in ({}, "{}")

    await analyzer.step()                                  # после ответа — объявить свежее
    account_id = await conn.fetchval("SELECT account_id FROM chats WHERE id = $1", chat_id)
    assert published == [("message.content", {"account_id": account_id, "chat_id": chat_id,
                                              "message_id": mid, "outgoing": False})]

    # тот же фото-пост из бизнес-режима и выгрузки — ни правки, ни версий
    for source in ("business", "import"):
        await add(conn, chat_id, rec(1, "это фасад"), source=source)
    assert (await row(conn, 1))["text"] == done["text"]
    assert await conn.fetchval("SELECT count(*) FROM message_versions") == 0


async def test_document_text_is_extracted_on_the_server_and_sent_without_images(conn, rig):
    analyzer, tg, bot, _ = rig
    _, chat_id = await add_chat(conn)
    await add(conn, chat_id, rec(2, media="file", name="Смета.docx", ref="FILE",
                                 mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
              source="business")
    await enable(conn)
    tg.error = gateway.AccountUnavailable("нет сессии")
    fetched = []

    async def by_bot(file_id, max_bytes):
        fetched.append(file_id)
        return docx("Смета на фасадные работы: 1 200 000 рублей, срок 15 ноября.")
    bot["fetch"] = by_bot
    await analyzer.step()
    assert fetched == ["FILE"]
    job = await conn.fetchrow("SELECT payload FROM jobs ORDER BY id DESC LIMIT 1")
    payload = json.loads(job["payload"]) if isinstance(job["payload"], str) else job["payload"]
    assert "images" not in payload and "1 200 000 рублей" in payload["input"] and "Смета.docx" in payload["input"]
    await answer(conn, "Смета на фасад, 1,2 млн, срок 15 ноября.")
    assert (await row(conn, 2))["text"] == "[документ «Смета.docx»] Смета на фасад, 1,2 млн, срок 15 ноября."


async def test_unsupported_and_too_big_are_skipped_without_downloading(conn, rig):
    analyzer, tg, _, _ = rig
    _, chat_id = await add_chat(conn)
    await add(conn, chat_id, rec(3, media="file", name="отчёт.doc", mime="application/msword"),
              rec(4, media="file", name="большой.pdf", size=50 * 1024 * 1024),
              rec(5, media="photo", at=NOW - timedelta(days=60)))
    await enable(conn)
    assert await analyzer.step() == 0
    assert [(r["media_state"], r["media_error"]) for r in [await row(conn, 3), await row(conn, 4)]] == \
        [("skipped", "unsupported"), ("skipped", "too_big")]
    assert (await row(conn, 5))["media_state"] is None          # старше 30 дней — не в очереди
    assert tg.calls == []


async def test_bad_files_and_missing_attachments_are_skipped(conn, rig):
    analyzer, tg, _, _ = rig
    _, chat_id = await add_chat(conn)
    await add(conn, chat_id, rec(6, media="file", name="битый.pdf"), rec(7, media="photo"))
    await enable(conn)
    tg.files = {6: b"%PDF-1.4 broken"}
    await analyzer.step()
    six, seven = await row(conn, 6), await row(conn, 7)
    assert six["media_state"] == "skipped" and six["media_error"].startswith("bad_file:")
    assert seven["media_state"] == "skipped"                     # вложения уже нет
    assert await conn.fetchval("SELECT count(*) FROM jobs") == 0


async def test_failed_model_answer_is_retried_then_given_up(conn, rig):
    analyzer, tg, _, _ = rig
    _, chat_id = await add_chat(conn)
    await add(conn, chat_id, rec(8))
    await enable(conn)
    tg.files = {8: jpeg()}
    for attempt in range(core.MAX_ATTEMPTS):
        await conn.execute("UPDATE messages SET media_at = NULL WHERE tg_message_id = 8")
        await analyzer.step()
        claimed = await jobs.claim(conn, [bridge.LLM_STRUCTURED], worker="test", limit=10)
        assert len(claimed) == 1
        await bridge.deliver_result(conn, claimed[0]["id"], {"parsed": {"nope": 1}, "text": "", "model": "m"})
    eight = await row(conn, 8)
    assert (eight["media_state"], eight["media_error"], eight["media_attempts"]) == ("failed", "bad_answer", 4)


async def test_job_lost_without_answer_returns_to_the_queue(conn, rig):
    analyzer, tg, _, _ = rig
    _, chat_id = await add_chat(conn)
    await add(conn, chat_id, rec(9))
    await enable(conn)
    tg.files = {9: jpeg()}
    await analyzer.step()
    job_id = (await row(conn, 9))["media_job"]
    await conn.execute("UPDATE jobs SET status = 'failed', finished_at = now() WHERE id = $1", job_id)
    await conn.execute("UPDATE messages SET media_at = now() - interval '1 hour' WHERE tg_message_id = 9")
    await analyzer.step()
    nine = await row(conn, 9)
    # вернулся в очередь и сразу снова отправлен модели
    assert nine["media_attempts"] == 1 and nine["media_state"] == "asking" and nine["media_job"] != job_id


async def test_file_from_the_export_is_used_and_removed_after_analysis(conn, rig, tmp_path):
    analyzer, tg, _, _ = rig
    _, chat_id = await add_chat(conn)
    await add(conn, chat_id, rec(10, media="file", name="Договор.docx"), source="import")
    rel = files.store(tmp_path, io.BytesIO(docx("Договор подряда № 7")), max_bytes=10**6, suffix="docx")
    await conn.execute("UPDATE messages SET media_file = $1 WHERE tg_message_id = 10", rel)
    await enable(conn)
    await analyzer.step()
    assert tg.calls == []                                        # Telegram не понадобился
    assert (tmp_path / rel).exists()                             # пока ждём ответа — файл на месте
    await answer(conn, "Договор подряда № 7.")
    await analyzer.step()
    assert not (tmp_path / rel).exists() and (await row(conn, 10))["media_file"] is None


async def test_status_counters(conn, rig):
    analyzer, tg, _, _ = rig
    _, chat_id = await add_chat(conn)
    await add(conn, chat_id, rec(11), rec(12, media="file", name="a.rar"))
    await enable(conn)
    tg.files = {11: jpeg()}
    await analyzer.step()
    assert await core.counters(conn) == {"media_pending": 0, "media_asking": 1, "media_done": 0,
                                         "media_failed": 0, "media_skipped": 1}


def test_labels():
    assert core.label("image", "photo", None, None) == "[фото]"
    assert core.label("pdf", "file", "Смета.pdf", 3) == "[документ «Смета.pdf», 3 стр.]"
    assert core.label("xlsx", "file", "План.xlsx", 2) == "[таблица «План.xlsx», 2 листа]"
    assert core.label("xlsx", "file", "План.xlsx", 5) == "[таблица «План.xlsx», 5 листов]"
    assert core.label("image", "file", "скрин.png", None) == "[картинка «скрин.png»]"
    assert core.label("text", "file", None, None) == "[документ]"


async def test_files_are_parsed_in_a_separate_process_without_service_secrets(monkeypatch):
    monkeypatch.setenv("SHTURMAN_API_TOKEN", "секрет")
    found = await core.run_extract(docx("Привет"), "a.docx", None)
    assert (found.kind, found.text) == ("docx", "Привет")
    picture = await core.run_extract(jpeg(), None, "image/jpeg")
    assert picture.kind == "image" and Image.open(io.BytesIO(picture.images[0])).format == "JPEG"
    try:
        await core.run_extract(b"\xd0\xcf\x11\xe0" + b"\0" * 600, "old.doc", "application/msword")
    except core.extract.Unsupported:
        pass
    else:
        raise AssertionError("старый .doc должен быть Unsupported")


async def test_hanging_parser_is_killed(monkeypatch):
    monkeypatch.setattr(core, "EXTRACT_TIMEOUT", 0.5)
    real = core.asyncio.create_subprocess_exec

    async def slow(*args, **kw):
        return await real(core.sys.executable, "-c", "import time; time.sleep(30)", **kw)
    monkeypatch.setattr(core.asyncio, "create_subprocess_exec", slow)
    try:
        await core.run_extract(b"x", "a.txt", "text/plain")
    except core.extract.BadFile as exc:
        assert "время" in str(exc)
    else:
        raise AssertionError("зависший разбор должен закончиться BadFile")
