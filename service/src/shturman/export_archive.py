"""Выгрузка Telegram Desktop архивом zip: result.json и файлы вложений рядом с ним.

Владелец может загрузить один result.json (тогда в архив попадает только текст) или архив всей
папки выгрузки — тогда рядом лежат голосовые, «кружки», фото и документы (voice_messages/,
round_video_messages/, photos/, files/ …), и их можно разобрать без сессии аккаунта.

Вид файла определяется по первым байтам (`PK\\x03\\x04` — zip), а не по имени.

Архив не распаковывается. result.json читается потоком прямо из архива, а файл вложения —
только тот, на который ссылается сообщение, и только копированием содержимого в новый файл со
случайным именем (`media.files.store`). Имена внутри архива на диск не попадают, поэтому
«выход из каталога» через имя члена архива (zip slip) невозможен. Члены архива, сжатые с
подозрительно большим коэффициентом, и слишком большие файлы не читаются.
"""

from __future__ import annotations

import posixpath
import zipfile
from pathlib import Path
from typing import IO

ZIP_MAGIC = b"PK\x03\x04"
RESULT_NAME = "result.json"
# Способы сжатия, которые читаются без дополнительных библиотек и которыми пользуются
# «Сжать» в macOS и «Сжатая ZIP-папка» в Windows.
_METHODS = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
# Коэффициент сжатия выше этого у файла больше BOMB_MIN_BYTES — похоже на «zip-бомбу».
BOMB_RATIO = 200
BOMB_MIN_BYTES = 1024 * 1024


class ArchiveError(ValueError):
    """Архив нельзя прочитать как выгрузку. Текст — для владельца, на русском."""


def is_zip(head: bytes) -> bool:
    return head[:4] == ZIP_MAGIC


def file_is_zip(path: Path) -> bool:
    with open(path, "rb") as fp:
        return is_zip(fp.read(4))


def _norm(name: str) -> str:
    return name.replace("\\", "/")


def suspicious(info: zipfile.ZipInfo) -> bool:
    """Член архива сжат так сильно, что похож на «zip-бомбу»."""
    return info.file_size > BOMB_MIN_BYTES and info.file_size > BOMB_RATIO * max(info.compress_size, 1)


def readable(info: zipfile.ZipInfo) -> bool:
    """Член архива можно прочитать: не зашифрован и сжат поддерживаемым способом."""
    return not (info.flag_bits & 0x1) and info.compress_type in _METHODS


def media_relpath(media_path: str | None) -> str | None:
    """Путь файла вложения из result.json — относительно папки result.json. Абсолютные пути,
    имена дисков и `..` не принимаются. Возвращает нормализованный путь или None."""
    if not isinstance(media_path, str):
        return None
    raw = _norm(media_path.strip())
    if not raw or raw.startswith("/") or (len(raw) > 1 and raw[1] == ":") or "\x00" in raw:
        return None
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return "/".join(parts)


class ExportArchive:
    """Открытый архив выгрузки. Не потокобезопасен: пользуйтесь из одного потока."""

    def __init__(self, path: Path) -> None:
        try:
            self._zip = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError, ValueError):
            raise ArchiveError(
                "архив zip повреждён или не дочитан — сожмите папку выгрузки заново "
                "или загрузите один файл result.json") from None
        try:
            self._members: dict[str, zipfile.ZipInfo] = {}
            for info in self._zip.infolist():
                if not info.is_dir():
                    self._members.setdefault(_norm(info.filename), info)
            self.result = self._find_result()
            self.base = posixpath.dirname(_norm(self.result.filename))
        except BaseException:
            self._zip.close()
            raise

    def _find_result(self) -> zipfile.ZipInfo:
        found = [(name.count("/"), len(name), name) for name in self._members
                 if posixpath.basename(name) == RESULT_NAME]
        if not found:
            raise ArchiveError(
                "в архиве нет файла result.json — нужен архив папки выгрузки Telegram Desktop "
                "в формате JSON (машиночитаемый JSON), а не HTML")
        info = self._members[min(found)[2]]
        if info.flag_bits & 0x1:
            raise ArchiveError("архив защищён паролем — сожмите папку выгрузки заново без пароля")
        if info.compress_type not in _METHODS:
            raise ArchiveError(
                "архив сжат способом, который сервис не читает — сожмите папку стандартной командой "
                "(Windows: «Сжатая ZIP-папка», macOS: «Сжать») или загрузите один файл result.json")
        if suspicious(info):
            raise ArchiveError("result.json в архиве сжат подозрительно сильно — такой архив не принимается")
        return info

    @property
    def result_size(self) -> int:
        return self.result.file_size

    def open_result(self) -> IO[bytes]:
        return self._zip.open(self.result)

    def media(self, media_path: str | None) -> zipfile.ZipInfo | None:
        """Член архива для файла вложения сообщения или None, если пути нет или он негоден."""
        rel = media_relpath(media_path)
        if rel is None:
            return None
        return self._members.get(posixpath.join(self.base, rel) if self.base else rel)

    def open(self, info: zipfile.ZipInfo) -> IO[bytes]:
        return self._zip.open(info)

    def close(self) -> None:
        self._zip.close()

    def __enter__(self) -> "ExportArchive":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
