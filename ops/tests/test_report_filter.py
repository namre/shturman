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

    def test_ordinary_words_of_a_defect_line_stay(self):
        """Д-41: обычные русские слова не становятся «…» — строка дефекта читается почти целиком."""
        text = ("Карточки ops_consent шлют без отключения предпросмотра ссылок; Codex из Windows PowerShell прислал "
                "отчёт не в UTF-8, карточка пришла с мусором. Токен внутреннего API позволяет закрыть договорённость "
                "без нажатия владельца.")
        out = rf.free_text(text, VOCAB)
        self.assertNotIn("…", out)
        self.assertEqual(out, text)

    def test_names_addresses_keys_phones_and_ids_still_go(self):
        out = rf.free_text("Иван Петров прислал +7 916 123-45-67 и ключ sk-proj-SECRETKEY1234567890abcdef, "
                           "чат 1234567890, адрес https://setup.example.com:8443/x", VOCAB)
        for s in ("Иван", "Петров", "916", "SECRETKEY", "1234567890", "setup.example.com"):
            self.assertNotIn(s, out, s)

    def test_text_not_in_utf8_is_refused(self):
        """Д-40: Windows PowerShell и кодовая страница 1251 — отказ с объяснением, а не карточка с мусором."""
        broken = b"\xcf\xf0\xe8\xe2\xe5\xf2".decode("utf-8", errors="surrogateescape")     # «Привет» в 1251
        mojibake = "Привет, карточка".encode("utf-8").decode("cp1251")                        # «РџСЂРёРІРµС‚»
        for bad in (broken, mojibake, "текст\ufffdтекст", "a\x00b"):
            self.assertTrue(rf.bad_encoding(bad), repr(bad))
            with self.assertRaises(ValueError) as caught:
                rf.build(ROOT, {"scenario": "О2", "result": "clean", "deviations": bad}, VOCAB)
            self.assertIn("UTF-8", str(caught.exception))
        for good in ("Россия, Сергей, Слава", "обычный текст", "PASS tg-session"):
            self.assertFalse(rf.bad_encoding(good), good)

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


class UpdateResult(unittest.TestCase):
    def test_not_installed_goes_as_rollback_for_an_older_service(self):
        """Д-58: «не установлено» в local/update/last.env — сервису (и прежней версии) это откат, не «не закончено»."""
        args = {"scenario": "О2", "result": "failed", "update": {"from": "v0.0.12", "to": "v0.0.13", "result": "not-installed"}}
        self.assertEqual(rf.build(ROOT, args, VOCAB)["update"]["result"], "rolled-back")
        args["update"]["result"] = "что-то"
        self.assertEqual(rf.build(ROOT, args, VOCAB)["update"]["result"], "running")


if __name__ == "__main__":
    unittest.main()
