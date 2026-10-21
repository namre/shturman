"""Журнал сервиса не пишет адреса запросов целиком (cli.configure_logging): httpx на уровне INFO
печатает «HTTP Request: POST <адрес>», а среди адресов — адрес внешнего сторожа, который
./ops/pulse.sh целиком не печатает. База не нужна."""

import asyncio
import logging

import httpx

from shturman import cli

URL = "https://hc-ping.com/0f0e0d0c-aaaa-bbbb-cccc-1234567890ab"


def _post(url: str) -> None:
    async def go():
        transport = httpx.MockTransport(lambda request: httpx.Response(200))
        async with httpx.AsyncClient(transport=transport) as client:
            await client.post(url)
    asyncio.run(go())


def test_request_addresses_not_logged(caplog):
    saved = {name: logging.getLogger(name).level for name in cli.QUIET_LOGGERS}
    try:
        caplog.set_level(logging.INFO)
        cli.configure_logging()
        for name in cli.QUIET_LOGGERS:
            assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING, name
        _post(URL)
        assert "0f0e0d0c" not in caplog.text
        # Проверка проверки: без configure_logging httpx этот адрес пишет.
        for name in cli.QUIET_LOGGERS:
            logging.getLogger(name).setLevel(logging.NOTSET)
        caplog.clear()
        _post(URL)
        assert "0f0e0d0c" in caplog.text
    finally:
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)
