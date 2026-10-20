"""Набор для измерения поиска и скрипт `tools/search_eval.py`.

Набор лежит в `tests/search/data/`. Тесты проверяют его целостность (каждый вопрос указывает на
существующее сообщение), то, что в нём нет фамилий, адресов почты, ссылок и телефонов, и что
скрипт измерения проходит путь сервиса от записи в архив до гибридного поиска. Сервер
эмбеддингов подставной.
"""

import argparse
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for sub in ("tools", "tests"):
    if str(ROOT / sub) not in sys.path:
        sys.path.insert(0, str(ROOT / sub))

import search_eval  # noqa: E402

from conftest import DSN  # noqa: E402
from test_embeddings import E5, FakeTEI  # noqa: E402

DATA = ROOT / "tests" / "search" / "data"


def rows(name):
    return search_eval.read_jsonl(DATA / name)


# --- набор ---

def test_short_set_is_whole_and_every_question_has_its_answer():
    corpus, queries = rows("short_corpus.jsonl"), rows("short_queries.jsonl")
    ids = [m["id"] for m in corpus]
    assert len(corpus) == 2695 and len(set(ids)) == len(ids)
    assert len(queries) == 165 and len({q["qid"] for q in queries}) == 165
    by_id = {m["id"]: m for m in corpus}
    for q in queries:
        assert by_id[q["target"]]["kind"] == "target" and q["query"].strip()
        assert q["domain"] in ("biz", "personal")
    assert len({q["target"] for q in queries}) == 165          # у каждого вопроса свой ответ
    assert all(m["sender"].strip() and m["text"].strip() for m in corpus)
    # исходящие владельца набора написаны от одного имени — его скрипт кладёт в «Избранное»
    assert sum(m["sender"] == search_eval.OWNER_NAME for m in corpus) > 100


def test_long_set_is_whole_and_facts_sit_where_questions_say():
    docs, queries = rows("long_docs.jsonl"), rows("long_queries.jsonl")
    by_id = {d["id"]: d for d in docs}
    assert len(docs) == 26 and len(queries) == 78
    for q in queries:
        doc = by_id[q["target"]]
        assert q["where"] in ("head", "tail", "blind")
        assert q["length"] == len(doc["text"]) and 0 <= q["pos"] < len(doc["text"])
    assert min(len(d["text"]) for d in docs) > 1000


@pytest.mark.parametrize("which,messages,questions", [("short", 2695, 165), ("long", 26, 78), ("mixed", 2721, 78)])
def test_sets_load(which, messages, questions):
    corpus, queries = search_eval.load(DATA, which)
    assert (len(corpus), len(queries)) == (messages, questions)
    ids = {m["id"] for m in corpus}
    assert all(q["target"] in ids and isinstance(q["tags"], list) for q in queries)


# Что в набор попасть не должно: фамилии (в наборе люди названы именем и ролью), ссылки, почта,
# телефоны, домены. Имена-отчества и названия городов допустимы.
_FORBIDDEN = [
    (r"https?://|www\.", "ссылка"),
    (r"[\w.+-]+@[\w-]+\.\w+", "почта"),
    (r"\b[\w-]+\.(?:ru|com|org|net|io|рф)\b", "домен"),
    (r"(?:\+7|\b8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}", "телефон"),
    (r"\b(?:ул\.|улица|проспект|пр-т|переулок|пер\.)\s+[А-ЯЁ][а-яё]+\w*,?\s+(?:д\.\s*)?\d+", "адрес"),
    # «Фамилия И. О.» и «И. О. Фамилия»
    (r"\b[А-ЯЁ][а-яё]+(?:ов|ев|ёв|ин|ын|ский|цкий|ова|ева|ина|ская)[а-я]{0,2}\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.", "фамилия с инициалами"),
    (r"\b[А-ЯЁ]\.\s?[А-ЯЁ]\.\s+[А-ЯЁ][а-яё]+(?:ов|ев|ёв|ин|ын|ский|цкий|ова|ева|ина|ская)\b", "инициалы с фамилией"),
]


def test_set_has_no_surnames_links_mail_phones_or_addresses():
    found = []
    for name, keys in (("short_corpus.jsonl", ("sender", "text")), ("short_queries.jsonl", ("query",)),
                       ("long_docs.jsonl", ("title", "text")), ("long_queries.jsonl", ("query",))):
        for row in rows(name):
            for key in keys:
                for pattern, what in _FORBIDDEN:
                    hit = re.search(pattern, row[key], flags=re.IGNORECASE if what in ("ссылка", "домен") else 0)
                    if hit:
                        found.append((name, row.get("id") or row.get("qid"), what, hit.group(0)))
    assert found == []


# Все, от чьего имени написаны сообщения набора, и все заголовки длинных текстов: имя, имя-отчество
# или имя с ролью — без фамилий. Перечень задан явно: новое имя в наборе должно пройти вычитку.
SENDERS = {
    "Автосервис Гена", "Алина Отдел продаж", "Антон IT", "Аркадий Вентиляция", "Борис Геннадьевич",
    "Вадим Лифты", "Глеб Олегович", "Гульнара Секретарь", "Денис Бетон", "Доставка продуктов",
    "Жанна Банк", "Зарина HR", "Игорь Фасады", "Кирилл Маркетинг", "Костя", "Лев Маркович Юрист",
    "Лена сестра", "Мама", "Марат Кровля", "Марина", "Михаил Геодезист", "Наиля Архитектор",
    "Няня Тамара", "Оксана Бухгалтер", "Олег Водитель", "Ольга Сергеевна Классрук",
    "Павел Электрика", "Папа", "Ремонт Саша", "Риелтор Инга", "Ринат Снабжение", "Роман Технадзор",
    "Светлана Сметчик", "Сергей Иванович ГИП", "Серёга", "Сосед Виктор", "Станислав Сантехника",
    "Стоматолог Карина", "Тимур Прораб", "Тренер Руслан", "Тёма", "Фарид Благоустройство",
    "Эльдар Окна", "Юрий Петрович Экспертиза",
}
TITLES = {
    "Алина Отдел продаж", "Борис Геннадьевич", "Вадим Лифты", "Жанна Банк", "Игорь Фасады",
    "Коммерческое предложение", "Костя", "Лев Маркович Юрист", "Лена сестра", "Мама", "Марина",
    "Наиля Архитектор", "Оксана Бухгалтер", "Ольга Сергеевна Классрук", "Отчёт", "Письмо банка",
    "Письмо управляющей компании", "Письмо экспертизы", "Претензия", "Протокол совещания",
    "Ринат Снабжение", "Роман Технадзор", "Сергей Иванович ГИП", "Служебная записка",
    "Стоматолог Карина", "Тимур Прораб",
}


def test_people_in_set_are_named_without_surnames():
    assert {m["sender"] for m in rows("short_corpus.jsonl")} == SENDERS
    assert {d["title"] for d in rows("long_docs.jsonl")} == TITLES


# --- метрики ---

def test_metrics_and_groups():
    m = search_eval.metrics([1, 3, None, 10, 11, None])
    assert m["n"] == 6 and m["r1"] == pytest.approx(1 / 6) and m["r5"] == pytest.approx(2 / 6)
    assert m["r10"] == pytest.approx(3 / 6) and m["mrr10"] == pytest.approx((1 + 1 / 3 + 1 / 10) / 6)
    assert 0.3 < m["ci10"] < 0.5
    groups = search_eval.by_tag([{"tags": ["a"], "domain": "biz"}, {"tags": ["a", "b"]}], [1, None])
    assert groups["a"]["r10"] == 0.5 and groups["b"]["r10"] == 0.0 and groups["biz"]["r1"] == 1.0
    assert search_eval._rank([0.1, 0.9, 0.5], 1) == 1 and search_eval._rank([0.1, 0.9, 0.5], 0) == 3
    assert search_eval._rank([1.0] + [2.0] * 10, 0) is None


# --- скрипт на пути сервиса ---

def small_set(tmp_path):
    """Шесть сообщений и три вопроса в формате набора; подставная модель знает «оси тем»."""
    corpus = [
        ("m0", "Игорь Фасады", "Пришлю смету по фасадам к пятнице, там всё расписано"),
        ("m1", "Оксана Бухгалтер", "Оплата по счёту прошла вчера, подтверждение в почте"),
        ("m2", search_eval.OWNER_NAME, "Встреча с подрядчиком будет во вторник утром"),
        ("m3", "Марина", "Отпуск планируем на август, билеты посмотрю позже"),
        ("m4", "Тимур Прораб", "Договор отправили на подпись сегодня утром"),
        ("m5", "Тимур Прораб", "ок"),
    ]
    queries = [("q0", "какой бюджет назвал подрядчик", "m0"), ("q1", "когда созвон по объекту", "m2"),
               ("q2", "что с контрактом", "m4")]
    (tmp_path / "short_corpus.jsonl").write_text("".join(
        json.dumps({"id": i, "sender": s, "text": t, "kind": "target"}, ensure_ascii=False) + "\n"
        for i, s, t in corpus), encoding="utf-8")
    (tmp_path / "short_queries.jsonl").write_text("".join(
        json.dumps({"qid": i, "query": q, "target": t, "tags": ["promise"], "domain": "biz"}, ensure_ascii=False) + "\n"
        for i, q, t in queries), encoding="utf-8")
    return tmp_path


def args_for(data, **over):
    base = dict(path="service", url="http://tei.test", model=E5, set="short", data=data, dsn=DSN, reset=True,
                during=[0.5], brief=True, json=None)
    base.update(over)
    return argparse.Namespace(**base)


async def test_service_path_runs_from_archive_to_hybrid_search(conn, tmp_path, monkeypatch, capsys):
    tei = FakeTEI()
    monkeypatch.setattr(search_eval, "make_embedder", lambda url, model: tei.embedder(model=model))
    out = await search_eval.run_service(args_for(small_set(tmp_path)))
    by_name = {r["name"]: r for r in out}
    # слов из вопросов в ответах нет: поиск по словам не находит ничего, гибрид находит всё
    assert by_name["только слова"]["r10"] == 0.0
    assert by_name["только смысл (индекс HNSW)"]["r1"] == 1.0
    assert by_name["гибрид — путь сервиса, набор short"]["r1"] == 1.0
    assert "гибрид, посчитано 50% векторов" in by_name
    printed = capsys.readouterr().out
    assert "в архиве сообщений: 6" in printed and "с вектором 5, пропущено 1" in printed
    # сообщения ушли серверу с именем отправителя и приставкой модели, вопросы — со своей
    assert "passage: Игорь Фасады: Пришлю смету по фасадам к пятнице, там всё расписано" in tei.inputs
    assert "query: что с контрактом" in tei.inputs
    # исходящие владельца набора записаны как исходящие
    assert await conn.fetchval("SELECT count(*) FROM messages WHERE is_outgoing") == 1


async def test_service_path_refuses_without_explicit_reset_and_dsn(tmp_path):
    with pytest.raises(SystemExit, match="--reset"):
        await search_eval.run_service(args_for(small_set(tmp_path), reset=False))
    with pytest.raises(SystemExit, match="--dsn"):
        await search_eval.run_service(args_for(tmp_path, dsn=None))


async def test_model_path_ranks_by_exact_cosine(tmp_path, monkeypatch):
    tei = FakeTEI(model="deepvk/USER2-small")
    monkeypatch.setattr(search_eval, "make_embedder", lambda url, model: tei.embedder(model=model))
    out = await search_eval.run_model(args_for(small_set(tmp_path), path="model", model="deepvk/USER2-small"))
    assert out[0]["r1"] == 1.0 and out[0]["n"] == 3
    assert any(text.startswith("search_document: Игорь Фасады: ") for text in tei.inputs)
    assert "search_query: что с контрактом" in tei.inputs
