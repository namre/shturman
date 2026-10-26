"""Утренняя сводка и обзор недели — расписание без терминала (решение Р-95, находка Х3).

Сводки — задачи Hermes `shturman:morning-brief` и `shturman:weekly-review` (их пишет ассистент по
скиллам плагина, приходят они владельцу в чат с ботом ассистента). Сервис их не выполняет и в Hermes не
ходит: он хранит, **что выбрал владелец** (`summaries.desired`), а применяет это плагин Hermes.

Как это ходит:
  1. Владелец на странице «Настройки Штурмана» или в приложении «Штурман» выбирает: утренняя сводка
     вкл/выкл и время, обзор недели вкл/выкл, день и время. Сервис записывает выбор (с номером версии)
     и ставит задание `hermes.cron` в общую очередь — ту же, из которой плагин берёт задания модели и
     сообщения (`jobs.py`, маршруты моста `/api/jobs/claim|complete` — новых маршрутов нет).
  2. Плагин в процессе шлюза Hermes забирает задание и штатными функциями Hermes (`cron`: создать
     задачу, `update --schedule`, `pause`, `resume`) приводит задачи к выбранному, затем отвечает
     фактическим состоянием: есть ли задача, включена ли, расписание, следующий запуск по поясу шлюза
     (Р-90). Сервис хранит ответ (`summaries.actual`).
  3. Раз в час сервис ставит то же задание только «посмотреть» (`apply: false`): так время следующего
     запуска свежее, а правка в самом Hermes видна как расхождение — её не перезаписывают молча.
     Расхождение показывают страница, приложение и строка `summaries` в `./ops/doctor.sh`.

Пока владелец ничего не выбирал, выбора нет (`desired` пуст): плагин только сообщает, что есть, и
ничего не меняет — уже заведённые задачи и их время остаются как есть.

Блок виден, только когда плагин хоть раз ответил (`available`): в режиме без Hermes заданий никто не
забирает, они гаснут по сроку (`jobs.QUEUE_TTL`), и блока нет. Сервис сам не знает, есть ли Hermes, —
судит по ответу.

Риск без особой цены: сводка уходит самому владельцу. Поэтому со страницы и из приложения — сразу, с
записью в журнал (`summaries.schedule`, «через: …») и в «Историю». Внутреннего API у выбора нет: ни
ассистент, ни дашборд Hermes его не меняют. Ответ плагина ассистент с токеном внутреннего API мог бы
подделать (как любой итог задания) — тогда страница покажет неверное «следующий запуск»; сами задачи
Hermes от этого не меняются.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

import asyncpg

from . import bridge, jobs

logger = logging.getLogger("shturman.summaries")

DESIRED = "summaries.desired"
ACTUAL = "summaries.actual"
KIND = "hermes.cron"
HANDLER = "summaries.sync"
KEYS = ("morning-brief", "weekly-review")
TITLES = {"morning-brief": "Утренняя сводка", "weekly-review": "Обзор недели"}
DEFAULTS = {"morning-brief": {"enabled": True, "time": "08:27"},
            "weekly-review": {"enabled": True, "time": "17:47", "day": 5}}
DAYS = ("вс", "пн", "вт", "ср", "чт", "пт", "сб")             # день недели в записи cron: 0 — воскресенье
DAYS_FULL = ("в воскресенье", "в понедельник", "во вторник", "в среду", "в четверг", "в пятницу", "в субботу")
AVAILABLE_DAYS = 14          # сколько дней после последнего ответа плагина блок ещё показывается
REFRESH_EVERY = 3600         # секунд между «посмотреть» без изменений
APPLY_GRACE = 600            # секунд, за которые плагин должен применить выбор, — дальше это расхождение
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_ERRORS = {
    "no_home_channel": "Сводке некуда приходить: у ассистента не задан ваш чат (мастер настройки ассистента, привязка бота).",
    "create_failed": "Ассистент не создал задачу сводки.",
    "update_failed": "Ассистент не изменил задачу сводки.",
    "no_cron": "В этой версии ассистента нет нужных функций расписания.",
    "failed": "Плагин не смог применить расписание.",
}


def _time(value: Any, key: str) -> str:
    if not isinstance(value, str) or not _TIME.match(value.strip()):
        raise ValueError(f"{TITLES[key]}: время — ЧЧ:ММ, например 08:30")
    return value.strip()


def validate(data: Any) -> dict[str, Any]:
    """Изменения выбора: {"morning-brief": {enabled?, time?}, "weekly-review": {enabled?, time?, day?}}."""
    if not isinstance(data, dict) or not data or set(data) - set(KEYS):
        raise ValueError("Нужно: morning-brief и/или weekly-review")
    out: dict[str, Any] = {}
    for key, value in data.items():
        if not isinstance(value, dict) or not value:
            raise ValueError(f"{TITLES[key]}: нечего менять")
        allowed = {"enabled", "time"} | ({"day"} if key == "weekly-review" else set())
        if set(value) - allowed:
            raise ValueError(f"{TITLES[key]}: допустимо " + ", ".join(sorted(allowed)))
        item: dict[str, Any] = {}
        if "enabled" in value:
            if not isinstance(value["enabled"], bool):
                raise ValueError(f"{TITLES[key]}: вкл — true или false")
            item["enabled"] = value["enabled"]
        if "time" in value:
            item["time"] = _time(value["time"], key)
        if "day" in value:
            day = value["day"]
            if isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6:
                raise ValueError("Обзор недели: день — от 0 (воскресенье) до 6 (суббота)")
            item["day"] = day
        out[key] = item
    return out


def _loads(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def _get(conn: asyncpg.Connection, key: str) -> dict[str, Any] | None:
    raw = _loads(await conn.fetchval("SELECT value FROM settings WHERE key = $1", key))
    return raw if isinstance(raw, dict) else None


async def _put(conn: asyncpg.Connection, key: str, value: dict[str, Any]) -> None:
    await conn.execute(
        """INSERT INTO settings (key, value) VALUES ($1, $2::jsonb)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
        key, json.dumps(value, ensure_ascii=False, sort_keys=True))


async def desired(conn: asyncpg.Connection) -> dict[str, Any] | None:
    return await _get(conn, DESIRED)


async def actual(conn: asyncpg.Connection) -> dict[str, Any] | None:
    return await _get(conn, ACTUAL)


def _current_choice(want: dict[str, Any] | None, have: dict[str, Any] | None) -> dict[str, Any]:
    """Выбор, от которого считается правка: сохранённый, иначе то, что сейчас в Hermes, иначе умолчание."""
    out: dict[str, Any] = {}
    jobs_ = (have or {}).get("jobs") or {}
    for key in KEYS:
        if want and isinstance(want.get(key), dict):
            out[key] = dict(want[key])
            continue
        job = jobs_.get(key) if isinstance(jobs_, dict) else None
        if isinstance(job, dict) and job.get("exists"):
            item = {"enabled": bool(job.get("enabled")), "time": job.get("time") or DEFAULTS[key]["time"]}
            if key == "weekly-review":
                day = job.get("day")
                item["day"] = day if isinstance(day, int) and 0 <= day <= 6 else DEFAULTS[key]["day"]
            out[key] = item
        else:
            out[key] = dict(DEFAULTS[key])
    return out


def words(key: str, item: dict[str, Any]) -> str:
    """«Утренняя сводка: каждый день в 08:30» / «Обзор недели: выключен»."""
    if not item.get("enabled"):
        return f"{TITLES[key]}: выключена" if key == "morning-brief" else f"{TITLES[key]}: выключен"
    if key == "weekly-review":
        return f"{TITLES[key]}: {DAYS_FULL[int(item.get('day', 5))]} в {item.get('time')}"
    return f"{TITLES[key]}: каждый день в {item.get('time')}"


async def set_desired(conn: asyncpg.Connection, changes: dict[str, Any], *, state: Any = None,
                      again: bool = False) -> dict[str, Any]:
    """Записать выбор владельца и попросить плагин применить. Сразу: сводка уходит самому владельцу.
    again — «Применить снова» без изменений (после правки в самом Hermes). Вне транзакции."""
    from . import history
    clean = {} if again and not changes else validate(changes)
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", "shturman.settings:summaries")
        want, have = await desired(conn), await actual(conn)
        before = _current_choice(want, have)
        after = {key: {**before[key], **clean.get(key, {})} for key in KEYS}
        revision = int((want or {}).get("revision") or 0) + 1
        value = {**after, "revision": revision, "changed_at": datetime.now(timezone.utc).isoformat()}
        await _put(conn, DESIRED, value)
        changed = [key for key in KEYS if before[key] != after[key]]
        if changed or want is None:
            await history.record(conn, "summaries", "summaries", "; ".join(words(k, after[k]) for k in (changed or KEYS)) + ".",
                                 {"kind": "summaries.schedule",
                                  "payload": {"changes": {k: before[k] for k in (changed or KEYS)}}})
        await _enqueue(conn, value, apply=True, key=f"rev:{revision}:{time.time_ns()}")
    return value


def _payload(want: dict[str, Any] | None, *, apply: bool) -> dict[str, Any]:
    body: dict[str, Any] = {"apply": bool(apply and want), "revision": int((want or {}).get("revision") or 0)}
    if want:
        body["desired"] = {key: want[key] for key in KEYS if isinstance(want.get(key), dict)}
    return body


async def _enqueue(conn: asyncpg.Connection, want: dict[str, Any] | None, *, apply: bool, key: str) -> int | None:
    return await jobs.enqueue(conn, KIND, _payload(want, apply=apply), handler=HANDLER, dedup_key=key,
                              max_attempts=2)


async def request_refresh(conn: asyncpg.Connection, *, key: str) -> int | None:
    """Попросить плагин только сообщить, что сейчас в Hermes (ничего не меняя)."""
    return await _enqueue(conn, await desired(conn), apply=False, key=key)


_JOB_FIELDS = ("exists", "enabled", "time", "day", "expr", "next_run_at", "next_local", "last", "last_run_at")
# Итог последнего запуска задачи в Hermes (Р-98) — код от плагина (`shturman_core.cron_jobs.last_run`):
# delivered — сводка ушла; silent — отработала, сказать было нечего ([SILENT]); failed — упала;
# delivery_failed — составлена, но не доставлена; queued — ждёт доставки; running — идёт сейчас;
# done — отработала, а ушла или промолчала, Hermes не записал; never — ещё не запускалась.
LAST_CODES = ("delivered", "silent", "failed", "delivery_failed", "queued", "running", "done", "never")
SILENT_AFTER = timedelta(hours=3)    # плагин не отвечал дольше (смотрит раз в час) — ассистент молчит
MISSED_AFTER = timedelta(minutes=90)  # столько после времени сводки — и её запуска так и нет


def _clean_report(result: dict[str, Any]) -> dict[str, Any]:
    """Ответ плагина — только ожидаемые поля и значения разумного вида (его мог подложить держатель токена)."""
    out_jobs: dict[str, Any] = {}
    raw = result.get("jobs") if isinstance(result.get("jobs"), dict) else {}
    for key in KEYS:
        job = raw.get(key)
        if not isinstance(job, dict):
            out_jobs[key] = {"exists": False}
            continue
        item: dict[str, Any] = {"exists": job.get("exists") is True, "enabled": job.get("enabled") is True}
        if isinstance(job.get("time"), str) and _TIME.match(job["time"]):
            item["time"] = job["time"]
        if isinstance(job.get("day"), int) and not isinstance(job.get("day"), bool) and 0 <= job["day"] <= 6:
            item["day"] = job["day"]
        for field in ("expr", "next_run_at", "next_local", "last_run_at"):
            if isinstance(job.get(field), str):
                item[field] = job[field][:64]
        if job.get("last") in LAST_CODES:
            item["last"] = job["last"]
        out_jobs[key] = item
    zone = result.get("timezone") if isinstance(result.get("timezone"), str) else None
    error = result.get("error") if result.get("error") in _ERRORS else None
    revision = result.get("revision")
    return {"jobs": out_jobs, "timezone": (zone or "")[:64] or None, "error": error,
            "revision": revision if isinstance(revision, int) and not isinstance(revision, bool) else None,
            "applied": result.get("applied") is True,
            "reported_at": datetime.now(timezone.utc).isoformat()}


@bridge.on_result(HANDLER)
async def _reported(conn: asyncpg.Connection, job: dict[str, Any], result: dict[str, Any]) -> None:
    report = _clean_report(result)
    before = await actual(conn) or {}
    if isinstance(before.get("job"), int) and before["job"] > int(job["id"]):
        return          # ответ на более раннее задание пришёл позже: он устарел
    report["job"] = int(job["id"])
    if not report["applied"]:
        # «Только посмотреть»: применённая версия выбора остаётся прежней.
        report["revision"] = before.get("revision")
    await _put(conn, ACTUAL, report)


@bridge.on_failure(HANDLER)
async def _failed(conn: asyncpg.Connection, job: dict[str, Any], error: str) -> None:
    # Задание погасло: либо плагина нет (режим без Hermes), либо он не смог. Прежний ответ не трогаем.
    logger.info("сводки: плагин не ответил на задание расписания")


def _parse(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo is not None else None


def compare(want: dict[str, Any] | None, have: dict[str, Any] | None, *, now: datetime | None = None) -> dict[str, Any]:
    """Совпадает ли выбранное с тем, что в Hermes. state: unset | in_sync | pending | drift | no_report | error."""
    now = now or datetime.now(timezone.utc)
    if want is None:
        return {"state": "unset", "differs": []}
    if have is None:
        return {"state": "no_report", "differs": []}
    changed_at = _parse(want.get("changed_at")) or now
    reported_at = _parse(have.get("reported_at"))
    fresh_change = (now - changed_at).total_seconds() < APPLY_GRACE
    differs = []
    for key in KEYS:
        item, job = want.get(key) or {}, (have.get("jobs") or {}).get(key) or {}
        if not item.get("enabled"):
            if job.get("exists") and job.get("enabled"):
                differs.append(key)
            continue
        if not job.get("exists") or not job.get("enabled") or job.get("time") != item.get("time") \
                or (key == "weekly-review" and job.get("day") != item.get("day")):
            differs.append(key)
    if have.get("error"):
        return {"state": "error", "differs": differs, "error": have["error"], "error_text": _ERRORS[have["error"]]}
    behind = (have.get("revision") or 0) < int(want.get("revision") or 0)
    if behind or (differs and reported_at is not None and reported_at < changed_at):
        return {"state": "pending" if fresh_change else "no_report", "differs": differs}
    return {"state": "drift" if differs else "in_sync", "differs": differs}


async def view(conn: asyncpg.Connection) -> dict[str, Any]:
    """Для страницы и приложения: выбор, что в Hermes, совпадают ли, следующий запуск. Только время и признаки."""
    want, have = await desired(conn), await actual(conn)
    reported = _parse((have or {}).get("reported_at"))
    available = reported is not None and datetime.now(timezone.utc) - reported < timedelta(days=AVAILABLE_DAYS)
    choice = _current_choice(want, have)
    jobs_ = (have or {}).get("jobs") or {}
    return {"available": available, "chosen": want is not None, "choice": choice,
            "hermes": {key: jobs_.get(key) or {"exists": False} for key in KEYS},
            "timezone": (have or {}).get("timezone"), "reported_at": reported.isoformat() if reported else None,
            "sync": compare(want, have),
            "words": {key: words(key, choice[key]) for key in KEYS},
            "days": list(DAYS)}


async def overview(conn: asyncpg.Connection) -> dict[str, Any]:
    """Для `GET /api/status` и строки `summaries` в `./ops/doctor.sh`: только признаки и время."""
    v = await view(conn)
    nxt = {key: (v["hermes"][key] or {}).get("next_local") for key in KEYS}
    morning = v["hermes"]["morning-brief"] or {}
    return {"summaries_available": v["available"], "summaries_chosen": v["chosen"],
            "summaries_state": v["sync"]["state"], "summaries_differs": v["sync"]["differs"],
            "summaries_reported_at": v["reported_at"], "summaries_next_morning": nxt["morning-brief"],
            "summaries_next_weekly": nxt["weekly-review"],
            "summaries_last_morning": morning.get("last"), "summaries_last_morning_at": morning.get("last_run_at")}


def _scheduled_today(time_text: Any, zone_name: Any, now: datetime) -> datetime | None:
    """Время сводки сегодня по поясу шлюза Hermes. None — не разобрать."""
    from zoneinfo import ZoneInfo

    if not isinstance(time_text, str) or not _TIME.match(time_text):
        return None
    try:
        zone = ZoneInfo(zone_name) if isinstance(zone_name, str) and zone_name else ZoneInfo("UTC")
    except Exception:  # noqa: BLE001
        return None
    local = now.astimezone(zone)
    hour, minute = (int(x) for x in time_text.split(":"))
    return local.replace(hour=hour, minute=minute, second=0, microsecond=0)


def morning_problem(have: dict[str, Any] | None, *, now: datetime | None = None,
                    zone: str | None = None) -> str | None:
    """Для внешнего сторожа (pulse.py): неполадка утренней сводки или None.

    assistant_silent — плагин Hermes давно не отвечал на «посмотреть» (раз в час): шлюз Hermes стоит;
    summary_failed — последний запуск за сутки упал или не доставлен;
    summary_missed — время сводки сегодня прошло на `MISSED_AFTER`, плагин с тех пор отвечал, а запуска не было.
    «Промолчала» ([SILENT]) — не неполадка. Сводка выключена, Hermes нет или он не отвечал давно
    (`AVAILABLE_DAYS`) — проверять нечего, None."""
    now = now or datetime.now(timezone.utc)
    reported = _parse((have or {}).get("reported_at"))
    if reported is None or now - reported >= timedelta(days=AVAILABLE_DAYS):
        return None
    if now - reported > SILENT_AFTER:
        return "assistant_silent"
    job = ((have or {}).get("jobs") or {}).get("morning-brief") or {}
    if not job.get("exists") or not job.get("enabled") or "last" not in job:
        return None     # плагин прежней версии итога запуска не сообщает — судить не по чему
    ran = _parse(job.get("last_run_at"))
    if job.get("last") in ("failed", "delivery_failed") and ran is not None and now - ran < timedelta(hours=26):
        return "summary_failed"
    planned = _scheduled_today(job.get("time"), (have or {}).get("timezone") or zone, now)
    if planned is None or now < planned + MISSED_AFTER or reported < planned + timedelta(minutes=15):
        return None
    if ran is None or ran < planned - timedelta(minutes=5):
        return "summary_missed"
    return None


SCHEDULED_SPAN = timedelta(minutes=60)   # столько после времени сводки или обзора длится их плановое чтение архива


async def running_now(conn: asyncpg.Connection, *, now: datetime | None = None,
                      span: timedelta = SCHEDULED_SPAN) -> str | None:
    """Идёт ли сейчас плановая сводка или обзор недели Hermes: ключ задачи или None.

    Время — то, что сообщил плагин (задача в Hermes и пояс шлюза), а если он не сообщал — выбор владельца
    или умолчание. Нужно границе выдачи архива (`archive_guard`): обзор недели читает много чатов подряд,
    и это не тревога. Отчёт плагина мог подложить держатель токена Hermes — тогда он знает и окно; поэтому
    окно ослабляет только признак «много чатов подряд», а не пределы выдачи, объём и ловушки."""
    now = now or datetime.now(timezone.utc)
    want, have = await desired(conn), await actual(conn)
    choice = _current_choice(want, have)
    jobs_ = (have or {}).get("jobs") or {}
    zone = (have or {}).get("timezone")
    for key in KEYS:
        job = jobs_.get(key) if isinstance(jobs_.get(key), dict) and jobs_[key].get("exists") else None
        enabled = bool(job.get("enabled")) if job else bool(choice[key].get("enabled"))
        if not enabled:
            continue
        time_text = (job or {}).get("time") or choice[key].get("time")
        for shift in (0, 1):     # окно могло начаться вчера (обзор поздно вечером)
            planned = _scheduled_today(time_text, zone, now - timedelta(days=shift))
            if planned is None:
                continue
            if key == "weekly-review":
                day = (job or {}).get("day", choice[key].get("day"))
                if not isinstance(day, int) or (planned.isoweekday() % 7) != day:
                    continue
            if planned <= now < planned + span:
                return key
    return None


BOOT_DELAY = 30              # секунд после запуска до первого «посмотреть»: плагин успевает подключиться


@contextlib.asynccontextmanager
async def lifespan(state: Any) -> AsyncIterator[None]:
    async def loop() -> None:
        await asyncio.sleep(BOOT_DELAY)
        boot = True
        while True:
            try:
                async with state.pool.acquire() as conn:
                    await tick(conn, boot=boot)
                boot = False
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — следующая попытка через час
                logger.warning("сводки: задание расписания не поставлено (%s)", type(exc).__name__)
            await asyncio.sleep(REFRESH_EVERY)

    state.spawn(loop(), name="summaries-tick")
    yield


async def tick(conn: asyncpg.Connection, *, boot: bool = False) -> int | None:
    """Раз в час (и при запуске) — «посмотреть». Пока плагин ни разу не ответил, — только при запуске."""
    want, have = await desired(conn), await actual(conn)
    if not boot and have is None:
        return None
    # Выбор ещё не применён (плагина не было, задание погасло) — попросить применить, иначе только посмотреть.
    behind = want is not None and int((have or {}).get("revision") or 0) < int(want.get("revision") or 0)
    key = f"boot:{time.time_ns()}" if boot else f"tick:{int(time.time() // REFRESH_EVERY)}"
    return await _enqueue(conn, want, apply=behind, key=key)
