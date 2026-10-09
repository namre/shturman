"""Извлечение фактов, решений и упоминаний проектов: проверка ответа модели текстом. Без базы."""

from datetime import datetime, timedelta, timezone

import pytest

from shturman.processing import extract
from shturman.processing.extract import Episode, Msg

T = datetime(2026, 10, 6, 11, 0, tzinfo=timezone.utc)


def m(mid, text, *, peer=2001, out=False, forwarded=False, by_service=False, minutes=0):
    return Msg(id=mid, chat_id=1, sent_at=T + timedelta(minutes=minutes), sender_peer_id=None if out else peer,
               sender_name=None if out else "Иван Петров", is_outgoing=out, text=text, forwarded=forwarded,
               by_service=by_service)


EPISODE = Episode(1, [
    m(10, "Я теперь директор по развитию в «Альфе», мой новый номер +7 999 123-45-67"),
    m(11, "По ЖК Северный решили: фасад делаем из керамогранита, бюджет 12 млн", out=True, minutes=1),
    m(12, "Переслано: цена 5 млн", forwarded=True, minutes=2),
    m(13, "Автоответ: буду в офисе с понедельника", out=True, by_service=True, minutes=3),
    m(14, "Наверное, Пётр теперь главный инженер", minutes=4),
    m(15, "Пароль от портала 12345, код подтверждения 777", minutes=5),
])
LABELS = extract.speaker_labels(EPISODE)


def fact(message, quote, about, text, *, slot=None, kind="fact", project=None):
    return {"message": message, "source_quote": quote, "about": about, "text": text, "slot": slot,
            "kind": kind, "project": project}


def test_labels_are_the_episode_speakers():
    assert LABELS == {("peer", 2001): "У1", ("owner",): "ВЛАДЕЛЕЦ"}


def test_facts_are_checked_against_the_message_text():
    parsed = {"facts": [
        fact(1, "Я теперь директор по развитию в «Альфе»", "У1", "директор по развитию", slot="Должность"),
        fact(1, "мой новый номер +7 999 123-45-67", "У1", "+7 999 123-45-67", slot="номер телефона"),
        fact(2, "фасад делаем из керамогранита", "ПРОЕКТ", "фасад из керамогранита", kind="decision",
             project="ЖК Северный"),
        fact(2, "бюджет 12 млн", "ПРОЕКТ", "бюджет 12 млн", slot="бюджет", project="ЖК «Северный»"),
        fact(2, "решили", "ВЛАДЕЛЕЦ", "владелец решает по фасадам", kind="decision"),   # решение без проекта
    ]}
    found, dropped = extract.validate_facts(parsed, EPISODE, LABELS)
    assert [(f.about, f.slot, f.kind, f.project, f.origin) for f in found] == [
        ("peer", "должность", "fact", None, "other"),
        ("peer", "телефон", "fact", None, "other"),
        ("project", None, "decision", "ЖК Северный", "owner"),
        ("project", "бюджет", "fact", "ЖК «Северный»", "owner"),
        ("owner", None, "fact", None, "owner"),
    ]
    assert found[0].speaker_key == ("peer", 2001) and found[0].message.id == 10
    assert sum(dropped.values()) == 0


@pytest.mark.parametrize("item, reason", [
    (fact(1, "директор по маркетингу", "У1", "директор"), "ungrounded_quote"),      # цитаты нет в сообщении
    (fact(7, "что-то", "У1", "что-то"), "bad_index"),
    (fact(0, "что-то", "У1", "что-то"), "bad_index"),
    (fact(3, "цена 5 млн", "У1", "цена 5 млн"), "forwarded"),
    (fact(4, "буду в офисе с понедельника", "ВЛАДЕЛЕЦ", "в офисе"), "by_service"),
    (fact(5, "Пётр теперь главный инженер", "У1", "Пётр — главный инженер"), "hedged"),
    (fact(6, "Пароль от портала 12345", "У1", "пароль 12345"), "sensitive"),
    (fact(1, "директор по развитию", "У7", "директор"), "bad_subject"),             # нет такого участника
    (fact(1, "директор по развитию", "ПРОЕКТ", "директор"), "bad_subject"),         # ПРОЕКТ без названия
    (fact(1, "директор по развитию", "У1", "  "), "empty"),
    ({"message": "1", "source_quote": "x", "about": "У1", "text": "x"}, "malformed"),
    ("строка", "malformed"),
])
def test_bad_facts_are_dropped(item, reason):
    found, dropped = extract.validate_facts({"facts": [item]}, EPISODE, LABELS)
    assert found == [] and dropped[reason] == 1, dropped


def test_context_messages_cannot_be_cited():
    episode = Episode(1, [m(21, "Спасибо", minutes=10)], context=[m(20, "Я теперь в «Бете»")])
    found, dropped = extract.validate_facts(
        {"facts": [fact(1, "Я теперь в «Бете»", "У1", "работает в «Бете»")]}, episode,
        extract.speaker_labels(episode))
    assert found == [] and dropped["ungrounded_quote"] == 1


def test_fact_limits_unknown_slots_and_old_answers():
    many = {"facts": [fact(1, "директор по развитию", "У1", f"факт номер {n}") for n in range(12)]}
    found, dropped = extract.validate_facts(many, EPISODE, LABELS)
    assert len(found) == extract.MAX_FACTS and dropped["over_limit"] == 12 - extract.MAX_FACTS
    weird = {"facts": [fact(1, "директор по развитию", "У1", "директор", slot="любимый цвет")]}
    assert extract.validate_facts(weird, EPISODE, LABELS)[0][0].slot is None
    assert extract.validate_facts({"commitments": []}, EPISODE, LABELS) == ([], extract.validate_facts({}, EPISODE, LABELS)[1])
    assert extract.validate_facts({"facts": "x"}, EPISODE, LABELS)[1]["malformed"] == 1
    # ссылка в формулировке — не ссылка
    linked = {"facts": [fact(1, "директор по развитию", "У1", "директор, см. https://evil.example/x")]}
    assert extract.validate_facts(linked, EPISODE, LABELS)[0][0].text == "директор, см. [ссылка]"


def test_project_mentions_must_be_in_the_text():
    parsed = {"projects": ["ЖК Северный", "  «жк северный»  ", "Проект Омега", "<b>", 5, "https://x.example",
                           "А" * 200]}
    found, dropped = extract.validate_projects(parsed, EPISODE)
    assert [(x.title, x.message.id) for x in found] == [("ЖК Северный", 11)]
    assert dropped == {"malformed": 3, "ungrounded": 2, "over_limit": 0}      # «<b>» стал ‹b› и не найден
    # пересланное и написанное сервисом названием проекта не служат
    assert extract.find_mention("цена", EPISODE) is None or extract.find_mention("цена", EPISODE).message.id != 12
    assert extract.find_mention("северн", EPISODE) is None        # только целые слова


def test_commitment_carries_the_project_label():
    episode = Episode(1, [m(30, "Пришлю смету по ЖК Северный к пятнице")])
    found, _ = extract.validate_extraction(
        {"commitments": [{"message": 1, "source_quote": "Пришлю смету по ЖК Северный к пятнице",
                          "what": "прислать смету", "project": " ЖК Северный "}]},
        episode, extract.speaker_labels(episode))
    assert found[0].project == "ЖК Северный"


@pytest.mark.parametrize("text, signal", [
    ("Цена выросла до 15 тыс за метр", True),
    ("Решили перенести монтаж", True),
    ("Мой новый номер 8 912 000-00-00", True),
    ("Площадь 120 м2", True),
    ("Я теперь работаю в другой компании", True),
    ("Встречаемся 12.10", False),             # голая дата — не факт
    ("Пришлю через 2 дня", False),
    ("Бюджет 12 млн, теперь я отвечаю за закупки", True),
    ("Спасибо, до встречи", False),
    ("Смету отправил, посмотрите почту", False),
])
def test_fact_signal(text, signal):
    assert extract.has_fact_signal(Episode(1, [m(1, text)])) is signal
    assert extract.has_memory_signal(Episode(1, [m(1, text)])) is (signal or extract.has_promise_signal(Episode(1, [m(1, text)])))


def test_forwarded_text_gives_no_fact_signal():
    assert not extract.has_fact_signal(Episode(1, [m(1, "Цена 15 тыс", forwarded=True)]))


def test_prompt_version_and_instructions():
    assert extract.PROMPT_VERSION == "3"
    assert '"facts": [' in extract.EXTRACT_INSTRUCTIONS and '"projects": [' in extract.EXTRACT_INSTRUCTIONS
    assert "Пароли, коды подтверждения и номера карт не извлекай" in extract.EXTRACT_INSTRUCTIONS
    text = extract.build_extract_input(EPISODE, LABELS, timezone.utc, chat_kind="личный",
                                       projects=["ЖК Северный", "<script>", ""])
    assert "Проекты владельца: ЖК Северный; ‹script›." in text and text.endswith("</переписка>")
    assert "Проекты владельца" not in extract.build_extract_input(EPISODE, LABELS, timezone.utc, chat_kind="личный")
