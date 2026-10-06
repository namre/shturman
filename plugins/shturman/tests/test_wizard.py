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
    assert len(snap["business"]["plugin"]["ref"]) == 40


def test_marks(store):
    wizard.mark(store, "model_ok")
    wizard.mark(store, "completed")
    snap = wizard.snapshot(store)
    assert set(snap["marks"]) == {"model_ok", "completed"} and snap["completed"] is True
    with pytest.raises(ValueError):
        wizard.mark(store, "что-то")


def test_probe_output_parsing():
    ok = wizard.parse_probe_output(0, "\nsession_id: 2026_abc\nРаботает\n")
    assert ok == {"ok": True, "reply": "Работает"}
    noisy = wizard.parse_probe_output(0, "  ⚠ предупреждение сканера\nРаботает\nsession_id: y\n")
    assert noisy == {"ok": True, "reply": "Работает"}
    assert wizard.parse_probe_output(0, "session_id: x\n\n")["ok"] is False
    failed = wizard.parse_probe_output(1, "session_id: x\nError: 401 Unauthorized\n")
    assert failed["ok"] is False and "401" in failed["error"]
