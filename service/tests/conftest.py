import io
import json
import os

import asyncpg
import pytest
import pytest_asyncio

from shturman import db

DSN = os.environ.get("SHTURMAN_TEST_DSN", "postgresql://postgres@127.0.0.1:54329/shturman_test")

OWNER = 1000
IVAN = 2001
MARIA = 2002


def msg(mid, ts, sender_id, sender, text, **extra):
    base = {
        "id": mid, "type": "message",
        "date": "2026-09-12T10:00:00", "date_unixtime": str(ts),
        "from": sender, "from_id": f"user{sender_id}",
        "text": text,
        "text_entities": [{"type": "plain", "text": text}] if isinstance(text, str) else [],
    }
    base.update(extra)
    return base


def full_export(chats):
    return {
        "about": "Here is the data you requested.",
        "personal_information": {"user_id": OWNER, "first_name": "Евгений", "last_name": "Тестов"},
        "contacts": {"about": "", "list": []},
        "chats": {"about": "", "list": chats},
        "left_chats": {"about": "", "list": []},
    }


def as_file(obj) -> io.BytesIO:
    return io.BytesIO(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


@pytest.fixture
def sample_export():
    t = 1789200000
    ivan = {
        "name": "Иван Петров", "type": "personal_chat", "id": IVAN,
        "messages": [
            msg(1, t, IVAN, "Иван Петров", "Добрый день! Пришлю смету по фасадам к пятнице."),
            msg(2, t + 60, OWNER, "Евгений Тестов", "Хорошо, жду. Сроки монтажа не сдвигаем."),
            msg(3, t + 120, IVAN, "Иван Петров",
                ["Договор лежит ", {"type": "link", "text": "https://example.org/doc"}, ", посмотрите"],
                text_entities=[{"type": "plain", "text": "Договор лежит "},
                               {"type": "link", "text": "https://example.org/doc"},
                               {"type": "plain", "text": ", посмотрите"}],
                reply_to_message_id=2),
            {"id": 4, "type": "service", "date": "2026-09-12T10:05:00", "date_unixtime": str(t + 300),
             "actor": "Иван Петров", "actor_id": f"user{IVAN}", "action": "phone_call",
             "duration_seconds": 42, "text": "", "text_entities": []},
            msg(5, t + 400, IVAN, "Иван Петров", "", media_type="voice_message",
                file="chats/chat_01/voice_messages/audio_1.ogg", duration_seconds=7),
            msg(6, t + 500, IVAN, "Иван Петров", "Фото объекта",
                photo="(File not included. Change data exporting settings to download.)"),
        ],
    }
    family = {
        "name": "Семья", "type": "private_group", "id": 3001,
        "messages": [msg(10, t + 10, MARIA, "Мария", "Купи хлеба и молока")],
    }
    channel = {
        "name": "Стройка: новости", "type": "public_channel", "id": 4001,
        "messages": [
            {"id": 1, "type": "message", "date": "x", "date_unixtime": str(t + 20),
             "from": "Стройка: новости", "from_id": "channel4001",
             "text": "Цены на арматуру выросли", "text_entities": []},
        ],
    }
    empty = {"name": "Пустой", "type": "personal_chat", "id": 2999, "messages": []}
    return full_export([ivan, family, channel, empty])


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    await c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await db.migrate(c)
    try:
        yield c
    finally:
        await c.close()
