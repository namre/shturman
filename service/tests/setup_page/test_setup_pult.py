"""«Настройки Штурмана» как пульт (пакет 3 ревизии удобства: С1, С2, С4, С7, С8, С10–С13, Х7, П5).

Сервер добавил только поля в существующие ответы: `now` в `GET /overview` (сколько ждёт решения по видам,
сбои модели за сутки, время ночного разбора) и `dialogs_total` в списке диалогов. Маршруты, роли и
подтверждения не менялись. Раскладку, сворачивание шагов, «Сейчас», «Готово», контраст и цели касания
проверяет браузерный сценарий `tests/e2e/setup_page.mjs` (и `miniapp.mjs` — приложение); здесь — файлы
страницы и ответы сервера.
"""

import json
import re
from pathlib import Path

from setup_fakes import stand  # noqa: F401, I001 — фикстура; первым: добавляет пути

from shturman import confirm  # noqa: E402
from shturman.outbox import drafts  # noqa: E402
from shturman.setup_page import service as page  # noqa: E402

from test_miniapp import _delivered, _helper_chat, ready  # noqa: E402

STATIC = Path(__file__).resolve().parents[2] / "src" / "shturman" / "setup_page" / "static"
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
SCRIPT = (STATIC / "setup.js").read_text(encoding="utf-8")
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")
APP_CSS = (STATIC / "app.css").read_text(encoding="utf-8")


@confirm.applier("t.pult")
async def _apply(conn, payload):
    return "Сделано в тесте."


async def test_now_counts_what_waits_by_kind_without_any_text(stand, conn):
    s = await ready(stand, conn)
    empty = (await s.page.get("/overview")).json()["now"]
    assert empty["ok"] is True and sum(empty["pending"].values()) == 0
    assert set(empty["pending"]) == set(page.NOW_KINDS)
    assert empty["model_failed_24h"] == {"media": 0, "requests": 0}
    assert empty["nightly_at"] == "03:30" and empty["timezone"]

    await confirm.request(conn, "t.pult", "Тайное действие «Склад на Окружной»", {"n": 1})
    chat_id = await _helper_chat(s)
    # черновик ассистента заводится только при включённой отправке и сессии помощника на связи (Р-105)
    import dataclasses
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outbox"))
    from outbox_helpers import FakeTg
    helper = FakeTg()
    helper.sendable.add(await conn.fetchval("SELECT id FROM accounts WHERE role = 'assistant'"))
    s.state.extras["tg"] = helper
    s.state.config = dataclasses.replace(s.state.config, sending=True)
    s.state.extras["outbox"].state.config = s.state.config
    await drafts.create(conn, s.state, chat_id=chat_id, text="Смету пришлю в пятницу.")
    await _delivered(s)
    body = (await s.page.get("/overview")).json()
    now = body["now"]
    assert now["pending"]["action"] == 1 and now["pending"]["draft"] == 1
    # только числа: ни текста карточек, ни имён собеседников
    raw = json.dumps(now, ensure_ascii=False)
    for word in ("Склад", "Окружной", "Смету", "Иван", "Петров"):
        assert word not in raw, word


async def test_now_does_not_break_the_overview_when_counting_fails(stand, conn, monkeypatch):
    s = await ready(stand, conn)
    from shturman.setup_page import approvals

    async def broken(*args, **kwargs):
        raise RuntimeError("сбой")
    monkeypatch.setattr(approvals, "collect", broken)
    body = (await s.page.get("/overview")).json()
    assert body["now"]["ok"] is False and "archive" in body and "audit" in body


def test_now_is_not_on_the_internal_api_or_in_the_app():
    # «Сейчас» — поле ответа страницы; внутренний API и приложение его не отдают
    from shturman.setup_page import miniapp
    assert "_now_state" not in Path(miniapp.__file__).read_text(encoding="utf-8")


def test_page_order_steps_then_everyday_cards():
    ids = re.findall(r'<(?:section|details|h2)[^>]*\bid="([^"]+)"', INDEX)
    order = [i for i in ids if i in {"s-bot", "s-mode", "s-business", "s-keys", "s-accounts", "s-assistant", "s-chats",
                                       "s-import", "s-media", "s-ready", "h-daily", "s-spheres", "s-notify", "s-watch",
                                       "s-rehearsal", "s-memory", "s-audit", "extras"}]
    assert order == ["s-bot", "s-mode", "s-business", "s-keys", "s-accounts", "s-assistant", "s-chats", "s-import",
                     "s-media", "s-ready", "h-daily", "s-spheres", "s-notify", "s-watch", "s-rehearsal", "s-memory",
                     "s-audit", "extras"]
    assert ">Повседневное</h2>" in INDEX
    # журнал — своей карточкой, а не внутри «Дополнительно»
    extras = INDEX[INDEX.index('id="extras"'):]
    assert 'id="audit"' not in extras


def test_pult_header_quick_links_and_title():
    assert "<title>Настройки Штурмана</title>" in INDEX                     # вкладка браузера — прежняя
    nav = INDEX[INDEX.index('<nav class="quick"'):INDEX.index("</nav>")]
    assert re.findall(r">([^<]+)</a>", nav) == ["Чаты", "Сферы", "Уведомления", "Черновики ответов", "Доверие",
                                                     "Память", "История"]
    assert '"Штурман — ваш пульт"' in SCRIPT
    # вступление и словарь в пульте уходят в «Частые вопросы»
    assert 'id="faq-intro"' in INDEX and 'id="intro-wizard"' in INDEX


def test_no_sphere_is_chosen_for_a_folder_by_default():
    for code in (SCRIPT, APP_JS):
        assert "Связать со сферой…" in code and "Выберите сферу…" in code
        assert "Предлагать новые чаты папки" in code
        for old in ('"Сделать сферу"', '"Сделать сферу из папки"', '"Дополнить «"', '"Предлагать"', '"Предлагать новые"'):
            assert old not in code, old
        # выбор сферы для папки начинается с пустого значения
        assert 'value: ""' in code or 'first.value = ""' in code


def test_same_portion_of_chats_on_page_and_in_app():
    assert "FIRST_CHATS = 20, MORE_CHATS = 20" in SCRIPT and "FIRST_CHATS = 20, MORE_CHATS = 20" in APP_JS
    assert "SECTION_FIRST = 20, SECTION_MORE = 20" in SCRIPT and "SECTION_FIRST = 20, SECTION_MORE = 20" in APP_JS
    for code in (SCRIPT, APP_JS):
        assert '"Читается "' in code and "dialogs_total" in code


def test_app_card_text_uses_text_color_and_theme_contrast_is_fixed():
    assert ".lines { margin: 0; padding: 0; list-style: none; color: var(--text);" in APP_CSS
    assert "--hint: var(--hint-fix, var(--hint-src));" in APP_CSS
    assert "--button: var(--button-fix, var(--button-src));" in APP_CSS
    assert "function fixContrast()" in APP_JS


def test_storage_is_only_for_conveniences_and_always_guarded():
    # ключ сессии, раскрытые шаги (sessionStorage) и «Готово уже показано»; каждое обращение — в try
    assert "shturman-setup-open" in SCRIPT and "shturman-setup-ready-seen" in SCRIPT
    code = re.sub(r"/\*.*?\*/|//[^\n]*", "", SCRIPT, flags=re.DOTALL)
    for line in code.splitlines():
        if "window[area]" in line:
            assert "try" in line, line
        if "OPEN_NAME" in line or "READY_NAME" in line:
            assert "var " in line or "readStore(" in line or "writeStore(" in line, line


async def test_dialogs_report_how_many_chats_the_account_has(stand, conn):
    """«Читается N из M» (С11): M — все чаты аккаунта в сохранённом списке, при любом отборе."""
    from test_miniapp_chats import loaded
    from test_miniapp_chats import ready as chats_ready
    s = await chats_ready(stand, conn)
    first = await loaded(s)
    assert first["dialogs_total"] == first["total"] == 47
    found = (await s.app.get(f"/chats/{s.account}/dialogs", params={"q": "иван"})).json()
    assert found["total"] == 1 and found["dialogs_total"] == 47
    on_page = (await s.page.get(f"/tg/accounts/{s.account}/dialogs", params={"kind": "group"})).json()
    assert on_page["dialogs_total"] == 47 and on_page["total"] == 2
