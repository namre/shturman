"""Кандидаты групп: настоящие TL-объекты и синтетические записи без сетевого Telegram."""
from dataclasses import asdict
from datetime import datetime, timezone

import pytest
from telethon.tl import types

from shturman.outbox import direct_address, scopes
from shturman.outbox.policy import Target
from shturman.tg.normalize import index_entities, message_record

NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)
OWNER, HELPER, PERSON = 100, 200, 300


def normalized(text, entities=(), **kwargs):
    msg = types.Message(id=10, peer_id=types.PeerChannel(555), from_id=types.PeerUser(PERSON),
                        date=NOW, message=text, entities=list(entities), **kwargs)
    users = [types.User(id=PERSON, first_name="Человек", bot=False)]
    return message_record(msg, index_entities(users), self_id=HELPER)


def addressed(record, **kwargs):
    return direct_address.is_direct_address(asdict(record), [OWNER, HELPER], **kwargs)


def test_true_telegram_id_mention_preserves_nested_entity_and_utf16():
    text = "😀 Владельцу вопрос"
    rec = normalized(text, [types.MessageEntityBold(3, 9), types.MessageEntityMentionName(3, 9, OWNER)])
    assert rec.entities == [{"type": "bold", "text": "Владельцу"}]
    assert rec.telegram_entities[1] == {"type": "mention_name", "offset": 3, "length": 9,
                                       "text": "Владельцу", "user_id": OWNER}
    assert addressed(rec)
    assert (rec.is_forwarded, rec.telegram_via_bot, rec.telegram_sender_bot) == (False, False, False)


@pytest.mark.parametrize("wrapper", [types.MessageEntityCode, types.MessageEntityPre, types.MessageEntityBlockquote])
@pytest.mark.parametrize("reverse", [False, True])
def test_code_and_quote_mention_never_addresses(wrapper, reverse):
    outer = wrapper(0, 4, language="") if wrapper is types.MessageEntityPre else wrapper(0, 4)
    ents = [outer, types.MessageEntityMentionName(0, 4, OWNER)]
    assert not addressed(normalized("Иван привет", list(reversed(ents)) if reverse else ents))


def test_forwarded_unknown_author_never_addresses_even_reply():
    rec = normalized("Владелец, привет", [types.MessageEntityMentionName(0, 8, OWNER)],
                     fwd_from=types.MessageFwdHeader(date=NOW))
    assert rec.forwarded_from is None and rec.is_forwarded is True
    assert not addressed(rec, reply_sender_tg_id=OWNER)


@pytest.mark.parametrize("text,result", [("Штурман, помоги", True), ("штурман: вопрос", True),
    ("Штурман помоги", False), ("О Штурман, помоги", False), ("Штурманов, привет", False),
    ("Он сказал: Штурман, помоги", False), ('"Штурман, помоги"', False),
    ("`Штурман, помоги`", False), ("> Штурман, помоги", False), ("@helper_name вопрос", True),
    ("@helper_name_fake вопрос", False)])
def test_alias_is_exact_vocative(text, result):
    assert addressed(normalized(text), aliases=["Штурман", "@helper_name"]) is result


def test_alias_in_real_code_entity_is_not_address():
    rec = normalized("Штурман, вопрос", [types.MessageEntityCode(0, 7)])
    assert not addressed(rec, aliases=["Штурман"])


def test_username_mention_does_not_invent_stable_identity():
    rec = normalized("@owner hello", [types.MessageEntityMention(0, 6)])
    assert not addressed(rec)
    assert addressed(rec, aliases=["@owner"])


def test_foreign_or_malformed_stable_mention_is_not_address():
    rec = normalized("Иван", [types.MessageEntityMentionName(0, 4, PERSON)])
    assert not addressed(rec)
    value = asdict(rec)
    value["telegram_entities"] = [{"type": "mention_name", "user_id": OWNER, "offset": 0, "length": 8, "text": "Иван"}]
    assert not direct_address.is_direct_address(value, [OWNER])


def test_topic_preserved_only_from_same_peer_forum_reply():
    rec = normalized("вопрос", reply_to=types.MessageReplyHeader(reply_to_msg_id=20, reply_to_top_id=5, forum_topic=True))
    assert rec.topic_tg_id == 5 and rec.reply_to_tg_id == 20
    root = normalized("вопрос", reply_to=types.MessageReplyHeader(reply_to_msg_id=5, forum_topic=True))
    assert root.topic_tg_id == 5
    ordinary = normalized("вопрос", reply_to=types.MessageReplyHeader(reply_to_msg_id=20, reply_to_top_id=5))
    assert ordinary.topic_tg_id is None
    cross = normalized("вопрос", reply_to=types.MessageReplyHeader(reply_to_msg_id=20, forum_topic=True,
                                                                    reply_to_peer_id=types.PeerChannel(777)))
    assert cross.topic_tg_id is None and cross.reply_to_tg_id is None


@pytest.mark.parametrize("aliases", [["x"], ["@valid_name"], ["Иван Петров", "иван петров"]])
def test_alias_validation_accepts_bounded_names(aliases):
    assert direct_address.validate_aliases(aliases)


@pytest.mark.parametrize("aliases", ["name", [""], ["@x"], ["a" * 65], ["a\ncommand"], ["a,b"], ["x"] * 17])
def test_alias_validation_rejects_malformed(aliases):
    with pytest.raises(ValueError):
        direct_address.validate_aliases(aliases)


def target(role="assistant", peer=555, account=2):
    return Target(1, account, role, "Помощник", "private_supergroup", "Группа", False,
                  peer, "channel", 888, "Группа", None, False)


class Connection:
    def __init__(self, rec, enabled=True, scope=None, reply=None):
        self.rec, self.enabled, self.scope, self.reply = rec, enabled, scope, reply
        self.calls = []

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        if "SELECT autoreply_enabled" in sql:
            return self.enabled
        if "FROM outbox_reply_scopes" in sql:
            return self.scope is not None and self.scope[0:2] == args[0:2] and self.scope[2] in (0, args[2])
        if "SELECT p.tg_id FROM messages" in sql:
            return self.reply
        return None

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        if "FROM messages m" in sql:
            # Моделируем отбор SQL; значения из события сами по себе не считаются доказательством.
            r = self.rec
            return r if (r and r["is_forwarded"] is False and r["telegram_via_bot"] is False
                         and r["telegram_sender_bot"] is False and r["telegram_entities"] is not None) else None
        return None

    async def fetch(self, sql, *args):
        return [{"tg_user_id": OWNER}, {"tg_user_id": HELPER}]


def row(text="вопрос", **kwargs):
    rec = normalized(text, **kwargs)
    return asdict(rec) | {"sender_tg_id": PERSON, "sender_class": "user"}


@pytest.mark.asyncio
async def test_exact_scope_cannot_cross_account_peer_or_topic():
    for scope, allow in [((2, 555, 0), True), ((2, 555, 5), True), ((2, 555, 9), False),
                         ((1, 555, 0), False), ((2, 999, 0), False)]:
        rec = row(reply_to=types.MessageReplyHeader(reply_to_msg_id=5, forum_topic=True))
        decision = await scopes.group_candidate(Connection(rec, scope=scope), target(), 10)
        assert decision.ok is allow


@pytest.mark.asyncio
async def test_disabled_and_owner_read_session_deny_even_direct_address():
    rec = row("Иван", entities=[types.MessageEntityMentionName(0, 4, OWNER)])
    assert (await scopes.group_candidate(Connection(rec, enabled=False), target(), 10)).code == "group_disabled"
    assert (await scopes.group_candidate(Connection(rec), target(role="owner"), 10)).code == "group_not_supported"


@pytest.mark.asyncio
async def test_direct_message_candidate_does_not_need_enabled_scope():
    rec = row("Иван", entities=[types.MessageEntityMentionName(0, 4, OWNER)])
    assert (await scopes.group_candidate(Connection(rec), target(), 10)).ok
    assert (await scopes.group_candidate(Connection(row()), target(), 10)).code == "group_not_addressed"


@pytest.mark.asyncio
async def test_reply_to_verified_registered_sender_is_direct_address():
    rec = row(reply_to=types.MessageReplyHeader(reply_to_msg_id=3))
    assert (await scopes.group_candidate(Connection(rec, reply=OWNER), target(), 10)).ok
    assert not (await scopes.group_candidate(Connection(rec, reply=PERSON), target(), 10)).ok


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("telegram_entities", None), ("is_forwarded", None),
    ("is_forwarded", True), ("telegram_sender_bot", True), ("telegram_sender_bot", None),
    ("telegram_via_bot", True)])
async def test_unknown_or_bot_metadata_never_becomes_group_candidate(field, value):
    rec = row() | {field: value}
    assert (await scopes.group_candidate(Connection(rec, scope=(2, 555, 0)), target(), 10)).code == "group_trigger_invalid"


@pytest.mark.asyncio
async def test_mutation_requires_verified_owner_context():
    # Обычный вызов / токен агента не становится согласием владельца.
    with pytest.raises(PermissionError):
        await scopes.put(None, 1)
    with pytest.raises(PermissionError):
        await scopes.remove(None, 1)
    with pytest.raises(PermissionError):
        await scopes.update_addressing(None, {"aliases": ["Штурман"]})


@pytest.mark.asyncio
async def test_database_scope_revocation_and_reenable_do_not_resurrect_revision(conn):
    from shturman import authority, store
    from shturman.records import ChatRecord
    from shturman.outbox import policy
    account = await store.ensure_account(conn, HELPER, "Помощник", role="assistant")
    chat, _ = await store.ensure_chat(conn, account, ChatRecord("channel", 555, "private_supergroup", "Группа"))
    await conn.execute("INSERT INTO outbox_accounts(account_id,autoreply_enabled) VALUES($1,true)", account)
    tgt = await policy.target(conn, chat)
    before = await scopes.policy_revision(conn, tgt)
    with authority.owner_context(OWNER):
        scope = await scopes.put(conn, chat, topic_tg_id=5)
    enabled = await scopes.policy_revision(conn, tgt)
    assert enabled != before
    assert await scopes.matches(conn, tgt, 5)
    assert not await scopes.matches(conn, tgt, 9)
    await conn.execute("UPDATE outbox_accounts SET last_send_at=now(),updated_at=now() WHERE account_id=$1", account)
    assert await scopes.policy_revision(conn, tgt) == enabled
    with authority.owner_context(OWNER):
        await scopes.put(conn, chat, topic_tg_id=5, enabled=False)
    assert not await scopes.matches(conn, tgt, 5)
    with authority.owner_context(OWNER):
        await scopes.put(conn, chat, topic_tg_id=5)
    assert await scopes.policy_revision(conn, tgt) != enabled
    before_toggle = await scopes.policy_revision(conn, tgt)
    await conn.execute("UPDATE outbox_accounts SET autoreply_enabled=false WHERE account_id=$1", account)
    await conn.execute("UPDATE outbox_accounts SET autoreply_enabled=true WHERE account_id=$1", account)
    assert await scopes.policy_revision(conn, tgt) != before_toggle
    with authority.owner_context(OWNER):
        assert await scopes.remove(conn, scope["id"])
    assert not await scopes.matches(conn, tgt, 5)


@pytest.mark.asyncio
async def test_database_session_reply_requires_same_chat_topic_and_visible_actual_sender(conn):
    from shturman import store
    from shturman.records import ChatRecord
    from shturman.outbox import policy
    account = await store.ensure_account(conn, HELPER, "Помощник", role="assistant")
    await store.ensure_account(conn, OWNER, "Владелец", role="owner")
    chat, _ = await store.ensure_chat(conn, account, ChatRecord("channel", 555, "private_supergroup", "Группа"))
    await conn.execute("INSERT INTO outbox_accounts(account_id,autoreply_enabled) VALUES($1,true)", account)
    sender = await store.ensure_peer(conn, "user", PERSON, name="Человек", is_bot=False)
    owner = await store.ensure_peer(conn, "user", OWNER, name="Владелец", is_bot=False)
    mid = await conn.fetchval(
        """INSERT INTO messages(chat_id,tg_message_id,sent_at,sender_peer_id,is_outgoing,text,sources,
                    reply_to_tg_id,topic_tg_id,telegram_entities,is_forwarded,telegram_via_bot,telegram_sender_bot)
           VALUES($1,10,now(),$2,false,'вопрос',ARRAY['session'],3,5,'[]',false,false,false) RETURNING id""", chat, sender)
    await conn.execute(
        """INSERT INTO messages(chat_id,tg_message_id,sent_at,sender_peer_id,is_outgoing,text,sources,
                    topic_tg_id,telegram_entities,is_forwarded,telegram_via_bot,telegram_sender_bot)
           VALUES($1,3,now(),$2,false,'владельца',ARRAY['session'],5,'[]',false,false,false)""", chat, owner)
    tgt = await policy.target(conn, chat)
    assert (await scopes.group_candidate(conn, tgt, mid)).ok
    await conn.execute("UPDATE messages SET topic_tg_id=9 WHERE chat_id=$1 AND tg_message_id=3", chat)
    assert not (await scopes.group_candidate(conn, tgt, mid)).ok
    await conn.execute("UPDATE messages SET topic_tg_id=5,agent_visible=false WHERE chat_id=$1 AND tg_message_id=3", chat)
    assert not (await scopes.group_candidate(conn, tgt, mid)).ok
    await conn.execute("UPDATE messages SET agent_visible=true,sources=ARRAY['import'] WHERE chat_id=$1 AND tg_message_id=3", chat)
    assert not (await scopes.group_candidate(conn, tgt, mid)).ok
    await conn.execute("UPDATE messages SET sources=ARRAY['session'],is_forwarded=true WHERE chat_id=$1 AND tg_message_id=3", chat)
    assert not (await scopes.group_candidate(conn, tgt, mid)).ok
    await conn.execute("UPDATE messages SET is_forwarded=false WHERE chat_id=$1 AND tg_message_id=3", chat)
    await conn.execute("UPDATE messages SET sources=ARRAY['import'] WHERE id=$1", mid)
    assert (await scopes.group_candidate(conn, tgt, mid)).code == "group_trigger_invalid"
