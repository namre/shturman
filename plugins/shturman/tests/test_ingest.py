"""Пересылка обновлений бизнес-режима в сервис: очередь, повторы, потери."""

import asyncio

import pytest

from shturman_core.bridge_stats import Stats
from shturman_core.ingest import CONNECTION, DELETED, MESSAGE, Ingest, _Later
from shturman_core.service_client import ServiceError, ServiceUnavailable

OWNER = {"user_id": 42, "chat_id": 42}
CONN = {"id": "bc1", "user": {"id": 42, "is_bot": False, "first_name": "Иван"}, "is_enabled": True,
        "rights": {"can_reply": True}}


def message(mid=1, connection="bc1"):
    return {"message_id": mid, "business_connection_id": connection, "chat": {"id": 99, "type": "private"},
            "text": "секретный текст"}


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.down = False
        self.known: set[str] = set()         # подключения, которые сервис знает
        self.owner: dict | None = None
        self.strangers: set[str] = set()     # подключения, созданные не владельцем
        self.broken: set[int] = set()        # сообщения, которые сервис не может разобрать

    async def __call__(self, method, path, json_body=None, *, timeout=None):
        self.calls.append((method, path, json_body))
        if self.down:
            raise ServiceUnavailable("нет связи")
        if path == "/api/owner":
            self.owner = json_body
            return {"ok": True}
        if path == CONNECTION:
            if self.owner is None:
                raise ServiceError("владелец ещё не привязан", status=409, code="owner_unknown")
            cid = json_body["connection"]["id"]
            if cid in self.strangers:
                raise ServiceError("чужое подключение", status=403, code="not_owner")
            self.known.add(cid)
            return {"ok": True}
        cid = json_body.get("business_connection_id") or json_body.get("message", {}).get("business_connection_id")
        if cid not in self.known:
            raise ServiceError("неизвестное бизнес-подключение", status=409, code="unknown_connection")
        if path == MESSAGE and json_body["message"]["message_id"] in self.broken:
            raise ServiceError("поле chat: нужен объект", status=400)
        return {"stored": True}

    def paths(self):
        return [path for _, path, _ in self.calls]


def make(*, owner=OWNER, fetch=None, **kwargs):
    service, stats = FakeService(), Stats()
    version = {"v": 1}
    fetched: list[str] = []

    async def fetch_connection(cid):
        fetched.append(cid)
        if isinstance(fetch, BaseException):
            raise fetch
        return fetch(cid) if callable(fetch) else dict(CONN, id=cid)

    ingest = Ingest(service, owner=lambda: owner, owner_version=lambda: version["v"],
                    fetch_connection=fetch_connection, stats=stats, **kwargs)
    return ingest, service, stats, fetched, version


def drain(ingest):
    async def go():
        while await ingest.step():
            pass
    asyncio.run(go())


def test_owner_is_pushed_at_start_and_updates_are_forwarded_in_order():
    ingest, service, stats, _, _ = make()
    assert ingest.put_connection(CONN) and ingest.put_message(message(1), edited=False)
    ingest.put_message(message(1), edited=True)
    ingest.put_deleted({"business_connection_id": "bc1", "chat": {"id": 99, "type": "private"}, "message_ids": [1]})
    drain(ingest)
    assert service.calls[0] == ("PUT", "/api/owner", {"user_id": 42, "chat_id": 42})
    assert service.paths()[1:] == [CONNECTION, MESSAGE, MESSAGE, DELETED]
    assert service.calls[2][2] == {"message": message(1), "edited": False}
    assert service.calls[3][2]["edited"] is True
    assert (stats.counters["forwarded_connections"], stats.counters["forwarded_messages"],
            stats.counters["forwarded_deleted"]) == (1, 2, 1)
    assert len(ingest) == 0 and stats.queue == 0 and stats.reachable is True


def test_owner_is_pushed_again_only_when_binding_changes():
    ingest, service, _, _, version = make()
    drain(ingest)
    drain(ingest)
    assert service.paths() == ["/api/owner"]
    version["v"] = 2
    drain(ingest)
    assert service.paths() == ["/api/owner", "/api/owner"]


def test_without_owner_nothing_is_pushed_and_connection_is_rejected():
    ingest, service, stats, _, _ = make(owner={})
    ingest.put_connection(CONN)
    drain(ingest)
    assert "/api/owner" not in service.paths()
    assert stats.counters["rejected"] == 1 and len(ingest) == 0


def test_unknown_connection_is_fetched_resent_and_message_retried_once():
    ingest, service, stats, fetched, _ = make()
    ingest.put_message(message(5), edited=False)
    drain(ingest)
    assert fetched == ["bc1"]
    assert service.paths() == ["/api/owner", MESSAGE, CONNECTION, MESSAGE]
    assert stats.counters["forwarded_messages"] == 1 and stats.counters["forwarded_connections"] == 1
    assert stats.counters["rejected"] == 0


def test_strangers_connection_is_not_archived_and_not_asked_about_again():
    ingest, service, stats, fetched, _ = make()
    service.strangers.add("bc-чужой")
    for mid in (1, 2, 3):
        ingest.put_message(message(mid, "bc-чужой"), edited=False)
    drain(ingest)
    assert fetched == ["bc-чужой"]                               # спросили у Telegram один раз
    assert service.paths().count(MESSAGE) == 1                   # остальные даже не отправлялись
    assert stats.counters["rejected"] == 3 and stats.counters["forwarded_messages"] == 0


def test_connection_unknown_to_telegram_is_dropped():
    ingest, service, stats, _, _ = make(fetch=lambda cid: None)
    ingest.put_message(message(1), edited=False)
    drain(ingest)
    assert stats.counters["rejected"] == 1 and CONNECTION not in service.paths()


def test_owner_change_forgets_refused_connections():
    ingest, service, stats, fetched, version = make()
    service.strangers.add("bc2")
    ingest.put_message(message(1, "bc2"), edited=False)
    drain(ingest)
    service.strangers.clear()                                    # новый владелец — это его подключение
    version["v"] = 2
    ingest.put_message(message(2, "bc2"), edited=False)
    drain(ingest)
    assert fetched == ["bc2", "bc2"] and stats.counters["forwarded_messages"] == 1


def test_owner_unknown_to_service_is_pushed_and_request_repeated():
    ingest, service, stats, _, _ = make()
    drain(ingest)                       # владелец передан
    service.owner = None                # сервис его потерял (например, пересоздана база)
    ingest.put_connection(CONN)
    drain(ingest)
    assert service.paths() == ["/api/owner", CONNECTION, "/api/owner", CONNECTION]
    assert stats.counters["forwarded_connections"] == 1


def test_unparseable_message_is_dropped_and_the_queue_moves_on():
    ingest, service, stats, _, _ = make()
    service.known.add("bc1")
    service.broken.add(1)
    ingest.put_message(message(1), edited=False)
    ingest.put_message(message(2), edited=False)
    drain(ingest)
    assert stats.counters["rejected"] == 1 and stats.counters["forwarded_messages"] == 1


def test_while_service_is_down_updates_wait_and_then_arrive():
    ingest, service, stats, _, _ = make()
    service.down = True
    ingest.put_message(message(1), edited=False)
    with pytest.raises(_Later):
        asyncio.run(ingest.step())
    assert len(ingest) == 1 and stats.reachable is False and stats.counters["forwarded_messages"] == 0
    service.down = False
    drain(ingest)
    assert len(ingest) == 0 and stats.counters["forwarded_messages"] == 1


def test_telegram_outage_keeps_the_message_for_later():
    ingest, service, stats, fetched, _ = make(fetch=TimeoutError("нет связи с Telegram"))
    ingest.put_message(message(1), edited=False)
    with pytest.raises(_Later):
        asyncio.run(ingest.step())
    assert len(ingest) == 1 and stats.counters["rejected"] == 0


def test_full_queue_drops_new_updates_and_counts_them():
    ingest, service, stats, _, _ = make(capacity=3)
    service.down = True
    results = [ingest.put_message(message(mid), edited=False) for mid in range(1, 6)]
    assert results == [True, True, True, False, False]
    assert len(ingest) == 3 and stats.counters["dropped"] == 2 and stats.queue == 3
    service.down = False
    service.known.add("bc1")
    drain(ingest)
    sent = [body["message"]["message_id"] for _, path, body in service.calls if path == MESSAGE]
    assert sent == [1, 2, 3]                                     # потеряны последние, порядок сохранён


def test_run_loop_survives_outage_and_stops_on_cancel():
    ingest, service, stats, _, _ = make(idle=0.01)
    service.down = True

    async def scenario():
        task = asyncio.create_task(ingest.run())
        await asyncio.sleep(0.02)
        ingest.put_message(message(1), edited=False)
        await asyncio.sleep(0.05)
        assert len(ingest) == 1
        service.down = False
        for _ in range(400):
            if stats.counters["forwarded_messages"]:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert stats.counters["forwarded_messages"] == 1 and len(ingest) == 0
