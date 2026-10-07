# Формат страницы — идеи, не код (скопированных фрагментов в файле нет):
#   двухслойная страница «сводка переписывается, хронология только дописывается» и алиасы в шапке —
#   garrytan/gbrain (MIT), docs/GBRAIN_RECOMMENDED_SCHEMA.md@c5fb020, строки 45–55 и 164–206;
#   шапка YAML с датой обновления и перечень проверок страниц —
#   NousResearch/hermes-agent (MIT), skills/research/llm-wiki/SKILL.md@f97608f, строки 121–136 и 320–366.
"""Страница памяти как файл Markdown: разбор, сборка, безопасная запись. Без базы.

Формат — docs/memory.md:

    ---
    entity_id: person:12
    type: person
    aliases: [Иван Петров, Ваня]
    updated: 2026-10-06
    ---
    # Иван Петров

    <!-- summary: … -->        сводка: переписывается целиком из базы
    <!-- owner: … -->          блок владельца: код его не меняет никогда
    <!-- commitments: … -->    таблица обязательств: перерисовывается из базы
    <!-- timeline: … -->       хронология: строки только дописываются

Как находится блок владельца. Метка блока — отдельная строка вида `<!-- имя… -->`. Первая метка
в файле обязана быть summary, следующая за ней — owner; последняя — timeline, перед ней —
commitments. Блок владельца — всё между меткой owner и этой последней меткой commitments, байт
в байт, включая строки, похожие на метки. Если порядок нарушен (метку удалили, после хронологии
появилась ещё одна метка), разбор отказывается (`PageError`), и файл не трогается вовсе: код не
угадывает, где чей текст.

Всё, что попадает в файл из переписки или из ответа модели, проходит `md_inline`: одна строка,
без невидимых знаков, с экранированными знаками разметки. Поэтому чужой текст не может создать
ни метку блока, ни ссылку `msg:`, ни скрытый ключ строки.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..sanitize import clean_line

SUMMARY, OWNER, COMMITMENTS, TIMELINE = "summary", "owner", "commitments", "timeline"
BLOCKS = (SUMMARY, OWNER, COMMITMENTS, TIMELINE)

MARKERS = {
    SUMMARY: "<!-- summary: пересобирается ассистентом из хронологии и таблиц -->",
    OWNER: "<!-- owner: ассистент этот блок не трогает -->",
    COMMITMENTS: "<!-- commitments: перерисовывается из базы, правки здесь будут затёрты -->",
    TIMELINE: "<!-- timeline: только дописывается -->",
}

ORIGINS = ("owner", "other", "model")
ORIGIN_TEXT = {"owner": "сказал владелец", "other": "сказал собеседник", "model": "вывела модель"}

NO_SUMMARY = "_Сводки пока нет._"
NO_COMMITMENTS = "_Обязательств нет._"
SUMMARY_NOT_UPDATED = "_Сводка не обновлена: модель не дала пригодного ответа, показана прежняя._"
DISPUTED_MARK = "⚠ противоречие: "

PEOPLE_DIR = "people"
MAX_FILE_BYTES = 2_000_000     # файл больше не читается: это уже не страница
MAX_PAGE_BYTES = 64_000        # с этого размера проверка сообщает «страница слишком длинная»
SLUG_CHARS = 60

_MARKER_LINE = re.compile(r"<!--[ \t]*(summary|owner|commitments|timeline)\b[^\n]*?-->[ \t]*")
_MARKER_ANYWHERE = re.compile(r"<!--\s*(?:summary|owner|commitments|timeline)\b", re.IGNORECASE)
# Ссылка на сообщение и скрытый ключ строки. Обратная косая перед знаком — признак чужого текста.
_REF = re.compile(r"(?<!\\)\]\(msg:(\d{1,18})\)")
_KEY = re.compile(r"(?<!\\)<!--[ \t]*id:([a-z0-9]{1,24})[ \t]*-->[ \t]*$")
_MD_SPECIAL = re.compile(r"([\\\[\]<>|`])")
_REL_PATH = re.compile(rf"{PEOPLE_DIR}/[0-9a-zа-яё]+(?:-[0-9a-zа-яё]+)*\.md")
_FRONT_KEY = re.compile(r"([A-Za-z_][A-Za-z0-9_-]*):(?:[ \t]+(.*))?")
_PLAIN_ALIAS = re.compile(r"[A-Za-zА-Яа-яЁё](?:[A-Za-zА-Яа-яЁё0-9 .-]*[A-Za-zА-Яа-яЁё0-9.])?")
_YAML_WORDS = frozenset({"null", "true", "false", "yes", "no", "on", "off", "y", "n"})
_OUR_KEYS = ("entity_id", "type", "aliases", "updated")


class PageError(ValueError):
    """Файл страницы нельзя разобрать или записать; текст — для владельца."""


# --- текст из переписки и от модели ------------------------------------------------------------

def md_inline(text: str | None, limit: int = 200) -> str:
    """Чужой текст для строки страницы: одна строка, ограниченной длины, знаки разметки
    Markdown и HTML экранированы."""
    line = clean_line((text or "")[:5000], 10_000)
    if len(line) > limit:
        line = line[: limit - 1].rstrip() + "…"
    return _MD_SPECIAL.sub(r"\\\1", line)


def has_marker(text: str) -> bool:
    """Есть ли в тексте что-то похожее на метку блока (для проверки текста владельца из кабинета)."""
    return bool(_MARKER_ANYWHERE.search(text or ""))


def link(message_id: int) -> str:
    return f"[сообщение](msg:{int(message_id)})"


def refs(text: str) -> list[int]:
    """Идентификаторы сообщений, на которые ссылается текст."""
    return [int(m) for m in _REF.findall(text or "")]


def line_key(line: str) -> str | None:
    found = _KEY.search(line.rstrip("\r\n"))
    return found.group(1) if found else None


def without_keys(text: str) -> str:
    """Текст без скрытых ключей строк — для выдачи агенту и для запроса к модели."""
    return "\n".join(_KEY.sub("", line).rstrip() for line in (text or "").split("\n"))


# --- строки блоков --------------------------------------------------------------------------------

def _sources(ids: Iterable[int]) -> str:
    return " ".join(link(i) for i in ids)


def timeline_line(day: str, text: str, sources: Sequence[int], origin: str, key: str) -> str:
    """Строка хронологии. `text` — уже безопасный текст (чужие части прошли `md_inline`)."""
    if origin not in ORIGIN_TEXT or not re.fullmatch(r"[a-z0-9]{1,24}", key) or not sources:
        raise ValueError("строка хронологии: нужны происхождение, ключ и хотя бы один источник")
    return f"- {day} — {text} {_sources(sources)} ({ORIGIN_TEXT[origin]}) <!-- id:{key} -->"


def summary_block(statements: Sequence[dict[str, Any]], *, not_updated: bool = False) -> str:
    """Блок сводки из утверждений: [{"text", "sources": [id], "origin", "disputed"}]."""
    lines = [SUMMARY_NOT_UPDATED] if not_updated else []
    for item in statements:
        mark = DISPUTED_MARK if item.get("disputed") else ""
        lines.append(f"- {mark}{md_inline(item['text'], 300)} {_sources(item['sources'])} "
                     f"({ORIGIN_TEXT[item['origin']]})")
    if not statements:
        lines.append(NO_SUMMARY)
    return "\n".join(lines)


def commitments_block(rows: Sequence[dict[str, Any]]) -> str:
    """Таблица обязательств: [{"what", "due", "status", "message_id"}]; `what` и `due` — уже безопасные."""
    if not rows:
        return NO_COMMITMENTS
    lines = ["| Что | Срок | Статус | Источник |", "|---|---|---|---|"]
    for row in rows:
        lines.append(f"| {row['what']} | {row['due']} | {row['status']} | {link(row['message_id'])} |")
    return "\n".join(lines)


def _split(text: str) -> list[str]:
    """Строки с переводом строки в конце. Режется только по \\n: другие разделители — часть текста."""
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def timeline_keys(raw: str) -> set[str]:
    return {key for key in (line_key(line) for line in _split(raw)) if key}


def sweep_lines(raw: str, dead_ids: set[int], dead_keys: set[str] = frozenset()) -> tuple[str, int]:
    """Убирает строки, опирающиеся на удалённые сообщения. Остальные строки не меняются."""
    kept, removed = [], 0
    for line in _split(raw):
        if (dead_ids and dead_ids.intersection(refs(line))) or (dead_keys and line_key(line) in dead_keys):
            removed += 1
        else:
            kept.append(line)
    return "".join(kept), removed


def append_lines(raw: str, lines: Sequence[str]) -> str:
    """Дописывает строки в конец. Существующий текст не меняется."""
    if not lines:
        return raw
    glue = "" if not raw or raw.endswith("\n") else "\n"
    return raw + glue + "".join(line + "\n" for line in lines)


# --- шапка ----------------------------------------------------------------------------------------

def _alias(value: str) -> str:
    if _PLAIN_ALIAS.fullmatch(value) and value.lower() not in _YAML_WORDS:
        return value
    return json.dumps(value, ensure_ascii=False)


def _flow_list(values: Sequence[str]) -> str:
    return "[" + ", ".join(_alias(v) for v in values) + "]"


def _parse_flow_list(value: str) -> list[str] | None:
    value = value.strip()
    if not (value.startswith("[") and value.endswith("]")):
        return None
    out, i, body = [], 0, value[1:-1]
    while i < len(body):
        if body[i] in " \t,":
            i += 1
            continue
        if body[i] == '"':
            j = i + 1
            while j < len(body) and body[j] != '"':
                j += 2 if body[j] == "\\" else 1
            try:
                out.append(str(json.loads(body[i: j + 1])))
            except ValueError:
                return None
            i = j + 1
        else:
            j = body.find(",", i)
            j = len(body) if j < 0 else j
            out.append(body[i:j].strip())
            i = j
    return out


# --- страница -------------------------------------------------------------------------------------

@dataclass
class Page:
    entity_id: str = ""
    type: str = "person"
    aliases: list[str] = field(default_factory=list)
    updated: str = ""
    # строки шапки, которые писал не код (например, tags из Obsidian): сохраняются как есть
    front_extra: list[str] = field(default_factory=list)
    title: str = ""
    # текст между заголовком и сводкой, который писал не код: сохраняется
    head_extra: str = ""
    summary: str = ""
    # блок владельца — байт в байт, как в файле
    owner: str = "\n"
    commitments: str = ""
    timeline: str = ""


def parse(text: str) -> Page:
    """Разбирает файл страницы. Нарушенная разметка — `PageError`, без попыток угадать."""
    lines = _split(text[1:] if text.startswith("﻿") else text)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise PageError("нет шапки: файл должен начинаться со строки ---")
    end = next((n for n in range(1, min(len(lines), 200)) if lines[n].rstrip("\r\n") == "---"), None)
    if end is None:
        raise PageError("шапка не закрыта строкой ---")
    page = Page(owner="")
    seen: dict[str, str] = {}
    ours = False
    for line in lines[1:end]:
        raw = line.rstrip("\r\n")
        if not raw.strip():
            continue
        if raw[0] in " \t-":            # продолжение значения предыдущего ключа
            if not ours:
                page.front_extra.append(raw)
            continue
        found = _FRONT_KEY.fullmatch(raw)
        if found and found.group(1) in _OUR_KEYS:
            if found.group(1) in seen:
                raise PageError(f"в шапке дважды указан ключ {found.group(1)}")
            seen[found.group(1)] = (found.group(2) or "").strip()
            ours = True
        else:
            page.front_extra.append(raw)
            ours = False
    if not seen.get("entity_id"):
        raise PageError("в шапке нет entity_id")
    page.entity_id, page.type = seen["entity_id"], seen.get("type", "")
    page.aliases = _parse_flow_list(seen.get("aliases", "")) or []
    page.updated = seen.get("updated", "")

    body = lines[end + 1:]
    marks = [(n, found.group(1)) for n, line in enumerate(body)
             if (found := _MARKER_LINE.fullmatch(line.rstrip("\r\n")))]
    names = [name for _, name in marks]
    if len(marks) < 4:
        missing = [name for name in BLOCKS if name not in names]
        raise PageError("нет меток блоков: " + ", ".join(missing or BLOCKS))
    if names[0] != SUMMARY or names[1] != OWNER:
        raise PageError("первыми должны идти блоки summary и owner")
    if names[-1] != TIMELINE or names[-2] != COMMITMENTS:
        raise PageError("последними должны идти блоки commitments и timeline, после хронологии меток нет")
    s, o, c, t = marks[0][0], marks[1][0], marks[-2][0], marks[-1][0]

    head = body[:s]
    title_at = next((n for n, line in enumerate(head) if line.startswith("# ")), None)
    if title_at is not None:
        page.title = head[title_at][2:].strip()
        head = head[:title_at] + head[title_at + 1:]
    page.head_extra = "".join(head).strip("\r\n") if "".join(head).strip() else ""
    page.summary = "".join(body[s + 1: o]).strip("\r\n")
    page.owner = "".join(body[o + 1: c])
    page.commitments = "".join(body[c + 1: t]).strip("\r\n")
    page.timeline = "".join(body[t + 1:])
    return page


def render(page: Page) -> str:
    """Собирает файл. Блок владельца и строки хронологии выводятся как есть."""
    out = ["---\n", f"entity_id: {page.entity_id}\n", f"type: {page.type}\n",
           f"aliases: {_flow_list(page.aliases)}\n", f"updated: {page.updated}\n"]
    out.extend(line + "\n" for line in page.front_extra)
    out.append("---\n")
    out.append(f"# {page.title}\n\n")
    if page.head_extra:
        out.append(page.head_extra + "\n\n")
    for name in (SUMMARY, OWNER, COMMITMENTS, TIMELINE):
        out.append(MARKERS[name] + "\n")
        value = getattr(page, name)
        if name in (OWNER, TIMELINE):
            out.append(value if not value or value.endswith("\n") else value + "\n")
        else:
            out.append(value.strip("\r\n") + "\n\n" if value.strip() else "\n")
    return "".join(out)


def owner_text(text: str) -> str:
    """Текст владельца из кабинета в том виде, в каком он ляжет в блок: с пустой строкой после."""
    text = text.replace("\x00", "").replace("\r\n", "\n").strip("\n")
    return text + "\n\n" if text.strip() else "\n"


def digest(data: bytes | str) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


# --- проверки одного файла -------------------------------------------------------------------------

def lint_text(text: str, *, entity_id: str) -> tuple[list[tuple[str, str]], Page | None]:
    """Проверки, которым хватает самого файла. Возвращает [(код, пояснение)] и разобранную страницу.
    В пояснениях нет текста страницы: только имена блоков и номера строк."""
    found: list[tuple[str, str]] = []
    if len(text.encode("utf-8")) > MAX_PAGE_BYTES:
        found.append(("too_long", f"больше {MAX_PAGE_BYTES // 1000} КБ"))
    try:
        page = parse(text)
    except PageError as exc:
        found.append(("structure", str(exc)))
        return found, None
    if page.entity_id != entity_id:
        found.append(("front_matter", "entity_id в шапке не совпадает с записью в базе"))
    if page.type != "person":
        found.append(("front_matter", "поле type должно быть person"))
    try:
        date.fromisoformat(page.updated)
    except ValueError:
        found.append(("front_matter", "поле updated должно быть датой вида ГГГГ-ММ-ДД"))
    if not page.title:
        found.append(("front_matter", "нет заголовка страницы"))
    for block in (SUMMARY, TIMELINE):
        for n, line in enumerate(_split(getattr(page, block)), start=1):
            if line.startswith("- ") and not refs(line):
                found.append(("no_source", f"{block}: строка {n} без ссылки на сообщение"))
    return found, page


# --- файлы -----------------------------------------------------------------------------------------

def slug(name: str | None) -> str:
    """Часть имени файла из имени человека: только строчные буквы, цифры и дефис."""
    text = unicodedata.normalize("NFKC", name or "").lower()
    text = re.sub(r"[^0-9a-zа-яё]+", "-", text).strip("-")[:SLUG_CHARS].strip("-")
    return text or "person"


def person_path(name: str | None, person_id: int) -> str:
    return f"{PEOPLE_DIR}/{slug(name)}-{int(person_id)}.md"


def is_page_path(rel: Any) -> bool:
    """Похож ли относительный путь на файл страницы: каталог людей, имя из букв, цифр и дефисов."""
    return isinstance(rel, str) and bool(_REL_PATH.fullmatch(rel))


def _target(root: Path, rel: str) -> Path:
    """Полный путь файла страницы. Имя проверяется по образцу, каталог — что он настоящий и внутри
    каталога страниц: ни имя человека, ни подложенная ссылка не выведут запись наружу."""
    if not is_page_path(rel):
        raise PageError("недопустимое имя файла страницы")
    base = Path(os.path.realpath(root))
    folder = base / PEOPLE_DIR
    if folder.is_symlink() or (folder.exists() and Path(os.path.realpath(folder)) != folder):
        raise PageError("каталог страниц подменён ссылкой")
    return folder / rel.split("/", 1)[1]


def read_page(root: Path, rel: str) -> str | None:
    """Текст файла страницы или None, если файла нет. По ссылкам не ходит."""
    path = _target(root, rel)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise PageError("на месте файла страницы — ссылка") from None
        raise
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise PageError("файл слишком большой для страницы")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise PageError("файл не читается как текст UTF-8") from None


def write_page(root: Path, rel: str, text: str) -> None:
    """Записывает файл целиком: временный файл в том же каталоге, затем переименование."""
    path = _target(root, rel)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix="~")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        _unlink_quietly(tmp)
        raise
    try:
        folder = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(folder)
    finally:
        os.close(folder)


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def list_files(root: Path) -> list[str]:
    """Относительные пути файлов .md в каталоге людей (для поиска файлов без записи в базе)."""
    base = Path(os.path.realpath(root)) / PEOPLE_DIR
    if base.is_symlink() or not base.is_dir():
        return []
    return sorted(f"{PEOPLE_DIR}/{entry.name}" for entry in os.scandir(base)
                  if entry.name.endswith(".md") and not entry.name.startswith("."))
