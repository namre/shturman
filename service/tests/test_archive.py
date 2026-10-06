"""Запросы чтения архива на настоящем Postgres: что видно агенту и что не видно никогда."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from shturman import archive, store
from shturman.records import ChatRecord, MessageRecord

OWNER = 1000
T0 = datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)

SECRET_EXCLUDED = "пароль от сейфа в исключённом чате"
SECRET_DELETED = "удалённое сообщение про зарплату"
SECRET_CODE = "Login code: 54321"


def rec(mid, text, *, sender=2001, name="Иван Петров", at=T0, reply=None, cls="user",
        kind="message", media=None, action=None, forwarded=None):
    return MessageRecord(
        tg_message_id=mid, sent_at=at, kind=kind, sender_class=cls, sender_tg_id=sender,
        sender_name=name, text=text, entities=None, reply_to_tg_id=reply, forwarded_from=forwarded,
        edited_at=None, media_type=media, media_path=None, service_action=action,
    )


def mine(mid, text, **kw):
    return rec(mid, text, sender=OWNER, name="Евгений Тестов", **kw)


@dataclass
class Seed:
    account: int
    ivan: int        # личный чат с Иваном Петровым
    sidorov: int     # личный чат с Иваном Сидоровым
    family: int      # группа «Семья»
    news: int        # канал
    bot: int         # чат с ботом
    secret: int      # исключённый личный чат
    ids: dict        # (чат, идентификатор сообщения в Telegram) -> идентификатор в архиве


async def put(conn, chat_id, records):
    result = await store.upsert_messages(conn, [(chat_id, r) for r in records],
                                         source="import", owner_tg_id=OWNER)
    rows = await conn.fetch(
        "SELECT id, tg_message_id FROM messages WHERE chat_id = $1 AND tg_message_id = ANY($2::bigint[])",
        chat_id, [r.tg_message_id for r in records])
    assert result.new == len(records)
    return {(chat_id, r["tg_message_id"]): r["id"] for r in rows}


async def seed(conn) -> Seed:
    """Небольшой архив: два Ивана, группа, канал, бот, исключённый чат, удалённое сообщение."""
    account = await store.ensure_account(conn, OWNER, "Владелец")

    async def chat(cls, tg_id, type_, name, username=None):
        chat_id, _ = await store.ensure_chat(
            conn, account, ChatRecord(cls, tg_id, type_, name, username=username))
        return chat_id

    ivan = await chat("user", 2001, "personal_chat", "Иван Петров", "ivan_p")
    sidorov = await chat("user", 2003, "personal_chat", "Иван Сидоров")
    family = await chat("chat", 3001, "private_group", "Семья")
    news = await chat("channel", 4001, "public_channel", "Стройка: новости", "stroyka_news")
    bot = await chat("user", 5001, "bot_chat", "Погода")
    secret = await chat("user", 2010, "personal_chat", "Тайный Иван")

    ids = {}
    minute = timedelta(minutes=1)
    ids |= await put(conn, ivan, [
        rec(1, "Добрый день! Пришлю смету по фасадам к пятнице."),
        mine(2, "Хорошо, жду. Сроки монтажа не сдвигаем.", at=T0 + minute),
        rec(3, "Договор лежит в папке, посмотрите", at=T0 + 2 * minute, reply=2),
        rec(4, "", at=T0 + 3 * minute, kind="service", action="phone_call"),
        rec(5, "", at=T0 + 4 * minute, media="voice_message"),
        rec(6, SECRET_DELETED, at=T0 + 5 * minute),
        rec(7, "Отвечаю на удалённое", at=T0 + 6 * minute, reply=6),
        rec(8, "Смета готова, отправил на почту", at=T0 + 2 * 24 * 60 * minute, reply=1),
    ])
    ids |= await put(conn, sidorov, [
        rec(1, "Смета по кровле будет завтра", sender=2003, name="Иван Сидоров", at=T0 + 10 * minute),
    ])
    ids |= await put(conn, family, [
        rec(10, "Купи хлеба и молока", sender=2002, name="Мария", at=T0 + 20 * minute),
        mine(11, "Куплю", at=T0 + 21 * minute),
        rec(12, "И смету не забудь распечатать", sender=2002, name="Мария", at=T0 + 22 * minute),
    ])
    ids |= await put(conn, news, [
        rec(1, "Цены на арматуру выросли", sender=4001, name="Стройка: новости", cls="channel",
            at=T0 + 30 * minute),
    ])
    ids |= await put(conn, bot, [
        rec(1, "Завтра дождь", sender=5001, name="Погода", at=T0 + 40 * minute),
    ])
    # Чат исключили уже после того, как сообщения попали в архив.
    ids |= await put(conn, secret, [
        rec(1, SECRET_EXCLUDED + ", смета там же", sender=2010, name="Тайный Иван", at=T0 + 50 * minute),
    ])
    await conn.execute("UPDATE chats SET excluded = true WHERE id = $1", secret)
    assert len(await store.mark_deleted(conn, ivan, [6])) == 1
    return Seed(account, ivan, sidorov, family, news, bot, secret, ids)


async def blocked_chat(conn, s: Seed, tg_id, username, *, in_group=True):
    """Служебный собеседник, который попал в архив в обход записи: чат не помечен исключённым."""
    chat_id, excluded = await store.ensure_chat(
        conn, s.account, ChatRecord("user", tg_id, "personal_chat", "Telegram", username=username))
    assert excluded is True
    await conn.execute("UPDATE chats SET excluded = false WHERE id = $1", chat_id)
    ids = await put(conn, chat_id, [rec(1, SECRET_CODE, sender=tg_id, name="Telegram", at=T0 + timedelta(hours=3))])
    if in_group:
        ids |= await put(conn, s.family, [
            rec(500, SECRET_CODE + " в группе", sender=tg_id, name="Telegram", at=T0 + timedelta(hours=4))])
    return chat_id, ids


async def all_visible_text(conn, s: Seed) -> str:
    """Всё, что архив готов отдать, одной строкой — для проверок «этого здесь нет»."""
    parts = [str(await archive.list_chats(conn, limit=500))]
    parts.append(str(await archive.history(conn, limit=200)))
    parts.append(str(await archive.history(conn, limit=200, order="asc")))
    parts.append(str(await archive.visible_messages(conn, list(s.ids.values()))))
    for message_id in s.ids.values():
        window = await archive.context(conn, message_id, before=50, after=50)
        parts.append(str(window))
    for name in ("Иван", "Тайный", "Telegram", "BotFather", "SpamBot", "Мария"):
        parts.append(str(await archive.find_people(conn, name, classes=("user", "chat", "channel"))))
        parts.append(str(await archive.find_chats(conn, name)))
    return "\n".join(parts)


# --- чаты ---

async def test_chats_are_listed_by_recent_activity_with_kind_and_counts(conn):
    s = await seed(conn)
    chats = await archive.list_chats(conn)
    assert [(c["id"], c["kind"], c["message_count"]) for c in chats] == [
        (s.ivan, "user", 7), (s.bot, "bot", 1), (s.news, "channel", 1),
        (s.family, "group", 3), (s.sidorov, "user", 1),
    ]
    assert chats[0]["last_message_at"] == T0 + timedelta(days=2)
    assert chats[0]["name"] == "Иван Петров" and chats[0]["username"] == "ivan_p"
    assert set(chats[0]) == {"id", "kind", "name", "username", "last_message_at", "message_count"}


async def test_chat_list_filters_by_kind_and_name(conn):
    s = await seed(conn)
    only = await archive.list_chats(conn, kinds=["group", "channel"])
    assert {c["id"] for c in only} == {s.family, s.news}
    without = await archive.list_chats(conn, exclude_kinds=["user", "bot"])
    assert {c["id"] for c in without} == {s.family, s.news}
    assert [c["id"] for c in await archive.list_chats(conn, query="иван")] == [s.ivan, s.sidorov]
    assert [c["id"] for c in await archive.list_chats(conn, query="stroyka")] == [s.news]
    assert len(await archive.list_chats(conn, limit=2)) == 2
    # знаки шаблона в запросе — обычные знаки, а не «любой текст»
    assert await archive.list_chats(conn, query="%") == []
    assert {c["id"] for c in await archive.list_chats(conn, query="_")} == {s.ivan, s.news}   # только адреса с «_»


async def test_chat_without_messages_is_listed_last(conn):
    s = await seed(conn)
    empty, _ = await store.ensure_chat(conn, s.account, ChatRecord("user", 2999, "personal_chat", "Пустой"))
    chats = await archive.list_chats(conn)
    assert chats[-1]["id"] == empty and chats[-1]["message_count"] == 0
    assert chats[-1]["last_message_at"] is None


async def test_chat_reference_by_id_username_and_name(conn):
    s = await seed(conn)
    assert (await archive.resolve_chat(conn, s.family)).row["id"] == s.family
    assert (await archive.resolve_chat(conn, str(s.family))).row["id"] == s.family
    assert (await archive.resolve_chat(conn, "@ivan_p")).row["id"] == s.ivan
    assert (await archive.resolve_chat(conn, "семья")).row["id"] == s.family          # регистр не важен
    assert (await archive.resolve_chat(conn, "Сидоров")).row["id"] == s.sidorov       # единственное частичное
    assert (await archive.resolve_chat(conn, "Петров Иван")).row["id"] == s.ivan      # порядок слов не важен
    assert (await archive.resolve_chat(conn, 999999)).status == "not_found"
    assert (await archive.resolve_chat(conn, "Несуществующий")).status == "not_found"
    assert (await archive.resolve_chat(conn, "")).status == "not_found"


async def test_ambiguous_chat_name_returns_candidates_instead_of_a_guess(conn):
    s = await seed(conn)
    found = await archive.resolve_chat(conn, "Иван")
    assert found.status == "ambiguous" and found.row is None
    assert {c["id"] for c in found.candidates} == {s.ivan, s.sidorov}


async def test_exact_chat_name_wins_over_longer_names(conn):
    s = await seed(conn)
    longer, _ = await store.ensure_chat(
        conn, s.account, ChatRecord("chat", 3002, "private_group", "Семья Петровых"))
    assert (await archive.resolve_chat(conn, "Семья")).row["id"] == s.family
    assert (await archive.resolve_chat(conn, "Семья Петровых")).row["id"] == longer


async def test_similar_name_is_offered_but_never_chosen_silently(conn):
    s = await seed(conn)
    found = await archive.resolve_chat(conn, "Сидоровв Иван")
    assert found.status == "ambiguous"
    assert [c["id"] for c in found.candidates] == [s.sidorov] and found.candidates[0]["tier"] == 2


# --- люди ---

async def test_person_is_found_with_direct_chat_and_last_interaction(conn):
    s = await seed(conn)
    people = await archive.find_people(conn, "Петров")
    assert len(people) == 1
    ivan = people[0]
    assert (ivan["name"], ivan["username"], ivan["tier"]) == ("Иван Петров", "ivan_p", 1)
    assert ivan["direct_chat_id"] == s.ivan
    assert ivan["last_interaction_at"] == T0 + timedelta(days=2)
    assert ivan["is_self"] is False
    assert "tg_id" not in ivan


async def test_person_search_tolerates_typos_case_and_username(conn):
    await seed(conn)
    assert [p["name"] for p in await archive.find_people(conn, "иван петров")] == ["Иван Петров"]
    typo = await archive.find_people(conn, "Петровв Иван")
    assert typo[0]["name"] == "Иван Петров" and typo[0]["tier"] == 2
    assert [p["name"] for p in await archive.find_people(conn, "@ivan_p")] == ["Иван Петров"]
    assert [p["name"] for p in await archive.find_people(conn, "ivan")] == ["Иван Петров"]
    assert await archive.find_people(conn, "Несуществующий") == []
    assert await archive.find_people(conn, "%") == []


async def test_same_first_name_gives_several_candidates_most_recent_first(conn):
    await seed(conn)
    people = await archive.find_people(conn, "Иван")
    assert [p["name"] for p in people] == ["Иван Петров", "Иван Сидоров"]
    found = await archive.resolve_person(conn, "Иван")
    assert found.status == "ambiguous" and len(found.candidates) == 2


async def test_person_known_only_from_a_group_has_no_direct_chat(conn):
    s = await seed(conn)
    maria = (await archive.find_people(conn, "Мария"))[0]
    assert maria["direct_chat_id"] is None
    assert maria["last_interaction_at"] == T0 + timedelta(minutes=22)
    assert (await archive.resolve_person(conn, "Мария")).row["id"] == maria["id"]
    assert (await archive.resolve_person(conn, maria["id"])).row["name"] == "Мария"
    assert (await archive.resolve_person(conn, 999999)).status == "not_found"
    rows = await archive.history(conn, sender_peer_id=maria["id"])
    assert [r["chat_id"] for r in rows] == [s.family, s.family]


async def test_owner_is_marked_as_self(conn):
    await seed(conn)
    me = (await archive.find_people(conn, "Евгений"))[0]
    assert me["is_self"] is True and me["direct_chat_id"] is None


# --- сообщения ---

async def test_context_window_marks_neighbours_and_reports_more(conn):
    s = await seed(conn)
    target = s.ids[(s.ivan, 3)]
    window = await archive.context(conn, target, before=1, after=2)
    assert window.target["id"] == target and window.target["text"].startswith("Договор")
    assert [m["tg_message_id"] for m in window.before] == [2]
    assert [m["tg_message_id"] for m in window.after] == [4, 5]      # удалённое 6 не считается
    assert (window.more_before, window.more_after) == (True, True)
    assert window.reply is None                                      # ответ на 2 — оно в окне
    assert window.target["reply_to_id"] == s.ids[(s.ivan, 2)]
    assert window.target["chat_title"] == "Иван Петров" and window.target["chat_kind"] == "user"
    whole = await archive.context(conn, target, before=50, after=50)
    assert (whole.more_before, whole.more_after) == (False, False)
    assert [m["tg_message_id"] for m in whole.before + whole.after] == [1, 2, 4, 5, 7, 8]


async def test_replied_message_outside_the_window_is_returned_separately(conn):
    s = await seed(conn)
    window = await archive.context(conn, s.ids[(s.ivan, 8)], before=1, after=1)
    assert [m["tg_message_id"] for m in window.before] == [7] and window.after == []
    assert window.reply["id"] == s.ids[(s.ivan, 1)] and "смету по фасадам" in window.reply["text"]
    zero = await archive.context(conn, s.ids[(s.ivan, 8)], before=0, after=0)
    assert zero.before == [] and zero.after == [] and zero.more_before is True
    assert zero.reply["id"] == s.ids[(s.ivan, 1)]


async def test_service_and_media_messages_keep_their_marks(conn):
    s = await seed(conn)
    rows = await archive.visible_messages(conn, [s.ids[(s.ivan, 4)], s.ids[(s.ivan, 5)]])
    call, voice = rows[s.ids[(s.ivan, 4)]], rows[s.ids[(s.ivan, 5)]]
    assert (call["kind"], call["service_action"]) == ("service", "phone_call")
    assert (voice["media_type"], voice["text"]) == ("voice_message", "")
    assert not set(archive._INTERNAL) & set(call)


async def test_history_filters_by_chat_time_and_direction(conn):
    s = await seed(conn)
    newest = await archive.history(conn, chat_id=s.ivan)
    assert [m["tg_message_id"] for m in newest] == [8, 7, 5, 4, 3, 2, 1]
    oldest = await archive.history(conn, chat_id=s.ivan, order="asc", limit=2)
    assert [m["tg_message_id"] for m in oldest] == [1, 2]
    minute = timedelta(minutes=1)
    # нижняя граница включается, верхняя — нет
    part = await archive.history(conn, chat_id=s.ivan, after=T0 + minute, before=T0 + 3 * minute, order="asc")
    assert [m["tg_message_id"] for m in part] == [2, 3]
    sent = await archive.history(conn, from_me=True, order="asc")
    assert [(m["chat_id"], m["tg_message_id"]) for m in sent] == [(s.ivan, 2), (s.family, 11)]
    received = await archive.history(conn, chat_id=s.family, from_me=False)
    assert [m["tg_message_id"] for m in received] == [12, 10]
    with pytest.raises(ValueError):
        await archive.history(conn, order="random")


@pytest.mark.parametrize("order", ["asc", "desc"])
async def test_history_pages_do_not_overlap_even_with_equal_times(conn, order):
    s = await seed(conn)
    # десять сообщений в одну и ту же секунду: порядок страниц держится на идентификаторе
    await put(conn, s.sidorov, [
        rec(100 + i, f"пачка {i}", sender=2003, name="Иван Сидоров", at=T0 + timedelta(hours=5))
        for i in range(10)
    ])
    everything = await archive.history(conn, limit=200, order=order)
    seen, cursor = [], None
    for _ in range(20):
        page = await archive.history(conn, limit=3, order=order, cursor=cursor)
        if not page:
            break
        seen += [m["id"] for m in page]
        cursor = (page[-1]["sent_at"], page[-1]["id"])
    assert seen == [m["id"] for m in everything] and len(seen) == len(set(seen)) == 23


# --- то, что не отдаётся никогда ---

async def test_excluded_chat_is_invisible_everywhere(conn):
    s = await seed(conn)
    secret_message = s.ids[(s.secret, 1)]
    assert await conn.fetchval("SELECT count(*) FROM messages WHERE chat_id = $1", s.secret) == 1
    assert s.secret not in {c["id"] for c in await archive.list_chats(conn, limit=500)}
    assert await archive.chat_by_id(conn, s.secret) is None
    assert (await archive.resolve_chat(conn, s.secret)).status == "not_found"
    assert (await archive.resolve_chat(conn, "Тайный Иван")).status == "not_found"
    assert await archive.find_chats(conn, "Тайный") == []
    assert await archive.visible_messages(conn, [secret_message]) == {}
    assert await archive.get_message(conn, secret_message) is None
    assert await archive.context(conn, secret_message) is None
    assert await archive.history(conn, chat_id=s.secret) == []
    # человек, известный только по исключённому чату, не находится — иначе чат выдал бы себя
    assert await archive.find_people(conn, "Тайный") == []
    peer_id = await conn.fetchval("SELECT id FROM peers WHERE tg_id = 2010")
    assert (await archive.resolve_person(conn, peer_id)).status == "not_found"
    assert await archive.history(conn, sender_peer_id=peer_id) == []
    text = await all_visible_text(conn, s)
    assert SECRET_EXCLUDED not in text and "Тайный" not in text


async def test_person_from_excluded_chat_seen_in_a_group_shows_only_the_group(conn):
    s = await seed(conn)
    await put(conn, s.family, [
        rec(600, "Всем привет", sender=2010, name="Тайный Иван", at=T0 + timedelta(minutes=25))])
    person = (await archive.find_people(conn, "Тайный"))[0]
    assert person["direct_chat_id"] is None
    assert person["last_interaction_at"] == T0 + timedelta(minutes=25)   # не время из исключённого чата
    assert SECRET_EXCLUDED not in await all_visible_text(conn, s)


async def test_deleted_message_is_invisible_everywhere(conn):
    s = await seed(conn)
    deleted = s.ids[(s.ivan, 6)]
    assert await conn.fetchval("SELECT deleted_at IS NOT NULL FROM messages WHERE id = $1", deleted)
    assert await archive.visible_messages(conn, [deleted]) == {}
    assert await archive.context(conn, deleted) is None
    assert deleted not in {m["id"] for m in await archive.history(conn, chat_id=s.ivan)}
    # ответ на удалённое не ссылается на него и не подтягивает его текст
    window = await archive.context(conn, s.ids[(s.ivan, 7)], before=1, after=0)
    assert window.target["reply_to_id"] is None and window.reply is None
    assert [m["tg_message_id"] for m in window.before] == [5]
    assert SECRET_DELETED not in await all_visible_text(conn, s)


async def test_fully_deleted_chat_leaves_no_time_or_count(conn):
    s = await seed(conn)
    await store.mark_deleted(conn, s.bot, [1])
    bot = next(c for c in await archive.list_chats(conn) if c["id"] == s.bot)
    assert (bot["message_count"], bot["last_message_at"]) == (0, None)


@pytest.mark.parametrize("tg_id,username", [
    (777000, None), (93372553, "BotFather"), (178220800, None),
    (5, "BotFather"), (6, "@SpamBot"), (7, "telegram"),
])
async def test_service_peers_with_codes_and_tokens_are_invisible(conn, tg_id, username):
    s = await seed(conn)
    chat_id, ids = await blocked_chat(conn, s, tg_id, username)
    # предусловие: признака «исключён» нет, сообщения в базе есть — скрывает только правило чтения
    assert await conn.fetchval("SELECT NOT excluded FROM chats WHERE id = $1", chat_id)
    assert await conn.fetchval("SELECT count(*) FROM messages WHERE text LIKE 'Login code%'") == 2
    assert chat_id not in {c["id"] for c in await archive.list_chats(conn, limit=500)}
    assert await archive.chat_by_id(conn, chat_id) is None
    assert (await archive.resolve_chat(conn, chat_id)).status == "not_found"
    assert (await archive.resolve_chat(conn, "Telegram")).status == "not_found"
    assert await archive.visible_messages(conn, list(ids.values())) == {}
    for message_id in ids.values():
        assert await archive.context(conn, message_id) is None
    assert await archive.history(conn, chat_id=chat_id) == []
    peer_id = await conn.fetchval("SELECT id FROM peers WHERE class = 'user' AND tg_id = $1", tg_id)
    assert (await archive.resolve_person(conn, peer_id)).status == "not_found"
    assert await archive.history(conn, sender_peer_id=peer_id) == []
    assert await archive.find_people(conn, "Telegram") == []
    if username:
        assert await archive.find_people(conn, username) == []
        assert (await archive.resolve_chat(conn, username)).status == "not_found"
    # сообщение служебного отправителя в обычной группе не видно и в счётчик не входит
    family = next(c for c in await archive.list_chats(conn) if c["id"] == s.family)
    assert family["message_count"] == 3 and family["last_message_at"] == T0 + timedelta(minutes=22)
    assert "Login code" not in await all_visible_text(conn, s)


def test_every_blocked_peer_from_store_is_in_the_read_filter():
    """Список служебных собеседников один — в store.py; запросы чтения собраны из него."""
    for tg_id in store.BLOCKED_USER_IDS:
        assert str(tg_id) in archive._BLOCKED_IDS
    for name in store.BLOCKED_USERNAMES:
        assert f"'{name}'" in archive._BLOCKED_NAMES
    assert archive._BLOCKED_IDS in archive._MESSAGES and archive._BLOCKED_NAMES in archive._MESSAGES
    assert archive._BLOCKED_IDS in archive._VISIBLE_CHATS and archive._BLOCKED_IDS in archive._PEOPLE


async def test_reply_to_a_service_peer_message_is_not_linked(conn):
    s = await seed(conn)
    await blocked_chat(conn, s, 777000, None)
    asked = await put(conn, s.family, [
        rec(501, "А это что за код?", sender=2002, name="Мария", at=T0 + timedelta(hours=5), reply=500)])
    window = await archive.context(conn, asked[(s.family, 501)])
    assert window.target["reply_to_id"] is None and window.reply is None
    assert all("Login code" not in m["text"] for m in window.before)


async def test_code_level_check_hides_service_peer_even_if_sql_filter_missed_it(conn, monkeypatch):
    """Вторая линия: проверка store.is_blocked_peer в коде не зависит от условия в SQL."""
    s = await seed(conn)
    monkeypatch.setattr(store, "is_blocked_peer", lambda cls, tg_id, username=None: tg_id == 2003)
    assert s.sidorov not in {c["id"] for c in await archive.list_chats(conn)}
    assert await archive.history(conn, chat_id=s.sidorov) == []
    assert [p["name"] for p in await archive.find_people(conn, "Иван")] == ["Иван Петров"]


async def test_queries_run_inside_a_read_only_transaction(conn):
    s = await seed(conn)
    async with conn.transaction(readonly=True):
        assert len(await archive.list_chats(conn, query="иван")) == 2
        assert (await archive.resolve_chat(conn, "Семья")).status == "ok"
        assert len(await archive.find_people(conn, "Иван")) == 2
        assert await archive.context(conn, s.ids[(s.ivan, 3)]) is not None
