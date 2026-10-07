"""Серверная часть мастера настройки. Hermes монтирует её в /api/plugins/shturman/.

Здесь только то, чего нет в самом дашборде Hermes. Ключи модели, токен бота, список разрешённых
пользователей и перезапуск шлюза страница мастера отправляет в штатные вызовы Hermes напрямую —
этот файл их не видит и не хранит.

Переписка настраивается не здесь, а на отдельной странице сервиса переписки (`/shturman-setup/`),
мимо Hermes и на своём адресе — не на адресе дашборда. Мастер показывает только её состояние
(`/correspondence`: признаки и числа) и обычную ссылку на неё; ссылку входа он не запрашивает
и не показывает, запросы на эту страницу не передаёт.

Все маршруты закрыты общим входом дашборда: без сессии Hermes до них не допускает.

Для страниц владельца здесь же лежит узкий проход к сервису переписки (`/service/...`):
только перечисленные в `service_routes.UI` маршруты, токен сервиса подставляет сервер,
в браузер он не попадает. Тела запросов передаются потоком как есть: не читаются, не разбираются
и нигде не записываются. Входа в аккаунт Telegram, управления аккаунтами, выбора их чатов
и импорта выгрузки в этом проходе нет (`service_routes.SETUP_PAGE_ONLY`): это делается только
на странице настройки переписки, и ни QR-код входа, ни облачный пароль через дашборд не идут.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from shturman_core import (  # noqa: E402
    botapi, bridge_stats, correspondence, cron_jobs, personas, service_client, service_routes, wizard,
)
from shturman_core.pairing import Pairing  # noqa: E402
from shturman_core.state import Store  # noqa: E402

router = APIRouter()
logger = logging.getLogger("shturman.dashboard")

PROBE_TIMEOUT = 90
OWNER_PUSH_TIMEOUT = 3
SETUP_STATUS_TIMEOUT = 3
PROXY_CONNECT_TIMEOUT = 3
PROXY_IO_TIMEOUT = 130          # сборка страниц памяти и долгие запросы сервис держит до минуты и дольше
MAX_QUERY = 2000


def _store() -> Store:
    return Store()


def _bot_token() -> str:
    from hermes_cli.config import get_env_value_prefer_dotenv

    return (get_env_value_prefer_dotenv("TELEGRAM_BOT_TOKEN") or "").strip()


# ------------------------------------------------------------------ состояние

def _state() -> dict[str, Any]:
    store = _store()
    out = wizard.snapshot(store)
    # Мост к сервису переписки: только числа и признаки, без обращения к самому сервису.
    out["service"] = bridge_stats.status(store, configured=service_client.configured())
    return out


@router.get("/state")
async def get_state() -> dict[str, Any]:
    return _state()


class MarkBody(BaseModel):
    key: str


@router.post("/mark")
async def post_mark(body: MarkBody) -> dict[str, Any]:
    try:
        wizard.mark(_store(), body.key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _state()


# ------------------------------------------------------------- имя и характер

class PersonaBody(BaseModel):
    persona: str = ""
    custom_name: str = ""
    custom_voice: str = ""
    tone: str = ""
    owner_address: str = ""
    intro: str = ""
    custom_intro: str = ""
    owner_genitive: str = ""
    soul: str = ""          # текущий текст SOUL.md, прочитанный страницей штатным вызовом


@router.post("/persona")
async def post_persona(body: PersonaBody) -> dict[str, Any]:
    """Сохраняет выбор и возвращает новый текст SOUL.md; записывает его страница штатным вызовом."""
    raw = body.model_dump()
    soul = raw.pop("soul")
    store = _store()
    choice = personas.save_choice(store, raw)
    wizard.mark(store, "persona_saved")
    return {
        "persona": choice,
        "resolved": personas.resolved(choice),
        "soul": personas.apply_to_soul(soul, choice),
    }


# ------------------------------------------------------------------------ бот

class BotCheckBody(BaseModel):
    token: Optional[str] = None     # пусто — проверить уже сохранённый токен


@router.post("/bot/check")
async def post_bot_check(body: BotCheckBody) -> dict[str, Any]:
    """Спрашивает у Telegram, чей это токен. Токен никуда не записывается."""
    token = (body.token or "").strip() or _bot_token()
    if not token:
        return {"ok": False, "error": "Токен не задан."}
    if not botapi.TOKEN_RE.match(token):
        return {"ok": False, "error": "Это не похоже на токен бота: в нём цифры, двоеточие и длинная строка."}
    try:
        me = await asyncio.to_thread(botapi.get_me, token)
    except botapi.BotApiError as exc:
        return {"ok": False, "error": f"Telegram не принял токен: {exc}"}
    username = str(me.get("username") or "")
    name = str(me.get("first_name") or "")
    wizard.remember_bot(_store(), username, name)
    return {"ok": True, "username": username, "name": name}


# ------------------------------------------------------------------- привязка

@router.post("/pairing/start")
async def post_pairing_start() -> dict[str, Any]:
    store = _store()
    bot = store.read("wizard").get("bot") or {}
    username = str(bot.get("username") or "")
    if not username:
        check = await post_bot_check(BotCheckBody())
        if not check.get("ok"):
            raise HTTPException(status_code=409, detail="Сначала сохраните и проверьте токен бота.")
        username = check["username"]
    started = Pairing(store).start()
    return {
        "bot_username": username,
        "deep_link": wizard.deep_link(username, started["token"]),
        "code": started["code"],
        "expires_at": started["expires_at"],
    }


@router.get("/pairing")
async def get_pairing() -> dict[str, Any]:
    return Pairing(_store()).status()


@router.post("/pairing/confirm")
async def post_pairing_confirm() -> dict[str, Any]:
    """Владелец в мастере подтвердил, что аккаунт, написавший боту, — его."""
    pairing = Pairing(_store())
    owner = pairing.confirm()
    if owner is None:
        raise HTTPException(status_code=409, detail="Подтверждать нечего: время привязки вышло. Получите новую ссылку.")
    await _push_owner(owner)
    return pairing.status()


async def _push_owner(owner: dict[str, Any]) -> bool:
    """Сообщает сервису переписки нового владельца сразу после привязки. Не получилось — не беда:
    шлюз передаёт владельца при запуске и при каждой смене привязки."""
    try:
        client = service_client.ServiceClient.from_env(service_routes.BRIDGE, timeout=OWNER_PUSH_TIMEOUT)
        if client is None:
            return False
        await asyncio.to_thread(client.request, "PUT", "/api/owner",
                                json_body={"user_id": owner["user_id"], "chat_id": owner["chat_id"]})
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("shturman: владелец не передан сервису переписки (%s)", type(exc).__name__)
        return False


@router.post("/pairing/reject")
async def post_pairing_reject() -> dict[str, Any]:
    pairing = Pairing(_store())
    pairing.reject()
    return pairing.status()


# ------------------------------------------------------------ проверка модели

@router.post("/model/probe")
async def post_model_probe() -> dict[str, Any]:
    """Короткий настоящий диалог через Hermes: проверяет весь путь от настроек до ответа модели."""
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "hermes_cli.main", "chat", "-Q", "--max-turns", "1",
            "-q", wizard.PROBE_PROMPT,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        return {"ok": False, "error": f"Не удалось запустить проверку ({type(exc).__name__})."}
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=PROBE_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"ok": False, "error": "Модель не ответила за полторы минуты."}
    result = wizard.parse_probe_output(proc.returncode or 0, out.decode("utf-8", "replace"))
    if result["ok"]:
        wizard.mark(_store(), "model_ok")
    return result


# ------------------------------------------------- шаг мастера «Переписка»

def _correspondence_sync() -> dict[str, Any]:
    # Адрес, по которому владелец открывает дашборд: его Hermes получает из SHTURMAN_PUBLIC_URL.
    # Он нужен только для сверки: на адрес дашборда ссылка на страницу настройки не строится.
    dashboard_url = os.environ.get("HERMES_DASHBOARD_PUBLIC_URL", "")
    try:
        client = service_client.ServiceClient.from_env(service_routes.UI, timeout=SETUP_STATUS_TIMEOUT)
    except ValueError:
        client = None
    if client is None:
        return correspondence.summary(None, dashboard_url=dashboard_url, state=correspondence.NO_SERVICE)
    try:
        status = client.request("GET", correspondence.STATUS_PATH)
    except service_client.ServiceError as exc:
        logger.warning("shturman: состояние настройки переписки не получено (%s)", exc.code or type(exc).__name__)
        return correspondence.summary(None, dashboard_url=dashboard_url, state=correspondence.UNREACHABLE)
    return correspondence.summary(status, dashboard_url=dashboard_url)


@router.get("/correspondence")
async def get_correspondence() -> dict[str, Any]:
    """Что известно о странице настройки переписки: состояние, адрес, признаки и числа.

    Один запрос `GET /api/status` к сервису переписки. Адрес страницы — из его поля
    `setup.origin`, и только если это верный https-адрес, не совпадающий с адресом дашборда.
    Ссылки входа на страницу здесь нет и запросить её отсюда нельзя: её выдаёт
    `./ops/setup-link.sh` на сервере.
    """
    return await asyncio.to_thread(_correspondence_sync)


# ------------------------------------------------------- сервис переписки

@router.api_route("/service/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def service_proxy(path: str, request: Request) -> StreamingResponse:
    """Проход к сервису переписки для страниц владельца. Разрешено только перечисленное
    в `service_routes.UI`; владелец сервиса, очередь заданий, нажатия кнопок и приём
    сообщений отсюда недоступны. Путь всегда начинается с `/api/`: страница настройки переписки
    (`/shturman-setup/`) и архив (`/mcp`) через этот проход не открываются.

    Вход в аккаунт Telegram, управление аккаунтами и импорт выгрузки отсюда тоже недоступны:
    с версии 0.0.6 это делается только на странице настройки переписки.

    Тело запроса и тело ответа идут потоком и в память целиком не читаются и в журнал не пишутся.

    Код и тело ответа сервиса передаются как есть. В том числе 202 с телом
    {"status": "pending_confirmation", "action_id", "summary", ...}: у сервиса свой бот
    согласований, действие не применено и ждёт нажатия владельца в боте. Страница должна
    показать это как «ждёт подтверждения», а не как «сохранено»; ждущие действия отдаёт
    `service/confirmations`, отменить можно через `service/confirmations/{id}/cancel`.
    Подтвердить действие отсюда нельзя.
    """
    target = "/api/" + path
    if not service_routes.allowed(service_routes.UI, request.method, target):
        raise HTTPException(status_code=404, detail="Такого адреса у сервиса переписки нет.")
    query = request.url.query or ""
    if len(query) > MAX_QUERY or not query.isascii() or any(ch.isspace() for ch in query):
        raise HTTPException(status_code=400, detail="Неверные параметры запроса.")
    base_url, token = service_client.settings()
    if not base_url or not token:
        raise HTTPException(status_code=503, detail="Сервис переписки не подключён.")

    import httpx  # зависимость самого Hermes (pyproject.toml:44), отдельно не ставится

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    # Из запроса браузера берутся только вид и длина тела: ни куки, ни его заголовок Authorization
    # сервису не передаются.
    has_body = request.method in ("POST", "PUT", "DELETE")
    for name in ("content-type", "content-length") if has_body else ():
        value = request.headers.get(name)
        if value:
            headers[name] = value
    client = httpx.AsyncClient(
        trust_env=False,            # без прокси из окружения: адрес локальный
        follow_redirects=False,     # токен не должен уйти по чужому адресу
        timeout=httpx.Timeout(PROXY_IO_TIMEOUT, connect=PROXY_CONNECT_TIMEOUT),
    )
    try:
        upstream = await client.send(
            client.build_request(request.method, base_url + target + (f"?{query}" if query else ""),
                                 headers=headers, content=request.stream() if has_body else None),
            stream=True)
    except Exception as exc:  # noqa: BLE001
        await client.aclose()
        # Только вид ошибки: ни тела запроса, ни заголовков в журнале быть не должно.
        logger.warning("shturman: сервис переписки не ответил (%s)", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Сервис переписки недоступен.") from None
    if upstream.status_code == 401 or 300 <= upstream.status_code < 400:
        await upstream.aclose()
        await client.aclose()
        # 401 наружу не отдаём: дашборд принял бы его за конец сессии владельца.
        raise HTTPException(status_code=502,
                            detail="Сервис переписки не принял запрос плагина: проверьте его токен.")

    async def body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        body(), status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type") or "application/json",
        headers={"Cache-Control": "no-store"})


# ------------------------------------------------ сводки по расписанию

class CronInstallBody(BaseModel):
    morning_at: Optional[str] = None      # «ЧЧ:ММ», по умолчанию 08:27
    weekly_at: Optional[str] = None       # «ЧЧ:ММ», по умолчанию 17:47
    weekly_day: Optional[int] = None      # 0 — воскресенье … 6 — суббота; по умолчанию пятница


_cron_lock = threading.Lock()


def _cron_context() -> dict[str, Any]:
    from hermes_cli.config import get_env_value_prefer_dotenv
    from hermes_time import get_timezone_name

    timezone = get_timezone_name()
    return {
        # Время задач считается по этим часам. Пусто — по часам сервера.
        "timezone": timezone or None,
        "timezone_configured": bool(timezone),
        "home_channel_set": bool((get_env_value_prefer_dotenv("TELEGRAM_HOME_CHANNEL") or "").strip()),
    }


def _cron_status_sync() -> dict[str, Any]:
    from cron.jobs import list_jobs

    create, present = cron_jobs.plan(list_jobs(include_disabled=True))
    return {"installed": present, "missing": [spec["name"] for spec in create], **_cron_context()}


def _cron_install_sync(morning_at: Optional[str] = None, weekly_at: Optional[str] = None,
                       weekly_day: Optional[int] = None) -> dict[str, Any]:
    """Заводит сводку и обзор недели штатной функцией Hermes — той же, какой пользуются дашборд
    и «чертежи» скиллов (cron/scheduler.py:3800, tools/blueprints.py:128-136). Повторный вызов
    ничего не дублирует; уже существующую задачу не трогает."""
    from cron.jobs import list_jobs
    from cron.scheduler import create_job_with_scheduler_registration

    context = _cron_context()
    times = {"morning-brief": morning_at, "weekly-review": weekly_at}
    weekday = cron_jobs.WEEKLY_DAY if weekly_day is None else weekly_day
    with _cron_lock:
        try:
            create, present = cron_jobs.plan(
                list_jobs(include_disabled=True), times={k: v for k, v in times.items() if v}, weekday=weekday)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if create and not context["home_channel_set"]:
            raise HTTPException(
                status_code=409,
                detail="Сначала привяжите бота в мастере настройки: сводке пока некуда приходить.")
        created = []
        for spec in create:
            try:
                job = create_job_with_scheduler_registration(**spec)
            except Exception as exc:  # noqa: BLE001 — Hermes не принял задачу (например, не разобрал расписание)
                logger.warning("shturman: задача %s не создана (%s)", spec["name"], type(exc).__name__)
                raise HTTPException(
                    status_code=502,
                    detail=f"Hermes не создал задачу «{spec['name']}» ({type(exc).__name__}). Созданные до неё остались.")
            created.append({"name": spec["name"], "id": job.get("id"), "schedule": spec["schedule"]})
    return {"created": created, "existing": present, **context}


@router.get("/cron")
async def get_cron() -> dict[str, Any]:
    return await asyncio.to_thread(_cron_status_sync)


@router.post("/cron/install")
async def post_cron_install(body: Optional[CronInstallBody] = None) -> dict[str, Any]:
    """Заводит в Hermes задачи «утренняя сводка» (каждый день) и «обзор недели» (по пятницам)
    с доставкой в управляющий чат владельца."""
    values = body.model_dump() if body is not None else {}
    return await asyncio.to_thread(_cron_install_sync, **values)
