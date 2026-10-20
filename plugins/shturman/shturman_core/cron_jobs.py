"""Задачи по расписанию, которые «Штурман» предлагает завести в Hermes: сводка и обзор недели.

Здесь только описание задач и расчёт «что уже есть, что создать». Сами задачи создаёт
plugin_api штатной функцией Hermes (`cron.scheduler.create_job_with_scheduler_registration`).

Почему не «чертёж» в шапке скилла (`metadata.hermes.blueprint`): Hermes 0.21.5 ищет чертежи только
в собственном каталоге скиллов (`tools/blueprints.py:100-116`, вызывается при установке скилла
из каталога), а скиллы плагина туда не копируются. Поэтому задачи заводятся явным вызовом.

Время задач — по часовому поясу, которым считает планировщик шлюза Hermes, а это `timezone`
в config.yaml профиля (Р-90). Переменная HERMES_TIMEZONE для шлюза в многопрофильном режиме
(в Hermes 0.21.5 он включён по умолчанию) не действует: `hermes_time._env_timezone()` при
`agent.secret_scope.is_multiplex_active()` её не читает, и без `timezone` в config.yaml шлюз
считает по часам сервера (в контейнере — UTC). Вне шлюза — в дашборде, в `hermes cron …` из
терминала — переменная действует, поэтому задача, созданная из дашборда, получает верный первый
запуск, а после первого прогона шлюз пересчитывает его по UTC. Поэтому пояс из окружения
контейнера (`HERMES_TIMEZONE` ← `SHTURMAN_TIMEZONE`) записывается в config.yaml профиля:
`./ops/up.sh` (функция tz_sync) и плагин при заведении задач. Проверка — строки `hermes-tz`
и `cron-schedule` в `./ops/doctor.sh`. Функции ниже — без зависимостей от Hermes.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

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


# ------------------------------------------------ часовой пояс шлюза (Р-90)

NAME_PREFIX = "shturman:"


def zone_valid(name: str | None) -> bool:
    """Известен ли пояс базе часовых поясов (IANA)."""
    if not isinstance(name, str) or not name.strip():
        return False
    try:
        ZoneInfo(name.strip())
    except Exception:  # noqa: BLE001 — неизвестное имя, путь вместо имени и прочее
        return False
    return True


def config_zone(raw_config: Mapping[str, Any] | None) -> str:
    """`timezone` из config.yaml профиля так, как его читает Hermes (`hermes_time`); пусто — не задан."""
    value = (raw_config or {}).get("timezone")
    return value.strip() if isinstance(value, str) else ""


def zone_state(expected: str | None, configured: str | None) -> str:
    """Сравнение пояса из окружения контейнера (`HERMES_TIMEZONE`) с `timezone` профиля.

    none — пояса в окружении нет: решает Hermes, ничего не трогаем; invalid — в окружении
    неизвестный пояс, записывать нельзя; same — совпадают; missing — в config.yaml пусто;
    differs — в config.yaml другой.
    """
    want = (expected or "").strip()
    have = (configured or "").strip()
    if not want:
        return "none"
    if not zone_valid(want):
        return "invalid"
    if not have:
        return "missing"
    return "same" if have == want else "differs"


# Запись плагина в своём хранилище (Store) о том, когда он сам записал пояс в config.yaml: шлюз читает
# пояс при запуске и держит в памяти, поэтому `./ops/up.sh` перезапускает Hermes, если запись новее
# запуска контейнера, а `./ops/doctor.sh` предупреждает об этом.
TZ_STATE = "timezone"


def zone_warning(state: str, expected: str | None, configured: str | None) -> str | None:
    """Предупреждение для мастера и вызова `cron/install`; None — всё в порядке."""
    want, have = (expected or "").strip(), (configured or "").strip()
    if state == "same" or (state == "none" and have):
        return None
    if state == "none":
        return ("Часовой пояс в Hermes не задан: сводки пойдут по часам сервера (обычно UTC). "
                "Задайте SHTURMAN_TIMEZONE в .env (docs/setup.md) и запустите ./ops/up.sh.")
    if state == "invalid":
        return (f"Часовой пояс «{want}» из SHTURMAN_TIMEZONE неизвестен — в настройки Hermes он не записан, "
                "сводки пойдут по часам сервера. Исправьте строку (вид Europe/Moscow) и запустите ./ops/up.sh.")
    if state == "written":
        return (f"Часовой пояс {want} записан в настройки Hermes (timezone в config.yaml). Шлюз Hermes "
                "начнёт считать расписание по нему после перезапуска: запустите ./ops/up.sh — он перезапустит "
                "Hermes и пересчитает время сводок.")
    return (f"Шлюз Hermes считает расписание по {('поясу ' + have) if have else 'часам сервера (UTC)'}, "
            f"а не по {want}: в config.yaml профиля другой пояс или его нет. Запустите ./ops/up.sh — "
            "он запишет пояс, перезапустит Hermes и пересчитает время сводок.")


def restart_pending(written_at: Any, started_at: float | None) -> bool:
    """Пояс записан плагином после запуска контейнера Hermes — шлюз ещё считает по прежнему."""
    if isinstance(written_at, bool) or not isinstance(written_at, (int, float)) or started_at is None:
        return False
    return written_at >= started_at


def expected_zone(expected: str | None, configured: str | None) -> str:
    """Пояс, по которому задачам положено стоять: из окружения, если он известен, иначе из config.yaml."""
    want = (expected or "").strip()
    return want if zone_valid(want) else (configured or "").strip()


def ours(jobs: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Задачи «Штурмана» — по имени `shturman:…`."""
    return [j for j in jobs if isinstance(j, Mapping) and str(j.get("name") or "").startswith(NAME_PREFIX)]


def _cron_field_ok(field: str, value: int, *, weekday: bool = False) -> bool | None:
    """Совпадает ли значение с полем cron. None — поле сложнее числа и списка чисел: не проверяем."""
    if field == "*":
        return True
    parts = field.split(",")
    if not all(p.isdigit() for p in parts):
        return None
    numbers = {int(p) for p in parts}
    if weekday and 7 in numbers:
        numbers.add(0)
    return value in numbers


def schedule_drift(job: Mapping[str, Any], zone: str | None) -> str:
    """Стоит ли следующий запуск задачи на её расписании по поясу шлюза.

    ok — смещение `next_run_at` совпадает со смещением пояса, а час и минута по нему — с записью
    cron; offset — смещение другое (задачу пересчитали по другому поясу, обычно по UTC);
    off-schedule — смещение то же, а время не по расписанию; no-next — включённая задача без
    следующего запуска; paused — задача выключена или на паузе (не проверяется);
    unchecked — расписание не cron или запись не разобрать. Пустой `zone` — пояс сервера (UTC).
    """
    if not job.get("enabled", True) or job.get("state") == "paused":
        return "paused"
    schedule = job.get("schedule")
    if not isinstance(schedule, Mapping) or schedule.get("kind") != "cron":
        return "unchecked"
    fields = str(schedule.get("expr") or "").split()
    raw = job.get("next_run_at")
    if not raw:
        return "no-next"
    try:
        moment = datetime.fromisoformat(str(raw))
        tz = ZoneInfo(zone.strip()) if zone and zone.strip() else ZoneInfo("UTC")
    except Exception:  # noqa: BLE001
        return "unchecked"
    if moment.tzinfo is None or len(fields) != 5:
        return "unchecked"
    local = moment.astimezone(tz)
    if moment.utcoffset() != local.utcoffset():
        return "offset"
    checks = (_cron_field_ok(fields[0], local.minute), _cron_field_ok(fields[1], local.hour),
              _cron_field_ok(fields[4], local.isoweekday() % 7, weekday=True))
    return "off-schedule" if False in checks else "ok"


_WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def local_run(raw: Any, zone: str | None) -> str:
    """Следующий запуск по поясу шлюза для людей: «пт 16.10 08:27». Пусто — не разобрать."""
    try:
        moment = datetime.fromisoformat(str(raw))
        tz = ZoneInfo(zone.strip()) if zone and zone.strip() else ZoneInfo("UTC")
    except Exception:  # noqa: BLE001
        return ""
    if moment.tzinfo is None:
        return ""
    local = moment.astimezone(tz)
    return f"{_WEEKDAYS[local.weekday()]} {local:%d.%m %H:%M}"


# ------------------------------------------------ итог последнего запуска (Р-98)

# Коды для сервиса (`summaries.LAST_CODES`) и для строки `summary-last` в `./ops/doctor.sh`:
#   delivered       — сводка ушла владельцу;
#   silent          — отработала, сказать было нечего: ответ [SILENT], доставка подавлена;
#   failed          — запуск упал (ошибка модели, инструментов, прерван перезапуском шлюза);
#   delivery_failed — составлена, но не доставлена (Telegram не принял, некуда доставлять);
#   queued          — ждёт доставки (шлюз переподключается к Telegram);
#   running         — идёт сейчас;
#   done            — отработала, а ушла или промолчала, — Hermes не записал (журнала запусков нет);
#   never           — ещё не запускалась.
# Источник — штатные записи Hermes 0.21.5 (`v2026.9.24`), без текста сводки: у задачи (`cron/jobs.py`,
# `_record_run_outcome`) — `last_run_at` и `last_status` (ok | error | delivery_failed | delivery_queued |
# blocked_config); в журнале запусков (`cron/executions.py`, таблица executions) — `status` (claimed |
# running | completed | failed | unknown) и `delivery_outcome` (`cron/scheduler.py`,
# `_classify_delivery_outcome`: delivered | failed | queued | suppressed | not_configured |
# suppressed_acked). Ответ [SILENT] Hermes записывает как успех с `last_status = ok` — отличить его от
# доставленной сводки можно только по журналу запусков: там `delivery_outcome = suppressed`.
LAST_CODES = ("delivered", "silent", "failed", "delivery_failed", "queued", "running", "done", "never")

_BY_OUTCOME = {"delivered": "delivered", "suppressed": "silent", "failed": "delivery_failed",
               "not_configured": "delivery_failed", "queued": "queued"}
_BY_STATUS = {"ok": "done", "error": "failed", "delivery_failed": "delivery_failed",
              "delivery_queued": "queued", "blocked_config": "failed"}


def last_run(job: Mapping[str, Any], execution: Mapping[str, Any] | None = None) -> str:
    """Код итога последнего запуска задачи по её записи и последней записи журнала запусков."""
    if isinstance(execution, Mapping):
        status = execution.get("status")
        if status in ("claimed", "running"):
            return "running"
        if status in ("failed", "unknown"):
            return "failed"
        if status == "completed":
            code = _BY_OUTCOME.get(str(execution.get("delivery_outcome") or ""))
            if code:
                return code
    if not job.get("last_run_at") and job.get("last_status") is None:
        return "never"
    return _BY_STATUS.get(str(job.get("last_status") or ""), "done")


def last_run_at(job: Mapping[str, Any], execution: Mapping[str, Any] | None = None) -> str | None:
    """Когда закончился последний запуск (ISO); None — не было."""
    finished = execution.get("finished_at") if isinstance(execution, Mapping) else None
    for raw in (finished, job.get("last_run_at")):
        if isinstance(raw, str) and raw:
            try:
                moment = datetime.fromisoformat(raw)
            except ValueError:
                continue
            if moment.tzinfo is not None:
                return moment.isoformat()[:64]
    return None


def overdue(job: Mapping[str, Any], now: datetime, *, grace_s: float = 600.0) -> bool:
    """Включённая задача, у которой следующий запуск давно в прошлом: планировщик её не запустил."""
    if not job.get("enabled", True) or job.get("state") == "paused":
        return False
    try:
        nxt = datetime.fromisoformat(str(job.get("next_run_at")))
    except (TypeError, ValueError):
        return False
    if nxt.tzinfo is None:
        return False
    return (now - nxt).total_seconds() > grace_s
