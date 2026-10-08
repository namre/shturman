import pytest

from shturman_core import wizard


def test_deep_link_shape():
    assert wizard.deep_link("ivan_shturman_bot", "abc_DEF-123") == \
        "https://t.me/ivan_shturman_bot?start=abc_DEF-123"


@pytest.mark.parametrize("username,token", [
    ("", "abc"), ("bad name", "abc"), ("x", "abc"), ("good_bot", ""), ("good_bot", "пробел тут"),
    ("good_bot", "a" * 65), ("good_bot", "a b"),
])
def test_deep_link_rejects_bad_input(username, token):
    with pytest.raises(ValueError):
        wizard.deep_link(username, token)


def test_pairing_token_fits_telegram_limit(store):
    from shturman_core.pairing import Pairing
    started = Pairing(store).start()
    assert wizard.deep_link("good_bot", started["token"]).endswith(started["token"])


def test_snapshot_has_no_secrets(store):
    from shturman_core.auth import Auth
    from shturman_core.pairing import Pairing
    Auth(store).issue_activation()
    started = Pairing(store).start()
    wizard.remember_bot(store, "good_bot", "Мой бот")
    text = repr(wizard.snapshot(store))
    assert started["token"] not in text and started["code"] not in text
    assert "digest" not in text
    snap = wizard.snapshot(store)
    assert snap["pairing"]["pending"] is True and snap["pairing"]["owner"] is None
    assert snap["bot"] == {"username": "good_bot", "name": "Мой бот"}
    assert snap["completed"] is False
    # Официальный плагин бизнес-режима мастер больше не ставит: его координат в состоянии нет.
    assert snap["business"] == {"connected": False, "can_reply": False, "updated_at": None}


def test_marks(store):
    wizard.mark(store, "model_ok")
    wizard.mark(store, "completed")
    snap = wizard.snapshot(store)
    assert set(snap["marks"]) == {"model_ok", "completed"} and snap["completed"] is True
    with pytest.raises(ValueError):
        wizard.mark(store, "что-то")


def test_correspondence_step_has_its_own_mark(store):
    wizard.mark(store, "correspondence_seen")
    assert set(wizard.snapshot(store)["marks"]) == {"correspondence_seen"}


def test_state_of_an_instance_set_up_before_the_correspondence_step_still_reads(store):
    """Экземпляр прошёл мастер до 0.0.6: в состоянии отметка business_skipped, запись о подключённом
    бизнес-режиме и поля, которых новый мастер не знает. После обновления мастер открывается,
    пройденные шаги остаются пройденными."""
    store.write("wizard", {
        "persona": {"persona": "jeeves", "owner_address": "Иван Иванович"},
        "marks": {"persona_saved": 1, "model_ok": 2, "bot_applied": 3, "business_skipped": 4, "completed": 5,
                  "какая-то_прежняя": 6},
        "completed_at": 5, "bot": {"username": "good_bot", "name": "Мой бот"}, "step": "later",
    })
    store.write("business", {"connected": True, "can_reply": False, "updated_at": 7,
                             "plugin": {"name": "telegram-business"}})
    snap = wizard.snapshot(store)
    assert snap["marks"] == {"persona_saved": 1, "model_ok": 2, "bot_applied": 3, "business_skipped": 4,
                             "completed": 5}
    assert snap["completed"] is True
    assert snap["business"] == {"connected": True, "can_reply": False, "updated_at": 7}
    wizard.mark(store, "business_skipped")          # прежняя отметка по-прежнему принимается
    wizard.mark(store, "correspondence_seen")
    assert {"business_skipped", "correspondence_seen"} <= set(wizard.snapshot(store)["marks"])


def test_probe_output_parsing():
    ok = wizard.parse_probe_output(0, "\nsession_id: 2026_abc\nРаботает\n")
    assert ok == {"ok": True, "reply": "Работает"}
    noisy = wizard.parse_probe_output(0, "  ⚠ предупреждение сканера\nРаботает\nsession_id: y\n")
    assert noisy == {"ok": True, "reply": "Работает"}
    assert wizard.parse_probe_output(0, "session_id: x\n\n")["ok"] is False
    failed = wizard.parse_probe_output(1, "session_id: x\nError: 401 Unauthorized\n")
    assert failed["ok"] is False and "401" in failed["error"]


# --- страница мастера: состав шагов и то, чего в ней быть не должно ---------------------------
# Сама страница проверяется в браузере (tests/e2e/wizard.mjs). Здесь — то, что видно по тексту
# скрипта и что нельзя потерять при правках.

from pathlib import Path  # noqa: E402
import re  # noqa: E402

_PAGE = (Path(__file__).resolve().parents[1] / "dashboard" / "dist" / "index.js").read_text(encoding="utf-8")
_CODE = re.sub(r"/\*.*?\*/", "", _PAGE, flags=re.S)                  # без блочных комментариев
_CODE = "\n".join(line for line in _CODE.splitlines() if not line.strip().startswith("//"))


def _steps() -> list[tuple[str, str]]:
    block = _CODE[_CODE.index("const STEPS = ["):]
    block = block[:block.index("];")]
    return re.findall(r'id:\s*"([a-z]+)",\s*title:\s*"([^"]+)"', block)


def test_wizard_steps_are_persona_model_bot_correspondence_done():
    assert _steps() == [("persona", "Помощник"), ("model", "Модель"), ("bot", "Бот в Telegram"),
                        ("correspondence", "Переписка"), ("done", "Готово")]


def test_wizard_no_longer_connects_the_assistant_bot_in_business_mode():
    """Шага «Бизнес-режим» нет: мастер не ставит официальный плагин и не ведёт владельца подключать
    бота-ассистента в настройках Telegram."""
    for gone in ("installAgentPlugin", "getPluginsHub", "telegram-business", "hermes-telegram-business",
                 "Установить защиту", "Business Mode", "business_skipped\" }", "BusinessStep", "LaterStep",
                 "В следующих версиях", "Скоро"):
        assert gone not in _CODE, gone


def _correspondence_step() -> str:
    return _CODE[_CODE.index("function CorrespondenceStep"):_CODE.index("function correspondenceSummary")]


def test_correspondence_step_is_short_and_names_the_approvals_bot_and_two_ways():
    """Шаг «Переписка» короткий: подробности — на странице сервиса. Он называет то, что там будет:
    бот согласований (первый шаг страницы) и два способа подключения. Блока «Два бота» нет."""
    step = _correspondence_step()
    for gone in ("Два бота", "shturman-bots", "не перепутайте", "Бот-ассистент", "К нему подключается",
                 "по коду от бота", "токен второго бота", "Почему отдельная страница", "Шагов там три"):
        assert gone not in _CODE, gone
    assert "чтобы ваш вход в Telegram не проходил через ассистента" in step
    assert "бота согласований" in step and "ассистент видит всё как вы" in step and "отдельный" in step
    fine = step[step.index("shturman-fine"):]
    assert "Разрешить ассистенту отправлять сообщения можно позже" in fine and "«Дополнительно»" not in fine
    # бот-ассистент в бизнес-режиме — только спокойное предупреждение для экземпляра прежней схемы
    assert "st.business.connected ?" in step and "Ничего не сломано" in step


def test_correspondence_step_shows_three_facts_of_the_simple_path():
    facts = _CODE[_CODE.index("function CorrFacts"):_CODE.index("function CorrespondenceStep")]
    assert re.findall(r'fact\("([^"]+)"', facts) == ["Ключи Telegram", "Аккаунт Telegram", "Чаты"]
    for gone in ("own_bot", "owner_bound", "own_model", "origin_set"):
        assert gone not in _CODE, gone


def test_correspondence_step_names_every_state_in_plain_words():
    problem = _CODE[_CODE.index("function CorrProblem"):_CODE.index("function CorrFacts")]
    for state in ("no_origin", "same_origin", "outdated", "disabled", "no_service"):
        assert f'state === "{state}"' in problem, state
    assert "Страница настройки переписки недоступна — обновите экземпляр" in problem
    assert "ей задан тот же адрес, что у ассистента" in problem and "./ops/set-setup-url.sh" in _CODE
    assert "пока нет адреса в интернете" in problem


def test_done_step_has_one_correspondence_row():
    done = _CODE[_CODE.index("function DoneStep"):_CODE.index("const STEPS = [")]
    assert done.count('row("Переписка"') == 1
    assert re.findall(r'row\("([^"]+)"', done) == ["Помощник", "Представляется", "Модель", "Бот и вход", "Переписка"]


def test_wizard_does_not_ask_for_show_or_proxy_the_setup_page():
    """Мастер даёт обычную ссылку на страницу настройки переписки — и только. Ссылку входа он
    не запрашивает, запросов на страницу не шлёт, проходом дашборда к сервису не пользуется."""
    assert "shturman-setup" not in _CODE          # адрес приходит с сервера готовым, в скрипте его нет
    assert "/service/" not in _CODE and "setup-link\"" not in _CODE
    requests = set(re.findall(r'(?:get|post)\("(/[a-z/]+)"', _CODE))
    assert requests == {"/state", "/persona", "/model/probe", "/bot/check", "/pairing/start", "/pairing",
                        "/pairing/confirm", "/pairing/reject", "/mark", "/correspondence"}
    link = _CODE[_CODE.index("Открыть настройку переписки") - 300:_CODE.index("Открыть настройку переписки")]
    assert "href: c.url" in link and 'target: "_blank"' in link and 'rel: "noopener noreferrer"' in link
    # Кнопка есть только в состоянии «всё в порядке» и только с адресом, который отдал сервер.
    assert 'const open = !!(c && c.state === "ok" && c.url);' in _CODE
    assert _CODE.count("href: c.url") == 1 and "window.open" not in _CODE and "location.href" not in _CODE
    # Каждая ссылка, открывающая новую вкладку, закрывает ей доступ к этой.
    assert _CODE.count('target: "_blank"') == _CODE.count('rel: "noopener noreferrer"') > 0


def test_wizard_tells_how_to_get_in_without_issuing_the_link():
    step = _correspondence_step()
    assert "По одноразовой ссылке — её выдаёт тот, кто ставил ассистента, или вы сами командой " in step
    assert "./ops/setup-link.sh" in _CODE and "Ссылка действует 30 минут." in step
    assert "Мастер эту ссылку не выдаёт и не показывает" in step


def test_old_marks_keep_the_merged_step_done():
    done = _CODE[_CODE.index('if (id === "correspondence")'):]
    done = done[:done.index("}")]
    for sign in ("m.correspondence_seen", "m.business_skipped", "st.business.connected", "st.completed"):
        assert sign in done, sign


def test_persona_step_does_not_promise_business_mode():
    """«Бизнес-ассистент» — только подпись в представлении; про бизнес-режим Telegram рядом ни слова."""
    step = _CODE[_CODE.index("function PersonaStep"):_CODE.index("const PROVIDERS")]
    assert "бизнес" not in step.lower()
