"""Разбор срока: дата считается кодом, и лучше отказ, чем неверная дата."""

from datetime import date, datetime, time, timezone

import pytest

from shturman.processing.dates import (
    DueStatus, Reason, find_due_expression, format_due, resolve_due_expression, to_local,
)

TUESDAY = datetime(2026, 10, 6, 14, 0)  # вторник

# Таблица из 38 деловых формулировок: что ожидается от вторника 6 октября 2026, 14:00.
# AMBIG — должен быть отказ; «a|b» — допустим любой из вариантов; AMBIG_OR_x — отказ или x.
TABLE = [
    ("завтра", "2026-10-07"), ("послезавтра", "2026-10-08"), ("сегодня до 18:00", "2026-10-06"),
    ("к пятнице", "2026-10-09"), ("до пятницы", "2026-10-09"), ("в пятницу", "2026-10-09"),
    ("к понедельнику", "2026-10-12"), ("в следующий вторник", "2026-10-13"), ("к среде", "2026-10-07"),
    ("до конца недели", "2026-10-09|2026-10-11"), ("до конца месяца", "2026-10-31"),
    ("к концу месяца", "2026-10-31"), ("до конца года", "2026-12-31"), ("до конца квартала", "2026-12-31"),
    ("через две недели", "2026-10-20"), ("через 2 недели", "2026-10-20"), ("через три дня", "2026-10-09"),
    ("через неделю", "2026-10-13"), ("через месяц", "2026-11-06"), ("через пару дней", "2026-10-08"),
    ("к 10-му", "2026-10-10"), ("к 10 числу", "2026-10-10"), ("до 15-го", "2026-10-15"), ("к 5-му", "2026-11-05"),
    ("к 10 октября", "2026-10-10"), ("до 1 ноября", "2026-11-01"), ("10.10", "2026-10-10"),
    ("к 10.10.2026", "2026-10-10"),
    ("на следующей неделе", "AMBIG"), ("в начале ноября", "AMBIG"), ("в середине месяца", "AMBIG"),
    ("на днях", "AMBIG"),
    ("в понедельник утром", "2026-10-12"), ("к вечеру", "2026-10-06"), ("до обеда завтра", "2026-10-07"),
    ("к 15 января", "2027-01-15"), ("в январе", "AMBIG"), ("к вторнику", "AMBIG_OR_2026-10-13"),
]


def verdict(expression, expected, anchor=TUESDAY):
    """'ok' — как ожидалось; 'refused' — отказ там, где ждали дату; 'wrong' — неверная дата."""
    got = resolve_due_expression(expression, anchor).due_date
    got = got.isoformat() if got else None
    if expected.startswith("AMBIG"):
        allowed = expected.split("_OR_")[1:]
        return "ok" if got is None or got in allowed else "wrong"
    if got is None:
        return "refused"
    return "ok" if got in expected.split("|") else "wrong"


def test_table_has_38_cases():
    assert len(TABLE) == 38


@pytest.mark.parametrize("expression,expected", TABLE)
def test_table_case(expression, expected):
    assert verdict(expression, expected) == "ok"


def test_table_score_has_no_wrong_dates():
    verdicts = [verdict(e, x) for e, x in TABLE]
    assert verdicts.count("wrong") == 0
    assert verdicts.count("ok") >= 34


def D(text):
    return date.fromisoformat(text)


@pytest.mark.parametrize("expression,anchor,expected", [
    # переход через год
    ("завтра", datetime(2026, 12, 31, 10, 0), "2027-01-01"),
    ("к 5-му", datetime(2026, 12, 20, 10, 0), "2027-01-05"),
    ("через месяц", datetime(2026, 12, 6, 10, 0), "2027-01-06"),
    ("до конца года", datetime(2026, 12, 31, 10, 0), "2026-12-31"),
    ("до конца следующего года", TUESDAY, "2027-12-31"),
    ("в пятницу", datetime(2026, 12, 30, 10, 0), "2027-01-01"),
    ("к 10 января", datetime(2026, 12, 20, 10, 0), "2027-01-10"),
    # 29 февраля и короткие месяцы
    ("через месяц", datetime(2027, 1, 31, 10, 0), "2027-02-28"),
    ("через год", datetime(2028, 2, 29, 10, 0), "2029-02-28"),
    ("к 29 февраля", datetime(2028, 1, 10, 10, 0), "2028-02-29"),
    ("к 29 февраля", datetime(2027, 11, 10, 10, 0), "2028-02-29"),
    ("до конца месяца", datetime(2028, 2, 3, 10, 0), "2028-02-29"),
    ("до конца февраля", datetime(2027, 1, 10, 10, 0), "2027-02-28"),
    # кварталы
    ("до конца квартала", datetime(2026, 2, 3, 10, 0), "2026-03-31"),
    ("до конца следующего квартала", datetime(2026, 11, 3, 10, 0), "2027-03-31"),
    # дни недели
    ("в следующий понедельник", datetime(2026, 10, 9, 10, 0), "2026-10-12"),
    ("в пятницу на следующей неделе", TUESDAY, "2026-10-16"),
    ("в ближайший вторник", TUESDAY, "2026-10-13"),
    ("не позднее пятницы", TUESDAY, "2026-10-09"),
    ("к пт", TUESDAY, "2026-10-09"),
    ("в пятницу 9 октября", TUESDAY, "2026-10-09"),
    ("в пятницу 16.10", TUESDAY, "2026-10-16"),
    ("до конца следующей недели", TUESDAY, "2026-10-16"),
    # числа месяца
    ("до 15 числа следующего месяца", TUESDAY, "2026-11-15"),
    ("по 25-е", TUESDAY, "2026-10-25"),
    ("до 25го", TUESDAY, "2026-10-25"),
    ("к 10 окт", TUESDAY, "2026-10-10"),
    ("25-го сентября 2027 г.", TUESDAY, "2027-09-25"),
    ("2026-11-03", TUESDAY, "2026-11-03"),
    ("до 01.11.26", TUESDAY, "2026-11-01"),
    ("до конца октября", TUESDAY, "2026-10-31"),
    # счёт от сообщения
    ("через 3 суток", TUESDAY, "2026-10-09"),
    ("в течение 3 дней", TUESDAY, "2026-10-09"),
    ("через полгода", TUESDAY, "2027-04-06"),
    ("через пару недель", TUESDAY, "2026-10-20"),
    ("через 10 часов", TUESDAY, "2026-10-07"),
    # части дня и время
    ("завтра утром", TUESDAY, "2026-10-07"),
    ("сегодня вечером", datetime(2026, 10, 6, 23, 50), "2026-10-06"),
    ("к утру", datetime(2026, 10, 6, 22, 0), "2026-10-07"),
    ("до конца дня", TUESDAY, "2026-10-06"),
    ("до конца рабочего дня", TUESDAY, "2026-10-06"),
    ("в течение дня", TUESDAY, "2026-10-06"),
    ("до 18:00", TUESDAY, "2026-10-06"),
    ("до 18-00", TUESDAY, "2026-10-06"),
    ("сегодня после обеда", TUESDAY, "2026-10-06"),
    ("завтра в 10.30", TUESDAY, "2026-10-07"),
    ("завтра в 10.10", TUESDAY, "2026-10-07"),
    ("завтра к 9 утра", TUESDAY, "2026-10-07"),
])
def test_resolves_to_exact_date(expression, anchor, expected):
    outcome = resolve_due_expression(expression, anchor)
    assert outcome.status is DueStatus.RESOLVED, outcome.reason
    assert outcome.due_date == D(expected)


@pytest.mark.parametrize("expression,anchor,reason", [
    (None, TUESDAY, Reason.NO_DEADLINE),
    ("  ", TUESDAY, Reason.NO_DEADLINE),
    ("как получится", TUESDAY, Reason.UNPARSEABLE),
    ("скоро", TUESDAY, Reason.VAGUE),
    ("в ближайшее время", TUESDAY, Reason.VAGUE),
    ("примерно к пятнице", TUESDAY, Reason.VAGUE),
    ("в пятницу или субботу", TUESDAY, Reason.VAGUE),
    ("после 10-го", TUESDAY, Reason.VAGUE),
    ("не раньше пятницы", TUESDAY, Reason.VAGUE),
    ("не в пятницу", TUESDAY, Reason.VAGUE),
    ("с 10 по 15 октября", TUESDAY, Reason.VAGUE),
    ("с понедельника", TUESDAY, Reason.VAGUE),
    ("через несколько дней", TUESDAY, Reason.VAGUE),
    ("через день", TUESDAY, Reason.VAGUE),
    ("через 2 рабочих дня", TUESDAY, Reason.VAGUE),
    ("после выходных", TUESDAY, Reason.VAGUE),
    ("10-12 октября", TUESDAY, Reason.UNPARSEABLE),
    ("к 10", TUESDAY, Reason.UNPARSEABLE),
    ("к первому числу", TUESDAY, Reason.UNPARSEABLE),
    ("через 2 недели и 3 дня", TUESDAY, Reason.UNPARSEABLE),
    ("до 30-го", datetime(2027, 1, 31, 10, 0), Reason.UNPARSEABLE),      # 30 февраля не бывает
    ("к 29 февраля", datetime(2026, 10, 6, 10, 0), Reason.UNPARSEABLE),  # в 2027 году его нет
    ("на этой неделе", TUESDAY, Reason.AMBIGUOUS_PERIOD),
    ("на неделе", TUESDAY, Reason.AMBIGUOUS_PERIOD),
    ("в течение недели", TUESDAY, Reason.AMBIGUOUS_PERIOD),
    ("в следующем месяце", TUESDAY, Reason.AMBIGUOUS_PERIOD),
    ("до ноября", TUESDAY, Reason.AMBIGUOUS_PERIOD),
    ("до конца недели", datetime(2026, 10, 10, 12, 0), Reason.AMBIGUOUS_PERIOD),  # сказано в субботу
    ("до пятницы", datetime(2026, 10, 9, 10, 0), Reason.AMBIGUOUS_WEEKDAY),       # сказано в пятницу
    ("в эту пятницу", datetime(2026, 10, 9, 10, 0), Reason.AMBIGUOUS_WEEKDAY),
    ("в следующую пятницу", TUESDAY, Reason.AMBIGUOUS_WEEKDAY),                   # ближайшая или через неделю
    ("в следующий понедельник", datetime(2026, 10, 11, 10, 0), Reason.AMBIGUOUS_WEEKDAY),  # сказано в воскресенье
    ("к 6-му", TUESDAY, Reason.AMBIGUOUS_DAY),
    ("к 5 октября", TUESDAY, Reason.AMBIGUOUS_YEAR),
    ("к 15 сентября", TUESDAY, Reason.AMBIGUOUS_YEAR),
    ("до конца сентября", TUESDAY, Reason.AMBIGUOUS_YEAR),
    ("в 10.10", TUESDAY, Reason.AMBIGUOUS_TIME),
    ("в пятницу в 16.10", TUESDAY, Reason.AMBIGUOUS_TIME),
    ("утром", TUESDAY, Reason.AMBIGUOUS_TIME),
    ("к вечеру", datetime(2026, 10, 6, 23, 30), Reason.AMBIGUOUS_TIME),
    ("до обеда", datetime(2026, 10, 6, 16, 0), Reason.AMBIGUOUS_TIME),
    ("до 12:00", TUESDAY, Reason.AMBIGUOUS_TIME),
    ("к 10.10 утра", TUESDAY, Reason.AMBIGUOUS_TIME),        # время 10:10 уже прошло, а не 10 октября
    ("через 2 дня и 3 часа", TUESDAY, Reason.UNPARSEABLE),
    ("ночью", TUESDAY, Reason.AMBIGUOUS_TIME),
    ("завтра ночью", TUESDAY, Reason.AMBIGUOUS_TIME),
    ("к 12 ночи", TUESDAY, Reason.AMBIGUOUS_TIME),
    ("в пятницу 10 октября", TUESDAY, Reason.CONFLICT),      # 10 октября 2026 — суббота
    ("завтра, 9 октября", TUESDAY, Reason.CONFLICT),
    ("завтра к 10.10", TUESDAY, Reason.CONFLICT),
    ("к пятнице, а лучше к четвергу", TUESDAY, Reason.CONFLICT),
    ("через неделю в пятницу", TUESDAY, Reason.CONFLICT),
    ("в прошлую пятницу", TUESDAY, Reason.NOT_A_DEADLINE),
    ("по пятницам", TUESDAY, Reason.NOT_A_DEADLINE),
    ("каждый понедельник", TUESDAY, Reason.NOT_A_DEADLINE),
    ("вчера", TUESDAY, Reason.NOT_A_DEADLINE),
    ("завтра", None, Reason.NO_ANCHOR),
    ("к пятнице", None, Reason.NO_ANCHOR),
    ("32.13.2026", TUESDAY, Reason.UNPARSEABLE),
    ("к пятнице " * 20, TUESDAY, Reason.UNPARSEABLE),
])
def test_refuses_instead_of_guessing(expression, anchor, reason):
    outcome = resolve_due_expression(expression, anchor)
    assert outcome.status is DueStatus.NEEDS_CONFIRMATION
    assert outcome.due_date is None
    assert outcome.reason is reason


def test_full_date_needs_no_anchor():
    assert resolve_due_expression("до 25.12.2026", None).due_date == date(2026, 12, 25)


def test_clock_and_part_of_day_are_reported():
    outcome = resolve_due_expression("сегодня до 18:00", TUESDAY)
    assert (outcome.due_date, outcome.due_time) == (date(2026, 10, 6), time(18, 0))
    assert resolve_due_expression("в понедельник утром", TUESDAY).part_of_day == "morning"
    assert resolve_due_expression("завтра к 6 вечера", TUESDAY).due_time == time(18, 0)
    assert resolve_due_expression("через полчаса", TUESDAY).due_time == time(14, 30)


def test_hours_can_cross_midnight():
    outcome = resolve_due_expression("через 3 часа", datetime(2026, 10, 6, 22, 30))
    assert (outcome.due_date, outcome.due_time) == (date(2026, 10, 7), time(1, 30))


def test_dateparser_is_used_only_for_uncovered_cherez_forms():
    outcome = resolve_due_expression("через полтора часа", TUESDAY)
    assert (outcome.due_date, outcome.due_time) == (date(2026, 10, 6), time(15, 30))
    assert resolve_due_expression("через двадцать дней", TUESDAY).due_date == date(2026, 10, 26)
    # то, что dateparser разбирает неверно или слишком смело, до него не доходит
    assert resolve_due_expression("10.10", TUESDAY).due_date == date(2026, 10, 10)
    assert resolve_due_expression("на следующей неделе", TUESDAY).due_date is None
    assert resolve_due_expression("через день", TUESDAY).due_date is None
    assert resolve_due_expression("через квартал", TUESDAY).due_date is None


def test_date_is_counted_in_owner_timezone():
    """23:30 UTC — это уже следующий день в Москве: «завтра» считается от него."""
    sent = datetime(2026, 10, 6, 23, 30, tzinfo=timezone.utc)
    assert to_local(sent, "Europe/Moscow") == datetime(2026, 10, 7, 2, 30)
    assert resolve_due_expression("завтра", sent, "Europe/Moscow").due_date == date(2026, 10, 8)
    assert resolve_due_expression("завтра", sent, "UTC").due_date == date(2026, 10, 7)
    # среда по Москве: «к среде» уже неоднозначно, хотя по UTC ещё вторник
    assert resolve_due_expression("к среде", sent, "Europe/Moscow").reason is Reason.AMBIGUOUS_WEEKDAY
    assert resolve_due_expression("к среде", sent, "UTC").due_date == date(2026, 10, 7)
    # к западу от UTC день, наоборот, ещё предыдущий
    early = datetime(2026, 10, 7, 2, 0, tzinfo=timezone.utc)
    assert resolve_due_expression("сегодня", early, "America/New_York").due_date == date(2026, 10, 6)


@pytest.mark.parametrize("text,expected", [
    ("Пришлю смету по фасадам к пятнице.", "к пятнице"),
    ("Отправлю договор завтра до 15:00", "завтра до 15:00"),
    ("Сделаем до конца месяца, не переживайте", "до конца месяца"),
    ("Оплатим до 15-го", "до 15-го"),
    ("Вышлю через две недели", "через две недели"),
    ("Акт будет 25 октября", "25 октября"),
    ("Пришлите, пожалуйста, смету до пятницы", "до пятницы"),
    ("Спасибо, всё получил", None),
])
def test_finds_deadline_wording_verbatim(text, expected):
    assert find_due_expression(text) == expected


def test_format_due_is_short_and_russian():
    assert format_due(date(2026, 10, 9)) == "пт, 9 октября"
    assert format_due(date(2027, 1, 15), time(18, 0), today=date(2026, 10, 6)) == "пт, 15 января 2027, 18:00"
