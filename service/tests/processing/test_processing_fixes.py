"""Исправления по итогам проверок: видимость после исключения чата, большой импорт, длина сводки,
идентификаторы в инструментах агента, автоответы сервиса."""

from datetime import date, time, timedelta, timezone, datetime

from shturman import authority, bridge, outbox, store
from shturman.processing import commitments, people, pipeline

from conftest import MCP_AUTH
from proc_helpers import OWNER, T0, TZ, account, answer, buttons_of, chat, claim, peer_id, press, say

IVAN, MARIA, PETR = 2001, 2002, 2005
NOW = T0 + timedelta(hours=1)
TODAY = date(2026, 10, 6)
SERVICE = ("shturman.api_core", "shturman.ingest_api", "shturman.mcp_server",
           "shturman.processing.service", "shturman.processing.mcp_tools")


def item(message, quote, what, due=None, recipient=None):
    return {"message": message, "source_quote": quote, "what": what, "due_expression": due,
            "due_message": None, "recipient": recipient, "duplicate_of": None}


async def plan(conn, **kw):
    return await pipeline.plan_run(conn, tz=TZ, now=kw.pop("now", NOW), **kw)


async def extract_once(conn, items, **kw):
    out = await plan(conn, **kw)
    for job in await claim(conn):
        await answer(conn, job, {"commitments": items, "updates": []})
    return out


async def digests(conn):
    return sorted(await claim(conn, bridge.NOTIFY_OWNER), key=lambda job: job["id"])


async def mcp(client, tool, **arguments):
    """Вызов инструмента агента. Возвращает (данные, текст ошибки)."""
    response = await client.post(
        "/mcp", headers={**MCP_AUTH, "Accept": "application/json, text/event-stream", "Host": "test"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    if result.get("isError"):
        return None, result["content"][0]["text"]
    return result["structuredContent"], None


async def two_chats(conn):
    """Иван и Мария, у каждого по открытому обязательству; у Марии ещё алиас и адрес."""
    account_id = await account(conn)
    await bridge.set_owner(conn, OWNER, OWNER)
    ivan_chat = await chat(conn, account_id, IVAN, "Иван Петров")
    maria_chat = await chat(conn, account_id, MARIA, "Мария Тайная", username="maria_secret")
    ivan_ids = await say(conn, ivan_chat, [(IVAN, "Иван Петров", "Смету по фасадам пришлю завтра")])
    await extract_once(conn, [item(1, "Смету по фасадам пришлю завтра", "прислать смету по фасадам", "завтра")])
    maria_ids = await say(conn, maria_chat, [(MARIA, "Мария Тайная", "Акт сверки пришлю завтра")],
                          first_id=50, start=T0 + timedelta(hours=2))
    await extract_once(conn, [item(1, "Акт сверки пришлю завтра", "прислать акт сверки", "завтра")],
                       now=T0 + timedelta(hours=3))
    ivan_c, maria_c = [r["id"] for r in await conn.fetch("SELECT id FROM commitments ORDER BY id")]
    for commitment_id in (ivan_c, maria_c):
        with authority.owner_context(OWNER, chat_id=OWNER):
            await commitments.accept(conn, commitment_id)
    maria_person = await people.person_for_peer(conn, await peer_id(conn, MARIA))
    await people.add_alias(conn, maria_person, "Маша Секретарь")
    await claim(conn, bridge.NOTIFY_OWNER)
    return {"account": account_id, "ivan_chat": ivan_chat, "maria_chat": maria_chat, "ivan_c": ivan_c,
            "maria_c": maria_c, "maria_person": maria_person, "ivan_ids": ivan_ids, "maria_ids": maria_ids,
            "ivan_person": await people.person_for_peer(conn, await peer_id(conn, IVAN))}


SECRETS = ("Мария", "Тайная", "maria_secret", "Секретарь", "акт сверки", "Акт сверки")


def leaks(payload) -> list[str]:
    text = str(payload)
    return [word for word in SECRETS if word in text]


# --- 1. исключённый чат не виден сразу, а не после ночной чистки ---------------------------------

async def test_excluded_chat_is_hidden_from_routes_at_once(make_client, conn):
    client, state = await make_client(*SERVICE)
    s = await two_chats(conn)
    assert leaks((await client.get("/api/commitments")).json()) and leaks((await client.get("/api/people")).json())

    # владелец исключил чат; событие ещё не обработано (или потерялось) — строки лежат в базе
    await conn.execute("UPDATE chats SET excluded = true WHERE id = $1", s["maria_chat"])
    assert await conn.fetchval("SELECT count(*) FROM commitments") == 2

    for view in commitments.VIEWS:
        body = (await client.get("/api/commitments", params={"view": view})).json()
        assert not leaks(body), view
    assert [c["id"] for c in (await client.get("/api/commitments", params={"view": "all"})).json()["commitments"]] == [s["ivan_c"]]
    assert (await client.get(f"/api/commitments/{s['maria_c']}")).status_code == 404
    assert (await client.get("/api/commitments", params={"person_id": s["maria_person"]})).status_code == 404
    by_peer = await client.get("/api/commitments", params={"peer_id": await peer_id(conn, MARIA), "view": "all"})
    assert by_peer.json()["commitments"] == []
    assert (await client.get("/api/commitments", params={"chat_id": s["maria_chat"], "view": "all"})).json()["commitments"] == []
    # и менять невидимое нельзя: для маршрутов его нет
    for action in ("close", "cancel", "reopen", "accept", "reject"):
        assert (await client.post(f"/api/commitments/{s['maria_c']}/{action}")).status_code == 404
    assert (await client.post(f"/api/commitments/{s['maria_c']}/reschedule", json={"due": "завтра"})).status_code == 404
    assert await conn.fetchval("SELECT status FROM commitments WHERE id = $1", s["maria_c"]) == "open"

    # человек, у которого остался только исключённый чат: ни имени, ни алиаса, ни адреса
    listed = (await client.get("/api/people")).json()
    assert not leaks(listed) and s["maria_person"] not in {p["id"] for p in listed["people"]}
    for query in ("Мария", "Марии Тайной", "Маша Секретарь", "@maria_secret", "Maria Taynaya"):
        assert (await client.get("/api/people", params={"query": query})).json()["people"] == [], query
    assert (await client.get(f"/api/people/{s['maria_person']}")).status_code == 404
    assert (await client.post(f"/api/people/{s['maria_person']}/aliases", json={"alias": "Маша"})).status_code == 404
    assert (await client.post("/api/people/merge", json={"source_id": s["maria_person"],
                                                         "target_id": s["ivan_person"]})).status_code == 404
    assert (await people.resolve_mention(conn, "Марии Тайной"))["status"] == "none"
    assert (await people.match_display_name(conn, "Мария Тайная"))["status"] == "none"
    # Иван на месте
    assert (await client.get(f"/api/people/{s['ivan_person']}")).json()["display_name"] == "Иван Петров"

    # удалённое сообщение-источник прячет обязательство так же — без события
    await store.mark_deleted(conn, s["ivan_chat"], [1])
    assert (await client.get(f"/api/commitments/{s['ivan_c']}")).status_code == 404
    assert (await client.get("/api/commitments", params={"view": "all"})).json()["commitments"] == []


async def test_excluded_chat_is_hidden_from_agent_tools(make_client, conn):
    client, state = await make_client(*SERVICE)
    s = await two_chats(conn)
    both, _ = await mcp(client, "list_commitments", view="all")
    assert {c["id"] for c in both["items"]} == {s["ivan_c"], s["maria_c"]}

    await conn.execute("UPDATE chats SET excluded = true WHERE id = $1", s["maria_chat"])
    for view in commitments.VIEWS:
        out, error = await mcp(client, "list_commitments", view=view)
        assert error is None and not leaks(out), view
    out, _ = await mcp(client, "list_commitments", view="all")
    assert [c["id"] for c in out["items"]] == [s["ivan_c"]]
    out, error = await mcp(client, "get_commitment", commitment_id=s["maria_c"])
    assert out is None and error.endswith("No such commitment.")
    out, error = await mcp(client, "list_commitments", view="all", person_id=s["maria_person"])
    assert out is None and "No person" in error
    out, error = await mcp(client, "list_commitments", view="all", peer_id=await peer_id(conn, MARIA))
    assert out is None and "No person" in error
    out, _ = await mcp(client, "list_commitments", view="all", chat_id=s["maria_chat"])
    assert out["items"] == []


async def test_excluding_a_chat_purges_its_commitments_without_waiting_for_the_night(make_client, conn):
    client, state = await make_client(*SERVICE)
    s = await two_chats(conn)
    done = await client.put(f"/api/chats/{s['maria_chat']}/excluded", json={"excluded": True})
    assert done.status_code == 200
    await state.events.drain()
    assert [r["id"] for r in await conn.fetch("SELECT id FROM commitments")] == [s["ivan_c"]]
    assert await conn.fetchval("SELECT count(*) FROM commitment_events WHERE commitment_id = $1", s["maria_c"]) == 0


async def test_hidden_commitment_is_not_shown_to_owner_either(conn):
    account_id = await account(conn)
    await bridge.set_owner(conn, OWNER, OWNER)
    ivan_chat = await chat(conn, account_id, IVAN, "Иван Петров")
    maria_chat = await chat(conn, account_id, MARIA, "Мария Тайная")
    await say(conn, ivan_chat, [(IVAN, "Иван Петров", "Смету по фасадам пришлю завтра")])
    await say(conn, maria_chat, [(MARIA, "Мария Тайная", "Акт сверки пришлю завтра")], first_id=50)
    out = await plan(conn)
    jobs_ = await claim(conn)
    # пока запросы были у модели, владелец исключил чат Марии
    await conn.execute("UPDATE chats SET excluded = true WHERE id = $1", maria_chat)
    for job in jobs_:
        quote = "Акт сверки пришлю завтра" if "Акт сверки" in job["payload"]["input"] else "Смету по фасадам пришлю завтра"
        await answer(conn, job, {"commitments": [item(1, quote, "прислать документ", "завтра")]})
    assert await conn.fetchval("SELECT count(*) FROM commitments") == 1       # по исключённому чату ничего не записано
    digest, = await digests(conn)
    assert not leaks(digest["payload"]["text"]) and len(digest["payload"]["buttons"]) == 1
    # предложение, чей чат исключили уже после показа: кнопка не работает, в итог не попадает
    proposed = await conn.fetchval("SELECT id FROM commitments")
    await conn.execute("UPDATE chats SET excluded = true WHERE id = $1", ivan_chat)
    assert (await press(conn, f"sh:cm:a:{proposed}"))["answer"] == "Это обязательство уже удалено."
    assert await conn.fetchval("SELECT status FROM commitments WHERE id = $1", proposed) == "proposed"
    assert out["planned"] == 2


async def test_partly_hidden_person_shows_only_visible_accounts(conn):
    account_id = await account(conn)
    await chat(conn, account_id, IVAN, "Иван Петров")
    hidden = await chat(conn, account_id, 2020, "Ivan Petrov", username="ivan_private")
    first = await people.ensure_person_for_peer(conn, await peer_id(conn, IVAN))
    second = await people.ensure_person_for_peer(conn, await peer_id(conn, 2020))
    await people.merge_people(conn, second, first)
    await conn.execute("UPDATE chats SET excluded = true WHERE id = $1", hidden)
    person = await people.get_person(conn, first)
    assert [p["tg_id"] for p in person["peers"]] == [IVAN]
    assert "ivan_private" not in str(person)
    assert person["untrusted_fields"] == people.UNTRUSTED_FIELDS


# --- 2. большой импорт старой истории не задерживает свежие сообщения ---------------------------

async def test_old_backlog_does_not_consume_runs(conn):
    account_id = await account(conn)
    await bridge.set_owner(conn, OWNER, OWNER)
    archive_chat = await chat(conn, account_id, IVAN, "Иван Петров")
    ivan = await peer_id(conn, IVAN)
    # 60 000 сообщений двухлетней давности — больше трёх прежних «окон» прогона
    await conn.execute(
        """INSERT INTO messages (chat_id, tg_message_id, sent_at, sender_peer_id, sender_name, is_outgoing, text, sources)
           SELECT $1, g, $2::timestamptz + g * interval '1 minute', $3, 'Иван Петров', false,
                  'Пришлю документ завтра', ARRAY['import']
           FROM generate_series(1, 60000) g""",
        archive_chat, T0 - timedelta(days=730), ivan)
    fresh = await say(conn, archive_chat, [(IVAN, "Иван Петров", "Смету по фасадам пришлю завтра")], first_id=70000)

    out = await plan(conn)
    assert (out["planned"], out["more"], out["cap_reached"]) == (1, False, False)     # свежее — в первом же прогоне
    assert (out["messages"]["skipped_old"], out["messages"]["eligible"], out["messages"]["deferred"]) == (60000, 1, 0)
    assert out["watermark"] == fresh[0]
    job, = await claim(conn)
    assert job["payload"]["input"].count("[1]") == 1 and "Смету по фасадам" in job["payload"]["input"]
    await answer(conn, job, {"commitments": []})
    again = await plan(conn)
    assert (again["status"], again["messages"]["new"]) == ("nothing_to_do", 0)


async def test_nightly_continues_while_there_is_more_but_not_forever(conn):
    account_id = await account(conn)
    await bridge.set_owner(conn, OWNER, OWNER)
    for n in range(5):
        chat_id = await chat(conn, account_id, 2100 + n, f"Контрагент {n}")
        await say(conn, chat_id, [(2100 + n, f"Контрагент {n}", f"Пришлю документ {n} завтра")], first_id=100 + n)
    options = pipeline.Options(limit=2, nightly_runs=2)
    at = time(3, 30)

    def msk(hour, minute):
        return datetime(2026, 10, 7, hour, minute, tzinfo=timezone(timedelta(hours=3)))

    first = await pipeline.nightly_tick(conn, tz=TZ, at=at, now=msk(3, 30), options=options)
    assert (first["planned"], first["more"]) == (2, True)
    # прогон ещё ждёт модель — следующий не запускается
    assert await pipeline.nightly_tick(conn, tz=TZ, at=at, now=msk(3, 31), options=options) is None
    for job in await claim(conn):
        await answer(conn, job, {"commitments": []})
    # закончился, а разбирать ещё есть что: продолжаем той же ночью, а не через сутки
    second = await pipeline.nightly_tick(conn, tz=TZ, at=at, now=msk(3, 32), options=options)
    assert (second["planned"], second["more"]) == (2, True)
    for job in await claim(conn):
        await answer(conn, job, {"commitments": []})
    # предел прогонов за ночь исчерпан — остаток подождёт следующей ночи
    assert await pipeline.nightly_tick(conn, tz=TZ, at=at, now=msk(3, 33), options=options) is None
    assert await conn.fetchval("SELECT count(*) FROM processing_runs WHERE trigger = 'nightly'") == 2
    third = await pipeline.nightly_tick(
        conn, tz=TZ, at=at, now=msk(3, 30) + timedelta(days=1), options=options)
    assert (third["planned"], third["more"]) == (1, False)
    for job in await claim(conn):
        await answer(conn, job, {"commitments": []})
    # разбирать нечего — продолжения нет
    assert await pipeline.nightly_tick(conn, tz=TZ, at=at, now=msk(3, 40) + timedelta(days=1), options=options) is None


# --- 4. длина сводки считается в единицах UTF-16 -------------------------------------------------

async def emoji_proposals(conn, count=5):
    account_id = await account(conn)
    await bridge.set_owner(conn, OWNER, OWNER)
    name = "Иван " + "🚀" * 70
    ivan_chat = await chat(conn, account_id, IVAN, name)
    smiles = "😀" * 150
    await say(conn, ivan_chat, [(IVAN, name, f"Документ номер {n} {smiles} пришлю завтра")
                                for n in range(1, count + 1)], step=10)
    await extract_once(conn, [item(n, f"Документ номер {n} {smiles} пришлю завтра",
                                   f"прислать документ номер {n} " + "🎉" * 180, "завтра")
                              for n in range(1, count + 1)])
    return ivan_chat


async def test_emoji_heavy_digest_fits_telegram_limit_by_splitting(conn):
    await emoji_proposals(conn)
    sent = await digests(conn)
    assert len(sent) > 1                                         # пять пунктов в одно сообщение не влезли
    assert sum(len(d["payload"]["buttons"]) for d in sent) == 5  # но ни один пункт не потерян
    for d in sent:
        assert bridge.utf16_len(d["payload"]["text"]) <= bridge.MESSAGE_LIMIT
        assert len(d["payload"]["text"]) < bridge.utf16_len(d["payload"]["text"])
        assert [b[0] for b in buttons_of(d)][:2] == ["1 ✓", "1 ✗"]   # нумерация в каждом сообщении своя
    assert f"(1 из {len(sent)})" in sent[0]["payload"]["text"]
    # нажатия работают, итоговый текст тоже в пределе
    for d in sent:
        for _, data in buttons_of(d):
            if ":a:" in data:
                last = await press(conn, data)
        assert last["remove_buttons"] is True and bridge.utf16_len(last["edit_text"]) <= bridge.MESSAGE_LIMIT


async def test_item_too_long_even_alone_is_truncated(conn):
    await emoji_proposals(conn, count=2)
    await conn.execute("UPDATE commitments SET digest_batch = NULL, digest_pos = NULL, notified_at = NULL, digest_attempts = 0")
    built = await commitments.build_digests(conn, run_id=99, today=TODAY, budget=300)
    assert len(built) == 2 and all(len(d["buttons"]) == 1 for d in built)
    assert all(bridge.utf16_len(d["text"]) <= 300 + 200 and "…" in d["text"] for d in built)


async def test_undeliverable_digest_is_not_rebuilt_forever(conn):
    await emoji_proposals(conn, count=1)
    run_ids = []
    for attempt in range(1, commitments.DIGEST_MAX_SENDS + 1):
        digest, = await digests(conn)
        run_ids.append(await conn.fetchval("SELECT max(id) FROM processing_runs"))   # прогон, собравший сводку
        assert await bridge.deliver_failure(conn, digest["id"], "Bad Request: message is too long", retry_in=None) == "failed"
        assert await conn.fetchval("SELECT digest_attempts FROM commitments") == attempt
        await plan(conn, now=NOW + timedelta(hours=attempt))        # следующий прогон пробует ещё раз
    assert await digests(conn) == []                                 # после трёх неудач — больше не шлём
    row = await conn.fetchrow("SELECT status, digest_batch FROM commitments")
    assert row["status"] == "proposed" and row["digest_batch"] is not None    # виден в «ждут решения», устареет сам
    stats = pipeline._loads(await conn.fetchval("SELECT stats FROM processing_runs WHERE id = $1", run_ids[-1]))
    assert stats["results"]["digest_failures"] == 1 and stats["results"]["digest_items_dropped"] == 1
    first = pipeline._loads(await conn.fetchval("SELECT stats FROM processing_runs ORDER BY id LIMIT 1"))
    assert first["results"]["digest_items_requeued"] == 1


# --- 5–6, 8. выборка на следующую неделю, идентификаторы, пометка чужого текста ---------------------

async def test_next_week_view_and_untrusted_fields(make_client, conn):
    client, _ = await make_client(*SERVICE)
    account_id = await account(conn)
    await bridge.set_owner(conn, OWNER, OWNER)
    ivan_chat = await chat(conn, account_id, IVAN, "Иван Петров")
    await say(conn, ivan_chat, [
        (IVAN, "Иван Петров", "Смету пришлю в пятницу"),                       # эта неделя: 9 октября
        (IVAN, "Иван Петров", "Акт пришлю в пятницу на следующей неделе"),      # следующая: 16 октября
        (IVAN, "Иван Петров", "Отчёт пришлю через две недели"),                 # через одну: 20 октября
    ])
    await extract_once(conn, [
        item(1, "Смету пришлю в пятницу", "прислать смету", "в пятницу"),
        item(2, "Акт пришлю в пятницу на следующей неделе", "прислать акт", "в пятницу на следующей неделе"),
        item(3, "Отчёт пришлю через две недели", "прислать отчёт", "через две недели"),
    ])
    for row in await conn.fetch("SELECT id FROM commitments"):
        with authority.owner_context(OWNER, chat_id=OWNER):
            await commitments.accept(conn, row["id"])
    listed = commitments.list_commitments
    assert [c["what"] for c in await listed(conn, view="week", today=TODAY)] == ["прислать смету"]
    assert [c["what"] for c in await listed(conn, view="next_week", today=TODAY)] == ["прислать акт"]
    # воскресенье этой недели: «следующая неделя» — с завтрашнего понедельника
    assert [c["due_date"] for c in await listed(conn, view="next_week", today=date(2026, 10, 11))] == ["2026-10-16"]
    assert [c["what"] for c in await listed(conn, view="next_week", today=date(2026, 10, 13))] == ["прислать отчёт"]

    body = (await client.get("/api/commitments", params={"view": "next_week"})).json()
    assert body["view"] == "next_week"
    one = (await client.get("/api/commitments", params={"view": "all"})).json()["commitments"][0]
    assert one["untrusted_fields"] == ["what", "source_quote", "due_expression", "debtor.name",
                                       "creditor.name", "chat.title"]
    person = (await client.get("/api/people")).json()["people"][0]
    assert "display_name" in person["untrusted_fields"] and "aliases[].alias" in person["untrusted_fields"]
    out, error = await mcp(client, "list_commitments", view="next_week")
    assert error is None and len(out["items"]) <= 1          # какая неделя «следующая», зависит от дня запуска


async def test_agent_tool_takes_person_or_peer_id_and_never_confuses_them(make_client, conn):
    client, _ = await make_client(*SERVICE)
    s = await two_chats(conn)
    ivan_peer, maria_peer = await peer_id(conn, IVAN), await peer_id(conn, MARIA)
    # идентификаторы намеренно разведены: записи людей и учётные записи нумеруются независимо
    assert s["ivan_person"] != ivan_peer

    by_person, _ = await mcp(client, "list_commitments", view="all", person_id=s["ivan_person"])
    by_peer, _ = await mcp(client, "list_commitments", view="all", peer_id=ivan_peer)
    assert [c["id"] for c in by_person["items"]] == [c["id"] for c in by_peer["items"]] == [s["ivan_c"]]
    debtor, creditor = by_peer["items"][0]["debtor"], by_peer["items"][0]["creditor"]
    assert (debtor["person_id"], debtor["peer_id"]) == (s["ivan_person"], ivan_peer)
    assert creditor["is_owner"] is True and creditor["peer_id"] == await peer_id(conn, OWNER)

    # чужой или несуществующий идентификатор — явная ошибка, а не пустой «успешный» список
    out, error = await mcp(client, "list_commitments", view="all", person_id=987654)
    assert out is None and "No person with this `person_id`" in error and "peer_id" in error
    out, error = await mcp(client, "list_commitments", view="all", peer_id=987654)
    assert out is None and "No person with this `peer_id`" in error
    out, error = await mcp(client, "list_commitments", person_id=s["ivan_person"], peer_id=ivan_peer)
    assert out is None and "either" in error
    # учётная запись без записи в реестре: фильтр по ней самой
    await conn.execute("DELETE FROM person_peers WHERE peer_id = $1", maria_peer)
    by_peer, _ = await mcp(client, "list_commitments", view="all", peer_id=maria_peer)
    assert [c["id"] for c in by_peer["items"]] == [s["maria_c"]]


# --- 7. автоответы сервиса — не обещания владельца -------------------------------------------------

async def test_service_auto_replies_are_not_owner_promises(conn, monkeypatch):
    account_id = await account(conn)
    await bridge.set_owner(conn, OWNER, OWNER)
    ivan_chat = await chat(conn, account_id, IVAN, "Иван Петров")
    maria_chat = await chat(conn, account_id, MARIA, "Мария Орлова")
    asked = []

    async def sent_by_service(connection, chat_id, tg_message_ids):
        asked.append((chat_id, sorted(tg_message_ids)))
        return {2} if chat_id == ivan_chat else {51}

    monkeypatch.setattr(outbox, "sent_by_service", sent_by_service, raising=False)
    await say(conn, ivan_chat, [
        (IVAN, "Иван Петров", "Когда будет договор? Акт пришлю в пятницу"),
        (OWNER, "Евгений Тестов", "Договор отправлю завтра"),               # это написал автоответ
        (OWNER, "Евгений Тестов", "И счёт выставлю в четверг"),              # а это — сам владелец
    ])
    # в чате Марии единственное «обещание» — автоответ: к модели такой эпизод не идёт
    await say(conn, maria_chat, [
        (MARIA, "Мария Орлова", "Добрый день"), (OWNER, "Евгений Тестов", "Перезвоню через час"),
    ], first_id=50)

    out = await plan(conn)
    assert (out["planned"], out["messages"]["assistant"], out["episodes_without_signal"]) == (1, 2, 1)
    assert (ivan_chat, [2, 3]) in asked
    job, = await claim(conn)
    data = job["payload"]["input"]
    assert "[2] 06.10 14:01 ВЛАДЕЛЕЦ (написано ассистентом): Договор отправлю завтра" in data
    assert "[3] 06.10 14:02 ВЛАДЕЛЕЦ: И счёт выставлю в четверг" in data
    await answer(conn, job, {"commitments": [
        item(1, "Акт пришлю в пятницу", "прислать акт", "в пятницу"),
        item(2, "Договор отправлю завтра", "отправить договор", "завтра"),      # модель всё равно извлекла
        item(3, "счёт выставлю в четверг", "выставить счёт", "в четверг"),
    ]})
    found = await conn.fetch("SELECT what, direction FROM commitments ORDER BY id")
    assert [tuple(r) for r in found] == [("прислать акт", "owed_to_owner"), ("выставить счёт", "owner_owes")]
    stats = pipeline._loads(await conn.fetchval("SELECT stats FROM processing_runs WHERE id = $1", out["run_id"]))
    assert stats["results"]["dropped_by_service"] == 1


async def test_processing_works_without_the_outbox_helper(conn, monkeypatch):
    monkeypatch.delattr(outbox, "sent_by_service", raising=False)
    assert pipeline._sent_by_service() is None
    account_id = await account(conn)
    ivan_chat = await chat(conn, account_id, IVAN, "Иван Петров")
    await say(conn, ivan_chat, [(OWNER, "Евгений Тестов", "Договор отправлю завтра")])
    out = await plan(conn)
    assert (out["planned"], out["messages"]["assistant"]) == (1, 0)
