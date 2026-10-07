"""Эмбеддинги: клиент сервера, фоновый счётчик, состояние. Сервер эмбеддингов подставной."""

import asyncio
import dataclasses
import hashlib
import json
import logging
import math
from datetime import datetime, timedelta, timezone

import asyncpg
import httpx
import pytest
import pytest_asyncio

from shturman import embeddings, retrieval, store
from shturman.config import ConfigError
from shturman.embeddings import Embedder, EmbedderError, ModelProfile, WorkerSettings
from shturman.records import ChatRecord, MessageRecord

from conftest import DSN

T0 = datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)
E5 = "intfloat/multilingual-e5-small"
URL = "http://tei.test"

# Подставная «модель»: у слов одной темы общая ось вектора. Так запрос «бюджет» оказывается
# рядом с сообщением про смету, хотя общих слов у них нет.
CONCEPTS = {
    "смет": 0, "бюджет": 0, "стоимост": 0,
    "встреч": 1, "созвон": 1, "совещан": 1,
    "договор": 2, "контракт": 2,
    "отпуск": 3, "отдых": 3,
    "оплат": 4, "счёт": 4, "счет": 4,
}


def fake_vector(text: str, *, dim: int = 384, salt: str = "") -> list[float]:
    """Детерминированный единичный вектор: оси тем плюс слабый шум от самого текста."""
    low = text.lower()
    vector = [0.0] * dim
    for stem, axis in CONCEPTS.items():
        if stem in low:
            vector[axis] = 1.0
    digest = hashlib.sha256((salt + low).encode()).digest()
    for i in range(8):
        vector[16 + (digest[i] * 256 + digest[i + 8]) % (dim - 16)] += 0.05
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector]


class FakeTEI:
    """Подставной сервер эмбеддингов с тем же HTTP-интерфейсом, что у TEI. В сеть не ходит."""

    def __init__(self, model: str = E5, dim: int = 384) -> None:
        self.model, self.dim = model, dim
        self.requests: list[dict] = []   # тела запросов /embed
        self.fail = 0                    # столько следующих /embed ответят 503
        self.down = False                # сервер недоступен совсем
        self.delay = 0.0                 # задержка ответа /embed
        self.reject = None               # функция(текст) -> True: ответить 422
        self.on_embed = None             # async-функция, вызывается во время /embed
        self.max_client_batch_size = 32

    @property
    def inputs(self) -> list[str]:
        return [text for body in self.requests for text in body["inputs"]]

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("нет соединения", request=request)
        if request.url.path == "/health":
            return httpx.Response(200)
        if request.url.path == "/info":
            return httpx.Response(200, json={
                "model_id": self.model, "served_model_name": self.model,
                "max_client_batch_size": self.max_client_batch_size, "version": "1.9.4"})
        assert request.method == "POST" and request.url.path == "/embed"
        body = json.loads(request.content)
        self.requests.append(body)
        if self.on_embed:
            await self.on_embed()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            self.fail -= 1
            return httpx.Response(503, json={"error": "unhealthy", "error_type": "unhealthy"})
        if self.reject and any(self.reject(t) for t in body["inputs"]):
            # Настоящий TEI может повторить вход в тексте ошибки — клиент не должен его читать.
            return httpx.Response(422, json={"error": "плохой вход: " + body["inputs"][0],
                                             "error_type": "tokenizer"})
        return httpx.Response(200, json=[fake_vector(t, dim=self.dim, salt=self.model)
                                         for t in body["inputs"]])

    def embedder(self, *, model: str = E5, dim: int = 384,
                 profile: ModelProfile | None = None) -> Embedder:
        return Embedder(URL, model, dim, profile or embeddings.profile_for(model, {}),
                        transport=httpx.MockTransport(self.handler))


def rec(mid, text, *, sender=2001, name="Иван Петров", at=T0, edited=None, kind="message"):
    return MessageRecord(
        tg_message_id=mid, sent_at=at, kind=kind, sender_class="user", sender_tg_id=sender,
        sender_name=name, text=text, entities=None, reply_to_tg_id=None, forwarded_from=None,
        edited_at=edited, media_type=None, media_path=None, service_action=None,
    )


async def add_chat(conn, *, tg_id=2001, name="Иван Петров", cls="user", type_="personal_chat",
                   owner=1000, label="Владелец", role="owner"):
    account_id = await store.ensure_account(conn, owner, label, role)
    chat_id, _ = await store.ensure_chat(conn, account_id, ChatRecord(cls, tg_id, type_, name))
    return account_id, chat_id


async def add(conn, chat_id, *records, source="import"):
    result = await store.upsert_messages(conn, [(chat_id, r) for r in records], source=source,
                                         owner_tg_id=1000)
    return result.new_ids


async def states(conn):
    """Состояние эмбеддинга по каждому сообщению: {tg_message_id: (есть вектор, модель)}."""
    rows = await conn.fetch("SELECT tg_message_id, embedding IS NOT NULL AS has, embedding_model FROM messages")
    return {r["tg_message_id"]: (r["has"], r["embedding_model"]) for r in rows}


@pytest_asyncio.fixture
async def pool(conn):
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    try:
        yield p
    finally:
        await p.close()


FAST = WorkerSettings(batch=4, pause=0.0, idle=1.0, backoff_base=2.0, backoff_max=16.0)


async def drain(pool, embedder, settings=FAST):
    """Гоняет шаги счётчика, пока очередь не опустеет. Возвращает суммы."""
    total = embeddings.BatchResult()
    for _ in range(100):
        step = await embeddings.embed_batch(pool, embedder, settings)
        if not step.taken:
            return total
        total.taken += step.taken
        total.embedded += step.embedded
        total.skipped += step.skipped
    raise AssertionError("очередь не пустеет")


class Idle(Exception):
    pass


async def run_until_idle(pool, embedder, settings=FAST):
    """Запускает цикл счётчика с подставным сном; останавливает на первой пустой очереди.

    Возвращает запрошенные паузы — по ним видно и паузы после сбоев, и обычный ритм.
    """
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        if seconds == settings.idle:
            raise Idle
        sleeps.append(seconds)
        await asyncio.sleep(0)

    with pytest.raises(Idle):
        await embeddings.run_worker(pool, embedder, settings, sleep=fake_sleep)
    return sleeps


# --- настройки и отбор ---

def test_e5_models_get_query_and_passage_prefixes():
    profile = embeddings.profile_for(E5, {})
    assert (profile.query_prefix, profile.passage_prefix) == ("query: ", "passage: ")


def test_unknown_model_has_no_prefixes_unless_configured(caplog):
    with caplog.at_level(logging.WARNING, logger="shturman.embeddings"):
        assert embeddings.profile_for("acme/unknown", {}) == ModelProfile("", "")
    assert "приставки не известны" in caplog.text
    env = {"SHTURMAN_EMBEDDINGS_QUERY_PREFIX": "search_query: ", "SHTURMAN_EMBEDDINGS_PASSAGE_PREFIX": ""}
    assert embeddings.profile_for("acme/unknown", env) == ModelProfile("search_query: ", "")
    # явная настройка сильнее встроенной
    assert embeddings.profile_for(E5, {"SHTURMAN_EMBEDDINGS_PASSAGE_PREFIX": "doc: "}) == ModelProfile("query: ", "doc: ")


@pytest.mark.parametrize("text,expected", [
    ("ок", False),
    ("Спасибо большое!", False),
    ("", False),
    ("👍👍👍 !!! ...", False),
    ("12 345 678 90", False),
    ("я и ты", False),                                   # однобуквенные не в счёт
    ("https://example.org/a/b/c?d=e смотри", False),    # ссылка — не слова
    ("Пришлю смету завтра", True),
    ("Договор лежит https://example.org/doc, посмотрите", True),
    ("Send the contract please", True),
])
def test_only_texts_with_real_content_are_embedded(text, expected):
    assert embeddings.is_embeddable(text) is expected


def test_min_words_is_configurable():
    assert embeddings.is_embeddable("Смета готова", 2) is True
    assert embeddings.is_embeddable("Смета готова", 3) is False


def test_worker_settings_from_env_and_validation():
    s = WorkerSettings.from_env({"SHTURMAN_EMBEDDINGS_BATCH": "8", "SHTURMAN_EMBEDDINGS_PAUSE_MS": "1500",
                                 "SHTURMAN_EMBEDDINGS_MIN_WORDS": "2"})
    assert (s.batch, s.pause, s.min_words) == (8, 1.5, 2)
    assert WorkerSettings.from_env({}) == WorkerSettings()
    with pytest.raises(ConfigError):
        WorkerSettings.from_env({"SHTURMAN_EMBEDDINGS_BATCH": "много"})
    with pytest.raises(ConfigError):
        WorkerSettings.from_env({"SHTURMAN_EMBEDDINGS_BATCH": "0"})


def test_backoff_doubles_and_is_capped():
    s = WorkerSettings(backoff_base=2.0, backoff_max=300.0)
    assert [embeddings.backoff_delay(n, s) for n in (1, 2, 3, 4, 9, 50)] == [2, 4, 8, 16, 300, 300]


def test_embedder_refuses_non_http_address():
    with pytest.raises(ConfigError):
        Embedder("tei:80", E5, 384, ModelProfile())


# --- выключенный модуль ---

async def test_without_url_module_is_idle_and_search_is_fulltext(make_client, conn):
    _, chat_id = await add_chat(conn)
    await add(conn, chat_id, rec(1, "Пришлю смету по фасадам к пятнице"), rec(2, "ок"))
    client, state = await make_client("shturman.embeddings")
    assert "embedder" not in state.extras
    assert not [t for t in state._tasks if t.get_name() == "embeddings-worker"]
    status = (await client.get("/api/embeddings/status")).json()
    assert status == {"enabled": False, "model": E5, "embedded": 0, "pending": 2, "skipped": 0,
                      "reachable": None, "problem": None}
    rows = await retrieval.find(state, conn, "смета")
    assert [r["tg_message_id"] for r in rows] == [1]
    assert "«смету»" in rows[0]["snippet"] and rows[0]["score"] > 0
    assert await states(conn) == {1: (False, None), 2: (False, None)}


# --- счётчик ---

async def test_worker_embeds_messages_and_skips_what_must_not_be_embedded(conn, pool):
    _, ivan = await add_chat(conn)
    _, secret = await add_chat(conn, tg_id=2002, name="Тайный чат")
    await add(conn, ivan,
              rec(1, "Пришлю смету по фасадам к пятнице"),
              rec(2, "ок"),
              rec(3, "Спасибо большое"),
              rec(4, "Договор подпишем на встрече во вторник", name=None),
              rec(5, "Это сообщение потом удалили навсегда"),
              rec(6, "", kind="service"),
              rec(7, ""))
    await add(conn, secret, rec(1, "Секретный разговор про отпуск на море"))
    await conn.execute("UPDATE chats SET excluded = true WHERE id = $1", secret)
    await store.mark_deleted(conn, ivan, [5])

    tei = FakeTEI()
    total = await drain(pool, tei.embedder())
    assert (total.embedded, total.skipped) == (2, 4)

    rows = await conn.fetch(
        """SELECT m.tg_message_id AS mid, c.id = $1 AS ivan, m.embedding IS NOT NULL AS has,
                  m.embedding_model AS model, vector_dims(m.embedding) AS dims
           FROM messages m JOIN chats c ON c.id = m.chat_id""", ivan)
    got = {(r["ivan"], r["mid"]): (r["has"], r["model"]) for r in rows}
    assert got == {
        (True, 1): (True, E5), (True, 4): (True, E5),      # посчитаны
        (True, 2): (False, E5), (True, 3): (False, E5),    # слишком короткие — рассмотрены, пропущены
        (True, 7): (False, E5),                            # пустой текст (вложение)
        (False, 1): (False, E5),                           # исключённый чат
        (True, 5): (False, None),                          # удалённое — не трогаем
        (True, 6): (False, None),                          # служебное — не трогаем
    }
    assert {r["dims"] for r in rows if r["has"]} == {384}
    # На сервер ушли только два текста, с приставкой модели и именем отправителя, где оно есть.
    assert sorted(tei.inputs) == [
        "passage: Договор подпишем на встрече во вторник",
        "passage: Иван Петров: Пришлю смету по фасадам к пятнице",
    ]
    assert all(body["truncate"] is True and body["normalize"] is True for body in tei.requests)
    # Очередь пуста: повторный шаг ничего не берёт и на сервер не ходит.
    before = len(tei.requests)
    assert (await embeddings.embed_batch(pool, tei.embedder(), FAST)).taken == 0
    assert len(tei.requests) == before


async def test_newest_messages_are_embedded_first_and_batch_respects_server_limit(conn, pool):
    _, chat_id = await add_chat(conn)
    await add(conn, chat_id, *[rec(i, f"Сообщение номер {i} про смету и договор") for i in range(1, 8)])
    tei = FakeTEI()
    tei.max_client_batch_size = 3
    step = await embeddings.embed_batch(pool, tei.embedder(), WorkerSettings(batch=32))
    assert (step.taken, step.embedded) == (3, 3)
    done = {mid for mid, (has, _) in (await states(conn)).items() if has}
    assert done == {5, 6, 7}


async def test_edit_returns_message_to_queue_and_delete_drops_vector(conn, pool):
    _, chat_id = await add_chat(conn)
    await add(conn, chat_id, rec(1, "Пришлю смету по фасадам к пятнице"), rec(2, "Встреча будет во вторник утром"))
    tei = FakeTEI()
    await drain(pool, tei.embedder())
    before = await conn.fetchval("SELECT embedding::text FROM messages WHERE tg_message_id = 1")

    # тот же текст из другого источника — вектор остаётся
    await add(conn, chat_id, rec(1, "Пришлю смету по фасадам к пятнице"), source="session")
    assert (await states(conn))[1] == (True, E5)

    # правка — вектор снят, сообщение снова в очереди и пересчитывается по новому тексту
    await add(conn, chat_id, rec(1, "Пришлю договор к понедельнику, не раньше",
                                 edited=T0 + timedelta(hours=1)), source="session")
    assert (await states(conn))[1] == (False, None)
    assert (await drain(pool, tei.embedder())).embedded == 1
    after = await conn.fetchval("SELECT embedding::text FROM messages WHERE tg_message_id = 1")
    assert after != before and (await states(conn))[1] == (True, E5)

    # удаление — вектор снят и больше не считается
    await store.mark_deleted(conn, chat_id, [2])
    assert (await states(conn))[2] == (False, None)
    assert (await drain(pool, tei.embedder())).taken == 0


async def test_edit_during_embedding_does_not_get_stale_vector(conn, pool):
    _, chat_id = await add_chat(conn)
    await add(conn, chat_id, rec(1, "Пришлю смету по фасадам к пятнице"))
    tei = FakeTEI()

    async def edit_meanwhile():
        tei.on_embed = None
        await conn.execute("UPDATE messages SET text = 'Встреча переносится на среду, смету не ждите'")

    tei.on_embed = edit_meanwhile
    step = await embeddings.embed_batch(pool, tei.embedder(), FAST)
    assert (step.taken, step.embedded) == (1, 0)
    assert (await states(conn))[1] == (False, None)       # вектор старого текста не записан
    assert (await embeddings.embed_batch(pool, tei.embedder(), FAST)).embedded == 1
    assert "Встреча переносится" in tei.inputs[-1]


async def test_model_change_reembeds_everything(conn, pool):
    _, chat_id = await add_chat(conn)
    await add(conn, chat_id, rec(1, "Пришлю смету по фасадам к пятнице"), rec(2, "ок"),
              rec(3, "Встреча будет во вторник утром"))
    await drain(pool, FakeTEI().embedder())
    old = await conn.fetchval("SELECT embedding::text FROM messages WHERE tg_message_id = 1")
    assert await states(conn) == {1: (True, E5), 2: (False, E5), 3: (True, E5)}

    # та же модель — ничего не возвращается в очередь
    assert await run_until_idle(pool, FakeTEI().embedder()) == []
    assert await conn.fetchval("SELECT embedding::text FROM messages WHERE tg_message_id = 1") == old

    other = "acme/other-384"
    tei = FakeTEI(model=other)
    await run_until_idle(pool, tei.embedder(model=other))
    assert await states(conn) == {1: (True, other), 2: (False, other), 3: (True, other)}
    assert await conn.fetchval("SELECT embedding::text FROM messages WHERE tg_message_id = 1") != old
    # у неизвестной модели приставок нет
    assert sorted(tei.inputs) == ["Иван Петров: Встреча будет во вторник утром",
                                  "Иван Петров: Пришлю смету по фасадам к пятнице"]


async def test_worker_backs_off_on_errors_and_recovers(conn, pool, caplog):
    _, chat_id = await add_chat(conn)
    await add(conn, chat_id, *[rec(i, f"Сообщение номер {i} про смету и договор") for i in range(1, 7)])
    tei = FakeTEI()
    tei.fail = 3
    with caplog.at_level(logging.DEBUG, logger="shturman"):
        sleeps = await run_until_idle(pool, tei.embedder())
    # три сбоя подряд — паузы 2, 4, 8 секунд; затем две пачки по четыре без пауз
    assert sleeps == [2.0, 4.0, 8.0, 0.0, 0.0]
    assert all(has for has, _ in (await states(conn)).values())
    assert "сбой (HTTP 503)" in caplog.text and "снова отвечает" in caplog.text
    assert "Сообщение номер" not in caplog.text      # текст переписки в журнал не попадает


async def test_unreachable_server_keeps_queue_intact(conn, pool):
    _, chat_id = await add_chat(conn)
    await add(conn, chat_id, rec(1, "Пришлю смету по фасадам к пятнице"))
    tei = FakeTEI()
    tei.down = True
    embedder = tei.embedder()
    with pytest.raises(EmbedderError) as err:
        await embeddings.embed_batch(pool, embedder, FAST)
    assert err.value.reason == "ConnectError" and embedder.problem == "unreachable"
    assert await states(conn) == {1: (False, None)}
    tei.down = False
    assert (await embeddings.embed_batch(pool, embedder, FAST)).embedded == 1
    assert embedder.problem is None


async def test_rejected_message_is_skipped_and_does_not_block_queue(conn, pool, caplog):
    _, chat_id = await add_chat(conn)
    await add(conn, chat_id, rec(1, "Пришлю смету по фасадам к пятнице"),
              rec(2, "Ядовитое сообщение ломает разбор текста"), rec(3, "Встреча будет во вторник утром"))
    tei = FakeTEI()
    tei.reject = lambda text: "Ядовитое" in text
    with caplog.at_level(logging.DEBUG, logger="shturman"):
        step = await embeddings.embed_batch(pool, tei.embedder(), FAST)
    assert (step.embedded, step.skipped) == (2, 1)
    assert await states(conn) == {1: (True, E5), 2: (False, E5), 3: (True, E5)}
    assert "не принял сообщение" in caplog.text and "Ядовитое" not in caplog.text


async def test_wrong_model_on_server_writes_nothing(conn, pool):
    _, chat_id = await add_chat(conn)
    await add(conn, chat_id, rec(1, "Пришлю смету по фасадам к пятнице"))
    tei = FakeTEI(model="acme/not-what-was-configured")
    embedder = tei.embedder()
    with pytest.raises(EmbedderError) as err:
        await embeddings.embed_batch(pool, embedder, FAST)
    assert err.value.mismatch and embedder.problem == "model_mismatch"
    assert tei.requests == [] and await states(conn) == {1: (False, None)}

    # в цикле такая ошибка ждёт дольше всего: сама она не пройдёт
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise Idle

    with pytest.raises(Idle):
        await embeddings.run_worker(pool, embedder, FAST, sleep=fake_sleep)
    assert sleeps == [FAST.backoff_max, FAST.backoff_max]


async def test_wrong_vector_size_from_server_writes_nothing(conn, pool):
    _, chat_id = await add_chat(conn)
    await add(conn, chat_id, rec(1, "Пришлю смету по фасадам к пятнице"))
    tei = FakeTEI(dim=768)
    with pytest.raises(EmbedderError) as err:
        await embeddings.embed_batch(pool, tei.embedder(), FAST)
    assert err.value.mismatch
    assert await states(conn) == {1: (False, None)}


# --- запрос ---

async def test_query_gets_query_prefix_and_fails_fast_after_timeout():
    tei = FakeTEI()
    embedder = tei.embedder()
    vector = await embedder.embed_query("когда пришлют смету")
    assert len(vector) == 384 and tei.inputs == ["query: когда пришлют смету"]

    tei.delay = 0.5
    with pytest.raises(EmbedderError):
        await embedder.embed_query("медленный запрос", timeout=0.05)
    tei.delay = 0.0
    seen = len(tei.requests)
    with pytest.raises(EmbedderError) as err:          # пауза после сбоя: на сервер не ходим
        await embedder.embed_query("сразу следом")
    assert err.value.reason == "пауза после сбоя" and len(tei.requests) == seen
    await embedder.embed_passages(["счётчик дозвонился до сервера"])   # сервер жив — пауза снята
    assert len(await embedder.embed_query("теперь можно")) == 384
    await embedder.aclose()


# --- сервис целиком ---

async def test_service_refuses_to_start_when_dimension_differs_from_schema(make_client, config, conn):
    assert await embeddings.column_dim(conn) == 384
    cfg = dataclasses.replace(config, embeddings_url=URL, embeddings_dim=768)
    with pytest.raises(ConfigError, match="768"):
        await make_client("shturman.embeddings", cfg=cfg)


async def test_service_runs_worker_and_reports_counts_only(make_client, config, conn, monkeypatch):
    _, ivan = await add_chat(conn)
    await add(conn, ivan, rec(1, "Пришлю смету по фасадам к пятнице"), rec(2, "ок"),
              rec(3, "Встреча будет во вторник утром"))
    tei = FakeTEI()
    monkeypatch.setattr(embeddings, "build_embedder", lambda cfg: tei.embedder(model=cfg.embeddings_model))
    monkeypatch.setenv("SHTURMAN_EMBEDDINGS_PAUSE_MS", "0")
    client, state = await make_client("shturman.embeddings", cfg=dataclasses.replace(config, embeddings_url=URL))
    assert isinstance(state.extras["embedder"], Embedder)

    for _ in range(200):
        status = (await client.get("/api/embeddings/status")).json()
        if status["pending"] == 0:
            break
        await asyncio.sleep(0.02)
    assert status == {"enabled": True, "model": E5, "embedded": 2, "pending": 0, "skipped": 1,
                      "reachable": True, "problem": None}

    tei.down = True
    status = (await client.get("/api/embeddings/status")).json()
    assert (status["reachable"], status["problem"]) == (False, "unreachable")
    assert (await client.get("/api/embeddings/status", headers={"Authorization": "Bearer no"})).status_code == 401
