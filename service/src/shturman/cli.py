"""Команды сервиса переписки.

Строка подключения берётся из переменной окружения SHTURMAN_DSN.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from . import db
from .importer import import_export, scan
from .search import search


def _dsn() -> str:
    dsn = os.environ.get("SHTURMAN_DSN")
    if not dsn:
        sys.exit("не задана переменная окружения SHTURMAN_DSN")
    return dsn


def _parse_exclude(values: list[str]) -> set[tuple[str, int]]:
    out: set[tuple[str, int]] = set()
    for v in values:
        cls, _, num = v.partition(":")
        if cls not in ("user", "chat", "channel") or not num.isdigit():
            sys.exit(f"неверное исключение {v!r}: нужен вид user:123, chat:123 или channel:123")
        out.add((cls, int(num)))
    return out


async def _migrate() -> None:
    conn = await db.connect(_dsn())
    try:
        applied = await db.migrate(conn)
    finally:
        await conn.close()
    print("применено: " + (", ".join(applied) if applied else "ничего, схема актуальна"))


def _scan(path: str) -> None:
    with open(path, "rb") as fp:
        owner, chats = scan(fp)
    if owner:
        print(f"владелец: {owner.name or '—'} ({owner.tg_user_id})")
    total = sum(c.messages for c in chats)
    print(f"чатов: {len(chats)}, сообщений: {total}")
    for c in chats:
        period = f"{c.first_at} … {c.last_at}" if c.first_at else "пусто"
        print(f"{c.messages:>8}  {c.peer_class}:{c.tg_id:<14} {c.type:<19} {period}  {c.name or '—'}")


async def _import(path: str, owner_id: int | None, exclude: set[tuple[str, int]]) -> None:
    conn = await db.connect(_dsn())
    try:
        await db.migrate(conn)
        with open(path, "rb") as fp:
            stats = await import_export(
                conn, fp, owner_tg_user_id=owner_id, exclude=exclude,
                source_name=os.path.basename(path),
            )
    finally:
        await conn.close()
    print(json.dumps(stats.as_dict(), ensure_ascii=False, indent=2))


async def _search(query: str, limit: int) -> None:
    conn = await db.connect(_dsn())
    try:
        hits = await search(conn, query, limit=limit)
    finally:
        await conn.close()
    for h in hits:
        who = "я" if h["is_outgoing"] else (h["sender_name"] or "—")
        print(f"[{h['sent_at']:%Y-%m-%d %H:%M}] {h['chat_title'] or h['chat_type']} · {who}: {h['snippet']}")
    if not hits:
        print("ничего не найдено")


def _local_api(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    """Запрос к своему же сервису с токеном из окружения (команды оператора внутри контейнера)."""
    import urllib.error
    import urllib.request

    token = os.environ.get("SHTURMAN_API_TOKEN", "")
    if not token:
        sys.exit("не задана переменная окружения SHTURMAN_API_TOKEN")
    port = os.environ.get("SHTURMAN_PORT", "8765")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method.upper(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # мимо прокси из окружения
    try:
        with opener.open(request, timeout=60) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {"error": f"HTTP {exc.code}"}
    except OSError as exc:
        sys.exit(f"сервис не отвечает: {exc}")


def _call(method: str, path: str, raw: str | None) -> None:
    if not path.startswith("/api/"):
        sys.exit("путь должен начинаться с /api/")
    try:
        payload = json.loads(raw) if raw else None
    except ValueError:
        sys.exit("третий аргумент должен быть JSON")
    status, out = _local_api(method, path, payload)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if status >= 400:
        sys.exit(1)


def _tg_login(role: str) -> None:
    """Вход в аккаунт Telegram по QR в терминале. Пароль 2FA вводится скрыто и не сохраняется."""
    import getpass
    import time

    import segno

    if not sys.stdin.isatty():
        sys.exit("вход выполняет человек в интерактивном терминале")
    body: dict = {"role": role}
    if role == "owner":
        print("Вы подключаете ОСНОВНОЙ аккаунт. Сервис будет только читать выбранные вами чаты и\n"
              "ничего не отправит от его имени. На сервере появится сессия этого аккаунта: риск\n"
              "ограничений со стороны Telegram ложится на него. Завершить сессию можно в Telegram:\n"
              "Настройки → Устройства.")
        if input("Чтобы продолжить, напишите «да»: ").strip().lower() != "да":
            sys.exit("отменено")
        body["confirm_owner"] = True
    status, out = _local_api("POST", "/api/tg/login", body)
    if status >= 400:
        sys.exit(out.get("error") or f"ошибка {status}")
    login_id, shown = out["login_id"], None
    try:
        while True:
            state = out.get("status")
            if state == "pending":
                if out.get("link") and out["link"] != shown:
                    shown = out["link"]
                    print("\nВ Telegram на телефоне: Настройки → Устройства → Подключить устройство.\n"
                          "Наведите камеру на код:\n")
                    segno.make(shown, error="l").terminal(compact=True, border=1)
            elif state == "password_required":
                hint = f" (подсказка: {out['hint']})" if out.get("hint") else ""
                password = getpass.getpass(f"Пароль облачного хранения Telegram{hint}: ")
                status, out = _local_api("POST", f"/api/tg/login/{login_id}/password", {"password": password})
                password = ""
                if status >= 400:
                    print(out.get("error") or f"ошибка {status}")
                    status, out = _local_api("GET", f"/api/tg/login/{login_id}")
                continue
            elif state == "completed":
                print("\nГотово: аккаунт подключён. Ничего не читается, пока вы не выберете чаты.")
                return
            else:
                sys.exit(out.get("error") or f"вход не выполнен: {state}")
            time.sleep(2)
            status, out = _local_api("GET", f"/api/tg/login/{login_id}")
            if status >= 400:
                sys.exit(out.get("error") or f"ошибка {status}")
    except KeyboardInterrupt:
        _local_api("POST", f"/api/tg/login/{login_id}/cancel", {})
        sys.exit("\nотменено")


def _serve() -> None:
    import logging

    import uvicorn

    from .app import build_app
    from .config import Config, ConfigError

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config = Config.from_env()
    except ConfigError as exc:
        sys.exit(str(exc))
    # access_log выключен: в адресах запросов нет секретов, но журнал не должен расти от опроса очереди
    uvicorn.run(build_app(config), host=config.host, port=config.port, log_level="warning", access_log=False)


def main() -> None:
    p = argparse.ArgumentParser(prog="shturman", description="Сервис переписки «Штурмана»")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="привести схему базы к текущей версии")
    s = sub.add_parser("scan", help="показать чаты экспорта, ничего не записывая")
    s.add_argument("path")
    i = sub.add_parser("import", help="импортировать экспорт Telegram Desktop (result.json)")
    i.add_argument("path")
    i.add_argument("--owner-id", type=int, help="идентификатор владельца — нужен для экспорта одного чата")
    i.add_argument("--exclude", action="append", default=[], metavar="КЛАСС:ID",
                   help="не принимать чат в архив, например user:123; можно повторять")
    q = sub.add_parser("search", help="поиск по архиву")
    q.add_argument("query")
    q.add_argument("--limit", type=int, default=10)
    sub.add_parser("serve", help="запустить сервис: внутренний API и MCP-сервер архива")
    c = sub.add_parser("call", help="запрос к внутреннему API работающего сервиса (для оператора)")
    c.add_argument("method", choices=["GET", "POST", "PUT", "DELETE"])
    c.add_argument("path")
    c.add_argument("json", nargs="?")
    t = sub.add_parser("tg-login", help="вход в аккаунт Telegram по QR в терминале (выполняет человек)")
    t.add_argument("role", choices=["assistant", "owner"])
    a = p.parse_args()

    if a.cmd == "migrate":
        asyncio.run(_migrate())
    elif a.cmd == "scan":
        _scan(a.path)
    elif a.cmd == "import":
        asyncio.run(_import(a.path, a.owner_id, _parse_exclude(a.exclude)))
    elif a.cmd == "search":
        asyncio.run(_search(a.query, a.limit))
    elif a.cmd == "serve":
        _serve()
    elif a.cmd == "call":
        _call(a.method, a.path, a.json)
    elif a.cmd == "tg-login":
        _tg_login(a.role)


if __name__ == "__main__":
    main()
