"""Расписание сводок по выбору владельца (Р-95): что плагин делает с задачами Hermes и что сообщает сервису.

Без Hermes: план и отчёт — `shturman_core.summaries`, задание `hermes.cron` — исполнитель с подставной
функцией расписания, а `shturman_bridge.cron_sync` — с подставными модулями Hermes (`cron.jobs`,
`tools.cronjob_tools`, `hermes_cli.config`, `cron.scheduler`), как их вызывает шлюз.
"""

import asyncio
import json
import sys
import types

import pytest

from shturman_core import summaries
from shturman_core.executor import HERMES_CRON, Executor

MORNING = {"id": "m1", "name": "shturman:morning-brief", "enabled": True, "state": "scheduled",
           "schedule": {"kind": "cron", "expr": "27 8 * * *"}, "next_run_at": "2026-10-16T08:27:00+03:00"}
WEEKLY = {"id": "w1", "name": "shturman:weekly-review", "enabled": True, "state": "scheduled",
          "schedule": {"kind": "cron", "expr": "47 17 * * 5"}, "next_run_at": "2026-10-16T17:47:00+03:00"}
OTHER = {"id": "x1", "name": "chores", "enabled": True, "schedule": {"kind": "cron", "expr": "0 9 * * *"}}


def want(morning=("08:27", True), weekly=("17:47", 5, True)):
    return {"morning-brief": {"enabled": morning[1], "time": morning[0]},
            "weekly-review": {"enabled": weekly[2], "time": weekly[0], "day": weekly[1]}}


def test_same_choice_changes_nothing_and_report_reads_time_and_next_run():
    actions, error = summaries.plan(want(), [MORNING, WEEKLY, OTHER])
    assert actions == [] and error is None
    out = summaries.report([MORNING, WEEKLY, OTHER], "Europe/Moscow")
    assert out["timezone"] == "Europe/Moscow"
    assert out["jobs"]["morning-brief"] == {"exists": True, "enabled": True, "expr": "27 8 * * *", "time": "08:27",
                                            "next_run_at": "2026-10-16T08:27:00+03:00", "next_local": "пт 16.10 08:27"}
    assert out["jobs"]["weekly-review"]["day"] == 5 and out["jobs"]["weekly-review"]["time"] == "17:47"
    assert "prompt" not in json.dumps(out)


def test_new_time_is_a_schedule_edit_and_off_is_a_pause_others_untouched():
    actions, _ = summaries.plan(want(morning=("07:05", True), weekly=("17:47", 5, False)), [MORNING, WEEKLY, OTHER])
    assert actions == [{"op": "schedule", "key": "morning-brief", "id": "m1", "expr": "5 7 * * *"},
                       {"op": "pause", "key": "weekly-review", "id": "w1"}]
    paused = {**WEEKLY, "enabled": False, "state": "paused"}
    actions, _ = summaries.plan(want(weekly=("10:00", 1, True)), [MORNING, paused])
    assert actions == [{"op": "schedule", "key": "weekly-review", "id": "w1", "expr": "0 10 * * 1"},
                       {"op": "resume", "key": "weekly-review", "id": "w1"}]
    assert summaries.report([paused], None)["jobs"]["weekly-review"]["enabled"] is False
    assert "next_local" not in summaries.report([paused], None)["jobs"]["weekly-review"]


def test_missing_job_is_created_only_when_wanted_and_deliverable():
    actions, error = summaries.plan(want(weekly=("17:47", 5, False)), [])
    assert [a["op"] for a in actions] == ["create"] and error is None
    spec = actions[0]["spec"]
    assert spec["name"] == "shturman:morning-brief" and spec["schedule"] == "27 8 * * *"
    assert spec["enabled_toolsets"] == ["shturman", "shturman_read"]          # только чтение архива
    actions, error = summaries.plan(want(), [], can_create=False)
    assert actions == [] and error == "no_home_channel"
    assert summaries.report([], None)["jobs"] == {"morning-brief": {"exists": False}, "weekly-review": {"exists": False}}


@pytest.mark.parametrize("bad", [{"enabled": "yes", "time": "08:00"}, {"enabled": True, "time": "8:00"},
                                 {"enabled": True, "time": "25:00"}, None])
def test_malformed_choice_is_ignored(bad):
    assert summaries.plan({"morning-brief": bad}, [MORNING]) == ([], None)


class Service:
    def __init__(self, jobs):
        self.jobs, self.reports = list(jobs), []

    async def __call__(self, method, path, json_body=None, *, timeout=None):
        if path == "/api/jobs/claim":
            taken = [j for j in self.jobs if j["kind"] in json_body["kinds"]]
            self.jobs = [j for j in self.jobs if j not in taken]
            return {"jobs": taken}
        self.reports.append((path, json_body))
        return {"ok": True}


def test_executor_has_a_hermes_lane_only_with_the_bridge_function():
    calls = []

    async def cron(payload):
        calls.append(payload)
        return {"jobs": {}, "applied": True, "revision": payload["revision"]}

    service = Service([{"id": 5, "kind": HERMES_CRON, "payload": {"apply": True, "revision": 3, "desired": want()}}])
    plain = Executor(service, llm=None, bot=None, owner=lambda: {})
    assert plain.kinds("hermes") == ()
    executor = Executor(service, llm=None, bot=None, owner=lambda: {}, cron=cron)
    assert executor.kinds("hermes") == (HERMES_CRON,)
    assert asyncio.run(executor.poll("hermes")) == 1
    assert calls[0]["revision"] == 3
    assert service.reports == [("/api/jobs/5/complete", {"result": {"jobs": {}, "applied": True, "revision": 3}})]


@pytest.fixture
def fake_hermes(monkeypatch):
    """Подставные модули Hermes 0.21.5 с теми именами, что зовёт мост."""
    state = {"jobs": [dict(MORNING), dict(WEEKLY)], "calls": [], "home": "42", "zone": "Europe/Moscow"}

    def list_jobs(include_disabled=False):
        return [dict(j) for j in state["jobs"]]

    def cronjob(action, job_id=None, schedule=None, reason=None, **_):
        state["calls"].append((action, job_id, schedule))
        for job in state["jobs"]:
            if job["id"] == job_id:
                if action == "update":
                    job["schedule"] = {"kind": "cron", "expr": schedule}
                elif action == "pause":
                    job.update(enabled=False, state="paused")
                elif action == "resume":
                    job.update(enabled=True, state="scheduled")
        return json.dumps({"success": True})

    def create(**spec):
        state["calls"].append(("create", spec["name"], spec["schedule"]))
        state["jobs"].append({"id": "new", "name": spec["name"], "enabled": True, "state": "scheduled",
                              "schedule": {"kind": "cron", "expr": spec["schedule"]}})
        return state["jobs"][-1]

    modules = {
        "cron": types.ModuleType("cron"), "cron.jobs": types.SimpleNamespace(list_jobs=list_jobs),
        "cron.scheduler": types.SimpleNamespace(create_job_with_scheduler_registration=create),
        "tools": types.ModuleType("tools"), "tools.cronjob_tools": types.SimpleNamespace(cronjob=cronjob),
        "hermes_cli": types.ModuleType("hermes_cli"),
        "hermes_cli.config": types.SimpleNamespace(
            read_raw_config=lambda: {"timezone": state["zone"]},
            get_env_value_prefer_dotenv=lambda name: state["home"] if name == "TELEGRAM_HOME_CHANNEL" else ""),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return state


def test_bridge_applies_with_hermes_functions_and_reports_what_is_there(fake_hermes):
    import shturman_bridge
    assert shturman_bridge.cron_available()
    out = shturman_bridge.cron_sync({"apply": True, "revision": 7,
                                     "desired": want(morning=("06:30", True), weekly=("17:47", 5, False))})
    assert fake_hermes["calls"] == [("update", "m1", "30 6 * * *"), ("pause", "w1", None)]
    assert out["applied"] is True and out["revision"] == 7 and "error" not in out
    assert out["jobs"]["morning-brief"]["time"] == "06:30" and out["jobs"]["weekly-review"]["enabled"] is False
    assert out["timezone"] == "Europe/Moscow"
    # «только посмотреть» ничего не трогает
    fake_hermes["calls"].clear()
    look = shturman_bridge.cron_sync({"apply": False, "revision": 7, "desired": want()})
    assert fake_hermes["calls"] == [] and look["applied"] is False


def test_bridge_does_not_create_without_a_home_channel(fake_hermes):
    import shturman_bridge
    fake_hermes["jobs"].clear()
    fake_hermes["home"] = ""
    out = shturman_bridge.cron_sync({"apply": True, "revision": 1, "desired": want()})
    assert fake_hermes["calls"] == [] and out["error"] == "no_home_channel" and out["applied"] is False
    fake_hermes["home"] = "42"
    out = shturman_bridge.cron_sync({"apply": True, "revision": 2, "desired": want()})
    assert [c[0] for c in fake_hermes["calls"]] == ["create", "create"] and out["applied"] is True
    assert out["jobs"]["morning-brief"]["exists"] is True
