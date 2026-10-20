"""Команда `shturman ops-consent` — разрешение владельца для скриптов ops/ (Р-100, `ops_consent.py`).

    shturman ops-consent ask --action ВИД --target ЦЕЛЬ [--detail поле=значение]… [--wait СЕКУНД] [--keep]
    shturman ops-consent ask … --direct      — путь без сервиса (одноразовый контейнер, `docker compose run`)
    shturman ops-consent status --action ВИД --target ЦЕЛЬ

`ask` через сервис: просит карточку (или находит уже заведённую), печатает «Ждём подтверждения владельца
в боте: …; запрос действует до ЧЧ:ММ» и ждёт до `--wait` секунд (0 — не ждать). Нажато «Разрешить» —
расходует разрешение (с `--keep` — не расходует: его израсходует следующий запуск скрипта) и выходит с 0.

Коды выхода — `ops_consent.GRANTED…CONFLICT`: 0 — разрешено; 4 — владелец ещё не ответил (запрос
остаётся в силе: повтор той же команды продолжит без новой карточки); 5 — отклонено, срок вышел или
разрешение уже израсходовано; 6 — бот согласований не настроен или владелец не привязан (подтвердить
через бот некому); 7 — сервис не отвечает; 8 — путь без сервиса невозможен (бота опрашивает кто-то
ещё, очередь обновлений переполнена); 2 — неверные параметры.

Печатается только вид действия, время и исход: ни токенов, ни переписки.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any, Callable

from . import ops_consent as oc

Call = Callable[[str, str, "dict | None"], "tuple[int, dict]"]


class ServiceDown(Exception):
    pass


def say(text: str) -> None:
    print(text, flush=True)


def _tz() -> str:
    return os.environ.get("SHTURMAN_TIMEZONE", "").strip() or "Europe/Moscow"


def api(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    """Запрос к своему сервису с токеном внутреннего API из окружения; сервиса нет — ServiceDown."""
    import urllib.error
    import urllib.request

    token = os.environ.get("SHTURMAN_OPERATOR_TOKEN", "")     # токен оператора (Р-104)
    if not token:
        raise ServiceDown("не задана переменная окружения SHTURMAN_OPERATOR_TOKEN")
    port = os.environ.get("SHTURMAN_PORT", "8765")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        if exc.code >= 500:
            raise ServiceDown(f"HTTP {exc.code}") from None
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {"error": f"HTTP {exc.code}"}
    except (OSError, ValueError) as exc:
        raise ServiceDown(type(exc).__name__) from None


def parse_details(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise ValueError(f"подробность {item!r}: нужен вид поле=значение")
        out[key] = value
    return out


def ask_service(action: str, target: str, details: dict[str, str], *, wait: float, keep: bool,
                call: Call = api, sleep: Callable[[float], None] = time.sleep,
                clock: Callable[[], float] = time.monotonic, every: float = 3.0) -> int:
    tz = _tz()
    try:
        status, out = call("POST", "/api/ops/consents", {"action": action, "target": target, "details": details})
    except ServiceDown:
        return oc.NO_SERVICE
    if status == 409 and out.get("code") == "owner_unknown":
        say(str(out.get("error")))
        return oc.NO_BOT
    if status == 429:
        say(str(out.get("error")))
        return oc.WAITING
    if status >= 400:
        say(str(out.get("error") or f"ошибка {status}"))
        return 2
    deadline = clock() + max(0.0, wait)
    announced = False
    while True:
        st, cid = out.get("status"), out.get("consent_id")
        if st == "granted":
            if keep:
                say(f"Разрешение владельца есть: {out.get('title')}; годно до {oc.iso_clock(out.get('valid_until'), tz)}, "
                    "один раз — его израсходует следующий запуск команды.")
                return oc.GRANTED
            try:
                code, used = call("POST", f"/api/ops/consents/{cid}/use", {"action": action, "target": target})
            except ServiceDown:
                return oc.NO_SERVICE
            if code == 200:
                say(f"Разрешено владельцем в боте согласований: {out.get('title')}. Разрешение израсходовано на этот запуск.")
                return oc.GRANTED
            out = used if isinstance(used, dict) and used.get("status") else {"status": "failed"}
            if out.get("status") == "granted":     # гонка с другим запуском — второй раз не пробуем
                out = {"status": "used"}
            continue
        if st == "pending":
            if not announced:
                say(f"Ждём подтверждения владельца в боте согласований: {out.get('title')}; запрос действует до "
                    f"{oc.iso_clock(out.get('expires_at'), tz)}. Владельцу: откройте бот и нажмите «Разрешить».")
                announced = True
            if clock() >= deadline:
                say(f"Владелец ещё не ответил. Запрос в боте действует до {oc.iso_clock(out.get('expires_at'), tz)}: "
                    "когда владелец нажмёт «Разрешить», запустите ту же команду снова — она продолжит без нового запроса.")
                return oc.WAITING
            sleep(every)
            try:
                code, out = call("GET", f"/api/ops/consents/{cid}", None)
            except ServiceDown:
                return oc.NO_SERVICE
            if code >= 400:
                out = {"status": "failed"}
            continue
        words = {"rejected": "Владелец отклонил запрос в боте согласований. Ничего не изменено.",
                 "expired": "Срок запроса вышел, а разрешения нет. Ничего не изменено; запустите команду снова — "
                            "придёт новый запрос.",
                 "used": "Это разрешение уже израсходовано. Ничего не изменено; запустите команду снова — придёт новый запрос.",
                 "failed": "Запрос не дошёл до владельца или не применился. Ничего не изменено; запустите команду снова."}
        say(words.get(str(st), words["failed"]))
        return oc.DENIED


def status_service(action: str, target: str, *, call: Call = api) -> int:
    try:
        code, out = call("GET", f"/api/ops/consents?action={action}&target={target}", None)
    except ServiceDown:
        say("unknown — сервис не отвечает")
        return oc.NO_SERVICE
    if code >= 400:
        say(f"unknown — {out.get('error')}")
        return 2
    st, tz = out.get("status", "none"), _tz()
    when = {"pending": out.get("expires_at"), "granted": out.get("valid_until")}.get(st)
    say(f"{st} {oc.iso_clock(when, tz)}" if when else str(st))
    return 0


async def load_owner(config: Any, bot_id: int) -> tuple[dict[str, int] | None, str]:
    """Владелец бота: из базы, если она отвечает (она главнее), иначе из копии сервиса. И откуда взят."""
    import asyncpg

    from .executor import binding

    try:
        conn = await asyncpg.connect(config.dsn, timeout=5)
    except Exception:  # noqa: BLE001 — базы нет: берём копию
        return oc.read_owner_copy(config.data_dir, bot_id), "копия сервиса"
    try:
        return await binding.bound_owner(conn, bot_id), "база"
    except Exception:  # noqa: BLE001 — база отвечает, но схема не та или таблиц нет
        return oc.read_owner_copy(config.data_dir, bot_id), "копия сервиса"
    finally:
        await conn.close()


async def ask_direct(action: str, target: str, details: dict[str, str], *, wait: float) -> int:
    from .config import Config, ConfigError
    from .executor.botapi import BotApi, BotApiError

    try:
        config = Config.from_env()
    except ConfigError as exc:
        say(f"Настройки сервиса не читаются ({exc}) — путь через бот без сервиса невозможен.")
        return oc.NO_BOT
    if not config.bot_token:
        say("Бот согласований не настроен — подтвердить через бот некому.")
        return oc.NO_BOT
    bot = BotApi(config.bot_token, proxy_url=config.proxy_url)
    try:
        if bot.broken:
            say(f"Бот согласований не готов ({bot.broken}).")
            return oc.NO_BOT
        try:
            me = await bot.get_me()
        except BotApiError as exc:
            say(f"Telegram не принял бота согласований или недоступен ({exc}).")
            return oc.NO_BOT
        bot_id = me.get("id")
        if not isinstance(bot_id, int):
            say("Telegram не назвал бота согласований.")
            return oc.NO_BOT
        owner, source = await load_owner(config, bot_id)
        if owner is None:
            say("Владелец к боту согласований не привязан (или без базы его не узнать) — подтвердить через бот некому.")
            return oc.NO_BOT
        say(f"Сервис не отвечает: запрос владельцу — напрямую от имени бота согласований (владелец бота — по: {source}).")
        try:
            return await oc.direct_ask(bot, owner, action, target, details, wait=wait, say=say, tz=config.timezone)
        except oc.DirectRefused as exc:
            say(exc.text)
            return exc.code
    finally:
        await bot.aclose()


def main(args: Any) -> None:
    try:
        details = parse_details(args.detail)
        details = oc.check(args.action, args.target, details)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
    if args.what == "status":
        sys.exit(status_service(args.action, args.target))
    if args.direct:
        sys.exit(asyncio.run(ask_direct(args.action, args.target, details, wait=float(args.wait))))
    sys.exit(ask_service(args.action, args.target, details, wait=float(args.wait), keep=args.keep))


def add_parser(sub: Any) -> None:
    p = sub.add_parser("ops-consent", help="разрешение владельца в боте согласований для скриптов ops/ (Р-100)")
    p.add_argument("what", choices=["ask", "status"])
    p.add_argument("--action", required=True, choices=sorted(oc.ACTIONS))
    p.add_argument("--target", required=True)
    p.add_argument("--detail", action="append", default=[], metavar="ПОЛЕ=ЗНАЧЕНИЕ")
    p.add_argument("--wait", type=int, default=300, help="сколько секунд ждать нажатия (0 — не ждать)")
    p.add_argument("--keep", action="store_true", help="не расходовать полученное разрешение")
    p.add_argument("--direct", action="store_true", help="путь без сервиса: напрямую через Bot API")
