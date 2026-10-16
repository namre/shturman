"""Здоровье сессий Telegram (Р-98): сбор переписки не стоит молча.

Слетевшая сессия видна строкой `tg_health` в `GET /api/status` (её читает `./ops/doctor.sh`) и одним
сообщением владельцу — без повторов каждые 5 минут; то же состояние — не чаще раза в сутки."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from shturman.events import Events
from shturman.tg import health
from shturman.tg.manager import TgManager

from tg_fakes import (U_IVAN, World, now_msg, pool, start_account, tg_config,  # noqa: F401 — pool это фикстура
                      wait_for)

T = datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def owner_messages(conn) -> list[str]:
    rows = await conn.fetch("SELECT payload FROM jobs WHERE kind = 'notify.owner' ORDER BY id")
    return [json.loads(r["payload"])["text"] if isinstance(r["payload"], str) else r["payload"]["text"] for r in rows]


# --- оценка ---

def test_states_by_status_and_time():
    clock = Clock()
    tracker = health.Tracker(clock=clock)
    rows = [{"role": "owner", "status": "error", "update_age": None},
            {"role": "assistant", "status": "unauthorized", "update_age": None}]
    first = {a["role"]: a["state"] for a in tracker.assess(rows)}
    assert first == {"owner": "connecting", "assistant": "relogin"}   # короткий разрыв — ещё не неполадка
    clock.now += health.ERROR_GRACE
    assert {a["role"]: a["state"] for a in tracker.assess(rows)}["owner"] == "error"
    # снова на связи — отсчёт сначала
    assert tracker.assess([{"role": "owner", "status": "running", "update_age": 5.0}])[0]["state"] == "ok"
    for status, state in (("no_session", "relogin"), ("failed", "relogin"), ("locked", "locked"),
                          ("paused", "paused"), ("starting", "connecting")):
        assert health.Tracker(clock=clock).assess([{"role": "owner", "status": status}])[0]["state"] == state
    assert health.worst(["ok", "paused", "relogin", "quiet"]) == "relogin" and health.worst([]) is None


def test_quiet_only_after_a_day_of_live_connection_without_updates():
    clock = Clock()
    tracker = health.Tracker(clock=clock)
    running = [{"role": "owner", "status": "running", "update_age": None}]
    assert tracker.assess(running)[0]["state"] == "ok"              # только подключились
    clock.now += health.QUIET_AFTER
    assert tracker.assess(running)[0]["state"] == "quiet"           # сутки на связи — и ни одного обновления
    fresh = [{"role": "owner", "status": "running", "update_age": 60.0}]
    assert tracker.assess(fresh)[0]["state"] == "ok"                # обновления идут
    stale = [{"role": "owner", "status": "running", "update_age": health.QUIET_AFTER + 1}]
    assert tracker.assess(stale)[0]["state"] == "quiet"


def test_public_snapshot_has_no_labels():
    out = health.public([{"role": "owner", "state": "relogin", "for_s": 10, "update_age_s": None,
                          "label": "Иван Петров"}], configured=True)
    assert out == {"configured": True, "checked": True, "worst": "relogin",
                   "accounts": [{"role": "owner", "state": "relogin", "for_s": 10, "update_age_s": None}]}
    assert health.public(None, configured=False)["checked"] is False


# --- сообщение владельцу ---

async def test_alert_once_then_daily_and_again_after_recovery(conn):
    bad = [{"role": "owner", "state": "relogin", "for_s": 0}]
    assert await health.alert(conn, bad, now=T) == [("owner", "relogin")]
    # проверка каждые 5 минут — без повторов
    for minutes in (5, 10, 60, 23 * 60):
        assert await health.alert(conn, bad, now=T + timedelta(minutes=minutes)) == []
    texts = await owner_messages(conn)
    assert len(texts) == 1
    assert "Войти заново" in texts[0] and "«Вход в ваш аккаунт Telegram»" in texts[0]
    # через сутки, если всё ещё плохо, — ещё раз
    assert await health.alert(conn, bad, now=T + timedelta(hours=24, minutes=1)) == [("owner", "relogin")]
    # был на связи больше часа — следующий сбой сообщается сразу
    later = T + timedelta(hours=25)
    await health.alert(conn, [{"role": "owner", "state": "ok", "for_s": int(health.CLEAR_AFTER)}], now=later)
    assert await health.alert(conn, bad, now=later + timedelta(minutes=5)) == [("owner", "relogin")]
    # другие плохие состояния — своим сообщением; пауза владельца — не неполадка
    assert await health.alert(conn, [{"role": "owner", "state": "quiet", "for_s": 0},
                                     {"role": "assistant", "state": "paused", "for_s": 0}], now=later) \
        == [("owner", "quiet")]
    texts = await owner_messages(conn)
    assert len(texts) == 4 and "Пауза" in texts[-1] and "Снять с паузы" in texts[-1]


def test_alert_texts_name_the_place_and_have_no_operator_secrets():
    for role in ("owner", "assistant"):
        for state in health.ALERT:
            text = health.alert_text(role, state)
            assert "⚠️" in text and len(text) < 1000
            assert "setup-link" not in text and "http" not in text
    assert "«Группы: аккаунт помощника»" in health.alert_text("assistant", "relogin")


# --- настоящий менеджер сессий с подставным Telegram ---

@pytest.fixture
async def manager(pool, conn, config):  # noqa: F811
    world = World()
    world.dialogs = [U_IVAN]
    m = TgManager(tg_config(config), pool, Events(), client_factory=world.factory, pacing=0.0)
    m._retry_first = 0.02
    try:
        yield m, world
    finally:
        await m.stop()


async def test_revoked_session_becomes_relogin_and_owner_is_told_once(manager, pool, conn):
    m, world = manager
    rt = await start_account(m, world, "owner")
    watcher = health.Watcher(m, pool)
    assessed = await watcher.check()
    assert [(a["role"], a["state"]) for a in assessed] == [("owner", "ok")]
    # Владелец завершил сессию в «Устройствах»: Telethon отключается (цикл обновлений получил
    # AuthKeyUnregisteredError), при переподключении get_me() отвечает None.
    rt.client.authorized = False
    rt.client.drop()
    await wait_for(lambda: rt.status == "unauthorized")
    assessed = await watcher.check()
    assert [(a["role"], a["state"]) for a in assessed] == [("owner", "relogin")]
    assert watcher.snapshot()["worst"] == "relogin"
    await watcher.check()
    await watcher.check()
    texts = await owner_messages(conn)
    assert len(texts) == 1 and "Сбор переписки остановлен" in texts[0]
    assert "Евгений" not in json.dumps(watcher.snapshot(), ensure_ascii=False)   # подписи аккаунта нет


async def test_live_updates_are_counted_for_quiet_detection(manager, pool):
    m, world = manager
    rt = await start_account(m, world, "owner")
    assert (await m.health_rows())[0]["update_age"] is None
    await rt.client.emit(__import__("telethon").tl.types.UpdateNewMessage(now_msg(1, ("user", U_IVAN.id), "привет"), 1, 1))
    age = (await m.health_rows())[0]["update_age"]
    assert age is not None and age < 5


async def test_status_route_reports_health(make_client, config):
    import dataclasses

    client, state = await make_client("shturman.api_core", "shturman.tg.service",
                                      cfg=dataclasses.replace(config, tg_api_id=0, tg_api_hash=""))
    body = (await client.get("/api/status")).json()
    assert body["tg_health"]["configured"] is False and body["tg_health"]["accounts"] == []
    assert body["pulse"]["enabled"] is False and body["pulse"]["configured"] is False
