"""Проверка списка выпусков (ops/release_manifest.py, Р-101): файл репозитория верен; запись тега, срок годности,
совпадение с config/hermes-versions.txt. Запуск: python3 -m unittest discover -s ops/tests -p 'test_*.py'"""

import copy
import importlib.util
import json
import pathlib
import tempfile
import unittest
from datetime import date

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
spec = importlib.util.spec_from_file_location("release_manifest", HERE.parent / "release_manifest.py")
rm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rm)
DATA = json.loads((ROOT / "releases.json").read_text("utf-8"))


class Manifest(unittest.TestCase):
    def check(self, data, *args):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "releases.json"
            path.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
            return rm.main(["release_manifest.py", "check", str(path), *args])

    def test_repository_file(self):
        self.assertEqual(rm.problems(DATA, today=date.fromisoformat(DATA["published"])), [])
        self.assertEqual(self.check(DATA, "--hermes-file", str(ROOT / "config/hermes-versions.txt"),
                                    "--today", DATA["published"], "--fresh"), 0)

    def test_tag_version_expiry_and_hermes(self):
        self.assertEqual(self.check(DATA, "--tag", "v0.0.9-rc.2", "--version", "0.0.9", "--today", DATA["published"]), 0)
        self.assertEqual(self.check(DATA, "--tag", "v0.0.9-rc.2", "--version", "0.0.10", "--today", DATA["published"]), 1)
        self.assertEqual(self.check(DATA, "--tag", "v9.9.9", "--today", DATA["published"]), 1)
        self.assertEqual(self.check(DATA, "--fresh", "--today", "2099-01-01"), 1)
        self.assertEqual(self.check(DATA, "--expiring-days", "500", "--today", DATA["published"]), 1)
        other = copy.deepcopy(DATA)
        other["hermes"] = []
        self.assertEqual(self.check(other, "--hermes-file", str(ROOT / "config/hermes-versions.txt")), 1)

    def test_bad_forms(self):
        for change in ({"schema": 2}, {"expires": "2030-01-01"}, {"releases": []}):
            bad = copy.deepcopy(DATA)
            bad.update(change)
            self.assertTrue(rm.problems(bad, today=None), change)
        for field, value in (("tag", "v1"), ("version", "0.0.1"), ("kind", "x"), ("changes", ["a\nb"]),
                             ("changes", ["x" * 300]), ("owner_action", True), ("downtime_minutes", 999)):
            bad = copy.deepcopy(DATA)
            bad["releases"][0][field] = value
            self.assertTrue(rm.problems(bad, today=None), (field, value))
        dup = copy.deepcopy(DATA)
        dup["releases"].append(copy.deepcopy(dup["releases"][0]))
        self.assertTrue(any("повторяется" in p for p in rm.problems(dup, today=None)))


if __name__ == "__main__":
    unittest.main()
