"""Реестр людей: кто есть кто в переписке.

Источник истины — Postgres. `peers` — это учётные записи Telegram; человек (`people`) может
иметь несколько учётных записей и несколько имён (`person_aliases`). Чтобы упоминание в тексте
(«Ивану Иванычу», «с Петровым») находило человека, из реестра вперёд строится указатель
словоформ (`person_forms`): все падежи имени, отчества и фамилии, уменьшительные, разговорные
отчества, алиасы.

Правила:
  * запись для учётной записи Telegram заводится автоматически, но две учётные записи в одного
    человека сами не сливаются никогда: вероятное совпадение становится предложением владельцу;
  * слияние, разделение и алиасы — действия владельца;
  * сравнение идёт по ключу: нижний регистр, ё->е, й->и, э->е, латиница -> кириллица по своей
    таблице (сторонние транслитераторы под GPL не используются).

Библиотеки: pytrovich (MIT) — падежи имён; pymorphy3 (MIT) — начальная форма неизвестной
словоформы; rapidfuzz (MIT) — похожесть отображаемых имён; pg_trgm — отбор кандидатов в базе.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable

import asyncpg

from .. import store

CUTOFF = 85        # с какой похожести отображаемое имя считается тем же человеком
MARGIN = 5         # насколько лучший кандидат должен опережать следующего
TOKEN_CUTOFF = 80  # каждое слово имени должно найти себе пару не хуже этого

# --- ключ сравнения ---------------------------------------------------------------

_LAT = {
    "shch": "щ", "sch": "щ", "zh": "ж", "kh": "х", "ts": "ц", "ch": "ч", "sh": "ш", "ph": "ф",
    "yu": "ю", "ju": "ю", "ya": "я", "ja": "я", "yo": "е", "ye": "е", "ck": "к", "th": "т",
    "ee": "и", "oo": "у",
    "a": "а", "b": "б", "v": "в", "g": "г", "d": "д", "e": "е", "z": "з", "i": "и", "y": "и",
    "j": "и", "k": "к", "l": "л", "m": "м", "n": "н", "o": "о", "p": "п", "r": "р", "s": "с",
    "t": "т", "u": "у", "f": "ф", "h": "х", "c": "к", "w": "в", "x": "кс", "q": "к",
}
_LAT_RE = re.compile("|".join(sorted(_LAT, key=len, reverse=True)))
_LAT_FINAL_IA = re.compile(r"ia\b")
_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё]+(?:-[A-Za-zА-Яа-яЁё]+)*")
# всё, что в отображаемом имени идёт после этих знаков, — компания, должность, подпись
_NAME_TAIL_RE = re.compile(r"[|/(\[@,•·—–]| - ")


def to_cyrillic(text: str) -> str:
    """Латиница -> кириллица по своей таблице («Aleksandr Erman» -> «александр ерман»)."""
    text = _LAT_FINAL_IA.sub("ия", text.lower())
    return _LAT_RE.sub(lambda m: _LAT[m.group()], text)


def fold(text: str) -> str:
    """Ключ сравнения имени или словоформы."""
    text = to_cyrillic(text.lower().replace("ё", "е"))
    text = text.replace("й", "и").replace("э", "е")
    text = re.sub(r"[ьъ]", "", text)
    text = re.sub(r"[^а-я ]+", " ", text)
    return " ".join(re.sub(r"ии$", "и", token) for token in text.split())


def _clean_display(name: str | None) -> str:
    """Имя для показа: без управляющих и невидимых знаков, одной строкой."""
    text = "".join(ch for ch in (name or "") if ch.isprintable())
    return re.sub(r"\s+", " ", text).strip()[:120]


# --- словари (собственные, небольшие) ---------------------------------------------------

# полное имя -> уменьшительные и варианты написания
DIMINUTIVES: dict[str, tuple[str, ...]] = {
    "Александр": ("Саша", "Саня", "Шура", "Сан", "Алекс"), "Александра": ("Саша", "Шура", "Саня"),
    "Алексей": ("Лёша", "Алёша", "Лёха"), "Анастасия": ("Настя",), "Анатолий": ("Толя",),
    "Андрей": ("Андрюша",), "Анна": ("Аня",), "Антон": ("Антоша",), "Борис": ("Боря",),
    "Валентин": ("Валя",), "Валентина": ("Валя",), "Валерий": ("Валера",), "Василий": ("Вася",),
    "Виктор": ("Витя",), "Виктория": ("Вика",), "Виталий": ("Виталик",),
    "Владимир": ("Вова", "Володя"), "Владислав": ("Влад", "Слава"), "Вячеслав": ("Слава",),
    "Галина": ("Галя",), "Геннадий": ("Гена",), "Георгий": ("Гоша", "Жора"), "Григорий": ("Гриша",),
    "Дарья": ("Даша",), "Дмитрий": ("Дима", "Митя"), "Евгений": ("Женя",), "Евгения": ("Женя",),
    "Екатерина": ("Катя",), "Елена": ("Лена",), "Елизавета": ("Лиза",), "Иван": ("Ваня",),
    "Илья": ("Илюша",), "Ирина": ("Ира",), "Константин": ("Костя",), "Ксения": ("Ксюша",),
    "Леонид": ("Лёня",), "Любовь": ("Люба",), "Людмила": ("Люда", "Мила"), "Максим": ("Макс",),
    "Маргарита": ("Рита",), "Мария": ("Маша",), "Михаил": ("Миша",), "Надежда": ("Надя",),
    "Наталья": ("Наташа", "Наталия"), "Николай": ("Коля",), "Ольга": ("Оля",), "Павел": ("Паша",),
    "Пётр": ("Петя",), "Роман": ("Рома",), "Светлана": ("Света",), "Семён": ("Сёма",),
    "Сергей": ("Серёжа", "Серёга"), "Софья": ("Соня", "София"), "Станислав": ("Стас",),
    "Степан": ("Стёпа",), "Татьяна": ("Таня",), "Тимофей": ("Тима",), "Фёдор": ("Федя",),
    "Юлия": ("Юля",), "Юрий": ("Юра",), "Яков": ("Яша",), "Ярослав": ("Ярик",),
}
_FEMALE_NAMES = frozenset({
    "Александра", "Анастасия", "Анна", "Валентина", "Виктория", "Галина", "Дарья", "Евгения",
    "Екатерина", "Елена", "Елизавета", "Ирина", "Ксения", "Любовь", "Людмила", "Маргарита",
    "Мария", "Надежда", "Наталья", "Ольга", "Светлана", "Софья", "Татьяна", "Юлия",
})
_CANONICAL_BY_KEY: dict[str, str] = {fold(name): name for name in DIMINUTIVES}
_CANONICALS_OF: dict[str, tuple[str, ...]] = {}
for _full, _shorts in DIMINUTIVES.items():
    for _short in _shorts:
        _CANONICALS_OF[fold(_short)] = _CANONICALS_OF.get(fold(_short), ()) + (_full,)

# разговорные отчества, которые не получаются правилом
_COLLOQUIAL: dict[str, tuple[str, ...]] = {
    "михаилович": ("Михалыч",), "павлович": ("Палыч",), "александрович": ("Саныч",),
    "николаевич": ("Николаич",), "василевич": ("Василич", "Васильич"), "дмитриевич": ("Дмитрич",),
    "игоревич": ("Игорич",), "михаиловна": ("Михална",), "павловна": ("Пална",),
    "александровна": ("Санна",), "николаевна": ("Николавна",),
}
_PATRONYMIC_RE = re.compile(r"(?:ович|евич|ич|овна|евна|ична|инична)$")

_STOP_WORDS = frozenset(
    "с со к ко у от для и или по на в во о об про за до из не при а но же ли бы "
    "г-н г-жа господин госпожа товарищ уважаемый уважаемая коллега".split()
)


# --- морфология (ленивая загрузка: словари большие) ---------------------------------------

@lru_cache(maxsize=1)
def _morph():
    import pymorphy3
    return pymorphy3.MorphAnalyzer()


@lru_cache(maxsize=1)
def _petrovich():
    from pytrovich.enums import Case, Gender, NamePart
    from pytrovich.maker import PetrovichDeclinationMaker
    return PetrovichDeclinationMaker(), Case, Gender, NamePart


def _decline(part: str, value: str, genders: Iterable[str]) -> set[str]:
    """Все падежи слова как имени (first), отчества (middle) или фамилии (last)."""
    maker, Case, Gender, NamePart = _petrovich()
    name_part = {"first": NamePart.FIRSTNAME, "middle": NamePart.MIDDLENAME, "last": NamePart.LASTNAME}[part]
    out = {value}
    for gender in genders:
        for case in Case:
            try:
                out.add(maker.make(name_part, Gender.MALE if gender == "m" else Gender.FEMALE, case, value))
            except Exception:  # редкие формы, на которых правила склонения спотыкаются
                continue
    return out


@lru_cache(maxsize=4096)
def _name_tags(word: str) -> tuple[tuple[frozenset[str], float, str, str | None], ...]:
    """Разборы слова, в которых оно — имя, отчество или фамилия: (пометы, вес, начальная форма, именительный)."""
    out = []
    for parse in _morph().parse(word):
        grammemes = frozenset(parse.tag.grammemes)
        if grammemes & {"Name", "Patr", "Surn"}:
            nominative = parse.inflect({"nomn"})
            out.append((grammemes, parse.score, parse.normal_form, nominative.word if nominative else None))
    return tuple(out)


def _looks_like_name(word: str) -> bool:
    if any(score >= 0.15 for _, score, _, _ in _name_tags(word)):
        return True
    return word[:1].isupper() and not _morph().word_is_known(word)


def _morph_keys(word: str) -> set[str]:
    """Ключи для словоформы, которой нет в указателе: именительный падеж того же рода,
    а для разговорных отчеств («Иванычу») — полное отчество."""
    keys: set[str] = set()
    for grammemes, _, normal_form, nominative in _name_tags(word):
        if nominative:
            keys.add(fold(nominative))
        if "Infr" in grammemes or "Name" in grammemes:
            keys.add(fold(normal_form))
    for key in list(keys) + [fold(word)]:
        for canonical in _CANONICALS_OF.get(key, ()):
            keys.add(fold(canonical))
    keys.discard("")
    return keys


# --- разбор отображаемого имени -----------------------------------------------------------

@dataclass(frozen=True)
class ParsedName:
    first: str | None = None
    middle: str | None = None
    last: str | None = None
    gender: str | None = None

    @property
    def parts(self) -> int:
        return sum(1 for value in (self.first, self.middle, self.last) if value)


def _is_first_name(word: str) -> bool:
    key = fold(word)
    if key in _CANONICAL_BY_KEY or key in _CANONICALS_OF:
        return True
    return any("Name" in g and score >= 0.3 for g, score, _, _ in _name_tags(word))


def _is_patronymic(word: str) -> bool:
    if not _PATRONYMIC_RE.search(word.lower()) or len(word) < 6:
        return False
    tags = _name_tags(word)
    patr = max((score for g, score, _, _ in tags if "Patr" in g), default=0.0)
    surn = max((score for g, score, _, _ in tags if "Surn" in g), default=0.0)
    return patr > 0 and patr >= surn


def _gender_of(first: str | None, middle: str | None, last: str | None) -> str | None:
    if middle:
        return "f" if middle.lower().endswith("на") else "m"
    if first:
        key = fold(first)
        canonical = _CANONICAL_BY_KEY.get(key)
        if canonical:
            return "f" if canonical in _FEMALE_NAMES else "m"
        options = {("f" if c in _FEMALE_NAMES else "m") for c in _CANONICALS_OF.get(key, ())}
        if len(options) == 1:
            return options.pop()
        if not options:
            for grammemes, score, _, _ in _name_tags(first):
                if "Name" in grammemes and score >= 0.3:
                    if "femn" in grammemes:
                        return "f"
                    if "masc" in grammemes:
                        return "m"
    if last and re.search(r"(?:ова|ева|ёва|ина|ына|ская|цкая|ая)$", last.lower()):
        return "f"
    return None


def parse_name(display: str | None) -> ParsedName:
    """Разбирает отображаемое имя Telegram на имя, отчество и фамилию.

    Хвост после «|», «/», «(», «@», запятой и тире — компания или подпись — отбрасывается.
    Латиница переводится в кириллицу. Если разобрать нечего, все части пустые.
    """
    head = _NAME_TAIL_RE.split(_clean_display(display))[0]
    words = []
    for raw in _WORD_RE.findall(head):
        word = raw if re.search(r"[А-Яа-яЁё]", raw) else to_cyrillic(raw)
        if len(word) >= 2:
            words.append(word[:1].upper() + word[1:])
    words = words[:4]
    if not words:
        return ParsedName()
    first = middle = last = None
    rest = []
    for word in words:
        if middle is None and len(words) > 1 and _is_patronymic(word):
            middle = word
        else:
            rest.append(word)
    if len(rest) == 1:
        if _is_first_name(rest[0]) or middle is not None or len(words) == 1:
            first = rest[0]
        else:
            last = rest[0]
    elif len(rest) >= 2:
        a, b = rest[0], rest[1]
        # в Telegram порядок «имя фамилия»; «Петров Иван» переставляем
        if not _is_first_name(a) and _is_first_name(b):
            first, last = b, a
        else:
            first, last = a, b
    if len(words) == 1 and not _is_first_name(words[0]):
        first, last = None, words[0]
    return ParsedName(first, middle, last, _gender_of(first, middle, last))


def _all_name_words(text: str) -> bool:
    """Все слова — имя, отчество или фамилия («Наталья Сергеевна»), а не прозвище («Петрович с Фасада»)."""
    words = _WORD_RE.findall(_NAME_TAIL_RE.split(text)[0])
    if not words or len(words) > 3:
        return False
    # предлог или слово со строчной буквы — это описание, а не имя
    if any(len(w) < 2 or not w[:1].isupper() or w.lower() in _STOP_WORDS for w in words):
        return False
    if len(words) == 1:
        return True
    cyr = [w if re.search(r"[А-Яа-яЁё]", w) else to_cyrillic(w).capitalize() for w in words]
    return any(_is_first_name(w) or _is_patronymic(w) for w in cyr)


def _colloquial_patronymics(middle: str) -> set[str]:
    key = fold(middle)
    out = set(_COLLOQUIAL.get(key, ()))
    lower = middle.lower().replace("ё", "е")
    if lower.endswith("ович"):
        out.add(lower[:-4] + "ыч")
    elif lower.endswith("евич"):
        out.add(lower[:-4] + "ич")
    elif lower.endswith("овна"):
        out.add(lower[:-4] + "на")
    elif lower.endswith("евна"):
        out.add(lower[:-4] + "вна")
    return {value[:1].upper() + value[1:] for value in out}


def name_forms(name: ParsedName) -> set[tuple[str, str]]:
    """Словоформы человека: (ключ, роль). Строятся вперёд — от записи к формам."""
    genders = (name.gender,) if name.gender else ("m", "f")
    forms: set[tuple[str, str]] = set()

    def add(part: str, value: str, as_part: str | None = None) -> None:
        for form in _decline(as_part or part, value, genders):
            key = fold(form)
            if len(key) >= 2:
                forms.add((key, part))

    if name.first:
        variants = {name.first}
        key = fold(name.first)
        canonicals = set(_CANONICALS_OF.get(key, ()))
        if key in _CANONICAL_BY_KEY:
            canonicals.add(_CANONICAL_BY_KEY[key])
        if name.gender:
            canonicals = {c for c in canonicals if (c in _FEMALE_NAMES) == (name.gender == "f")} or canonicals
        for canonical in canonicals:
            variants.add(canonical)
            variants.update(DIMINUTIVES[canonical])
        for variant in variants:
            add("first", variant)
    if name.middle:
        add("middle", name.middle)
        for variant in _colloquial_patronymics(name.middle):
            add("middle", variant, as_part="first")  # разговорные отчества склоняются как имена
    if name.last:
        add("last", name.last)
    return forms


def _display_variants(name: ParsedName) -> set[str]:
    """Как человека могут назвать в отображаемом имени: ключи для сравнения по похожести."""
    firsts = set()
    if name.first:
        firsts.add(name.first)
        key = fold(name.first)
        canonicals = set(_CANONICALS_OF.get(key, ()))
        if key in _CANONICAL_BY_KEY:
            canonicals.add(_CANONICAL_BY_KEY[key])
        for canonical in canonicals:
            firsts.add(canonical)
            firsts.update(DIMINUTIVES[canonical])
    out = set()
    for first in firsts or {""}:
        out.add(fold(" ".join(v for v in (first, name.last) if v)))
        out.add(fold(" ".join(v for v in (first, name.middle, name.last) if v)))
        out.add(fold(" ".join(v for v in (first, name.middle) if v)))
    out.discard("")
    return out


# --- видимость --------------------------------------------------------------------------
# То же правило, что у архива (archive._PEOPLE): человек показывается, только если у него есть
# видимый след — неисключённый личный чат или хотя бы одно неудалённое сообщение в неисключённом
# чате. Иначе по имени, алиасу или адресу можно было бы узнать о существовании исключённого чата.
# Сам владелец и люди, заведённые владельцем без учётной записи Telegram, видны всегда.

def _peer_trace(peer: str) -> str:
    """Условие «у учётной записи есть видимый след»; peer — выражение с peers.id."""
    return f"""(EXISTS (SELECT 1 FROM chats vc WHERE vc.peer_id = {peer} AND NOT vc.excluded)
                OR EXISTS (SELECT 1 FROM messages vm JOIN chats vc ON vc.id = vm.chat_id
                           WHERE vm.sender_peer_id = {peer} AND vm.deleted_at IS NULL AND NOT vc.excluded))"""


def visible_person(alias: str = "p") -> str:
    """Условие SQL «человек виден»; alias — псевдоним таблицы people."""
    return f"""({alias}.is_owner
                OR NOT EXISTS (SELECT 1 FROM person_peers vp WHERE vp.person_id = {alias}.id)
                OR EXISTS (SELECT 1 FROM person_peers vp
                           WHERE vp.person_id = {alias}.id AND {_peer_trace('vp.peer_id')}))"""


# Поля словаря человека, взятые из чужого текста (имена и адреса из Telegram, алиасы).
UNTRUSTED_FIELDS = ["display_name", "first_name", "middle_name", "last_name",
                    "aliases[].alias", "peers[].name", "peers[].username"]


# --- чтение ----------------------------------------------------------------------------

def _person_dict(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": row["id"], "display_name": row["display_name"], "first_name": row["first_name"],
        "middle_name": row["middle_name"], "last_name": row["last_name"], "gender": row["gender"],
        "is_owner": row["is_owner"], "origin": row["origin"], "confirmed": row["confirmed"],
        "merged_into": row["merged_into"], "untrusted_fields": list(UNTRUSTED_FIELDS),
    }


async def active_id(conn: asyncpg.Connection, person_id: int) -> int | None:
    """Идентификатор действующей записи: по цепочке слияний, если запись была влита в другую."""
    seen = set()
    current: int | None = person_id
    while current is not None and current not in seen:
        seen.add(current)
        row = await conn.fetchrow("SELECT merged_into FROM people WHERE id = $1", current)
        if row is None:
            return None
        if row["merged_into"] is None:
            return current
        current = row["merged_into"]
    return None


async def visible_id(conn: asyncpg.Connection, person_id: int) -> int | None:
    """Идентификатор действующей записи, если человек виден (см. «видимость»); иначе None."""
    current = await active_id(conn, person_id)
    if current is None:
        return None
    return await conn.fetchval(f"SELECT p.id FROM people p WHERE p.id = $1 AND {visible_person('p')}", current)


async def get_person(conn: asyncpg.Connection, person_id: int) -> dict[str, Any] | None:
    """Человек с алиасами и учётными записями. Для влитой записи возвращается та, в которую влили.

    Человек без видимого следа (его единственный чат исключён) не возвращается. У видимого
    человека не показываются учётные записи без видимого следа и имена, взятые из них.
    """
    current = await visible_id(conn, person_id)
    if current is None:
        return None
    row = await conn.fetchrow("SELECT * FROM people WHERE id = $1", current)
    out = _person_dict(row)
    out["aliases"] = [
        {"alias": r["alias"], "origin": r["origin"]} for r in await conn.fetch(
            f"""SELECT a.alias, a.origin FROM person_aliases a
                WHERE a.person_id = $1 AND (a.peer_id IS NULL OR {_peer_trace('a.peer_id')})
                ORDER BY a.id""", current)
    ]
    out["peers"] = [
        {"peer_id": r["id"], "tg_id": r["tg_id"], "name": r["name"], "username": r["username"]}
        for r in await conn.fetch(
            f"""SELECT p.id, p.tg_id, p.name, p.username FROM person_peers pp
                JOIN peers p ON p.id = pp.peer_id
                WHERE pp.person_id = $1 AND {_peer_trace('pp.peer_id')} ORDER BY pp.linked_at, p.id""",
            current)
    ]
    return out


async def person_for_peer(conn: asyncpg.Connection, peer_id: int) -> int | None:
    return await conn.fetchval("SELECT person_id FROM person_peers WHERE peer_id = $1", peer_id)


async def chat_people(conn: asyncpg.Connection, chat_id: int) -> set[int]:
    """Люди чата: собеседник личного чата и все, кто в чате писал."""
    rows = await conn.fetch(
        """SELECT DISTINCT pp.person_id FROM person_peers pp
           WHERE pp.peer_id IN (SELECT peer_id FROM chats WHERE id = $1
                                UNION SELECT sender_peer_id FROM messages
                                      WHERE chat_id = $1 AND sender_peer_id IS NOT NULL)""",
        chat_id,
    )
    return {r["person_id"] for r in rows}


# --- указатель словоформ ------------------------------------------------------------------

async def rebuild_forms(conn: asyncpg.Connection, person_id: int | None = None) -> int:
    """Перестраивает указатель словоформ одного человека или всех. Возвращает число форм."""
    if person_id is None:
        ids = [r["id"] for r in await conn.fetch("SELECT id FROM people WHERE merged_into IS NULL")]
        await conn.execute("DELETE FROM person_forms WHERE person_id IN (SELECT id FROM people WHERE merged_into IS NOT NULL)")
    else:
        ids = [person_id]
    total = 0
    for pid in ids:
        row = await conn.fetchrow(
            "SELECT first_name, middle_name, last_name, gender, merged_into FROM people WHERE id = $1", pid)
        await conn.execute("DELETE FROM person_forms WHERE person_id = $1", pid)
        if row is None or row["merged_into"] is not None:
            continue
        forms = name_forms(ParsedName(row["first_name"], row["middle_name"], row["last_name"], row["gender"]))
        aliases = await conn.fetch("SELECT alias, alias_norm, origin FROM person_aliases WHERE person_id = $1", pid)
        for alias in aliases:
            if alias["alias"].startswith("@"):
                forms.add((alias["alias"][1:].lower(), "username"))
                continue
            if alias["origin"] == "telegram" or _all_name_words(alias["alias"]):
                parsed = parse_name(alias["alias"])
                # род берём у самой записи: алиас «Саша» сам его не знает
                forms |= name_forms(ParsedName(parsed.first, parsed.middle, parsed.last, row["gender"] or parsed.gender))
            if alias["alias_norm"]:
                forms.add((alias["alias_norm"], "alias"))
                if " " not in alias["alias_norm"]:
                    # одиночное прозвище («Михалыч») склоняем как имя
                    for form in _decline("first", alias["alias"].strip(), (row["gender"],) if row["gender"] else ("m", "f")):
                        forms.add((fold(form), "alias"))
        forms = {(form, slot) for form, slot in forms if form}
        await conn.executemany(
            "INSERT INTO person_forms (form, person_id, slot) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
            [(form, pid, slot) for form, slot in sorted(forms)],
        )
        total += len(forms)
    return total


# --- поиск ------------------------------------------------------------------------------

async def resolve_mention(
    conn: asyncpg.Connection, mention: str, *, chat_id: int | None = None,
) -> dict[str, Any]:
    """Находит, о ком речь в упоминании («Ивану Иванычу», «с Петровым», «@ipetrov»).

    Возвращает {"status": match | ambiguous | partial | none, "candidates": [...]}.
      match     — ровно один человек подходит под все слова упоминания;
      ambiguous — подходят несколько (два Ивана; «Петрову» — Петров или Петрова);
      partial   — человек подходит, но в упоминании есть слово, похожее на имя, которого у него нет
                  («Ивану Смирнову» при одном Иване Петрове) — выдавать как совпадение нельзя;
      none      — никого.
    `chat_id` даёт перевес участникам этого чата, если по именам выбрать нельзя.
    """
    words = [w for w in re.findall(r"@?[A-Za-zА-Яа-яЁё_0-9]+(?:-[A-Za-zА-Яа-яЁё]+)*", mention or "")][:12]
    direct: dict[str, set[str]] = {}
    extra: dict[str, set[str]] = {}
    for word in words:
        if word.startswith("@"):
            direct[word] = {word[1:].lower()}
            continue
        if len(word) < 2 or word.lower() in _STOP_WORDS or not re.search(r"[A-Za-zА-Яа-яЁё]", word):
            continue
        key = fold(word)
        if key:
            direct[word] = {key}
            cyr = word if re.search(r"[А-Яа-яЁё]", word) else to_cyrillic(word)
            extra[word] = _morph_keys(cyr) - {key}
    if not direct:
        return {"status": "none", "candidates": []}
    whole = fold(mention)
    keys = set().union(*direct.values(), *extra.values()) | ({whole} if whole else set())
    rows = await conn.fetch(
        f"""SELECT f.form, f.person_id, f.slot FROM person_forms f
            JOIN people p ON p.id = f.person_id AND p.merged_into IS NULL AND {visible_person('p')}
            WHERE f.form = ANY($1::text[])""",
        sorted(keys),
    )
    by_form: dict[str, set[tuple[int, str]]] = {}
    for r in rows:
        by_form.setdefault(r["form"], set()).add((r["person_id"], r["slot"]))

    slots: dict[int, set[str]] = {}
    hits_per_word: list[set[int]] = []
    unknown_names = 0
    for word, own in direct.items():
        found = set().union(*(by_form.get(k, set()) for k in own))
        if word.startswith("@"):
            found = {(pid, slot) for pid, slot in found if slot == "username"}
        else:
            found = {(pid, slot) for pid, slot in found if slot != "username"}
            if not found:  # словоформы нет в указателе — пробуем через начальную форму
                found = {(pid, slot) for k in extra.get(word, ()) for pid, slot in by_form.get(k, set())
                         if slot != "username"}
        if not found:
            cyr = word if re.search(r"[А-Яа-яЁё]", word) else to_cyrillic(word)
            shown = cyr[:1].upper() + cyr[1:] if word[:1].isupper() else cyr
            if word.startswith("@") or _looks_like_name(shown):
                unknown_names += 1
            continue
        hits_per_word.append({pid for pid, _ in found})
        for pid, slot in found:
            slots.setdefault(pid, set()).add(slot)
    # многословный алиас целиком («Петрович с Фасада»)
    whole_hits = {pid for pid, slot in by_form.get(whole, set()) if slot == "alias"} if " " in whole else set()
    for pid in whole_hits:
        slots.setdefault(pid, set()).add("alias")

    fitting = [pid for pid in slots
               if pid in whole_hits or (hits_per_word and all(pid in hit for hit in hits_per_word))]
    if not fitting:
        return {"status": "none", "candidates": []}
    in_chat = await chat_people(conn, chat_id) if chat_id is not None else set()
    people = {r["id"]: r for r in await conn.fetch(
        "SELECT id, display_name, is_owner FROM people WHERE id = ANY($1::bigint[])", fitting)}
    candidates = sorted(
        ({"person_id": pid, "display_name": people[pid]["display_name"], "is_owner": people[pid]["is_owner"],
          "slots": sorted(slots[pid]), "score": len(slots[pid]) + (2 if pid in whole_hits else 0),
          "in_chat": pid in in_chat} for pid in fitting),
        key=lambda c: (-c["score"], not c["in_chat"], c["person_id"]),
    )
    top = [c for c in candidates if c["score"] == candidates[0]["score"]]
    if len(top) > 1:
        local = [c for c in top if c["in_chat"]]
        if len(local) == 1:
            top = local
            candidates.sort(key=lambda c: (c is not local[0], -c["score"], c["person_id"]))
    if len(top) > 1:
        status = "ambiguous"
    elif unknown_names and top[0]["person_id"] not in whole_hits:
        status = "partial"
    else:
        status = "match"
    return {"status": status, "candidates": candidates[:10]}


async def match_display_name(
    conn: asyncpg.Connection, display: str, *, exclude_person_id: int | None = None,
    cutoff: int = CUTOFF, margin: int = MARGIN,
) -> dict[str, Any]:
    """Сопоставляет отображаемое имя («Пётр Петренко | ГК Фасад», «Aleksandr Erman») с реестром.

    Возвращает {"status": match | ambiguous | none, "candidates": [{person_id, display_name, score}]}.
    Одно слово («Иван») сравнивается только на точное совпадение с именем, фамилией или алиасом.
    Сливать по результату нельзя: это основание для предложения владельцу.
    """
    from rapidfuzz import fuzz

    query = fold(_NAME_TAIL_RE.split(_clean_display(display))[0])
    if not query:
        return {"status": "none", "candidates": []}
    q_words = query.split()
    parsed = parse_name(display)
    probe = {query, *q_words} | {form for form, _ in name_forms(parsed)}
    rows = await conn.fetch(
        f"""SELECT DISTINCT p.id FROM people p
           WHERE p.merged_into IS NULL AND p.id IS DISTINCT FROM $3::bigint AND {visible_person('p')}
             AND (p.id IN (SELECT person_id FROM person_forms WHERE form = ANY($1::text[]) AND slot <> 'username')
                  OR p.id IN (SELECT person_id FROM person_aliases WHERE alias_norm % $2::text))
           LIMIT 200""",
        sorted(probe), query, exclude_person_id,
    )
    ids = [r["id"] for r in rows]
    if not ids:
        return {"status": "none", "candidates": []}
    people = await conn.fetch(
        "SELECT id, display_name, first_name, middle_name, last_name, gender FROM people WHERE id = ANY($1::bigint[])", ids)
    aliases: dict[int, list[str]] = {}
    for r in await conn.fetch(
            "SELECT person_id, alias, alias_norm FROM person_aliases WHERE person_id = ANY($1::bigint[])", ids):
        if not r["alias"].startswith("@") and r["alias_norm"]:
            aliases.setdefault(r["person_id"], []).append(r["alias_norm"])
    scored = []
    for person in people:
        name = ParsedName(person["first_name"], person["middle_name"], person["last_name"], person["gender"])
        names = _display_variants(name) | set(aliases.get(person["id"], ())) | {fold(person["display_name"])}
        names.discard("")
        if len(q_words) == 1:
            words = {w for n in names for w in n.split()}
            score = 100 if query in words else 0
        else:
            score = 0
            for candidate in names:
                c_words = candidate.split()
                if len(c_words) < 2:
                    continue
                # каждое слово более короткого имени должно найти пару в более длинном:
                # «Иван Петренко» — не «Иван Петров», «Иван Сергеевич Петров» — не «Иван Иванович Петров»
                short, long = (q_words, c_words) if len(q_words) <= len(c_words) else (c_words, q_words)
                if any(max(fuzz.ratio(s, w) for w in long) < TOKEN_CUTOFF for s in short):
                    continue
                score = max(score, round(fuzz.token_set_ratio(query, candidate)))
        if score >= cutoff:
            scored.append({"person_id": person["id"], "display_name": person["display_name"], "score": score})
    scored.sort(key=lambda c: (-c["score"], c["person_id"]))
    if not scored:
        return {"status": "none", "candidates": []}
    if len(scored) > 1 and scored[0]["score"] - scored[1]["score"] < margin:
        return {"status": "ambiguous", "candidates": scored[:10]}
    return {"status": "match", "candidates": scored[:1]}


async def search_people(
    conn: asyncpg.Connection, query: str | None = None, *, chat_id: int | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    """Список людей; с запросом — подходящие под упоминание или отображаемое имя."""
    limit = max(1, min(int(limit), 200))
    if not query or not query.strip():
        rows = await conn.fetch(
            f"""SELECT p.* FROM people p WHERE p.merged_into IS NULL AND {visible_person('p')}
                ORDER BY p.is_owner DESC, p.display_name, p.id LIMIT $1""", limit)
        return [_person_dict(r) for r in rows]
    found: dict[int, dict[str, Any]] = {}
    mention = await resolve_mention(conn, query, chat_id=chat_id)
    for c in mention["candidates"]:
        found[c["person_id"]] = {"match": mention["status"], "score": 100}
    display = await match_display_name(conn, query)
    for c in display["candidates"]:
        found.setdefault(c["person_id"], {"match": display["status"], "score": c["score"]})
    if not found:
        return []
    rows = await conn.fetch("SELECT * FROM people WHERE id = ANY($1::bigint[])", list(found))
    out = [{**_person_dict(r), **found[r["id"]]} for r in rows]
    out.sort(key=lambda p: (-p["score"], p["display_name"], p["id"]))
    return out[:limit]


# --- изменение -----------------------------------------------------------------------------

class PeopleError(ValueError):
    """Действие над реестром невозможно; текст — для владельца."""


async def _lock(conn: asyncpg.Connection) -> None:
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext('shturman.people'))")


async def _add_alias_row(
    conn: asyncpg.Connection, person_id: int, alias: str, origin: str, peer_id: int | None = None,
) -> bool:
    alias = _clean_display(alias)
    norm = alias[1:].lower() if alias.startswith("@") else fold(alias)
    if not alias or not norm:
        return False
    done = await conn.execute(
        """INSERT INTO person_aliases (person_id, alias, alias_norm, origin, peer_id)
           VALUES ($1, $2, $3, $4, $5) ON CONFLICT (person_id, alias_norm) DO NOTHING""",
        person_id, alias, norm, origin, peer_id,
    )
    return done.endswith("1")


async def _insert_person(
    conn: asyncpg.Connection, display_name: str, parsed: ParsedName, *, origin: str,
    is_owner: bool = False, confirmed: bool = False,
) -> int:
    return await conn.fetchval(
        """INSERT INTO people (display_name, first_name, middle_name, last_name, gender, is_owner, origin, confirmed)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id""",
        display_name, parsed.first, parsed.middle, parsed.last, parsed.gender, is_owner, origin, confirmed,
    )


async def create_person(conn: asyncpg.Connection, display_name: str, *, aliases: Iterable[str] = ()) -> int:
    """Заводит человека без учётной записи Telegram. Действие владельца."""
    display_name = _clean_display(display_name)
    if not display_name:
        raise PeopleError("Нужно имя.")
    async with conn.transaction():
        await _lock(conn)
        person_id = await _insert_person(conn, display_name, parse_name(display_name), origin="owner", confirmed=True)
        await _add_alias_row(conn, person_id, display_name, "owner")
        for alias in aliases:
            await _add_alias_row(conn, person_id, alias, "owner")
        await rebuild_forms(conn, person_id)
    return person_id


async def _propose_merges(conn: asyncpg.Connection, person_id: int, display: str) -> int:
    """Вероятное совпадение с другой записью — предложение владельцу, не слияние."""
    if parse_name(display).parts < 2:
        return 0  # по одному имени («Иван») людей не сопоставляем
    found = await match_display_name(conn, display, exclude_person_id=person_id)
    created = 0
    for candidate in found["candidates"][:3]:
        done = await conn.execute(
            """INSERT INTO person_proposals (kind, person_id, other_person_id, score)
               VALUES ('merge', $1, $2, $3) ON CONFLICT DO NOTHING""",
            person_id, candidate["person_id"], candidate["score"],
        )
        created += int(done.endswith("1"))
    return created


async def ensure_person_for_peer(conn: asyncpg.Connection, peer_id: int, *, propose: bool = True) -> int | None:
    """Находит или заводит человека для учётной записи Telegram.

    Запись заводится автоматически по отображаемому имени и остаётся неподтверждённой. С другими
    записями она не сливается: при вероятном совпадении создаётся предложение владельцу.
    Для ботов, групп, каналов и служебных собеседников Telegram возвращает None.
    """
    known = await person_for_peer(conn, peer_id)
    if known is not None:
        return known
    peer = await conn.fetchrow("SELECT id, class, tg_id, name, username, is_bot FROM peers WHERE id = $1", peer_id)
    if peer is None or peer["class"] != "user" or peer["is_bot"]:
        return None
    if store.is_blocked_peer(peer["class"], peer["tg_id"], peer["username"]):
        return None
    async with conn.transaction():
        await _lock(conn)
        known = await person_for_peer(conn, peer_id)
        if known is not None:
            return known
        is_owner = bool(await conn.fetchval(
            "SELECT 1 FROM accounts WHERE role = 'owner' AND tg_user_id = $1", peer["tg_id"]))
        if is_owner:
            existing_owner = await conn.fetchval("SELECT id FROM people WHERE is_owner AND merged_into IS NULL")
            if existing_owner is not None:
                await conn.execute(
                    "INSERT INTO person_peers (peer_id, person_id, origin) VALUES ($1, $2, 'auto')",
                    peer_id, existing_owner)
                return existing_owner
        name = _clean_display(peer["name"])
        display = name or (f"@{peer['username']}" if peer["username"] else f"Telegram {peer['tg_id']}")
        person_id = await _insert_person(conn, display, parse_name(name), origin="auto",
                                         is_owner=is_owner, confirmed=is_owner)
        await conn.execute(
            "INSERT INTO person_peers (peer_id, person_id, origin) VALUES ($1, $2, 'auto')", peer_id, person_id)
        if name:
            await _add_alias_row(conn, person_id, name, "telegram", peer_id)
        if peer["username"]:
            await _add_alias_row(conn, person_id, "@" + peer["username"], "telegram", peer_id)
        await rebuild_forms(conn, person_id)
        if propose and not is_owner and name:
            await _propose_merges(conn, person_id, name)
    return person_id


async def sync_people(conn: asyncpg.Connection, *, limit: int = 500) -> dict[str, int]:
    """Заводит людей для собеседников личных чатов и подхватывает сменившиеся имена Telegram."""
    rows = await conn.fetch(
        """SELECT DISTINCT p.id FROM chats c JOIN peers p ON p.id = c.peer_id
           WHERE c.type = 'personal_chat' AND NOT c.excluded AND p.class = 'user'
             AND p.is_bot IS NOT TRUE
             AND NOT EXISTS (SELECT 1 FROM person_peers pp WHERE pp.peer_id = p.id)
           ORDER BY p.id LIMIT $1""",
        limit,
    )
    created = 0
    for r in rows:
        created += int(await ensure_person_for_peer(conn, r["id"]) is not None)
    renamed = await conn.fetch(
        """SELECT pp.person_id, p.id AS peer_id, p.name FROM person_peers pp JOIN peers p ON p.id = pp.peer_id
           WHERE p.name IS NOT NULL AND p.name <> ''
             AND NOT EXISTS (SELECT 1 FROM person_aliases a
                             WHERE a.person_id = pp.person_id AND a.peer_id = p.id AND a.alias = p.name)
           LIMIT $1""",
        limit,
    )
    refreshed = 0
    for r in renamed:
        if await _add_alias_row(conn, r["person_id"], r["name"], "telegram", r["peer_id"]):
            await rebuild_forms(conn, r["person_id"])
            refreshed += 1
    return {"created": created, "renamed": refreshed}


async def _require_active(conn: asyncpg.Connection, person_id: int) -> asyncpg.Record:
    row = await conn.fetchrow("SELECT * FROM people WHERE id = $1 FOR UPDATE", person_id)
    if row is None:
        raise PeopleError("Такого человека нет в реестре.")
    if row["merged_into"] is not None:
        raise PeopleError("Эта запись уже объединена с другой.")
    return row


async def add_alias(conn: asyncpg.Connection, person_id: int, alias: str, *, origin: str = "owner") -> dict[str, Any]:
    """Добавляет алиас («Михалыч», «Петрович с Фасада») и перестраивает словоформы."""
    if origin not in ("owner", "auto"):
        raise PeopleError("Неизвестное происхождение алиаса.")
    if not fold(alias) and not _clean_display(alias).startswith("@"):
        raise PeopleError("Алиас должен содержать буквы.")
    async with conn.transaction():
        await _lock(conn)
        await _require_active(conn, person_id)
        added = await _add_alias_row(conn, person_id, alias, origin)
        if origin == "owner":
            await conn.execute("UPDATE people SET confirmed = true, updated_at = now() WHERE id = $1", person_id)
        await rebuild_forms(conn, person_id)
    return {"ok": True, "added": added}


async def remove_alias(conn: asyncpg.Connection, person_id: int, alias: str) -> dict[str, Any]:
    """Убирает алиас. Имена из Telegram не убираются: они вернутся при следующей сверке."""
    alias = _clean_display(alias)
    norm = alias[1:].lower() if alias.startswith("@") else fold(alias)
    async with conn.transaction():
        await _lock(conn)
        await _require_active(conn, person_id)
        done = await conn.execute(
            "DELETE FROM person_aliases WHERE person_id = $1 AND alias_norm = $2 AND origin <> 'telegram'",
            person_id, norm)
        await rebuild_forms(conn, person_id)
    return {"ok": True, "removed": int(done.split()[-1])}


async def confirm_person(conn: asyncpg.Connection, person_id: int) -> None:
    async with conn.transaction():
        await _require_active(conn, person_id)
        await conn.execute("UPDATE people SET confirmed = true, updated_at = now() WHERE id = $1", person_id)


async def merge_people(conn: asyncpg.Connection, source_id: int, target_id: int) -> dict[str, Any]:
    """Вливает запись `source_id` в `target_id`. Действие владельца.

    Учётные записи и алиасы переходят к `target_id`; исходная запись остаётся как указатель
    (merged_into), чтобы старые ссылки вели к новой. Обязательства привязаны к учётным записям
    Telegram, поэтому переходят сами.
    """
    if source_id == target_id:
        raise PeopleError("Нельзя объединить запись саму с собой.")
    async with conn.transaction():
        await _lock(conn)
        source = await _require_active(conn, source_id)
        await _require_active(conn, target_id)
        await conn.execute("UPDATE person_peers SET person_id = $2 WHERE person_id = $1", source_id, target_id)
        await conn.execute(
            """INSERT INTO person_aliases (person_id, alias, alias_norm, origin, peer_id)
               SELECT $2, alias, alias_norm, origin, peer_id FROM person_aliases WHERE person_id = $1
               ON CONFLICT (person_id, alias_norm) DO NOTHING""",
            source_id, target_id)
        await conn.execute("DELETE FROM person_aliases WHERE person_id = $1", source_id)
        await _add_alias_row(conn, target_id, source["display_name"], "auto")
        await conn.execute("DELETE FROM person_forms WHERE person_id = $1", source_id)
        if source["is_owner"]:
            await conn.execute("UPDATE people SET is_owner = false WHERE id = $1", source_id)
        await conn.execute(
            "UPDATE people SET merged_into = $2, updated_at = now() WHERE id = $1", source_id, target_id)
        await conn.execute(
            """UPDATE people SET first_name = COALESCE(first_name, $2), middle_name = COALESCE(middle_name, $3),
                      last_name = COALESCE(last_name, $4), gender = COALESCE(gender, $5),
                      is_owner = is_owner OR $6, confirmed = true, updated_at = now()
               WHERE id = $1""",
            target_id, source["first_name"], source["middle_name"], source["last_name"], source["gender"],
            source["is_owner"])
        await conn.execute(
            """UPDATE person_proposals SET status = 'accepted', decided_at = now()
               WHERE kind = 'merge' AND status = 'pending'
                 AND LEAST(person_id, other_person_id) = LEAST($1::bigint, $2::bigint)
                 AND GREATEST(person_id, other_person_id) = GREATEST($1::bigint, $2::bigint)""",
            source_id, target_id)
        # остальные предложения с влитой записью потеряли смысл
        await conn.execute(
            "DELETE FROM person_proposals WHERE status = 'pending' AND (person_id = $1 OR other_person_id = $1)",
            source_id)
        # факты о человеке переходят вместе с ним; сменяемые выстраиваются заново по датам
        from . import facts
        await conn.execute("UPDATE facts SET person_id = $2 WHERE person_id = $1", source_id, target_id)
        for row in await conn.fetch(
                "SELECT DISTINCT slot FROM facts WHERE person_id = $1 AND slot IS NOT NULL", target_id):
            await facts.rechain(conn, "person", target_id, None, row["slot"])
        await conn.execute("UPDATE pages SET dirty = true WHERE person_id = $1", target_id)
        await rebuild_forms(conn, target_id)
    return {"ok": True, "person_id": target_id, "merged": source_id}


async def split_person(conn: asyncpg.Connection, person_id: int, peer_id: int) -> dict[str, Any]:
    """Отделяет учётную запись Telegram в самостоятельного человека. Действие владельца."""
    async with conn.transaction():
        await _lock(conn)
        await _require_active(conn, person_id)
        linked = await conn.fetchval("SELECT person_id FROM person_peers WHERE peer_id = $1", peer_id)
        if linked != person_id:
            raise PeopleError("Эта учётная запись не привязана к этому человеку.")
        if await conn.fetchval("SELECT count(*) FROM person_peers WHERE person_id = $1", person_id) < 2:
            raise PeopleError("У человека одна учётная запись — отделять нечего.")
        peer = await conn.fetchrow("SELECT tg_id, name, username FROM peers WHERE id = $1", peer_id)
        name = _clean_display(peer["name"])
        display = name or (f"@{peer['username']}" if peer["username"] else f"Telegram {peer['tg_id']}")
        new_id = await _insert_person(conn, display, parse_name(name), origin="owner", confirmed=True)
        await conn.execute("UPDATE person_peers SET person_id = $2, origin = 'owner' WHERE peer_id = $1", peer_id, new_id)
        await conn.execute("DELETE FROM person_aliases WHERE person_id = $1 AND peer_id = $2", person_id, peer_id)
        if name:
            await _add_alias_row(conn, new_id, name, "telegram", peer_id)
        if peer["username"]:
            await _add_alias_row(conn, new_id, "@" + peer["username"], "telegram", peer_id)
        # владелец решил, что это разные люди: пару больше не предлагаем
        await conn.execute(
            """INSERT INTO person_proposals (kind, person_id, other_person_id, score, status, decided_at)
               VALUES ('merge', $1, $2, 0, 'rejected', now()) ON CONFLICT DO NOTHING""",
            new_id, person_id)
        await rebuild_forms(conn, person_id)
        await rebuild_forms(conn, new_id)
    return {"ok": True, "person_id": new_id, "split_from": person_id}


# --- предложения владельцу -------------------------------------------------------------------

async def list_proposals(conn: asyncpg.Connection, *, status: str = "pending", limit: int = 50) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """SELECT pr.id, pr.kind, pr.score, pr.status, pr.created_at,
                  a.id AS a_id, a.display_name AS a_name, b.id AS b_id, b.display_name AS b_name
           FROM person_proposals pr
           JOIN people a ON a.id = pr.person_id JOIN people b ON b.id = pr.other_person_id
           WHERE pr.status = $1 AND """ + visible_person("a") + " AND " + visible_person("b") + """
           ORDER BY pr.score DESC, pr.id LIMIT $2""",
        status, max(1, min(int(limit), 200)),
    )
    return [
        {"id": r["id"], "kind": r["kind"], "score": r["score"], "status": r["status"],
         "created_at": r["created_at"].isoformat(),
         "person": {"id": r["a_id"], "display_name": r["a_name"]},
         "other": {"id": r["b_id"], "display_name": r["b_name"]},
         "untrusted_fields": ["person.display_name", "other.display_name"]}
        for r in rows
    ]


async def decide_proposal(conn: asyncpg.Connection, proposal_id: int, accept: bool) -> dict[str, Any]:
    """Решение владельца по предложению слияния. При согласии новая запись вливается в прежнюю."""
    async with conn.transaction():
        row = await conn.fetchrow("SELECT * FROM person_proposals WHERE id = $1 FOR UPDATE", proposal_id)
        if row is None:
            raise PeopleError("Такого предложения нет.")
        if row["status"] != "pending":
            return {"ok": True, "status": row["status"], "changed": False}
        if not accept:
            await conn.execute(
                "UPDATE person_proposals SET status = 'rejected', decided_at = now() WHERE id = $1", proposal_id)
            return {"ok": True, "status": "rejected", "changed": True}
        result = await merge_people(conn, row["person_id"], row["other_person_id"])
    return {"ok": True, "status": "accepted", "changed": True, "person_id": result["person_id"]}
