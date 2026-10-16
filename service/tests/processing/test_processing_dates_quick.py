"""Срок (Р-96): быстрые обещания, конец дня, части дня, срок-событие, время по другому поясу."""

from datetime import date, datetime, time, timezone

import pytest

from shturman.processing.dates import Reason, resolve_due_expression

TUESDAY = datetime(2026, 10, 6, 14, 0)    # вторник, 14:00 по часам владельца
MORNING = datetime(2026, 10, 6, 10, 0)
EVENING = datetime(2026, 10, 6, 21, 0)


@pytest.mark.parametrize("expression,due_time", [
    ("сейчас", None), ("ща", None), ("щас", None), ("сча", None), ("сразу", None), ("прямо сейчас", None),
    ("сию минуту", None), ("через 10 минут", time(14, 10)), ("минут через 15", time(14, 15)),
    ("через полчаса", time(14, 30)), ("через 2 часа", time(16, 0)), ("через 3 часа", time(17, 0)),
    ("в течение часа", time(15, 0)), ("сегодня в течение часа", time(15, 0)), ("через полтора часа", time(15, 30)),
])
def test_quick_promises_are_today_and_marked_immediate(expression, due_time):
    outcome = resolve_due_expression(expression, TUESDAY)
    assert (outcome.reason, outcome.due_date, outcome.due_time) == (Reason.IMMEDIATE, date(2026, 10, 6), due_time)
    assert outcome.resolved and outcome.is_immediate


def test_longer_delays_are_ordinary_deadlines():
    outcome = resolve_due_expression("через 4 часа", TUESDAY)
    assert (outcome.reason, outcome.due_time) == (Reason.OK, time(18, 0))
    assert resolve_due_expression("сейчас", None).reason is Reason.NO_ANCHOR


@pytest.mark.parametrize("expression,anchor,expected,part", [
    ("на конец дня", TUESDAY, "2026-10-06", "eod"), ("конец дня", EVENING, "2026-10-06", "eod"),
    ("на конец рабочего дня", TUESDAY, "2026-10-06", "eod"), ("на конец дня рабочего", TUESDAY, "2026-10-06", "eod"),
    ("к концу рабочего дня", TUESDAY, "2026-10-06", "eod"), ("к обеду", MORNING, "2026-10-06", "noon"),
    ("вечером", TUESDAY, "2026-10-06", "evening"), ("вечерком", TUESDAY, "2026-10-06", "evening"),
    ("сегодня вечером", TUESDAY, "2026-10-06", "evening"), ("утром", EVENING, "2026-10-07", "morning"),
    ("поутру", EVENING, "2026-10-07", "morning"), ("к концу недели", TUESDAY, "2026-10-09", None),
])
def test_parts_of_the_day_and_end_of_day(expression, anchor, expected, part):
    outcome = resolve_due_expression(expression, anchor)
    assert outcome.resolved, (expression, outcome.reason)
    assert (outcome.due_date.isoformat(), outcome.part_of_day) == (expected, part)


@pytest.mark.parametrize("expression", [
    "к уикли", "к weekly", "к планёрке", "до созвона", "после созвона", "как прилечу", "как закончит",
    "как только закончим", "по готовности", "по факту", "по итогам встречи", "к ней", "перед встречей",
])
def test_event_deadlines_are_named_not_unparseable(expression):
    outcome = resolve_due_expression(expression, TUESDAY)
    assert outcome.reason is Reason.EVENT and outcome.due_date is None


@pytest.mark.parametrize("expression,reason", [
    ("как получится", Reason.UNPARSEABLE), ("после выходных", Reason.VAGUE), ("когда смогу", Reason.VAGUE),
])
def test_hedges_are_not_events(expression, reason):
    assert resolve_due_expression(expression, TUESDAY).reason is reason


def test_time_in_another_zone_is_converted_to_the_owners():
    anchor = datetime(2026, 10, 6, 6, 0, tzinfo=timezone.utc)          # 10:00 в Самаре
    outcome = resolve_due_expression("в 08:35 UTC", anchor, "Europe/Samara")
    assert (outcome.resolved, outcome.due_date, outcome.due_time) == (True, date(2026, 10, 6), time(12, 35))
    assert resolve_due_expression("к 15:00 по Москве", anchor, "Europe/Samara").due_time == time(16, 0)
    late = datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc)
    assert resolve_due_expression("в 08:35 UTC", late, "Europe/Samara").reason is Reason.AMBIGUOUS_TIME
