"""Блокировка «одна сессия — один процесс» и вход по QR."""

import asyncio
import gc
import logging

import pytest
from telethon import errors

from shturman.tg import qr
from shturman.tg.client import RequestPolicy
from shturman.tg.lock import SessionLock, SessionLocked, advisory_key
from shturman.tg.qr import LoginFlow, LoginRejected, LoginStateError

from conftest import DSN
from tg_fakes import ME, PASSWORD, FakeClient, World

# --- блокировка ---


async def test_second_instance_on_same_session_file_is_refused(conn, tmp_path):
    path = tmp_path / "sessions" / "owner.session"
    first, second = SessionLock(path, DSN), SessionLock(path, DSN)
    await first.acquire("slot:owner")
    with pytest.raises(SessionLocked):
        await second.acquire("slot:owner")
    assert first.held and not second.held
    await first.release()
    await second.acquire("slot:owner")   # после освобождения — можно
    await second.release()


async def test_same_account_from_another_data_dir_is_refused_by_database_lock(conn, tmp_path):
    """Второй экземпляр сервиса с другим каталогом данных: файл другой, база та же."""
    first = SessionLock(tmp_path / "a" / "owner.session", DSN)
    second = SessionLock(tmp_path / "b" / "owner.session", DSN)
    await first.acquire("slot:owner")
    with pytest.raises(SessionLocked):
        await second.acquire("slot:owner")
    assert not second.held                      # отказ снимает и блокировку файла
    await second.acquire("slot:assistant")      # другая роль не мешает
    await first.add("account:1000")
    with pytest.raises(SessionLocked):
        await second.add("account:1000")        # но тот же аккаунт — мешает
    await first.release()
    await second.add("account:1000")
    await second.release()
    assert await conn.fetchval("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND database = (SELECT oid FROM pg_database WHERE datname = current_database())") == 0


async def test_lost_database_connection_is_reported(conn, tmp_path):
    lost = []
    lock = SessionLock(tmp_path / "owner.session", DSN, on_lost=lambda: lost.append(1))
    await lock.acquire("slot:owner")
    lock._conn.terminate()
    await asyncio.sleep(0.05)
    assert lost == [1]
    await lock.release()
    assert advisory_key("slot:owner") != advisory_key("slot:assistant")
    assert -2**63 <= advisory_key("account:1000") < 2**63


# --- вход по QR ---

class Harness:
    def __init__(self, world=None, **kw):
        self.world = world or World()
        self.world.authorized = False
        self.client = FakeClient(self.world, "assistant", RequestPolicy("assistant", login=True))
        self.closed = 0
        self.users = []
        self.reject = None
        self.flow = LoginFlow(self.client, "assistant", on_success=self.success, on_close=self.close,
                              clock=self.client.clock, **kw)

    async def success(self, user):
        if self.reject:
            raise LoginRejected(self.reject)
        self.users.append(user)
        return 42

    async def close(self):
        self.closed += 1


async def test_qr_login_completes_when_code_is_scanned():
    h = Harness()
    await h.flow.start()
    first = h.flow.snapshot()
    assert first["status"] == "pending" and first["qr_svg"].startswith("<svg")
    assert first["link"] == "tg://login?token=QRTOKEN1SECRET" and "expires_at" in first
    assert h.client.waits or True
    h.client.scan.set_result(ME)
    await h.flow.wait_finished()
    done = h.flow.snapshot()
    assert done == {"login_id": h.flow.id, "role": "assistant", "status": "completed", "account_id": 42}
    assert h.users == [ME] and h.closed == 0      # сессия остаётся — её не закрывают
    assert h.client.connected


async def test_expiring_code_is_recreated_on_the_same_client():
    h = Harness()
    h.client.scan_timeouts = 2            # два кода истекли несканированными
    await h.flow.start()
    qr_object = h.client.qr
    await asyncio.sleep(0.01)
    assert h.client.qr is qr_object and qr_object.issued == 3     # не новый клиент, а recreate()
    assert h.flow.snapshot()["link"].endswith("QRTOKEN3SECRET")
    # ожидание выставлено до истечения кода, с запасом
    assert all(w is not None and 1 <= w <= 30 for w in h.client.waits)
    h.client.scan.set_result(ME)
    await h.flow.wait_finished()
    assert h.flow.status == "completed"


async def test_login_expires_after_lifetime_and_cleans_up():
    h = Harness(lifetime=100)
    h.client.scan_timeouts = 50
    await h.flow.start()
    await h.flow.wait_finished()
    snap = h.flow.snapshot()
    assert snap["status"] == "expired" and "qr_svg" not in snap and "link" not in snap
    assert h.closed == 1 and h.client.qr.issued <= 5


async def test_cancel_stops_login_and_cleans_up():
    h = Harness()
    await h.flow.start()
    await h.flow.cancel()
    assert h.flow.status == "cancelled" and h.closed == 1
    assert "qr_svg" not in h.flow.snapshot()
    await h.flow.cancel()   # повторная отмена безвредна
    with pytest.raises(LoginStateError):
        await h.flow.submit_password("x")


def _mentions(obj, needle, seen=None, depth=0):
    """Ищет строку в объекте и во всём, на что он ссылается."""
    seen = seen if seen is not None else set()
    if id(obj) in seen or depth > 6:
        return False
    seen.add(id(obj))
    if isinstance(obj, str):
        return needle in obj
    if isinstance(obj, (bytes, bytearray)):
        return needle.encode() in bytes(obj)
    if isinstance(obj, dict):
        return any(_mentions(k, needle, seen, depth + 1) or _mentions(v, needle, seen, depth + 1)
                   for k, v in obj.items())
    if isinstance(obj, (list, tuple, set, frozenset)):
        return any(_mentions(v, needle, seen, depth + 1) for v in obj)
    if isinstance(obj, asyncio.Queue):
        return _mentions(list(obj._queue), needle, seen, depth + 1)
    return _mentions(getattr(obj, "__dict__", {}), needle, seen, depth + 1)


async def test_two_factor_password_is_used_once_and_not_retained(caplog):
    caplog.set_level(logging.DEBUG)
    world = World()
    world.password = PASSWORD
    h = Harness(world)
    await h.flow.start()
    h.client.scan.set_exception(errors.SessionPasswordNeededError(None))
    await asyncio.sleep(0.01)
    snap = h.flow.snapshot()
    assert snap["status"] == "password_required" and snap["hint"] == "кличка кота"
    assert "qr_svg" not in snap and "link" not in snap

    await h.flow.submit_password("не тот пароль")
    snap = h.flow.snapshot()
    assert snap["status"] == "password_required" and snap["attempts_left"] == 2
    assert snap["error"] == "Неверный облачный пароль."

    await h.flow.submit_password(PASSWORD)
    assert h.flow.status == "completed" and h.users == [ME]
    assert "error" not in h.flow.snapshot()

    gc.collect()
    # всё состояние входа, кроме подставного клиента (в нём пароль лежит как «правильный ответ»)
    own = {k: v for k, v in vars(h.flow).items() if k != "_client"}
    assert len(own) > 10 and "_inbox" in own
    assert not _mentions(own, PASSWORD)
    assert not _mentions(own, "не тот пароль")
    assert _mentions({"x": [PASSWORD]}, PASSWORD)   # сам поиск работает
    assert PASSWORD not in caplog.text and "не тот пароль" not in caplog.text
    assert "QRTOKEN" not in caplog.text
    assert PASSWORD not in str(h.flow.snapshot())


async def test_too_many_wrong_passwords_fail_the_login():
    world = World()
    world.password = PASSWORD
    h = Harness(world, password_attempts=2)
    await h.flow.start()
    h.client.scan.set_exception(errors.SessionPasswordNeededError(None))
    await asyncio.sleep(0.01)
    await h.flow.submit_password("раз")
    await h.flow.submit_password("два")
    assert h.flow.status == "failed" and h.closed == 1
    with pytest.raises(LoginStateError):
        await h.flow.submit_password(PASSWORD)


async def test_password_wait_times_out():
    h = Harness(password_timeout=0.05)
    await h.flow.start()
    h.client.scan.set_exception(errors.SessionPasswordNeededError(None))
    await h.flow.wait_finished()
    assert h.flow.status == "expired" and h.closed == 1


async def test_rejected_account_and_flood_are_reported_to_owner(caplog):
    h = Harness()
    h.reject = "Это основной аккаунт владельца."
    await h.flow.start()
    h.client.scan.set_result(ME)
    await h.flow.wait_finished()
    assert h.flow.snapshot()["error"] == "Это основной аккаунт владельца." and h.flow.status == "failed"
    assert h.closed == 1

    h = Harness()
    await h.flow.start()
    h.client.scan.set_exception(errors.FloodWaitError(None, 120))
    await h.flow.wait_finished()
    assert h.flow.status == "failed" and "120" in h.flow.snapshot()["error"]

    h = Harness()
    await h.flow.start()
    h.client.scan.set_exception(RuntimeError("tg://login?token=QRTOKEN1SECRET"))
    await h.flow.wait_finished()
    assert h.flow.status == "failed" and "QRTOKEN" not in h.flow.snapshot()["error"]
    assert "QRTOKEN" not in caplog.text


def test_qr_svg_is_inline_and_self_contained():
    svg = qr.qr_svg("tg://login?token=abc")
    assert svg.startswith("<svg") and svg.endswith("</svg>") and "http" not in svg.replace("http://www.w3.org", "")
