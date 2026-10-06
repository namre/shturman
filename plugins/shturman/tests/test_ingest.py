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
        self.disabled: set[str] = set()      # подключения, которые сервис держит выключенными
        self.excluded: set[int] = set()      # сообщения из исключённых чатов
        self.heal_on_connection = True       # повторно присланное подключение сервис включает
        self.broken: set[int] = set()        # сообщения, которые сервис не может разобрать

    async def __call__(self, method, path, json_body=None, *, timeout=None):
        self.calls.append((method, path, json_body))
        if self.down:
            raise ServiceUnavailable("нет связи")
        if path == "/api/owner":
            self.owner = json_body if method == "PUT" else None
            return {"ok": True}
        if path == CONNECTION:
            if self.owner is None:
                raise ServiceError("владелец ещё не привязан", status=409, code="owner_unknown")
            cid = json_body["connection"]["id"]
            if cid in self.strangers:
                raise ServiceError("чужое подключение", status=403, code="not_owner")
            self.known.add(cid)
            if self.heal_on_connection:
                self.disabled.discard(cid)
            return {"ok": True}
        cid = json_body.get("business_connection_id") or json_body.get("message", {}).get("business_connection_id")
        if cid not in self.known:
            raise ServiceError("неизвестное бизнес-подключение", status=409, code="unknown_connection")
        if cid in self.disabled:
            return ({"stored": False, "message_id": None, "reason": "connection_disabled"} if path == MESSAGE
                    else {"deleted": 0, "reason": "connection_disabled"})
        if path == MESSAGE and json_body["message"]["message_id"] in self.broken:
            raise ServiceError("поле chat: нужен объект", status=400)
        if path == MESSAGE and json_body["message"]["message_id"] in self.excluded:
            return {"stored": False, "message_id": None, "reason": "excluded"}
        if path == MESSAGE and json_body["message"]["message_id"] is None:
            return {"stored": False, "message_id": None, "reason": "no_message_id"}
        return {"stored": True} if path == MESSAGE else {"deleted": 1}

    def paths(self):
        return [path for _, path, _ in self.calls]


class State:
    """Состояние привязки, каким его видит шлюз: владелец, отметка сброса, ошибка чтения."""

    def __init__(self, owner) -> None:
        self.owner, self.marker, self.broken, self.v = owner, False, False, 1

    def read_owner(self):
        if self.broken:
            raise OSError("диск не отвечает")
        return self.owner

    def read_marker(self):
        if self.broken:
            raise OSError("диск не отвечает")
        return self.marker

    def clear_marker(self):
        self.marker = False
        self.v += 1

    def set(self, **changes):
        for key, value in changes.items():
            setattr(self, key, value)
        self.v += 1


def make_state(owner=OWNER, **kwargs):
    service, stats, state = FakeService(), Stats(), State(owner)
    ingest = Ingest(service, owner=state.read_owner, owner_version=lambda: state.v,
                    unbound_marker=state.read_marker, clear_marker=state.clear_marker, stats=stats, **kwargs)
    return ingest, service, state


def owner_calls(service):
    return [(m, body) for m, p, body in service.calls if p == "/api/owner"]


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
    # Свежий процесс без владельца: сервису о владельце не сообщается ничего — ни PUT, ни DELETE.
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


# --- когда сервису говорят «владелец отвязан» ---

def test_first_install_never_wipes_the_owner_the_service_remembers():
    """Свежий процесс, состояние пусто (первая установка либо каталог состояния потерян):
    сервис хранит своего владельца, пока ему явно не скажут иное."""
    ingest, service, state = make_state(owner={})
    service.owner = {"user_id": 42, "chat_id": 42}
    drain(ingest)
    drain(ingest)
    assert owner_calls(service) == [] and service.owner == {"user_id": 42, "chat_id": 42}
    state.set(owner=OWNER)                                   # владельца привязали в мастере
    drain(ingest)
    assert owner_calls(service) == [("PUT", {"user_id": 42, "chat_id": 42})]


def test_recovery_link_while_gateway_runs_unbinds_the_owner_once():
    ingest, service, state = make_state()
    drain(ingest)
    assert owner_calls(service) == [("PUT", {"user_id": 42, "chat_id": 42})]
    state.set(owner={}, marker=True)                         # вход по ссылке восстановления
    drain(ingest)
    drain(ingest)
    assert owner_calls(service)[1:] == [("DELETE", None)]    # один раз
    assert state.marker is False and service.owner is None


def test_recovery_link_while_gateway_was_down_is_not_lost():
    """Привязку сбросили, пока шлюз не работал: отметка лежит на диске, и свежий процесс её исполняет."""
    ingest, service, state = make_state(owner={})
    state.marker = True
    service.owner = {"user_id": 42, "chat_id": 42}
    drain(ingest)
    assert owner_calls(service) == [("DELETE", None)] and state.marker is False and service.owner is None
    drain(ingest)
    assert len(owner_calls(service)) == 1


def test_rebinding_before_the_gateway_noticed_still_resets_the_service_first():
    ingest, service, state = make_state(owner={"user_id": 77, "chat_id": 77})
    state.marker = True                                      # сброс и новая привязка прошли без шлюза
    service.owner = {"user_id": 42, "chat_id": 42}
    drain(ingest)
    assert owner_calls(service) == [("DELETE", None), ("PUT", {"user_id": 77, "chat_id": 77})]
    assert state.marker is False


def test_owner_that_this_process_announced_and_then_vanished_is_unbound():
    ingest, service, state = make_state()
    drain(ingest)
    state.set(owner={})                                      # записи достоверно нет, отметки тоже
    drain(ingest)
    assert owner_calls(service)[-1] == ("DELETE", None)


def test_unreadable_state_changes_nothing(caplog):
    """Сбой чтения — не «владельца нет»: сервис не должен из-за него отклонить черновики
    и очистить список доверенных."""
    ingest, service, state = make_state()
    drain(ingest)
    state.set(broken=True)
    with caplog.at_level("WARNING"):
        for _ in range(5):
            drain(ingest)
    assert owner_calls(service) == [("PUT", {"user_id": 42, "chat_id": 42})]
    assert len([r for r in caplog.records if "не прочитано" in r.getMessage()]) == 1     # один раз, не на каждый шаг
    ingest.put_connection(CONN)                              # очередь при этом не стоит
    drain(ingest)
    assert CONNECTION in service.paths()
    state.set(broken=False)
    drain(ingest)
    assert [m for m, _ in owner_calls(service)] == ["PUT", "PUT"]     # прочитали — владелец на месте, DELETE не было


def test_unreadable_state_on_a_fresh_process_then_recovery_mark():
    ingest, service, state = make_state(owner={})
    state.broken = True
    drain(ingest)
    assert owner_calls(service) == []
    state.set(broken=False, marker=True)
    drain(ingest)
    assert owner_calls(service) == [("DELETE", None)]


def test_unbinding_waits_for_the_service_and_is_not_forgotten():
    ingest, service, state = make_state()
    drain(ingest)
    state.set(owner={}, marker=True)
    service.down = True
    with pytest.raises(_Later):
        asyncio.run(ingest.step())
    assert state.marker is True                              # отметка снимается только после ответа сервиса
    service.down = False
    drain(ingest)
    assert owner_calls(service)[-1] == ("DELETE", None) and state.marker is False


# --- сервис принял запрос, но сообщение не записал ---

def test_disabled_connection_is_checked_with_telegram_once_and_message_retried():
    ingest, service, stats, fetched, _ = make()
    service.known.add("bc1")
    service.disabled.add("bc1")
    ingest.put_message(message(1), edited=False)
    drain(ingest)
    assert fetched == ["bc1"]
    assert [p for p in service.paths() if p != "/api/owner"] == [MESSAGE, CONNECTION, MESSAGE]
    assert stats.counters["forwarded_messages"] == 1 and stats.counters["not_stored_disabled"] == 0
    assert stats.business_disabled is False


def test_connection_that_stays_disabled_is_counted_flagged_and_not_asked_about_again():
    ingest, service, stats, fetched, _ = make()
    service.known.add("bc1")
    service.disabled.add("bc1")
    service.heal_on_connection = False                       # владелец выключил бота в Telegram
    for mid in (1, 2, 3):
        ingest.put_message(message(mid), edited=False)
    ingest.put_deleted({"business_connection_id": "bc1", "chat": {"id": 99, "type": "private"}, "message_ids": [1]})
    drain(ingest)
    assert fetched == ["bc1"]                                # Telegram спросили один раз
    assert stats.counters["not_stored_disabled"] == 4 and stats.counters["forwarded_messages"] == 0
    assert stats.counters["forwarded_deleted"] == 0 and stats.counters["rejected"] == 0
    assert stats.business_disabled is True
    service.disabled.clear()                                 # владелец включил обратно
    ingest.put_message(message(4), edited=False)
    drain(ingest)
    assert stats.counters["forwarded_messages"] == 1 and stats.business_disabled is False


def test_excluded_chat_and_message_without_number_have_their_own_counters():
    ingest, service, stats, fetched, _ = make()
    service.known.add("bc1")
    service.excluded.add(5)
    ingest.put_message(message(5), edited=False)
    ingest.put_message(message(None), edited=False)
    ingest.put_message(message(6), edited=False)
    drain(ingest)
    assert (stats.counters["not_stored_excluded"], stats.counters["not_stored_other"],
            stats.counters["forwarded_messages"]) == (1, 1, 1)
    assert fetched == [] and stats.business_disabled is False
