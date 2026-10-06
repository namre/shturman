"""Загрузка экспорта Telegram Desktop для мастера настройки: поток на диск, просмотр, фоновый импорт."""

import asyncio
import json
import stat
from datetime import datetime, timedelta, timezone

import pytest

import shturman.ingest_api as ingest_api
from shturman.importer import ImportStats

from conftest import IVAN, OWNER, full_export, msg

T = 1789200000
MODULES = ("shturman.api_core", "shturman.ingest_api")


def payload(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


async def pieces(data: bytes, size: int = 512, seen: list | None = None):
    """Тело запроса кусками, без заранее известной длины — как при отправке большого файла."""
    for i in range(0, len(data), size):
        if seen is not None:
            seen.append(i)
        yield data[i:i + size]


async def upload(client, obj) -> str:
    r = await client.post("/api/imports", content=pieces(payload(obj)))
    assert r.status_code == 201, r.text
    return r.json()["import_id"]


async def wait_state(client, import_id, *states, timeout=10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        body = (await client.get(f"/api/imports/{import_id}")).json()
        if body["state"] in states:
            return body
        assert asyncio.get_running_loop().time() < deadline, body
        await asyncio.sleep(0.02)


def files(config):
    return sorted(p.name for p in config.uploads_dir.iterdir())


def russian(response) -> bool:
    return any("а" <= ch <= "я" for ch in response.json()["error"])


async def test_upload_is_streamed_to_a_private_file(make_client, config, sample_export):
    client, _ = await make_client(*MODULES)
    data = payload(sample_export)
    r = await client.post("/api/imports", content=pieces(data))
    assert r.status_code == 201
    body = r.json()
    assert body["size_bytes"] == len(data) and body["state"] == "uploaded"
    name = f"export-{body['import_id']}.json"
    assert files(config) == [name] and len(body["import_id"]) == 32
    path = config.uploads_dir / name
    assert path.read_bytes() == data
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    status = (await client.get(f"/api/imports/{body['import_id']}")).json()
    assert (status["state"], status["size_bytes"], status["file_kept"], status["scanned"]) == (
        "uploaded", len(data), True, False)
    assert status["progress"] == {"bytes_read": 0, "bytes_total": len(data), "percent": 0}
    assert [i["import_id"] for i in (await client.get("/api/imports")).json()["imports"]] == [body["import_id"]]


async def test_upload_size_cap(make_client, config, monkeypatch):
    monkeypatch.setenv(ingest_api.UPLOAD_ENV, "2000")
    client, _ = await make_client(*MODULES)
    # длина объявлена заранее — отказ до чтения тела
    r = await client.post("/api/imports", content=b"x" * 2001)
    assert r.status_code == 413 and russian(r)
    # длина неизвестна — приём обрывается, как только предел пройден, остаток не читается
    seen: list[int] = []
    r = await client.post("/api/imports", content=pieces(b"x" * 100_000, 500, seen))
    assert r.status_code == 413 and russian(r)
    assert len(seen) < 10
    assert files(config) == []                      # недописанный файл не остаётся
    assert (await client.post("/api/imports", content=pieces(b"x" * 2000, 500))).status_code == 201
    assert (await client.get("/api/imports")).json()["imports"][0]["size_bytes"] == 2000


async def test_upload_rejects_empty_and_form_bodies(make_client, config):
    client, _ = await make_client(*MODULES)
    r = await client.post("/api/imports", content=b"")
    assert r.status_code == 400 and russian(r)
    r = await client.post("/api/imports", files={"file": ("result.json", b"{}", "application/json")})
    assert r.status_code == 415 and russian(r)
    assert files(config) == []


async def test_bad_upload_limit_setting_stops_the_service(make_client, monkeypatch):
    from shturman.config import ConfigError
    monkeypatch.setenv(ingest_api.UPLOAD_ENV, "два гигабайта")
    with pytest.raises(ConfigError):
        await make_client(*MODULES)


async def test_number_of_kept_files_is_limited(make_client, config):
    client, _ = await make_client(*MODULES)
    ids = [await upload(client, {"n": i}) for i in range(ingest_api.MAX_KEPT_FILES)]
    r = await client.post("/api/imports", content=b"{}")
    assert (r.status_code, r.json()["code"]) == (409, "too_many_uploads") and russian(r)
    assert (await client.delete(f"/api/imports/{ids[0]}")).json() == {"deleted": True, "was_running": False}
    assert (await client.post("/api/imports", content=b"{}")).status_code == 201
    assert len(files(config)) == ingest_api.MAX_KEPT_FILES


async def test_scan_lists_owner_and_chats(make_client, conn, sample_export):
    client, _ = await make_client(*MODULES)
    sample_export["chats"]["list"].append({
        "name": "Telegram", "type": "personal_chat", "id": 777000,
        "messages": [msg(1, T, 777000, "Telegram", "Login code: 12345")]})
    import_id = await upload(client, sample_export)
    r = await client.get(f"/api/imports/{import_id}/scan")
    assert r.status_code == 200
    body = r.json()
    assert body["owner"] == {"tg_user_id": OWNER, "name": "Евгений Тестов"} and body["total_messages"] == 9
    chats = {c["key"]: c for c in body["chats"]}
    assert body["chats"][0]["key"] == f"user:{IVAN}"                 # самый большой — первым
    assert chats[f"user:{IVAN}"] == {
        "key": f"user:{IVAN}", "kind": "user", "tg_id": IVAN, "type": "personal_chat", "name": "Иван Петров",
        "messages": 6, "first_at": "2026-09-12", "last_at": "2026-09-12", "locked": False, "excluded": False}
    assert chats["chat:3001"]["type"] == "private_group" and chats["channel:4001"]["messages"] == 1
    assert chats["user:2999"]["messages"] == 0 and chats["user:777000"]["locked"] is True
    assert "Login code" not in r.text and "смету" not in r.text       # текста сообщений в ответе нет
    assert await conn.fetchval("SELECT count(*) FROM chats") == 0     # просмотр в базу не пишет
    status = (await client.get(f"/api/imports/{import_id}")).json()
    assert (status["state"], status["scanned"], status["file_kept"]) == ("uploaded", True, True)
    assert (await client.get(f"/api/imports/{import_id}/scan")).json() == body   # второй раз — из памяти


async def test_slow_scan_answers_202_and_is_picked_up_later(make_client, sample_export, monkeypatch):
    client, _ = await make_client(*MODULES)
    release = asyncio.Event()
    loop = asyncio.get_running_loop()
    real = ingest_api.scan

    def slow_scan(fp):
        asyncio.run_coroutine_threadsafe(release.wait(), loop).result(10)
        return real(fp)

    monkeypatch.setattr(ingest_api, "scan", slow_scan)
    import_id = await upload(client, sample_export)
    r = await client.get(f"/api/imports/{import_id}/scan", params={"wait": "0.05"})
    assert r.status_code == 202 and r.json()["state"] == "scanning"
    busy = await client.post(f"/api/imports/{import_id}/run", json={})
    assert (busy.status_code, busy.json()["code"]) == (409, "scanning")
    release.set()
    r = await client.get(f"/api/imports/{import_id}/scan")
    assert r.status_code == 200 and r.json()["total_messages"] == 8
    bad = await client.get(f"/api/imports/{import_id}/scan", params={"wait": "долго"})
    assert bad.status_code == 400 and russian(bad)


async def test_run_imports_in_background_and_removes_the_file(make_client, conn, config, sample_export):
    client, state = await make_client(*MODULES)
    seen = []

    async def on_live(event):
        seen.append(event)

    state.events.subscribe("message.live", on_live)
    import_id = await upload(client, sample_export)
    r = await client.post(f"/api/imports/{import_id}/run", json={"exclude": ["chat:3001"]})
    assert r.status_code == 202 and r.json()["state"] == "running"
    done = await wait_state(client, import_id, "done", "failed")
    assert done["state"] == "done" and done["error"] is None
    assert done["stats"]["messages_new"] == 7 and done["stats"]["chats_excluded"] == 1
    assert done["stats"]["owner_tg_user_id"] == OWNER and done["stats"]["excluded_names"] == ["Семья"]
    assert done["progress"]["percent"] == 100 and done["progress"]["bytes_read"] == done["size_bytes"]
    assert done["file_kept"] is False and files(config) == []          # выгрузка на диске не остаётся
    assert await conn.fetchval("SELECT count(*) FROM messages") == 7
    assert await conn.fetchval("SELECT count(*) FROM imports WHERE finished_at IS NOT NULL") == 1
    assert await conn.fetchval("SELECT role FROM accounts WHERE tg_user_id = $1", OWNER) == "owner"
    await state.events.drain()
    assert seen == []                                                  # импорт прошлого событий не порождает

    # файл уже удалён: ни повторного запуска, ни просмотра
    for method, path in (("POST", "run"), ("GET", "scan")):
        r = await client.request(method, f"/api/imports/{import_id}/{path}")
        assert (r.status_code, r.json()["code"]) == (409, "no_file") and russian(r)
    # итоги остаются доступны, пока запись не удалят
    assert (await client.delete(f"/api/imports/{import_id}")).json()["deleted"] is True
    assert (await client.get(f"/api/imports/{import_id}")).status_code == 404

    # тот же экспорт ещё раз: дублей нет, запрет на чат помнится
    again = await upload(client, sample_export)
    await client.post(f"/api/imports/{again}/run")
    stats = (await wait_state(client, again, "done", "failed"))["stats"]
    assert (stats["messages_new"], stats["messages_known"], stats["chats_excluded"]) == (0, 7, 1)
    scan_after = await upload(client, sample_export)
    chats = {c["key"]: c for c in (await client.get(f"/api/imports/{scan_after}/scan")).json()["chats"]}
    assert chats["chat:3001"]["excluded"] is True and chats[f"user:{IVAN}"]["excluded"] is False


async def test_single_chat_export_needs_owner_and_can_be_retried(make_client, conn, config):
    client, _ = await make_client(*MODULES)
    single = {"name": "Иван Петров", "type": "personal_chat", "id": IVAN,
              "messages": [msg(1, T, OWNER, "Евгений", "Привет")]}
    import_id = await upload(client, single)
    assert (await client.get(f"/api/imports/{import_id}/scan")).json()["owner"] is None
    await client.post(f"/api/imports/{import_id}/run", json={})
    failed = await wait_state(client, import_id, "done", "failed")
    assert failed["state"] == "failed" and "владельц" in failed["error"] and failed["file_kept"] is True
    assert await conn.fetchval("SELECT count(*) FROM messages") == 0

    r = await client.post(f"/api/imports/{import_id}/run", json={"owner_id": OWNER})
    assert r.status_code == 202
    done = await wait_state(client, import_id, "done", "failed")
    assert done["state"] == "done" and done["stats"]["messages_new"] == 1 and files(config) == []
    assert await conn.fetchval("SELECT is_outgoing FROM messages") is True


async def test_owner_mismatch_fails_with_clear_text(make_client, conn, sample_export):
    client, _ = await make_client(*MODULES)
    import_id = await upload(client, sample_export)
    await client.post(f"/api/imports/{import_id}/run", json={"owner_id": 42})
    failed = await wait_state(client, import_id, "done", "failed")
    assert failed["state"] == "failed" and "не совпадает" in failed["error"]
    assert await conn.fetchval("SELECT count(*) FROM messages") == 0


@pytest.mark.parametrize("raw", [
    b'{"hello": [1, 2, 3]}',                       # JSON, но не экспорт
    b'{"chats": {"list": [{"name": "x", "type": "personal_chat", "id": 5, "messages": [{"id": 1,',   # оборван
    b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,           # вообще не JSON
    payload(full_export([{"name": "Ответы", "type": "марсианский_чат", "id": 7,
                          "messages": [msg(1, T, 7, "x", "y")]}])),
])
async def test_not_an_export_fails_and_file_is_removed(make_client, conn, config, raw):
    client, _ = await make_client(*MODULES)
    for step in ("scan", "run"):
        r = await client.post("/api/imports", content=raw)
        import_id = r.json()["import_id"]
        if step == "scan":
            r = await client.get(f"/api/imports/{import_id}/scan")
            assert (r.status_code, r.json()["code"]) == (422, "scan_failed") and russian(r)
            again = await client.get(f"/api/imports/{import_id}/scan")
            assert again.status_code == 422
        else:
            await client.post(f"/api/imports/{import_id}/run", json={})
        failed = await wait_state(client, import_id, "failed", "done")
        assert failed["state"] == "failed" and failed["file_kept"] is False
        assert any("а" <= ch <= "я" for ch in failed["error"])
        assert files(config) == []
        r = await client.post(f"/api/imports/{import_id}/run", json={})
        assert (r.status_code, r.json()["code"]) == (409, "no_file")
    assert await conn.fetchval("SELECT count(*) FROM messages") == 0


async def test_one_import_at_a_time_with_live_progress(make_client, conn, config, sample_export, monkeypatch):
    """Импортёр подменён: он стоит на месте, пока тест не разрешит, и отдаёт счётчики по ходу работы
    (так будет работать настоящий, когда получит параметр stats)."""
    client, _ = await make_client(*MODULES)
    release = asyncio.Event()

    async def held(db_conn, fp, *, owner_tg_user_id=None, exclude=(), source_name="", stats=None):
        assert await db_conn.fetchval("SELECT 1") == 1 and source_name == "result.json"
        fp.read(100)
        stats.chats, stats.messages_read, stats.messages_new = 2, 40, 30
        await release.wait()
        stats.messages_new = 31
        return stats

    monkeypatch.setattr(ingest_api, "import_export", held)
    first, second = await upload(client, sample_export), await upload(client, sample_export)
    assert (await client.post(f"/api/imports/{first}/run", json={})).status_code == 202
    running = await wait_state(client, first, "running")
    for _ in range(100):
        if running["progress"].get("messages_read"):
            break
        await asyncio.sleep(0.01)
        running = (await client.get(f"/api/imports/{first}")).json()
    assert running["progress"]["bytes_read"] == 100 and 0 < running["progress"]["percent"] < 100
    assert (running["progress"]["chats"], running["progress"]["messages_read"],
            running["progress"]["messages_new"]) == (2, 40, 30)

    for target in (first, second):
        r = await client.post(f"/api/imports/{target}/run", json={})
        assert (r.status_code, r.json()["code"]) == (409, "import_running") and russian(r)
    r = await client.get(f"/api/imports/{first}/scan")
    assert (r.status_code, r.json()["code"]) == (409, "import_running")
    # менять исключения посреди импорта нельзя: пачка могла уже пройти проверку запрета
    r = await client.put("/api/chats/1/excluded", json={"excluded": True, "purge": True})
    assert (r.status_code, r.json()["code"]) == (409, "import_running") and russian(r)

    release.set()
    done = await wait_state(client, first, "done", "failed")
    assert done["state"] == "done" and done["stats"]["messages_new"] == 31
    assert (await client.post(f"/api/imports/{second}/run", json={})).status_code == 202


async def test_delete_stops_a_running_import_and_removes_the_file(make_client, config, sample_export, monkeypatch):
    client, state = await make_client(*MODULES)
    started = asyncio.Event()

    async def stuck(db_conn, fp, **kwargs):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(ingest_api, "import_export", stuck)
    import_id = await upload(client, sample_export)
    await client.post(f"/api/imports/{import_id}/run", json={})
    await asyncio.wait_for(started.wait(), 5)
    r = await client.delete(f"/api/imports/{import_id}")
    assert r.json() == {"deleted": True, "was_running": True}
    assert files(config) == [] and state.extras["imports"].running() is None
    assert (await client.get(f"/api/imports/{import_id}")).status_code == 404
    assert not [t for t in asyncio.all_tasks() if t.get_name() == f"import-{import_id}"]


async def test_internal_failure_is_reported_without_details(make_client, config, sample_export, monkeypatch, caplog):
    client, _ = await make_client(*MODULES)

    async def broken(db_conn, fp, **kwargs):
        raise RuntimeError("Login code: 12345 — этого в ответе и журнале быть не должно")

    monkeypatch.setattr(ingest_api, "import_export", broken)
    import_id = await upload(client, sample_export)
    await client.post(f"/api/imports/{import_id}/run", json={})
    failed = await wait_state(client, import_id, "failed", "done")
    assert failed["state"] == "failed" and "12345" not in failed["error"] and failed["file_kept"] is True
    assert "12345" not in caplog.text and "RuntimeError" in caplog.text


async def test_unknown_ids_and_bad_run_bodies(make_client, sample_export):
    client, _ = await make_client(*MODULES)
    for import_id in ("0" * 32, "..%2F..%2Fetc%2Fpasswd", "нет", "A" * 32):
        for method, suffix in (("GET", ""), ("GET", "/scan"), ("POST", "/run"), ("DELETE", "")):
            r = await client.request(method, f"/api/imports/{import_id}{suffix}")
            assert r.status_code == 404, (method, import_id, suffix)
    import_id = await upload(client, sample_export)
    for body in ({"exclude": "user:1"}, {"exclude": ["user:abc"]}, {"exclude": ["bot:1"]}, {"exclude": [5]},
                 {"exclude": ["user:" + "9" * 30]}, {"owner_id": "1000"}, {"owner_id": -1}, {"owner_id": True},
                 {"owner_id": 2 ** 70}):
        r = await client.post(f"/api/imports/{import_id}/run", json=body)
        assert r.status_code == 400 and russian(r), body
    r = await client.post(f"/api/imports/{import_id}/run", content=b"[1]")
    assert r.status_code == 400 and russian(r)
    assert (await client.get(f"/api/imports/{import_id}")).json()["state"] == "uploaded"


async def test_stale_uploads_are_removed_at_startup(make_client, config):
    config.uploads_dir.mkdir(parents=True)
    for name in ("export-" + "a" * 32 + ".json", "export-" + "b" * 32 + ".part", "чужой-файл.bin"):
        (config.uploads_dir / name).write_bytes(b"{}")
    client, _ = await make_client(*MODULES)
    assert files(config) == ["чужой-файл.bin"]                        # чужое не трогаем
    assert (await client.get("/api/imports")).json() == {"imports": []}


async def test_unused_uploads_expire(make_client, config, sample_export):
    client, state = await make_client(*MODULES)
    registry = state.extras["imports"]
    old, fresh = await upload(client, sample_export), await upload(client, sample_export)
    registry.items[old].uploaded_at = datetime.now(timezone.utc) - ingest_api.UPLOAD_TTL - timedelta(minutes=1)
    assert registry.expire(datetime.now(timezone.utc)) == 1
    assert files(config) == [f"export-{fresh}.json"]
    assert (await client.get(f"/api/imports/{old}")).status_code == 404


def test_finished_records_make_room_for_new_ones(tmp_path):
    registry = ingest_api.Registry(directory=tmp_path, max_bytes=10)
    now = datetime.now(timezone.utc)
    for i in range(ingest_api.MAX_RECORDS):
        item = ingest_api.Upload(f"{i:032x}", tmp_path / f"export-{i}.json", 1, now + timedelta(seconds=i),
                                 state="done", stats=ImportStats().as_dict())
        registry.items[item.id] = item
    registry.trim()
    assert len(registry.items) == ingest_api.MAX_RECORDS - 1 and f"{0:032x}" not in registry.items
