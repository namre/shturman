"""Выгрузка Telegram Desktop архивом zip: поиск result.json, просмотр и импорт из архива, файлы
голосовых, фото и документов на разбор, чтение файла очередью голосовых, уборка файлов."""

import asyncio
import dataclasses
import errno
import io
import json
import os
import stat
import time
import zipfile
from datetime import datetime, timedelta, timezone

import asyncpg
import httpx
import pytest
import pytest_asyncio

import shturman.ingest_api as ingest_api
from shturman import authority, cli, store
from shturman.export_archive import ArchiveError, ExportArchive, media_relpath
from shturman.importer import import_export, scan
from shturman.media import files as media_files
from shturman.media import from_export
from shturman.records import ChatRecord, MessageRecord
from shturman.voice import core
from shturman.voice.asr import AsrClient

from conftest import API_TOKEN, DSN, IVAN, MCP_TOKEN, OWNER, full_export, msg

MODULES = ("shturman.api_core", "shturman.ingest_api")
NOW = int(time.time())
DAY = 86400
BASE = "ChatExport_2026-10-01"
OGG = b"OggS" + b"\x01voice" * 50


def payload(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def make_zip(entries: dict[str, bytes], method=zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=method) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def media_export():
    """Один личный чат с голосовыми, фото и документом — свежими и старыми, с плохими путями."""
    def voice(mid, ts, path):
        return msg(mid, ts, IVAN, "Иван Петров", "", media_type="voice_message", file=path, duration_seconds=7)
    messages = [
        voice(1, NOW - DAY, "voice_messages/audio_1.ogg"),             # свежее — берётся
        voice(2, NOW - 60 * DAY, "voice_messages/audio_2.ogg"),        # старше asr_days — нет
        voice(3, NOW - DAY, "voice_messages/big.ogg"),                 # больше предела — нет
        voice(4, NOW - DAY, "voice_messages/missing.ogg"),             # нет в архиве — нет
        msg(5, NOW - DAY, IVAN, "Иван Петров", "Фото объекта", photo="photos/photo_1.jpg"),
        msg(6, NOW - DAY, IVAN, "Иван Петров", "Договор", file="files/doc.pdf", file_name="doc.pdf",
            mime_type="application/pdf"),
        voice(7, NOW - DAY, "../outside.ogg"),                         # выход из папки — нет
        voice(8, NOW - DAY, "/etc/passwd"),                            # абсолютный путь — нет
        voice(9, NOW - DAY, "voice_messages/bomb.ogg"),                # «zip-бомба» — нет
        msg(10, NOW - DAY, IVAN, "Иван Петров", "Просто текст"),
        voice(11, NOW - DAY, "voice_messages\\audio_11.ogg"),          # путь с обратной чертой — берётся
    ]
    return full_export([{"name": "Иван Петров", "type": "personal_chat", "id": IVAN, "messages": messages}])


def media_zip(export=None, base=BASE) -> bytes:
    prefix = f"{base}/" if base else ""
    return make_zip({
        f"{prefix}result.json": payload(export or media_export()),
        f"{prefix}voice_messages/audio_1.ogg": OGG,
        f"{prefix}voice_messages/audio_2.ogg": OGG,
        f"{prefix}voice_messages/big.ogg": os.urandom(5000),
        f"{prefix}voice_messages/bomb.ogg": b"\x00" * (3 * 1024 * 1024),
        f"{prefix}voice_messages/audio_11.ogg": OGG + b"11",
        f"{prefix}photos/photo_1.jpg": b"\xff\xd8\xff" + os.urandom(300),
        f"{prefix}files/doc.pdf": b"%PDF-1.4 " + os.urandom(300),
        "outside.ogg": OGG,
    })


async def upload_bytes(client, data: bytes) -> str:
    r = await client.post("/api/imports", content=data)
    assert r.status_code == 201, r.text
    return r.json()["import_id"]


async def run_import(client, import_id, body=None):
    path = f"/api/imports/{import_id}/run"
    with authority.setup_context("test-import-owner-session", action=path):
        return await client.post(path, json=body or {})


async def wait_state(client, import_id, *states, timeout=10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        body = (await client.get(f"/api/imports/{import_id}")).json()
        if body["state"] in states:
            return body
        assert asyncio.get_running_loop().time() < deadline, body
        await asyncio.sleep(0.02)


def media_cfg(config, **kw):
    base = dict(asr=True, asr_days=30, asr_max_bytes=4 * 1024 * 1024, media_days=30, media_max_bytes=4 * 1024 * 1024)
    base.update(kw)
    return dataclasses.replace(config, **base)


async def enable_media(conn):
    await conn.execute("INSERT INTO settings (key, value) VALUES ('media.enabled', '{\"enabled\": true}')")


async def stored(conn):
    return {r["tg_message_id"]: r["media_file"] for r in await conn.fetch(
        "SELECT tg_message_id, media_file FROM messages WHERE media_file IS NOT NULL")}


def russian(text: str) -> bool:
    return any("а" <= ch <= "я" for ch in text)


# --- архив: где result.json, что не принимается ---------------------------------------------------

@pytest.mark.parametrize("base", ["", "ChatExport_2026-10-01", "Telegram Desktop/DataExport_2026-10-01"])
def test_result_json_is_found_at_root_and_nested(tmp_path, base):
    path = tmp_path / "x.zip"
    prefix = f"{base}/" if base else ""
    path.write_bytes(make_zip({
        f"{prefix}result.json": b"{}",
        f"{prefix}chats/chat_01/result.json": b"[]",       # глубже — не берётся
        f"__MACOSX/{prefix}._result.json": b"junk",
    }))
    with ExportArchive(path) as archive:
        assert archive.result.filename == f"{prefix}result.json" and archive.base == base
        with archive.open_result() as fp:
            assert fp.read() == b"{}"


def test_archive_without_result_json_or_broken_is_refused_in_russian(tmp_path):
    path = tmp_path / "a.zip"
    path.write_bytes(make_zip({"photos/1.jpg": b"x", "result.json.bak": b"{}"}))
    with pytest.raises(ArchiveError, match="нет файла result.json"):
        ExportArchive(path)
    path.write_bytes(make_zip({"result.json": b"{}"})[:30])
    with pytest.raises(ArchiveError, match="повреждён"):
        ExportArchive(path)


def _encrypt_flag(data: bytes, name: bytes) -> bytes:
    """Ставит у члена архива признак шифрования — так выглядит архив с паролем."""
    out = bytearray(data)
    for sig, flag_at, name_at in ((b"PK\x03\x04", 6, 30), (b"PK\x01\x02", 8, 46)):
        pos = out.find(sig)
        while pos != -1:
            if out[pos + name_at:pos + name_at + len(name)] == name:
                out[pos + flag_at] |= 0x1
            pos = out.find(sig, pos + 4)
    return bytes(out)


def test_encrypted_and_unsupported_compression_are_refused(tmp_path):
    path = tmp_path / "e.zip"
    path.write_bytes(_encrypt_flag(make_zip({"result.json": b"{}"}), b"result.json"))
    with pytest.raises(ArchiveError, match="паролем"):
        ExportArchive(path)
    path.write_bytes(make_zip({"result.json": b"{}" * 1000}, method=zipfile.ZIP_BZIP2))
    with pytest.raises(ArchiveError, match="способом"):
        ExportArchive(path)
    path.write_bytes(make_zip({"result.json": b" " * (3 * 1024 * 1024)}))
    with pytest.raises(ArchiveError, match="подозрительно"):
        ExportArchive(path)


@pytest.mark.parametrize("raw, expected", [
    ("voice_messages/a.ogg", "voice_messages/a.ogg"),
    ("chats\\chat_01\\photos\\p.jpg", "chats/chat_01/photos/p.jpg"),
    ("./files//doc.pdf", "files/doc.pdf"),
    ("../a.ogg", None), ("files/../../a", None), ("/etc/passwd", None), ("C:/x.ogg", None),
    ("", None), (None, None),
])
def test_media_path_is_normalized_and_escapes_rejected(raw, expected):
    assert media_relpath(raw) == expected


def test_scan_and_import_from_zip_equal_json(sample_export, tmp_path):
    path = tmp_path / "e.zip"
    path.write_bytes(make_zip({f"{BASE}/result.json": payload(sample_export)}))
    with ExportArchive(path) as archive, archive.open_result() as fp:
        from_zip = scan(fp)
    from_json = scan(io.BytesIO(payload(sample_export)))
    assert from_zip == from_json


async def test_import_from_zip_equals_import_from_json(conn, sample_export, tmp_path):
    path = tmp_path / "e.zip"
    path.write_bytes(make_zip({f"{BASE}/result.json": payload(sample_export)}))
    with ExportArchive(path) as archive, archive.open_result() as fp:
        first = await import_export(conn, fp)
    rows = [tuple(r) for r in await conn.fetch("SELECT chat_id, tg_message_id, text FROM messages ORDER BY 1, 2")]
    second = await import_export(conn, io.BytesIO(payload(sample_export)))
    assert first.messages_new == 8 and (second.messages_new, second.messages_known) == (0, 8)
    assert rows == [tuple(r) for r in await conn.fetch("SELECT chat_id, tg_message_id, text FROM messages ORDER BY 1, 2")]


# --- загрузка и импорт через API -------------------------------------------------------------------

async def test_zip_upload_is_detected_by_content_and_scanned(make_client, config, sample_export):
    client, _ = await make_client(*MODULES)
    data = make_zip({f"Telegram Desktop/DataExport_2026/result.json": payload(sample_export),
                     "Telegram Desktop/DataExport_2026/photos/1.jpg": b"jpeg"})
    r = await client.post("/api/imports", content=data, headers={"Content-Type": "application/json"})
    assert r.status_code == 201 and r.json()["kind"] == "zip"
    import_id = r.json()["import_id"]
    path = config.uploads_dir / f"export-{import_id}.zip"
    assert sorted(p.name for p in config.uploads_dir.iterdir()) == [path.name]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    body = (await client.get(f"/api/imports/{import_id}/scan")).json()
    json_id = await upload_bytes(client, payload(sample_export))
    assert (await client.get(f"/api/imports/{json_id}")).json()["kind"] == "json"
    as_json = (await client.get(f"/api/imports/{json_id}/scan")).json()
    assert {k: v for k, v in body.items() if k != "import_id"} == {k: v for k, v in as_json.items() if k != "import_id"}
    status = (await client.get(f"/api/imports/{import_id}")).json()
    # ход просмотра — по байтам result.json внутри архива, а не по размеру архива
    assert status["progress"]["bytes_total"] == len(payload(sample_export))


async def test_zip_without_result_json_fails_with_clear_text_and_is_removed(make_client, config):
    client, _ = await make_client(*MODULES)
    import_id = await upload_bytes(client, make_zip({"DataExport/export_results.html": b"<html>"}))
    r = await client.get(f"/api/imports/{import_id}/scan")
    assert (r.status_code, r.json()["code"]) == (422, "scan_failed")
    assert "result.json" in r.json()["error"] and russian(r.json()["error"])
    assert list(config.uploads_dir.iterdir()) == []


async def test_broken_zip_fails_on_run_and_is_removed(make_client, config, sample_export):
    client, _ = await make_client(*MODULES)
    data = make_zip({"result.json": payload(sample_export)})
    import_id = await upload_bytes(client, data[:len(data) // 2])
    await run_import(client, import_id)
    failed = await wait_state(client, import_id, "failed", "done")
    assert failed["file_kept"] is False and russian(failed["error"]) and "zip" in failed["error"]


async def test_stale_zip_uploads_are_removed_at_startup(make_client, config):
    config.uploads_dir.mkdir(parents=True)
    for name in ("export-a.zip", "export-b.json", "export-c.part"):
        (config.uploads_dir / name).write_bytes(b"x")
    await make_client(*MODULES)
    assert list(config.uploads_dir.iterdir()) == []


async def test_import_takes_voice_files_from_zip_within_window_and_limits(make_client, conn, config):
    cfg = media_cfg(config, asr_max_bytes=4 * 1024 * 1024)
    client, _ = await make_client(*MODULES, cfg=cfg)
    data = media_zip()
    import_id = await upload_bytes(client, data)
    await run_import(client, import_id)
    done = await wait_state(client, import_id, "done", "failed")
    assert done["state"] == "done", done
    st = done["stats"]
    # фото и документ не берутся: разбор вложений выключен
    files = await stored(conn)
    assert sorted(files) == [1, 3, 11]
    assert (st["media_files"], st["media_no_space"]) == (3, False)
    # 4 — нет в архиве, 7 и 8 — негодный путь, 9 — «бомба»; 2 — старое, в кандидаты не попадает
    assert st["media_skipped"] == 4
    for mid, rel in files.items():
        path = cfg.data_dir / rel
        assert rel.startswith("media-files/") and rel.endswith(".ogg")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert (cfg.data_dir / files[1]).read_bytes() == OGG
    assert (cfg.data_dir / files[11]).read_bytes() == OGG + b"11"
    assert len(list(cfg.media_files_dir.iterdir())) == 3            # лишних файлов нет
    assert done["file_kept"] is False and list(cfg.uploads_dir.iterdir()) == []
    assert await conn.fetchval("SELECT source_name FROM imports") == "export.zip"
    stats_row = json.loads(await conn.fetchval("SELECT stats FROM imports"))
    assert stats_row["media_files"] == 3


async def test_oversize_voice_is_not_taken(make_client, conn, config):
    client, _ = await make_client(*MODULES, cfg=media_cfg(config, asr_max_bytes=1000))
    import_id = await upload_bytes(client, media_zip())
    await run_import(client, import_id)
    done = await wait_state(client, import_id, "done", "failed")
    # audio_1/audio_11 (около 350 байт) берутся, big.ogg (5000) — нет
    assert sorted(await stored(conn)) == [1, 11] and done["stats"]["media_files"] == 2


async def test_photo_and_file_are_taken_only_when_media_is_enabled(make_client, conn, config):
    client, _ = await make_client(*MODULES, cfg=media_cfg(config, asr=False))
    import_id = await upload_bytes(client, media_zip())
    await run_import(client, import_id)
    done = await wait_state(client, import_id, "done", "failed")
    assert await stored(conn) == {} and done["stats"]["media_files"] == 0

    await enable_media(conn)
    await conn.execute("DELETE FROM messages")
    import_id = await upload_bytes(client, media_zip())
    await run_import(client, import_id)
    done = await wait_state(client, import_id, "done", "failed")
    files = await stored(conn)
    assert sorted(files) == [5, 6] and done["stats"]["media_files"] == 2
    assert files[5].endswith(".jpg") and files[6].endswith(".pdf")
    row = await conn.fetchrow("SELECT media_name, media_mime FROM messages WHERE tg_message_id = 6")
    assert (row["media_name"], row["media_mime"]) == ("doc.pdf", "application/pdf")


async def test_json_upload_takes_no_files_and_reimport_keeps_existing(make_client, conn, config):
    client, _ = await make_client(*MODULES, cfg=media_cfg(config))
    import_id = await upload_bytes(client, payload(media_export()))
    await run_import(client, import_id)
    done = await wait_state(client, import_id, "done", "failed")
    assert done["kind"] == "json" and done["stats"]["media_files"] == 0 and await stored(conn) == {}
    # теперь архив: голосовые получают файлы; ещё раз тот же архив — новых файлов не появляется
    for expected in (3, 0):
        import_id = await upload_bytes(client, media_zip())
        await run_import(client, import_id)
        done = await wait_state(client, import_id, "done", "failed")
        assert done["stats"]["media_files"] == expected
    assert len(list(config.data_dir.joinpath("media-files").iterdir())) == 3


async def test_voice_skipped_for_lack_of_source_comes_back_with_the_file(make_client, conn, config):
    client, _ = await make_client(*MODULES, cfg=media_cfg(config))
    import_id = await upload_bytes(client, payload(media_export()))
    await run_import(client, import_id)
    await wait_state(client, import_id, "done", "failed")
    await conn.execute("""UPDATE messages SET transcript_state = 'skipped', transcript_error = 'no_source'
                          WHERE tg_message_id = 1""")
    await conn.execute("""UPDATE messages SET transcript_state = 'done', transcript = 'уже'
                          WHERE tg_message_id = 11""")
    import_id = await upload_bytes(client, media_zip())
    await run_import(client, import_id)
    await wait_state(client, import_id, "done", "failed")
    assert sorted(await stored(conn)) == [1, 3]                      # 11 уже расшифровано
    row = await conn.fetchrow("SELECT transcript_state, transcript_error FROM messages WHERE tg_message_id = 1")
    assert (row["transcript_state"], row["transcript_error"]) == ("pending", None)


async def test_no_space_stops_copying_but_keeps_the_import(make_client, conn, config, monkeypatch):
    client, _ = await make_client(*MODULES, cfg=media_cfg(config))
    real, calls = media_files.store, []

    def store_or_full(*args, **kwargs):
        calls.append(1)
        if len(calls) > 1:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(*args, **kwargs)

    monkeypatch.setattr(media_files, "store", store_or_full)
    import_id = await upload_bytes(client, media_zip())
    await run_import(client, import_id)
    done = await wait_state(client, import_id, "done", "failed")
    assert done["state"] == "done" and done["stats"]["messages_new"] == 11
    assert done["stats"]["media_no_space"] is True and done["stats"]["media_files"] == 1
    assert len(calls) == 2 and len(await stored(conn)) == 1


async def test_files_of_excluded_chats_are_not_taken(make_client, conn, config):
    client, _ = await make_client(*MODULES, cfg=media_cfg(config))
    import_id = await upload_bytes(client, media_zip())
    await run_import(client, import_id, {"exclude": [f"user:{IVAN}"]})
    done = await wait_state(client, import_id, "done", "failed")
    assert done["stats"]["media_files"] == 0 and not config.data_dir.joinpath("media-files").exists()


# --- очередь голосовых берёт файл из выгрузки -------------------------------------------------------

class Asr:
    def __init__(self):
        self.calls, self.down = [], False

    def transport(self):
        def handle(request: httpx.Request) -> httpx.Response:
            if self.down:
                raise httpx.ConnectError("нет связи")
            if request.url.path == "/health":
                return httpx.Response(200, json={"model": "m"})
            self.calls.append(request.content)
            return httpx.Response(200, json={"text": "пришлю смету", "seconds": 7.0})
        return httpx.MockTransport(handle)


@pytest_asyncio.fixture
async def voice_rig(conn, tmp_path):
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    asr, tg_calls = Asr(), []

    async def session(account_id, peer_class, tg_id, tg_message_id, max_bytes):
        tg_calls.append(tg_message_id)
        return b"from-telegram", None

    client = AsrClient("http://asr", transport=asr.transport())
    data_dir = tmp_path / "data"
    t = core.Transcriber(pool, client, core.Settings(days=30, max_seconds=600),
                         session_fetch=lambda: session, bot_fetch=lambda: None, data_dir=data_dir)
    account_id = await store.ensure_account(conn, OWNER, "Владелец", "owner")
    chat_id, _ = await store.ensure_chat(conn, account_id, ChatRecord("user", IVAN, "personal_chat", "Иван"))
    try:
        yield t, asr, tg_calls, chat_id, data_dir
    finally:
        await client.close()
        await pool.close()


async def add_voice(conn, chat_id, data_dir, mid, content=OGG):
    rec = MessageRecord(
        tg_message_id=mid, sent_at=datetime.now(timezone.utc) - timedelta(hours=1), kind="message",
        sender_class="user", sender_tg_id=IVAN, sender_name="Иван", text="", entities=None,
        reply_to_tg_id=None, forwarded_from=None, edited_at=None, media_type="voice_message",
        media_path="voice_messages/a.ogg", service_action=None, media_duration=7)
    await store.upsert_messages(conn, [(chat_id, rec)], source="import", owner_tg_id=OWNER)
    rel = media_files.store(data_dir, io.BytesIO(content), max_bytes=10**6, suffix="ogg") if content else \
        "media-files/" + "0" * 32 + ".ogg"
    await conn.execute("UPDATE messages SET media_file = $2 WHERE tg_message_id = $1", mid, rel)
    return rel


async def test_transcriber_reads_the_export_file_and_drops_it_when_done(conn, voice_rig):
    t, asr, tg_calls, chat_id, data_dir = voice_rig
    rel = await add_voice(conn, chat_id, data_dir, 1)
    assert await t.step() == 1
    row = await conn.fetchrow("SELECT transcript_state, media_file, text FROM messages WHERE tg_message_id = 1")
    assert row["transcript_state"] == "done" and "пришлю смету" in row["text"]
    assert asr.calls == [OGG] and tg_calls == []                     # из Telegram ничего не скачивалось
    assert row["media_file"] is None and not (data_dir / rel).exists()


async def test_missing_export_file_falls_back_to_telegram(conn, voice_rig):
    t, asr, tg_calls, chat_id, data_dir = voice_rig
    await add_voice(conn, chat_id, data_dir, 2, content=None)
    await t.step()
    assert tg_calls == [2] and asr.calls == [b"from-telegram"]
    assert await conn.fetchval("SELECT media_file FROM messages WHERE tg_message_id = 2") is None


async def test_file_is_kept_while_the_voice_still_waits(conn, voice_rig):
    t, asr, tg_calls, chat_id, data_dir = voice_rig
    rel = await add_voice(conn, chat_id, data_dir, 3)
    asr.down = True
    await t.step()
    row = await conn.fetchrow("SELECT transcript_state, media_file FROM messages WHERE tg_message_id = 3")
    assert row["transcript_state"] == "pending" and row["media_file"] == rel and (data_dir / rel).exists()
    mid = await conn.fetchval("SELECT id FROM messages WHERE tg_message_id = 3")
    assert await media_files.drop(conn, data_dir, mid) is False and (data_dir / rel).exists()


# --- уборка файлов ---------------------------------------------------------------------------------

def _age(path, delta):
    moment = time.time() - delta.total_seconds()
    os.utime(path, (moment, moment))


async def test_sweep_removes_orphans_settled_gone_and_expired(conn, voice_rig):
    _, _, _, chat_id, data_dir = voice_rig
    root = data_dir / "media-files"
    pending = await add_voice(conn, chat_id, data_dir, 1)            # ждёт расшифровки
    settled_old = await add_voice(conn, chat_id, data_dir, 2)        # расшифровано сутки назад
    settled_new = await add_voice(conn, chat_id, data_dir, 3)        # расшифровано только что
    deleted = await add_voice(conn, chat_id, data_dir, 4)            # сообщение удалено
    expired = await add_voice(conn, chat_id, data_dir, 5)            # ждёт, но лежит 46 дней
    missing = await add_voice(conn, chat_id, data_dir, 6, content=None)
    await conn.execute("UPDATE messages SET transcript_state = 'pending' WHERE tg_message_id IN (1, 5)")
    await conn.execute("UPDATE messages SET transcript_state = 'done' WHERE tg_message_id IN (2, 3)")
    await conn.execute("UPDATE messages SET deleted_at = now() WHERE tg_message_id = 4")
    _age(data_dir / settled_old, timedelta(days=2))
    _age(data_dir / expired, timedelta(days=46))
    orphan_old = media_files.store(data_dir, io.BytesIO(b"x"), max_bytes=10)
    orphan_new = media_files.store(data_dir, io.BytesIO(b"y"), max_bytes=10)
    _age(data_dir / orphan_old, timedelta(hours=2))

    out = await media_files.sweep(conn, data_dir)
    assert out == {"orphans": 1, "settled": 1, "gone": 1, "expired": 1, "missing": 1}
    left = sorted(f"media-files/{p.name}" for p in root.iterdir())
    assert left == sorted([pending, settled_new, orphan_new])
    refs = await stored(conn)
    assert refs == {1: pending, 3: settled_new}
    assert missing not in refs.values() and deleted not in refs.values()
    # второй проход ничего не трогает
    assert await media_files.sweep(conn, data_dir) == {"orphans": 0, "settled": 0, "gone": 0, "expired": 0, "missing": 0}


async def test_janitor_runs_the_sweep(make_client, conn, config, monkeypatch):
    calls = []

    async def fake_sweep(db_conn, data_dir, now=None):
        calls.append(data_dir)
        return {}

    monkeypatch.setattr(ingest_api, "JANITOR_EVERY", 0.01)
    monkeypatch.setattr(media_files, "sweep", fake_sweep)
    await make_client(*MODULES)
    for _ in range(200):
        if calls:
            break
        await asyncio.sleep(0.01)
    assert calls and calls[0] == config.data_dir


# --- путь в терминале ------------------------------------------------------------------------------

async def test_cli_scans_and_imports_a_zip_with_voice_files(conn, tmp_path, monkeypatch, capsys):
    path = tmp_path / "export"                          # так файл подключает ./ops/import-export.sh
    path.write_bytes(media_zip(base="Telegram Desktop/DataExport_2026-10-01"))
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for key, value in {"SHTURMAN_DSN": DSN, "SHTURMAN_API_TOKEN": API_TOKEN, "SHTURMAN_MCP_TOKEN": MCP_TOKEN,
                       "SHTURMAN_DATA_DIR": str(data_dir), "SHTURMAN_ASR": "on"}.items():
        monkeypatch.setenv(key, value)
    cli._scan(str(path))
    assert "сообщений: 11" in capsys.readouterr().out
    await cli._import(str(path), None, set())
    stats = json.loads(capsys.readouterr().out)
    assert stats["messages_new"] == 11 and stats["media_files"] == 3
    assert sorted(await stored(conn)) == [1, 3, 11]
    assert len(list((data_dir / "media-files").iterdir())) == 3


def test_cli_refuses_a_zip_without_result_json(tmp_path):
    path = tmp_path / "export"
    path.write_bytes(make_zip({"a.txt": b"x"}))
    with pytest.raises(SystemExit, match="result.json"):
        cli._scan(str(path))


def test_candidate_filter_respects_rules(tmp_path):
    path = tmp_path / "e.zip"
    path.write_bytes(media_zip())
    now = datetime.now(timezone.utc)
    rules = from_export.Rules(voice_since=now - timedelta(days=30), voice_max=100,
                              files_since=None, files_max=100)
    with ExportArchive(path) as archive:
        att = from_export.Attachments(archive, tmp_path / "d", rules)

        def rec(mid, mt, path, days, size=None, kind="message"):
            return MessageRecord(
                tg_message_id=mid, sent_at=now - timedelta(days=days), kind=kind, sender_class="user",
                sender_tg_id=IVAN, sender_name="И", text="", entities=None, reply_to_tg_id=None,
                forwarded_from=None, edited_at=None, media_type=mt, media_path=path, service_action=None,
                media_size=size)
        att.add(1, rec(1, "voice_message", "v/a.ogg", 1))
        att.add(1, rec(2, "voice_message", "v/a.ogg", 40))                # старое
        att.add(1, rec(3, "photo", "p/a.jpg", 1))                         # фото выключены
        att.add(1, rec(4, "voice_message", None, 1))                      # файла нет в выгрузке
        att.add(1, rec(5, "video_message", "r/a.mp4", 1, size=500))       # больше предела по сведениям
        att.add(1, rec(6, "video_message", "r/b.mp4", 1, kind="service"))
        assert sorted(att.items) == [(1, 1)]


async def test_stop_during_copy_leaves_no_partial_file(conn, config, tmp_path):
    """Удаление загрузки или остановка сервиса посреди копирования: файла не остаётся."""
    path = tmp_path / "e.zip"
    path.write_bytes(media_zip())
    data_dir = tmp_path / "d"
    reads = []

    class Stopped(Exception):
        pass

    def check():
        reads.append(1)
        if len(reads) > 2:          # первый вызов — перед файлом, второй — первое чтение
            raise Stopped()

    rules = await from_export.rules(conn, media_cfg(config))
    with ExportArchive(path) as archive, archive.open_result() as fp:
        att = from_export.Attachments(archive, data_dir, rules, check=check)
        with pytest.raises(Stopped):
            await import_export(conn, fp, attachments=att)
    assert list((data_dir / "media-files").iterdir()) == []
    assert await stored(conn) == {}
