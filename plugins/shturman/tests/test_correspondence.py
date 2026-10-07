"""Шаг мастера «Переписка»: сводка о странице настройки переписки — состояние, проверенный адрес,
признаки и числа."""

import pytest

from shturman_core import correspondence
from shturman_core.correspondence import clean_origin, origin_of, summary

DASHBOARD = "https://assistant.example.com"
SETUP = "https://assistant.example.com:8443"           # то же имя, другой порт — так по умолчанию
PAGE = SETUP + "/shturman-setup/"
NOTHING = {"enabled": True, "origin": SETUP, "reason": None, "origin_set": True, "tg_keys": False,
           "accounts": 0, "own_bot": False, "owner_bound": False, "business_connected": False,
           "own_model": False}
EVERYTHING = {"enabled": True, "origin": SETUP, "reason": None, "origin_set": True, "tg_keys": True,
              "accounts": 2, "own_bot": True, "owner_bound": True, "business_connected": True,
              "own_model": True}
# Сборка сервиса до отдельного адреса: объект setup есть, полей origin и reason в нём нет.
BEFORE_ORIGIN = {k: v for k, v in NOTHING.items() if k not in ("origin", "reason")}


# --- адрес страницы: в ссылку попадает только проверенное -------------------------------------

@pytest.mark.parametrize("value, expected", [
    ("https://assistant.example.com:8443", "https://assistant.example.com:8443"),
    ("https://assistant.example.com:8443/", "https://assistant.example.com:8443"),
    ("https://setup.example.com", "https://setup.example.com"),
    ("https://setup.example.com:443", "https://setup.example.com"),          # обычный порт не пишется
    ("https://Setup.Example.com:9443", "https://setup.example.com:9443"),
])
def test_clean_origin_accepts_a_plain_https_address(value, expected):
    assert clean_origin(value) == expected


@pytest.mark.parametrize("value", [
    None, "", "   ", 8443, True, ["https://assistant.example.com:8443"], {"origin": SETUP},
    "assistant.example.com:8443", "http://assistant.example.com:8443", "ftp://assistant.example.com",
    "javascript:alert(1)", "data:text/html,x", "//assistant.example.com:8443",
    "https://user:pass@assistant.example.com:8443", "https://user@assistant.example.com",
    "https://assistant.example.com:8443/path", "https://assistant.example.com:8443/?next=x",
    "https://assistant.example.com:8443#frag", "https://assistant.example.com:8443?x=1",
    "https://", "https://assistant.example.com:port", "https://assistant.example.com:0",
    "https://assistant.example.com:99999", "https://assistant.example.com:", "https://localhost:8443",
    "https://127.0.0.1:8443", "https://10.0.0.1", "https://[::1]:8443",
    "https://assistant .example.com", " https://assistant.example.com:8443", "https://assistant.example.com:8443 ",
    "https://assistant.example.com:8443\n", "https://assistant.example.com\"onclick=", "https://-bad.example.com",
    "https://assistant.example.com\\@evil.example.com", "https://assistant.example.com%2f.evil.example.com",
    "https://ассистент.example.com", "https://" + "a" * 300 + ".example.com",
])
def test_clean_origin_refuses_everything_else(value):
    assert clean_origin(value) is None


@pytest.mark.parametrize("a, b", [
    ("https://assistant.example.com", "https://ASSISTANT.example.com:443/"),
    ("https://assistant.example.com/dash/", "https://assistant.example.com"),
    ("http://shturman.test:8080", "http://shturman.test:8080/"),
])
def test_origin_of_ignores_case_default_port_and_path(a, b):
    assert origin_of(a) == origin_of(b) is not None


def test_same_name_on_another_port_is_another_origin():
    assert origin_of(DASHBOARD) != origin_of(SETUP)
    assert origin_of("https://assistant.example.com:8443") != origin_of("https://assistant.example.com:9443")
    assert origin_of("http://assistant.example.com") != origin_of("https://assistant.example.com")
    for junk in (None, "", "assistant.example.com", "https://", 5, "https://u:p@assistant.example.com"):
        assert origin_of(junk) is None


# --- состояния ---------------------------------------------------------------------------------

def test_all_is_well_gives_the_button_address():
    out = summary({"messages": 0, "chats": 0, "setup": NOTHING}, dashboard_url=DASHBOARD)
    assert out == {"state": "ok", "url": PAGE,
                   "setup": {"tg_keys": False, "business_connected": False, "accounts": 0},
                   "archive": {"messages": 0, "chats": 0}}


def test_everything_is_configured():
    out = summary({"messages": 300_000, "chats": 87, "setup": EVERYTHING}, dashboard_url=DASHBOARD)
    assert out["state"] == correspondence.OK and out["url"] == PAGE
    assert out["setup"] == {"tg_keys": True, "business_connected": True, "accounts": 2}
    assert out["archive"] == {"messages": 300_000, "chats": 87}


def test_a_separate_host_name_works_too():
    separate = dict(NOTHING, origin="https://setup.example.com")
    out = summary({"setup": separate}, dashboard_url=DASHBOARD)
    assert out["state"] == correspondence.OK and out["url"] == "https://setup.example.com/shturman-setup/"


def test_old_service_without_the_setup_object_is_reported_as_outdated():
    """Сервис 0.0.5 объекта setup не отдаёт: мастер должен это пережить и сказать «обновите»."""
    old = {"messages": 1200, "chats": 14, "chats_excluded": 0, "own_bot": False, "owner_known": True}
    out = summary(old, dashboard_url=DASHBOARD)
    assert out["state"] == correspondence.OUTDATED and out["setup"] is None and out["url"] is None
    assert out["archive"] == {"messages": 1200, "chats": 14}       # размер архива виден и так
    for broken in (None, [], "да", 1, True):
        assert summary({"setup": broken})["state"] == correspondence.OUTDATED


def test_service_built_before_the_separate_address_is_outdated_and_gets_no_link():
    """Сборка до отдельного адреса отдавала страницу на адресе дашборда: ссылку на неё мастер не даёт."""
    out = summary({"messages": 5, "chats": 1, "setup": BEFORE_ORIGIN}, dashboard_url=DASHBOARD)
    assert out["state"] == correspondence.OUTDATED and out["url"] is None and out["setup"] is None
    assert DASHBOARD not in repr(out)


def test_same_origin_reported_by_the_service_disables_the_step_button():
    same = dict(NOTHING, enabled=False, origin=None, reason="same_origin")
    out = summary({"setup": same}, dashboard_url=DASHBOARD)
    assert out["state"] == correspondence.SAME_ORIGIN and out["url"] is None
    assert out["setup"] == {"tg_keys": False, "business_connected": False, "accounts": 0}


@pytest.mark.parametrize("origin", [
    DASHBOARD, DASHBOARD + "/", "https://ASSISTANT.example.com", "https://assistant.example.com:443",
])
def test_the_plugin_never_links_to_the_dashboard_origin_even_if_the_service_missed_it(origin):
    """Сервис совпадения не заметил (или ему подсунули адрес): на адрес дашборда мастер не ведёт."""
    out = summary({"setup": dict(NOTHING, origin=origin)}, dashboard_url=DASHBOARD + "/")
    assert out["state"] == correspondence.SAME_ORIGIN and out["url"] is None


def test_no_origin_means_tunnel_only_but_the_state_is_still_shown():
    none = dict(NOTHING, origin=None, reason="no_origin", tg_keys=True, accounts=1)
    out = summary({"messages": 40, "chats": 2, "setup": none}, dashboard_url=DASHBOARD)
    assert out["state"] == correspondence.NO_ORIGIN and out["url"] is None
    assert out["setup"] == {"tg_keys": True, "business_connected": False, "accounts": 1}
    # причина названа, даже если сервис при этом считает страницу выключенной
    assert summary({"setup": dict(none, enabled=False)})["state"] == correspondence.NO_ORIGIN


@pytest.mark.parametrize("origin", [
    None, "", "http://assistant.example.com:8443", "https://assistant.example.com:8443/x",
    "javascript:alert(1)", "https://user@assistant.example.com:8443", 8443, {"href": SETUP},
])
def test_an_origin_unfit_for_a_link_is_treated_as_no_address(origin):
    out = summary({"setup": dict(NOTHING, origin=origin)}, dashboard_url=DASHBOARD)
    assert out["state"] == correspondence.NO_ORIGIN and out["url"] is None


def test_a_switched_off_page_is_not_presented_as_working():
    for value in (False, None, "true", 1):
        out = summary({"setup": dict(EVERYTHING, enabled=value)}, dashboard_url=DASHBOARD)
        assert out["state"] == correspondence.DISABLED and out["url"] is None


def test_an_unknown_reason_does_not_become_a_state():
    out = summary({"setup": dict(NOTHING, reason="что-то новое")}, dashboard_url=DASHBOARD)
    assert out["state"] == correspondence.OK and out["url"] == PAGE


def test_without_a_dashboard_address_the_page_address_still_works():
    """Аварийный режим: адрес дашборда Hermes не передан, сверять не с чем — адрес страницы годится."""
    out = summary({"setup": NOTHING}, dashboard_url="")
    assert out["state"] == correspondence.OK and out["url"] == PAGE


def test_only_flags_numbers_and_the_checked_address_leave_the_summary():
    """Что бы сервис ни положил в эти поля, строки и вложенные объекты в браузер не уходят."""
    secret = "1234567890:" + "A" * 35
    status = {"messages": "много", "chats": -5, "token": secret,
              "setup": {"enabled": True, "origin": SETUP, "reason": None,
                        "own_bot": secret, "owner_bound": {"name": "Иван"}, "tg_keys": 1,
                        "accounts": "2", "origin_set": None, "login_link": "/shturman-setup/#" + "t" * 40,
                        "business_connected": [True], "own_model": "gpt"}}
    out = summary(status, dashboard_url=DASHBOARD)
    assert secret not in repr(out) and "Иван" not in repr(out) and "t" * 40 not in repr(out)
    assert out["setup"] == {"tg_keys": None, "business_connected": None, "accounts": 0}
    assert out["archive"] == {"messages": None, "chats": None}
    assert set(out) == {"state", "url", "setup", "archive"}
    assert summary({"setup": dict(NOTHING, accounts=True)})["setup"]["accounts"] == 0      # True — не число
    assert summary({"setup": dict(NOTHING, accounts=10 ** 15)})["setup"]["accounts"] == 0
    # бот согласований и своя модель к первой настройке не относятся: в сводке их нет вовсе
    assert "own_bot" not in out["setup"] and "owner_bound" not in out["setup"] and "own_model" not in out["setup"]


@pytest.mark.parametrize("state", [correspondence.NO_SERVICE, correspondence.UNREACHABLE])
def test_no_answer_from_the_service(state):
    out = summary(None, dashboard_url=DASHBOARD, state=state)
    assert out == {"state": state, "url": None, "setup": None, "archive": {"messages": None, "chats": None}}


def test_no_answer_is_never_reported_as_ok():
    assert summary(None)["state"] == correspondence.UNREACHABLE
    assert summary(None, state=correspondence.OK)["state"] == correspondence.UNREACHABLE
    assert summary(None, state="что-то")["state"] == correspondence.UNREACHABLE


def test_the_address_is_given_only_when_all_is_well():
    cases = [summary(None), summary(None, state=correspondence.NO_SERVICE), summary({}),
             summary({"setup": BEFORE_ORIGIN}), summary({"setup": dict(NOTHING, enabled=False)}),
             summary({"setup": dict(NOTHING, origin=None, reason="no_origin")}),
             summary({"setup": dict(NOTHING, reason="same_origin")}),
             summary({"setup": NOTHING}, dashboard_url=DASHBOARD)]
    assert {c["state"] for c in cases} == set(correspondence.STATES)        # все известные состояния
    for case in cases:
        assert (case["url"] is not None) == (case["state"] == correspondence.OK)
