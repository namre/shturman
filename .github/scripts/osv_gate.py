#!/usr/bin/env python3
"""Тревога по уязвимостям зависимостей (docs/releases.md, «Уязвимости зависимостей»). Стандартная библиотека.

Читает вывод `osv-scanner scan source --format json` и печатает перечень уязвимостей высокой и критической
тяжести: наибольшая оценка CVSS группы (`max_severity`) не ниже 7,0 или тяжесть у источника HIGH/CRITICAL.
Остальное — одной строкой счётчиком. Код 1 — есть что чинить (задача CI падает и открывает issue), 0 — нет.

    osv_gate.py OSV.json [--report ИТОГ.md]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HIGH = 7.0
WORDS = ("HIGH", "CRITICAL")


def findings(data: dict) -> tuple[list[dict], int]:
    """(серьёзные, сколько прочих)."""
    serious, other = [], 0
    for result in data.get("results") or []:
        source = Path((result.get("source") or {}).get("path") or "?")
        lock = "/".join(source.parts[-2:])
        for pkg in result.get("packages") or []:
            info = pkg.get("package") or {}
            vulns = {v.get("id"): v for v in pkg.get("vulnerabilities") or []}
            for group in pkg.get("groups") or [{"ids": [i]} for i in vulns]:
                ids = group.get("ids") or []
                try:
                    score = float(group.get("max_severity") or 0)
                except (TypeError, ValueError):
                    score = 0.0
                words = {str((vulns.get(i) or {}).get("database_specific", {}).get("severity", "")).upper() for i in ids}
                if score >= HIGH or words & set(WORDS):
                    aliases = sorted(set(group.get("aliases") or ids))
                    serious.append({"lock": lock, "name": info.get("name", "?"), "version": info.get("version", "?"),
                                    "ids": ids, "aliases": aliases, "score": score,
                                    "severity": next((w for w in WORDS if w in words), "") or f"CVSS {score}",
                                    "summary": next(((vulns.get(i) or {}).get("summary") or "" for i in ids), "")})
                else:
                    other += 1
    return serious, other


def report(serious: list[dict], other: int) -> str:
    if not serious:
        return f"Уязвимостей высокой и критической тяжести нет (прочих: {other})."
    lines = [f"Уязвимости высокой и критической тяжести: {len(serious)} (прочих: {other}).", "",
             "| Файл | Пакет | Версия | Тяжесть | Идентификаторы | Кратко |", "|---|---|---|---|---|---|"]
    for f in serious:
        cells = [f["lock"], f["name"], f["version"], f["severity"], ", ".join(f["aliases"][:6]),
                 f["summary"][:120]]
        lines.append("| " + " | ".join(str(c).replace("|", "/").replace("\n", " ") for c in cells) + " |")
    lines += ["", "Что делать: обновить строку в requirements.lock командой из его шапки, тесты, THIRD_PARTY_NOTICES.md, "
              "выпуск с kind: \"security\" в releases.json (docs/releases.md, «Уязвимости зависимостей»)."]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("osv_json")
    p.add_argument("--report")
    a = p.parse_args(argv[1:])
    try:
        data = json.loads(Path(a.osv_json).read_text("utf-8") or "{}")
    except (OSError, ValueError) as exc:
        print(f"вывод OSV-Scanner не прочитан: {exc}", file=sys.stderr)
        return 2
    serious, other = findings(data)
    text = report(serious, other)
    print(text)
    if a.report:
        Path(a.report).write_text(text + "\n", "utf-8")
    return 1 if serious else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
