"""Счётчики моста, задачи по расписанию и скиллы."""

import re
from pathlib import Path

import pytest

from shturman_core import bridge_stats, cron_jobs, tools
from shturman_core.bridge_stats import COUNTERS, Heartbeat, Stats

PLUGIN = Path(__file__).resolve().parents[1]


# --- счётчики ---

def test_status_is_numbers_only_and_shows_a_running_executor(store, clock):
    stats = Stats(now=clock)
    stats.bump("forwarded_messages", 3)
    stats.bump("dropped")
    stats.job_finished(True)
    stats.job_finished(False)
    stats.seen(True)
    stats.set_queue(2)
    assert Heartbeat(store, stats, now=clock).tick() is True
    status = bridge_stats.status(store, configured=True, now=clock)
    assert status["configured"] is True and status["executor_running"] is True and status["reachable"] is True
    assert status["last_job_at"] == int(clock()) and status["queue"] == 2
    assert status["counters"]["forwarded_messages"] == 3 and status["counters"]["dropped"] == 1
    assert status["counters"]["jobs_done"] == 1 and status["counters"]["jobs_failed"] == 1
    assert set(status["counters"]) == set(COUNTERS)

    def only_numbers(value):
        if isinstance(value, dict):
            return all(only_numbers(v) for v in value.values())
        return value is None or isinstance(value, (bool, int))

    assert only_numbers(status)


def test_stale_heartbeat_means_executor_is_not_running(store, clock):
    stats = Stats(now=clock)
    stats.seen(True)
    Heartbeat(store, stats, now=clock).tick()
    clock.tick(bridge_stats.STALE_AFTER + 1)
    status = bridge_stats.status(store, configured=True, now=clock)
    assert status["executor_running"] is False and status["reachable"] is None and status["checked_at"] is None
    # счётчики прошлого запуска остаются видны
    assert status["started_at"] == stats.started_at


def test_not_configured_or_never_started(store, clock):
    assert bridge_stats.status(store, configured=False, now=clock) == {
        "configured": False, "executor_running": False, "reachable": None, "checked_at": None,
        "started_at": None, "last_job_at": None, "queue": 0, "business_disabled": None, "sending": None,
        "own_bot": None, "counters": {name: 0 for name in COUNTERS}}
    Heartbeat(store, Stats(now=clock), now=clock).tick()
    assert bridge_stats.status(store, configured=False, now=clock)["executor_running"] is False


def test_heartbeat_writes_on_change_and_at_most_every_half_minute(store, clock):
    stats = Stats(now=clock)
    heartbeat = Heartbeat(store, stats, now=clock)
    assert heartbeat.tick() is True and heartbeat.tick() is False
    stats.bump("callbacks")
    assert heartbeat.tick() is True
    clock.tick(bridge_stats.HEARTBEAT_EVERY - 1)
    assert heartbeat.tick() is False
    clock.tick(2)
    assert heartbeat.tick() is True
    heartbeat.clear()
    assert bridge_stats.status(store, configured=True, now=clock)["executor_running"] is False


def test_garbage_in_state_file_is_harmless(store, clock):
    store.write(bridge_stats.STATE_NAME, {"heartbeat_at": "вчера", "counters": ["x"], "queue": True, "reachable": "да"})
    status = bridge_stats.status(store, configured=True, now=clock)
    assert status["executor_running"] is False and status["queue"] == 0 and status["counters"]["dropped"] == 0


# --- задачи по расписанию ---

def test_default_schedules():
    assert cron_jobs.schedule_for("morning-brief") == "27 8 * * *"
    assert cron_jobs.schedule_for("weekly-review") == "47 17 * * 5"
    assert cron_jobs.schedule_for("morning-brief", "7:05") == "5 7 * * *"
    assert cron_jobs.schedule_for("weekly-review", "09:00", weekday=1) == "0 9 * * 1"


@pytest.mark.parametrize("at", ["25:00", "8", "08:60", "8:5", "утром", "08:27; rm -rf /", "* * * * *"])
def test_bad_time_is_rejected(at):
    with pytest.raises(ValueError):
        cron_jobs.schedule_for("morning-brief", at)


@pytest.mark.parametrize("weekday", [7, -1, True, "5"])
def test_bad_weekday_is_rejected(weekday):
    with pytest.raises(ValueError):
        cron_jobs.schedule_for("weekly-review", weekday=weekday)


def test_plan_creates_both_jobs_once():
    create, present = cron_jobs.plan([])
    assert present == [] and [spec["name"] for spec in create] == ["shturman:morning-brief", "shturman:weekly-review"]
    brief = create[0]
    assert brief["schedule"] == "27 8 * * *" and brief["deliver"] == "telegram"
    assert brief["skills"] == ["shturman:morning-brief"]
    # Прогон читает чужой текст: только архив и чтение обязательств — ни терминала, ни веба, ни правок.
    assert brief["enabled_toolsets"] == ["shturman", tools.TOOLSET_READ]
    existing = [{"id": "a1", "name": "shturman:morning-brief", "enabled": False, "schedule_display": "0 7 * * *"},
                {"id": "zz", "name": "чужая задача"}]
    create, present = cron_jobs.plan(existing, times={"weekly-review": "18:00"})
    assert [spec["name"] for spec in create] == ["shturman:weekly-review"] and create[0]["schedule"] == "0 18 * * 5"
    assert present == [{"key": "morning-brief", "name": "shturman:morning-brief", "title": "Утренняя сводка",
                        "id": "a1", "enabled": False, "schedule": "0 7 * * *"}]
    both = existing + [{"id": "b2", "name": "shturman:weekly-review"}]
    assert cron_jobs.plan(both)[0] == []


# --- часовой пояс шлюза (Р-90) ---

@pytest.mark.parametrize("want, have, state", [
    ("", "", "none"), (None, "Europe/Moscow", "none"), ("  ", "UTC", "none"),
    ("Europe/Moscow", "", "missing"), ("Europe/Moscow", None, "missing"),
    ("Europe/Moscow", "Europe/Moscow", "same"), (" Europe/Moscow ", "Europe/Moscow", "same"),
    ("Europe/Moscow", "UTC", "differs"),
    ("Mars/Base", "", "invalid"), ("../../etc/passwd", "UTC", "invalid"),
])
def test_zone_state(want, have, state):
    assert cron_jobs.zone_state(want, have) == state


def test_config_zone_and_expected_zone():
    assert cron_jobs.config_zone({"timezone": " Asia/Novosibirsk "}) == "Asia/Novosibirsk"
    assert cron_jobs.config_zone({"timezone": 3}) == "" and cron_jobs.config_zone(None) == ""
    assert cron_jobs.expected_zone("Europe/Moscow", "UTC") == "Europe/Moscow"
    assert cron_jobs.expected_zone("Mars/Base", "UTC") == "UTC"            # неизвестный — берём пояс шлюза
    assert cron_jobs.expected_zone("", "") == ""


def _job(next_run_at, expr="27 8 * * *", **extra):
    return {"id": "a1", "name": "shturman:morning-brief", "enabled": True,
            "schedule": {"kind": "cron", "expr": expr}, "next_run_at": next_run_at, **extra}


def test_schedule_drift_catches_the_pilot_case():
    """Пилот 2026-10-15: шлюз пересчитал сводку по UTC — 08:27+00:00, то есть 11:27 по Москве."""
    assert cron_jobs.schedule_drift(_job("2026-10-16T08:27:00+00:00"), "Europe/Moscow") == "offset"
    assert cron_jobs.schedule_drift(_job("2026-10-16T08:27:00+03:00"), "Europe/Moscow") == "ok"
    # Без пояса шлюз считает по часам сервера (в контейнере — UTC): тогда та же запись «верна».
    assert cron_jobs.schedule_drift(_job("2026-10-16T08:27:00+00:00"), "") == "ok"
    assert cron_jobs.schedule_drift(_job("2026-10-16T09:27:00+03:00"), "Europe/Moscow") == "off-schedule"


def test_schedule_drift_weekly_and_edge_cases():
    friday = "2026-10-16T17:47:00+03:00"                        # 16.10.2026 — пятница
    assert cron_jobs.schedule_drift(_job(friday, "47 17 * * 5"), "Europe/Moscow") == "ok"
    assert cron_jobs.schedule_drift(_job(friday, "47 17 * * 1"), "Europe/Moscow") == "off-schedule"
    assert cron_jobs.schedule_drift(_job("2026-10-18T17:47:00+03:00", "47 17 * * 7"), "Europe/Moscow") == "ok"
    assert cron_jobs.schedule_drift(_job(friday, "47 17 * * 1-5"), "Europe/Moscow") == "ok"   # диапазон не проверяем
    assert cron_jobs.schedule_drift(_job(friday, "*/5 17 * * *"), "Europe/Moscow") == "ok"
    assert cron_jobs.schedule_drift(_job(None), "Europe/Moscow") == "no-next"
    assert cron_jobs.schedule_drift(_job(None, enabled=False), "Europe/Moscow") == "paused"
    assert cron_jobs.schedule_drift(_job(None, state="paused"), "Europe/Moscow") == "paused"
    assert cron_jobs.schedule_drift(_job("вчера"), "Europe/Moscow") == "unchecked"
    assert cron_jobs.schedule_drift(_job("2026-10-16T08:27:00"), "Europe/Moscow") == "unchecked"
    assert cron_jobs.schedule_drift({"enabled": True, "schedule": {"kind": "interval", "minutes": 5},
                                     "next_run_at": friday}, "Europe/Moscow") == "unchecked"


def test_local_run_and_ours():
    assert cron_jobs.local_run("2026-10-16T08:27:00+00:00", "Europe/Moscow") == "пт 16.10 11:27"
    assert cron_jobs.local_run("2026-10-16T08:27:00+03:00", "Europe/Moscow") == "пт 16.10 08:27"
    assert cron_jobs.local_run(None, "Europe/Moscow") == "" and cron_jobs.local_run("x", "UTC") == ""
    jobs = [{"name": "shturman:morning-brief"}, {"name": "чужая"}, {"name": None}, "мусор"]
    assert [j["name"] for j in cron_jobs.ours(jobs)] == ["shturman:morning-brief"]


def test_zone_warning_and_restart_pending():
    assert cron_jobs.zone_warning("same", "Europe/Moscow", "Europe/Moscow") is None
    assert cron_jobs.zone_warning("none", "", "Europe/Moscow") is None
    assert "UTC" in cron_jobs.zone_warning("none", "", "")
    assert "./ops/up.sh" in cron_jobs.zone_warning("missing", "Europe/Moscow", "")
    assert "часам сервера" in cron_jobs.zone_warning("missing", "Europe/Moscow", "")
    assert "поясу UTC" in cron_jobs.zone_warning("differs", "Europe/Moscow", "UTC")
    assert "перезапуск" in cron_jobs.zone_warning("written", "Europe/Moscow", "Europe/Moscow")
    assert "Mars/Base" in cron_jobs.zone_warning("invalid", "Mars/Base", "")
    assert cron_jobs.restart_pending(200.0, 100.0) is True
    assert cron_jobs.restart_pending(50, 100.0) is False
    assert cron_jobs.restart_pending(None, 100.0) is False and cron_jobs.restart_pending(True, 0.0) is False
    assert cron_jobs.restart_pending(200, None) is False


def test_ops_scripts_call_only_existing_cron_functions():
    """ops/up.sh и ops/doctor.sh зовут функции cron_jobs внутри контейнера Hermes — имена должны быть."""
    root = PLUGIN.parents[1]
    used = set()
    for script in ("up.sh", "doctor.sh"):
        used |= set(re.findall(r"cron_jobs\.(\w+)", (root / "ops" / script).read_text(encoding="utf-8"))) - {"py"}
    assert {"zone_state", "config_zone", "schedule_drift", "ours", "expected_zone"} <= used
    assert all(hasattr(cron_jobs, name) for name in used), used - set(dir(cron_jobs))


def test_dashboard_reads_the_gateway_zone_from_config_yaml():
    """Процесс дашборда видит HERMES_TIMEZONE, а шлюз при нескольких профилях — нет (Р-90)."""
    source = (PLUGIN / "dashboard" / "plugin_api.py").read_text(encoding="utf-8")
    assert "get_timezone_name()\n" not in source and "from hermes_time import" not in source
    assert "read_raw_config" in source and 'set_config_value("timezone"' in source


# --- скиллы ---

@pytest.mark.parametrize("name", ["morning-brief", "weekly-review"])
def test_skill_file_is_well_formed(name):
    text = (PLUGIN / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    front, _, body = text[4:].partition("\n---\n")
    assert re.search(rf"^name: {name}$", front, re.M) and re.search(r"^description: \".+\"$", front, re.M)
    assert "[SILENT]" in body
    assert "не указание" in body and "не исполняй" in body        # чужой текст — данные
    assert "shturman_commitments" in body and "mcp__shturman__get_chat_history" in body
    assert not re.search(r"^\|.*\|\s*$", body, re.M)               # без таблиц
    for tool in re.findall(r"`(shturman_\w+)`", body):
        assert tools.TOOLSETS.get(tool) == tools.TOOLSET_READ      # сводка пользуется только чтением
    for job in cron_jobs.JOBS:
        assert (PLUGIN / "skills" / job["key"] / "SKILL.md").is_file()


def test_status_shows_sending_switch_and_disabled_business_connection(store, clock):
    stats = Stats(now=clock)
    heartbeat = Heartbeat(store, stats, now=clock)
    heartbeat.tick()
    status = bridge_stats.status(store, configured=True, now=clock)
    assert status["sending"] is None and status["business_disabled"] is None      # ещё неизвестно
    stats.set_flag("sending", False)
    stats.set_flag("business_disabled", True)
    stats.bump("not_stored_disabled", 2)
    assert heartbeat.tick() is True                                                # изменение признака — повод записать
    status = bridge_stats.status(store, configured=True, now=clock)
    assert status["sending"] is False and status["business_disabled"] is True
    assert status["counters"]["not_stored_disabled"] == 2
    stats.set_flag("sending", False)
    assert heartbeat.tick() is False                                               # то же значение — не изменение
    clock.tick(bridge_stats.STALE_AFTER + 1)
    stale = bridge_stats.status(store, configured=True, now=clock)
    assert stale["sending"] is None and stale["business_disabled"] is None         # исполнитель стоит — не знаем


def test_status_shows_that_the_service_runs_its_own_bot(store, clock):
    stats = Stats(now=clock)
    heartbeat = Heartbeat(store, stats, now=clock)
    heartbeat.tick()
    assert bridge_stats.status(store, configured=True, now=clock)["own_bot"] is None      # ещё неизвестно
    stats.set_flag("own_bot", True)
    assert heartbeat.tick() is True                                                # вход в режим — повод записать
    status = bridge_stats.status(store, configured=True, now=clock)
    assert status["own_bot"] is True and store.read("bridge")["own_bot"] is True
    assert status["counters"] == {name: 0 for name in COUNTERS}                    # прежние поля на месте
    stats.set_flag("own_bot", False)
    heartbeat.tick()
    assert bridge_stats.status(store, configured=True, now=clock)["own_bot"] is False
    clock.tick(bridge_stats.STALE_AFTER + 1)
    assert bridge_stats.status(store, configured=True, now=clock)["own_bot"] is None      # исполнитель стоит


def test_state_written_before_the_field_existed_reads_as_unknown(store, clock):
    store.write("bridge", {"heartbeat_at": int(clock()), "counters": {}})
    assert bridge_stats.status(store, configured=True, now=clock)["own_bot"] is None


# --- чтение состояния: «файла нет» и «не удалось прочитать» — разные ответы ---

def test_strict_read_tells_absent_from_unreadable(store):
    from shturman_core.state import StoreReadError

    with pytest.raises(StoreReadError):
        store.read_strict("owner")                       # каталога состояния ещё нет: судить не о чем
    store.write("wizard", {})
    assert store.read_strict("owner") is None            # каталог есть, файла нет — владельца точно нет
    store.write("owner", {"user_id": 42, "chat_id": 42})
    assert store.read_strict("owner") == {"user_id": 42, "chat_id": 42}
    (store.root / "owner.json").write_text("{ оборванная запись", encoding="utf-8")
    with pytest.raises(StoreReadError):
        store.read_strict("owner")
    assert store.read("owner") == {}                     # прежнее чтение по-прежнему молчит
    (store.root / "owner.json").write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(StoreReadError):
        store.read_strict("owner")
    (store.root / "owner.json").unlink()
    (store.root / "owner.json").mkdir()                  # ошибка чтения, не «файла нет»
    with pytest.raises(StoreReadError):
        store.read_strict("owner")


def test_recovery_link_leaves_a_mark_for_the_gateway(store, clock):
    from shturman_core.auth import Auth
    from shturman_core.state import OWNER_UNBOUND

    store.write("owner", {"user_id": 42, "chat_id": 42, "name": "Иван"})
    auth = Auth(store, now=clock)
    assert store.read_strict(OWNER_UNBOUND) is None
    assert auth.redeem_activation(auth.issue_activation()) is True
    assert auth.owner() is None
    assert store.read_strict(OWNER_UNBOUND) == {"at": int(clock())}


def test_first_activation_leaves_no_mark(store, clock):
    from shturman_core.auth import Auth
    from shturman_core.state import OWNER_UNBOUND

    auth = Auth(store, now=clock)
    assert auth.redeem_activation(auth.issue_activation()) is True
    assert store.read_strict(OWNER_UNBOUND) is None


# --- длина по счёту Telegram и сверка со схемой ---

def test_utf16_length_and_cut():
    from shturman_core.textlimits import cut_utf16, utf16_len

    assert utf16_len("привет") == 6 and utf16_len("😀") == 2 and utf16_len("a😀б") == 4
    assert cut_utf16("x" * 4096) == "x" * 4096
    long = cut_utf16("x" * 5000)
    assert utf16_len(long) == 4096 and long.endswith("…")
    emoji = cut_utf16("😀" * 3000)                        # 3000 знаков Python, но 6000 единиц Telegram
    assert utf16_len(emoji) <= 4096 and emoji.endswith("…")
    assert emoji[:-1] == "😀" * (len(emoji) - 1)           # знак не разорван пополам
    assert cut_utf16("😀" * 2048) == "😀" * 2048           # ровно 4096 единиц — помещается
    assert utf16_len(cut_utf16("a" + "😀" * 2048)) <= 4096


def test_schema_check_covers_what_service_schemas_use():
    from shturman_core.textlimits import matches_schema

    schema = {"type": "object", "additionalProperties": False, "required": ["commitments"], "properties": {
        "commitments": {"type": "array", "items": {"type": "object", "required": ["message", "what"], "properties": {
            "message": {"type": "integer"}, "what": {"type": "string"}, "due": {"type": ["string", "null"]},
            "status": {"type": "string", "enum": ["a", "b"]}}}}}}
    ok = {"commitments": [{"message": 1, "what": "смета", "due": None, "status": "a"}]}
    assert matches_schema(ok, schema) and matches_schema({"commitments": []}, schema)
    for bad in ({}, [], {"commitments": {}}, {"commitments": [{"what": "x"}]}, {"commitments": [], "extra": 1},
                {"commitments": [{"message": "1", "what": "x"}]}, {"commitments": [{"message": True, "what": "x"}]},
                {"commitments": [{"message": 1, "what": "x", "due": 5}]},
                {"commitments": [{"message": 1, "what": "x", "status": "c"}]}):
        assert not matches_schema(bad, schema), bad
    assert matches_schema({"что угодно": 1}, None) and matches_schema(1.5, {"type": "number"})


def test_cli_status_tells_doctor_whether_the_bridge_is_in_own_bot_mode(store):
    """`cli.py status` читает ops/doctor.sh: одна строка о том, что мост знает о своём боте сервиса."""
    import cli
    from shturman_core.auth import Auth

    def own_bot_line() -> str:
        lines = cli.status_lines(store, Auth(store))
        assert all(re.fullmatch(r"[a-z_]+=(yes|no|unknown)", line) for line in lines)     # только признаки
        return [line for line in lines if line.startswith("bridge_own_bot=")][0]

    assert own_bot_line() == "bridge_own_bot=unknown"              # шлюз ещё ничего не записал
    stats = Stats()
    heartbeat = Heartbeat(store, stats)
    stats.set_flag("own_bot", True)
    heartbeat.tick()
    assert own_bot_line() == "bridge_own_bot=yes"
    stats.set_flag("own_bot", False)
    heartbeat.tick()
    assert own_bot_line() == "bridge_own_bot=no"


def test_cli_status_tells_doctor_when_the_assistant_bot_is_connected_in_business_mode(store):
    """Схема до 0.0.6: бот-ассистент подключён в бизнес-режиме. ops/doctor.sh сообщает об этом
    владельцу и ничего не трогает; по умолчанию признак — «нет»."""
    import cli
    from shturman_core.auth import Auth

    def line() -> str:
        return [x for x in cli.status_lines(store, Auth(store)) if x.startswith("hermes_business=")][0]

    assert line() == "hermes_business=no"
    store.write("business", {"connected": True, "can_reply": False, "updated_at": 1})
    assert line() == "hermes_business=yes"
    store.write("business", {"connected": False, "updated_at": 2})
    assert line() == "hermes_business=no"
    store.write("business", {"connected": "yes"})            # не признак — не «да»
    assert line() == "hermes_business=no"


# --- область секретов профиля Hermes для вызова модели (Р-83) ---

def _bridge(monkeypatch):
    import shturman_bridge

    monkeypatch.setattr(shturman_bridge, "_scope_factory", None)
    return shturman_bridge


def test_llm_scope_is_hermes_launch_profile_scope(monkeypatch):
    import asyncio
    import contextlib
    import sys
    import types

    entered = []

    @contextlib.asynccontextmanager
    async def launch():
        entered.append(True)
        yield

    fake = types.ModuleType("tui_gateway.launch_profile_policy")
    fake.async_launch_profile_scope_if_multiplexed = launch
    monkeypatch.setitem(sys.modules, "tui_gateway", types.ModuleType("tui_gateway"))
    monkeypatch.setitem(sys.modules, "tui_gateway.launch_profile_policy", fake)
    bridge = _bridge(monkeypatch)

    async def use():
        async with bridge.hermes_llm_scope():
            return "ok"
    assert asyncio.run(use()) == "ok" and asyncio.run(use()) == "ok"
    assert entered == [True, True] and bridge._scope_factory is launch


def test_llm_scope_without_that_hermes_function_is_empty_and_said_once(monkeypatch, caplog):
    import asyncio
    import logging
    import sys

    monkeypatch.setitem(sys.modules, "tui_gateway.launch_profile_policy", None)   # импорт не удастся
    bridge = _bridge(monkeypatch)

    async def use():
        async with bridge.hermes_llm_scope():
            return "ok"
    with caplog.at_level(logging.WARNING, logger="shturman.bridge"):
        assert asyncio.run(use()) == "ok" and asyncio.run(use()) == "ok"
    assert sum("области секретов профиля" in r.getMessage() for r in caplog.records) == 1


def test_runtime_hands_the_scope_to_the_executor(monkeypatch, tmp_path):
    import asyncio

    bridge = _bridge(monkeypatch)
    monkeypatch.setenv("SHTURMAN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SHTURMAN_SERVICE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("SHTURMAN_API_TOKEN", "t" * 40)
    runtime = bridge.Runtime()
    runtime.application = object()
    seen = {}

    async def start():
        assert runtime.ensure_started()
        seen["scope"] = runtime.executor._llm_scope
        await runtime.stop()
    asyncio.run(start())
    assert seen["scope"] is bridge.hermes_llm_scope
