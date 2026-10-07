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
