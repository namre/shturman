"""Эмбеддинги сообщений: клиент сервера эмбеддингов и фоновый счётчик.

Сервер эмбеддингов — отдельный контейнер HuggingFace Text Embeddings Inference (TEI); сервис
ходит к нему по HTTP. Проверено по исходникам TEI 1.9.4 (`router/src/http/types.rs`,
`server.rs`): `POST /embed`, `GET /health`, `GET /info`.

Если `SHTURMAN_EMBEDDINGS_URL` пуст, модуль ничего не делает, а поиск остаётся полнотекстовым.

Иначе фоновый счётчик пачками берёт нерассмотренные сообщения, считает векторы и пишет их в
архив. Очередь — это сами строки архива (`embedding_model IS NULL`), поэтому перезапуск ничего
не теряет. Состояния строки описаны в миграции `0004_embeddings.sql`.

Что считается: обычные сообщения (`kind = 'message'`), не удалённые, из неисключённых чатов,
с содержательным текстом — не меньше `min_words` слов (по умолчанию 3). Реплики вроде «ок» или
«спасибо большое» смысла для поиска не несут, зато оказываются «ближайшими» к любому короткому
запросу; по словам их и так находит полнотекстовая ветка. Длинный текст обрезает сам сервер
(`truncate: true`): модель видит первые 512 токенов.

Текст сообщений и запросов в журнал не пишется: только счётчики, идентификаторы и вид ошибки.

Переменные окружения модуля (кроме общих `SHTURMAN_EMBEDDINGS_URL`, `_MODEL`, `_DIM`):
  SHTURMAN_EMBEDDINGS_BATCH           размер пачки, по умолчанию 32 (предел TEI по умолчанию);
  SHTURMAN_EMBEDDINGS_PAUSE_MS        пауза между пачками, по умолчанию 500 — чтобы не занимать
                                      процессор сервера целиком;
  SHTURMAN_EMBEDDINGS_MIN_WORDS       минимум слов в сообщении, по умолчанию 3;
  SHTURMAN_EMBEDDINGS_QUERY_PREFIX,
  SHTURMAN_EMBEDDINGS_PASSAGE_PREFIX  приставки к тексту запроса и сообщения для модели, которой
                                      нет в MODEL_PROFILES; значение берётся как есть, вместе
                                      с пробелами. Менять их при уже посчитанных векторах
                                      нельзя без пересчёта.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Sequence

import asyncpg
import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from .config import Config, ConfigError

logger = logging.getLogger("shturman.embeddings")

QUERY_TIMEOUT = 2.0      # секунд на эмбеддинг запроса: дольше поиск ждать не должен
BATCH_TIMEOUT = 120.0    # секунд на пачку сообщений
HEALTH_TIMEOUT = 1.0
QUERY_COOLDOWN = 30.0    # после сбоя запросы столько секунд идут без смысловой ветки
STALE_CHUNK = 5000       # строк за один шаг при возврате устаревших векторов в очередь



# --- модели ---

@dataclass(frozen=True)
class ModelProfile:
    """То, что зависит от модели, а не от сервера: приставки к тексту запроса и сообщения."""
    query_prefix: str = ""
    passage_prefix: str = ""


# Семейство multilingual-e5 обучено с приставками, без них качество падает. Карточки моделей
# на huggingface.co (сверено 2026-10-06): «Each input text should start with "query: " or
# "passage: ", even for non-English texts».
_E5 = ModelProfile(query_prefix="query: ", passage_prefix="passage: ")

MODEL_PROFILES: dict[str, ModelProfile] = {
    "intfloat/multilingual-e5-small": _E5,   # 384
    "intfloat/multilingual-e5-base": _E5,    # 768 — нужна миграция размерности
    "intfloat/multilingual-e5-large": _E5,   # 1024 — нужна миграция размерности
}


def profile_for(model: str, env: Mapping[str, str] | None = None) -> ModelProfile:
    """Приставки для модели: из переменных окружения, иначе из MODEL_PROFILES, иначе пустые."""
    env = os.environ if env is None else env
    known = MODEL_PROFILES.get(model)
    query = env.get("SHTURMAN_EMBEDDINGS_QUERY_PREFIX")
    passage = env.get("SHTURMAN_EMBEDDINGS_PASSAGE_PREFIX")
    if known is None and query is None and passage is None:
        logger.warning(
            "для модели %s приставки не известны — тексты уйдут без приставок; если модели они "
            "нужны, задайте SHTURMAN_EMBEDDINGS_QUERY_PREFIX и SHTURMAN_EMBEDDINGS_PASSAGE_PREFIX",
            model)
    base = known or ModelProfile()
    return ModelProfile(
        query_prefix=base.query_prefix if query is None else query,
        passage_prefix=base.passage_prefix if passage is None else passage,
    )


# --- клиент сервера эмбеддингов ---

class EmbedderError(Exception):
    """Сервер эмбеддингов недоступен или ответил не тем. Текст ошибки не содержит данных.

    rejected — сервер отказался принять именно эти тексты (коды 400, 413, 422): повтор той же
    пачки не поможет. mismatch — сервер отдаёт не ту модель, что указана в настройках.
    """

    def __init__(self, reason: str, *, rejected: bool = False, mismatch: bool = False,
                 cooldown: bool = False) -> None:
        super().__init__(reason)
        self.reason, self.rejected, self.mismatch = reason, rejected, mismatch
        # cooldown — к серверу не обращались: идёт пауза после недавнего сбоя.
        self.cooldown = cooldown


class Embedder:
    """Клиент TEI. Общий для счётчика и поиска; лежит в `state.extras["embedder"]`."""

    def __init__(
        self, url: str, model: str, dim: int, profile: ModelProfile, *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not url.startswith(("http://", "https://")):
            raise ConfigError("SHTURMAN_EMBEDDINGS_URL: нужен адрес вида http://имя:порт")
        self.model, self.dim, self.profile = model, dim, profile
        # trust_env=False: тексты переписки идут только на указанный адрес, а не через прокси
        # из переменных окружения.
        self._client = httpx.AsyncClient(
            base_url=url.rstrip("/"), transport=transport, trust_env=False,
            timeout=httpx.Timeout(BATCH_TIMEOUT, connect=5.0),
        )
        self._verified = False
        self._blocked_until = 0.0
        # Сколько текстов сервер принимает за запрос; уточняется из /info.
        self.max_batch = 32
        # Что не так, если сервер не годится: None, "unreachable" или "model_mismatch".
        self.problem: str | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, *, timeout: float, json: Any = None) -> httpx.Response:
        try:
            response = await self._client.request(method, path, json=json, timeout=timeout)
        except httpx.HTTPError as exc:
            self._verified = False
            self.problem = "unreachable"
            # Только вид ошибки: в её тексте могут оказаться адрес и подробности запроса.
            raise EmbedderError(type(exc).__name__) from None
        if response.status_code != 200:
            if response.status_code >= 500:
                self._verified = False
                self.problem = "unreachable"
            # Тело ответа не читаем и не пишем в журнал: TEI может повторить в нём входной текст.
            raise EmbedderError(f"HTTP {response.status_code}",
                                rejected=response.status_code in (400, 413, 422))
        return response

    async def healthy(self) -> bool:
        try:
            await self._request("GET", "/health", timeout=HEALTH_TIMEOUT)
        except EmbedderError:
            return False
        return True

    async def verify(self, *, timeout: float = 5.0) -> None:
        """Убеждается, что сервер отдаёт именно ту модель, которой будут подписаны векторы.

        Иначе векторы чужой модели легли бы в архив под именем настроенной, и ошибку потом
        было бы не найти. Если TEI запущен с моделью из локального каталога, имя задаётся
        его параметром `--served-model-name`.
        """
        response = await self._request("GET", "/info", timeout=timeout)
        try:
            info = response.json()
            names = {info.get("model_id"), info.get("served_model_name")}
            limit = info.get("max_client_batch_size")
        except (ValueError, AttributeError):
            raise EmbedderError("непонятный ответ /info") from None
        if self.model not in names:
            self.problem = "model_mismatch"
            raise EmbedderError("сервер эмбеддингов отдаёт другую модель", mismatch=True)
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
            self.max_batch = limit
        self._verified = True
        self.problem = None

    async def ensure_verified(self, *, timeout: float = 5.0) -> None:
        """Сверяет модель, если после последнего сбоя связи это ещё не сделано."""
        if not self._verified:
            await self.verify(timeout=timeout)

    async def _embed(self, inputs: list[str], *, timeout: float) -> list[list[float]]:
        await self.ensure_verified(timeout=min(timeout, 5.0))
        response = await self._request(
            "POST", "/embed", timeout=timeout,
            json={"inputs": inputs, "truncate": True, "normalize": True},
        )
        try:
            vectors = response.json()
        except ValueError:
            raise EmbedderError("ответ не JSON") from None
        if not isinstance(vectors, list) or len(vectors) != len(inputs):
            raise EmbedderError("число векторов не совпало с числом текстов")
        for vector in vectors:
            if not isinstance(vector, list) or len(vector) != self.dim:
                self.problem = "model_mismatch"
                raise EmbedderError(
                    f"размерность вектора не {self.dim}: проверьте модель и SHTURMAN_EMBEDDINGS_DIM",
                    mismatch=True)
            if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
                       for x in vector):
                raise EmbedderError("в векторе не числа")
        return vectors

    async def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        """Векторы сообщений — для записи в архив."""
        vectors = await self._embed([self.profile.passage_prefix + t for t in texts],
                                    timeout=BATCH_TIMEOUT)
        self._blocked_until = 0.0  # сервер работает — поиск снова может к нему обращаться
        return vectors

    async def embed_query(self, text: str, *, timeout: float = QUERY_TIMEOUT) -> list[float]:
        """Вектор поискового запроса. Короткий срок ожидания; после сбоя — пауза.

        Пауза нужна, чтобы при лежащем сервере каждый поиск не ждал свои две секунды.
        """
        if time.monotonic() < self._blocked_until:
            raise EmbedderError("пауза после сбоя", cooldown=True)
        try:
            # Общий срок на всё, включая сверку модели при первом обращении.
            vectors = await asyncio.wait_for(
                self._embed([self.profile.query_prefix + text], timeout=timeout), timeout)
        except asyncio.TimeoutError:
            self._blocked_until = time.monotonic() + QUERY_COOLDOWN
            raise EmbedderError("TimeoutError") from None
        except EmbedderError as exc:
            if not exc.rejected:
                self._blocked_until = time.monotonic() + QUERY_COOLDOWN
            raise
        return vectors[0]


def build_embedder(config: Config) -> Embedder:
    """Создаёт клиента по настройкам. Тесты подменяют эту функцию, чтобы не ходить в сеть."""
    return Embedder(config.embeddings_url, config.embeddings_model, config.embeddings_dim,
                    profile_for(config.embeddings_model))


def vector_literal(vector: Sequence[float]) -> str:
    """Вектор в текстовой записи pgvector: параметр запроса с приведением `$n::halfvec`."""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


# --- что считать ---

_URL = re.compile(r"https?://\S+")
_WORD = re.compile(r"[^\W\d_]{2,}")  # слово — от двух букв подряд; числа и знаки не в счёт


def is_embeddable(text: str, min_words: int = 3) -> bool:
    """Есть ли в тексте что искать по смыслу: не меньше min_words слов, ссылки не в счёт."""
    found = 0
    for _ in _WORD.finditer(_URL.sub(" ", text)):
        found += 1
        if found >= min_words:
            return True
    return False


def passage_text(text: str, *, sender_name: str | None = None) -> str:
    """Единица для эмбеддинга: «имя отправителя: текст сообщения».

    Выбор сделан замером на синтетическом русском наборе (94 сообщения, 40 запросов, модель
    multilingual-e5-small; цифры — в отчёте к задаче и в сообщении коммита):
      * имя отправителя заметно помогает, когда в имени контакта есть роль («Ольга Бухгалтер»,
        «Сергей Прораб»), и ничего не меняет при обычных именах;
      * приставлять имя только к входящим хуже, чем ко всем: формат должен быть единым;
      * окно из соседних сообщений находит реплики, понятные только из контекста, но сбивает
        порядок выдачи и требует пересчёта при правке соседа. Вместо него агент берёт соседей
        найденного сообщения через `search.thread`.

    Смена единицы или приставок делает уже посчитанные векторы несравнимыми с новыми: после
    такой правки их нужно пересчитать (обнулить embedding и embedding_model).
    """
    sender = (sender_name or "").strip()
    return f"{sender}: {text}" if sender else text


# --- фоновый счётчик ---

@dataclass(frozen=True)
class WorkerSettings:
    batch: int = 32
    pause: float = 0.5          # между пачками, секунд
    idle: float = 5.0           # между проверками пустой очереди
    min_words: int = 3
    backoff_base: float = 2.0   # первая пауза после сбоя; дальше удваивается
    backoff_max: float = 300.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "WorkerSettings":
        env = os.environ if env is None else env

        def number(name: str, default: int, low: int, high: int) -> int:
            raw = (env.get(name) or "").strip()
            if not raw:
                return default
            try:
                value = int(raw)
            except ValueError:
                raise ConfigError(f"{name}: нужно целое число") from None
            if not low <= value <= high:
                raise ConfigError(f"{name}: допустимо от {low} до {high}")
            return value

        return cls(
            batch=number("SHTURMAN_EMBEDDINGS_BATCH", 32, 1, 256),
            pause=number("SHTURMAN_EMBEDDINGS_PAUSE_MS", 500, 0, 60_000) / 1000,
            min_words=number("SHTURMAN_EMBEDDINGS_MIN_WORDS", 3, 1, 50),
        )


@dataclass
class BatchResult:
    taken: int = 0      # сколько строк взято из очереди
    embedded: int = 0   # скольким записан вектор
    skipped: int = 0    # сколько отмечено «рассмотрено, вектора не будет»


def backoff_delay(failures: int, settings: WorkerSettings) -> float:
    """Пауза после `failures` сбоев подряд: 2, 4, 8 … секунд, не больше backoff_max."""
    return min(settings.backoff_max, settings.backoff_base * 2 ** min(max(failures, 1) - 1, 30))


# Свежие сообщения — первыми: живая переписка должна искаться сразу, даже пока считается
# большой импорт. md5 текста возвращается в запись результата как защита от гонки с правкой.
_TAKE = """
SELECT m.id, m.text, m.sender_name, md5(m.text) AS digest, c.excluded
FROM messages m
JOIN chats c ON c.id = m.chat_id
WHERE m.embedding_model IS NULL AND m.kind = 'message' AND m.deleted_at IS NULL
ORDER BY m.id DESC
LIMIT $1
"""

# Запись только если строка всё ещё ждёт и текст тот же, что считали: если сообщение за это
# время исправили или удалили, оно останется в очереди со своим новым текстом.
_STORE = """
UPDATE messages m
SET embedding = v.embedding::halfvec, embedding_model = $1
FROM unnest($2::bigint[], $3::text[], $4::text[]) AS v (id, embedding, digest)
WHERE m.id = v.id AND m.embedding_model IS NULL AND m.deleted_at IS NULL AND md5(m.text) = v.digest
"""

_SKIP = """
UPDATE messages m
SET embedding = NULL, embedding_model = $1
FROM unnest($2::bigint[], $3::text[]) AS v (id, digest)
WHERE m.id = v.id AND m.embedding_model IS NULL AND md5(m.text) = v.digest
"""

_COLUMN_TYPE = """
SELECT format_type(a.atttypid, a.atttypmod)
FROM pg_attribute a
WHERE a.attrelid = 'messages'::regclass AND a.attname = 'embedding' AND NOT a.attisdropped
"""


def _count(status: str) -> int:
    return int(status.rsplit(" ", 1)[-1])


async def column_dim(conn: asyncpg.Connection) -> int | None:
    """Размерность столбца messages.embedding, как она записана в схеме."""
    declared = await conn.fetchval(_COLUMN_TYPE)
    match = re.fullmatch(r"halfvec\((\d+)\)", declared or "")
    return int(match.group(1)) if match else None


async def reset_stale(
    pool: asyncpg.Pool, model: str, *, chunk: int = STALE_CHUNK,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> int:
    """Возвращает в очередь всё, что посчитано или пропущено другой моделью.

    Вызывается один раз при запуске: модель меняется только настройкой и перезапуском. Идёт по
    первичному ключу небольшими шагами, чтобы не держать долгих блокировок и не читать таблицу
    целиком одним запросом.
    """
    async with pool.acquire() as conn:
        top = await conn.fetchval("SELECT max(id) FROM messages") or 0
    total = 0
    for low in range(0, top, chunk):
        async with pool.acquire() as conn:
            status = await conn.execute(
                """UPDATE messages SET embedding = NULL, embedding_model = NULL
                   WHERE id > $1 AND id <= $2
                     AND embedding_model IS NOT NULL AND embedding_model <> $3""",
                low, low + chunk, model,
            )
        changed = _count(status)
        total += changed
        if changed:
            await sleep(0.05)
    return total


async def embed_batch(pool: asyncpg.Pool, embedder: Embedder, settings: WorkerSettings) -> BatchResult:
    """Один шаг счётчика: взять пачку из очереди, посчитать, записать.

    Соединение с базой на время обращения к серверу эмбеддингов не удерживается.
    """
    # Сначала сервер: если он недоступен или отдаёт не ту модель, базу не трогаем; заодно
    # узнаём, сколько текстов он принимает за раз.
    await embedder.ensure_verified()
    async with pool.acquire() as conn:
        rows = await conn.fetch(_TAKE, max(1, min(settings.batch, embedder.max_batch)))
    result = BatchResult(taken=len(rows))
    if not rows:
        return result
    work = [r for r in rows if not r["excluded"] and is_embeddable(r["text"], settings.min_words)]
    chosen = {r["id"] for r in work}
    skip = [r for r in rows if r["id"] not in chosen]
    vectors: list[tuple[asyncpg.Record, list[float]]] = []
    if work:
        texts = [passage_text(r["text"], sender_name=r["sender_name"]) for r in work]
        try:
            vectors = list(zip(work, await embedder.embed_passages(texts), strict=True))
        except EmbedderError as exc:
            if not exc.rejected:
                raise
            # Сервер отверг пачку из-за её содержимого. Ищем виновника по одному, иначе одно
            # неудобное сообщение остановило бы очередь навсегда.
            for row, text in zip(work, texts, strict=True):
                try:
                    vectors.append((row, (await embedder.embed_passages([text]))[0]))
                except EmbedderError as single:
                    if not single.rejected:
                        raise
                    logger.warning("сервер эмбеддингов не принял сообщение %s (%s) — пропущено",
                                   row["id"], single.reason)
                    skip.append(row)
    async with pool.acquire() as conn:
        if vectors:
            result.embedded = _count(await conn.execute(
                _STORE, embedder.model, [r["id"] for r, _ in vectors],
                [vector_literal(v) for _, v in vectors], [r["digest"] for r, _ in vectors],
            ))
        if skip:
            result.skipped = _count(await conn.execute(
                _SKIP, embedder.model, [r["id"] for r in skip], [r["digest"] for r in skip],
            ))
    return result


async def run_worker(
    pool: asyncpg.Pool, embedder: Embedder, settings: WorkerSettings, *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Бесконечный цикл счётчика. Любой сбой — пауза с удвоением и повтор; сам не падает."""
    failures = 0
    stale_done = False
    while True:
        try:
            if not stale_done:
                returned = await reset_stale(pool, embedder.model, sleep=sleep)
                stale_done = True
                if returned:
                    logger.info("модель эмбеддингов сменилась: %d сообщений возвращено в очередь", returned)
            result = await embed_batch(pool, embedder, settings)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures += 1
            delay = settings.backoff_max if getattr(exc, "mismatch", False) else backoff_delay(failures, settings)
            reason = exc.reason if isinstance(exc, EmbedderError) else type(exc).__name__
            # Журнал не засоряем: первый сбой и затем каждый десятый.
            if failures == 1 or failures % 10 == 0:
                logger.warning("эмбеддинги: сбой (%s), подряд: %d, следующая попытка через %.0f с",
                               reason, failures, delay)
            await sleep(delay)
            continue
        if failures:
            logger.info("эмбеддинги: сервер снова отвечает")
            failures = 0
        if result.taken:
            logger.debug("эмбеддинги: взято %d, посчитано %d, пропущено %d",
                         result.taken, result.embedded, result.skipped)
        await sleep(settings.pause if result.taken else settings.idle)


# --- состояние для владельца ---

_COUNTS = """
SELECT count(*) FILTER (WHERE embedding IS NOT NULL AND embedding_model = $1) AS embedded,
       count(*) FILTER (WHERE embedding_model IS NULL AND kind = 'message' AND deleted_at IS NULL) AS pending,
       count(*) FILTER (WHERE embedding IS NULL AND embedding_model = $1) AS skipped
FROM messages
"""


async def status(request: Request) -> JSONResponse:
    """Состояние индексации: только счётчики, без содержимого переписки."""
    state = request.app.state.shturman
    embedder: Embedder | None = state.extras.get("embedder")
    async with state.ro_pool.acquire() as conn:
        row = await conn.fetchrow(_COUNTS, state.config.embeddings_model)
    reachable = await embedder.healthy() if embedder else None
    # None — всё в порядке или модуль выключен; "unreachable" — сервер не отвечает;
    # "model_mismatch" — отвечает, но отдаёт не ту модель, поэтому векторы не считаются.
    problem = None
    if embedder is not None:
        if not reachable:
            problem = "unreachable"
        elif embedder.problem == "model_mismatch":
            problem = "model_mismatch"
    return JSONResponse({
        "enabled": embedder is not None,
        "model": state.config.embeddings_model,
        "embedded": row["embedded"],
        "pending": row["pending"],
        "skipped": row["skipped"],
        "reachable": reachable,
        "problem": problem,
    })


def routes() -> list[BaseRoute]:
    return [Route("/api/embeddings/status", status, methods=["GET"])]


@contextlib.asynccontextmanager
async def lifespan(state: Any) -> AsyncIterator[None]:
    config: Config = state.config
    if not config.embeddings_url:
        logger.info("эмбеддинги выключены (SHTURMAN_EMBEDDINGS_URL пуст): поиск только полнотекстовый")
        yield
        return
    async with state.pool.acquire() as conn:
        dim = await column_dim(conn)
    if dim != config.embeddings_dim:
        raise ConfigError(
            f"SHTURMAN_EMBEDDINGS_DIM={config.embeddings_dim}, а столбец messages.embedding "
            f"рассчитан на {dim}: смена размерности требует миграции (см. 0004_embeddings.sql)")
    settings = WorkerSettings.from_env()
    embedder = build_embedder(config)
    state.extras["embedder"] = embedder
    state.spawn(run_worker(state.pool, embedder, settings), name="embeddings-worker")
    try:
        yield
    finally:
        state.extras.pop("embedder", None)
        await embedder.aclose()
