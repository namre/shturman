"""Owner authority is a verified receipt, never inherited by archive workers."""
import asyncio

import pytest
from starlette.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from shturman import authority, confirm
from shturman.app import AppState, Gate
from shturman.config import Config
from shturman.events import Events
from shturman.remote_mcp import Gateway


def test_actor_string_and_setup_session_are_not_telegram_receipts():
    assert not authority.is_owner()
    with authority.setup_context("authenticated-session"):
        assert authority.is_owner()
        assert authority.current_owner_id() == "setup:authenticated-session"
        assert authority.get_owner_principal().source == "setup"
    assert not authority.is_owner()


async def test_owner_authority_does_not_leak_to_background_tasks(config):
    state = AppState(config, None, None)
    seen = []
    async def worker(payload=None):
        seen.append(authority.is_owner())
    events = Events()
    events.subscribe("test", worker)
    with authority.owner_context(1000, chat_id=1000):
        task = state.spawn(worker(), name="authority-test")
        events.publish("test", {})
        assert authority.is_owner()
        await task
        await events.drain()
    assert seen == [False, False]


async def test_legacy_claimed_owner_callback_cannot_touch_confirmation_database():
    class NoDatabase:
        def __getattr__(self, name):
            raise AssertionError("unverified callback reached database")
    with pytest.raises(confirm.Refused) as error:
        await confirm._pressed(NoDatabase(), "y:1:nonce", 1000)
    assert error.value.code == "owner_required"
    with authority.setup_context("browser"):
        with pytest.raises(confirm.Refused):
            await confirm._pressed(NoDatabase(), "y:1:nonce", 1000)


async def test_remote_host_cannot_use_internal_static_token_for_api(config):
    import dataclasses
    cfg = dataclasses.replace(config, remote_mcp_origin="https://memory.example.com")
    calls = []
    async def inner(scope, receive, send):
        calls.append(scope["path"])
        await JSONResponse({"inner": True})(scope, receive, send)
    gate = Gate(inner, cfg, remote=Gateway(inner, cfg))
    async with AsyncClient(transport=ASGITransport(app=gate), base_url=cfg.remote_mcp_origin) as client:
        response = await client.get("/api/accounts", headers={"Authorization": "Bearer " + cfg.api_token})
        assert response.status_code == 404
        assert (await client.get("/health")).status_code == 404
    assert calls == []


@pytest.mark.parametrize("origin", ["http://memory.example.com", "https://setup.example.com", "https://hermes.example.com"])
def test_remote_endpoint_requires_separate_https_origin(config, origin):
    import dataclasses
    cfg = dataclasses.replace(config, remote_mcp_origin=origin,
                              setup_origin="https://setup.example.com", dashboard_origin="https://hermes.example.com")
    with pytest.raises(ValueError):
        Gateway(None, cfg)
