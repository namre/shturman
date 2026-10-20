"""Разрешения владельца на действия сопровождения сервера (Р-100, дефект Д-29).

Зачем. Восстановление из копии, откат обновления, автоматический откат базы при обновлении, перезапуск
без свежей копии, порог защиты и удаление старых копий — стоп-точки `AGENTS.md`: на них нужно «да»
владельца. Раньше скрипт `ops/` спрашивал фразу с одноразовым числом в терминале человека. Владелец,
который сопровождает экземпляр с помощью Codex, терминалом и ssh не владеет — сопровождение
останавливалось (дефект Д-29). А фразу в терминале агент с теми же правами может
набрать программно. Теперь «да» — нажатие «Разрешить» в боте согласований: туда владелец и так
нажимает решения, и нажатие приходит из его аккаунта Telegram, а не с сервера.

Как устроено.

  * Скрипт `ops/` (функция `owner_consent` в `ops/lib.sh`) запускает в контейнере сервиса
    `shturman ops-consent ask --action … --target … --detail ключ=значение …`. Команда просит сервис
    (`POST /api/ops/consents`) создать запрос: строка `ops_consents` и подтверждение вида `ops.consent`
    — карточка в боте с кнопками «Разрешить» / «Отклонить». Текст карточки собирает сервис по виду
    действия из проверенных полей, а не берёт готовым.
  * Нажать может только привязанный владелец в личном чате с ботом (общие правила `confirm.py`), и
    только в самом боте — не в приложении «Штурман» (`BOT_ONLY`): войти в приложение может и держатель
    токена бота. Функция применения `ops.consent` ещё раз проверяет, что её вызвало нажатие под
    карточкой (`confirm.card_press`), и только тогда ставит `granted`.
  * Разрешение одноразовое, на одно действие и одну цель (коммит выпуска, метку копии, число), с
    истечением: ждёт нажатия час (`confirm.TTL`), после нажатия годно два часа (`GRANT_TTL`). Скрипт
    расходует его (`POST /api/ops/consents/{id}/use`) с той же целью; другая цель — отказ.
  * Маршрута, который выдаёт разрешение без нажатия, нет: внутренний API (а значит и `shturman call`,
    и ассистент в Hermes) может только попросить карточку, узнать её исход и израсходовать уже выданное.
    Разрешение, выпрошенное ассистентом, ему бесполезно: скрипты `ops/` он не запускает.

Когда сервис не работает (база сломана, сервис не стартует) — путь без сервиса: тот же скрипт
запускает одноразовый контейнер из образа сервиса (`docker compose run … ops-consent ask --direct`),
и команда сама говорит с Telegram от имени бота согласований (токен — из окружения контейнера и
файла страницы настройки; он не печатается, агент его не видит): шлёт владельцу карточку и ждёт
нажатия через `getUpdates`. Опрос без `offset` ничего не подтверждает: обновления, которые придут боту
за это время (в том числе сообщения бизнес-режима), остаются сервису. Если бота опрашивает кто-то ещё
(сервис жив) — Telegram отвечает 409, и команда отказывает: идти нужно через сервис.

Кто владелец на пути без сервиса. Сначала — база (если контейнер базы жив): тот же владелец, которого
проверяет бот (`binding.bound_owner`). Без базы — копия `<каталог данных>/setup/owner.json`: её пишет
сам сервис (бот согласований при запуске, после привязки и при каждой сверке кнопки меню), стирает при
отвязке. Почему там, а не в `local/` или `.env`: каталог данных сервиса подключён только контейнеру
сервиса (ассистенту в Hermes он не виден), копия ложится в резервные копии вместе с базой и
восстанавливается согласованно с ней, а `local/` и `.env` — область скриптов, куда сервис не пишет.
Предел: оператор с правами на сервере может подменить и этот файл, и саму базу — как и всё остальное
(`docs/security-review.md`, «Противники»: ИИ-агент оператора).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import asyncpg
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from . import authority, confirm
from .api_core import BadRequest, body, handler
from .app import state_of
from .sanitize import clean_line

logger = logging.getLogger("shturman.ops_consent")

KIND = "ops.consent"
GRANT_TTL = 2 * 3600          # секунд после нажатия: столько разрешение можно израсходовать
DETAIL_LIMIT = 200            # знаков в одном поле подробностей
OWNER_FILE = "owner.json"     # копия «кто владелец бота» для пути без сервиса: <каталог данных>/setup/

_SHA = r"^[0-9a-f]{40}$"
_STAMP = r"^[0-9]{8}T[0-9]{6}Z$"


@dataclass(frozen=True)
class Action:
    title: str                 # коротко, для строки «Ждём подтверждения владельца в боте: …»
    target: str                # регулярное выражение цели
    details: tuple[str, ...]   # какие поля подробностей принимаются


ACTIONS: dict[str, Action] = {
    "update": Action("обновление с откатом базы при провале", _SHA,
                     ("from", "to", "migrations", "hermes", "scope")),
    "restore": Action("восстановление из резервной копии", r"^[0-9]{8}T[0-9]{6}Z:(all|db|data):(env|keep)$",
                      ("created", "parts", "env", "version")),
    "restore_nocopy": Action("восстановление без копии нынешнего состояния", _STAMP, ("reason",)),
    "rollback": Action("откат последнего обновления", _SHA, ("from", "to", "stamp", "minutes", "scope")),
    "no_backup": Action("перезапуск без свежей резервной копии", r"^[0-9a-f]{16}$", ("need",)),
    "guard_threshold": Action("смена порога защиты", r"^(0\.[5-9][0-9]{0,3}|1|1\.0)$", ("old",)),
    "backups_delete": Action("удаление старых резервных копий", r"^[0-9]{1,3}:[0-9a-f]{16}$",
                             ("count", "size", "oldest", "newest")),
}

ONCE = "Разрешение — только на это и один раз; после нажатия действует 2 часа."
DIRECT_NOTE = ("Сервис «Штурмана» сейчас не работает, поэтому запрос прислал напрямую скрипт на сервере. "
               "Если вы ни о чём таком не просили — нажмите «Отклонить».")


def check(action: str, target: str, details: Any) -> dict[str, str]:
    """Проверяет вид, цель и подробности. Возвращает очищенные подробности или бросает ValueError."""
    spec = ACTIONS.get(action)
    if spec is None:
        raise ValueError(f"неизвестный вид действия: {action}")
    if not isinstance(target, str) or not re.match(spec.target, target):
        raise ValueError(f"цель действия {action} записана не так")
    if details is None:
        details = {}
    if not isinstance(details, dict):
        raise ValueError("подробности — объект «поле: строка»")
    out: dict[str, str] = {}
    for key, value in details.items():
        if key not in spec.details:
            raise ValueError(f"поле подробностей {key!r} для {action} не принимается")
        if not isinstance(value, str):
            raise ValueError(f"поле подробностей {key!r} — строка")
        cleaned = clean_line(value, DETAIL_LIMIT)
        if cleaned:
            out[key] = cleaned
    return out


def summary(action: str, target: str, d: dict[str, str]) -> str:
    """Текст карточки простыми словами. Собирается здесь, по виду действия, а не берётся готовым."""
    g = d.get
    if action == "update":
        lines = [f"Обновление «Штурмана»: {g('from', 'нынешняя версия')} → {g('to', 'новая версия')}."]
        if g("scope", "data") == "data":
            lines.append("Что вы разрешаете: если проверка после обновления не пройдёт, сервер сам вернёт прежнюю "
                         "версию ВМЕСТЕ С БАЗОЙ архива и данными из копии, которую снимет прямо перед обновлением. "
                         "То, что придёт в архив за время обновления (обычно минуты), может пропасть.")
        if g("migrations"):
            lines.append(f"Изменения базы: {g('migrations')}.")
        if g("hermes"):
            lines.append(f"Ещё — запуск Hermes {g('hermes')}: этой версии нет в списке проверенных, ассистент с ней "
                         "не проверен; вернуть прежний Hermes можно только из копии.")
        lines.append(f"Цель — коммит {target[:12]}. {ONCE}")
        return "\n\n".join(lines)
    if action == "restore":
        stamp, only, env = target.split(":")
        lines = [f"Восстановление из резервной копии {stamp}" + (f" (сделана {g('created')})" if g("created") else "") + "."]
        lines.append(f"Заменятся копией: {g('parts', 'база архива и каталоги данных')}. Всё, что пришло в архив после "
                     "этой копии, пропадёт.")
        lines.append("Файл настроек .env: " + (g("env") or ("заменяется файлом из копии" if env == "env" else "остаётся нынешний")) + ".")
        lines.append("Перед заменой сервер снимет копию нынешнего состояния.")
        if g("version"):
            lines.append(f"Версия кода копии: {g('version')}.")
        lines.append(ONCE)
        return "\n\n".join(lines)
    if action == "restore_nocopy":
        return "\n\n".join([
            f"Продолжить восстановление из копии {target} БЕЗ копии нынешнего состояния: снять её не удалось"
            + (f" ({g('reason')})" if g("reason") else "") + ".",
            "Нынешние данные после восстановления вернуть будет нечем.", ONCE])
    if action == "rollback":
        lines = [f"Откат последнего обновления: {g('to', 'нынешняя версия')} → {g('from', 'прежняя версия')}."]
        if g("scope") == "data":
            lines.append(f"База архива и данные вернутся из копии {g('stamp', 'до обновления')}, снятой перед обновлением"
                         + (f" ({g('minutes')} мин назад)" if g("minutes") else "")
                         + ": всё, что пришло в архив после неё, пропадёт. Перед откатом сервер снимет копию нынешнего состояния.")
        else:
            lines.append("Возвращается только код; база и данные не меняются.")
        lines.append(f"Прежний коммит — {target[:12]}. {ONCE}")
        return "\n\n".join(lines)
    if action == "no_backup":
        return "\n\n".join([
            "Перезапуск «Штурмана» без свежей резервной копии.",
            f"Ждут: {g('need', 'изменения базы или смена Hermes')}.",
            "Если что-то пойдёт не так, вернуть базу и данные будет не из чего, кроме старых копий. Обычно "
            "правильнее сначала снять копию — попросите об этом того, кто сопровождает сервер.", ONCE])
    if action == "guard_threshold":
        return "\n\n".join([
            f"Порог защиты от внедрённых инструкций: {g('old', 'прежний')} → {target}.",
            "Выше порог — меньше скрытых обычных сообщений и больше пропущенных атак. Расчёт на вашей "
            "переписке — ./ops/guard.sh stats.", ONCE])
    if action == "backups_delete":
        keep = target.split(":")[0]
        span = f", с {g('oldest')} по {g('newest')}" if g("oldest") and g("newest") else ""
        return "\n\n".join([
            f"Удалить старые резервные копии: {g('count', '?')} шт." + (f" ({g('size')})" if g("size") else "") + span + ".",
            f"Останутся {keep} последних. Удалённую копию не вернуть.", ONCE])
    raise ValueError(f"неизвестный вид действия: {action}")


# --- запрос, исход, расход ---

async def create(conn: asyncpg.Connection, action: str, target: str, details: dict[str, str]) -> dict[str, Any]:
    """Запрос разрешения. Есть ждущий или ещё не израсходованный с тем же видом и целью — он и
    возвращается, новой карточки нет. Бросает confirm.NoOwner / TooManyPending, ValueError."""
    text = summary(action, target, details)
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"shturman.ops_consent:{action}:{target}")
        rows = await conn.fetch(
            "SELECT id FROM ops_consents WHERE action = $1 AND target = $2 AND status <> 'used' ORDER BY id DESC",
            action, target)
        for row in rows:
            st = await state(conn, row["id"])
            if st and st["status"] in ("pending", "granted"):
                return st
        row = await conn.fetchrow(
            """INSERT INTO ops_consents (action, target, details, summary, expires_at)
               VALUES ($1, $2, $3::jsonb, $4, now() + make_interval(secs => $5)) RETURNING id""",
            action, target, json.dumps(details, ensure_ascii=False, sort_keys=True), text, float(confirm.TTL))
        waiting = await confirm.request_card(conn, KIND, text, {"consent_id": row["id"]})
        await conn.execute(
            "UPDATE ops_consents SET action_id = $2, expires_at = $3::timestamptz WHERE id = $1",
            row["id"], waiting["action_id"], datetime.fromisoformat(waiting["expires_at"]))
    out = await state(conn, row["id"])
    assert out is not None
    return out


async def state(conn: asyncpg.Connection, consent_id: int) -> dict[str, Any] | None:
    """Исход запроса: pending, granted, used, rejected, expired или failed. Без подробностей."""
    r = await conn.fetchrow(
        """SELECT c.id, c.action, c.target, c.summary, c.status, c.expires_at, c.valid_until, c.used_at,
                  c.valid_until > now() AS valid, c.expires_at > now() AS open,
                  a.status AS card, a.expires_at > now() AS card_open
           FROM ops_consents c LEFT JOIN pending_actions a ON a.id = c.action_id WHERE c.id = $1""", consent_id)
    if r is None:
        return None
    if r["status"] == "used":
        status = "used"
    elif r["status"] == "granted":
        status = "granted" if r["valid"] else "expired"
    elif r["card"] == "pending" and r["card_open"] and r["open"]:
        status = "pending"
    elif r["card"] in ("rejected", "failed"):
        status = r["card"]
    else:
        status = "expired"
    return {"consent_id": r["id"], "action": r["action"], "target": r["target"], "status": status,
            "title": ACTIONS[r["action"]].title if r["action"] in ACTIONS else r["action"],
            "summary": r["summary"], "expires_at": r["expires_at"].isoformat(),
            "valid_until": r["valid_until"].isoformat() if r["valid_until"] else None,
            "used_at": r["used_at"].isoformat() if r["used_at"] else None}


async def latest(conn: asyncpg.Connection, action: str, target: str) -> dict[str, Any] | None:
    """Последний запрос с этим видом и целью (для `./ops/update.sh --check`)."""
    consent_id = await conn.fetchval(
        "SELECT id FROM ops_consents WHERE action = $1 AND target = $2 ORDER BY id DESC LIMIT 1", action, target)
    return None if consent_id is None else await state(conn, consent_id)


async def use(conn: asyncpg.Connection, consent_id: int, action: str, target: str) -> bool:
    """Израсходовать выданное разрешение: только своё (тот же вид и цель), годное и один раз."""
    used = await conn.fetchval(
        """UPDATE ops_consents SET status = 'used', used_at = now()
           WHERE id = $1 AND action = $2 AND target = $3 AND status = 'granted' AND valid_until > now()
           RETURNING id""", consent_id, action, target)
    return used is not None


@confirm.applier(KIND)
async def _grant(conn: asyncpg.Connection, payload: dict[str, Any]) -> confirm.Done:
    """Нажатие «Разрешить» под карточкой в самом боте согласований — и ничто другое."""
    if not confirm.card_press():
        raise confirm.Refused("Разрешение на сопровождение даёт только нажатие владельца в боте согласований.",
                              403, "owner_press_required")
    principal = authority.requires_owner()
    if principal.source != "telegram" or principal.action == "telegram.miniapp":
        raise confirm.Refused("Разрешить можно только кнопкой в самом боте согласований.", 403, "bot_only")
    until = await conn.fetchval(
        """UPDATE ops_consents SET status = 'granted', granted_at = now(),
                  valid_until = now() + make_interval(secs => $2)
           WHERE id = $1 AND status = 'pending' AND expires_at > now() RETURNING valid_until""",
        payload.get("consent_id"), float(GRANT_TTL))
    if until is None:
        raise confirm.Refused("Запрос уже недействителен — пусть команду на сервере запустят снова.", 409, "consent_gone")
    return confirm.Done(note="Разрешение действует 2 часа и один раз. Сервер продолжит сам — "
                             "или его продолжит тот, кто сопровождает сервер, повтором той же команды.")


# --- маршруты внутреннего API: попросить, узнать, израсходовать. Выдать — нельзя ---

def _int_id(request: Request) -> int:
    return int(request.path_params["consent_id"])


@handler
async def ask(request: Request) -> JSONResponse:
    data = await body(request)
    action, target = data.get("action"), data.get("target")
    try:
        details = check(action, target, data.get("details"))
    except ValueError as exc:
        raise BadRequest(str(exc)) from None
    try:
        async with state_of(request).pool.acquire() as conn:
            out = await create(conn, action, target, details)
    except confirm.NoOwner:
        raise BadRequest("Разрешение даёт владелец кнопкой в боте согласований, а бот не настроен или владелец к "
                         "нему не привязан: подтвердить некому.", 409, "owner_unknown") from None
    except confirm.TooManyPending:
        raise BadRequest("Слишком много действий уже ждут владельца в боте согласований. Пусть он ответит на "
                         "карточки, затем повторите.", 429, "too_many_pending") from None
    return JSONResponse(out, status_code=202 if out["status"] == "pending" else 200)


@handler
async def get_one(request: Request) -> JSONResponse:
    async with state_of(request).ro_pool.acquire() as conn:
        out = await state(conn, _int_id(request))
    if out is None:
        raise BadRequest("такого запроса нет", 404)
    return JSONResponse(out)


@handler
async def find(request: Request) -> JSONResponse:
    action, target = request.query_params.get("action", ""), request.query_params.get("target", "")
    try:
        check(action, target, {})
    except ValueError as exc:
        raise BadRequest(str(exc)) from None
    async with state_of(request).ro_pool.acquire() as conn:
        out = await latest(conn, action, target)
    return JSONResponse(out or {"status": "none", "action": action, "target": target})


@handler
async def use_one(request: Request) -> JSONResponse:
    data = await body(request)
    action, target = data.get("action"), data.get("target")
    if not isinstance(action, str) or not isinstance(target, str):
        raise BadRequest("нужны поля action и target")
    async with state_of(request).pool.acquire() as conn:
        ok = await use(conn, _int_id(request), action, target)
        out = await state(conn, _int_id(request))
    if out is None:
        raise BadRequest("такого запроса нет", 404)
    return JSONResponse(out, status_code=200 if ok else 409)


def routes() -> list[BaseRoute]:
    return [
        Route("/api/ops/consents", ask, methods=["POST"]),
        Route("/api/ops/consents", find, methods=["GET"]),
        Route("/api/ops/consents/{consent_id:int}", get_one, methods=["GET"]),
        Route("/api/ops/consents/{consent_id:int}/use", use_one, methods=["POST"]),
    ]


# --- копия «кто владелец бота» для пути без сервиса ---

def owner_path(data_dir: Path) -> Path:
    return Path(data_dir) / "setup" / OWNER_FILE


def remember_owner(data_dir: Path, bot_id: int | None, owner: dict[str, int] | None) -> None:
    """Пишет (или стирает, если владельца нет) копию «бот → владелец». Только если изменилась."""
    path = owner_path(data_dir)
    if not bot_id or not owner:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    want = {"bot_id": int(bot_id), "user_id": int(owner["user_id"]), "chat_id": int(owner["chat_id"])}
    try:
        if json.loads(path.read_text("utf-8")) == want:
            return
    except (OSError, ValueError):
        pass
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fp:
        json.dump(want, fp)
    os.replace(tmp, path)


def read_owner_copy(data_dir: Path, bot_id: int) -> dict[str, int] | None:
    try:
        saved = json.loads(owner_path(data_dir).read_text("utf-8"))
        if int(saved["bot_id"]) != int(bot_id):
            return None          # копия о другом боте: владелец этого бота неизвестен
        user_id, chat_id = int(saved["user_id"]), int(saved["chat_id"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return {"user_id": user_id, "chat_id": chat_id} if user_id > 0 and chat_id == user_id else None


# --- путь без сервиса: карточка и нажатие напрямую через Bot API ---

class DirectRefused(Exception):
    """Путь без сервиса невозможен. code — код выхода команды, text — простыми словами."""

    def __init__(self, code: int, text: str) -> None:
        super().__init__(text)
        self.code, self.text = code, text


# Коды выхода `shturman ops-consent ask` (их же разбирает `owner_consent` в ops/lib.sh).
GRANTED, WAITING, DENIED, NO_BOT, NO_SERVICE, CONFLICT = 0, 4, 5, 6, 7, 8
CALLBACK = "oc"              # данные кнопок пути без сервиса: oc:y:<метка> / oc:n:<метка>; не «sh:» — сервис их не примет


async def direct_ask(api: Any, owner: dict[str, int], action: str, target: str, details: dict[str, str], *,
                     wait: float, say: Callable[[str], None], tz: str,
                     clock: Callable[[], float] = time.monotonic,
                     sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, every: float = 2.0) -> int:
    """Карточка владельцу от имени бота согласований и ожидание нажатия. Возвращает код выхода."""
    from .executor.botapi import BotApiError, Refused

    try:
        probe = await api.call("getUpdates", {"timeout": 0, "limit": 1})
    except Refused as exc:
        if exc.code == 409:
            raise DirectRefused(CONFLICT, "Бота согласований сейчас опрашивает кто-то ещё (обычно — сам сервис, "
                                          "он жив) или у бота включён webhook: напрямую ждать нажатия нельзя. "
                                          "Запустите команду, когда сервис отвечает, — запрос пойдёт через него.") from None
        raise DirectRefused(NO_BOT, f"Telegram не принял запрос бота согласований ({exc.reason}).") from None
    except BotApiError as exc:
        raise DirectRefused(NO_BOT, f"Telegram недоступен с сервера ({exc}).") from None
    del probe
    nonce = secrets.token_urlsafe(12)
    text = "Подтвердите действие\n\n" + summary(action, target, details) + "\n\n" + DIRECT_NOTE
    deadline = clock() + max(0.0, min(wait, float(confirm.TTL)))
    until = _clock_text(time.time() + max(0.0, min(wait, float(confirm.TTL))), tz)
    try:
        # Без предпросмотра ссылок (Д-39): в карточке адреса сервера и коммиты — Telegram не должен ходить по ним.
        message_id = await api.send_message(owner["chat_id"], text, buttons=[
            [("Разрешить", f"{CALLBACK}:y:{nonce}"), ("Отклонить", f"{CALLBACK}:n:{nonce}")]], no_preview=True)
    except BotApiError as exc:
        raise DirectRefused(NO_BOT, f"Карточку в бот согласований отправить не удалось ({exc}).") from None
    say(f"Ждём подтверждения владельца в боте согласований: {ACTIONS[action].title}; ждём до {until}. "
        "Сервис не работает — запрос отправлен напрямую от имени бота.")
    answered: set[str] = set()
    while True:
        try:
            updates = await api.call("getUpdates", {"timeout": 0, "limit": 100})
        except Refused as exc:
            if exc.code == 409:
                await _close(api, owner, message_id, "Запрос снят: бота снова опрашивает сервис.\n\n" + text)
                raise DirectRefused(CONFLICT, "Пока ждали, бота стал опрашивать сервис (он ожил). Запустите команду "
                                              "снова — запрос пойдёт через сервис.") from None
            updates = []
        except BotApiError:
            updates = []       # временный сбой сети: попробуем ещё раз
        updates = [u for u in updates if isinstance(u, dict)] if isinstance(updates, list) else []
        for update in updates:
            query = update.get("callback_query")
            if not isinstance(query, dict) or query.get("data") not in (f"{CALLBACK}:y:{nonce}", f"{CALLBACK}:n:{nonce}"):
                continue
            qid = query.get("id")
            sender = query.get("from") if isinstance(query.get("from"), dict) else {}
            message = query.get("message") if isinstance(query.get("message"), dict) else {}
            chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
            mine = (sender.get("id") == owner["user_id"] and chat.get("id") == owner["chat_id"]
                    and chat.get("type") == "private" and message.get("message_id") == message_id)
            if not mine:
                if isinstance(qid, str) and qid not in answered:
                    answered.add(qid)
                    await _answer(api, qid, "Кнопка недоступна.")
                continue
            yes = query["data"] == f"{CALLBACK}:y:{nonce}"
            if isinstance(qid, str):
                await _answer(api, qid, "Разрешено." if yes else "Отклонено.")
            await _close(api, owner, message_id, ("Разрешено:\n" if yes else "Отклонено:\n") + summary(action, target, details))
            if yes:
                say("Разрешено владельцем в боте согласований.")
                return GRANTED
            say("Владелец отклонил запрос в боте согласований. Ничего не изменено.")
            return DENIED
        if len(updates) >= 100:
            await _close(api, owner, message_id, "Запрос снят: у бота слишком много непрочитанных обновлений.\n\n" + text)
            raise DirectRefused(CONFLICT, "У бота согласований накопилось 100 и больше непрочитанных обновлений: без "
                                          "сервиса их нельзя пропустить, не потеряв (среди них могут быть сообщения "
                                          "бизнес-режима). Поднимите сервис (./ops/up.sh) и повторите — запрос пойдёт через него.")
        if clock() >= deadline:
            await _close(api, owner, message_id, "Срок вышел — запрос снят:\n" + summary(action, target, details))
            say("Владелец не ответил за отведённое время; запрос в боте снят. Запустите команду снова — придёт новый запрос.")
            return WAITING
        await sleep(every)


async def _answer(api: Any, query_id: str, text: str) -> None:
    from .executor.botapi import BotApiError
    try:
        await api.answer_callback_query(query_id, text)
    except BotApiError:
        pass


async def _close(api: Any, owner: dict[str, int], message_id: int, text: str) -> None:
    from .bridge import fit_message
    from .executor.botapi import BotApiError
    try:
        await api.edit_message_text(owner["chat_id"], message_id, fit_message(text))
    except BotApiError:
        pass


def _clock_text(epoch: float, tz: str) -> str:
    """«14:35 (Europe/Moscow)» — время по поясу владельца."""
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(tz)
    except Exception:  # noqa: BLE001 — неизвестный пояс: по UTC
        zone, tz = timezone.utc, "UTC"
    return datetime.fromtimestamp(epoch, zone).strftime("%H:%M") + f" ({tz})"


def iso_clock(iso: str | None, tz: str) -> str:
    if not iso:
        return "—"
    try:
        return _clock_text(datetime.fromisoformat(iso).timestamp(), tz)
    except ValueError:
        return iso


def target_hash(text: str) -> str:
    """Короткий отпечаток для целей вида «что именно» (список копий, перечень ожидающего)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

