"""Две модели поиска по смыслу: приставки, смена модели, пересчёт, поиск во время пересчёта.

Сервер эмбеддингов подставной (FakeTEI из test_embeddings): у него общие «оси тем» для любых
моделей, поэтому вектор прежней модели здесь нарочно оказывается рядом с запросом новой —
и тест ловит именно смешение векторов разных моделей, а не случайное несовпадение.
"""

import asyncio
import dataclasses
from types import SimpleNamespace

import pytest

from shturman import embeddings, retrieval
from shturman.embeddings import EmbedderError, ModelProfile, WorkerSettings

from test_embeddings import (
    E5, FAST, URL, FakeTEI, Idle, add, add_chat, drain, pool, run_until_idle, states,  # noqa: F401
)
from test_embeddings import rec as _rec

USER2 = "deepvk/USER2-small"
ONE = dataclasses.replace(FAST, batch=1)


def rec(mid, text, **extra):
    """Сообщение от собеседника по имени Иван: имя входит в текст, который уходит модели."""
    return _rec(mid, text, name="Иван", **extra)


def mids(rows):
    return [r["tg_message_id"] for r in rows]


def search_state(embedder):
    return SimpleNamespace(extras={"embedder": embedder})


# --- перечень моделей ---

def test_user2_gets_search_prefixes():
    profile = embeddings.profile_for(USER2, {})
    assert (profile.query_prefix, profile.passage_prefix) == ("search_query: ", "search_document: ")
    # приставки e5 при этом прежние
    assert embeddings.profile_for(E5, {}) == ModelProfile("query: ", "passage: ")


def test_supported_models_are_two_and_each_has_prefixes(caplog):
    assert embeddings.SUPPORTED_MODELS == (E5, USER2)
    for model in embeddings.SUPPORTED_MODELS:
        profile = embeddings.profile_for(model, {})
        assert profile.query_prefix and profile.passage_prefix
        assert profile.query_prefix != profile.passage_prefix
    assert "приставки не известны" not in caplog.text


async def test_user2_worker_and_query_use_its_prefixes(conn, pool):
    _, chat_id = await add_chat(conn)
    await add(conn, chat_id, rec(1, "Пришлю смету по фасадам к пятнице"), rec(2, "ок"))
    tei = FakeTEI(model=USER2)
    embedder = tei.embedder(model=USER2)
    await drain(pool, embedder)
    assert await states(conn) == {1: (True, USER2), 2: (False, USER2)}
    assert tei.inputs == ["search_document: Иван: Пришлю смету по фасадам к пятнице"]
    await embedder.embed_query("когда пришлют смету")
    assert tei.inputs[-1] == "search_query: когда пришлют смету"


# --- смена модели туда и обратно ---

async def test_switch_e5_to_user2_and_back_recomputes_everything_each_time(conn, pool):
    _, chat_id = await add_chat(conn)
    await add(conn, chat_id, rec(1, "Пришлю смету по фасадам к пятнице"), rec(2, "ок"),
              rec(3, "Встреча будет во вторник утром"))

    await run_until_idle(pool, FakeTEI().embedder())
    assert await states(conn) == {1: (True, E5), 2: (False, E5), 3: (True, E5)}
    first = await conn.fetchval("SELECT embedding::text FROM messages WHERE tg_message_id = 1")

    user2 = FakeTEI(model=USER2)
    await run_until_idle(pool, user2.embedder(model=USER2))
    assert await states(conn) == {1: (True, USER2), 2: (False, USER2), 3: (True, USER2)}
    assert all(text.startswith("search_document: ") for text in user2.inputs)
    assert await conn.fetchval("SELECT embedding::text FROM messages WHERE tg_message_id = 1") != first
    assert await embeddings.counters(conn, USER2) == {"embedded": 2, "pending": 0, "skipped": 1, "stale": 0}

    back = FakeTEI()
    await run_until_idle(pool, back.embedder())
    assert await states(conn) == {1: (True, E5), 2: (False, E5), 3: (True, E5)}
    # прежние векторы рядом не хранились: e5 считает всё заново и получает то же самое
    assert sorted(back.inputs) == ["passage: Иван: Встреча будет во вторник утром",
                                   "passage: Иван: Пришлю смету по фасадам к пятнице"]
    assert await conn.fetchval("SELECT embedding::text FROM messages WHERE tg_message_id = 1") == first


async def test_counters_show_what_is_left_after_model_change(conn, pool):
    _, chat_id = await add_chat(conn)
    await add(conn, chat_id, rec(1, "Пришлю смету по фасадам к пятнице"), rec(2, "ок"),
              rec(3, "Встреча будет во вторник утром"), rec(4, "Договор отправили на подпись сегодня"))
    await drain(pool, FakeTEI().embedder())
    assert await embeddings.counters(conn, E5) == {"embedded": 3, "pending": 0, "skipped": 1, "stale": 0}

    # Настройку сменили, счётчик ещё не начал: всё прежнее числится устаревшим.
    assert await embeddings.counters(conn, USER2) == {"embedded": 0, "pending": 0, "skipped": 0, "stale": 4}
    assert await embeddings.reset_stale(pool, USER2) == 4
    assert await embeddings.counters(conn, USER2) == {"embedded": 0, "pending": 4, "skipped": 0, "stale": 0}

    embedder = FakeTEI(model=USER2).embedder(model=USER2)
    await embeddings.embed_batch(pool, embedder, ONE)       # одно сообщение — самое свежее
    assert await embeddings.counters(conn, USER2) == {"embedded": 1, "pending": 3, "skipped": 0, "stale": 0}
    assert (await states(conn))[4] == (True, USER2)
    await drain(pool, embedder)
    assert await embeddings.counters(conn, USER2) == {"embedded": 3, "pending": 0, "skipped": 1, "stale": 0}


async def test_old_vectors_survive_until_server_confirms_new_model(conn, pool):
    """В настройках уже новая модель, а контейнер ещё отдаёт прежнюю: векторы не трогаем."""
    _, chat_id = await add_chat(conn)
    await add(conn, chat_id, rec(1, "Пришлю смету по фасадам к пятнице"),
              rec(2, "Встреча будет во вторник утром"))
    await drain(pool, FakeTEI().embedder())
    before = await states(conn)

    still_e5 = FakeTEI()                                   # сервер отдаёт e5
    embedder = still_e5.embedder(model=USER2)              # сервис настроен на USER2
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise Idle

    with pytest.raises(Idle):
        await embeddings.run_worker(pool, embedder, FAST, sleep=fake_sleep)
    assert embedder.problem == "model_mismatch" and sleeps == [FAST.backoff_max] * 2
    assert await states(conn) == before and still_e5.requests == []
    # и поиск такими векторами не пользуется: запрос к серверу отвергнут сверкой модели
    rows = await retrieval.find(search_state(embedder), conn, "смета")
    assert mids(rows) == [1]

    # сервер перезапущен с новой моделью — пересчёт пошёл
    embedder = FakeTEI(model=USER2).embedder(model=USER2)
    await run_until_idle(pool, embedder)
    assert await states(conn) == {1: (True, USER2), 2: (True, USER2)}


# --- поиск во время пересчёта ---

async def test_search_never_mixes_vectors_of_two_models_during_recompute(conn, pool):
    _, chat_id = await add_chat(conn)
    await add(
        conn, chat_id,
        rec(1, "Бюджет на отделку согласовали вчера вечером"),       # найдётся только по смыслу
        rec(2, "Стоимость работ по кровле обсудим завтра днём"),     # найдётся только по смыслу
        rec(3, "Смету жду до вечера, пожалуйста не тяните"),          # есть слово из запроса
        rec(4, "Договор отправили на подпись сегодня утром"),        # мимо
    )
    await drain(pool, FakeTEI().embedder())
    e5_state = search_state(FakeTEI().embedder())
    assert set(mids(await retrieval.find(e5_state, conn, "смета"))) == {1, 2, 3, 4}

    # Модель сменили. Счётчик ещё ничего не успел: смысловая ветка пуста, работает ветка слов.
    user2 = FakeTEI(model=USER2)
    embedder = user2.embedder(model=USER2)
    state = search_state(embedder)
    assert mids(await retrieval.find(state, conn, "смета")) == [3]
    # Даже до возврата прежних векторов в очередь они в выдачу не попадают.
    assert (await states(conn))[1] == (True, E5)

    await embeddings.reset_stale(pool, USER2)
    assert mids(await retrieval.find(state, conn, "смета")) == [3]

    # Пересчитаны два самых свежих сообщения (4 и 3): по смыслу ищутся только они.
    await embeddings.embed_batch(pool, embedder, ONE)
    await embeddings.embed_batch(pool, embedder, ONE)
    assert {mid for mid, (has, model) in (await states(conn)).items() if has} == {3, 4}
    found = mids(await retrieval.find(state, conn, "смета"))
    assert found[0] == 3 and set(found) == {3, 4}

    # Пересчёт окончен — выдача та же, что была до смены модели.
    await drain(pool, embedder)
    assert set(mids(await retrieval.find(state, conn, "смета"))) == {1, 2, 3, 4}
    assert all(model == USER2 for _, model in (await states(conn)).values())


# --- длинные сообщения не останавливают очередь ---

def _row(mid, text, excluded=False):
    return {"id": mid, "text": text, "excluded": excluded}


def test_batch_is_cut_by_total_text_length_but_never_to_zero():
    settings = WorkerSettings(batch=32, batch_chars=1000)
    long = "слово " * 100                                   # 600 знаков
    short = "короткое сообщение про смету"
    cut = embeddings._within_budget
    assert [r["id"] for r in cut([_row(1, long), _row(2, long), _row(3, short)], settings)] == [1]
    assert [r["id"] for r in cut([_row(1, short), _row(2, long), _row(3, long)], settings)] == [1, 2]
    # одно сообщение длиннее предела уходит всё равно: дальше его обрежет сам сервер
    assert [r["id"] for r in cut([_row(1, long * 5), _row(2, short)], settings)] == [1]
    # то, что серверу не уйдёт (исключённый чат, слишком короткий текст), в счёт не идёт
    assert [r["id"] for r in cut([_row(1, long), _row(2, long, excluded=True), _row(3, "ок"),
                                  _row(4, short)], settings)] == [1, 2, 3, 4]
    assert cut([], settings) == []


async def test_long_messages_go_in_several_requests_and_all_get_vectors(conn, pool):
    _, chat_id = await add_chat(conn)
    long = "Подробное описание работ по фасаду и смета к нему. " * 20     # около 1000 знаков
    await add(conn, chat_id, *[rec(i, f"{i}. {long}") for i in range(1, 6)],
              rec(6, "Короткое сообщение про договор"))
    tei = FakeTEI(model=USER2)
    settings = dataclasses.replace(FAST, batch=32, batch_chars=2500)
    total = await drain(pool, tei.embedder(model=USER2), settings)
    assert (total.embedded, total.skipped) == (6, 0)
    assert [len(body["inputs"]) for body in tei.requests] == [3, 2, 1]
    assert all(has for has, _ in (await states(conn)).values())


def test_batch_chars_setting_from_env():
    assert WorkerSettings.from_env({}).batch_chars == 8000
    assert WorkerSettings.from_env({"SHTURMAN_EMBEDDINGS_BATCH_CHARS": "2000"}).batch_chars == 2000
    with pytest.raises(embeddings.ConfigError):
        WorkerSettings.from_env({"SHTURMAN_EMBEDDINGS_BATCH_CHARS": "10"})


# --- состояние для владельца ---

async def test_api_status_reports_counters_when_search_by_meaning_is_off(make_client, conn):
    _, chat_id = await add_chat(conn)
    await add(conn, chat_id, rec(1, "Пришлю смету по фасадам к пятнице"), rec(2, "ок"),
              rec(3, "Встреча будет во вторник утром"))

    client, _ = await make_client("shturman.api_core", "shturman.embeddings")   # поиск по смыслу выключен
    status = (await client.get("/api/status")).json()
    assert (status["embeddings_enabled"], status["embeddings_model"]) == (False, None)
    assert (status["embeddings_embedded"], status["embeddings_left"]) == (0, 3)
    assert status["embeddings_problem"] is None


async def test_api_status_follows_model_change(make_client, config, conn, pool, monkeypatch):
    _, chat_id = await add_chat(conn)
    await add(conn, chat_id, rec(1, "Пришлю смету по фасадам к пятнице"), rec(2, "ок"),
              rec(3, "Встреча будет во вторник утром"))
    await drain(pool, FakeTEI().embedder())                         # архив посчитан e5

    wrong = FakeTEI()                                               # контейнер ещё отдаёт e5
    monkeypatch.setattr(embeddings, "build_embedder", lambda cfg: wrong.embedder(model=cfg.embeddings_model))
    monkeypatch.setenv("SHTURMAN_EMBEDDINGS_PAUSE_MS", "0")
    cfg = dataclasses.replace(config, embeddings_url=URL, embeddings_model=USER2)
    client, state = await make_client("shturman.api_core", "shturman.embeddings", cfg=cfg)
    status = {}
    for _ in range(200):
        status = (await client.get("/api/status")).json()
        if status["embeddings_problem"]:
            break
        await asyncio.sleep(0.02)
    assert status["embeddings_enabled"] is True and status["embeddings_model"] == USER2
    assert status["embeddings_problem"] == "model_mismatch"
    assert (status["embeddings_embedded"], status["embeddings_left"]) == (0, 3)
    detail = (await client.get("/api/embeddings/status")).json()
    assert (detail["model"], detail["stale"], detail["pending"], detail["embedded"]) == (USER2, 3, 0, 0)
    # ни текста, ни имён в сводке нет
    assert "смет" not in (await client.get("/api/status")).text
    assert "Иван" not in (await client.get("/api/status")).text


async def test_query_to_server_with_other_model_is_refused_not_mixed():
    """Запрос не уходит серверу, который отдаёт другую модель: вектор запроса был бы чужим."""
    tei = FakeTEI()                                 # отдаёт e5
    embedder = tei.embedder(model=USER2)
    with pytest.raises(EmbedderError) as err:
        await embedder.embed_query("когда пришлют смету")
    assert err.value.mismatch and tei.requests == []
    await embedder.aclose()
