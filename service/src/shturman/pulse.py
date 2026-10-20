"""Внешний сторож: пульс наружу (Р-98, пробел «внешний сторож» в сопровождении).

Сервер не может сторожить сам себя: если он выключился целиком, ни `./ops/doctor.sh`, ни сообщение
в бот не уйдут. Поэтому сервис раз в `pulse_every` секунд зовёт адрес внешней службы, которую завёл
владелец (`SHTURMAN_PULSE_URL` в `.env`, пишет `./ops/pulse.sh`): «жив, всё в порядке» — запрос на сам
адрес, «жив, но есть неполадка» — на тот же адрес с `/fail` на конце. Так понимают адрес
healthchecks.io (и его копии на своём сервере) и Better Stack (heartbeat); служба сама пишет
владельцу, если пульс перестал приходить вовсе (сервер или сервис лежат) или пришёл `/fail`.

Что проверяется перед каждым пульсом (`checks`):
  * база отвечает — `SELECT 1`;
  * сбор переписки идёт — худшее состояние сессий Telegram (`tg/health.py`): «нужно войти заново»,
    «нет связи дольше 15 минут», «сессия занята», «обновлений нет больше суток» — неполадка; пауза
    владельца — нет;
  * утренняя сводка — `summaries.morning_problem`: последний запуск упал, сегодня её не было,
    плагин Hermes давно не отвечает. «Промолчала» ([SILENT]) — не неполадка.

Тело запроса — коды неполадок через пробел («telegram:relogin summary:missed») или «ok»: служба
показывает его владельцу в журнале проверки. Ни имён, ни текста, ни адресов в нём нет.

Адрес — только https; другое не принимается (пульс выключен, `problem = bad_url`). Сеть — как у
остальных обращений сервиса наружу: через `EGRESS_PROXY_URL`, если он задан, без переменных
окружения прокси и без перенаправлений. Адрес целиком не пишется ни в журнал, ни в `GET /api/status`:
только имя узла.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

logger = logging.getLogger("shturman.pulse")

BOOT_DELAY = 60.0      # первый пульс — когда сессии Telegram и плагин успели подключиться
TIMEOUT = 10.0
BODY_LIMIT = 500


def parse_url(raw: str) -> tuple[str, str] | None:
    """(адрес «жив», адрес «неполадка») или None, если адрес не годится."""
    try:
        parts = urlsplit((raw or "").strip())
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment:
        return None
    path = parts.path.rstrip("/")
    ok = urlunsplit(("https", parts.netloc, path or "/", parts.query, ""))
    fail = urlunsplit(("https", parts.netloc, f"{path}/fail", parts.query, ""))
    return ok, fail


def host_of(raw: str) -> str | None:
    try:
        return urlsplit((raw or "").strip()).hostname
    except ValueError:
        return None


@dataclass
class Pulse:
    """Состояние пульса — в `state.extras["pulse"]`; `public()` — для `GET /api/status`."""

    url: str = ""
    every: float = 300.0
    clock: Callable[[], float] = time.monotonic
    enabled: bool = False
    problem: str | None = None            # bad_url — адрес не годится
    last_at: float | None = None          # когда последний раз пробовали
    last_sent: str | None = None          # ok | fail — что отправили последним
    last_delivered: bool | None = None    # приняла ли служба
    last_error: str | None = None         # вид ошибки доставки: http_404, ConnectError…
    last_ok_at: float | None = None       # когда служба последний раз приняла пульс
    failing: list[str] = field(default_factory=list)
    sent: int = 0
    errors: int = 0

    def __post_init__(self) -> None:
        if self.url:
            self.enabled = parse_url(self.url) is not None
            self.problem = None if self.enabled else "bad_url"

    def _age(self, moment: float | None) -> int | None:
        return None if moment is None else max(0, int(self.clock() - moment))

    def public(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "configured": bool(self.url), "problem": self.problem,
                "host": host_of(self.url) if self.enabled else None, "every_s": int(self.every),
                "last_age_s": self._age(self.last_at), "last_sent": self.last_sent,
                "last_delivered": self.last_delivered, "last_error": self.last_error,
                "last_ok_age_s": self._age(self.last_ok_at), "failing": list(self.failing),
                "sent": self.sent, "errors": self.errors}


async def checks(state: Any, *, now: datetime | None = None) -> list[str]:
    """Коды неполадок; пустой список — всё в порядке."""
    from . import summaries
    from .tg import health

    out: list[str] = []
    try:
        async with state.ro_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
            have = await summaries.actual(conn)
    except Exception as exc:  # noqa: BLE001
        logger.warning("пульс: база не ответила (%s)", type(exc).__name__)
        return ["database"]
    tg = health.snapshot(state)
    worst = tg.get("worst")
    if worst in health.ALERT:
        out.append(f"telegram:{worst}")
    problem = summaries.morning_problem(have, now=now, zone=getattr(state.config, "timezone", None))
    if problem:
        out.append(problem.replace("summary_", "summary:").replace("assistant_silent", "assistant:silent"))
    return out


async def beat(state: Any, pulse: Pulse, client: httpx.AsyncClient, *, now: datetime | None = None) -> bool:
    """Один пульс: проверки и запрос. True — служба приняла."""
    urls = parse_url(pulse.url)
    if urls is None:
        return False
    failing = await checks(state, now=now)
    pulse.failing = failing
    target = urls[1] if failing else urls[0]
    body = (" ".join(failing) if failing else "ok")[:BODY_LIMIT]
    pulse.last_at = pulse.clock()
    pulse.last_sent = "fail" if failing else "ok"
    try:
        response = await client.post(target, content=body.encode("utf-8"),
                                     headers={"Content-Type": "text/plain; charset=utf-8"})
    except httpx.HTTPError as exc:
        pulse.last_delivered, pulse.last_error = False, type(exc).__name__
        pulse.errors += 1
        logger.warning("пульс не доставлен (%s)", type(exc).__name__)
        return False
    if 200 <= response.status_code < 300:
        pulse.last_delivered, pulse.last_error, pulse.last_ok_at = True, None, pulse.clock()
        pulse.sent += 1
        return True
    pulse.last_delivered, pulse.last_error = False, f"http_{response.status_code}"
    pulse.errors += 1
    logger.warning("пульс не принят: служба ответила HTTP %s", response.status_code)
    return False


def make_client(config: Any, transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    if transport is None:
        transport = httpx.AsyncHTTPTransport(proxy=getattr(config, "proxy_url", "") or None, trust_env=False,
                                             retries=1)
    return httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                             timeout=httpx.Timeout(TIMEOUT))


# Подмена сети в тестах: {"transport": httpx.MockTransport(...)}.
TEST_OVERRIDES: dict[str, Any] = {}


def snapshot(state: Any) -> dict[str, Any]:
    pulse = state.extras.get("pulse")
    return pulse.public() if isinstance(pulse, Pulse) else Pulse().public()


@contextlib.asynccontextmanager
async def lifespan(state: Any) -> AsyncIterator[None]:
    pulse = Pulse(url=state.config.pulse_url, every=state.config.pulse_every)
    state.extras["pulse"] = pulse
    if pulse.problem:
        logger.warning("внешний сторож выключен: адрес SHTURMAN_PULSE_URL не https или записан неверно")
    if not pulse.enabled:
        yield
        return
    try:
        client = make_client(state.config, TEST_OVERRIDES.get("transport"))
    except Exception as exc:  # noqa: BLE001 — например, прокси SOCKS без пакета socksio
        pulse.enabled, pulse.problem = False, "bad_proxy"
        logger.warning("внешний сторож выключен: клиент не создан (%s)", type(exc).__name__)
        yield
        return

    async def loop() -> None:
        await asyncio.sleep(TEST_OVERRIDES.get("boot_delay", BOOT_DELAY))
        while True:
            try:
                await beat(state, pulse, client, now=datetime.now(timezone.utc))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — следующий пульс по расписанию
                pulse.errors += 1
                pulse.last_error = type(exc).__name__
                logger.warning("пульс: сбой (%s)", type(exc).__name__)
            await asyncio.sleep(pulse.every)

    task = state.spawn(loop(), name="pulse")
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client.aclose()
