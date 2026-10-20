"""Качество договорённостей (Р-96): состояние на момент показа, проверка закрытия, быстрые обещания,
«В команде», голое согласие, не обязательства, дубли, перепроверка, измерение, «Не так» с причиной.
Модель заменена подставными ответами; примеры синтетические."""

from datetime import date, datetime, timedelta, timezone

from shturman import authority, bridge
from shturman.processing import commitment_stats, commitments, extract, pipeline

from proc_helpers import OWNER, T0, TZ, account, answer, buttons_of, chat, claim, peer_id, press, say

IVAN, PETR, MARIA = 2001, 2005, 2002
TODAY = date(2026, 10, 6)


async def scene(conn):
    account_id = await account(conn)
    await bridge.set_owner(conn, OWNER, OWNER)
    return account_id, await chat(conn, account_id, IVAN, "Иван Петров")


def item(message, quote, what, due=None, **extra):
    return {"message": message, "source_quote": quote, "what": what, "due_expression": due, "due_message": None,
            "recipient": extra.pop("recipient", None), "duplicate_of": None, **extra}


async def rows(conn):
    return [dict(r) for r in await conn.fetch("SELECT * FROM commitments ORDER BY id")]


async def plan(conn, now, **kw):
    return await pipeline.plan_run(conn, tz=TZ, now=now, **kw)


async def run_once(conn, now, answer_for, **kw):
    """План -> ответы модели по заданиям: answer_for(job) -> parsed (или None — пропустить)."""
    out = await plan(conn, now, **kw)
    for job in await claim(conn):
        parsed = answer_for(job)
        if parsed is not None:
            await answer(conn, job, parsed)
    return out


def extract_only(items):
    return lambda job: {"commitments": items} if job["payload"]["schema_name"] == "commitments" else {"updates": []}


async def digest_jobs(conn):
    return sorted(await claim(conn, bridge.NOTIFY_OWNER), key=lambda job: job["id"])


async def events(conn, commitment_id):
    return [(r["actor"], r["action"], r["to_status"], pipeline._loads(r["details"])) for r in await conn.fetch(
        "SELECT * FROM commitment_events WHERE commitment_id = $1 ORDER BY at, id", commitment_id)]


# --- 1. состояние на момент показа: блок «После» ---------------------------------------------------------

async def test_after_block_lets_model_say_done_and_done_is_not_proposed(conn):
    _, ivan = await scene(conn)
    ids = await say(conn, ivan, [
        (IVAN, "Иван Петров", "Сегодня пополню баланс на сервере"),
        (IVAN, "Иван Петров", "Баланс пополнил, проверь", {"at": T0 + timedelta(hours=2)}),
        (OWNER, "Евгений Тестов", "Спасибо!", {"at": T0 + timedelta(hours=2, minutes=1)}),
    ])
    await conn.execute("UPDATE messages SET agent_visible = false WHERE id = $1", ids[2])   # скрыто защитой
    out = await run_once(conn, T0 + timedelta(hours=5), extract_only([item(
        1, "Сегодня пополню баланс на сервере", "пополнить баланс на сервере", "Сегодня", recipient="ВЛАДЕЛЕЦ",
        status="fulfilled", status_message="П1", status_quote="Баланс пополнил")]))
    assert out["planned"] == 1
    job = await conn.fetchrow("SELECT * FROM processing_requests")
    assert list(job["message_ids"]) == [ids[0]]           # эпизод — само обещание; «После» — отдельно
    row, = await rows(conn)
    assert (row["status"], row["quiet_reason"]) == ("expired", "done_before_shown")
    assert (row["checked_until_id"], row["prompt_version"]) == (ids[1], extract.PROMPT_VERSION)
    log = await events(conn, row["id"])
    assert [(a, b, c) for a, b, c, _ in log] == [("model", "proposed", "proposed"), ("model", "quiet_closed", "expired")]
    assert log[-1][3] == {"reason": "done_before_shown", "evidence_message_id": ids[1]}
    stats = pipeline._loads(await conn.fetchval("SELECT stats FROM processing_runs"))["results"]
    assert stats["quiet_done_before_shown"] == 1 and "proposed" not in stats
    assert await digest_jobs(conn) == []                 # владельцу не предлагается
    assert await commitments.list_commitments(conn, view="proposed", today=TODAY) == []


async def test_after_block_is_in_the_request_without_hidden_and_bounded(conn):
    _, ivan = await scene(conn)
    ids = await say(conn, ivan, [
        (IVAN, "Иван Петров", "Сейчас поправлю и напишу"),
        (IVAN, "Иван Петров", "Поправил", {"at": T0 + timedelta(minutes=50)}),
        (IVAN, "Иван Петров", "секрет", {"at": T0 + timedelta(minutes=52)}),
        (IVAN, "Иван Петров", "через неделю", {"at": T0 + timedelta(days=8)}),
    ])
    await conn.execute("UPDATE messages SET agent_visible = false WHERE id = $1", ids[2])
    await plan(conn, T0 + timedelta(days=9))
    job = next(j for j in await claim(conn) if j["payload"]["schema_name"] == "commitments")
    text = job["payload"]["input"]
    assert "После (что было в этом чате дальше" in text and "[П1] 06.10 14:50 У1: Поправил" in text
    assert "секрет" not in text and "через неделю" not in text     # скрытое и позже 7 дней — нет
    assert job["payload"]["instructions"] == extract.EXTRACT_INSTRUCTIONS


async def test_evidence_must_be_later_than_the_promise(conn):
    """Мутационная проверка: основание «сделано» — только сообщение ПОЗЖЕ обещания."""
    _, ivan = await scene(conn)
    await say(conn, ivan, [
        (IVAN, "Иван Петров", "Смету уже отправлял на прошлой неделе"),
        (IVAN, "Иван Петров", "Договор пришлю завтра", {"at": T0 + timedelta(minutes=5)}),
    ])
    await run_once(conn, T0 + timedelta(hours=3), extract_only([
        # основание — сообщение раньше обещания
        item(2, "Договор пришлю завтра", "прислать договор", "завтра", recipient="ВЛАДЕЛЕЦ",
             status="fulfilled", status_message=1, status_quote="Смету уже отправлял"),
    ]))
    row, = await rows(conn)
    assert (row["status"], row["quiet_reason"]) == ("proposed", None)
    stats = pipeline._loads(await conn.fetchval("SELECT stats FROM processing_runs"))["results"]
    assert stats["dropped_ungrounded_status"] == 1


def test_evidence_checks_in_validation():
    def m(mid, text, minutes=0, out=False):
        return extract.Msg(id=mid, chat_id=1, sent_at=T0 + timedelta(minutes=minutes), sender_peer_id=None if out else 10,
                           sender_name="Иван", is_outgoing=out, text=text)
    episode = extract.Episode(1, [m(1, "Сейчас пришлю ссылку"), m(2, "вот https://example.org/a", 1)],
                              after=[m(3, "Спасибо, получил", 30, out=True)])
    labels = extract.speaker_labels(episode)

    def one(**extra):
        accepted, dropped = extract.validate_extraction(
            {"commitments": [item(1, "Сейчас пришлю ссылку", "прислать ссылку", "Сейчас", **extra)]}, episode, labels)
        return accepted[0], dropped

    assert one(status="fulfilled", status_message=2, status_quote="вот https://example.org/a")[0].status == "fulfilled"
    assert one(status="fulfilled", status_message="П1", status_quote="получил")[0].evidence.id == 3
    for bad in ({"status_message": 1, "status_quote": "Сейчас пришлю"},       # само обещание
                {"status_message": "П1", "status_quote": "не получил"},       # цитаты нет
                {"status_message": "П9", "status_quote": "получил"},          # нет такого
                {"status_message": None, "status_quote": None}):
        got, dropped = one(status="fulfilled", **bad)
        assert (got.status, got.evidence, dropped["ungrounded_status"]) == ("open", None, 1), bad
    moved, _ = one(status="rescheduled", status_message="П1", status_quote="получил", new_due_expression="получил")
    assert moved.status == "open" and moved.due_message.id == 3


async def test_digest_card_shows_promise_date(conn):
    _, ivan = await scene(conn)
    await say(conn, ivan, [(IVAN, "Иван Петров", "Пришлю смету к пятнице")], start=T0 - timedelta(days=13))
    await run_once(conn, T0, extract_only([item(1, "Пришлю смету к пятнице", "прислать смету", "к пятнице",
                                                recipient="ВЛАДЕЛЕЦ")]))
    digest, = await digest_jobs(conn)
    assert "Срок: пт, 25 сентября («к пятнице») · обещано 23.09" in digest["payload"]["text"]


# --- 2. проверка закрытия: и предложенное, и принятое; с момента обещания ---------------------------------

async def test_resolve_quietly_drops_proposed_but_only_proposes_change_for_open(conn):
    """Мутационная проверка: тихое закрытие не трогает принятое (open)."""
    _, ivan = await scene(conn)
    await say(conn, ivan, [(IVAN, "Иван Петров", "Смету пришлю к пятнице"),
                           (IVAN, "Иван Петров", "Договор пришлю завтра", {"at": T0 + timedelta(minutes=1)})])
    await run_once(conn, T0 + timedelta(hours=1), extract_only([
        item(1, "Смету пришлю к пятнице", "прислать смету", "к пятнице", recipient="ВЛАДЕЛЕЦ"),
        item(2, "Договор пришлю завтра", "прислать договор", "завтра", recipient="ВЛАДЕЛЕЦ")]))
    smeta, dogovor = await rows(conn)
    with authority.owner_context(OWNER, chat_id=OWNER):
        await commitments.accept(conn, smeta["id"])
    later = await say(conn, ivan, [(IVAN, "Иван Петров", "Смету и договор отправил")],
                      start=T0 + timedelta(days=1), first_id=10)
    out = await plan(conn, T0 + timedelta(days=1, hours=1))
    assert out["resolve_planned"] == 1
    job, = await claim(conn)
    assert "1. У1: прислать смету" in job["payload"]["input"] and "2. У1: прислать договор" in job["payload"]["input"]
    await answer(conn, job, {"updates": [
        {"commitment": 1, "status": "fulfilled", "message": 1, "quote": "Смету и договор отправил", "new_due_expression": None},
        {"commitment": 2, "status": "fulfilled", "message": 1, "quote": "Смету и договор отправил", "new_due_expression": None}]})
    smeta, dogovor = await rows(conn)
    assert smeta["status"] == "open"                                      # принятое — только нажатием владельца
    change = await conn.fetchrow("SELECT * FROM commitment_changes")
    assert change is not None, "принятому — предложение изменения владельцу"
    assert (change["commitment_id"], change["kind"], change["evidence_message_id"]) == (smeta["id"], "fulfilled", later[0])
    assert (dogovor["status"], dogovor["quiet_reason"]) == ("expired", "done_before_decision")
    assert {smeta["checked_until_id"], dogovor["checked_until_id"]} == {later[0]}
    # следующий прогон без новых сообщений проверять нечего
    assert (await plan(conn, T0 + timedelta(days=1, hours=2)))["resolve_planned"] == 0


async def test_first_check_starts_at_the_promise_not_at_this_runs_messages(conn):
    """Принятое прежней версией (без отметки проверки): первая проверка — с момента обещания."""
    _, ivan = await scene(conn)
    ids = await say(conn, ivan, [(IVAN, "Иван Петров", "Ссылку скину сегодня"),
                                 (IVAN, "Иван Петров", "Держите ссылку https://example.org/x",
                                  {"at": T0 + timedelta(hours=1)})])
    await run_once(conn, T0 + timedelta(minutes=10), extract_only([
        item(1, "Ссылку скину сегодня", "скинуть ссылку", "сегодня", recipient="ВЛАДЕЛЕЦ")]))
    await conn.execute("UPDATE commitments SET checked_until_at = NULL, checked_until_id = NULL, status = 'open'")
    await conn.execute("UPDATE settings SET value = jsonb_set(value, '{watermark}', to_jsonb($1::bigint)) "
                       "WHERE key = 'processing.state'", max(ids))
    out = await plan(conn, T0 + timedelta(days=2))
    assert out["resolve_planned"] == 1 and out["planned"] == 0
    job, = await claim(conn)
    assert "Держите ссылку" in job["payload"]["input"]                   # сообщение до этого прогона


# --- 3. быстрые обещания: показ — только если не сделано за сутки ------------------------------------------

async def test_immediate_promise_waits_a_day_and_is_shown_only_if_not_done(conn):
    _, ivan = await scene(conn)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = now - timedelta(hours=2)
    await say(conn, ivan, [(IVAN, "Иван Петров", "Ща скину отчёт"), (IVAN, "Иван Петров", "Сейчас подойду",
                                                                       {"at": start + timedelta(minutes=30)})],
              start=start)
    await run_once(conn, now, extract_only([
        item(1, "Ща скину отчёт", "скинуть отчёт", "Ща", recipient="ВЛАДЕЛЕЦ"),
        item(2, "Сейчас подойду", "подойти", "Сейчас", recipient="ВЛАДЕЛЕЦ")]))
    otchet, podoyti = await rows(conn)
    assert (otchet["due_reason"], otchet["due_date"]) == ("immediate", start.astimezone(pipeline.ZoneInfo(TZ)).date())
    assert otchet["hold_until"] == start + timedelta(hours=24)
    assert await digest_jobs(conn) == []
    assert await commitments.list_commitments(conn, view="proposed", today=TODAY) == []
    stats = pipeline._loads(await conn.fetchval("SELECT stats FROM processing_runs"))["results"]
    assert stats["held_immediate"] == 2
    # одно сделано — проверка закрытия снимает тихо
    done = await say(conn, ivan, [(IVAN, "Иван Петров", "Отчёт скинул")], start=now - timedelta(minutes=5), first_id=10)
    await plan(conn, now + timedelta(minutes=1))
    job, = await claim(conn)
    await answer(conn, job, {"updates": [{"commitment": 1, "status": "fulfilled", "message": 1, "quote": "Отчёт скинул",
                                          "new_due_expression": None}]})
    otchet = (await rows(conn))[0]
    assert (otchet["status"], otchet["quiet_reason"]) == ("expired", "done_before_decision")
    assert (await events(conn, otchet["id"]))[-1][3]["evidence_message_id"] == done[0]
    # сутки прошли, второе не сделано — уходит со сводкой как «похоже, так и не сделано»
    await conn.execute("UPDATE commitments SET hold_until = now() - interval '1 minute' WHERE id = $1", podoyti["id"])
    out = await commitments.build_digests(conn, run_id=99, today=TODAY, tz=TZ)
    assert len(out) == 1 and "Быстрое обещание («Сейчас») · обещано" in out[0]["text"]
    assert "похоже, так и не сделано" in out[0]["text"]


async def test_old_immediate_promise_is_shown_at_once_when_not_done(conn):
    _, ivan = await scene(conn)
    await say(conn, ivan, [(IVAN, "Иван Петров", "Сейчас гляну счёт")], start=T0 - timedelta(days=3))
    await say(conn, ivan, [(OWNER, "Евгений Тестов", "Глянь счёт на оплату")], start=T0 - timedelta(days=3, minutes=1),
              first_id=5)
    await run_once(conn, T0, extract_only([item(2, "Сейчас гляну счёт", "глянуть счёт", "Сейчас",
                                                request_message=1, request_quote="Глянь счёт на оплату")]))
    row, = await rows(conn)
    assert (row["status"], row["hold_until"], row["direction"]) == ("proposed", None, "owed_to_owner")


# --- 4. «В команде» ------------------------------------------------------------------------------------------

async def group_scene(conn):
    account_id, _ = await scene(conn)
    group = await chat(conn, account_id, 3001, "Стройка", type_="private_group", cls="chat")
    return group


async def test_others_go_to_team_not_to_digest_and_owner_involvement_is_not_others(conn):
    group = await group_scene(conn)
    ids = await say(conn, group, [
        (OWNER, "Евгений Тестов", "Коллеги, кто привезёт образцы?"),
        (IVAN, "Иван Петров", "К обеду привезу пироги"),
        (PETR, "Пётр Петренко", "Образцы привезу завтра"),                  # ответ на сообщение владельца
        (MARIA, "Мария Сидорова", "Евгению скину смету вечером"),            # владелец назван по имени
        (PETR, "Пётр Петренко", "Схожу привиться в пятницу"),
    ])
    await conn.execute("UPDATE messages SET reply_to_tg_id = 1 WHERE id = $1", ids[2])
    await run_once(conn, T0 + timedelta(hours=3), extract_only([
        item(2, "К обеду привезу пироги", "привезти пироги", "К обеду"),
        item(3, "Образцы привезу завтра", "привезти образцы", "завтра"),
        item(4, "Евгению скину смету вечером", "скинуть смету", "вечером"),
        item(5, "Схожу привиться в пятницу", "сходить привиться", "в пятницу"),
    ]))
    got = [(r["what"], r["direction"], r["status"]) for r in await rows(conn)]
    assert got == [("привезти пироги", "others", "team"), ("привезти образцы", "owed_to_owner", "proposed"),
                   ("скинуть смету", "owed_to_owner", "proposed"), ("сходить привиться", "others", "team")]
    digest, = await digest_jobs(conn)
    assert "пироги" not in digest["payload"]["text"] and "привиться" not in digest["payload"]["text"]
    assert len(digest["payload"]["buttons"]) == 2
    from shturman.processing import agenda
    board = await agenda.board(conn, today=TODAY, tz=TZ)
    assert [i["what"] for i in board["team"]] == ["сходить привиться", "привезти пироги"]   # свежие сверху
    assert board["team"][0]["who"] == "Пётр Петренко" and board["team"][0]["promised"] == "2026-10-06"
    assert board["waiting"] == 2
    # «Скрыть» — только владелец; скрытое не возвращается тем же сообщением
    team_id = board["team"][1]["id"]
    assert (await commitments.hide(conn, team_id)).get("code") == "approval_required"
    with authority.owner_context(OWNER, chat_id=OWNER):
        assert (await commitments.hide(conn, team_id))["ok"]
    assert [i["what"] for i in (await agenda.board(conn, today=TODAY, tz=TZ))["team"]] == ["сходить привиться"]


async def test_team_items_expire_after_two_weeks_and_existing_others_moved_by_migration(conn):
    group = await group_scene(conn)
    await say(conn, group, [(IVAN, "Иван Петров", "Привезу пироги")], start=datetime.now(timezone.utc) - timedelta(days=15))
    await run_once(conn, datetime.now(timezone.utc), extract_only([item(1, "Привезу пироги", "привезти пироги")]))
    out = await commitments.expire_stale(conn)
    assert out["team"] == 1
    row, = await rows(conn)
    assert (row["status"], row["quiet_reason"]) == ("expired", "team_stale")


# --- 5–6. голое согласие, не обязательства ---------------------------------------------------------------

def test_not_promises_are_dropped_by_code():
    def m(mid, text):
        return extract.Msg(id=mid, chat_id=1, sent_at=T0 + timedelta(minutes=mid), sender_peer_id=10,
                           sender_name="Иван", is_outgoing=False, text=text)
    cases = ["Запускать всё же будем на новом сервере", "Буду воспринимать это как шеринг информации",
             "Будем мониторить ситуацию", "Будем разбираться", "Буду параллельно править текст",
             "Пусть сделает заявку, и мы дадим доступ", "Если пришлёшь ТЗ, сделаю к пятнице"]
    episode = extract.Episode(1, [m(n, text) for n, text in enumerate(cases, start=1)])
    accepted, dropped = extract.validate_extraction(
        {"commitments": [item(n, text, "сделать дело") for n, text in enumerate(cases, start=1)]},
        episode, extract.speaker_labels(episode))
    assert accepted == [] and dropped["not_a_promise"] == len(cases)
    ok = extract.Episode(1, [m(1, "Пришлю смету завтра"), m(2, "Счёт оплачу сегодня")])
    accepted, _ = extract.validate_extraction(
        {"commitments": [item(1, "Пришлю смету завтра", "прислать смету"), item(2, "Счёт оплачу сегодня", "оплатить счёт")]},
        ok, extract.speaker_labels(ok))
    assert len(accepted) == 2


def test_bare_acknowledgements():
    for text in ("Ок", "Окей", "Принял", "Да, конечно", "Проверю", "Поправим", "Ок:)", "Сейчас гляну"):
        assert extract.is_bare_ack(text), text
    for text in ("Пришлю смету завтра", "Ок, акт подпишу в понедельник", "Скину ссылку", "Сейчас подойду",
                 "Ок, перезвоню"):
        assert not extract.is_bare_ack(text), text

    def m(mid, text, out=False):
        return extract.Msg(id=mid, chat_id=1, sent_at=T0 + timedelta(minutes=mid), sender_peer_id=None if out else 10,
                           sender_name="Иван", is_outgoing=out, text=text)
    episode = extract.Episode(1, [m(1, "Надо заполнить поля в карточках клиентов", out=True), m(2, "Ок")],
                              context=[m(0, "Обязать менеджеров заполнять поля")])
    labels = extract.speaker_labels(episode)
    good = item(2, "Ок", "заполнить поля в карточках", request_message=1,
                request_quote="заполнить поля в карточках клиентов")
    accepted, _ = extract.validate_extraction({"commitments": [good]}, episode, labels)
    assert accepted[0].request.id == 1 and accepted[0].recipient_key == ("owner",)
    for bad in (item(2, "Ок", "обязать менеджеров заполнять поля"),                     # просьбы нет
                item(2, "Ок", "обязать менеджеров", request_message=1, request_quote="заполнить поля"),  # не из просьбы
                item(2, "Ок", "заполнить поля", request_message=1, request_quote="это не цитата")):
        accepted, dropped = extract.validate_extraction({"commitments": [bad]}, episode, labels)
        assert accepted == [] and dropped["bare_ack"] == 1


# --- 8. дубли внутри ответа -------------------------------------------------------------------------------

def test_duplicate_pairs_are_merged_by_model_hint_and_by_code():
    def m(mid, text, minutes, out=False):
        return extract.Msg(id=mid, chat_id=1, sent_at=T0 + timedelta(minutes=minutes), sender_peer_id=None if out else 10,
                           sender_name="Иван", is_outgoing=out, text=text)
    episode = extract.Episode(1, [m(1, "давай минут через 15 созвонимся", 0, out=True), m(2, "окей, сейчас назначу созвон", 1),
                                  m(3, "окей, я гляну цену", 5, out=True), m(4, "утром отрапортую по цене", 6, out=True),
                                  m(5, "сейчас найду файл", 8), m(6, "сейчас скину файл", 9),
                                  m(7, "пришлю акт", 10), m(8, "пришлю смету", 11)])
    labels = extract.speaker_labels(episode)
    parsed = {"commitments": [
        item(1, "давай минут через 15 созвонимся", "созвониться"),
        item(2, "сейчас назначу созвон", "назначить созвон", same_as=1),
        item(3, "я гляну цену", "выяснить цену"), item(4, "утром отрапортую по цене", "сообщить цену"),
        item(5, "сейчас найду файл", "найти файл"), item(6, "сейчас скину файл", "скинуть файл"),
        item(7, "пришлю акт", "прислать акт"), item(8, "пришлю смету", "прислать смету")]}
    accepted, _ = extract.validate_extraction(parsed, episode, labels)
    kept, pairs = extract.merge_duplicates(accepted)
    assert [c.what for c in kept] == ["созвониться", "выяснить цену", "найти файл", "прислать акт", "прислать смету"]
    assert len(pairs) == 3


def test_duplicates_need_one_debtor_or_a_mutual_call():
    """Д-59: склеиваются только обязательства одного должника (кредитор совместим) и взаимный созвон двух
    сторон, где одно — ответ на другое. Отметка модели `same_as` пару не делает и предмета не отменяет."""
    def m(mid, text, minutes, peer=10, name="Иван", out=False):
        return extract.Msg(id=mid, chat_id=1, sent_at=T0 + timedelta(minutes=minutes), sender_peer_id=None if out else peer,
                           sender_name=name, is_outgoing=out, text=text)

    def merged(messages, items, **kw):
        episode = extract.Episode(1, messages)
        accepted, _ = extract.validate_extraction({"commitments": items}, episode, extract.speaker_labels(episode))
        assert len(accepted) == len(items)
        kept, pairs = extract.merge_duplicates(accepted, **kw)
        return [(c.message.sender_name, c.what) for c in kept], len(pairs)

    two = [m(1, "Пришлю свой отчёт", 0), m(2, "Пришлю свой отчёт", 3, peer=11, name="Пётр")]
    same = [item(1, "Пришлю свой отчёт", "прислать свой отчёт"), item(2, "Пришлю свой отчёт", "прислать свой отчёт")]
    assert merged(two, same)[1] == 0                                                     # два должника
    assert merged(two, [same[0], {**same[1], "same_as": 1}])[1] == 0                     # и с отметкой модели
    assert merged([m(1, "пришлю акт", 0), m(2, "пришлю смету", 1)],
                  [item(1, "пришлю акт", "прислать акт"), item(2, "пришлю смету", "прислать смету", same_as=1)])[1] == 0
    assert merged([m(1, "оплачу счёт 15", 0), m(2, "позвоню подрядчику", 100, peer=11, name="Пётр")],
                  [item(1, "оплачу счёт 15", "оплатить счёт 15"),
                   item(2, "позвоню подрядчику", "позвонить подрядчику", same_as=1)])[1] == 0
    # один на один: каждый пришлёт свой отчёт — два обязательства, а не одно «общее»
    kept, n = merged([m(1, "пришлю свой отчёт вечером", 0, out=True, name="Я"), m(2, "и я свой пришлю", 2)],
                     [item(1, "пришлю свой отчёт вечером", "прислать свой отчёт"),
                      item(2, "и я свой пришлю", "прислать свой отчёт", same_as=1)])
    assert n == 0 and len(kept) == 2
    # двое в группе «созвонюсь», ничем не связанные, — два; ответ на просьбу о созвоне — одно
    assert merged([m(1, "созвонюсь сегодня", 0), m(2, "созвонюсь сегодня", 2, peer=11, name="Пётр")],
                  [item(1, "созвонюсь сегодня", "созвониться"), item(2, "созвонюсь сегодня", "созвониться")])[1] == 0
    assert merged([m(1, "давай минут через 15 созвонимся", 0, out=True, name="Я"), m(2, "окей, сейчас назначу созвон", 1)],
                  [item(1, "давай минут через 15 созвонимся", "созвониться"),
                   item(2, "сейчас назначу созвон", "назначить созвон", request_message=1,
                        request_quote="давай минут через 15 созвонимся")])[1] == 1
    # один должник, разные кредиторы (по разбору) — два; тот же кредитор — одно
    msgs = [m(1, "сейчас скину файл", 0), m(2, "сейчас скину файл", 4)]
    files = [item(1, "сейчас скину файл", "скинуть файл"), item(2, "сейчас скину файл", "скинуть файл")]
    assert merged(msgs, files, parties=lambda c: {1: (10, 20), 2: (10, 30)}[c.message.id])[1] == 0
    assert merged(msgs, files, parties=lambda c: {1: (10, 20), 2: (10, None)}[c.message.id])[1] == 1
    # (ревизия) двое «позвоню подрядчику» и отметка модели — два: подсказка разных должников не склеивает
    assert merged([m(1, "позвоню подрядчику", 0), m(2, "позвоню подрядчику", 3, peer=11, name="Пётр")],
                  [item(1, "позвоню подрядчику", "позвонить подрядчику"),
                   item(2, "позвоню подрядчику", "позвонить подрядчику", same_as=1)])[1] == 0
    # ответ другой стороны на предложение во взаимной форме — одно, и без просьбы и без отметки модели
    assert merged([m(1, "давай минут через 15 созвонимся", 0, out=True, name="Я"), m(2, "окей, сейчас назначу созвон", 1)],
                  [item(1, "давай минут через 15 созвонимся", "созвониться"),
                   item(2, "сейчас назначу созвон", "назначить созвон")])[1] == 1
    # …но не ответ со своим, новым предметом и не позже окна
    assert merged([m(1, "давай минут через 15 созвонимся", 0, out=True, name="Я"), m(2, "созвонюсь с подрядчиком", 1)],
                  [item(1, "давай минут через 15 созвонимся", "созвониться"),
                   item(2, "созвонюсь с подрядчиком", "созвониться с подрядчиком")])[1] == 0
    assert merged([m(1, "давай минут через 15 созвонимся", 0, out=True, name="Я"), m(2, "окей, сейчас назначу созвон", 45)],
                  [item(1, "давай минут через 15 созвонимся", "созвониться"),
                   item(2, "сейчас назначу созвон", "назначить созвон")])[1] == 0
    # (вторая ревизия) «давай я позвоню» — не предложение; созвон на встречу — не ответ; свой предмет — не ответ
    assert merged([m(1, "давай я позвоню подрядчику", 0), m(2, "я тоже позвоню подрядчику", 2, peer=11, name="Пётр")],
                  [item(1, "давай я позвоню подрядчику", "позвонить подрядчику"),
                   item(2, "я тоже позвоню подрядчику", "позвонить подрядчику")])[1] == 0
    assert merged([m(1, "завтра встретимся с подрядчиком", 0), m(2, "я позвоню подрядчику", 2, peer=11, name="Пётр")],
                  [item(1, "завтра встретимся с подрядчиком", "встретиться с подрядчиком"),
                   item(2, "я позвоню подрядчику", "позвонить подрядчику")])[1] == 0
    assert merged([m(1, "давай завтра созвонимся", 0, name="Иван"), m(2, "ок, позвоню Ивану утром", 2, peer=11, name="Пётр")],
                  [item(1, "давай завтра созвонимся", "созвониться"),
                   item(2, "позвоню Ивану утром", "позвонить Ивану")])[1] == 1          # назван предложивший
    # один должник, действие не распознано («подготовить»), отметка модели — одно; без отметки — два; числа — всегда
    kp = [m(1, "подготовлю КП", 0), m(2, "отправлю предложение", 20)]
    assert merged(kp, [item(1, "подготовлю КП", "подготовить КП"),
                       item(2, "отправлю предложение", "отправить предложение", same_as=1)])[1] == 1
    assert merged(kp, [item(1, "подготовлю КП", "подготовить КП"), item(2, "отправлю предложение", "отправить предложение")])[1] == 0
    assert merged([m(1, "подготовлю КП 7", 0), m(2, "отправлю предложение 8", 20)],
                  [item(1, "подготовлю КП 7", "подготовить КП 7"),
                   item(2, "отправлю предложение 8", "отправить предложение 8", same_as=1)])[1] == 0
    # неизвестный должник (нет peer) — сравнивается по говорящему, а не как «оба None»
    nameless = [m(1, "сейчас скину файл", 0, peer=None, name="Аня"), m(2, "сейчас скину файл", 4, peer=None, name="Оля")]
    assert merged(nameless, files, parties=lambda c: (None, None))[1] == 0


async def test_two_people_with_the_same_promise_get_two_commitments(conn):
    """Д-59 на всём пути разбора: в группе Иван и Пётр каждый «пришлю свой отчёт», модель отметила второе как
    то же (`same_as`) — в базе два обязательства с разными должниками."""
    group = await group_scene(conn)
    await say(conn, group, [(IVAN, "Иван Петров", "Пришлю свой отчёт к вечеру"),
                            (PETR, "Пётр Петренко", "Пришлю свой отчёт к вечеру")])
    await run_once(conn, T0 + timedelta(hours=3), extract_only([
        item(1, "Пришлю свой отчёт к вечеру", "прислать свой отчёт", "к вечеру"),
        item(2, "Пришлю свой отчёт к вечеру", "прислать свой отчёт", "к вечеру", same_as=1)]))
    got = await rows(conn)
    assert len(got) == 2
    assert len({r["debtor_peer_id"] for r in got}) == 2
    stats = pipeline._loads(await conn.fetchval("SELECT stats FROM processing_runs"))["results"]
    assert not stats.get("duplicates_in_answer")


# --- 10. перепроверка ---------------------------------------------------------------------------------------

async def test_recheck_replaces_old_proposals_and_checks_accepted_from_the_promise(conn):
    _, ivan = await scene(conn)
    ids = await say(conn, ivan, [
        (IVAN, "Иван Петров", "Сегодня добавлю две команды"),
        (IVAN, "Иван Петров", "Сделал две команды /new, /report", {"at": T0 + timedelta(hours=5)}),
        (IVAN, "Иван Петров", "Акт подпишу в понедельник", {"at": T0 + timedelta(days=1)}),
        (IVAN, "Иван Петров", "Акт подписал", {"at": T0 + timedelta(days=4)}),
    ])
    await run_once(conn, T0 + timedelta(days=5), extract_only([
        item(1, "Сегодня добавлю две команды", "добавить две команды", "Сегодня", recipient="ВЛАДЕЛЕЦ")]))
    await claim(conn, bridge.NOTIFY_OWNER)
    old, = await rows(conn)
    # «прежние правила»: без версии и без отметки проверки; ещё одно — принято владельцем
    await conn.execute("UPDATE commitments SET prompt_version = NULL, checked_until_at = NULL, checked_until_id = NULL")
    accepted_id = await conn.fetchval(
        """INSERT INTO commitments (chat_id, source_message_id, direction, what, source_quote, status,
                                    debtor_peer_id, creditor_peer_id)
           VALUES ($1, $2, 'owed_to_owner', 'подписать акт', 'Акт подпишу в понедельник', 'open', $3, $4) RETURNING id""",
        ivan, ids[2], await peer_id(conn, IVAN), await peer_id(conn, OWNER))

    estimate = await pipeline.recheck(conn, tz=TZ, dry_run=True, now=T0 + timedelta(days=6))
    assert (estimate["extract_planned"], estimate["resolve_planned"], estimate["proposals_to_recheck"]) == (1, 1, 1)
    assert estimate["tokens_in_estimate"] > 500 and "run_id" not in estimate
    assert await conn.fetchval("SELECT count(*) FROM jobs WHERE status = 'queued'") == 0     # оценка — без заданий
    assert await conn.fetchval("SELECT count(*) FROM processing_runs WHERE trigger = 'recheck'") == 0

    out = await pipeline.recheck(conn, tz=TZ, now=T0 + timedelta(days=6))
    assert (out["status"], out["requests"]) == ("planned", 2)
    for job in await claim(conn):
        if job["payload"]["schema_name"] == "commitments":
            assert "[П1]" in job["payload"]["input"]
            await answer(conn, job, {"commitments": [item(
                1, "Сегодня добавлю две команды", "добавить две команды", "Сегодня", recipient="ВЛАДЕЛЕЦ",
                status="fulfilled", status_message="П1", status_quote="Сделал две команды")]})
        else:
            text = job["payload"]["input"]
            number = next(int(line.split(".")[0]) for line in text.splitlines() if "подписать акт" in line)
            message = next(int(line[1:].split("]")[0]) for line in text.splitlines() if line.endswith("Акт подписал"))
            await answer(conn, job, {"updates": [{"commitment": number, "status": "fulfilled", "message": message,
                                                  "quote": "Акт подписал", "new_due_expression": None}]})
    statuses = {r["id"]: (r["status"], r["quiet_reason"]) for r in await rows(conn)}
    assert statuses[old["id"]] == ("expired", "recheck")
    assert statuses[accepted_id] == ("open", None)
    new = [k for k in statuses if k not in (old["id"], accepted_id)]
    assert [statuses[k] for k in new] == [("expired", "done_before_shown")]
    change = await conn.fetchrow("SELECT * FROM commitment_changes")
    assert (change["commitment_id"], change["kind"], change["status"]) == (accepted_id, "fulfilled", "proposed")
    # прогон перепроверки сводку сам не шлёт — она уйдёт в час сводки
    assert await conn.fetchval("SELECT status FROM processing_runs WHERE trigger = 'recheck'") == "done"
    assert await digest_jobs(conn) == []
    assert (await pipeline.recheck(conn, tz=TZ))["status"] == "already_done"
    sent = await pipeline.send_digests(conn, label="s1", tz=TZ)
    assert sent["digest_messages"] == 1
    digest, = await digest_jobs(conn)
    assert "Похоже, выполнено: Иван Петров → вам — подписать акт\n«Акт подписал»" in digest["payload"]["text"]


# --- 11. измерение ---------------------------------------------------------------------------------------

async def test_stats_are_numbers_only(conn):
    group = await group_scene(conn)
    await say(conn, group, [(IVAN, "Иван Петров", "Привезу пироги"), (IVAN, "Иван Петров", "Смету пришлю Евгению завтра"),
                            (IVAN, "Иван Петров", "Сейчас подойду")])
    await run_once(conn, T0 + timedelta(hours=1), extract_only([
        item(1, "Привезу пироги", "привезти пироги"), item(2, "Смету пришлю Евгению завтра", "прислать смету", "завтра"),
        item(3, "Сейчас подойду", "подойти", "Сейчас", recipient="ВЛАДЕЛЕЦ")]))
    stats = await commitment_stats.collect(conn)
    assert stats["total"] == 3 and stats["by_status"] == {"team": 1, "proposed": 2}
    assert stats["others_share"] == round(1 / 3, 3) and stats["immediate_share"] == round(1 / 3, 3)
    assert stats["by_version"] == {extract.PROMPT_VERSION: {"team": 1, "proposed": 2}}
    text = commitment_stats.render(stats)
    assert "Договорённостей всего: 3" in text and "пирог" not in text and "смет" not in text
    fields = await commitment_stats.status_fields(conn)
    assert fields == {"commitments_proposed": 2, "commitments_open": 0, "commitments_team": 1, "commitments_quiet": 0,
                      "commitments_rejected": 0}


# --- 12. «Не так» с причиной ---------------------------------------------------------------------------------

async def test_wrong_with_reason_from_the_app_list(conn):
    _, ivan = await scene(conn)
    await say(conn, ivan, [(IVAN, "Иван Петров", "Смету пришлю завтра")])
    await run_once(conn, T0 + timedelta(hours=1), extract_only([item(1, "Смету пришлю завтра", "прислать смету", "завтра",
                                                                     recipient="ВЛАДЕЛЕЦ")]))
    row, = await rows(conn)
    found = await commitments.awaiting_owner(conn, today=TODAY, tz=TZ)
    entry, = found["items"]
    assert [b["data"] for b in entry["why"]][0] == f"sh:cm:rw:{row['id']}:not_promise"
    assert (await press(conn, f"sh:cm:rw:{row['id']}:already_done"))["answer"] == "Отклонено: уже сделано."
    assert (await rows(conn))[0]["status"] == "rejected"
    assert (await events(conn, row["id"]))[-1][3] == {"reason": "already_done"}
    assert (await press(conn, f"sh:cm:rw:{row['id']}:bogus"))["answer"].startswith("Уже решено")
    stats = await commitment_stats.collect(conn)
    assert stats["wrong_reasons"] == {"already_done": 1}
