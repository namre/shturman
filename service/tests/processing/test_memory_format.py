"""Формат страниц памяти, этап 2: блоки decisions и facts, страницы проектов и профиля. Без базы."""

import pytest

from shturman.processing import pages

M = pages.MARKERS

# Страница версии 0.0.7 — так её пишет сборка до этапа 2. Владелец оставил в своём блоке строки,
# похожие на метки новых блоков: это его текст, а не разметка.
OLD = (
    "---\n"
    "entity_id: person:12\n"
    "type: person\n"
    "aliases: [Иван Петров]\n"
    "updated: 2026-10-07\n"
    "tags: [подрядчики]\n"
    "---\n"
    "# Иван Петров\n"
    "\n"
    f"{M['summary']}\n"
    "- Подрядчик по фасадам [сообщение](msg:1) (сказал собеседник)\n"
    "\n"
    f"{M['owner']}\n"
    "Не писать после 19:00.\n"
    "<!-- facts: это просто заметка владельца -->\n"
    "<!-- decisions -->\n"
    "\n"
    f"{M['commitments']}\n"
    "_Обязательств нет._\n"
    "\n"
    f"{M['timeline']}\n"
    "- 2026-10-06 — что-то [сообщение](msg:1) (сказал собеседник) <!-- id:c1 -->\n"
    "- рукой владельца: <!-- facts: и это в хронологии -->\n"
)


def test_old_page_reads_and_renders_byte_for_byte():
    page = pages.parse(OLD)
    assert page.facts is None and page.decisions is None
    assert page.owner == "Не писать после 19:00.\n<!-- facts: это просто заметка владельца -->\n<!-- decisions -->\n\n"
    assert page.commitments == "_Обязательств нет._"
    assert page.timeline.endswith("<!-- facts: и это в хронологии -->\n")
    assert page.chats == [] and page.participants == []
    assert pages.render(page) == OLD


def test_new_blocks_sit_between_commitments_and_timeline():
    page = pages.parse(OLD)
    owner_before = page.owner
    page.facts = pages.facts_block([
        {"slot": "должность", "text": "директор по развитию", "since": "2026-10-01", "message_id": 7, "origin": "other"},
        {"slot": None, "text": "любит <b>звонки</b> [x](msg:9)", "since": "2026-10-02", "message_id": 8,
         "origin": "owner"}])
    page.decisions = pages.decisions_block([{"day": "2026-10-03", "text": "фасад — керамогранит", "message_id": 5,
                                             "origin": "owner"}])
    text = pages.render(page)
    assert text.index(M["commitments"]) < text.index(M["decisions"]) < text.index(M["facts"]) < text.index(M["timeline"])
    again = pages.parse(text)
    assert again.owner == owner_before                          # блок владельца — байт в байт
    assert again.facts == page.facts and again.decisions == page.decisions
    assert again.facts.splitlines()[0] == ("- должность: директор по развитию (с 2026-10-01) [сообщение](msg:7) "
                                           "(сказал собеседник)")
    # чужой текст не создаёт ни разметки, ни ссылки на сообщение
    assert pages.refs(again.facts) == [7, 8] and "\\<b\\>" in again.facts
    assert pages.render(again) == text
    assert again.decisions == "- 2026-10-03 — фасад — керамогранит [сообщение](msg:5) (сказал владелец)"


def test_only_one_of_the_new_blocks_is_fine():
    page = pages.parse(OLD)
    page.facts = pages.NO_FACTS
    assert pages.parse(pages.render(page)).facts == pages.NO_FACTS
    page.facts, page.decisions = None, pages.NO_DECISIONS
    parsed = pages.parse(pages.render(page))
    assert parsed.decisions == pages.NO_DECISIONS and parsed.facts is None


@pytest.mark.parametrize("tail", [
    f"{M['facts']}\nx\n\n{M['decisions']}\ny\n\n",          # не в том порядке
    f"{M['facts']}\nx\n\n{M['facts']}\ny\n\n",              # дважды
])
def test_wrong_order_of_new_blocks_freezes_the_file(tail):
    text = OLD.replace(f"{M['timeline']}\n", tail + f"{M['timeline']}\n", 1)
    with pytest.raises(pages.PageError, match="decisions и facts"):
        pages.parse(text)


def test_marker_after_the_timeline_is_still_refused_for_old_blocks():
    with pytest.raises(pages.PageError):
        pages.parse(OLD + f"{M['commitments']}\n")


def test_project_page_front_matter_carries_chats_and_participants():
    page = pages.Page(entity_id="project:3", type=pages.PROJECT, aliases=["Северный"], updated="2026-10-07",
                      title="ЖК Северный", chats=["Стройка: Северный", 'чат "с кавычками"'],
                      participants=["Иван Петров"], owner="\n", commitments=pages.NO_COMMITMENTS,
                      decisions=pages.NO_DECISIONS, facts=pages.NO_FACTS, summary=pages.NO_SUMMARY)
    text = pages.render(page)
    assert '\nchats: ["Стройка: Северный", "чат \\"с кавычками\\""]\nparticipants: [Иван Петров]\n' in text
    again = pages.parse(text)
    assert again.chats == page.chats and again.participants == ["Иван Петров"] and again.type == "project"
    assert pages.render(again) == text
    # у пустого проекта списки всё равно в шапке; у человека их нет
    page.chats = page.participants = []
    assert "chats: []\nparticipants: []\n" in pages.render(page)
    assert "chats:" not in pages.render(pages.parse(OLD))


def test_paths_of_new_pages():
    assert pages.project_path("ЖК «Северный» / 2 очередь", 3) == "projects/жк-северный-2-очередь-3.md"
    assert pages.project_path("!!!", 4) == "projects/project-4.md"
    assert pages.OWNER_PATH == "owner/profile.md"
    for good in ("projects/жк-северный-3.md", "owner/profile.md", "people/иван-12.md"):
        assert pages.is_page_path(good)
    for bad in ("owner/other.md", "projects/../owner/profile.md", "projects/x/y.md", "owner/profile.md.bak",
                "secrets/profile.md", "projects/.md"):
        assert not pages.is_page_path(bad)


def test_files_of_all_kinds_are_written_and_listed(tmp_path):
    for rel in ("people/иван-1.md", "projects/северный-2.md", pages.OWNER_PATH):
        pages.write_page(tmp_path, rel, "текст")
        assert pages.read_page(tmp_path, rel) == "текст"
    assert pages.list_files(tmp_path) == ["owner/profile.md", "people/иван-1.md", "projects/северный-2.md"]


def test_lint_checks_type_and_sources_of_new_blocks():
    page = pages.parse(OLD)
    page.facts = "- должность: директор без ссылки"
    page.decisions = "- 2026-10-01 — решение [сообщение](msg:3) (сказал владелец)"
    found, parsed = pages.lint_text(pages.render(page), entity_id="person:12")
    assert ("no_source", "facts: строка 1 без ссылки на сообщение") in found and parsed is not None
    found, _ = pages.lint_text(pages.render(page), entity_id="person:12", entity_type="project")
    assert ("front_matter", "поле type должно быть project") in found


def test_owner_text_with_new_markers_is_refused():
    assert pages.has_marker("ок\n<!-- facts: взлом -->") and pages.has_marker("<!--decisions-->")
    assert not pages.has_marker("факты: <b>нет</b>")
