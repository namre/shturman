"""Шаг мастера «Переписка»: сводка о странице настройки переписки — только признаки и числа."""

import pytest

from shturman_core import correspondence
from shturman_core.correspondence import page_url, summary

PUBLIC = "https://assistant.example.com"
NOTHING = {"enabled": True, "origin_set": True, "tg_keys": False, "accounts": 0, "own_bot": False,
           "owner_bound": False, "business_connected": False, "own_model": False}
EVERYTHING = {"enabled": True, "origin_set": True, "tg_keys": True, "accounts": 2, "own_bot": True,
              "owner_bound": True, "business_connected": True, "own_model": True}


@pytest.mark.parametrize("public, expected", [
    ("https://assistant.example.com", "https://assistant.example.com/shturman-setup/"),
    ("https://assistant.example.com/", "https://assistant.example.com/shturman-setup/"),
    ("http://shturman.test:8080", "http://shturman.test:8080/shturman-setup/"),
    ("  https://assistant.example.com  ", "https://assistant.example.com/shturman-setup/"),
])
def test_page_url_is_the_public_address_plus_the_fixed_path(public, expected):
    assert page_url(public) == expected


@pytest.mark.parametrize("public", [
    "", "   ", "assistant.example.com", "ftp://assistant.example.com", "javascript:alert(1)",
    "https://user:pass@assistant.example.com", "https://assistant.example.com/?next=x",
    "https://assistant.example.com/#frag", "https://", "https://assistant.example.com:port",
    "https://assistant.example.com/\"onclick=", "https://assistant .example.com", None,
])
def test_page_url_refuses_anything_that_is_not_a_plain_address(public):
    assert page_url(public) is None


def test_old_service_without_the_setup_object_is_reported_as_outdated():
    """Сервис прежней версии объекта setup не отдаёт: мастер должен это пережить и сказать «обновите»."""
    old = {"messages": 1200, "chats": 14, "chats_excluded": 0, "own_bot": False, "owner_known": True}
    out = summary(old, public_url=PUBLIC)
    assert out["state"] == correspondence.OUTDATED and out["setup"] is None
    assert out["archive"] == {"messages": 1200, "chats": 14}       # размер архива виден и так
    assert out["url"] == PUBLIC + "/shturman-setup/"
    for broken in (None, [], "да", 1, True):
        assert summary({"setup": broken})["state"] == correspondence.OUTDATED


def test_nothing_is_configured_yet():
    out = summary({"messages": 0, "chats": 0, "setup": NOTHING}, public_url=PUBLIC)
    assert out["state"] == correspondence.OK
    assert out["setup"] == {"origin_set": True, "tg_keys": False, "own_bot": False, "owner_bound": False,
                            "business_connected": False, "own_model": False, "accounts": 0}
    assert out["archive"] == {"messages": 0, "chats": 0}


def test_everything_is_configured():
    out = summary({"messages": 300_000, "chats": 87, "setup": EVERYTHING}, public_url=PUBLIC)
    assert out["state"] == correspondence.OK
    assert out["setup"] == {"origin_set": True, "tg_keys": True, "own_bot": True, "owner_bound": True,
                            "business_connected": True, "own_model": True, "accounts": 2}
    assert out["archive"] == {"messages": 300_000, "chats": 87}


def test_a_switched_off_page_is_not_presented_as_working():
    for value in (False, None, "true", 1):
        out = summary({"setup": dict(EVERYTHING, enabled=value)}, public_url=PUBLIC)
        assert out["state"] == correspondence.DISABLED


def test_only_flags_and_numbers_leave_the_summary():
    """Что бы сервис ни положил в эти поля, строки и вложенные объекты в браузер не уходят."""
    secret = "1234567890:" + "A" * 35
    status = {"messages": "много", "chats": -5, "token": secret,
              "setup": {"enabled": True, "own_bot": secret, "owner_bound": {"name": "Иван"}, "tg_keys": 1,
                        "accounts": "2", "origin_set": None, "login_link": "/shturman-setup/#" + "t" * 40,
                        "business_connected": [True], "own_model": "gpt"}}
    out = summary(status, public_url=PUBLIC)
    assert secret not in repr(out) and "Иван" not in repr(out) and "t" * 40 not in repr(out)
    assert out["setup"] == {"origin_set": None, "tg_keys": None, "own_bot": None, "owner_bound": None,
                            "business_connected": None, "own_model": None, "accounts": 0}
    assert out["archive"] == {"messages": None, "chats": None}
    assert set(out) == {"state", "url", "setup", "archive"}
    assert summary({"setup": dict(NOTHING, accounts=True)})["setup"]["accounts"] == 0      # True — не число
    assert summary({"setup": dict(NOTHING, accounts=10 ** 15)})["setup"]["accounts"] == 0


@pytest.mark.parametrize("state", [correspondence.NO_SERVICE, correspondence.UNREACHABLE])
def test_no_answer_from_the_service(state):
    out = summary(None, public_url=PUBLIC, state=state)
    assert out == {"state": state, "url": PUBLIC + "/shturman-setup/", "setup": None,
                   "archive": {"messages": None, "chats": None}}


def test_no_answer_is_never_reported_as_ok():
    assert summary(None)["state"] == correspondence.UNREACHABLE
    assert summary(None, state=correspondence.OK)["state"] == correspondence.UNREACHABLE
    assert summary(None, state="что-то")["state"] == correspondence.UNREACHABLE


def test_without_a_public_address_there_is_no_link():
    """Адрес не задан (аварийный режим, только сервер): страница открывается через туннель, кнопки нет."""
    out = summary({"setup": dict(NOTHING, origin_set=False)}, public_url="")
    assert out["url"] is None and out["state"] == correspondence.OK and out["setup"]["origin_set"] is False


def test_every_reported_state_is_a_known_one():
    seen = {summary(None)["state"], summary(None, state=correspondence.NO_SERVICE)["state"],
            summary({})["state"], summary({"setup": NOTHING})["state"],
            summary({"setup": dict(NOTHING, enabled=False)})["state"]}
    assert seen == set(correspondence.STATES)
