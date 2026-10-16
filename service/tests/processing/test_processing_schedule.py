"""Расписание обработки (Р-96): днём раз в час — только закрытые эпизоды, без дублей с ночным прогоном,
вне тихих часов и в пределах запросов; сводка с кнопками — только в свои часы."""

from datetime import datetime, time, timedelta, timezone

from shturman import bridge
from shturman.notify import settings as notify_settings
from shturman.processing import pipeline

from proc_helpers import OWNER, TZ, account, answer, chat, claim, say

IVAN, MARIA = 2001, 2002


async def scene(conn, *, quiet=False):
    account_id = await account(conn)
    await bridge.set_owner(conn, OWNER, OWNER)
    await notify_settings.save(conn, {**notify_settings.DEFAULTS, "quiet": quiet})
    return (await chat(conn, account_id, IVAN, "Иван Петров"), await chat(conn, account_id, MARIA, "Мария Сидорова"))


def item(message, quote, what, due=None):
    return {"message": message, "source_quote": quote, "what": what, "due_expression": due, "due_message": None,
            "recipient": "ВЛАДЕЛЕЦ", "duplicate_of": None}


async def answer_all(conn):
    """Подставная модель: каждое сообщение эпизода — обещание (что — сам текст)."""
    for job in await claim(conn):
        if job["payload"]["schema_name"] != "commitments":
            await answer(conn, job, {"updates": []})
            continue
        lines = [line for line in job["payload"]["input"].splitlines() if line.startswith("[") and not line.startswith("[П")]
        found = []
        for line in lines:
            number, text = int(line[1:].split("]")[0]), line.split(": ", 1)[1]
            found.append(item(number, text, text.lower()))
        await answer(conn, job, {"commitments": found})


async def digests(conn):
    return await claim(conn, bridge.NOTIFY_OWNER)


async def test_hourly_run_takes_only_closed_episodes_and_never_repeats_them(conn):
    ivan, maria = await scene(conn)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    await say(conn, ivan, [(IVAN, "Иван Петров", "Смету пришлю завтра")], start=now - timedelta(hours=3))
    await say(conn, maria, [(MARIA, "Мария Сидорова", "Договор пришлю в пятницу")], start=now - timedelta(minutes=10),
              first_id=20)
    await say(conn, ivan, [(IVAN, "Иван Петров", "Акт подпишу завтра")], start=now - timedelta(hours=2), first_id=30)
    schedule = pipeline.Schedule(every=60)

    out = await pipeline.hourly_tick(conn, tz=TZ, schedule=schedule, now=now)
    assert (out["status"], out["planned"], out["trigger"]) == ("planned", 2, "hourly")   # открытый разговор ждёт
    assert out["more"] is True and out["tokens_in_estimate"] > 0
    await answer_all(conn)
    assert sorted(r["what"] for r in await conn.fetch("SELECT what FROM commitments")) == [
        "акт подпишу завтра", "смету пришлю завтра"]
    assert await digests(conn) == []                       # сводка — только в час сводки
    assert await pipeline.hourly_tick(conn, tz=TZ, schedule=schedule, now=now + timedelta(minutes=30)) is None

    # через час разговор закрыт: берётся только он
    out = await pipeline.hourly_tick(conn, tz=TZ, schedule=schedule, now=now + timedelta(minutes=61))
    assert out["planned"] == 1
    await answer_all(conn)
    assert await conn.fetchval("SELECT count(*) FROM commitments") == 3
    # ночной прогон после дневных ничего не повторяет и владельцу ночью ничего не шлёт
    out = await pipeline.plan_run(conn, tz=TZ, trigger="nightly", now=now + timedelta(hours=10))
    assert out["planned"] == 0
    assert await digests(conn) == []
    assert await conn.fetchval("SELECT count(*) FROM processing_requests WHERE kind = 'extract'") == 3


async def test_hourly_run_respects_quiet_hours_and_interval_setting(conn):
    await scene(conn, quiet=True)
    await notify_settings.save(conn, {**notify_settings.DEFAULTS, "quiet": True, "quiet_from": "00:00",
                                      "quiet_to": "23:59"})
    now = datetime.now(timezone.utc)
    assert await pipeline.hourly_tick(conn, tz=TZ, schedule=pipeline.Schedule(), now=now) is None
    await notify_settings.save(conn, {**notify_settings.DEFAULTS, "quiet": False})
    assert await pipeline.hourly_tick(conn, tz=TZ, schedule=pipeline.Schedule(every=0), now=now) is None


async def test_daily_cap_defers_to_the_nightly_run_without_losing_anything(conn):
    ivan, maria = await scene(conn)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    await say(conn, ivan, [(IVAN, "Иван Петров", "Смету пришлю завтра")], start=now - timedelta(hours=3))
    await say(conn, maria, [(MARIA, "Мария Сидорова", "Цена 15 тыс, договор пришлю в пятницу")],
              start=now - timedelta(hours=2), first_id=20)
    schedule = pipeline.Schedule(every=60, hourly_cap=10, daily_cap=1)
    out = await pipeline.hourly_tick(conn, tz=TZ, schedule=schedule, now=now)
    assert (out["planned"], out["cap_reached"], out["fact_episodes_skipped"]) == (1, True, 0)
    await answer_all(conn)
    out = await pipeline.hourly_tick(conn, tz=TZ, schedule=schedule, now=now + timedelta(minutes=61))
    assert out["status"] == "capped"
    assert (await pipeline.overview(conn))["processing_capped"] is True
    # ночью — остальное
    out = await pipeline.plan_run(conn, tz=TZ, trigger="nightly", now=now + timedelta(hours=8))
    assert out["planned"] == 1
    await answer_all(conn)
    assert await conn.fetchval("SELECT count(*) FROM commitments") == 2


async def test_digest_goes_only_at_its_hours_and_never_in_quiet_hours(conn):
    ivan, _ = await scene(conn)
    await say(conn, ivan, [(IVAN, "Иван Петров", "Смету пришлю завтра")])
    await pipeline.plan_run(conn, tz=TZ, trigger="nightly", now=datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc))
    await answer_all(conn)
    assert await digests(conn) == []
    slots = pipeline.parse_slots("13:00, 19:00, мусор")
    assert slots == (time(13, 0), time(19, 0))
    msk = lambda h, m=0: datetime(2026, 10, 6, h - 3, m, tzinfo=timezone.utc)   # noqa: E731 — часы по Москве
    assert await pipeline.digest_tick(conn, tz=TZ, slots=slots, now=msk(12, 50)) is None
    sent = await pipeline.digest_tick(conn, tz=TZ, slots=slots, now=msk(13, 5))
    assert sent["digest_messages"] == 1
    digest, = await digests(conn)
    assert "Смету пришлю завтра".lower() in digest["payload"]["text"]
    assert await pipeline.digest_tick(conn, tz=TZ, slots=slots, now=msk(13, 40)) is None    # не чаще
    # новых нет — к 19:00 сводка пустая, сообщения нет
    assert (await pipeline.digest_tick(conn, tz=TZ, slots=slots, now=msk(19, 1)))["digest_messages"] == 0
    await say(conn, ivan, [(IVAN, "Иван Петров", "Акт подпишу в понедельник")], first_id=40)
    await pipeline.plan_run(conn, tz=TZ, trigger="hourly", now=datetime(2026, 10, 6, 17, 0, tzinfo=timezone.utc))
    await answer_all(conn)
    # пропущенная из-за простоя сводка уходит в пределах трёх часов, позже — нет; в тихие часы — никогда
    await notify_settings.save(conn, {**notify_settings.DEFAULTS, "quiet": True, "quiet_from": "21:00",
                                      "quiet_to": "08:00"})
    next_day = lambda h, m=0: msk(h, m) + timedelta(days=1)   # noqa: E731
    assert await pipeline.digest_tick(conn, tz=TZ, slots=slots, now=next_day(7, 30)) is None
    assert await pipeline.digest_tick(conn, tz=TZ, slots=slots, now=next_day(16, 30)) is None
    sent = await pipeline.digest_tick(conn, tz=TZ, slots=slots, now=next_day(19, 30))
    assert sent["digest_messages"] == 1
