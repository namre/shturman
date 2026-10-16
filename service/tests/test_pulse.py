"""Внешний сторож (Р-98): пульс наружу с итогом проверок и итог последней утренней сводки."""

import asyncio
import dataclasses
from datetime import datetime, timedelta, timezone

import httpx

from shturman import pulse, summaries

URL = "https://hc-ping.com/0f0e0d0c-aaaa-bbbb-cccc-1234567890ab"
NOW = datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc)         # 12:00 по Москве


def test_url_forms():
    assert pulse.parse_url(URL) == (URL, URL + "/fail")
    assert pulse.parse_url(URL + "/") == (URL, URL + "/fail")
    assert pulse.parse_url("https://uptime.betterstack.com/api/v1/heartbeat/abc") == \
        ("https://uptime.betterstack.com/api/v1/heartbeat/abc", "https://uptime.betterstack.com/api/v1/heartbeat/abc/fail")
    for bad in ("http://hc-ping.com/x", "hc-ping.com/x", "https://user:pw@hc-ping.com/x", "", "ftp://x/y",
                "https://hc-ping.com/x#frag"):
        assert pulse.parse_url(bad) is None, bad
    p = pulse.Pulse(url="http://hc-ping.com/x")
    assert p.enabled is False and p.problem == "bad_url"
    assert pulse.Pulse(url=URL).public()["host"] == "hc-ping.com"
    assert "0f0e0d0c" not in str(pulse.Pulse(url=URL).public())       # адрес целиком наружу не отдаётся


def _report(*, last="delivered", ran="2026-10-16T05:27:10+00:00", reported=NOW - timedelta(minutes=20),
            enabled=True, time="08:27", **job):
    return {"reported_at": reported.isoformat(), "timezone": "Europe/Moscow",
            "jobs": {"morning-brief": {"exists": True, "enabled": enabled, "time": time, "last": last,
                                       "last_run_at": ran, **job}}}


def test_morning_problem():
    # ушла сегодня или промолчала — порядок
    assert summaries.morning_problem(_report(), now=NOW) is None
    assert summaries.morning_problem(_report(last="silent"), now=NOW) is None
    # упала или не доставлена
    assert summaries.morning_problem(_report(last="failed"), now=NOW) == "summary_failed"
    assert summaries.morning_problem(_report(last="delivery_failed"), now=NOW) == "summary_failed"
    # сегодня не запускалась: последний запуск — вчера
    assert summaries.morning_problem(_report(ran="2026-10-15T05:27:10+00:00"), now=NOW) == "summary_missed"
    assert summaries.morning_problem(_report(last="never", ran=None), now=NOW) == "summary_missed"
    # время ещё не наступило или плагин с тех пор не отвечал — судить рано
    early = datetime(2026, 10, 16, 6, 0, tzinfo=timezone.utc)                 # 09:00 по Москве
    assert summaries.morning_problem(_report(ran="2026-10-15T05:27:10+00:00",
                                             reported=early - timedelta(minutes=5)), now=early) is None
    # сводка выключена владельцем, Hermes нет — проверять нечего
    assert summaries.morning_problem(_report(enabled=False, last="failed"), now=NOW) is None
    assert summaries.morning_problem(None, now=NOW) is None
    assert summaries.morning_problem(_report(reported=NOW - timedelta(days=20)), now=NOW) is None
    # плагин прежней версии итога не сообщает — ложной тревоги нет
    old = _report()
    del old["jobs"]["morning-brief"]["last"]
    old["jobs"]["morning-brief"].pop("last_run_at")
    assert summaries.morning_problem(old, now=NOW) is None
    # плагин давно молчит — шлюз Hermes стоит
    assert summaries.morning_problem(_report(reported=NOW - timedelta(hours=4)), now=NOW) == "assistant_silent"


def test_report_keeps_only_known_last_codes():
    cleaned = summaries._clean_report({"jobs": {"morning-brief": {
        "exists": True, "enabled": True, "last": "delivered", "last_run_at": "2026-10-16T05:27:10+00:00",
        "output": "текст сводки"}, "weekly-review": {"exists": True, "enabled": True, "last": "что-то"}}})
    assert cleaned["jobs"]["morning-brief"] == {"exists": True, "enabled": True, "last": "delivered",
                                                "last_run_at": "2026-10-16T05:27:10+00:00"}
    assert "last" not in cleaned["jobs"]["weekly-review"]


class FakeState:
    def __init__(self, ro_pool, config, extras=None):
        self.ro_pool, self.config, self.extras = ro_pool, config, extras or {}


class Hits:
    def __init__(self, status=200):
        self.requests: list[httpx.Request] = []
        self.status = status

    def transport(self):
        def handle(request):
            self.requests.append(request)
            return httpx.Response(self.status, text="OK")
        return httpx.MockTransport(handle)


async def test_beat_sends_ok_or_fail_with_codes(conn, config):
    import asyncpg

    from conftest import DSN
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    try:
        state = FakeState(pool, config)
        hits = Hits()
        p = pulse.Pulse(url=URL)
        async with httpx.AsyncClient(transport=hits.transport()) as client:
            assert await pulse.beat(state, p, client, now=NOW) is True
            assert str(hits.requests[-1].url) == URL and hits.requests[-1].content == b"ok"
            assert p.public()["last_sent"] == "ok" and p.public()["last_delivered"] is True
            # сессия Telegram слетела и утренняя сводка упала — /fail с кодами, без имён
            from shturman.tg import health
            watcher = health.Watcher(type("M", (), {"configured": True})(), pool)
            watcher.last = [{"role": "owner", "state": "relogin", "for_s": 10, "update_age_s": None}]
            state.extras["tg_health"] = watcher
            await summaries._put(conn, summaries.ACTUAL, _report(last="failed"))
            assert await pulse.beat(state, p, client, now=NOW) is True
            assert str(hits.requests[-1].url) == URL + "/fail"
            assert hits.requests[-1].content.decode() == "telegram:relogin summary:failed"
            assert p.public()["failing"] == ["telegram:relogin", "summary:failed"]
        # служба не приняла — видно в состоянии, сервис не падает
        refused = Hits(status=404)
        async with httpx.AsyncClient(transport=refused.transport()) as client:
            assert await pulse.beat(state, p, client, now=NOW) is False
        assert p.public()["last_error"] == "http_404" and p.errors == 1
    finally:
        await pool.close()


async def test_service_beats_on_schedule_and_status_shows_it(make_client, config):
    hits = Hits()
    pulse.TEST_OVERRIDES.update(transport=hits.transport(), boot_delay=0)
    try:
        client, state = await make_client("shturman.api_core", "shturman.pulse",
                                          cfg=dataclasses.replace(config, pulse_url=URL, pulse_every=3600))
        for _ in range(100):
            if hits.requests:
                break
            await asyncio.sleep(0.02)
        assert hits.requests and str(hits.requests[0].url) == URL
        body = (await client.get("/api/status")).json()["pulse"]
        assert body["enabled"] is True and body["host"] == "hc-ping.com" and body["last_sent"] == "ok"
        assert URL not in (await client.get("/api/status")).text
    finally:
        pulse.TEST_OVERRIDES.clear()


async def test_bad_url_turns_pulse_off_without_stopping_the_service(make_client, config):
    client, state = await make_client("shturman.api_core", "shturman.pulse",
                                      cfg=dataclasses.replace(config, pulse_url="http://hc-ping.com/x"))
    body = (await client.get("/api/status")).json()["pulse"]
    assert body == {**body, "enabled": False, "configured": True, "problem": "bad_url", "host": None}
