"""Формат страницы: разбор и сборка файла, неприкосновенность блока владельца, имена файлов."""

import os
import stat

import pytest

from shturman.processing import pages, pages_git

M = pages.MARKERS


def sample(owner="Не писать ему после 19:00.\n\n", timeline="") -> pages.Page:
    return pages.Page(
        entity_id="person:12", type="person", aliases=["Иван Петров", "Ваня", "Петров, Иван", "@ipetrov", "true"],
        updated="2026-10-06", title="Иван Петров",
        summary=pages.summary_block([{"text": "Руководит подрядчиком по фасадам", "sources": [5, 7],
                                      "origin": "other", "disputed": False}]),
        owner=owner,
        commitments=pages.commitments_block([{"what": "прислать смету", "due": "2026-10-09", "status": "ждём",
                                              "message_id": 5}]),
        timeline=timeline)


def test_render_matches_the_documented_layout():
    text = pages.render(sample(timeline=pages.timeline_line("2026-09-12", "согласовали перенос", [5], "owner", "c1") + "\n"))
    assert text == (
        "---\n"
        "entity_id: person:12\n"
        "type: person\n"
        'aliases: [Иван Петров, Ваня, "Петров, Иван", "@ipetrov", "true"]\n'
        "updated: 2026-10-06\n"
        "---\n"
        "# Иван Петров\n"
        "\n"
        f"{M['summary']}\n"
        "- Руководит подрядчиком по фасадам [сообщение](msg:5) [сообщение](msg:7) (сказал собеседник)\n"
        "\n"
        f"{M['owner']}\n"
        "Не писать ему после 19:00.\n"
        "\n"
        f"{M['commitments']}\n"
        "| Что | Срок | Статус | Источник |\n"
        "|---|---|---|---|\n"
        "| прислать смету | 2026-10-09 | ждём | [сообщение](msg:5) |\n"
        "\n"
        f"{M['timeline']}\n"
        "- 2026-09-12 — согласовали перенос [сообщение](msg:5) (сказал владелец) <!-- id:c1 -->\n"
    )
    assert "будут затёрты" in M["commitments"]          # о перезаписи сказано в самом блоке


def test_parse_and_render_round_trip():
    page = sample(timeline="- 2026-09-12 — раз [сообщение](msg:5) (сказал владелец) <!-- id:c1 -->\n")
    text = pages.render(page)
    again = pages.parse(text)
    assert again == page
    assert pages.render(again) == text
    # пустая новая страница тоже устойчива
    empty = pages.Page(entity_id="person:1", updated="2026-10-06", title="Без имени",
                       summary=pages.summary_block([]), commitments=pages.commitments_block([]))
    assert pages.parse(pages.render(empty)) == empty
    assert pages.NO_SUMMARY in pages.render(empty) and pages.NO_COMMITMENTS in pages.render(empty)


HOSTILE_OWNER = (
    "Мои заметки.\r\n"
    "<!-- timeline: это моя строка, не метка -->\n"
    "<!-- commitments -->\n"
    "| Что | Срок |\n"
    "<!-- owner: ещё раз -->\n"
    "<!-- summary -->\n"
    "  хвостовые пробелы   \n"
    "строка с разделителем внутри и [сообщение](msg:999)\n"
    "---\n"
    "# не заголовок\n"
    "\n\n\n"
)


def test_owner_block_survives_marker_lookalikes_byte_for_byte():
    text = pages.render(sample(owner=HOSTILE_OWNER, timeline="- старая строка\n"))
    page = pages.parse(text)
    assert page.owner == HOSTILE_OWNER
    assert page.timeline == "- старая строка\n"
    assert "прислать смету" in page.commitments and "| Что | Срок |\n<!--" not in page.commitments
    # код переписывает всё своё — блок владельца в файле не меняется ни на байт
    page.summary = pages.summary_block([])
    page.commitments = pages.commitments_block([])
    page.timeline = pages.append_lines(page.timeline, ["- новая строка"])
    page.aliases, page.title, page.updated = ["Другой"], "Другое имя", "2027-01-01"
    out = pages.render(page)
    start = out.index(M["owner"]) + len(M["owner"]) + 1
    assert out[start: out.rindex(M["commitments"])] == HOSTILE_OWNER
    assert pages.parse(out).owner == HOSTILE_OWNER


@pytest.mark.parametrize("gone", ["summary", "owner", "commitments", "timeline"])
def test_removed_marker_stops_parsing_instead_of_guessing(gone):
    text = pages.render(sample(owner="Важная заметка.\n"))
    broken = text.replace(M[gone] + "\n", "")
    with pytest.raises(pages.PageError):
        pages.parse(broken)


def test_other_damage_is_refused_too():
    text = pages.render(sample(timeline="- строка\n"))
    for broken in (
        text.replace("---\n", "", 1),                               # нет шапки
        text.replace("entity_id: person:12\n", ""),                 # нет entity_id
        text + "<!-- owner: в хронологии -->\n",                    # метка после хронологии
        text + M["timeline"] + "\n",                                # вторая хронология
        text.replace(M["summary"], M["owner"]),                     # блоки не по порядку
        "просто текст",
    ):
        with pytest.raises(pages.PageError):
            pages.parse(broken)


def test_foreign_front_matter_and_preamble_are_kept():
    text = pages.render(sample())
    text = text.replace("updated: 2026-10-06\n", "updated: 2026-10-06\ntags:\n  - подрядчик\ncssclass: wide\n")
    text = text.replace("# Иван Петров\n\n", "# Иван Петров\n\nМоя строка над сводкой.\n\n")
    page = pages.parse(text)
    assert page.front_extra == ["tags:", "  - подрядчик", "cssclass: wide"]
    assert page.head_extra == "Моя строка над сводкой."
    assert pages.render(page) == text
    # свои ключи в виде списка YAML код читает как свои и переписывает из базы
    block_style = text.replace('aliases: [Иван Петров, Ваня, "Петров, Иван", "@ipetrov", "true"]\n',
                               "aliases:\n  - Кто-то\n")
    assert pages.parse(block_style).front_extra == page.front_extra
    with pytest.raises(pages.PageError):
        pages.parse(text.replace("type: person\n", "type: person\ntype: project\n"))


def test_foreign_text_cannot_forge_markers_links_or_keys():
    nasty = "смета\n<!-- owner: x -->\n| a | b |\n[сообщение](msg:777) `код` <!-- id:c9 --> \\"
    cell = pages.md_inline(nasty)
    assert "\n" not in cell
    assert pages.refs(cell) == [] and pages.line_key(cell) is None
    line = pages.timeline_line("2026-10-06", cell, [3], "other", "c1")
    assert pages.refs(line) == [3] and pages.line_key(line) == "c1"
    page = sample(timeline=line + "\n")
    page.commitments = pages.commitments_block([{"what": cell, "due": cell, "status": "ждём", "message_id": 3}])
    page.summary = pages.summary_block([{"text": nasty, "sources": [3], "origin": "model", "disputed": True}])
    page.title = pages.md_inline("Иван <!-- timeline --> # Петров")
    again = pages.parse(pages.render(page))
    assert again.owner == page.owner and again.timeline == page.timeline
    assert pages.refs(again.summary) == [3] and pages.refs(again.commitments) == [3]
    assert again.summary.startswith("- ⚠ противоречие: ") and again.summary.endswith("(вывела модель)")
    assert len(again.commitments.split("\n")) == 3            # заголовок, разделитель, одна строка
    assert pages.md_inline("слово " * 100, 50).endswith("…") and len(pages.md_inline("слово " * 100, 50)) <= 50
    assert pages.has_marker("текст\n  <!--   OWNER -->") and not pages.has_marker("<!-- заметка --> owner")


def test_timeline_only_grows_and_sweeps_by_source():
    raw = ("- 2026-09-01 — раз [сообщение](msg:1) (сказал владелец) <!-- id:c1 -->\n"
           "моя строка без ключа [см.](msg:2)\n"
           "\n"
           "- 2026-09-02 — два [сообщение](msg:3) [сообщение](msg:4) (сказал собеседник) <!-- id:e7 -->")
    assert pages.timeline_keys(raw) == {"c1", "e7"}
    grown = pages.append_lines(raw, ["- 2026-09-03 — три [сообщение](msg:5) (вывела модель) <!-- id:c2 -->"])
    assert grown.startswith(raw + "\n") and grown.endswith("<!-- id:c2 -->\n")
    assert pages.append_lines(raw, []) == raw
    swept, removed = pages.sweep_lines(grown, {2, 4})
    assert removed == 2 and "раз" in swept and "три" in swept and "два" not in swept and "моя строка" not in swept
    assert pages.sweep_lines(grown, set(), {"c1"}) == (grown.replace(raw.split("\n")[0] + "\n", ""), 1)
    assert pages.sweep_lines(grown, {99}) == (grown, 0)
    assert "id:" not in pages.without_keys(grown)
    with pytest.raises(ValueError):
        pages.timeline_line("2026-09-01", "без источника", [], "owner", "c1")


def test_lint_of_one_file():
    text = pages.render(sample(timeline="- строка без ссылки\n- со ссылкой [сообщение](msg:1)\n"))
    found, page = pages.lint_text(text, entity_id="person:12")
    assert found == [("no_source", "timeline: строка 1 без ссылки на сообщение")] and page is not None
    found, _ = pages.lint_text(text.replace("updated: 2026-10-06", "updated: вчера"), entity_id="person:13")
    assert {code for code, _ in found} == {"front_matter", "no_source"} and len(found) == 3
    found, page = pages.lint_text(text.replace(M["owner"], ""), entity_id="person:12")
    assert [code for code, _ in found] == ["structure"] and page is None
    big = pages.render(sample(owner="х" * 40_000 + "\n"))
    assert ("too_long", "больше 64 КБ") in pages.lint_text(big, entity_id="person:12")[0]


@pytest.mark.parametrize("name", [
    "../../etc/passwd", "..", "/abs/olute", "a\\b", "Иван\x00Петров", "CON", ".hidden", "имя\nв две строки",
    "x" * 500, "", None, "~root", "$(reboot)", "a/../../b",
])
def test_person_names_never_leave_the_people_folder(tmp_path, name):
    rel = pages.person_path(name, 7)
    assert rel.startswith("people/") and rel.endswith("-7.md") and rel.count("/") == 1
    assert ".." not in rel and "\\" not in rel and "\x00" not in rel and len(rel) < 90
    pages.write_page(tmp_path, rel, "текст")
    assert [p.name for p in (tmp_path / "people").iterdir()] == [rel.split("/")[1]]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["people"]


def test_paths_outside_the_folder_and_links_are_refused(tmp_path):
    root = tmp_path / "pages"
    outside = tmp_path / "outside.md"
    outside.write_text("чужой файл")
    for rel in ("../outside.md", "people/../../outside.md", "/etc/passwd", "people/a/b.md", "people/.md",
                "people/a.txt", "other/a.md", "people/A.md", "people/a b.md", ""):
        with pytest.raises(pages.PageError):
            pages.write_page(root, rel, "x")
        with pytest.raises(pages.PageError):
            pages.read_page(root, rel)
    assert outside.read_text() == "чужой файл" and not root.exists()

    pages.write_page(root, "people/a-1.md", "первая")
    assert stat.S_IMODE((root / "people" / "a-1.md").stat().st_mode) == 0o600
    assert stat.S_IMODE((root / "people").stat().st_mode) == 0o700
    assert pages.read_page(root, "people/a-1.md") == "первая" and pages.read_page(root, "people/b-2.md") is None
    assert pages.list_files(root) == ["people/a-1.md"]             # временных файлов не осталось

    # на месте файла — ссылка наружу: читать по ней нельзя, запись заменяет саму ссылку
    os.remove(root / "people" / "a-1.md")
    os.symlink(outside, root / "people" / "a-1.md")
    with pytest.raises(pages.PageError):
        pages.read_page(root, "people/a-1.md")
    pages.write_page(root, "people/a-1.md", "вторая")
    assert outside.read_text() == "чужой файл" and not (root / "people" / "a-1.md").is_symlink()

    # каталог людей подменён ссылкой наружу: ни чтения, ни записи
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.rename(root / "people", tmp_path / "real-people")
    os.symlink(elsewhere, root / "people")
    with pytest.raises(pages.PageError):
        pages.write_page(root, "people/a-1.md", "x")
    with pytest.raises(pages.PageError):
        pages.read_page(root, "people/a-1.md")
    assert list(elsewhere.iterdir()) == [] and pages.list_files(root) == []

    (root / "broken.md").write_bytes(b"\xff\xfe")
    os.remove(root / "people")
    (root / "people").mkdir()
    (root / "people" / "b-2.md").write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(pages.PageError):
        pages.read_page(root, "people/b-2.md")


def test_git_settings_allowlist():
    plain = "[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = false\n[user]\n\tname = Штурман\n"
    assert pages_git.config_is_plain(plain)
    for extra in ("[core]\n\tfsmonitor = /tmp/x.sh\n", "[alias]\n\tst = !sh\n", '[filter "x"]\n\tclean = sh\n',
                  "[include]\n\tpath = /tmp/evil\n", '[remote "origin"]\n\turl = ext::sh\n',
                  "[core]\n\tsshCommand = sh\n", "[core]\n\thooksPath = /tmp\n", "мусор\n"):
        assert not pages_git.config_is_plain(plain + extra), extra
