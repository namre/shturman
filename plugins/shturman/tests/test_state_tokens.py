import os
import stat

from shturman_core import tokens
from shturman_core.state import Store


def test_write_is_private_and_atomic(store):
    store.write("owner", {"chat_id": 1})
    path = store.root / "owner.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert store.read("owner") == {"chat_id": 1}
    assert [p.name for p in store.root.iterdir() if p.suffix == ".tmp"] == []


def test_read_tolerates_missing_and_broken(store):
    assert store.read("nothing") == {}
    store.root.mkdir(parents=True)
    (store.root / "bad.json").write_text("{не json")
    assert store.read("bad") == {}


def test_locked_persists_changes_only(store):
    with store.locked("login") as data:
        data["attempts"] = 2
    assert store.read("login") == {"attempts": 2}
    before = store.mtime("login")
    os.utime(store.root / "login.json", (1, 1))
    with store.locked("login"):
        pass
    assert store.mtime("login") == 1   # без изменений файл не переписывается
    assert before > 1


def test_secret_is_stable_between_instances(store):
    first = store.secret()
    assert len(first) == 32
    assert Store(store.root).secret() == first
    assert stat.S_IMODE((store.root / "secret.key").stat().st_mode) == 0o600


def test_rejects_path_tricks(store):
    import pytest
    with pytest.raises(ValueError):
        store.read("../etc/passwd")


def test_sign_and_unsign_roundtrip():
    secret = b"k" * 32
    token = tokens.sign({"sub": "owner", "kind": "access", "exp": 200}, secret)
    assert tokens.unsign(token, secret, "access", now=lambda: 100)["sub"] == "owner"
    assert tokens.unsign(token, secret, "refresh", now=lambda: 100) is None     # не тот вид
    assert tokens.unsign(token, secret, "access", now=lambda: 200) is None      # истёк
    assert tokens.unsign(token, b"x" * 32, "access", now=lambda: 100) is None   # чужой ключ
    assert tokens.unsign(token[:-4] + "AAAA", secret, "access", now=lambda: 100) is None
    assert tokens.unsign("мусор", secret, "access") is None


def test_digest_comparison():
    secret = b"k" * 32
    d = tokens.digest("482913", secret)
    assert tokens.same("482913", d, secret)
    assert not tokens.same("482914", d, secret)
    assert not tokens.same("", d, secret)
    assert not tokens.same("482913", "", secret)
