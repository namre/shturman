#!/usr/bin/env python3
"""Проверка списка выпусков releases.json (Р-101, docs/releases.md, «Список выпусков»). Стандартная библиотека.

Список читает сервис каждого экземпляра раз в сутки и по нему пишет владельцу о новых версиях; правила формы —
те же, что у разбора в сервисе (service/src/shturman/updates.py, `parse`; тест сервиса сверяет файл репозитория
его разбором). Здесь — ещё проверки выпуска:

  release_manifest.py check FILE [--hermes-file config/hermes-versions.txt] [--tag vX.Y.Z[-rc.N] --version X.Y.Z]
                                 [--today ГГГГ-ММ-ДД] [--fresh] [--expiring-days N]

  --hermes-file    теги образов Hermes в списке = проверенные версии из config/hermes-versions.txt;
  --tag/--version  в списке есть запись этого тега, и её версия — это VERSION (выпуск собран верно);
  --fresh          срок годности ещё не вышел (на выпуске — обязательно: экземпляры принимают список только в
                   пределах срока);
  --expiring-days  срок годности кончится не раньше чем через N дней (еженедельная проверка в CI — напоминание
                   разработчику обновить список, пока экземпляры его ещё принимают).
Код 0 — всё верно; 1 — нет (что именно — в выводе); 2 — неверные параметры.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

TAG = re.compile(r"^v(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?:-rc\.(\d{1,3}))?$")
VERSION = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$")
HERMES_TAG = re.compile(r"^v\d{4}\.\d{1,2}\.\d{1,2}$")
WARN_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,40}$")
MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,80}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CTRL = re.compile(r"[\x00-\x1f\x7f​-‏ -‮⁠-⁤﻿]")
MAX_LIFETIME = 120
TEXT_LIMIT = 200


def problems(data, *, today: date | None) -> list[str]:
    bad: list[str] = []

    def day(value, name):
        if not isinstance(value, str) or not DATE.match(value):
            bad.append(f"{name}: нужна дата ГГГГ-ММ-ДД")
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            bad.append(f"{name}: такой даты нет")
            return None

    def text(value, name):
        if not isinstance(value, str) or not value.strip():
            bad.append(f"{name}: нужна непустая строка")
        elif len(value) > TEXT_LIMIT or CTRL.search(value):
            bad.append(f"{name}: не длиннее {TEXT_LIMIT} знаков, без переводов строк и управляющих знаков")

    def texts(value, name, most, least):
        if not isinstance(value, list) or not least <= len(value) <= most:
            bad.append(f"{name}: список из {least}–{most} строк")
            return
        for i, v in enumerate(value):
            text(v, f"{name}[{i}]")

    if not isinstance(data, dict):
        return ["нужен объект JSON"]
    if data.get("schema") != 1:
        bad.append("schema: 1")
    published, expires = day(data.get("published"), "published"), day(data.get("expires"), "expires")
    if published and expires and not 0 <= (expires - published).days <= MAX_LIFETIME:
        bad.append(f"expires: от published до expires — от 0 до {MAX_LIFETIME} дней")
    if published and today and published > today + timedelta(days=2):
        bad.append("published: дата из будущего")
    releases = data.get("releases")
    if not isinstance(releases, list) or not releases:
        bad.append("releases: непустой список")
        releases = []
    seen = set()
    for i, r in enumerate(releases):
        n = f"releases[{i}]"
        if not isinstance(r, dict):
            bad.append(f"{n}: объект")
            continue
        tag = r.get("tag")
        if not isinstance(tag, str) or not TAG.match(tag):
            bad.append(f"{n}.tag: vX.Y.Z или vX.Y.Z-rc.N")
        elif tag in seen:
            bad.append(f"{n}.tag: {tag} повторяется")
        else:
            seen.add(tag)
            if r.get("version") != tag[1:].split("-rc.")[0]:
                bad.append(f"{n}.version: должна совпадать с номером тега {tag}")
        if not isinstance(r.get("version"), str) or not VERSION.match(r.get("version") or ""):
            bad.append(f"{n}.version: X.Y.Z")
        day(r.get("date"), f"{n}.date")
        if r.get("kind") not in ("security", "regular"):
            bad.append(f"{n}.kind: security или regular")
        texts(r.get("changes"), f"{n}.changes", 3, 1)
        dt = r.get("downtime_minutes")
        if dt is not None and (isinstance(dt, bool) or not isinstance(dt, int) or not 0 <= dt <= 240):
            bad.append(f"{n}.downtime_minutes: 0–240 или null")
        if not isinstance(r.get("owner_action"), bool):
            bad.append(f"{n}.owner_action: true или false")
        else:
            texts(r.get("owner_steps", []), f"{n}.owner_steps", 3, 1 if r["owner_action"] else 0)
        hermes = r.get("hermes", [])
        if not isinstance(hermes, list) or len(hermes) > 5 or not all(isinstance(h, str) and HERMES_TAG.match(h) for h in hermes):
            bad.append(f"{n}.hermes: теги образа Hermes вида v2026.9.24")
    htags = set()
    for i, h in enumerate(data.get("hermes", []) if isinstance(data.get("hermes", []), list) else [None]):
        n = f"hermes[{i}]"
        if not isinstance(h, dict) or not isinstance(h.get("tag"), str) or not HERMES_TAG.match(h["tag"]):
            bad.append(f"{n}.tag: v2026.9.24")
            continue
        if h["tag"] in htags:
            bad.append(f"{n}.tag: повторяется")
        htags.add(h["tag"])
        if not isinstance(h.get("version"), str) or not VERSION.match(h["version"]):
            bad.append(f"{n}.version: 0.21.5")
        day(h.get("verified"), f"{n}.verified")
    wids = set()
    for i, w in enumerate(data.get("warnings", []) if isinstance(data.get("warnings", []), list) else [None]):
        n = f"warnings[{i}]"
        if not isinstance(w, dict):
            bad.append(f"{n}: объект")
            continue
        if not isinstance(w.get("id"), str) or not WARN_ID.match(w["id"]) or w["id"] in wids:
            bad.append(f"{n}.id: латиница, цифры, точка, дефис; не повторяется")
        wids.add(w.get("id"))
        if not isinstance(w.get("model"), str) or not MODEL.match(w["model"]):
            bad.append(f"{n}.model: имя модели")
        text(w.get("provider"), f"{n}.provider")
        text(w.get("text"), f"{n}.text")
        day(w.get("until"), f"{n}.until")
    return bad


def hermes_file_tags(path: Path) -> set[str]:
    out = set()
    for line in path.read_text("utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            out.add(line.split("\t")[0].strip())
    return out


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="release_manifest.py", description="Проверка releases.json (docs/releases.md)")
    p.add_argument("what", choices=["check"])
    p.add_argument("file")
    p.add_argument("--hermes-file")
    p.add_argument("--tag")
    p.add_argument("--version")
    p.add_argument("--today")
    p.add_argument("--fresh", action="store_true")
    p.add_argument("--expiring-days", type=int)
    a = p.parse_args(argv[1:])
    try:
        today = date.fromisoformat(a.today) if a.today else date.today()
    except ValueError:
        print("--today: ГГГГ-ММ-ДД", file=sys.stderr)
        return 2
    try:
        data = json.loads(Path(a.file).read_text("utf-8"))
    except (OSError, ValueError) as exc:
        print(f"НЕТ: {a.file} не читается как JSON ({type(exc).__name__})")
        return 1
    bad = problems(data, today=today)
    if not bad:
        expires = date.fromisoformat(data["expires"])
        if a.fresh and expires < today:
            bad.append(f"срок годности вышел {expires} — экземпляры такой список не примут: продлите expires")
        if a.expiring_days is not None and (expires - today).days < a.expiring_days:
            bad.append(f"срок годности кончается {expires} (меньше чем через {a.expiring_days} дн.): обновите "
                       "published и expires (docs/releases.md, «Список выпусков»)")
        if a.tag:
            entry = next((r for r in data["releases"] if r.get("tag") == a.tag), None)
            if entry is None:
                bad.append(f"в списке нет записи {a.tag} — шаг выпуска «запись в список выпусков»")
            elif a.version and entry.get("version") != a.version:
                bad.append(f"версия записи {a.tag} — {entry.get('version')}, а VERSION — {a.version}")
        if a.hermes_file:
            want = hermes_file_tags(Path(a.hermes_file))
            have = {h["tag"] for h in data.get("hermes", [])}
            if want != have:
                bad.append(f"образы Hermes в списке ({', '.join(sorted(have)) or 'нет'}) не совпадают с "
                           f"{a.hermes_file} ({', '.join(sorted(want)) or 'нет'})")
    for line in bad:
        print(f"НЕТ: {line}")
    if bad:
        return 1
    print(f"да: {a.file} — форма верна, срок годности до {data['expires']}"
          + (f", запись {a.tag} есть" if a.tag else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
