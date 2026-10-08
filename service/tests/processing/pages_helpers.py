"""Заготовки тестов страниц памяти: переписка, обязательства, подставной ответ модели, чтение файла."""

import subprocess
from datetime import timedelta

from shturman import authority, bridge
from shturman.processing import commitments, pages, pages_build, people

from proc_helpers import OWNER, T0, TZ, account, answer, chat, claim, peer_id, say

IVAN, MARIA, PETR = 2001, 2002, 2003
NOW = T0 + timedelta(days=1)


class World:
    """Что посеяно: чаты, сообщения, люди."""


async def seed(conn, *, confirm=True):
    """Владелец, личные чаты с Иваном и Марией, по нескольку сообщений. Иван подтверждён."""
    w = World()
    w.account = await account(conn)
    await bridge.set_owner(conn, OWNER, OWNER)
    w.owner_peer = await peer_id(conn, OWNER)
    w.ivan_chat = await chat(conn, w.account, IVAN, "Иван Петров")
    w.maria_chat = await chat(conn, w.account, MARIA, "Мария Сидорова")
    w.ivan_msgs = await say(conn, w.ivan_chat, [
        (IVAN, "Иван Петров", "Добрый день! Пришлю смету по фасадам к пятнице."),
        (OWNER, "Евгений Тестов", "Хорошо, жду. Договор отправлю завтра."),
        (IVAN, "Иван Петров", "Монтаж начнём в октябре, бригада уже на объекте."),
    ])
    w.maria_msgs = await say(conn, w.maria_chat, [
        (MARIA, "Мария Сидорова", "Акт сверки подпишу в понедельник."),
        (OWNER, "Евгений Тестов", "Спасибо, Мария."),
    ])
    w.ivan_peer = await peer_id(conn, IVAN)
    w.maria_peer = await peer_id(conn, MARIA)
    w.ivan = await people.ensure_person_for_peer(conn, w.ivan_peer)
    w.maria = await people.ensure_person_for_peer(conn, w.maria_peer)
    if confirm:
        await people.confirm_person(conn, w.ivan)
    return w


async def commitment(conn, chat_id, source_id, *, debtor, creditor, direction, what, accept=True,
                     due_expression=None, due_date=None, due_message_id=None, quote="цитата"):
    """Обязательство как после разбора ответа модели; accept=True — владелец его принял."""
    commitment_id = await conn.fetchval(
        """INSERT INTO commitments (chat_id, source_message_id, due_message_id, debtor_peer_id, creditor_peer_id,
                                    direction, what, source_quote, due_expression, due_date, due_reason)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) RETURNING id""",
        chat_id, source_id, due_message_id, debtor, creditor, direction, what, quote, due_expression, due_date,
        "ok" if due_date else "no_deadline")
    if accept:
        with authority.owner_context(OWNER, chat_id=OWNER, action="fixture.accept"):
            assert (await commitments.accept(conn, commitment_id))["ok"]
    return commitment_id


async def ivan_owes_estimate(conn, w, **extra):
    from datetime import date
    return await commitment(
        conn, w.ivan_chat, w.ivan_msgs[0], debtor=w.ivan_peer, creditor=w.owner_peer, direction="owed_to_owner",
        what="прислать смету по фасадам", due_expression="к пятнице", due_date=date(2026, 10, 9), **extra)


async def build(conn, config, **kw):
    return await pages_build.build(conn, config.pages_dir, tz=TZ, now=kw.pop("now", NOW), **kw)


async def finish(conn, config, **kw):
    return await pages_build.finish_build(conn, config.pages_dir, tz=TZ, now=kw.pop("now", NOW), **kw)


def statement(text, sources, origin="other", contradiction=False):
    return {"text": text, "sources": list(sources), "origin": origin, "contradiction": contradiction}


async def build_with(conn, config, answers=None, **kw):
    """Сборка от начала до конца: на каждый запрос сводки отвечает `answers(задание)` (список
    утверждений, None — «модель не ответила по схеме») и дописывает файлы. Возвращает
    (итог плана, итог записи, задания)."""
    now = kw.get("now", NOW)
    plan = await build(conn, config, **kw)
    jobs = await claim(conn)
    for job in jobs:
        parsed = answers(job) if answers else {"statements": []}
        if isinstance(parsed, list):
            parsed = {"statements": parsed}
        assert await answer(conn, job, parsed)
    done = await finish(conn, config, now=now) if plan["status"] == "planned" else plan
    return plan, done, jobs


async def page_row(conn, person_id):
    return await conn.fetchrow("SELECT * FROM pages WHERE person_id = $1", person_id)


async def path_of(conn, config, person_id):
    row = await page_row(conn, person_id)
    return config.pages_dir / row["path"]


def blocks_of(text):
    return pages.parse(text)


def owner_bytes(data: bytes) -> bytes:
    """Блок владельца так, как его видит человек: от метки owner до последней метки commitments."""
    start = data.index(pages.MARKERS["owner"].encode()) + len(pages.MARKERS["owner"].encode()) + 1
    end = data.rindex(pages.MARKERS["commitments"].encode())
    return data[start:end]


def git(config, *args):
    done = subprocess.run(["git", "-C", str(config.pages_dir), "-c", "core.quotepath=false", *args], capture_output=True, text=True, check=True)
    return done.stdout


def log(config):
    """[(автор, заголовок)] от новых к старым."""
    out = git(config, "log", "--format=%an|%s")
    return [tuple(line.split("|", 1)) for line in out.splitlines()]

