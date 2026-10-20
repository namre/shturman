"""Quoted terms do not suppress independent direct-address evidence."""
from dataclasses import asdict
from datetime import datetime, timezone

import pytest
from telethon.tl import types

from shturman.outbox import direct_address
from shturman.tg.normalize import index_entities, message_record

OWNER = 100
NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)


def record(text, entities=()):
    msg = types.Message(id=10, peer_id=types.PeerChannel(555), from_id=types.PeerUser(300),
                        date=NOW, message=text, entities=list(entities))
    users = [types.User(id=300, first_name="Человек", bot=False)]
    return asdict(message_record(msg, index_entities(users), self_id=200))


def mention(text, word="Иван", occurrence=0):
    starts = [i for i in range(len(text)) if text.startswith(word, i)]
    start = starts[occurrence]
    offset = len(text[:start].encode("utf-16-le")) // 2
    length = len(word.encode("utf-16-le")) // 2
    return types.MessageEntityMentionName(offset, length, OWNER)


@pytest.mark.parametrize("text", [
    "Штурман, объясни «бартер»",
    'Штурман, объясни "бартер"',
    "Штурман, объясни `бартер`",
    "Штурман, объясни ‘бартер’",
    "Штурман, explain what's meant by barter",
])
def test_leading_configured_vocative_outside_later_quotation_addresses(text):
    assert direct_address.is_direct_address(record(text), [OWNER], ["Штурман"])
    assert not direct_address.is_direct_address(record(text), [OWNER], [])


@pytest.mark.parametrize("text", [
    "😀 Иван, объясни «бартер»",
    '😀 «термин» Иван, помоги',
    "😀 `термин` Иван, помоги",
    "I can't explain it; Иван, помоги",
    "L’homme: Иван, помоги",
])
def test_verified_stable_mention_outside_quote_uses_utf16_positions(text):
    assert direct_address.is_direct_address(record(text, [mention(text)]), [OWNER])


@pytest.mark.parametrize("text", [
    '«Иван, помоги»', '"Иван, помоги"', "‘Иван, помоги’", "'Иван, помоги'",
    "`Иван, помоги`", "``Иван, помоги``", "```Иван, помоги```",
    "> Иван, помоги", "😀 цитата:\n  > Иван, помоги",
    "«цитата «термин» Иван, помоги»",
    "«Иван, помоги", '"Иван, помоги', "```Иван, помоги",
])
def test_verified_stable_mention_inside_plain_quote_or_code_is_denied(text):
    assert not direct_address.is_direct_address(record(text, [mention(text)]), [OWNER])


@pytest.mark.parametrize("kind", [types.MessageEntityCode, types.MessageEntityPre,
                                  types.MessageEntityBlockquote])
def test_overlap_with_any_part_of_verified_mention_is_blocked(kind):
    text = "😀 Иван, помоги"
    stable = mention(text)
    # Only the last two letters are inside the protected entity.
    outer = kind(stable.offset + 2, 2, language="") if kind is types.MessageEntityPre else kind(stable.offset + 2, 2)
    assert not direct_address.is_direct_address(record(text, [stable, outer]), [OWNER])


def test_unquoted_second_mention_is_independent_of_first_quoted_mention():
    text = "😀 «Иван, помоги»; Иван, объясни"
    assert direct_address.is_direct_address(record(text, [mention(text, occurrence=0),
                                                         mention(text, occurrence=1)]), [OWNER])
    assert not direct_address.is_direct_address(record(text, [mention(text, occurrence=0)]), [OWNER])


def test_code_entity_later_in_text_does_not_suppress_true_mention():
    text = "😀 Иван, объясни бартер"
    offset = len("😀 Иван, объясни ".encode("utf-16-le")) // 2
    assert direct_address.is_direct_address(record(text, [mention(text),
        types.MessageEntityCode(offset, len("бартер"))]), [OWNER])


@pytest.mark.parametrize("text", [
    r'"пример \"Иван\""',
    r'"пример \\\"Иван\\\""',
])
def test_escaped_inner_quote_does_not_expose_verified_mention(text):
    assert not direct_address.is_direct_address(record(text, [mention(text)]), [OWNER])


def test_even_escape_parity_still_closes_quote_before_real_mention():
    text = r'"пример \\"; Иван, помоги'
    assert direct_address.is_direct_address(record(text, [mention(text)]), [OWNER])
