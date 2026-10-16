"""Расписание утренней сводки и обзора недели по выбору владельца (решение Р-95). Без зависимостей от Hermes.

Владелец выбирает в «Настройках Штурмана» или в приложении «Штурман»: сводка вкл/выкл и время, обзор
недели вкл/выкл, день и время. Сервис переписки хранит выбор и ставит задание `hermes.cron` в общую
очередь; исполнитель плагина (`executor.py`) забирает его в процессе шлюза Hermes и приводит задачи
`shturman:morning-brief` и `shturman:weekly-review` к выбранному штатными функциями Hermes — создать
(`create_job_with_scheduler_registration`, как `cron/install`), сменить расписание (`cronjob(action="update",
schedule=…)` — то же, что `hermes cron edit --schedule`), поставить на паузу и снять с неё
(`cronjob(action="pause"|"resume")`). Затем отвечает, что в Hermes на самом деле: задача есть, включена,
время, следующий запуск по поясу шлюза (Р-90). Здесь — расчёт «что сделать» и «что сообщить».

Правила:
  * задачи нет, а сводка нужна — создать с выбранным временем (если некуда доставлять — не создавать,
    сообщить `no_home_channel`); задачи нет и сводка не нужна — ничего;
  * задача есть — расписание привести к выбранному, даже если сводка выключена (время запомнится до
    включения); выключить — пауза, включить — снять с паузы. Чужие задачи и задачи без имени
    `shturman:` не трогаются никогда;
  * «только посмотреть» (`apply: false`) — ничего не меняет.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from . import cron_jobs

KIND = "hermes.cron"
KEYS = ("morning-brief", "weekly-review")
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
PAUSE_REASON = "Штурман: сводка выключена владельцем в «Настройках Штурмана»"


def _by_key(jobs: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    names = {job["name"]: job["key"] for job in cron_jobs.JOBS}
    out: dict[str, Mapping[str, Any]] = {}
    for job in jobs:
        if isinstance(job, Mapping) and job.get("name") in names:
            out.setdefault(names[job["name"]], job)
    return out


def expr_of(job: Mapping[str, Any]) -> str | None:
    schedule = job.get("schedule")
    if isinstance(schedule, Mapping) and schedule.get("kind") == "cron" and isinstance(schedule.get("expr"), str):
        return schedule["expr"].strip()
    return None


def parse_expr(expr: str | None) -> tuple[str | None, int | None]:
    """«27 8 * * *» → ("08:27", None); «47 17 * * 5» → ("17:47", 5). Сложнее числа — (None, None)."""
    fields = (expr or "").split()
    if len(fields) != 5 or not fields[0].isdigit() or not fields[1].isdigit():
        return None, None
    minute, hour = int(fields[0]), int(fields[1])
    if not (0 <= minute <= 59 and 0 <= hour <= 23):
        return None, None
    day = int(fields[4]) % 7 if fields[4].isdigit() and int(fields[4]) <= 7 else None
    return f"{hour:02d}:{minute:02d}", day


def active(job: Mapping[str, Any]) -> bool:
    return bool(job.get("enabled", True)) and job.get("state") != "paused"


def _want(item: Any, key: str) -> dict[str, Any] | None:
    if not isinstance(item, Mapping) or not isinstance(item.get("enabled"), bool):
        return None
    time = item.get("time")
    if not isinstance(time, str) or not _TIME.match(time):
        return None
    out = {"enabled": item["enabled"], "time": time}
    if key == "weekly-review":
        day = item.get("day")
        if isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6:
            return None
        out["day"] = day
    return out


def plan(desired: Mapping[str, Any], jobs: Iterable[Mapping[str, Any]], *,
         can_create: bool = True) -> tuple[list[dict[str, Any]], str | None]:
    """Что сделать, чтобы задачи совпали с выбором. Возвращает (действия, ошибка-код или None)."""
    found = _by_key(jobs)
    specs = {job["key"]: job for job in cron_jobs.JOBS}
    actions: list[dict[str, Any]] = []
    error = None
    for key in KEYS:
        want = _want(desired.get(key), key)
        if want is None:
            continue
        expr = cron_jobs.schedule_for(key, want["time"], weekday=want.get("day", cron_jobs.WEEKLY_DAY))
        job = found.get(key)
        if job is None:
            if not want["enabled"]:
                continue
            if not can_create:
                error = "no_home_channel"
                continue
            actions.append({"op": "create", "key": key,
                            "spec": cron_jobs.job_spec(specs[key], at=want["time"],
                                                       weekday=want.get("day", cron_jobs.WEEKLY_DAY))})
            continue
        job_id = job.get("id")
        if not isinstance(job_id, str) or not job_id:
            continue
        if expr_of(job) != expr:
            actions.append({"op": "schedule", "key": key, "id": job_id, "expr": expr})
        if want["enabled"] and not active(job):
            actions.append({"op": "resume", "key": key, "id": job_id})
        elif not want["enabled"] and active(job):
            actions.append({"op": "pause", "key": key, "id": job_id})
    return actions, error


def report(jobs: Iterable[Mapping[str, Any]], zone: str | None,
           executions: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Что сейчас в Hermes — для сервиса. Только имя задачи, расписание, время и код итога последнего
    запуска (Р-98, `cron_jobs.last_run`): ни подсказок, ни вывода, ни текста сводки.
    executions — последняя запись журнала запусков Hermes по номеру задачи, если журнал есть."""
    found = _by_key(jobs)
    out: dict[str, Any] = {}
    for key in KEYS:
        job = found.get(key)
        if job is None:
            out[key] = {"exists": False}
            continue
        expr = expr_of(job)
        time, day = parse_expr(expr)
        item: dict[str, Any] = {"exists": True, "enabled": active(job), "expr": expr}
        if time:
            item["time"] = time
        if key == "weekly-review" and day is not None:
            item["day"] = day
        nxt = job.get("next_run_at")
        if active(job) and nxt:
            item["next_run_at"] = str(nxt)[:64]
            item["next_local"] = cron_jobs.local_run(nxt, zone)
        execution = (executions or {}).get(str(job.get("id"))) if job.get("id") is not None else None
        item["last"] = cron_jobs.last_run(job, execution)
        ran = cron_jobs.last_run_at(job, execution)
        if ran:
            item["last_run_at"] = ran
        out[key] = item
    return {"jobs": out, "timezone": (zone or None)}
