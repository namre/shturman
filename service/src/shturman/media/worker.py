"""Разбор одного файла в отдельном процессе: `python -m shturman.media.worker`.

Файлы приходят от чужих людей, а разбирают их библиотеки на C и C++ (PDFium, Pillow). Отдельный
процесс с пределами памяти и процессорного времени не даёт зависшему или раздутому файлу
остановить очередь и сервис; переменных окружения сервиса (токены, адреса) у него нет.

Вход (stdin): строка JSON {"name", "mime"} и после перевода строки — содержимое файла.
Выход (stdout): одна строка JSON — {"kind", "text", "pages", "truncated", "images": [base64]}
либо {"error": "unsupported" | "bad_file", "reason": "…"}.
"""

from __future__ import annotations

import base64
import json
import sys

MEMORY_LIMIT = 1024 * 1024 * 1024     # байт адресного пространства
CPU_SECONDS = 60


def _limits() -> None:
    try:
        import resource
    except ImportError:      # не Linux — без пределов
        return
    for what, value in ((resource.RLIMIT_AS, MEMORY_LIMIT), (resource.RLIMIT_CPU, CPU_SECONDS)):
        try:
            resource.setrlimit(what, (value, value))
        except (ValueError, OSError):
            pass


def main() -> int:
    _limits()
    raw = sys.stdin.buffer.read()
    head, _, data = raw.partition(b"\n")
    try:
        meta = json.loads(head)
    except ValueError:
        meta = {}
    name = meta.get("name") if isinstance(meta.get("name"), str) else None
    mime = meta.get("mime") if isinstance(meta.get("mime"), str) else None

    from .extract import BadFile, Unsupported, extract
    try:
        found = extract(data, name, mime)
    except Unsupported as exc:
        out = {"error": "unsupported", "reason": str(exc)[:200]}
    except BadFile as exc:
        out = {"error": "bad_file", "reason": str(exc)[:200]}
    else:
        out = {"kind": found.kind, "text": found.text, "pages": found.pages, "truncated": found.truncated,
               "images": [base64.b64encode(i).decode("ascii") for i in found.images]}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
