"""Сферы (Р-82): модель данных, папки Telegram, раскладки списка чатов — на уровне функций и базы.

Главное, что здесь проверяется:
  * сферы заводятся, переименовываются, упорядочиваются; одно название — одна сфера (без учёта регистра и «ё»);
  * чат может быть в нескольких сферах; сферу можно назначить и чату, которого ещё нет в архиве, —
    запись в `chats` для него не появляется (иначе он стал бы виден ассистенту);
  * удаление сферы — только снятие её с чатов и проектов: чаты, сообщения и проекты остаются;
  * состав папки Telegram — по явным перечням и правилам; что вычислено не точно — сказано;
  * «Сделать сферу из папки» и повтор — дополнение; связанная папка новые чаты только предлагает;
  * раскладки: активность по времени последнего сообщения, вид, сферы, папки.
"""

from datetime import datetime, timedelta, timezone

import pytest

from shturman import spheres, store
from shturman.records import ChatRecord
from shturman.tg import dialogs as dl

IVAN, MARIA, BOT, NEW = ("user", 2001), ("user", 2002), ("user", 2100), ("user", 2003)
FAMILY, CHAT, NEWS = ("chat", 3001), ("channel", 4002), ("channel", 4001)


def row(key, title, kind, *, contact=None, muted=False, unread=False, archived=False, at=None, bot=False):
    chat = ChatRecord(key[0], key[1], kind, title, is_bot=bot or None)
    return dl.DialogRow(chat=chat, top_message=10, last_message_at=at, folder_id=1 if archived else 0,
                        is_contact=contact, muted_until=datetime(2038, 1, 1, tzinfo=timezone.utc) if muted else None,
                        unread=unread)


async def account(conn, tg_user_id=1000, role="owner"):
    return await store.ensure_account(conn, tg_user_id, "Евгений", role)


async def put_dialogs(conn, account_id, rows):
    load_id = await dl.begin(conn, account_id)
    await dl.save_page(conn, account_id, load_id, rows, position=0, expected=len(rows))
    await dl.finish(conn, account_id, load_id, mark_seen=False)


def world(now):
    return [
        row(IVAN, "Иван Петров", "personal_chat", contact=True, unread=True, at=now - timedelta(days=1)),
        row(MARIA, "Мария", "personal_chat", contact=False, muted=True, at=now - timedelta(days=20)),
        row(BOT, "Погода", "bot_chat", bot=True, at=now - timedelta(days=60)),
        row(FAMILY, "Семья", "private_group", unread=True, at=now - timedelta(days=200)),
        row(CHAT, "Стройка: чат", "private_supergroup", archived=True, at=now - timedelta(days=3)),
        row(NEWS, "Стройка: новости", "public_channel", at=None),
    ]


def folder(fid, title, *, include=(), exclude=(), pinned=(), flags=(), kind="filter"):
    ref = lambda key: f"{key[0]}:{key[1]}"   # noqa: E731
    return dl.FolderRow(folder_id=fid, title=title, kind=kind, position=fid, include=[ref(k) for k in include],
                        exclude=[ref(k) for k in exclude], pinned=[ref(k) for k in pinned], flags=list(flags))


# --- сферы -------------------------------------------------------------------------------------------

async def test_spheres_are_created_renamed_ordered_and_names_are_unique(conn):
    work = await spheres.create_sphere(conn, "  Работа  РАЗУМ ", "стройка, продажи")
    assert (work["title"], work["description"], work["chats"], work["projects"]) == ("Работа РАЗУМ", "стройка, продажи", 0, 0)
    home = await spheres.create_sphere(conn, "Личное")
    for taken in ("работа разум", "РАБОТА  разум", "«Работа РАЗУМ»"):
        with pytest.raises(spheres.SpheresError) as exc:
            await spheres.create_sphere(conn, taken)
        assert exc.value.code == "exists"
    assert await spheres.create_sphere(conn, "Ёлки") and not await conn.fetchval(
        "SELECT 1 FROM spheres WHERE title_norm = 'елки' AND title <> 'Ёлки'")
    with pytest.raises(spheres.SpheresError):
        await spheres.create_sphere(conn, "елки")                     # ё и е — одно название
    for bad in ("", "   ", "!!!", "все", "Без сферы", "Работа " * 10, 5):
        with pytest.raises(spheres.SpheresError):
            await spheres.create_sphere(conn, bad)
    with pytest.raises(spheres.SpheresError):
        await spheres.create_sphere(conn, "Учёба", "описание " * 40)

    renamed = await spheres.update_sphere(conn, home["id"], title="Семья и дом", description=None)
    assert (renamed["title"], renamed["description"]) == ("Семья и дом", None)
    with pytest.raises(spheres.SpheresError):
        await spheres.update_sphere(conn, home["id"], title="работа разум")
    order = [s["id"] for s in await spheres.list_spheres(conn)]
    assert order[:2] == [work["id"], home["id"]]
    reordered = await spheres.reorder(conn, list(reversed(order)))
    assert [s["id"] for s in reordered] == list(reversed(order))
    with pytest.raises(spheres.SpheresError):
        await spheres.reorder(conn, order[:1])                          # не все сферы — отказ


async def test_chat_is_in_several_spheres_and_deleting_a_sphere_keeps_chats_messages_and_projects(conn):
    acc = await account(conn)
    ivan_chat, _ = await store.ensure_chat(conn, acc, ChatRecord("user", IVAN[1], "personal_chat", "Иван Петров"))
    await conn.execute("""INSERT INTO messages (chat_id, tg_message_id, sent_at, text, sources)
                          VALUES ($1, 1, now(), 'смета', '{session}')""", ivan_chat)
    work, home = await spheres.create_sphere(conn, "Работа"), await spheres.create_sphere(conn, "Личное")
    assert await spheres.add_chats(conn, work["id"], acc, [IVAN]) == 1
    assert await spheres.add_chats(conn, home["id"], acc, [IVAN, IVAN]) == 1
    assert await spheres.add_chats(conn, home["id"], acc, [IVAN]) == 0            # уже там
    assert await spheres.titles_by_chat(conn, [ivan_chat]) == {ivan_chat: ["Работа", "Личное"]}
    project_id = await conn.fetchval(
        """INSERT INTO projects (title, title_norm, status, origin) VALUES ('ЖК Северный', 'жк северный', 'active', 'owner')
           RETURNING id""")
    await conn.execute("INSERT INTO project_chats (project_id, chat_id) VALUES ($1, $2)", project_id, ivan_chat)
    assert (await spheres.set_project_sphere(conn, project_id, home["id"]))["changed"] is True

    gone = await spheres.delete_sphere(conn, home["id"])
    assert (gone["chats"], gone["projects"]) == (1, 1)
    assert await spheres.titles_by_chat(conn, [ivan_chat]) == {ivan_chat: ["Работа"]}     # во второй сфере остался
    assert await conn.fetchval("SELECT count(*) FROM chats") == 1
    assert await conn.fetchval("SELECT count(*) FROM messages") == 1
    assert await conn.fetchval("SELECT (status, sphere_id) FROM projects WHERE id = $1", project_id) == ("active", None)
    assert await conn.fetchval("SELECT count(*) FROM project_chats") == 1
    assert await conn.fetchval("SELECT count(*) FROM sphere_chats WHERE sphere_id = $1", home["id"]) == 0
    with pytest.raises(spheres.SpheresError):
        await spheres.delete_sphere(conn, home["id"])


async def test_a_chat_outside_the_archive_gets_a_sphere_without_a_chat_record(conn):
    acc = await account(conn)
    now = await conn.fetchval("SELECT now()")
    await put_dialogs(conn, acc, world(now))
    work = await spheres.create_sphere(conn, "Работа")
    assert await spheres.add_chats(conn, work["id"], acc, [CHAT, NEWS]) == 2
    assert await conn.fetchval("SELECT count(*) FROM chats") == 0               # архив и ассистент его не видят
    listed = await spheres.sphere_chats(conn, work["id"])
    assert [(c["title"], c["enabled"]) for c in listed] == [("Стройка: новости", False), ("Стройка: чат", False)]
    # чат включили для чтения — сфера сходится с ним через (аккаунт, класс, номер)
    chat_id, _ = await store.ensure_chat(conn, acc, ChatRecord("channel", CHAT[1], "private_supergroup", "Стройка: чат"))
    assert await spheres.titles_by_chat(conn, [chat_id]) == {chat_id: ["Работа"]}
    assert (await spheres.list_spheres(conn))[0]["archived_chats"] == 1
    # выход из аккаунта стирает список диалогов, но не решения владельца о сферах
    await dl.forget(conn, acc)
    assert [c["title"] for c in await spheres.sphere_chats(conn, work["id"])] == [None, "Стройка: чат"]


async def test_set_chat_spheres_and_removing_marks_declined(conn):
    acc = await account(conn)
    a = await spheres.create_sphere(conn, "Работа")
    b = await spheres.create_sphere(conn, "Личное")
    c = await spheres.create_sphere(conn, "Учёба")
    out = await spheres.set_chat_spheres(conn, acc, IVAN, [a["id"], b["id"]])
    assert sorted(out["added"]) == sorted([a["id"], b["id"]]) and out["removed"] == []
    out = await spheres.set_chat_spheres(conn, acc, IVAN, [b["id"], c["id"]])
    assert out == {"added": [c["id"]], "removed": [a["id"]]}
    assert (await spheres.spheres_of_keys(conn, acc, [IVAN]))[IVAN] == [b["id"], c["id"]]
    assert await conn.fetchval("SELECT count(*) FROM sphere_declined WHERE sphere_id = $1", a["id"]) == 1
    with pytest.raises(spheres.SpheresError):
        await spheres.set_chat_spheres(conn, acc, IVAN, [999999])


# --- папки Telegram -----------------------------------------------------------------------------------

def test_folder_members_follow_explicit_lists_and_rules():
    now = datetime(2026, 10, 14, tzinfo=timezone.utc)
    rows = world(now)
    keys = lambda f: spheres.folder_members(f, rows, now=now)  # noqa: E731
    assert keys(folder(2, "Явные", include=[IVAN, NEWS], pinned=[FAMILY])) == [IVAN, FAMILY, NEWS]
    # «никогда» сильнее правила, «всегда» сильнее отсева
    assert keys(folder(3, "Контакты", flags=["contacts"])) == [IVAN]
    assert keys(folder(4, "Не контакты", flags=["non_contacts"])) == [MARIA]
    assert keys(folder(5, "Группы", flags=["groups"])) == [FAMILY, CHAT]
    assert keys(folder(6, "Каналы", flags=["broadcasts"])) == [NEWS]
    assert keys(folder(7, "Боты", flags=["bots"])) == [BOT]
    assert keys(folder(8, "Люди без звука", flags=["contacts", "non_contacts", "exclude_muted"])) == [IVAN]
    assert keys(folder(9, "Непрочитанные", flags=["contacts", "non_contacts", "groups", "exclude_read"])) == [IVAN, FAMILY]
    assert keys(folder(10, "Без архива", flags=["groups", "exclude_archived"], include=[CHAT])) == [FAMILY, CHAT]
    assert keys(folder(11, "Группы, кроме семьи", flags=["groups"], exclude=[FAMILY])) == [CHAT]
    # общая папка по ссылке — только явный перечень, правил у неё нет
    assert keys(folder(12, "Общая", include=[MARIA], flags=["groups"], kind="chatlist")) == [MARIA]
    # чего не знаем точно — сказано
    notes = spheres.folder_notes(folder(13, "Тихие", flags=["groups", "exclude_muted", "exclude_read"], include=[("user", 9999)]), rows)
    assert any("общие настройки" in n for n in notes) and any("последнее обновление" in n for n in notes)
    assert spheres.MISSING_NOTE in notes
    assert spheres.folder_notes(folder(14, "Явные", include=[IVAN]), rows) == []


def test_folders_are_parsed_from_telegram_objects():
    from telethon.tl import types

    response = types.messages.DialogFilters(filters=[
        types.DialogFilterDefault(),
        types.DialogFilter(id=2, title=types.TextWithEntities("Работа", []), emoticon="💼",
                           pinned_peers=[types.InputPeerChat(3001)], include_peers=[types.InputPeerUser(2001, 7), types.InputPeerSelf()],
                           exclude_peers=[types.InputPeerChannel(4001, 7)], groups=True, exclude_muted=True),
        types.DialogFilterChatlist(id=3, title=types.TextWithEntities("Общая", []), pinned_peers=[],
                                   include_peers=[types.InputPeerChannel(4002, 7)]),
    ])
    parsed = dl.parse_folders(response, self_id=1000)
    assert [(f.folder_id, f.title, f.kind, f.emoticon) for f in parsed] == [(2, "Работа", "filter", "💼"),
                                                                             (3, "Общая", "chatlist", None)]
    assert parsed[0].pinned == ["chat:3001"] and parsed[0].include == ["user:2001", "user:1000"]
    assert parsed[0].exclude == ["channel:4001"] and parsed[0].flags == ["groups", "exclude_muted"]
    assert parsed[1].include == ["channel:4002"] and parsed[1].flags == [] and parsed[1].exclude == []


async def test_sphere_from_folder_and_repeating_extends_it(conn):
    acc = await account(conn)
    now = await conn.fetchval("SELECT now()")
    rows = world(now)
    await put_dialogs(conn, acc, rows)
    await dl.save_folders(conn, acc, [folder(2, "Стройка", include=[CHAT, NEWS, IVAN])])
    made = await spheres.make_from_folder(conn, acc, 2)
    assert made["created"] and made["sphere"]["title"] == "Стройка" and made["added"] == 3
    sphere_id = made["sphere"]["id"]
    assert set(await spheres.keys_of(conn, sphere_id, acc)) == {CHAT, NEWS, IVAN}
    # владелец убрал Ивана; в папке появился новый чат — «дополнить» добавит новый, но не вернёт убранного
    await spheres.remove_chats(conn, sphere_id, acc, [IVAN])
    await put_dialogs(conn, acc, rows + [row(NEW, "Прораб", "personal_chat", at=now)])
    await dl.save_folders(conn, acc, [folder(2, "Стройка", include=[CHAT, NEWS, IVAN, NEW])])
    again = await spheres.make_from_folder(conn, acc, 2)
    assert not again["created"] and again["sphere"]["id"] == sphere_id and again["added"] == 1
    assert set(await spheres.keys_of(conn, sphere_id, acc)) == {CHAT, NEWS, NEW}
    assert await conn.fetchval("SELECT count(*) FROM spheres") == 1
    # имя папки уже занято сферой — новая получает номер
    await dl.save_folders(conn, acc, [folder(2, "Стройка", include=[CHAT]), folder(5, "стройка", include=[MARIA])])
    other = await spheres.make_from_folder(conn, acc, 5)
    assert other["created"] and other["sphere"]["title"] == "стройка 2"
    with pytest.raises(spheres.SpheresError):
        await spheres.make_from_folder(conn, acc, 77)


async def test_linked_folder_offers_new_chats_and_never_adds_them_by_itself(conn):
    acc = await account(conn)
    now = await conn.fetchval("SELECT now()")
    rows = world(now)
    await put_dialogs(conn, acc, rows)
    await dl.save_folders(conn, acc, [folder(2, "Работа", include=[IVAN, CHAT])])
    work = await spheres.create_sphere(conn, "Работа")
    await spheres.add_chats(conn, work["id"], acc, [IVAN])
    await spheres.link_folder(conn, work["id"], acc, 2, propose=True)
    (offer,) = await spheres.proposals(conn)
    assert (offer["sphere_id"], offer["folder_id"], offer["count"], offer["keys"]) == (work["id"], 2, 1, [CHAT])
    # новые чаты в папке после обновления списка — предлагаются, в сферу сами не попадают
    await put_dialogs(conn, acc, rows + [row(NEW, "Прораб", "personal_chat", at=now)])
    await dl.save_folders(conn, acc, [folder(2, "Работа", include=[IVAN, CHAT, NEW])])
    (offer,) = await spheres.proposals(conn, work["id"])
    assert offer["keys"] == [CHAT, NEW] and [c["title"] for c in offer["chats"]] == ["Стройка: чат", "Прораб"]
    assert set(await spheres.keys_of(conn, work["id"], acc)) == {IVAN}
    assert await spheres.decide_proposals(conn, work["id"], acc, 2, accept=False, keys=[NEW]) == {"added": 0, "declined": 1}
    assert await spheres.decide_proposals(conn, work["id"], acc, 2, accept=True) == {"added": 1, "declined": 0}
    assert await spheres.proposals(conn) == []
    assert set(await spheres.keys_of(conn, work["id"], acc)) == {IVAN, CHAT}
    # отвязали — предложений нет, сфера и её чаты на месте
    await dl.save_folders(conn, acc, [folder(2, "Работа", include=[IVAN, CHAT, NEW, MARIA])])
    assert [o["keys"] for o in await spheres.proposals(conn)] == [[MARIA]]
    await spheres.link_folder(conn, work["id"], acc, 2, propose=False)
    assert await spheres.proposals(conn) == [] and len(await spheres.keys_of(conn, work["id"], acc)) == 2


# --- раскладки ------------------------------------------------------------------------------------------

async def test_layouts_by_activity_kind_spheres_and_folders(conn):
    acc = await account(conn)
    now = await conn.fetchval("SELECT now()")
    await put_dialogs(conn, acc, world(now))
    by = lambda found: {s.id: s.keys for s in found}  # noqa: E731
    assert by(await spheres.sections(conn, acc, "activity")) == {
        "activity:week": [IVAN, CHAT], "activity:month": [MARIA], "activity:quarter": [BOT],
        "activity:silent": [FAMILY, NEWS]}
    assert by(await spheres.sections(conn, acc, "kind")) == {
        "kind:personal": [IVAN, MARIA], "kind:group": [FAMILY, CHAT], "kind:channel": [NEWS], "kind:bot": [BOT]}
    work = await spheres.create_sphere(conn, "Работа")
    await spheres.create_sphere(conn, "Пустая")
    await spheres.add_chats(conn, work["id"], acc, [IVAN, CHAT])
    found = await spheres.sections(conn, acc, "spheres")
    assert [(s.title, s.keys) for s in found] == [("Работа", [IVAN, CHAT]), ("Пустая", []),
                                                  ("Без сферы", [MARIA, BOT, FAMILY, NEWS])]
    await dl.save_folders(conn, acc, [folder(2, "Люди", flags=["contacts", "non_contacts"])])
    found = await spheres.sections(conn, acc, "folders")
    assert [(s.id, s.keys) for s in found] == [("folder:2", [IVAN, MARIA]), ("folder:none", [BOT, FAMILY, CHAT, NEWS])]
    section, _ = await spheres.section_keys(conn, acc, "sphere:none")
    assert section.keys == [MARIA, BOT, FAMILY, NEWS]
    for bad in ("folder:x", "sphere:", "activity:year", "", "kind:people; DROP"):
        with pytest.raises(spheres.SpheresError):
            await spheres.section_keys(conn, acc, bad)
    with pytest.raises(spheres.SpheresError):
        await spheres.sections(conn, acc, "alphabet")


# --- сужение для ассистента -----------------------------------------------------------------------------

async def test_scope_names_all_and_no_sphere(conn):
    acc = await account(conn)
    work = await spheres.create_sphere(conn, "Работа РАЗУМ")
    await spheres.create_sphere(conn, "Личное")
    ivan_chat, _ = await store.ensure_chat(conn, acc, ChatRecord("user", IVAN[1], "personal_chat", "Иван"))
    maria_chat, _ = await store.ensure_chat(conn, acc, ChatRecord("user", MARIA[1], "personal_chat", "Мария"))
    await spheres.add_chats(conn, work["id"], acc, [IVAN])
    for everything in (None, [], "все", ["all"], ["Работа РАЗУМ", "везде"]):
        assert await spheres.resolve_scope(conn, everything) is None
    scope = await spheres.resolve_scope(conn, ["работа  разум"])
    assert scope.titles == ("Работа РАЗУМ",) and not scope.without
    assert await spheres.chat_ids_in(conn, scope) == [ivan_chat]
    scope = await spheres.resolve_scope(conn, "без сферы")
    assert await spheres.chat_ids_in(conn, scope) == [maria_chat]
    assert scope.allows([]) and not scope.allows(["Личное"])
    with pytest.raises(spheres.SpheresError) as exc:
        await spheres.resolve_scope(conn, ["Учёба"])
    assert '"Работа РАЗУМ", "Личное"' in exc.value.message
    assert spheres.query_names({"spheres": "Работа\nЛичное"}) == ["Работа", "Личное"]
