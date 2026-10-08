"""Свой генератор QR-кода страницы настройки (setup_page/static/qr.js).

Страница рисует QR сама, в браузере: ссылка входа в аккаунт Telegram — секрет, и отдавать её
стороннему сервису нельзя. Ошибка в генераторе выглядела бы как «код не сканируется», поэтому:

  * матрицы сверяются модуль в модуль с segno (ей сервис рисует QR в терминале) — для каждой
    версии и каждой маски, на тексте ровно в ёмкость версии. Так проверены коррекция ошибок,
    разбиение на блоки, раскладка по матрице, маски, сведения о формате и о версии;
  * заполнение неполного кода сверяется со стандартом напрямую (ISO/IEC 18004, 7.4.9–7.4.10).
    С segno его сравнить нельзя: segno 1.6.6 после завершителя добавляет лишний нулевой байт,
    когда поток уже выровнен по байту. Коды читаются в обоих вариантах, но матрицы разные.

Читается ли нарисованный код камерой, проверяет браузерный сценарий (tests/e2e): он распознаёт
QR со снимка страницы. Нужен Node.js; без него тест пропускается.
"""

import json
import shutil
import subprocess
from importlib import resources

import pytest
import segno

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="нужен Node.js")

RUNNER = """
const qr = require(process.argv[process.argv.length - 1]);
const cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const out = cases.map(function (c) {
  if (c.words) return { words: qr.dataWords(c.text, c.words) };
  try {
    const m = qr.matrix(c.text, c.options || {});
    return { version: m.version, mask: m.mask, size: m.size,
             rows: m.rows.map(function (row) { return row.map(function (v) { return v ? 1 : 0; }).join(''); }) };
  } catch (e) { return { refused: true }; }
});
process.stdout.write(JSON.stringify(out));
"""

LOGIN = "tg://login?token=AQJpbmV4YW1wbGVfdG9rZW5fMDEyMzQ1Njc4OWFiY2RlZl9fLS0tXw"
BIND = "https://t.me/shturman_soglasovaniya_bot?start=Zm9vYmFyMDEyMzQ1Njc4OWFiY2RlZl8tQUJD"
# Сколько байт текста помещается в версии 1–10 на уровне M и сколько в них слов данных.
CAPACITY = (14, 26, 42, 62, 84, 106, 122, 152, 180, 213)
DATA_WORDS = (16, 28, 44, 64, 86, 108, 124, 154, 182, 216)
ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_:/?=."


def run(cases: list[dict]) -> list[dict]:
    script = resources.files("shturman.setup_page") / "static" / "qr.js"
    done = subprocess.run([NODE, "-e", RUNNER, str(script)], input=json.dumps(cases), capture_output=True,
                          text=True, timeout=120, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def sample(length: int, seed: int = 0) -> str:
    return "".join(ALPHABET[(i * 7 + seed) % len(ALPHABET)] for i in range(length))


def reference(content, version: int, mask: int) -> list[str]:
    code = segno.make(content, error="m", mode="byte", micro=False, boost_error=False, version=version, mask=mask)
    return ["".join("1" if bit else "0" for bit in row) for row in code.matrix]


def standard_words(payload: bytes, version: int) -> list[int]:
    """Слова данных по стандарту: режим 0100, длина, байты, завершитель, слова 0xEC и 0x11."""
    room = DATA_WORDS[version - 1] * 8
    bits = "0100" + format(len(payload), "08b" if version < 10 else "016b") + "".join(format(b, "08b") for b in payload)
    assert len(bits) <= room
    bits += "0" * min(4, room - len(bits))
    bits += "0" * (-len(bits) % 8)
    words = [int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)]
    pads = (0xEC, 0x11)
    return words + [pads[i % 2] for i in range(room // 8 - len(words))]


def test_every_version_and_mask_matches_segno_module_for_module():
    cases = [{"text": sample(room, version), "options": {"version": version, "mask": mask}}
             for version, room in enumerate(CAPACITY, start=1) for mask in range(8)]
    for case, got in zip(cases, run(cases)):
        version, mask = case["options"]["version"], case["options"]["mask"]
        assert got["size"] == 17 + 4 * version
        assert got["rows"] == reference(case["text"], version, mask), (version, mask)


def test_padding_follows_the_standard_for_every_fill_level():
    cases, expected = [], []
    for version, room in enumerate(CAPACITY, start=1):
        for length in sorted({0, 1, 2, room // 2, room - 2, room - 1, room}):
            text = sample(length, length)
            cases.append({"text": text, "words": version})
            expected.append(standard_words(text.encode(), version))
    got = run(cases)
    assert [g["words"] for g in got] == expected
    # и пример из жизни, посчитанный вручную: «hello» в версии 1
    (hello,) = run([{"text": "hello", "words": 1}])
    assert hello["words"] == [0x40, 0x56, 0x86, 0x56, 0xC6, 0xC6, 0xF0] + [0xEC, 0x11] * 4 + [0xEC]


def test_version_is_the_smallest_that_fits_and_real_links_are_small():
    lengths = (1, 14, 15, 26, 27, 62, 63, 106, 107, 180, 181, 213)
    got = run([{"text": "x" * n} for n in lengths] + [{"text": LOGIN}, {"text": BIND}])
    assert [g["version"] for g in got[:12]] == [1, 1, 2, 2, 3, 4, 5, 6, 7, 9, 10, 10]
    assert got[12]["version"] <= 5 and got[13]["version"] <= 6      # ссылки входа и привязки — небольшие коды
    assert all(0 <= g["mask"] <= 7 and len(g["rows"]) == g["size"] for g in got)


def test_text_that_does_not_fit_is_refused_instead_of_drawing_a_broken_code():
    got = run([{"text": "x" * 214}, {"text": "x" * 15, "options": {"version": 1}}, {"text": "x" * 213}])
    assert [bool(g.get("refused")) for g in got] == [True, True, False]


def test_non_latin_text_is_encoded_as_utf8():
    text = "Штурман на связи"
    payload = text.encode("utf-8")
    version = next(v for v, room in enumerate(CAPACITY, start=1) if room >= len(payload))
    (words,) = run([{"text": text, "words": version}])
    assert words["words"] == standard_words(payload, version)
    filled = "Ш" * 7                                 # 14 байт: ровно ёмкость версии 1
    (got,) = run([{"text": filled, "options": {"version": 1, "mask": 3}}])
    assert got["rows"] == reference(filled.encode("utf-8"), 1, 3)
