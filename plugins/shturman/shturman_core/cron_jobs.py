"""Задачи по расписанию, которые «Штурман» предлагает завести в Hermes: сводка и обзор недели.

Здесь только описание задач и расчёт «что уже есть, что создать». Сами задачи создаёт
plugin_api штатной функцией Hermes (`cron.scheduler.create_job_with_scheduler_registration`).

Почему не «чертёж» в шапке скилла (`metadata.hermes.blueprint`): Hermes 0.21.5 ищет чертежи только
в собственном каталоге скиллов (`tools/blueprints.py:100-116`, вызывается при установке скилла
из каталога), а скиллы плагина туда не копируются. Поэтому задачи заводятся явным вызовом.

Время указано по часам, настроенным в Hermes (`timezone` в config.yaml или HERMES_TIMEZONE);
если часовой пояс не задан, Hermes считает по часам сервера.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

SKILL_NAMESPACE = "shturman"
MCP_SERVER = "shturman"          # под этим именем архив подключён к Hermes как MCP-сервер
READ_TOOLSET = "shturman_read"
DELIVER = "telegram"             # домашний канал Telegram (TELEGRAM_HOME_CHANNEL) — управляющий чат владельца

DEFAULT_TIMES = {"morning-brief": "08:27", "weekly-review": "17:47"}
WEEKLY_DAY = 5                   # пятница в записи cron

_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

JOBS: tuple[dict[str, Any], ...] = (
    {
        "key": "morning-brief",
        "name": "shturman:morning-brief",
        "title": "Утренняя сводка",
        "prompt": "Составь утреннюю сводку для владельца по инструкции скилла morning-brief.",
    },
    {
        "key": "weekly-review",
        "name": "shturman:weekly-review",
        "title": "Обзор недели",
        "prompt": "Составь обзор недели для владельца по инструкции скилла weekly-review.",
    },
)


def schedule_for(key: str, at: str | None = None, *, weekday: int = WEEKLY_DAY) -> str:
    """Запись cron: сводка — каждый день, обзор — раз в неделю."""
    match = _TIME.match((at or DEFAULT_TIMES[key]).strip())
    if match is None:
        raise ValueError("время нужно в виде ЧЧ:ММ, например 08:27")
    hour, minute = int(match.group(1)), int(match.group(2))
    if key == "weekly-review":
        if isinstance(weekday, bool) or not isinstance(weekday, int) or not 0 <= weekday <= 6:
            raise ValueError("день недели — число от 0 (воскресенье) до 6 (суббота)")
        return f"{minute} {hour} * * {weekday}"
    return f"{minute} {hour} * * *"


def job_spec(job: Mapping[str, Any], *, at: str | None = None, weekday: int = WEEKLY_DAY) -> dict[str, Any]:
    """Аргументы для создания задачи в Hermes.

    Прогон читает чужой текст из переписки, поэтому получает только архив (MCP) и чтение
    обязательств: ни терминала, ни веб-поиска, ни инструментов, которые что-то меняют.
    """
    return {
        "prompt": job["prompt"],
        "schedule": schedule_for(job["key"], at, weekday=weekday),
        "name": job["name"],
        "deliver": DELIVER,
        "skills": [f"{SKILL_NAMESPACE}:{job['key']}"],
        "enabled_toolsets": [MCP_SERVER, READ_TOOLSET],
    }


def plan(existing: Iterable[Mapping[str, Any]], *, times: Mapping[str, str] | None = None,
         weekday: int = WEEKLY_DAY) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Что создать и что уже есть. Повторный вызов ничего не дублирует: задача узнаётся по имени.

    Существующую задачу не трогаем: владелец мог поменять её время или приостановить.
    """
    by_name = {str(j.get("name")): j for j in existing if isinstance(j, Mapping)}
    create, present = [], []
    for job in JOBS:
        found = by_name.get(job["name"])
        if found is not None:
            present.append({"key": job["key"], "name": job["name"], "title": job["title"],
                            "id": found.get("id"), "enabled": bool(found.get("enabled", True)),
                            "schedule": found.get("schedule_display") or None})
        else:
            create.append(job_spec(job, at=(times or {}).get(job["key"]), weekday=weekday))
    return create, present
