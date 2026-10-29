"""Проверочный набор памяти (tools/memory_eval.py, ОТ-45): набор согласован с проверкой ответа сервиса.

Ответ, совпадающий с разметкой, проходит всё; пустой — ничего; значение работы у организации и
разовое действие владельца в профиле попадают в «absent»."""

import importlib.util
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parents[2] / "tools" / "memory_eval.py"
spec = importlib.util.spec_from_file_location("memory_eval", TOOL)
memory_eval = importlib.util.module_from_spec(spec)
sys.modules["memory_eval"] = memory_eval          # dataclass ищет модуль по имени
spec.loader.exec_module(memory_eval)

CASES = memory_eval.load(memory_eval.DATA)


def by_id(case_id):
    return next(c for c in CASES if c["id"] == case_id)


def test_gold_answers_pass_every_check():
    total = memory_eval.Score()
    for case in CASES:
        total.add(memory_eval.score(case, memory_eval.gold(case)))
    assert total.misses == [] and total.absent == 0 and total.extra == 0
    assert set(total.total) == set(memory_eval.METRICS) and len(CASES) >= 20
    tags = {t for c in CASES for t in c["tags"]}
    assert {"nature", "detail", "owner", "kind", "subject", "document", "negative"} <= tags


def test_empty_answer_finds_nothing():
    total = memory_eval.Score()
    for case in CASES:
        total.add(memory_eval.score(case, {"commitments": []}))
    assert total.ok["facts"] == 0 and total.ok["kind"] == 0 and total.absent == 0


def test_wrong_attribution_and_owner_action_count_as_absent():
    org = by_id("org-work-status")
    wrong = memory_eval.gold(org)
    wrong["facts"][0]["project"] = "ГК «Вектор»"                  # работа приписана компании
    one = memory_eval.score(org, wrong)
    assert one.absent == 1 and one.ok["project"] == 0
    owner = by_id("owner-action")
    leaked = {"facts": [{"message": 1, "source_quote": "Подключил сервис учёта к агенту", "about": "ВЛАДЕЛЕЦ",
                         "text": "Подключил сервис учёта", "slot": None, "kind": "fact", "nature": "event"}]}
    assert memory_eval.score(owner, leaked).absent == 1


def test_report_and_replay(tmp_path, capsys):
    answers = tmp_path / "answers.jsonl"
    import json
    answers.write_text("".join(json.dumps({"id": c["id"], "parsed": memory_eval.gold(c)}, ensure_ascii=False) + "\n"
                               for c in CASES), encoding="utf-8")
    assert memory_eval.main(["--answers", str(answers), "--by-tag", "--tag", "kind"]) == 0
    out = capsys.readouterr().out
    assert "ответы из файла" in out and "kind" in out and "100.0%" in out
