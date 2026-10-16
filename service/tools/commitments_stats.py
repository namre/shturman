#!/usr/bin/env python3
"""Договорённости «было / стало» (Р-96): только агрегаты по базе, без текста переписки.

Что печатает: сколько найдено по статусам (и отдельно — прежними правилами и правилами текущей версии
инструкции модели), сколько снято без владельца и почему (сделано до показа, до решения, заменено
перепроверкой, без ответа), причины «Не так», доля причин срока, доля «между другими» и быстрых
обещаний, сколько часов от обещания до сводки в боте и до записи. Считает `shturman.processing.
commitment_stats` — то же, что `shturman commitments stats` в контейнере сервиса и
`./ops/commitments.sh stats` на сервере.

Пример (из каталога service/, база разработчика или стенда):
  SHTURMAN_DSN=postgresql://… PYTHONPATH=src python tools/commitments_stats.py [--json]

Базу экземпляра владельца сюда не подставляют: на сервере есть ./ops/commitments.sh stats.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import asyncpg

from shturman.processing import commitment_stats


async def main(as_json: bool) -> None:
    dsn = os.environ.get("SHTURMAN_DSN")
    if not dsn:
        sys.exit("не задана переменная окружения SHTURMAN_DSN")
    conn = await asyncpg.connect(dsn)
    try:
        stats = await commitment_stats.collect(conn)
    finally:
        await conn.close()
    print(json.dumps(stats, ensure_ascii=False, indent=2) if as_json else commitment_stats.render(stats))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="вывести JSON")
    asyncio.run(main(parser.parse_args().json))
