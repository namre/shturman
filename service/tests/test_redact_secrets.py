"""Ключи и токены из переписки модели не уходят (Р-96): замена на «[ключ скрыт]» в одном месте —
в постановке любого запроса к модели (`bridge.request_structured`, `bridge.request_text`)."""

import pytest

from shturman import bridge, jobs
from shturman.sanitize import SECRET_MARK, redact_secrets

# Ненастоящие ключи собираются по частям: так их не принимает за утечку сканер секретов в git.
KEY = "sk" + "-" + "a1B2c3D4" * 6
JWT = "ey" + "JhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
TUNNEL = "ey" + "JhIjoiNGM2YzZiZjRmYmNiYzUwNjY3NDZmMmQ0ZGIzNjE5YTQiLCJ0IjoiZjQ0YjA4YzEtNGE5MS00ZmQ5"
BOT = "123456789" + ":" + "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw4"


@pytest.mark.parametrize("text,kept", [
    (f"Вот ключ {KEY}, держи", "Вот ключ"),
    ("Authorization: Bearer 3f2504e0-4f89-11d3-9a0c-0305e82c3301", "Authorization: Bearer"),
    (f"jwt {JWT}", "jwt"),
    (f"токен туннеля {TUNNEL}", "токен туннеля"),
    ("пароль: Qwerty123!", "пароль:"),
    ("пароль от wifi: kotik2020", "пароль от wifi:"),
    ("api key 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08", "api key"),
    ("ghp_" + "Ab1" * 12, ""),
    ("AKIA" + "IOSFODNN7EXAMPLE", ""),
    ("xox" + "b-1234567890-abcdefghijklmn", ""),
    (f"бот {BOT} готов", "бот"),
    ("-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEow\n-----END RSA " + "PRIVATE KEY-----", ""),
])
def test_secrets_are_replaced(text, kept):
    out = redact_secrets(text)
    assert SECRET_MARK in out and out.startswith(kept)
    for secret in (KEY, JWT, TUNNEL, "Qwerty123", "kotik2020", "3f2504e0", "MIIEow", "AAHdqTcv"):
        assert secret not in out


@pytest.mark.parametrize("text", [
    "Позвони +7 912 345-67-89, заказ 1234567890123",
    "Ссылка https://docs.google.com/document/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/edit — ключевой документ",
    "Коммит 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b исправил вёрстку",
    "Ключевой момент: 50000 рублей до 15.10",
    "t.me/joinchat/AAAAAEHbEkejzxUjAUCzYg и ещё task-manager-for-teams",
    "Номер договора 2026-10-06/ЖК-Северный-17, ИНН 7701234567",
    "Токен ещё не пришёл, жду ссылку",
])
def test_ordinary_text_is_left_alone(text):
    assert redact_secrets(text) == text


async def test_every_model_request_is_redacted(conn):
    job_id = await bridge.request_structured(
        conn, handler="t.redact", instructions="разбери", input=f"[1] У1: ключ {KEY} и пароль: hunter22",
        json_schema={"type": "object"}, schema_name="t")
    payload = (await jobs.get(conn, job_id))["payload"]
    assert KEY not in payload["input"] and "hunter22" not in payload["input"]
    assert payload["input"] == f"[1] У1: ключ {SECRET_MARK} и пароль: {SECRET_MARK}"
    text_job = await bridge.request_text(conn, handler="t.redact", messages=[
        {"role": "system", "content": "ты помощник"}, {"role": "user", "content": f"вот {JWT}"}])
    sent = (await jobs.get(conn, text_job))["payload"]["messages"]
    assert sent == [{"role": "system", "content": "ты помощник"}, {"role": "user", "content": f"вот {SECRET_MARK}"}]
