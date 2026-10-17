#!/usr/bin/env python3
"""Отчёт сессии сопровождения для ./ops/report.sh (Р-102). Только стандартная библиотека.

Принцип — тот же белый список, что у диагностического пакета (ops/diag_filter.py): в отчёт берутся только
поля известного вида, остальное отбрасывается.

  * Версии — VERSION, тег выпуска на коммите и 12 знаков коммита; режим установки.
  * Строки ./ops/doctor.sh — коды PASS/WARN/FAIL/SKIP и имена строк; пояснение — только у WARN и FAIL, после
    той же замены адресов, имён узлов, номеров и похожего на ключи (`diag_filter.mask`) и словарного фильтра.
  * Итог обновления — из local/update/last.env (версии, код итога, была ли копия).
  * Свободный текст агента («где отступили от runbook», «что помогло», строки дефектов): `mask`, затем пары
    слов с заглавной буквы (похоже на имя и фамилию) — «<имя>», пути в data/ и local/backups — без хвоста,
    `ИМЯ=значение` — без значения, и словарь: слово остаётся, только если такое (или с тем же началом) есть
    в документах и скриптах репозитория. Слова переписки, имён и названий чатов там нет — они становятся
    «…». Ограничение длины. Это не замена внимания агента: текст отчёта до отправки видит владелец.

Команды:
  report_filter.py snapshot REPO          — снимок «до» или «после»: stdin — вывод ./ops/doctor.sh; JSON в stdout
  report_filter.py build REPO ARGS.json   — отчёт: JSON с полями сессии (снимки, итог обновления, текст агента)
  report_filter.py render REPORT.json     — читаемый текст отчёта (для показа владельцу в терминале агента)
  report_filter.py text REPO              — только фильтр свободного текста: stdin → stdout (для проверки)
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diag_filter import mask  # noqa: E402

FREE_LIMIT = 500        # знаков в «отступлениях» и «что помогло»
NOTE_LIMIT = 120        # знаков в пояснении строки doctor
ROWS_LIMIT = 8          # строк WARN/FAIL в снимке
DEFECTS_LIMIT = 3
CELL_LIMIT = 300

RESULTS = {"clean": "без вмешательства", "intervened": "с вмешательством", "failed": "не прошло",
           "без вмешательства": "без вмешательства", "с вмешательством": "с вмешательством",
           "не прошло": "не прошло"}
SEVERITY = ("критично", "мешает", "косметика")
CAUSES = ("сценарий", "логика", "промпт", "эксплуатация", "сопровождение")
FIRST = ("владелец", "система", "Codex", "оператор (Claude)", "Claude")
UPDATE_RESULTS = ("ok", "rolled-back", "failed-needs-human", "rollback-failed", "running")
SCENARIO = re.compile(r"^[ОУП]\d{1,2}$")
DOCTOR = re.compile(r"^(PASS|WARN|FAIL|SKIP)  ([a-z0-9][a-z0-9-]{0,40}): (.*)$")

# --- словарь репозитория ---

CORPUS = ("AGENTS.md", "docs/runbooks", "docs/pilot", "docs/releases.md", "docs/deployment.md",
          "prompts/operate-codex.md", "ops")
_WORD = re.compile(r"[A-Za-zА-Яа-яЁё]+")
# Слова продукта, которые пишут с заглавной: пара таких слов — не имя человека.
PRODUCT = {"штурман", "штурмана", "штурману", "штурманом", "hermes", "codex", "claude", "telegram", "caddy",
           "docker", "github", "openai", "chatgpt", "настройки", "настройках", "сейчас", "согласования",
           "приложение", "приложении", "бот", "боте", "договорённости", "сводка", "ассистент", "владелец",
           "postgres", "ops", "итог", "разрешить", "отклонить", "отправить", "пропустить", "напомнить"}


def vocabulary(repo: Path) -> set[str]:
    """Префиксы (от 5 знаков) и короткие слова из документов и скриптов репозитория — в нижнем регистре."""
    words: set[str] = set()
    for name in CORPUS:
        path = repo / name
        files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.suffix in (".md", ".sh", ".py")
                                                       and "tests" not in p.parts) if path.is_dir() else []
        for f in files:
            try:
                text = f.read_text("utf-8", errors="replace")
            except OSError:
                continue
            words.update(w.lower() for w in _WORD.findall(text))
    out: set[str] = set()
    for w in words:
        if len(w) <= 4:
            out.add(w)
        else:
            out.update(w[:n] for n in range(5, len(w) + 1))
    return out


def known(word: str, vocab: set[str]) -> bool:
    w = word.lower()
    if len(w) <= 4:
        return w in vocab
    return w[:max(5, len(w) - 2)] in vocab


# Безопасные знаки целиком: скрипты ops/, ключи, версии, номера дефектов и сценариев, короткий коммит,
# числа и время, метки замен mask().
_SAFE = re.compile(
    r"\./ops/[a-z0-9_-]+\.(?:sh|py)|(?<![\w-])--?[a-z][a-z0-9-]{1,40}|\bv\d{1,3}\.\d{1,3}\.\d{1,3}(?:-rc\.\d{1,3})?\b"
    r"|\b[ДКОУПЗВ]-?\d{1,3}\b|\b[0-9a-f]{7,12}\b|\b\d{1,6}(?:[.:,]\d{1,4})*\b|<[^<>\n]{1,20}>|@<имя>")
_PAIR = re.compile(r"\b([A-ZА-ЯЁ][a-zа-яё]{1,30})(\s+)([A-ZА-ЯЁ][a-zа-яё]{1,30})\b")
_PATH = re.compile(r"((?:/opt/shturman/)?(?:data|local/backups|local/restore|local/diag|local/report))/\S+")
_ENV = re.compile(r"\b([A-Z][A-Z0-9_]{2,60})=\S+")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f​-‏ -‮⁠-⁤﻿]")


def free_text(text: str, vocab: set[str], limit: int = FREE_LIMIT) -> str:
    """Свободный текст агента → одна строка без адресов, ключей, номеров, имён и незнакомых слов."""
    text = _CTRL.sub("", text or "")
    text = " ".join(text.split())
    text = mask(text)
    text = _ENV.sub(lambda m: f"{m.group(1)}=<…>", text)
    text = _PATH.sub(lambda m: f"{m.group(1)}/<…>", text)
    text = _PAIR.sub(lambda m: m.group(0) if {m.group(1).lower(), m.group(3).lower()} <= PRODUCT else "<имя>", text)
    keep: list[str] = []

    def hold(m: re.Match) -> str:
        keep.append(m.group(0))
        return f"\x00{len(keep) - 1}\x00"

    text = _SAFE.sub(hold, text)
    text = _WORD.sub(lambda m: m.group(0) if known(m.group(0), vocab) else "…", text)
    text = re.sub(r"…(?:[\s…]*…)?", "…", text)
    text = re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], text)
    text = text.replace("|", "/").strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


# --- снимок: версии и doctor ---

def doctor_snapshot(lines: list[str], vocab: set[str]) -> dict:
    counts = {"PASS": 0, "WARN": 0, "FAIL": 0, "SKIP": 0}
    rows: list[dict] = []
    total = "нет"
    for line in lines:
        line = line.rstrip("\n")
        m = DOCTOR.match(line)
        if m:
            code, name, note = m.groups()
            counts[code] += 1
            if code in ("WARN", "FAIL") and len(rows) < ROWS_LIMIT:
                rows.append({"code": code, "name": name, "note": free_text(note, vocab, NOTE_LIMIT)})
            continue
        t = re.fullmatch(r"ИТОГ: (OK|FAIL \(\d{1,3}\))", line.strip())
        if t:
            total = t.group(1)
    rows.sort(key=lambda r: (r["code"] != "FAIL", r["name"]))
    return {"total": total, "counts": counts, "rows": rows}


def _sha(value: str) -> str:
    value = (value or "").strip().lower()
    return value[:12] if re.fullmatch(r"[0-9a-f]{40}", value) else ""


def _tag(value: str) -> str:
    value = (value or "").strip()
    return value if re.fullmatch(r"v\d{1,3}\.\d{1,3}\.\d{1,3}(?:-rc\.\d{1,3})?", value) else ""


def snapshot(repo: Path, doctor_lines: list[str], vocab: set[str]) -> dict:
    env = os.environ
    version = (env.get("REPORT_VERSION") or "").strip()
    return {"version": version if re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,3}", version) else "?",
            "tag": _tag(env.get("REPORT_TAG", "")), "commit": _sha(env.get("REPORT_COMMIT", "")),
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "doctor": doctor_snapshot(doctor_lines, vocab)}


# --- строки дефектов ---

def defect(line: str, vocab: set[str]) -> dict | None:
    """Строка формата docs/pilot/defects.md → поля; не та форма — None."""
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    if len(cells) != 9:
        return None
    no, when, what, func, sev, cause, first, fix, pinned = cells
    no = no if re.fullmatch(r"Д-(\?|\d{1,3})", no) else "Д-?"
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", when):
        when = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    func = func if re.fullmatch(r"[ПОУ0-9 ,→/?]{1,40}", func) and re.search(r"[ПОУ]\d", func) else "?"
    sev = sev if sev in SEVERITY else "?"
    causes = [c.strip() for c in re.split(r"[,/]", cause) if c.strip() in CAUSES]
    first = first if first in FIRST else "?"
    return {"no": no, "date": when, "what": free_text(what, vocab, CELL_LIMIT), "function": func, "severity": sev,
            "cause": ", ".join(causes) or "?", "first": first, "fix": free_text(fix, vocab, 120) or "не исправлено",
            "pinned": free_text(pinned, vocab, 120) or "нет"}


# --- отчёт ---

def build(repo: Path, args: dict, vocab: set[str]) -> dict:
    scenario = str(args.get("scenario") or "")
    if not SCENARIO.match(scenario):
        raise ValueError("сценарий — номер из docs/pilot/scenarios-ops.md (О1…О20) или учение (У1…У17)")
    result = RESULTS.get(str(args.get("result") or ""))
    if result is None:
        raise ValueError("итог — clean (без вмешательства), intervened (с вмешательством) или failed (не прошло)")
    title = ""
    try:
        for line in (repo / "docs/pilot/scenarios-ops.md").read_text("utf-8").splitlines():
            if line.startswith(f"## {scenario}. "):
                title = line[len(f"## {scenario}. "):].strip()[:80]
                break
    except OSError:
        pass
    defects = []
    for line in args.get("defects") or []:
        d = defect(str(line), vocab)
        if d is not None and len(defects) < DEFECTS_LIMIT:
            defects.append(d)
    update = args.get("update") or None
    if isinstance(update, dict):
        result_code = update.get("result") if update.get("result") in UPDATE_RESULTS else "running"
        update = {"from": free_text(str(update.get("from") or ""), vocab, 60) or "?",
                  "to": free_text(str(update.get("to") or ""), vocab, 60) or "?",
                  "result": result_code, "backup": bool(update.get("backup"))}
    now = datetime.now(timezone.utc)
    rid = now.strftime("%Y%m%dT%H%M%SZ") + "-" + os.urandom(2).hex()
    mode = args.get("mode") if args.get("mode") in ("hermes", "standalone") else "?"
    return {"shturman_report": 1, "id": rid, "created": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": mode,
            "scenario": scenario, "scenario_title": free_text(title, vocab, 80), "result": result,
            "before": args.get("before") or None, "after": args.get("after") or None, "update": update,
            "deviations": free_text(str(args.get("deviations") or ""), vocab),
            "helped": free_text(str(args.get("helped") or ""), vocab), "defects": defects}


def _snap_line(name: str, s: dict | None) -> list[str]:
    if not s:
        return [f"{name}: не снималась"]
    d = s.get("doctor") or {}
    c = d.get("counts") or {}
    out = [f"{name}: ИТОГ {d.get('total', 'нет')} — PASS {c.get('PASS', 0)}, WARN {c.get('WARN', 0)}, FAIL {c.get('FAIL', 0)}"]
    out += [f"  {r['code']} {r['name']}: {r['note']}" for r in d.get("rows") or []]
    return out


def _ver(s: dict | None) -> str:
    if not s:
        return "?"
    head = s.get("tag") or s.get("version") or "?"
    return head + (f" ({s['commit']})" if s.get("commit") else "")


def render(r: dict) -> str:
    lines = ["Отчёт сессии сопровождения «Штурмана»",
             f"№ {r['id']} · режим: {'с Hermes' if r['mode'] == 'hermes' else 'без Hermes' if r['mode'] == 'standalone' else '?'}",
             f"Сценарий: {r['scenario']}" + (f" — {r['scenario_title']}" if r.get("scenario_title") else ""),
             f"Итог: {r['result']}",
             f"Версия: {_ver(r.get('before'))} → {_ver(r.get('after'))}"]
    u = r.get("update")
    if u:
        words = {"ok": "OK", "rolled-back": "ОТКАТ", "failed-needs-human": "FAIL — нужен человек",
                 "rollback-failed": "FAIL — откат не удался", "running": "не закончено"}
        lines.append(f"Обновление: {u['from']} → {u['to']} — {words.get(u['result'], '?')}, копия до: "
                     + ("есть" if u.get("backup") else "нет"))
    lines.append("")
    lines += _snap_line("Проверка до", r.get("before"))
    lines += _snap_line("Проверка после", r.get("after"))
    if r.get("deviations"):
        lines += ["", f"Где отступили от runbook: {r['deviations']}"]
    if r.get("helped"):
        lines += ["", f"Что помогло: {r['helped']}"]
    if r.get("defects"):
        lines += ["", f"Строки дефектов ({len(r['defects'])}):"]
        for d in r["defects"]:
            lines.append("| " + " | ".join(d[k] for k in ("no", "date", "what", "function", "severity", "cause",
                                                            "first", "fix", "pinned")) + " |")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in ("snapshot", "build", "render", "text"):
        print("использование: report_filter.py snapshot REPO | build REPO ARGS.json | render REPORT.json | text REPO",
              file=sys.stderr)
        return 2
    cmd = argv[1]
    if cmd == "render":
        print(render(json.loads(Path(argv[2]).read_text("utf-8"))))
        return 0
    repo = Path(argv[2])
    vocab = vocabulary(repo)
    if cmd == "text":
        print(free_text(sys.stdin.read(), vocab))
        return 0
    if cmd == "snapshot":
        lines = sys.stdin.buffer.read().decode("utf-8", errors="replace").splitlines()
        print(json.dumps(snapshot(repo, lines, vocab), ensure_ascii=False))
        return 0
    args = json.loads(Path(argv[3]).read_text("utf-8"))
    try:
        report = build(repo, args, vocab)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
