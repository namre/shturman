"""Тексты «Настроек Штурмана» для владельца без жаргона (Р-86: С3, С5, С6, Х4).

Разметка и скрипт страницы проверяются как файлы; живую отрисовку проверяет браузерный сценарий
`tests/e2e/setup_page.mjs` (там же — подтверждение «Не сохранять» в строке).
"""
import re
from pathlib import Path

from shturman.setup_page import audit

STATIC = Path(__file__).resolve().parents[2] / "src" / "shturman" / "setup_page" / "static"
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
SCRIPT = (STATIC / "setup.js").read_text(encoding="utf-8")
APP = (STATIC / "app.html").read_text(encoding="utf-8") + (STATIC / "app.js").read_text(encoding="utf-8")


def _owner_text(html: str) -> str:
    """Разметка без свёрнутых строк «Для того, кто обслуживает сервер» и без комментариев."""
    html = re.sub(r'<details class="for-admin">.*?</details>', "", html, flags=re.DOTALL)
    return re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)


def test_page_names_itself_settings_of_shturman():
    assert "<title>Настройки Штурмана</title>" in INDEX
    header = INDEX[INDEX.index('<header class="top">'):INDEX.index("</header>")]
    assert "Настройки Штурмана" in header and "· настройка" not in header


def test_no_server_commands_variables_or_zone_names_for_the_owner():
    text = _owner_text(INDEX)
    for word in ("./ops/", "SHTURMAN_", "Europe/", "Hermes", "кабинет", "«Переписка»"):
        assert word not in text, word
    # команды для администратора — только в свёрнутых строках
    assert INDEX.count('<details class="for-admin">') >= 3
    for admin in re.findall(r'<details class="for-admin">.*?</details>', INDEX, flags=re.DOTALL):
        assert "Для того, кто обслуживает сервер" in admin


def test_script_has_no_system_confirm_dialogs_and_no_jargon_in_owner_strings():
    code = re.sub(r"/\*.*?\*/|//[^\n]*", "", SCRIPT, flags=re.DOTALL)
    assert "confirm(" not in code
    strings = re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"', code)
    owner = [s for s in strings if re.search("[а-яё]", s, re.IGNORECASE)]
    for word in ("SHTURMAN_", "Europe/", "Hermes", "./ops/"):
        assert not [s for s in owner if word in s], word
    # «Не сохранять»: два явных выбора, стирание — отдельной красной кнопкой
    assert "Только перестать сохранять" in SCRIPT and "Перестать и стереть сохранённое" in SCRIPT
    assert '"по московскому времени"' in SCRIPT


def test_two_ways_and_the_promise_about_drafts():
    text = _owner_text(INDEX)
    assert "<h3>Через ваш аккаунт</h3>" in text and "<h3>Через бота и помощника</h3>" in text
    assert "Сейчас — только черновики" not in text + SCRIPT
    assert "Пока только репетиция: ассистент пишет, что ответил бы, вы оцениваете; ничего не отправляется." in text
    assert "Черновики ответов, если вы их включите" in text
    # прежние названия способов — как названия (в кавычках и заголовках) больше не встречаются
    for old in ("«Ассистент видит всё как вы»", "<h3>Ассистент видит", "«Ассистент — отдельный сотрудник»",
                "<h3>Ассистент — отдельный", "аккаунт ассистента", "Аккаунт ассистента"):
        assert old not in text and old not in SCRIPT and old not in APP, old


def test_reason_codes_in_the_journal_are_words():
    assert audit.human_detail("адрес не подошёл: wrong_state") == ("адрес не подошёл: адрес из другой, устаревшей "
                                                                   "попытки входа")
    assert audit.human_detail("проверка не прошла: blocked_address") == ("проверка не прошла: адрес во внутренней "
                                                                         "сети сервера — так нельзя")
    assert audit.human_detail("причина: some_new_code") == "причина: подробности — в журнале сервиса"
    assert audit.human_detail("глубина: 3 месяца") == "глубина: 3 месяца"
    assert audit.human_detail("") == ""
