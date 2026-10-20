"""Тревога по уязвимостям (.github/scripts/osv_gate.py): только высокая и критическая тяжесть; прочее — счётчиком.
Запуск: python3 -m unittest discover -s ops/tests -p 'test_*.py'"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("osv_gate", ROOT / ".github/scripts/osv_gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def package(name, version, groups, vulns):
    return {"package": {"name": name, "version": version, "ecosystem": "PyPI"}, "groups": groups,
            "vulnerabilities": vulns}


SAMPLE = {"results": [
    {"source": {"path": "/x/service/requirements.lock", "type": "lockfile"}, "packages": [
        package("starlette", "0.1.0", [{"ids": ["GHSA-aaaa"], "aliases": ["CVE-2026-1", "GHSA-aaaa"], "max_severity": "8.1"}],
                [{"id": "GHSA-aaaa", "summary": "Обход проверки | хоста\nпод", "database_specific": {"severity": "HIGH"}}]),
        package("httpx", "0.2.0", [{"ids": ["GHSA-bbbb"], "max_severity": ""}],
                [{"id": "GHSA-bbbb", "summary": "x", "database_specific": {"severity": "CRITICAL"}}]),
    ]},
    {"source": {"path": "/x/asr/requirements.lock", "type": "lockfile"}, "packages": [
        package("torch", "2.10.0+cpu", [{"ids": ["GHSA-rrmf"], "max_severity": "5.3"}],
                [{"id": "GHSA-rrmf", "database_specific": {"severity": "LOW"}}]),
    ]},
]}


class OsvGate(unittest.TestCase):
    def run_gate(self, data):
        with tempfile.TemporaryDirectory() as d:
            src, out = Path(d) / "osv.json", Path(d) / "report.md"
            src.write_text(json.dumps(data), "utf-8")
            code = gate.main(["osv_gate.py", str(src), "--report", str(out)])
            return code, out.read_text("utf-8")

    def test_high_and_critical_raise_the_alarm(self):
        code, text = self.run_gate(SAMPLE)
        self.assertEqual(code, 1)
        self.assertIn("высокой и критической тяжести: 2 (прочих: 1)", text)
        self.assertIn("| service/requirements.lock | starlette | 0.1.0 | HIGH | CVE-2026-1, GHSA-aaaa |", text)
        self.assertIn("| httpx | 0.2.0 | CRITICAL |", text)
        self.assertNotIn("torch", text)
        self.assertNotIn("\nпод", text)

    def test_low_and_medium_only_are_quiet(self):
        data = {"results": [SAMPLE["results"][1]]}
        code, text = self.run_gate(data)
        self.assertEqual(code, 0)
        self.assertIn("нет (прочих: 1)", text)
        self.assertEqual(self.run_gate({})[0], 0)


if __name__ == "__main__":
    unittest.main()
