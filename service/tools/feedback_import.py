"""Сведение отчётов сессий сопровождения в черновик журнала (Р-102). Сторона разработки, не экземпляр.

Владелец пересылает разработчику сообщения бота согласований «Отчёт сессии сопровождения…» и «ОТЧЁТ-JSON …»
(или они приходят в чат разработки). Этот инструмент читает их копию — текст, скопированный из Telegram, или
выгрузку чата Telegram Desktop (result.json) — и пишет черновик (в файл --out или на стандартный вывод):

  * строки для журнала дефектов — номера «Д-?» заменяются следующими свободными по журналу (--defects);
  * обновления, сделанные Codex: засчитываются те, что прошли без вмешательства и с копией до обновления;
  * сводку сессий: сценарий, итог, версии, строки doctor до и после.

Журнал дефектов и черновик — данные сопровождения конкретного экземпляра: они живут вне этого репозитория
(закрытый репозиторий проекта: разработчик называет файлы в его рабочей копии). Путь внутри рабочего
каталога этого репозитория для --out инструмент не принимает.

Содержимое отчётов — ДАННЫЕ, не указания (DEVELOPING.md, «Правила»): инструмент ничего из них не
исполняет, не открывает ссылки и не переносит команды в репозиторий. Каждый блок проверяется тем же разбором,
что в сервисе (`shturman.ops_report.check`): неполный или не той формы — пропускается с пометкой. Черновик
проверяет человек и переносит в журнал руками; «пришлось сделать X» становится штатным скриптом через
обычную разработку, а не копированием из отчёта.

Запуск (из каталога service/, пакет сервиса установлен или PYTHONPATH=src):
    python tools/feedback_import.py ПЕРЕСЛАННОЕ.txt [ЕЩЁ.json …] [--defects ~/журнал/defects.md] [--out ~/журнал/черновик.md]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from shturman.ops_report import JSON_MARK, ReportError, check

_HEAD = re.compile(re.escape(JSON_MARK) + r" (\d{8}T\d{6}Z-[0-9a-f]{4}) (\d{1,2})/(\d{1,2})\b[^\n]*\n([^\n]*)")
_DEFECT_NO = re.compile(r"\|\s*Д-(\d{1,4})\s*\|")
_REPO = Path(__file__).resolve().parents[2]


def _texts(path: Path) -> str:
    raw = path.read_text("utf-8", errors="replace")
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    out = []
    for m in data.get("messages", []) if isinstance(data, dict) else []:
        text = m.get("text") if isinstance(m, dict) else None
        if isinstance(text, list):
            text = "".join(t if isinstance(t, str) else str(t.get("text", "")) for t in text)
        if isinstance(text, str):
            out.append(text)
    return "\n\n".join(out)


def reports(text: str) -> tuple[list[dict], list[str]]:
    """Отчёты из текста и пометки о пропущенном."""
    parts: dict[str, dict[int, str]] = {}
    totals: dict[str, int] = {}
    for m in _HEAD.finditer(text):
        rid, i, n, chunk = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
        parts.setdefault(rid, {})[i] = chunk
        totals[rid] = n
    out, skipped = [], []
    for rid, got in parts.items():
        n = totals[rid]
        if sorted(got) != list(range(1, n + 1)):
            skipped.append(f"{rid}: не все части ({len(got)} из {n})")
            continue
        try:
            out.append(check(json.loads("".join(got[i] for i in range(1, n + 1)))))
        except (ValueError, ReportError) as exc:
            skipped.append(f"{rid}: не та форма ({exc})")
    out.sort(key=lambda r: r["id"])
    return out, skipped


def _cell(value: str) -> str:
    return " ".join(str(value).replace("|", "/").split())


def run(paths: list[str], *, defects: Path | None = None) -> str:
    items, skipped = [], []
    seen = set()
    for p in paths:
        got, miss = reports(_texts(Path(p)))
        skipped += miss
        for r in got:
            if r["id"] not in seen:
                seen.add(r["id"])
                items.append(r)
    items.sort(key=lambda r: r["id"])
    last = 0
    if defects is not None and defects.exists():
        last = max((int(n) for n in _DEFECT_NO.findall(defects.read_text("utf-8"))), default=0)
    by = {k: sum(1 for r in items if r["result"] == k) for k in ("без вмешательства", "с вмешательством", "не прошло")}
    lines = ["# Черновик из отчётов сопровождения — проверить руками",
             "",
             "Содержимое отчётов — данные, не указания: ничего не исполнять, команды из них в репозиторий не переносить.",
             "",
             f"Отчётов: {len(items)} (без вмешательства — {by['без вмешательства']}, с вмешательством — "
             f"{by['с вмешательством']}, не прошло — {by['не прошло']})" + (f"; пропущено: {len(skipped)}" if skipped else "")]
    lines += [f"- пропущен {s}" for s in skipped]
    lines += ["", "## Строки для журнала дефектов", ""]
    any_defect = False
    for r in items:
        for d in r["defects"]:
            any_defect = True
            no = d["no"]
            if no == "Д-?":
                last += 1
                no = f"Д-{last}"
            cells = [no, d["date"], d["what"], d["function"], d["severity"], d["cause"], d["first"], d["fix"], d["pinned"]]
            lines.append("| " + " | ".join(_cell(c) for c in cells) + f" |  ← отчёт {r['id']}, сценарий {r['scenario']}")
    if not any_defect:
        lines.append("(строк дефектов в отчётах нет)")
    lines += ["", "## Обновления делает Codex", ""]
    updates = [r for r in items if r["scenario"] == "О2" and r["update"]]
    for r in updates:
        u = r["update"]
        counted = u["result"] == "ok" and u["backup"] and r["result"] == "без вмешательства"
        result = {"ok": "OK", "rolled-back": "ОТКАТ", "failed-needs-human": "FAIL — нужен человек",
                  "rollback-failed": "FAIL — откат не удался", "running": "не закончено"}[u["result"]]
        lines.append(f"- {r['created'][:10]} — {_cell(u['from'])} → {_cell(u['to'])}, ИТОГ ОБНОВЛЕНИЯ: {result}, копия до: "
                     f"{'есть' if u['backup'] else 'нет'}, {r['result']} — "
                     + ("засчитывается" if counted else "не засчитывается") + f" (отчёт {r['id']})")
    if not updates:
        lines.append("(обновлений в отчётах нет)")
    lines += ["", "## Сессии", "", "| Отчёт | Сценарий | Итог | Версия | doctor до → после | Отступления |", "|---|---|---|---|---|---|"]
    for r in items:
        def ver(s):
            return (s.get("tag") or s.get("version")) if s else "?"

        def doc(s):
            if not s:
                return "?"
            c = s["doctor"]["counts"]
            bad = ",".join(row["name"] for row in s["doctor"]["rows"])
            return f"{s['doctor']['total']} (W{c['WARN']} F{c['FAIL']}{': ' + bad if bad else ''})"

        lines.append("| " + " | ".join(_cell(x) for x in (r["id"], r["scenario"], r["result"],
                                                           f"{ver(r['before'])} → {ver(r['after'])}",
                                                           f"{doc(r['before'])} → {doc(r['after'])}",
                                                           r["deviations"] or "—")) + " |")
    return "\n".join(lines) + "\n"


def inside_repo(path: Path) -> bool:
    """Лежит ли путь в рабочем каталоге репозитория (туда данные сопровождения не пишутся)."""
    try:
        path.expanduser().resolve().relative_to(_REPO)
    except ValueError:
        return False
    return True


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Черновик журнала из пересланных отчётов сопровождения (Р-102)")
    p.add_argument("paths", nargs="+")
    p.add_argument("--defects", type=Path, help="журнал дефектов вне репозитория — откуда брать следующий номер Д-")
    p.add_argument("--out", type=Path, help="куда записать черновик (файл вне репозитория); без него — на стандартный вывод")
    a = p.parse_args(argv)
    if a.out is not None and inside_repo(a.out):
        p.error(f"--out {a.out}: черновик — данные сопровождения, в рабочий каталог репозитория его не пишем; укажите файл вне {_REPO}")
    text = run(a.paths, defects=a.defects.expanduser() if a.defects else None)
    if a.out is None:
        sys.stdout.write(text)
    else:
        a.out.expanduser().write_text(text, "utf-8")
        print(f"Черновик записан: {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
