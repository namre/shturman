import pytest

from shturman_core.pairing import PAIRING_MAX_WRONG, PAIRING_TTL, Pairing, extract_candidate


@pytest.fixture
def pairing(store, clock):
    return Pairing(store, now=clock)


@pytest.mark.parametrize("text,expected", [
    ("/start abc_DEF-123", "abc_DEF-123"),
    ("/start", None),
    ("/start   ", None),
    ("482 913", "482913"),
    ("482913", "482913"),
    ("48291", None),
    ("привет", None),
    ("", None),
])
def test_extract_candidate(text, expected):
    assert extract_candidate(text) == expected


def test_not_pending_by_default(pairing):
    assert pairing.is_pending() is False
    assert pairing.try_bind("/start x", user_id=1, chat_id=1) == "not_pending"


def test_bind_by_deep_link_needs_confirmation(pairing, store):
    started = pairing.start()
    assert pairing.is_pending() is True
    assert started["token"] not in (store.root / "pairing.json").read_text()
    result = pairing.try_bind(f"/start {started['token']}", user_id=42, chat_id=42,
                              name="Иван Иванов", username="ivan")
    assert result == "accepted"
    assert pairing.is_pending() is False               # второе сообщение уже не ждём
    assert store.read("owner") == {}                   # до подтверждения владельца нет
    status = pairing.status()
    assert status["pending"] is False and status["owner"] is None
    assert (status["candidate"]["user_id"], status["candidate"]["name"]) == (42, "Иван Иванов")

    confirmed = pairing.confirm()
    assert confirmed["user_id"] == 42
    owner = store.read("owner")
    assert (owner["user_id"], owner["chat_id"], owner["name"], owner["username"]) == \
        (42, 42, "Иван Иванов", "ivan")
    assert pairing.status()["owner"]["name"] == "Иван Иванов"
    assert pairing.status()["candidate"] is None


def test_rejected_candidate_never_becomes_owner(pairing, store):
    store.write("owner", {"user_id": 1, "chat_id": 1, "name": "Прежний"})
    started = pairing.start()
    assert pairing.try_bind(started["code"], user_id=666, chat_id=666, name="Чужой") == "accepted"
    pairing.reject()
    assert pairing.confirm() is None
    assert store.read("owner")["user_id"] == 1


def test_confirm_without_candidate_does_nothing(pairing, store):
    pairing.start()
    assert pairing.confirm() is None
    assert store.read("owner") == {}


def test_candidate_expires_with_the_window(pairing, store, clock):
    started = pairing.start()
    pairing.try_bind(started["code"], user_id=5, chat_id=5)
    clock.tick(PAIRING_TTL)
    assert pairing.status()["candidate"] is None
    assert pairing.confirm() is None and store.read("owner") == {}


def test_bind_by_typed_code(pairing, store):
    started = pairing.start()
    code = started["code"]
    assert pairing.try_bind(f"{code[:3]} {code[3:]}", user_id=5, chat_id=5) == "accepted"
    assert pairing.confirm()["user_id"] == 5


def test_value_works_once(pairing):
    started = pairing.start()
    assert pairing.try_bind(f"/start {started['token']}", user_id=1, chat_id=1) == "accepted"
    assert pairing.try_bind(f"/start {started['token']}", user_id=2, chat_id=2) == "not_pending"
    assert pairing.status()["candidate"]["user_id"] == 1


def test_window_expires(pairing, clock):
    started = pairing.start()
    clock.tick(PAIRING_TTL)
    assert pairing.is_pending() is False
    assert pairing.try_bind(f"/start {started['token']}", user_id=1, chat_id=1) == "expired"


def test_plain_text_is_not_counted_as_guess(pairing):
    pairing.start()
    for _ in range(PAIRING_MAX_WRONG + 5):
        assert pairing.try_bind("/start", user_id=1, chat_id=1) == "wrong"
        assert pairing.try_bind("здравствуйте", user_id=1, chat_id=1) == "wrong"
    assert pairing.is_pending() is True


def test_too_many_wrong_values_cancel_pairing(pairing, store):
    started = pairing.start()
    wrong = "000000" if started["code"] != "000000" else "111111"
    for _ in range(PAIRING_MAX_WRONG - 1):
        assert pairing.try_bind(wrong, user_id=9, chat_id=9) == "wrong"
    assert pairing.try_bind(wrong, user_id=9, chat_id=9) == "cancelled"
    assert pairing.try_bind(started["code"], user_id=1, chat_id=1) == "not_pending"
    assert store.read("owner") == {}


def test_restart_replaces_previous_values(pairing):
    first = pairing.start()
    second = pairing.start()
    assert pairing.try_bind(f"/start {first['token']}", user_id=1, chat_id=1) == "wrong"
    assert pairing.try_bind(f"/start {second['token']}", user_id=1, chat_id=1) == "accepted"
