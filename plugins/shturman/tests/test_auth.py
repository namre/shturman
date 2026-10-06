import pytest

from shturman_core.auth import (
    ACCESS_TTL, ACTIVATION_TTL, LOCK_STEPS, LOGIN_CODE_TTL, LOGIN_MAX_ATTEMPTS,
    LOGIN_RESEND_INTERVAL, REFRESH_TTL, Auth,
)


class Outbox:
    def __init__(self):
        self.codes, self.notices, self.fail = [], [], False

    def send(self, owner, code):
        if self.fail:
            raise RuntimeError("нет связи")
        self.codes.append((owner["chat_id"], code))

    def notify(self, owner, text):
        self.notices.append(text)


@pytest.fixture
def outbox():
    return Outbox()


@pytest.fixture
def auth(store, clock, outbox):
    return Auth(store, now=clock, send_code=outbox.send, notify=outbox.notify)


def bind(store):
    store.write("owner", {"user_id": 7, "chat_id": 7, "name": "Иван"})


# --- ссылка активации ---

def test_activation_is_single_use(auth):
    value = auth.issue_activation()
    assert auth.redeem_activation(value) is True
    assert auth.redeem_activation(value) is False


def test_activation_expires(auth, clock):
    value = auth.issue_activation()
    clock.tick(ACTIVATION_TTL)
    assert auth.redeem_activation(value) is False


def test_new_activation_replaces_previous(auth):
    old = auth.issue_activation()
    new = auth.issue_activation()
    assert auth.redeem_activation(old) is False
    assert auth.redeem_activation(new) is True


def test_wrong_activation_does_not_burn_the_real_one(auth):
    value = auth.issue_activation()
    assert auth.redeem_activation("не-та") is False
    assert auth.redeem_activation(value) is True


def test_activation_value_is_not_stored(auth, store):
    value = auth.issue_activation()
    assert value not in (store.root / "activation.json").read_text()


def test_complete_routes_activation_prefix(auth):
    value = auth.issue_activation()
    assert auth.complete("a." + value) == (True, "ok")
    assert auth.complete("a." + value) == (False, "activation_invalid")


# --- код от бота ---

def test_no_owner_means_no_code(auth, outbox):
    assert auth.request_login_code() == "no_owner"
    assert outbox.codes == []


def test_code_is_sent_and_accepted_once(auth, store, outbox):
    bind(store)
    assert auth.request_login_code() == "sent"
    chat_id, code = outbox.codes[-1]
    assert chat_id == 7 and len(code) == 8 and code.isdigit()
    assert code not in (store.root / "login.json").read_text()
    assert auth.verify_login_code(f"{code[:4]} {code[4:]}") == "ok"    # пробел при вводе допустим
    assert auth.verify_login_code(code) == "none"


def test_resend_is_throttled(auth, store, clock, outbox):
    bind(store)
    assert auth.request_login_code() == "sent"
    assert auth.request_login_code() == "reused"
    assert len(outbox.codes) == 1
    clock.tick(LOGIN_RESEND_INTERVAL)
    assert auth.request_login_code() == "sent"
    assert len(outbox.codes) == 2
    assert auth.verify_login_code(outbox.codes[0][1]) == "wrong"       # старый код заменён
    assert auth.verify_login_code(outbox.codes[1][1]) == "ok"


def test_code_expires(auth, store, clock, outbox):
    bind(store)
    auth.request_login_code()
    clock.tick(LOGIN_CODE_TTL)
    assert auth.verify_login_code(outbox.codes[-1][1]) == "expired"


def test_send_failure_keeps_no_code(auth, store, outbox):
    bind(store)
    outbox.fail = True
    assert auth.request_login_code() == "send_failed"
    assert auth.verify_login_code("00000000") == "none"


def test_lock_after_repeated_wrong_codes(auth, store, clock, outbox):
    bind(store)
    auth.request_login_code()
    code = outbox.codes[-1][1]
    wrong = "0" * 8 if code != "0" * 8 else "1" * 8
    for _ in range(LOGIN_MAX_ATTEMPTS - 1):
        assert auth.verify_login_code(wrong) == "wrong"
    assert auth.verify_login_code(wrong) == "locked"
    assert len(outbox.notices) == 1
    assert auth.verify_login_code(code) == "locked"            # даже верный код не принимается
    assert auth.request_login_code() == "locked"
    assert auth.login_lock_remaining() == LOCK_STEPS[0]
    clock.tick(LOCK_STEPS[0])
    assert auth.request_login_code() == "sent"


def test_fresh_code_does_not_reset_wrong_attempts(auth, store, clock, outbox):
    """Перебор «по четыре попытки на каждый новый код» должен упираться в блокировку."""
    bind(store)
    auth.request_login_code()
    for _ in range(LOGIN_MAX_ATTEMPTS - 1):
        assert auth.verify_login_code("00000000") in ("wrong", "ok")
    clock.tick(LOGIN_RESEND_INTERVAL)
    assert auth.request_login_code() == "sent"
    assert auth.verify_login_code("00000001") == "locked"


def test_locks_escalate_and_reset_on_success(auth, store, clock, outbox):
    bind(store)
    for level in range(2):
        clock.tick(LOCK_STEPS[-1])
        auth.request_login_code()
        for _ in range(LOGIN_MAX_ATTEMPTS):
            auth.verify_login_code("99999999" if outbox.codes[-1][1] != "99999999" else "11111111")
        assert auth.login_lock_remaining() == LOCK_STEPS[level]
    clock.tick(LOCK_STEPS[-1])
    auth.request_login_code()
    assert auth.verify_login_code(outbox.codes[-1][1]) == "ok"
    assert store.read("login") == {}


def test_activation_clears_login_lock(auth, store, clock, outbox):
    bind(store)
    auth.request_login_code()
    for _ in range(LOGIN_MAX_ATTEMPTS):
        auth.verify_login_code("00000000" if outbox.codes[-1][1] != "00000000" else "1" * 8)
    assert auth.request_login_code() == "locked"
    assert auth.redeem_activation(auth.issue_activation()) is True
    assert auth.request_login_code() == "sent"


# --- сессии ---

def test_session_tokens_verify_and_expire(auth, store, clock):
    bind(store)
    s = auth.mint_session()
    assert s.display_name == "Иван"
    assert auth.verify_access(s.access_token) == s.expires_at
    assert auth.verify_access(s.refresh_token) is None           # вид токена важен
    clock.tick(ACCESS_TTL)
    assert auth.verify_access(s.access_token) is None
    renewed = auth.refresh(s.refresh_token)
    assert renewed is not None and auth.verify_access(renewed.access_token)
    clock.tick(REFRESH_TTL)
    assert auth.refresh(s.refresh_token) is None


def test_logout_all_invalidates_sessions(auth):
    s = auth.mint_session()
    auth.revoke_all_sessions()
    assert auth.verify_access(s.access_token) is None
    assert auth.refresh(s.refresh_token) is None
    assert auth.verify_access(auth.mint_session().access_token)


def test_tokens_of_another_instance_are_rejected(auth, tmp_path, clock):
    from shturman_core.state import Store
    other = Auth(Store(tmp_path / "other"), now=clock)
    assert auth.verify_access(other.mint_session().access_token) is None
