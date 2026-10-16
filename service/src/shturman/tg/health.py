"""Здоровье сессий Telegram: сбор переписки не должен стоять молча (Р-98, пробел О6 этапа обкатки).

Раньше сессия, которую Telegram перестал принимать (владелец завершил её в «Устройствах», Telegram
сбросил ключ — `AuthKeyUnregisteredError`, `SessionRevokedError`, `SessionExpiredError` и прочие
`UnauthorizedError` Telethon 1.45, а также `AuthKeyError`, например AUTH_KEY_DUPLICATED), только меняла
состояние аккаунта на экране: сбор стоял, пока владелец сам не откроет приложение. Как сервис узнаёт
о разрыве — `tg/manager.py`, `_supervise` и `_session`: Telethon при отказе авторизации в цикле
обновлений отключается (`client/updates.py`, ветка `UnauthorizedError, AuthKeyError` → `disconnect()`),
следующее подключение спрашивает `get_me()`, а он возвращает None ровно тогда, когда Telegram ответил
`UnauthorizedError` (`client/users.py`), — состояние `unauthorized`. `AuthKeyError` — состояние `failed`.

Здесь — оценка по ролям (основной аккаунт, помощник) и одно сообщение владельцу в бот согласований при
переходе в плохое состояние:

  ok         — на связи, обновления идут;
  connecting — подключается или переподключается меньше `ERROR_GRACE`;
  error      — нет связи дольше `ERROR_GRACE` (сервис сам пробует снова, до раза в 10 минут);
  relogin    — нужно войти заново: сессия не действует, ключ отозван, файла сессии нет;
  locked     — сессия занята другим процессом;
  quiet      — соединение есть, а обновлений о сообщениях от Telegram нет дольше `QUIET_AFTER`
               (обновления считаются по всем чатам аккаунта, не только по выбранным);
  paused     — владелец поставил аккаунт на паузу: его решение, не неполадка.

Сообщение владельцу — при первом входе в плохое состояние (`ALERT`); то же состояние той же роли —
не чаще раза в сутки (`REPEAT_AFTER`), а после того как аккаунт был на связи не меньше `CLEAR_AFTER`, —
снова сразу. Что и когда отправлено — в `settings` под ключом `tg.health.alerts` (роль, состояние,
время; без имён). Без своего бота согласований и без Hermes сообщение доставить некому — тогда сбой
видят только строка `tg-session` в `./ops/doctor.sh` и внешний сторож (`pulse.py`).

Наружу (`GET /api/status`, `./ops/doctor.sh`, внешний сторож) уходят только роль, код состояния и
время — ни подписи аккаунта, ни текста ошибки.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Callable

import asyncpg

from .. import bridge

logger = logging.getLogger("shturman.tg.health")

SETTING = "tg.health.alerts"
CHECK_EVERY = 300.0            # секунд между проверками
ERROR_GRACE = 900.0            # нет связи дольше — «error» (короткие разрывы — норма, сервис переподключается сам)
QUIET_AFTER = 24 * 3600.0      # соединение есть, а обновлений нет дольше — «quiet»
REPEAT_AFTER = timedelta(hours=24)
CLEAR_AFTER = 3600.0           # столько на связи — и следующий сбой снова сообщается сразу

OK, CONNECTING, ERROR, RELOGIN, LOCKED, QUIET, PAUSED = (
    "ok", "connecting", "error", "relogin", "locked", "quiet", "paused")
ALERT = (RELOGIN, ERROR, LOCKED, QUIET)
# Хуже — раньше: по нему выбирается одно состояние для строки doctor и для сторожа.
ORDER = (RELOGIN, LOCKED, ERROR, QUIET, CONNECTING, PAUSED, OK)

_RELOGIN_STATUSES = ("unauthorized", "no_session", "failed")
_CONNECTING_STATUSES = ("starting", "disconnected", "error")

ROLE_WORDS = {"owner": "ваш аккаунт Telegram", "assistant": "аккаунт помощника"}
WHERE = {"owner": "«Настройки Штурмана» → «Вход в ваш аккаунт Telegram»",
         "assistant": "«Настройки Штурмана» → «Группы: аккаунт помощника»"}


def worst(states: list[str]) -> str | None:
    known = [s for s in states if s in ORDER]
    return min(known, key=ORDER.index) if known else None


@dataclass
class Tracker:
    """Помнит, с какого момента у роли нынешнее состояние сессии (в памяти процесса)."""

    clock: Callable[[], float] = time.monotonic
    seen: dict[str, tuple[str, float]] = field(default_factory=dict)

    def assess(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Строки `TgManager.health_rows()` → [{role, state, for_s, update_age_s}]."""
        now = self.clock()
        out = []
        roles = set()
        for row in rows:
            role, status = str(row.get("role") or ""), str(row.get("status") or "")
            if role not in ROLE_WORDS:
                continue
            roles.add(role)
            prev = self.seen.get(role)
            since = prev[1] if prev is not None and prev[0] == status else now
            self.seen[role] = (status, since)
            held = now - since
            age = row.get("update_age")
            age = float(age) if isinstance(age, (int, float)) and not isinstance(age, bool) else None
            if status == "running":
                # «Тихо» — только если и сессия на связи дольше порога, и обновлений столько же нет.
                quiet = held >= QUIET_AFTER and (age is None or age >= QUIET_AFTER)
                state = QUIET if quiet else OK
            elif status in _RELOGIN_STATUSES:
                state = RELOGIN
            elif status == "locked":
                state = LOCKED
            elif status == "paused":
                state = PAUSED
            elif status in _CONNECTING_STATUSES:
                state = ERROR if held >= ERROR_GRACE else CONNECTING
            else:
                state = ERROR if held >= ERROR_GRACE else CONNECTING
            out.append({"role": role, "state": state, "for_s": int(held),
                        "update_age_s": None if age is None else int(age)})
        for gone in set(self.seen) - roles:
            self.seen.pop(gone, None)
        return out


def alert_text(role: str, state: str) -> str:
    """Сообщение владельцу простыми словами: что случилось, что это значит, что сделать."""
    who = ROLE_WORDS.get(role, "аккаунт Telegram")
    where = WHERE.get(role, WHERE["owner"])
    stopped = ("Новые сообщения не попадают в архив: договорённости, разбор входящих и поиск по ним "
               "стоят, пока это не исправлено.")
    if state == RELOGIN:
        return (f"⚠️ Сбор переписки остановлен: Telegram больше не принимает вход «Штурмана» в {who} "
                f"(сессию завершили в «Устройствах» Telegram, или Telegram сбросил её сам). {stopped}\n\n"
                f"Что сделать: {where} → «Войти заново» и отсканируйте QR-код телефоном. "
                "Уже собранное не потеряется.")
    if state == LOCKED:
        return (f"⚠️ Сбор переписки остановлен: сессию «Штурмана» в {who} занял другой процесс на сервере. "
                f"{stopped}\n\nЧто сделать: попросите оператора проверить сервер — ./ops/doctor.sh, "
                "строка tg-session.")
    if state == QUIET:
        return (f"⚠️ Похоже, сбор переписки завис: соединение с Telegram есть, но больше суток от Telegram не "
                f"пришло ни одного обновления по {'вашему аккаунту' if role == 'owner' else 'аккаунту помощника'}. "
                f"Если вам за это время писали — сообщения не попали в архив.\n\nЧто сделать: {where} → "
                "«Пауза», затем «Снять с паузы» — сервис подключится заново. Не помогло — оператору: "
                "./ops/doctor.sh, строка tg-session.")
    return (f"⚠️ Сбор переписки стоит: у «Штурмана» нет связи с Telegram по {'вашему аккаунту' if role == 'owner' else 'аккаунту помощника'} "
            "дольше 15 минут. Сервис сам пробует подключиться снова. Если сообщение повторится, — "
            "оператору: ./ops/doctor.sh, строка tg-session (и строки service, disk).")


def _loads(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _parse(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo is not None else None


async def remember(conn: asyncpg.Connection) -> dict[str, Any]:
    raw = _loads(await conn.fetchval("SELECT value FROM settings WHERE key = $1", SETTING))
    return raw if isinstance(raw, dict) else {}


async def _save(conn: asyncpg.Connection, value: dict[str, Any]) -> None:
    await conn.execute(
        """INSERT INTO settings (key, value) VALUES ($1, $2::jsonb)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
        SETTING, json.dumps(value, ensure_ascii=False, sort_keys=True))


async def alert(conn: asyncpg.Connection, assessed: list[dict[str, Any]], *,
                now: datetime | None = None) -> list[tuple[str, str]]:
    """Шлёт владельцу сообщения о плохих состояниях по правилам из шапки. Возвращает [(роль, состояние)]
    отправленного."""
    now = now or datetime.now(timezone.utc)
    sent: list[tuple[str, str]] = []
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", SETTING)
        memory = await remember(conn)
        changed = False
        for item in assessed:
            role, state = item["role"], item["state"]
            notes = memory.get(role) if isinstance(memory.get(role), dict) else {}
            if state == OK and item.get("for_s", 0) >= CLEAR_AFTER:
                for note in notes.values():
                    if isinstance(note, dict) and not note.get("cleared"):
                        note["cleared"] = True
                        changed = True
            if state not in ALERT:
                memory[role] = notes
                continue
            note = notes.get(state) if isinstance(notes.get(state), dict) else None
            last = _parse(note.get("at")) if note else None
            if note is not None and not note.get("cleared") and last is not None and now - last < REPEAT_AFTER:
                continue
            await bridge.notify_owner(conn, alert_text(role, state),
                                      dedup_key=f"tg.health:{role}:{state}:{now:%Y%m%d%H%M}")
            notes[state] = {"at": now.isoformat(), "cleared": False}
            memory[role] = notes
            changed = True
            sent.append((role, state))
            logger.warning("аккаунт (%s): %s — владельцу отправлено сообщение", role, state)
        if changed:
            await _save(conn, memory)
    return sent


def public(assessed: list[dict[str, Any]] | None, *, configured: bool) -> dict[str, Any]:
    """Для `GET /api/status` (поле `tg_health`): роль, код состояния, сколько держится, давность обновлений."""
    items = [{"role": a["role"], "state": a["state"], "for_s": a.get("for_s"),
              "update_age_s": a.get("update_age_s")} for a in (assessed or [])]
    return {"configured": configured, "checked": assessed is not None, "accounts": items,
            "worst": worst([a["state"] for a in items])}


class Watcher:
    """Раз в `CHECK_EVERY` секунд — оценка и сообщения владельцу. Последняя оценка — `last`."""

    def __init__(self, manager: Any, pool: asyncpg.Pool, *, tracker: Tracker | None = None) -> None:
        self.manager, self.pool = manager, pool
        self.tracker = tracker or Tracker()
        self.last: list[dict[str, Any]] | None = None

    async def check(self) -> list[dict[str, Any]]:
        rows = await self.manager.health_rows() if self.manager.configured else []
        assessed = self.tracker.assess(rows)
        self.last = assessed
        async with self.pool.acquire() as conn:
            await alert(conn, assessed)
        return assessed

    def snapshot(self) -> dict[str, Any]:
        return public(self.last, configured=bool(getattr(self.manager, "configured", False)))


@contextlib.asynccontextmanager
async def running(state: Any, manager: Any) -> AsyncIterator[Watcher]:
    watcher = Watcher(manager, state.pool)
    state.extras["tg_health"] = watcher

    async def loop() -> None:
        while True:
            try:
                await watcher.check()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — следующая проверка через CHECK_EVERY
                logger.warning("проверка сессий Telegram не удалась (%s)", type(exc).__name__)
            await asyncio.sleep(CHECK_EVERY)

    task = state.spawn(loop(), name="tg-health")
    try:
        yield watcher
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        state.extras.pop("tg_health", None)


def snapshot(state: Any) -> dict[str, Any]:
    """`tg_health` для `GET /api/status`; модуль аккаунтов не запущен — пусто."""
    watcher = state.extras.get("tg_health")
    if isinstance(watcher, Watcher):
        return watcher.snapshot()
    return public(None, configured=False)
