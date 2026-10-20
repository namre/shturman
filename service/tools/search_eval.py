#!/usr/bin/env python3
"""Измерение поиска по смыслу на размеченном наборе вопросов.

Что меряет: для каждого вопроса известно одно сообщение-ответ; считается, на каком месте выдачи
оно оказалось. Итог — доля вопросов, у которых ответ попал в первые 1, 5 и 10 результатов
(R@1, R@5, R@10), и средний обратный ранг в первой десятке (MRR@10). Отдельно по меткам вопросов.

Два пути (`--path`):
  model    только модель: тексты и вопросы идут в сервер эмбеддингов тем же клиентом и с теми же
           приставками, что у сервиса (`shturman.embeddings`), поиск — точный косинус по всем
           векторам. База не нужна. Так сравнивают модели между собой.
  service  настоящий путь сервиса: набор записывается в архив обычным путём записи, векторы
           считает фоновый счётчик, вопросы идут через `shturman.retrieval.find` — тот же гибридный
           поиск (слова + смысл, индекс HNSW), которым пользуется агент. Печатаются три строки:
           только слова, только смысл, гибрид. Нужна ОТДЕЛЬНАЯ пустая база: с ключом `--reset`
           скрипт пересоздаёт в ней схему `public`. Базу экземпляра сюда подставлять нельзя.

Наборы (`--set`), каталог `service/tests/search/data/`:
  short  2695 коротких сообщений (медиана 50 знаков) и 165 вопросов к ним; у 141 вопроса нет
         ни одного общего корня с ответом — поиск по словам на таком наборе почти бессилен
         по построению;
  long   26 длинных текстов (1,4–6 тысяч знаков: страницы о людях, письма, личные сообщения)
         и 78 вопросов: к началу текста, к факту в глубине с подсказкой темы и «слепой» вопрос
         к факту в глубине;
  mixed  длинные тексты среди коротких сообщений, вопросы — к длинным.

Сервер эмбеддингов — работающий TEI с нужной моделью (`--url`). Имя модели (`--model`) должно
совпадать с тем, под которым сервер её отдаёт: клиент сверяет его так же, как сервис.

Примеры (из каталога service/):
  python tools/search_eval.py --path model --url http://127.0.0.1:8080 --model deepvk/USER2-small
  python tools/search_eval.py --path service --url http://127.0.0.1:8080 \\
      --model intfloat/multilingual-e5-small --dsn postgresql://postgres@127.0.0.1:5432/search_eval --reset
  python tools/search_eval.py --path service … --during 0.25 0.5   # качество посреди пересчёта

На сервере контейнер с моделью закрыт от интернета и от хоста, поэтому путь `model` запускают
изнутри контейнера сервиса; скрипт и набор в образ не входят, их копируют (docs/search.md):
  docker exec shturman-service python /tmp/search_eval.py --path model --url http://embeddings:80 \\
      --model intfloat/multilingual-e5-small --data /tmp/search-data

Текст набора никуда не отправляется, кроме указанного сервера эмбеддингов и указанной базы.
Набор синтетический и написан одним автором, имена и компании вымышлены; 165 вопросов дают
разброс около ±8 процентных пунктов. Числа на нём — ориентир для сравнения моделей, а не оценка
качества на настоящей переписке.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from shturman import embeddings  # noqa: E402
from shturman.embeddings import Embedder, EmbedderError, WorkerSettings  # noqa: E402

DATA = HERE.parent / "tests" / "search" / "data"
OWNER_ID = 1000
OWNER_NAME = "Глеб Олегович"       # от его имени в наборе написаны исходящие сообщения
T0 = datetime(2026, 1, 12, 9, 0, tzinfo=timezone.utc)
TOP = 10


# --- набор ---

def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def load(data: Path, which: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Сообщения {id, sender, text} и вопросы {qid, query, target, tags}."""
    short = read_jsonl(data / "short_corpus.jsonl")
    if which == "short":
        return short, read_jsonl(data / "short_queries.jsonl")
    docs = [{"id": d["id"], "sender": d["title"], "text": d["text"]} for d in read_jsonl(data / "long_docs.jsonl")]
    queries = read_jsonl(data / "long_queries.jsonl")
    for q in queries:
        q["tags"] = [q["where"]]
    if which == "long":
        return docs, queries
    # mixed: длинные тексты равномерно среди коротких сообщений
    step = max(1, len(short) // len(docs))
    mixed = list(short)
    for n, doc in enumerate(docs):
        mixed.insert(n * (step + 1), doc)
    return mixed, queries


# --- метрики ---

def metrics(ranks: Sequence[int | None]) -> dict[str, float]:
    """ranks — место ответа в выдаче (с единицы) или None, если его нет в первой десятке."""
    n = len(ranks) or 1
    r10 = sum(1 for r in ranks if r is not None and r <= 10) / n
    return {
        "n": len(ranks),
        "r1": sum(1 for r in ranks if r == 1) / n,
        "r5": sum(1 for r in ranks if r is not None and r <= 5) / n,
        "r10": r10,
        "mrr10": sum(1 / r for r in ranks if r is not None and r <= 10) / n,
        # полуширина 95-процентного интервала для R@10 (нормальное приближение)
        "ci10": 1.96 * math.sqrt(max(r10 * (1 - r10), 1e-9) / n),
    }


def by_tag(queries: Sequence[dict[str, Any]], ranks: Sequence[int | None]) -> dict[str, dict[str, float]]:
    groups: dict[str, list[int | None]] = {}
    for q, rank in zip(queries, ranks, strict=True):
        for tag in list(q.get("tags") or []) + ([q["domain"]] if q.get("domain") else []):
            groups.setdefault(tag, []).append(rank)
    return {tag: metrics(found) for tag, found in sorted(groups.items())}


def line(name: str, m: dict[str, float]) -> str:
    return (f"{name:<34} R@1 {m['r1']:.3f}  R@5 {m['r5']:.3f}  R@10 {m['r10']:.3f} ±{m['ci10']:.3f}"
            f"  MRR@10 {m['mrr10']:.3f}  (вопросов: {m['n']})")


def report(name: str, queries: Sequence[dict[str, Any]], ranks: Sequence[int | None], *, tags: bool) -> dict[str, Any]:
    total = metrics(ranks)
    print(line(name, total))
    groups = by_tag(queries, ranks)
    if tags:
        for tag, m in groups.items():
            print("    " + line(tag, m))
    return {"name": name, **total, "tags": groups}


# --- сервер эмбеддингов ---

def make_embedder(url: str, model: str) -> Embedder:
    return Embedder(url, model, 384, embeddings.profile_for(model))


class CountingEmbedder:
    """Обёртка для пути сервиса: считает вопросы, оставшиеся без смысловой ветки.

    `retrieval.find` при сбое или опоздании сервера молча ищет только по словам — так и задумано
    для владельца, но в измерении такие вопросы нужно видеть, иначе числа занижены незаметно.
    """

    def __init__(self, inner: Embedder) -> None:
        self.inner, self.model, self.failed = inner, inner.model, 0

    async def embed_query(self, text: str) -> list[float]:
        try:
            # Срок больше рабочих двух секунд: меряется качество выдачи, а не скорость стенда.
            return await self.inner.embed_query(text, timeout=30.0)
        except EmbedderError:
            self.failed += 1
            raise


# --- путь «только модель» ---

def _rank(scores: Sequence[float], target: int) -> int | None:
    best = scores[target]
    place = 1 + sum(1 for s in scores if s > best)
    return place if place <= TOP else None


async def run_model(args: argparse.Namespace) -> list[dict[str, Any]]:
    messages, queries = load(args.data, args.set)
    index = {m["id"]: n for n, m in enumerate(messages)}
    embedder = make_embedder(args.url, args.model)
    try:
        await embedder.verify()
        texts = [embeddings.passage_text(m["text"], sender_name=m["sender"]) for m in messages]
        started = time.monotonic()
        vectors: list[list[float]] = []
        settings = WorkerSettings()
        batch: list[str] = []
        size = 0
        for text in texts + [None]:      # None — «дослать остаток»
            if text is None or (batch and (len(batch) >= embedder.max_batch or size + len(text) > settings.batch_chars)):
                vectors += await embedder.embed_passages(batch)
                batch, size = [], 0
            if text is not None:
                batch.append(text)
                size += len(text)
        spent = time.monotonic() - started
        print(f"модель {args.model}: {len(texts)} текстов за {spent:.1f} с ({len(texts) / spent:.1f} в секунду)")
        qvectors = [await embedder.embed_query(q["query"], timeout=30.0) for q in queries]
    finally:
        await embedder.aclose()
    try:
        import numpy as np
        scores = (np.asarray(qvectors, dtype="float32") @ np.asarray(vectors, dtype="float32").T).tolist()
    except ImportError:      # без numpy — то же самое, но медленно
        scores = [[sum(a * b for a, b in zip(q, v)) for v in vectors] for q in qvectors]
    ranks = [_rank(row, index[q["target"]]) for q, row in zip(queries, scores, strict=True)]
    print()
    return [report(f"только модель, набор {args.set}", queries, ranks, tags=not args.brief)]


# --- путь сервиса ---

_SEMANTIC_ONLY = """
SELECT m.tg_message_id
FROM messages m JOIN chats c ON c.id = m.chat_id
WHERE m.embedding IS NOT NULL AND m.embedding_model = $2 AND m.deleted_at IS NULL
  AND m.agent_visible AND NOT c.excluded
ORDER BY m.embedding <=> $1::text::halfvec
LIMIT $3
"""


async def _load_archive(conn: Any, messages: Sequence[dict[str, Any]]) -> None:
    from shturman import store
    from shturman.records import ChatRecord, MessageRecord

    account_id = await store.ensure_account(conn, OWNER_ID, OWNER_NAME, "owner")
    senders: dict[str, int] = {}
    chats: dict[str, int] = {}
    rows = []
    for n, message in enumerate(messages):
        sender = message["sender"]
        if sender not in senders:
            senders[sender] = OWNER_ID if sender == OWNER_NAME else 2000 + len(senders)
        if sender not in chats:
            # Исходящие владельца лежат в чате «Избранное»; остальные — по чату на собеседника.
            peer = senders[sender]
            chats[sender], _ = await store.ensure_chat(
                conn, account_id, ChatRecord("user", peer, "saved_messages" if peer == OWNER_ID else "personal_chat", sender))
        rows.append((chats[sender], MessageRecord(
            tg_message_id=n + 1, sent_at=T0 + timedelta(minutes=n), kind="message", sender_class="user",
            sender_tg_id=senders[sender], sender_name=sender, text=message["text"], entities=None,
            reply_to_tg_id=None, forwarded_from=None, edited_at=None, media_type=None, media_path=None,
            service_action=None)))
    for start in range(0, len(rows), 500):
        await store.upsert_messages(conn, rows[start:start + 500], source="import", owner_tg_id=OWNER_ID)


async def _ask(state: Any, conn: Any, queries: Sequence[dict[str, Any]], number: dict[str, int]) -> list[int | None]:
    from shturman import retrieval

    ranks: list[int | None] = []
    for q in queries:
        rows = await retrieval.find(state, conn, q["query"], limit=TOP)
        found = [r["tg_message_id"] for r in rows]
        target = number[q["target"]]
        ranks.append(found.index(target) + 1 if target in found else None)
    return ranks


async def _ask_semantic(embedder: CountingEmbedder, conn: Any, queries: Sequence[dict[str, Any]],
                        number: dict[str, int]) -> list[int | None]:
    ranks: list[int | None] = []
    async with conn.transaction():
        await conn.execute("SET LOCAL hnsw.ef_search = 200")
        for q in queries:
            vector = embeddings.vector_literal(await embedder.embed_query(q["query"]))
            found = [r["tg_message_id"] for r in await conn.fetch(_SEMANTIC_ONLY, vector, embedder.model, TOP)]
            target = number[q["target"]]
            ranks.append(found.index(target) + 1 if target in found else None)
    return ranks


async def run_service(args: argparse.Namespace) -> list[dict[str, Any]]:
    import asyncpg

    from shturman import db

    if not args.dsn:
        raise SystemExit("для --path service нужна отдельная база: --dsn postgresql://… и ключ --reset")
    if not args.reset:
        raise SystemExit("ключ --reset обязателен: скрипт пересоздаёт схему public в указанной базе. "
                         "Базу экземпляра сюда подставлять нельзя.")
    messages, queries = load(args.data, args.set)
    number = {m["id"]: n + 1 for n, m in enumerate(messages)}
    out: list[dict[str, Any]] = []
    conn = await asyncpg.connect(args.dsn)
    pool = await asyncpg.create_pool(args.dsn, min_size=1, max_size=3)
    inner = make_embedder(args.url, args.model)
    embedder = CountingEmbedder(inner)
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await db.migrate(conn)
        await _load_archive(conn, messages)
        total = await conn.fetchval("SELECT count(*) FROM messages")
        print(f"в архиве сообщений: {total}; модель {args.model}")

        words = SimpleNamespace(extras={})
        hybrid = SimpleNamespace(extras={"embedder": embedder})
        out.append(report("только слова", queries, await _ask(words, conn, queries, number), tags=False))

        # Фоновый счётчик сервиса, шаг за шагом; по дороге — замеры «посреди пересчёта».
        settings = WorkerSettings(pause=0.0)
        marks = sorted(set(args.during or []))
        started = time.monotonic()
        done = 0
        while True:
            step = await embeddings.embed_batch(pool, inner, settings)
            if not step.taken:
                break
            done += step.taken
            while marks and done >= marks[0] * total:
                share = marks.pop(0)
                name = f"гибрид, посчитано {share:.0%} векторов"
                out.append(report(name, queries, await _ask(hybrid, conn, queries, number), tags=False))
        spent = time.monotonic() - started
        count = await embeddings.counters(conn, args.model)
        print(f"счётчик: {done} сообщений за {spent:.1f} с ({done / max(spent, 1e-9):.0f} в секунду вместе с "
              f"замерами по дороге); с вектором {count['embedded']}, пропущено {count['skipped']}")

        out.append(report("только смысл (индекс HNSW)", queries,
                          await _ask_semantic(embedder, conn, queries, number), tags=False))
        print()
        out.append(report(f"гибрид — путь сервиса, набор {args.set}", queries,
                          await _ask(hybrid, conn, queries, number), tags=not args.brief))
        if embedder.failed:
            print(f"\nВНИМАНИЕ: {embedder.failed} обращений к серверу эмбеддингов не удались — такие вопросы "
                  "в гибриде искались только по словам, числа занижены.")
    finally:
        await inner.aclose()
        await pool.close()
        await conn.close()
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Измерение поиска по смыслу на размеченном наборе.")
    parser.add_argument("--path", choices=["model", "service"], default="model")
    parser.add_argument("--url", required=True, help="адрес сервера эмбеддингов (TEI)")
    parser.add_argument("--model", required=True, help="имя модели, как её отдаёт сервер")
    parser.add_argument("--set", choices=["short", "long", "mixed"], default="short")
    parser.add_argument("--data", type=Path, default=DATA, help="каталог набора")
    parser.add_argument("--dsn", help="отдельная пустая база для --path service")
    parser.add_argument("--reset", action="store_true", help="разрешить пересоздать схему public в базе --dsn")
    parser.add_argument("--during", type=float, nargs="*", metavar="ДОЛЯ",
                        help="замерить гибрид, когда посчитана эта доля векторов (например 0.25 0.5)")
    parser.add_argument("--brief", action="store_true", help="без разбивки по меткам вопросов")
    parser.add_argument("--json", type=Path, help="записать результат в файл JSON")
    args = parser.parse_args()
    result = asyncio.run(run_service(args) if args.path == "service" else run_model(args))
    if args.json:
        args.json.write_text(json.dumps({"model": args.model, "set": args.set, "path": args.path,
                                         "results": result}, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
