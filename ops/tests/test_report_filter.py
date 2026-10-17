"""Белый список отчёта сопровождения (ops/report_filter.py, Р-102): ни одна строка fixtures/secrets.txt не
проходит через свободный текст агента и строки doctor, а слова runbook остаются читаемыми.
Запуск: python3 -m unittest discover -s ops/tests -p 'test_*.py'"""

import importlib.util
import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE.parent))
spec = importlib.util.spec_from_file_location("report_filter", HERE.parent / "report_filter.py")
rf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rf)

SECRETS = [s for s in (HERE / "fixtures" / "secrets.txt").read_text(encoding="utf-8").splitlines() if s.strip()]
VOCAB = rf.vocabulary(ROOT)


class FreeText(unittest.TestCase):
    def test_no_secret_line_survives_alone_or_together(self):
        for s in SECRETS:
            self.assertNotIn(s, rf.free_text(s, VOCAB), s)
            self.assertNotIn(s, rf.free_text(f"Пришлось: {s} — повторил ./ops/up.sh", VOCAB), s)
        together = rf.free_text(" ".join(SECRETS), VOCAB)
        for s in SECRETS:
            self.assertNotIn(s, together, s)

    def test_operator_words_stay_readable(self):
        text = ("Перезапустил сервис через ./ops/up.sh, затем ./ops/update.sh --all до v0.0.10-rc.1 (коммит 97caaaa); "
                "строка tg-session была FAIL, помогло повторить doctor через 5 минут.")
        self.assertEqual(rf.free_text(text, VOCAB), text)

    def test_paths_env_values_and_pipes(self):
        out = rf.free_text("читал data/shturman/sessions/owner.session и SHTURMAN_BOT_TOKEN=abc | дальше", VOCAB)
        self.assertIn("data/<…>", out)
        self.assertNotIn("owner.session", out)
        self.assertNotIn("abc", out)
        self.assertNotIn("|", out)

    def test_doctor_rows_keep_codes_and_names_only_notes_of_warn_and_fail(self):
        lines = (HERE / "fixtures" / "doctor.txt").read_text(encoding="utf-8").splitlines()
        snap = rf.doctor_snapshot(lines, VOCAB)
        self.assertEqual(snap["total"], "FAIL (1)")
        self.assertEqual([r["name"] for r in snap["rows"]], ["tg-session", "hermes-tz", "stopped"])
        dumped = repr(snap)
        for s in SECRETS + ["ivanpetrov", "gpt-6.1-sol", "hc-ping.com"]:
            self.assertNotIn(s, dumped, s)

    def test_defect_line_form(self):
        d = rf.defect("| Д-? | 2026-10-20 | бот молчал | О8 | мешает | эксплуатация, сопровождение | Codex | не исправлено | нет |", VOCAB)
        self.assertEqual((d["function"], d["severity"], d["cause"], d["first"]),
                         ("О8", "мешает", "эксплуатация, сопровождение", "Codex"))
        self.assertIsNone(rf.defect("не строка", VOCAB))
        odd = rf.defect("| Д-1; rm | вчера | x | ls -la | очень | всё | я | - | - |", VOCAB)
        self.assertEqual((odd["no"], odd["function"], odd["severity"], odd["cause"], odd["first"]), ("Д-?", "?", "?", "?", "?"))


if __name__ == "__main__":
    unittest.main()
