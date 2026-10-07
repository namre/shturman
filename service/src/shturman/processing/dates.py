# Основано на VsevaTech/promise-tracker (MIT), app/dates.py@ffcf27a
#
# MIT License
#
# Copyright (c) 2026 VsevaTech
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""Срок обязательства: формулировка -> дата. Без модели, только правила.

Модель копирует формулировку срока дословно («к пятнице»), а дату от времени сообщения
в часовом поясе владельца считает этот модуль. Главное правило: лучше отказаться, чем ошибиться.
Если формулировка не сводится ровно к одной дате, возвращается «нужно уточнение» с кодом причины.

Что взято из promise-tracker: общий ход разбора (полная дата -> «25 сентября» -> относительные
слова -> «через N» -> конец недели и месяца -> день недели), правило для даты без года, отказ на
«пятницу», сказанную в пятницу. Что изменено: разбор по частям с проверкой остатка (непонятая
цифра или второй день недели — отказ), числительные словами, «к 10-му», конец квартала и года,
«10.10» как дата, части дня, часовой пояс; английский словарь убран.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from enum import Enum
from zoneinfo import ZoneInfo


class DueStatus(str, Enum):
    RESOLVED = "resolved"
    NEEDS_CONFIRMATION = "needs_confirmation"


class Reason(str, Enum):
    OK = "ok"
    NO_DEADLINE = "no_deadline"                # срок не назван
    UNPARSEABLE = "unparseable"                # формулировку не удалось разобрать
    VAGUE = "vague"                            # «на днях», «примерно», «или»
    AMBIGUOUS_PERIOD = "ambiguous_period"      # назван период, а не день: «на следующей неделе», «в январе»
    AMBIGUOUS_WEEKDAY = "ambiguous_weekday"    # день недели совпал с днём сообщения и т. п.
    AMBIGUOUS_DAY = "ambiguous_day"            # «к 6-му», сказанное шестого
    AMBIGUOUS_YEAR = "ambiguous_year"          # дата без года уже прошла
    AMBIGUOUS_TIME = "ambiguous_time"          # часть дня уже прошла; «в 10.10» — время или дата
    CONFLICT = "conflict"                      # части формулировки не сходятся
    NOT_A_DEADLINE = "not_a_deadline"          # прошлое или повторяющееся («по пятницам»)
    NO_ANCHOR = "no_anchor"                    # нет времени сообщения, от которого считать
    NOT_IN_SOURCE = "not_in_source"            # формулировки нет в исходном тексте — выдумана


REASON_TEXT: dict[Reason, str] = {
    Reason.OK: "Срок определён по формулировке из сообщения.",
    Reason.NO_DEADLINE: "Срок в сообщении не назван.",
    Reason.UNPARSEABLE: "Формулировку срока не удалось перевести в дату.",
    Reason.VAGUE: "Срок назван приблизительно.",
    Reason.AMBIGUOUS_PERIOD: "Назван период, а не день.",
    Reason.AMBIGUOUS_WEEKDAY: "Неясно, о какой неделе речь.",
    Reason.AMBIGUOUS_DAY: "Неясно, этот месяц или следующий.",
    Reason.AMBIGUOUS_YEAR: "Дата без года уже прошла на момент сообщения.",
    Reason.AMBIGUOUS_TIME: "Неясно, сегодня или завтра, либо время это или дата.",
    Reason.CONFLICT: "Части формулировки срока не сходятся между собой.",
    Reason.NOT_A_DEADLINE: "Формулировка говорит о прошлом или о повторяющемся событии.",
    Reason.NO_ANCHOR: "Нет времени сообщения, от которого считать срок.",
    Reason.NOT_IN_SOURCE: "Срока нет в тексте сообщения, он отброшен.",
}


@dataclass(frozen=True)
class Resolution:
    """Итог разбора одной формулировки срока."""

    status: DueStatus
    reason: Reason
    due_date: date | None = None
    due_time: time | None = None       # только явно названное время («до 18:00»)
    part_of_day: str | None = None     # morning | noon | afternoon | evening | night | eod

    @property
    def resolved(self) -> bool:
        return self.status is DueStatus.RESOLVED

    @classmethod
    def ok(cls, due_date: date, due_time: time | None = None, part: str | None = None) -> "Resolution":
        return cls(DueStatus.RESOLVED, Reason.OK, due_date, due_time, part)

    @classmethod
    def unresolved(cls, reason: Reason) -> "Resolution":
        return cls(DueStatus.NEEDS_CONFIRMATION, reason)


# --- словарь -----------------------------------------------------------------

_WEEKDAY_FORMS: tuple[tuple[str, int], ...] = (
    (r"понедельник(?:а|у|е|ом)?|пн", 0),
    (r"вторник(?:а|у|е|ом)?|вт", 1),
    (r"сред(?:а|у|е|ы|ой)|ср", 2),
    (r"четверг(?:а|у|е|ом)?|чт", 3),
    (r"пятниц(?:а|у|е|ы|ей)|пт", 4),
    (r"суббот(?:а|у|е|ы|ой)|сб", 5),
    (r"воскресень(?:е|я|ю|ем)|вс", 6),
)
_WEEKDAY_RE = re.compile(
    r"\b(?:" + "|".join(f"(?P<w{n}>{forms})" for forms, n in _WEEKDAY_FORMS) + r")\b\.?"
)
# «по пятницам», «по средам» — повторяющееся, сроком не считается
_WEEKDAY_PLURAL_RE = re.compile(
    r"\b(?:понедельникам|вторникам|средам|четвергам|пятницам|субботам|воскресеньям)\b"
)

_MONTH_FORMS: tuple[tuple[str, int], ...] = (
    (r"январ(?:ь|я|ю|е|ем)|янв", 1),
    (r"феврал(?:ь|я|ю|е|ем)|фев(?:р)?", 2),
    (r"март(?:а|у|е|ом)?|мар", 3),
    (r"апрел(?:ь|я|ю|е|ем)|апр", 4),
    (r"ма(?:й|я|ю|е|ем)", 5),
    (r"июн(?:ь|я|ю|е|ем)", 6),
    (r"июл(?:ь|я|ю|е|ем)", 7),
    (r"август(?:а|у|е|ом)?|авг", 8),
    (r"сентябр(?:ь|я|ю|е|ем)|сент?", 9),
    (r"октябр(?:ь|я|ю|е|ем)|окт", 10),
    (r"ноябр(?:ь|я|ю|е|ем)|ноя", 11),
    (r"декабр(?:ь|я|ю|е|ем)|дек", 12),
)
_MONTH_ALT = "|".join(f"(?:{forms})" for forms, _ in _MONTH_FORMS)
_MONTH_EACH = [(re.compile(rf"^(?:{forms})$"), n) for forms, n in _MONTH_FORMS]

_WORD_NUMBERS: dict[str, int] = {
    "один": 1, "одну": 1, "одного": 1, "одной": 1,
    "два": 2, "две": 2, "двух": 2, "пару": 2, "пара": 2,
    "три": 3, "трех": 3, "четыре": 4, "четырех": 4, "пять": 5, "пяти": 5,
    "шесть": 6, "шести": 6, "семь": 7, "семи": 7, "восемь": 8, "восьми": 8,
    "девять": 9, "девяти": 9, "десять": 10, "десяти": 10,
}
_NUM_ALT = r"\d{1,3}|" + "|".join(sorted(_WORD_NUMBERS, key=len, reverse=True))

_UNIT_ALT = (
    r"(?P<minutes>мин(?:ут(?:а|у|ы|ок)?)?\.?)|(?P<hours>час(?:а|ов|у)?|ч\.?)|"
    r"(?P<days>дн(?:я|ей|ю)|день|дн\.?|сут(?:ки|ок))|(?P<weeks>недел(?:я|ю|и|ь)|нед\.?)|"
    r"(?P<months>месяц(?:а|ев|у)?|мес\.?)|(?P<years>год(?:а|у)?|лет)"
)
_DELTA_RE = re.compile(
    rf"\b(?P<lead>через|в\s+течени[ие])\s+(?:(?P<num>{_NUM_ALT})\s*)?(?:{_UNIT_ALT})(?![а-я])"
)
_HALF_RE = re.compile(r"\bчерез\s+пол(?P<what>часа|года)\b")

# Слова, по которым dateparser разрешено досчитать «через …», если свои правила не справились.
_FALLBACK_VOCAB = re.compile(
    r"^через(?:\s+(?:\d{1,3}|и|полтора|полторы|полчаса|полгода|"
    r"один|одну|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять|одиннадцать|двенадцать|"
    r"тринадцать|четырнадцать|пятнадцать|шестнадцать|семнадцать|восемнадцать|девятнадцать|"
    r"двадцать|тридцать|сорок|пятьдесят|шестьдесят|девяносто|"
    r"минут(?:у|ы)?|час(?:а|ов)?|дн(?:я|ей)|недел(?:ю|и|ь)|месяц(?:а|ев)?|год(?:а)?|лет))+$"
)

_NOT_DEADLINE_RE = re.compile(
    r"\b(?:прошл\w+|назад|вчера|позавчера|кажд\w+|ежедневно|еженедельно|ежемесячно|регулярно)\b"
)
_VAGUE_RE = re.compile(
    r"\b(?:примерно|ориентировочно|приблизительно|где-то|гдето|около|или|либо|нескольк\w+|"
    r"не\s+раньше|не\s+ранее|ближе\s+к|начал[аеоу]|середин[аеуы]|на\s+днях|скоро|"
    r"ближайш\w+\s+(?:время|дни|дней|день|час\w*|недел\w+)|как\s+только|когда|если|"
    r"после(?!\s+обеда)|(?<!конц[ау]\s)рабоч\w+\s+дн\w+|выходн\w+|праздник\w*|возможно|наверное|может)\b"
)
# отрицание, кроме «не позднее / не позже»
_NEGATION_RE = re.compile(r"\bне\b(?!\s+(?:позднее|позже|дольше))")
_RANGE_RE = re.compile(r"\bс\s+\S+(?:\s+\S+)?\s+по\b|\bс\s+(?:\d|понедельник|вторник|сред|четверг|пятниц|суббот|воскресен)")

_PART_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bв\s+течени[ие]\s+дня\b", "eod"),
    (r"\bпосле\s+обеда\b", "afternoon"),
    (r"\b(?:до\s+обеда|к\s+обеду|в\s+обед|обед(?:а|у|ом)?)\b", "noon"),
    (r"\b(?:с\s+утра|утр(?:о|ом|а|у)|с\s+утреца)\b", "morning"),
    (r"\bдн[её]м\b", "day"),
    (r"\b(?:вечер(?:ом|у|а)?)\b", "evening"),
    (r"\b(?:ночь(?:ю)?|ночи)\b", "night"),
)

# Последний час, до которого голая часть дня ещё относится к сегодня.
_PART_TODAY_BEFORE = {"morning": 10, "noon": 12, "afternoon": 16, "day": 12, "evening": 19, "eod": 24}

PAST_TOLERANCE_DAYS = 183  # дата без года, прошедшая не больше чем на полгода, — не угадываем год


# --- помощники ---------------------------------------------------------------

def to_local(moment: datetime | None, tz: tzinfo | str | None) -> datetime | None:
    """Время сообщения в часовом поясе владельца (без пояса — считаем уже местным)."""
    if moment is None:
        return None
    if isinstance(tz, str):
        tz = ZoneInfo(tz)
    if moment.tzinfo is not None and tz is not None:
        moment = moment.astimezone(tz)
    return moment.replace(tzinfo=None)


def _normalize(expression: str) -> str:
    text = expression.lower().replace("ё", "е").strip()
    text = re.sub(r"[«»\"'()\[\]!?;]", " ", text)
    text = re.sub(r"[‐‑‒–—−]", "-", text)
    return re.sub(r"\s+", " ", text).strip(" .,:-")


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _month_number(word: str) -> int | None:
    word = word.rstrip(".")
    for pattern, number in _MONTH_EACH:
        if pattern.match(word):
            return number
    return None


def _add_months(start: date, amount: int) -> date:
    index = start.month - 1 + amount
    year, month = start.year + index // 12, index % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def _last_day(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _resolve_yearless(month: int, day: int, anchor: date) -> Resolution:
    """Дата без года. Будущая в этом году — берём; давно прошедшая — следующий год;
    прошедшая недавно — не угадываем."""
    this_year = _safe_date(anchor.year, month, day)
    if this_year is not None and this_year >= anchor:
        return Resolution.ok(this_year)
    if this_year is not None and (anchor - this_year).days <= PAST_TOLERANCE_DAYS:
        return Resolution.unresolved(Reason.AMBIGUOUS_YEAR)
    next_year = _safe_date(anchor.year + 1, month, day)
    if next_year is None:
        return Resolution.unresolved(Reason.UNPARSEABLE)
    return Resolution.ok(next_year)


class _Parts:
    """Разобранные части формулировки. Каждая найденная часть вырезается из текста."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.dates: list[tuple[str, tuple]] = []   # (вид, данные) — то, что задаёт день
        self.clock: time | None = None
        self.clock_night = False
        self.part: str | None = None
        self.flags: set[str] = set()

    def cut(self, match: re.Match) -> None:
        self.text = self.text[: match.start()] + " | " + self.text[match.end():]

    def take(self, pattern: str | re.Pattern) -> re.Match | None:
        match = re.search(pattern, self.text)
        if match:
            self.cut(match)
        return match


def _extract(parts: _Parts) -> Reason | None:
    """Вырезает из текста известные части. Возвращает причину отказа, если она ясна сразу."""
    # полные даты
    m = parts.take(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
    if m:
        parts.dates.append(("full", (int(m[1]), int(m[2]), int(m[3]))))
    m = parts.take(r"(?<![\d.])(\d{1,2})[./](\d{1,2})[./](\d{4}|\d{2})(?![\d.])(?:\s*г(?:ода|\.)?(?![а-я]))?")
    if m:
        year = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
        parts.dates.append(("full", (year, int(m[2]), int(m[1]))))

    # время с двоеточием и «18-00»
    m = parts.take(r"(?<![\d:.])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])")
    if m is None:
        m = parts.take(r"(?<![\d.-])([01]?\d|2[0-3])-(00|30)(?![\d-])")
    if m:
        parts.clock = time(int(m[1]), int(m[2]))

    # «через полчаса», «через N дней», «в течение N дней»
    m = parts.take(_HALF_RE)
    if m:
        parts.dates.append(("delta", ("minutes", 30) if m["what"] == "часа" else ("months", 6)))
    m = _DELTA_RE.search(parts.text)
    if m:
        unit = next(u for u in ("minutes", "hours", "days", "weeks", "months", "years") if m[u])
        within = not m["lead"].startswith("через")
        raw = m["num"]
        if raw is None:
            if within and unit == "days":
                amount = None                      # «в течение дня» — сегодня, разбирается как часть дня
            elif within and unit != "hours":
                return Reason.AMBIGUOUS_PERIOD     # «в течение недели» — неделя с сегодня или эта неделя?
            elif unit == "days":
                return Reason.VAGUE                # «через день» — завтра или послезавтра
            else:
                amount = 1
        else:
            amount = int(raw) if raw.isdigit() else _WORD_NUMBERS[raw]
        if amount is not None:
            parts.cut(m)
            parts.dates.append(("delta", (unit, amount)))

    # «25 сентября», «25-го сентября 2026 г.»
    m = parts.take(
        rf"(?<![\d.])(\d{{1,2}})(?:\s*-?\s*(?:го|е|ое|му))?\s+({_MONTH_ALT})\b\.?"
        r"(?:\s+(20\d{2})(?:\s*г(?:ода|\.)?(?![а-я]))?)?"
    )
    if m:
        parts.dates.append(("daymonth", (int(m[1]), _month_number(m[2]), int(m[3]) if m[3] else None)))

    # «до конца октября»
    m = parts.take(rf"\bконц[аеу]\s+({_MONTH_ALT})\b")
    if m:
        parts.dates.append(("monthend", (_month_number(m[1]),)))

    # «10.10» — дата; «18.30» — время
    m = re.search(r"(?<![\d.])(\d{1,2})\.(\d{1,2})(?![\d.])", parts.text)
    if m:
        first, second = int(m[1]), int(m[2])
        as_date = 1 <= first <= 31 and 1 <= second <= 12
        as_time = first <= 23 and second <= 59 and len(m[2]) == 2
        after_v = bool(re.search(r"\bв\s*$", parts.text[: m.start()]))
        half = re.match(r"\s*(утра|дня|вечера|ночи)\b", parts.text[m.end():])
        if half and as_time and parts.clock is None:
            # «к 10.10 утра» — это время, а не 10 октября
            parts.text = parts.text[: m.start()] + " | " + parts.text[m.end() + half.end():]
            hour = first + 12 if half[1] in ("дня", "вечера") and first < 12 else first
            parts.clock = time(hour % 24, second)
            parts.clock_night = half[1] == "ночи"
        elif as_date:
            parts.cut(m)
            parts.dates.append(("dm", (first, second, as_time, after_v)))
        elif as_time and parts.clock is None:
            parts.cut(m)
            parts.clock = time(first, second)

    # «к 10-му», «до 15-го числа», «к 10 числу следующего месяца»
    m = parts.take(
        r"(?<![\d.])(\d{1,2})(?:\s*-?\s*(?:го|му|ое|е)\b(?:\s+числ[ауо])?|\s+числ[ауо]\b)"
        r"(?:\s+(следующего|этого|текущего)\s+месяца)?"
    )
    if m:
        parts.dates.append(("dom", (int(m[1]), m[2])))

    # «к 9 утра», «в 15 часов», «к 6 вечера»
    # предлог обязателен: «и 3 дня» — это три дня, а не три часа дня
    m = parts.take(
        r"\b(?:в|к|до|около)\s+(\d{1,2})\s*(?:час(?:ов|а|ам|у)?|ч\.?)(?![а-я])(?:\s*(утра|дня|вечера|ночи))?"
    )
    if m is None:
        m = parts.take(r"\b(?:в|к|до|около)\s+(\d{1,2})\s+(утра|дня|вечера|ночи)\b")
    if m and parts.clock is None:
        hour, half = int(m[1]), m[2]
        if hour > 23:
            return Reason.UNPARSEABLE
        if half in ("дня", "вечера") and hour < 12:
            hour += 12
        if half == "ночи":
            parts.clock_night = True
        parts.clock = time(hour % 24, 0)

    # относительные дни
    for word, shift in (("послезавтра", 2), ("завтра", 1), ("сегодня", 0)):
        if parts.take(rf"\b{word}\b"):
            parts.dates.append(("rel", (shift,)))

    # «до конца недели / месяца / квартала / года / дня»
    m = parts.take(
        r"\bконц[аеу]\s+(?:(эт\w+|текущ\w+|следующ\w+|будущ\w+)\s+)?(?:рабоч\w+\s+)?"
        r"(дня|недели|месяца|квартала|года)\b"
    )
    if m:
        is_next = bool(m[1]) and m[1].startswith(("следующ", "будущ"))
        if m[2] == "дня":
            if is_next:
                return Reason.UNPARSEABLE
            parts.dates.append(("rel", (0,)))
            parts.part = "eod"
        else:
            parts.dates.append(("end", (m[2], is_next)))

    # неделя как уточнение к дню недели
    if parts.take(r"\b(?:на|в)\s+(?:следующ\w+|будущ\w+)\s+недел\w+"):
        parts.flags.add("next_week")
    elif parts.take(r"\b(?:на|в)\s+(?:эт\w+|текущ\w+)\s+недел\w+"):
        parts.flags.add("this_week")
    elif parts.take(r"\bна\s+недел[еи]\b"):
        parts.flags.add("some_week")

    # дни недели (все вхождения)
    weekdays: list[int] = []
    while True:
        m = parts.take(_WEEKDAY_RE)
        if m is None:
            break
        weekdays.append(next(n for _, n in _WEEKDAY_FORMS if m[f"w{n}"]))
    if len(set(weekdays)) > 1:
        return Reason.CONFLICT
    if weekdays:
        modifier = None
        if parts.take(r"\b(?:следующ\w+|будущ\w+)\b"):
            modifier = "next"
        elif parts.take(r"\bближайш\w+\b"):
            modifier = "nearest"
        elif parts.take(r"\bэт(?:от|у|о|ой|ому)\b"):
            modifier = "this"
        parts.dates.append(("weekday", (weekdays[0], modifier)))

    # части дня
    for pattern, name in _PART_PATTERNS:
        if parts.take(pattern):
            if parts.part is not None and parts.part != name:
                return Reason.CONFLICT
            parts.part = name
    return None


def _leftover_reason(parts: _Parts) -> Reason | None:
    """Непонятый остаток — причина отказаться: неразобранная цифра, месяц, неделя."""
    rest = parts.text
    if re.search(r"\d", rest):
        return Reason.UNPARSEABLE
    if re.search(rf"\b(?:{_MONTH_ALT})\b", rest):
        return Reason.AMBIGUOUS_PERIOD
    if re.search(r"\b(?:недел\w*|месяц\w*|квартал\w*|год[ау]?|полугоди\w+)\b", rest):
        return Reason.AMBIGUOUS_PERIOD
    if re.search(r"\b(?:через|течени[ие]|полтор\w+|числ\w+|конц\w+)\b", rest):
        return Reason.UNPARSEABLE
    return None


def _weekday_date(anchor: date, weekday: int, modifier: str | None, flags: set[str]) -> date | Reason:
    delta = (weekday - anchor.weekday()) % 7
    if "next_week" in flags:
        monday = anchor - timedelta(days=anchor.weekday()) + timedelta(days=7)
        return monday + timedelta(days=weekday)
    if "some_week" in flags:
        return Reason.AMBIGUOUS_PERIOD
    if "this_week" in flags:
        if weekday <= anchor.weekday():
            return Reason.AMBIGUOUS_WEEKDAY
        return anchor + timedelta(days=delta)
    if modifier == "next":
        if delta == 0:
            return anchor + timedelta(days=7)
        if weekday > anchor.weekday() or anchor.weekday() >= 5:
            # «в следующую пятницу», сказанное во вторник: ближайшая или через неделю — не угадываем
            return Reason.AMBIGUOUS_WEEKDAY
        return anchor + timedelta(days=delta)
    if modifier == "nearest":
        return anchor + timedelta(days=delta or 7)
    if delta == 0:
        return Reason.AMBIGUOUS_WEEKDAY
    return anchor + timedelta(days=delta)


def _one_date(kind: str, data: tuple, local: datetime | None, parts: _Parts) -> Resolution:
    """Дата по одной части формулировки."""
    if kind == "full":
        found = _safe_date(*data)
        return Resolution.ok(found) if found else Resolution.unresolved(Reason.UNPARSEABLE)
    if kind == "daymonth" and data[2] is not None:
        found = _safe_date(data[2], data[1], data[0])
        return Resolution.ok(found) if found else Resolution.unresolved(Reason.UNPARSEABLE)
    if local is None:
        return Resolution.unresolved(Reason.NO_ANCHOR)
    anchor = local.date()

    if kind == "daymonth":
        return _resolve_yearless(data[1], data[0], anchor)
    if kind == "dm":
        return _resolve_yearless(data[1], data[0], anchor)
    if kind == "monthend":
        this_year = _last_day(anchor.year, data[0])
        if this_year >= anchor:
            return Resolution.ok(this_year)
        if (anchor - this_year).days <= PAST_TOLERANCE_DAYS:
            return Resolution.unresolved(Reason.AMBIGUOUS_YEAR)
        return Resolution.ok(_last_day(anchor.year + 1, data[0]))
    if kind == "dom":
        day, which = data
        if which == "следующего":
            first = _add_months(anchor.replace(day=1), 1)
            found = _safe_date(first.year, first.month, day)
            return Resolution.ok(found) if found else Resolution.unresolved(Reason.UNPARSEABLE)
        if day == anchor.day:
            return Resolution.unresolved(Reason.AMBIGUOUS_DAY)
        if day > anchor.day:
            found = _safe_date(anchor.year, anchor.month, day)
        elif which is not None:
            return Resolution.unresolved(Reason.NOT_A_DEADLINE)  # «5-го этого месяца» уже прошло
        else:
            first = _add_months(anchor.replace(day=1), 1)
            found = _safe_date(first.year, first.month, day)
        return Resolution.ok(found) if found else Resolution.unresolved(Reason.UNPARSEABLE)
    if kind == "rel":
        return Resolution.ok(anchor + timedelta(days=data[0]))
    if kind == "delta":
        unit, amount = data
        if unit in ("minutes", "hours"):
            moment = local + timedelta(**{unit: amount})
            return Resolution.ok(moment.date(), moment.time().replace(second=0, microsecond=0))
        if unit == "days":
            return Resolution.ok(anchor + timedelta(days=amount))
        if unit == "weeks":
            return Resolution.ok(anchor + timedelta(weeks=amount))
        if unit == "months":
            return Resolution.ok(_add_months(anchor, amount))
        return Resolution.ok(_add_months(anchor, 12 * amount))
    if kind == "end":
        period, is_next = data
        if period == "недели":
            if is_next:
                monday = anchor - timedelta(days=anchor.weekday()) + timedelta(days=7)
                return Resolution.ok(monday + timedelta(days=4))
            if anchor.weekday() > 4:
                return Resolution.unresolved(Reason.AMBIGUOUS_PERIOD)  # в выходные: эта неделя или следующая
            return Resolution.ok(anchor + timedelta(days=4 - anchor.weekday()))
        if period == "месяца":
            base = _add_months(anchor.replace(day=1), 1) if is_next else anchor
            return Resolution.ok(_last_day(base.year, base.month))
        if period == "квартала":
            base = _add_months(anchor.replace(day=1), 3) if is_next else anchor
            month = ((base.month - 1) // 3) * 3 + 3
            return Resolution.ok(_last_day(base.year, month))
        return Resolution.ok(date(anchor.year + (1 if is_next else 0), 12, 31))
    if kind == "weekday":
        found = _weekday_date(anchor, data[0], data[1], parts.flags)
        return Resolution.unresolved(found) if isinstance(found, Reason) else Resolution.ok(found)
    return Resolution.unresolved(Reason.UNPARSEABLE)


def _via_dateparser(text: str, local: datetime | None) -> Resolution | None:
    """Запасной путь только для «через …», которое свои правила не посчитали («через полтора часа»)."""
    if local is None or not _FALLBACK_VOCAB.match(text):
        return None
    try:
        import dateparser  # BSD-3-Clause; импорт здесь — библиотека нужна редко
    except ImportError:  # pragma: no cover
        return None
    found = dateparser.parse(text, languages=["ru"], settings={
        "RELATIVE_BASE": local, "PREFER_DATES_FROM": "future", "RETURN_AS_TIMEZONE_AWARE": False,
    })
    if found is None or found <= local or found - local > timedelta(days=366 * 3):
        return None
    with_clock = bool(re.search(r"минут|час", text))
    return Resolution.ok(found.date(), found.time().replace(second=0, microsecond=0) if with_clock else None)


def resolve_due_expression(
    expression: str | None, anchor: datetime | None, tz: tzinfo | str | None = None,
) -> Resolution:
    """Переводит формулировку срока в дату — или отказывается.

    `anchor` — время отправки исходного сообщения; все относительные слова («завтра», «через
    3 дня») считаются от него в часовом поясе `tz`.
    """
    if expression is None or not expression.strip():
        return Resolution.unresolved(Reason.NO_DEADLINE)
    if len(expression) > 120:
        return Resolution.unresolved(Reason.UNPARSEABLE)
    text = _normalize(expression)
    if not text:
        return Resolution.unresolved(Reason.UNPARSEABLE)
    local = to_local(anchor, tz)

    if _NOT_DEADLINE_RE.search(text) or _WEEKDAY_PLURAL_RE.search(text):
        return Resolution.unresolved(Reason.NOT_A_DEADLINE)
    if _VAGUE_RE.search(text) or _NEGATION_RE.search(text) or _RANGE_RE.search(text):
        return Resolution.unresolved(Reason.VAGUE)

    parts = _Parts(text)
    early = _extract(parts)
    if early is not None:
        return Resolution.unresolved(early)
    leftover = _leftover_reason(parts)
    if leftover is not None:
        if text.startswith("через") and not parts.dates:
            fallback = _via_dateparser(text, local)
            if fallback is not None:
                return fallback
        return Resolution.unresolved(leftover)

    dates = list(parts.dates)
    clock, part = parts.clock, parts.part

    if "next_week" in parts.flags or "this_week" in parts.flags or "some_week" in parts.flags:
        if not any(kind == "weekday" for kind, _ in dates):
            return Resolution.unresolved(Reason.AMBIGUOUS_PERIOD)
    if parts.clock_night:
        return Resolution.unresolved(Reason.AMBIGUOUS_TIME)

    if not dates:
        if clock is None and part is None:
            return Resolution.unresolved(Reason.UNPARSEABLE)
        if local is None:
            return Resolution.unresolved(Reason.NO_ANCHOR)
        # голое время или часть дня: сегодня, только если ещё не прошло
        if clock is not None:
            if clock <= local.time():
                return Resolution.unresolved(Reason.AMBIGUOUS_TIME)
            return Resolution.ok(local.date(), clock, part)
        if part == "night":
            return Resolution.unresolved(Reason.AMBIGUOUS_TIME)
        if part == "morning" and local.hour >= 15:
            return Resolution.ok(local.date() + timedelta(days=1), None, part)  # «к утру», сказанное вечером
        if local.hour >= _PART_TODAY_BEFORE[part]:
            return Resolution.unresolved(Reason.AMBIGUOUS_TIME)
        return Resolution.ok(local.date(), None, part)

    if len(dates) > 2:
        return Resolution.unresolved(Reason.CONFLICT)
    if len(dates) == 1:
        kind, data = dates[0]
        if kind == "dm" and data[2] and data[3]:
            return Resolution.unresolved(Reason.AMBIGUOUS_TIME)  # «в 10.10» — время или дата
        outcome = _one_date(kind, data, local, parts)
    else:
        # допустимые пары: день недели или «завтра» вместе с числом — и только если они совпадают
        soft = next((item for item in dates if item[0] in ("weekday", "rel")), None)
        hard = next((item for item in dates if item[0] in ("full", "daymonth", "dm", "dom")), None)
        if soft is None or hard is None:
            return Resolution.unresolved(Reason.CONFLICT)
        soft_result = _one_date(soft[0], soft[1], local, parts)
        hard_result = _one_date(hard[0], hard[1], local, parts)
        maybe_time = hard[0] == "dm" and hard[1][2]   # «10.30» может быть и временем
        after_v = hard[0] == "dm" and hard[1][3]
        if not hard_result.resolved:
            agrees = False
        elif soft[0] == "weekday":
            agrees = hard_result.due_date.weekday() == soft[1][0]
        else:
            agrees = soft_result.resolved and hard_result.due_date == soft_result.due_date
        if agrees:
            if maybe_time and after_v and (not soft_result.resolved
                                           or soft_result.due_date != hard_result.due_date):
                # «в пятницу в 16.10»: ближайшая пятница в 16:10 или пятница 16 октября
                return Resolution.unresolved(Reason.AMBIGUOUS_TIME)
            outcome = hard_result
        elif maybe_time and after_v and clock is None:
            if not soft_result.resolved:
                return soft_result
            clock = time(hard[1][0], hard[1][1])    # «завтра в 10.10» — это время
            outcome = soft_result
        elif not hard_result.resolved and not maybe_time:
            return hard_result
        else:
            return Resolution.unresolved(Reason.CONFLICT)
    if not outcome.resolved:
        return outcome
    if part == "night":
        return Resolution.unresolved(Reason.AMBIGUOUS_TIME)  # ночь переходит через полночь
    return Resolution.ok(outcome.due_date, clock or outcome.due_time, part)


# --- поиск формулировки срока в тексте ------------------------------------------
# Нужен предварительному отбору эпизодов и тестам; модель формулировку копирует сама.

_DUE_PATTERNS: tuple[str, ...] = (
    rf"\b(?:до|к|ко|по|на|не\s+позднее|не\s+позже)?\s*\d{{1,2}}(?:\s*-?\s*(?:го|е|му))?\s+(?:{_MONTH_ALT})\b(?:\s+20\d{{2}})?",
    r"\b\d{4}-\d{2}-\d{2}\b",
    r"(?<![\d.])\d{1,2}[./]\d{1,2}[./](?:\d{4}|\d{2})(?![\d.])",
    r"\b(?:до|к|ко|по|не\s+позднее|не\s+позже)\s+\d{1,2}\.\d{1,2}(?![\d.])",
    r"\b(?:до|к|ко|по)\s+\d{1,2}(?:\s*-?\s*(?:го|му|е)\b|\s+числ[ауо]\b)",
    r"\b(?:послезавтра|завтра|сегодня)\b(?:\s+(?:утром|днем|вечером|до\s+обеда|после\s+обеда))?",
    rf"\bчерез\s+(?:(?:{_NUM_ALT})\s*)?(?:минут\w*|час\w*|дн\w+|день|недел\w+|месяц\w*|год\w*|полчаса|полгода)",
    r"\b(?:до|к)\s+конц[аеу]\s+(?:(?:этой|этого|следующей|следующего)\s+)?(?:дня|недели|месяца|квартала|года)",
    r"\b(?:до|к|ко|в|во|на)\s+(?:(?:следующ\w+|эт\w+|ближайш\w+)\s+)?"
    r"(?:понедельник\w*|вторник\w*|сред[ауеы]|четверг\w*|пятниц[ауеы]|суббот[ауеы]|воскресень[еяю])\b"
    r"(?:\s+(?:утром|днем|вечером))?",
    r"\bна\s+следующей\s+неделе\b",
    r"\b(?:к|до)\s+(?:вечер[ау]|утр[ау]|обед[ау]|ночи)\b",
    r"\b(?:до|к|в)\s+(?:[01]?\d|2[0-3]):[0-5]\d\b",
)
_TIME_SUFFIX = r"(?:\s*(?:до|к|в)?\s*(?:[01]?\d|2[0-3]):[0-5]\d)?"


def find_due_expression(text: str) -> str | None:
    """Первая формулировка срока, найденная в тексте, дословно."""
    normalized = text.replace("ё", "е").replace("Ё", "Е")
    best: tuple[int, str] | None = None
    for pattern in _DUE_PATTERNS:
        match = re.search(pattern + _TIME_SUFFIX, normalized, flags=re.IGNORECASE)
        if match and (best is None or match.start() < best[0]):
            best = (match.start(), text[match.start(): match.end()].strip(" .,;:"))
    return best[1] if best else None


_WEEKDAY_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
_MONTH_GENITIVE = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
                   "сентября", "октября", "ноября", "декабря")


def format_due(due_date: date, due_time: time | None = None, *, today: date | None = None) -> str:
    """Дата для владельца: «пт, 9 октября»; год добавляется, если он не текущий."""
    out = f"{_WEEKDAY_SHORT[due_date.weekday()]}, {due_date.day} {_MONTH_GENITIVE[due_date.month - 1]}"
    if today is not None and due_date.year != today.year:
        out += f" {due_date.year}"
    if due_time is not None:
        out += f", {due_time:%H:%M}"
    return out
