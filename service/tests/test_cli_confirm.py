"""Команды оператора и ответ «ждёт подтверждения в боте»: его нельзя принять за «сделано»."""

import json

import pytest

from shturman import cli

WAITING = {"status": "pending_confirmation", "action_id": 7, "expires_at": "2026-10-07T12:00:00+00:00",
           "summary": "Добавить в доверенные: Иван Петров (идентификатор Telegram 2001).",
           "note": "Ждёт вашего подтверждения в боте согласований."}


def test_call_explains_a_waiting_action_in_plain_words(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_local_api", lambda method, path, payload=None: (202, WAITING))
    cli._call("POST", "/api/outbox/trusted", '{"tg_user_id": 2001}')
    out, err = capsys.readouterr()
    assert json.loads(out) == WAITING                       # ответ сервиса — как есть, для разбора
    assert "ЖДЁТ ПОДТВЕРЖДЕНИЯ В БОТЕ" in err and "Действие не выполнено" in err
    assert "действие № 7" in err and "Иван Петров" in err
    assert "shturman call GET /api/confirmations/7" in err and "/api/confirmations/7/cancel" in err


def test_call_says_what_was_applied_at_once(monkeypatch, capsys):
    answer = {**WAITING, "applied_now": {"chat_window_max": 2}}
    monkeypatch.setattr(cli, "_local_api", lambda method, path, payload=None: (202, answer))
    cli._call("PUT", "/api/outbox/policy", "{}")
    assert "уже применена" in capsys.readouterr().err


def test_ordinary_answers_get_no_extra_words(monkeypatch, capsys):
    # 202 бывает и у обычных ответов (идёт импорт, идёт просмотр файла) — это не ожидание владельца
    monkeypatch.setattr(cli, "_local_api", lambda method, path, payload=None: (202, {"state": "running"}))
    cli._call("POST", "/api/imports/" + "a" * 32 + "/run", None)
    out, err = capsys.readouterr()
    assert json.loads(out) == {"state": "running"} and err == ""
    assert cli.pending_text({"status": "pending"}) is None and cli.pending_text({"ok": True}) is None


def test_failed_call_still_exits_with_an_error(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_local_api", lambda method, path, payload=None: (409, {"error": "нельзя"}))
    with pytest.raises(SystemExit) as stop:
        cli._call("POST", "/api/outbox/trusted", "{}")
    assert stop.value.code == 1


def answers(monkeypatch, *replies):
    """Подставной сервис: ответы по очереди; запоминает, что спрашивали."""
    asked, queue = [], list(replies)

    def local_api(method, path, payload=None):
        asked.append((method, path))
        return queue.pop(0)

    monkeypatch.setattr(cli, "_local_api", local_api)
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    return asked


def test_login_command_waits_for_the_owner_and_stops_on_refusal(monkeypatch, capsys):
    asked = answers(monkeypatch, (200, {"id": 7, "status": "pending"}), (200, {"id": 7, "status": "rejected"}))
    with pytest.raises(SystemExit) as stop:
        cli._await_confirmation(WAITING)
    assert "отклонён" in str(stop.value.code)
    assert asked == [("GET", "/api/confirmations/7")] * 2            # новых запросов на вход не было
    assert "подтвердить в боте согласований" in capsys.readouterr().out


def test_login_command_goes_on_after_the_owner_agrees(monkeypatch):
    asked = answers(monkeypatch, (200, {"id": 7, "status": "pending"}), (200, {"id": 7, "status": "applied"}))
    assert cli._await_confirmation(WAITING) is None
    assert len(asked) == 2
