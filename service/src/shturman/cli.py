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


if __name__ == "__main__":
    main()
