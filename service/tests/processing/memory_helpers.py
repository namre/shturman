"""Заготовки тестов памяти этапа 2: факты, проекты, профиль владельца."""

from datetime import timedelta

from shturman import authority, bridge
from shturman.processing import pipeline

from proc_helpers import OWNER, T0, TZ, answer, buttons_of, claim, press

NOW = T0 + timedelta(hours=1)


def fact(message, quote, about, text, *, slot=None, kind="fact", project=None):
    return {"message": message, "source_quote": quote, "about": about, "text": text, "slot": slot,
            "kind": kind, "project": project}


def owner():
    """Проверенный владелец: нажатие в боте согласований или страница настройки."""
    return authority.owner_context(OWNER, chat_id=OWNER, action="test.memory")


async def plan(conn, **kw):
    return await pipeline.plan_run(conn, tz=TZ, now=kw.pop("now", NOW), **kw)


async def run_with(conn, reply, **plan_kw):
    """Прогон обработки: на каждый запрос извлечения отвечает reply(задание) -> разобранный ответ."""
    out = await plan(conn, **plan_kw)
    for job in await claim(conn):
        if job["payload"].get("schema_name") == "commitments":
            parsed = reply(job)
            assert await answer(conn, job, parsed)
    return out


async def notifications(conn):
    """Сообщения владельцу, ещё не забранные исполнителем, в порядке постановки."""
    return sorted(await claim(conn, bridge.NOTIFY_OWNER), key=lambda job: job["id"])


def buttons(jobs, module):
    return [data for job in jobs for _, data in buttons_of(job) if data.startswith(f"sh:{module}:")]


async def press_button(conn, data):
    return await press(conn, data)
