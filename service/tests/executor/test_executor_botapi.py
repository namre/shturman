"""Клиент Bot API: три вида итога, один запрос на вызов, токен не выходит наружу."""

import logging

import httpx
import pytest

from shturman.executor.botapi import (
    ALLOWED_UPDATES, BotApi, NeverLeft, OutcomeUnknown, Refused, reason_code,
)

from exec_fakes import TOKEN, FakeTelegram, ok, refusal


@pytest.fixture
def tg():
    return FakeTelegram()


async def test_token_is_put_into_the_address_only_at_the_last_moment(tg, caplog):
    caplog.set_level(logging.DEBUG)          # всё, включая журналы httpx и httpcore
    api = BotApi(TOKEN, transport=tg.transport())
    assert (await api.get_me())["username"]
    assert tg.foreign_paths == [] and tg.requests[0][0] == "getMe"     # Telegram получил настоящий адрес
    assert any(r.name == "httpx" for r in caplog.records)             # httpx запрос в журнал записал…
    assert "SENTINEL" not in caplog.text and TOKEN not in caplog.text  # …но без токена
    assert "/bot/getMe" in caplog.text
    await api.aclose()


@pytest.mark.parametrize("failure, expected", [
    (httpx.ConnectError(f"no route to https://api.telegram.org/bot{TOKEN}/x"), NeverLeft),
    (httpx.ConnectTimeout("t"), NeverLeft),
    (httpx.PoolTimeout("t"), NeverLeft),
    (httpx.ReadTimeout("t"), OutcomeUnknown),
    (httpx.WriteTimeout("t"), OutcomeUnknown),
    (httpx.ReadError("reset"), OutcomeUnknown),
    (httpx.RemoteProtocolError("closed"), OutcomeUnknown),
    (httpx.Response(500, json={"ok": False, "error_code": 500, "description": "Internal Server Error"}), OutcomeUnknown),
    (httpx.Response(502, text="<html>bad gateway</html>"), OutcomeUnknown),
    (httpx.Response(403, text="<html>blocked by proxy</html>"), OutcomeUnknown),   # отказал не Telegram
    (httpx.Response(200, text="not json"), OutcomeUnknown),
    (refusal(400, "Bad Request: chat not found"), Refused),
    (refusal(403, "Forbidden: bot was blocked by the user"), Refused),
    (refusal(401, "Unauthorized"), Refused),
    (refusal(429, "Too Many Requests: retry after 7", retry_after=7), Refused),
])
async def test_every_failure_falls_into_one_of_three_kinds_and_is_one_request(tg, caplog, failure, expected):
    caplog.set_level(logging.DEBUG)
    tg.script["sendMessage"] = [failure]
    api = BotApi(TOKEN, transport=tg.transport())
    with pytest.raises(expected) as caught:
        await api.send_message(1000, "Секретный текст черновика")
    assert len(tg.calls("sendMessage")) == 1                 # клиент сам ничего не повторяет
    shown = f"{caught.value!r} {caught.value} {caught.value.__cause__!r} {caught.value.__context__!r}"
    assert "SENTINEL" not in shown and "Секретный" not in shown and "api.telegram.org" not in shown
    assert "SENTINEL" not in caplog.text and "Секретный" not in caplog.text
    if expected is Refused and failure.status_code == 429:
        assert caught.value.retry_after == 7 and caught.value.reason == "too_many_requests"
    await api.aclose()


async def test_send_parameters_are_plain_text_with_optional_parts(tg):
    api = BotApi(TOKEN, transport=tg.transport())
    card = await api.send_message(1000, "*не жирный* <b>и не тег</b>", buttons=[[("Да", "sh:cf:y:1:n")]],
                                  silent=True, no_preview=True)
    reply = await api.send_message(2001, "Добрый день", business_connection_id="bc-1", reply_to=41)
    first, second = tg.calls("sendMessage")
    assert card == 501 and reply == 502
    assert "parse_mode" not in first and "parse_mode" not in second
    assert first == {"chat_id": 1000, "text": "*не жирный* <b>и не тег</b>", "disable_notification": True,
                     "reply_markup": {"inline_keyboard": [[{"text": "Да", "callback_data": "sh:cf:y:1:n"}]]},
                     "link_preview_options": {"is_disabled": True}}
    assert second == {"chat_id": 2001, "text": "Добрый день", "business_connection_id": "bc-1",
                      "reply_parameters": {"message_id": 41}}
    await api.aclose()


async def test_long_polling_asks_only_for_the_updates_the_bot_handles(tg):
    api = BotApi(TOKEN, transport=tg.transport())
    tg.push(message={"message_id": 1})
    assert [u["update_id"] for u in await api.get_updates(None, poll=0)] == [101]
    assert await api.get_updates(102, poll=0) == []
    first, second = tg.calls("getUpdates")
    assert "offset" not in first and second["offset"] == 102
    assert first["allowed_updates"] == list(ALLOWED_UPDATES) == [
        "message", "callback_query", "business_connection", "business_message",
        "edited_business_message", "deleted_business_messages"]
    await api.aclose()


async def test_success_without_message_number_is_not_reported_as_sent_or_refused(tg):
    tg.script["sendMessage"] = [ok(True)]
    api = BotApi(TOKEN, transport=tg.transport())
    with pytest.raises(OutcomeUnknown):
        await api.send_message(1000, "текст")
    await api.aclose()


async def test_broken_setup_never_goes_around_the_proxy():
    for api, why in ((BotApi("не токен"), "bad_token_format"),
                     (BotApi(TOKEN, proxy_url="socks5://127.0.0.1:1"), "proxy_needs_socksio")):
        if api.broken is None:       # пакет socksio установлен — прокси SOCKS поддержан, проверять нечего
            await api.aclose()
            continue
        assert api.broken == why
        with pytest.raises(NeverLeft):
            await api.get_me()


def test_refusal_reason_is_a_code_not_the_description():
    assert reason_code("Bad Request: message is not modified: specified new message content…") == "not_modified"
    assert reason_code("Bad Request: BUSINESS_PEER_INVALID") == "business_peer_invalid"
    assert reason_code("Conflict: terminated by other getUpdates request; make sure…") == "other_poller"
    assert reason_code("Conflict: can't use getUpdates method while webhook is active") == "webhook"
    assert reason_code("Bad Request: что-то с текстом «Секретный текст»") == "other"
    assert reason_code(None) == "other"


async def test_voice_file_is_downloaded_with_the_token_only_in_the_real_address(caplog):
    """getFile, затем файл по /file/bot<токен>/<путь>: токен появляется только в настоящем адресе."""
    caplog.set_level(logging.DEBUG)
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path)
        if request.url.raw_path.endswith(b"/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_id": "v", "file_size": 9,
                                                                   "file_path": "voice/file_1.oga"}})
        return httpx.Response(200, content=b"OggS-data")

    api = BotApi(TOKEN, transport=httpx.MockTransport(handle))
    assert await api.download_file("v", max_bytes=1000) == b"OggS-data"
    assert seen == [f"/bot{TOKEN}/getFile".encode(), f"/file/bot{TOKEN}/voice/file_1.oga".encode()]
    assert TOKEN not in caplog.text
    with pytest.raises(Refused) as too_big:
        await api.download_file("v", max_bytes=5)
    assert too_big.value.reason == "file_too_big"
    await api.aclose()

    def bad_path(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"file_path": "../../etc/passwd"}})
    api = BotApi(TOKEN, transport=httpx.MockTransport(bad_path))
    with pytest.raises(Refused):
        await api.download_file("v", max_bytes=1000)
    await api.aclose()
