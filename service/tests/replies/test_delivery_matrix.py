"""Матрица состояний доставки ответа (Р-105): кнопки под черновиком — одни и те же везде, отправка — только
нажатием владельца или автоответом доверенному при включённой отправке.

Слой A — без базы: чистая функция `delivery.decide` перебирается по всему произведению фактов (16 384 случая,
доли секунды). Слой B — через настоящий сервис (архив, конвейер черновика, ответ «модели», карточка, нажатия,
отправщик) с подставным Telegram: способ работы × отправка × бизнес-подключение × кто написал × окно суток при
нажатии × остановка при нажатии × сессия основного аккаунта на связи («В чат», Р-105, часть 2).

Что проверяется в слое B для каждого мира:
  1. основной аккаунт не отправляет ничего — ни одного вызова отправки Telegram от него;
  2. задание `business.send` и согласованный черновик появляются только после нажатия «Отправить» (или у
     автоответа доверенному, когда отправка включена и канал доступен — тогда черновика сервиса нет вовсе);
  3. «Отправить» под карточкой ⇔ `delivery.decide` говорит «можно»; при закрытом окне или остановке нажатие
     ничего не согласует, черновик ждёт, а кнопки пересобираются без «Отправить»;
  4. кнопки сервиса в боте и в приложении («Согласования») совпадают (Д-46);
  5. «В чат» — только когда «Отправить» нет, сессия основного аккаунта на связи и это черновик сервиса; его
     нажатие кладёт текст в поле ввода и ничего не отправляет, а при остановке — ничего не кладёт.
"""

import dataclasses
import itertools
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rehearsal"))

from rehearsal_helpers import (ANNA, OWNER, answer, cards, chat, enable, live, message, model_jobs,  # noqa: E402,F401
                               payload, press, rv)

from shturman import bridge  # noqa: E402
from shturman.replies import delivery  # noqa: E402
from shturman.setup_page import approvals  # noqa: E402


# --- слой A: чистая функция ---------------------------------------------------------------------------

FIELDS = ("role", "private", "own_bot", "sending", "business", "window", "helper", "stopped", "card_whole",
          "pending", "suggest", "owner_session")
LENGTHS = (12, 256, 257, 5000)


def _expected_send(f: delivery.Facts) -> str | None:
    if not (f.pending and not f.stopped and f.sending and f.card_whole):
        return None
    if f.role == "owner":
        ok = f.private and f.business and f.window and f.text_len <= delivery.BUSINESS_TEXT_LIMIT
        return "business" if ok else None
    return "session" if f.helper else None


def _expected_put(f: delivery.Facts) -> bool:
    """«В чат» — только вместо «Отправить», только черновик сервиса в личном чате основного аккаунта."""
    return (_expected_send(f) is None and f.pending and f.suggest and not f.stopped and f.own_bot
            and f.role == "owner" and f.private and 0 < f.text_len <= delivery.BUSINESS_TEXT_LIMIT
            and f.owner_session)


def _all_facts():
    for values in itertools.product(("owner", "assistant"), *([(True, False)] * (len(FIELDS) - 1))):
        for length in LENGTHS:
            yield delivery.Facts(text_len=length, **dict(zip(FIELDS, values)))


DRAFT = {"id": 41, "nonce": "n" * 22, "status": "pending", "text": "Да, в четверг."}
ITEM = {"id": 7, "nonce": "m" * 16}


def test_decide_exhaustive(own_bot):
    seen = 0
    for f in _all_facts():
        seen += 1
        avail = delivery.decide(f)
        assert avail.send == _expected_send(f), f
        assert bool(avail.missing) == (avail.send is None), f
        assert avail.copy == (f.pending and 0 < f.text_len <= delivery.COPY_LIMIT), f
        assert avail.put == _expected_put(f), f
        assert not (avail.put and avail.send), f            # «Отправить» и «В чат» вместе не бывают
        draft = {**DRAFT, "status": "pending" if f.pending else "sent"}
        for item in (ITEM, None):
            bot = delivery.buttons(avail, draft=draft, item=item, link="https://t.me/anna_k", bot=True)
            app = delivery.buttons(avail, draft=draft, item=item, link="https://t.me/anna_k", bot=False)
            # Бот и приложение — одни и те же кнопки сервиса; голос, «Скопировать» и ссылка — только в боте.
            assert [d for d in delivery.callbacks(bot) if not d.startswith("sh:vr:")] == delivery.callbacks(app), f
            sends = [d for d in delivery.callbacks(app) if d.startswith("sh:ob:s:")]
            assert bool(sends) == (avail.send is not None), f
            puts = [d for d in delivery.callbacks(app) if d.startswith("sh:ob:p:")]
            # «В чат» — только у черновика сервиса (`item`); у черновика ассистента — нет (отложено)
            assert bool(puts) == (avail.put and item is not None), f
            assert not (puts and sends), f
    assert seen == 2 ** len(FIELDS) * len(LENGTHS) == 16384


def test_main_account_never_gets_a_session_send():
    """Основной аккаунт отвечает только через бизнес-бота: канала сессии у него нет при любых фактах."""
    for f in _all_facts():
        if f.role == "owner":
            assert delivery.decide(f).send in (None, "business")


# --- слой B: через сервис -------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class World:
    mode: str            # own | staff
    sending: bool
    business: str        # none | ok | no_reply
    who: str             # listed | trusted | on_demand
    window: str          # open | closed — окно суток к моменту нажатия
    stop: bool           # «Остановить всё» к моменту нажатия
    session: bool = True  # сессия основного аккаунта на связи («В чат»)

    @property
    def id(self) -> str:
        return "-".join([self.mode, "send" if self.sending else "nosend", "biz-" + self.business, self.who,
                         "win-" + self.window] + (["stop"] if self.stop else []) + ([] if self.session else ["nosess"]))


def _worlds() -> list[World]:
    out = []
    for n, (sending, business, who) in enumerate(itertools.product((True, False), ("none", "ok", "no_reply"),
                                                                  ("listed", "trusted", "on_demand"))):
        out.append(World(mode="staff" if n % 2 else "own", sending=sending, business=business, who=who,
                         window="closed" if n % 3 == 1 else "open", stop=n % 5 == 3, session=n % 4 != 2))
    # отдельно: всё для отправки на месте, нажатие — в открытом окне и без остановки, в обоих способах
    out += [World(mode, True, "ok", "listed", "open", False) for mode in ("own", "staff")]
    return out


WORLDS = _worlds()


async def _build(rv, w: World) -> tuple[int, int]:
    conn = rv.conn
    await conn.execute("INSERT INTO setup_state (key, value) VALUES ('scenario', $1::jsonb) "
                       "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", json.dumps({"scenario": w.mode}))
    rv.state.config = dataclasses.replace(rv.state.config, sending=w.sending)
    rv.state.extras["outbox"].state.config = rv.state.config
    if w.business != "none":
        await conn.execute("INSERT INTO business_connections (id, account_id, can_reply, enabled, via) "
                           "VALUES ('bc-owner', $1, $2, true, 'service')", rv.owner_acc, w.business == "ok")
    # «Отправить» основного аккаунта уходит только бизнес-ботом: подставной шлюз готов «отправить» от любого
    # аккаунта — если код где-то позовёт отправку от основного, тест это увидит.
    rv.tg.sendable.update({rv.owner_acc, rv.helper_acc})
    if w.session:
        rv.tg.puttable.add(rv.owner_acc)
    anna = await chat(conn, rv.owner_acc, ANNA, name="Анна Ким", username="anna_k")
    if w.who == "listed":
        await enable(conn)
    elif w.who == "trusted":
        await conn.execute("INSERT INTO outbox_trusted (tg_user_id) VALUES ($1)", ANNA)
        await conn.execute("INSERT INTO outbox_accounts (account_id, autoreply_enabled) VALUES ($1, true)",
                           rv.owner_acc)
    mid = await message(conn, anna, 1, "Встреча завтра в силе?")
    return anna, mid


async def _draft_card(rv, w: World, anna: int, mid: int):
    if w.who == "on_demand":
        from shturman.rehearsal import core
        await core.request_once(rv.state, mid)
    else:
        # Оба подписчика живого сообщения, как в сервисе: автоответ доверенным, затем уведомления и черновик.
        from shturman.outbox import autoreply
        await autoreply.on_message(rv.state.extras["outbox"], payload(rv.owner_acc, anna, mid))
        await live(rv, rv.owner_acc, anna, mid)
    jobs_ = await model_jobs(rv.conn)
    if not jobs_:
        return None
    await answer(rv.conn, jobs_[-1]["id"], {"outcome": "reply", "text": "Да, всё в силе."})
    return (await cards(rv.conn))[-1]


async def _owner_sends(rv) -> list:
    return [s for s in rv.tg.sent if s["account_id"] == rv.owner_acc]


@pytest.mark.parametrize("w", WORLDS, ids=[w.id for w in WORLDS])
async def test_world(rv, w):
    conn = rv.conn
    anna, mid = await _build(rv, w)
    auto = w.who == "trusted" and w.sending and w.business == "ok"
    card = await _draft_card(rv, w, anna, mid)
    if auto:
        # Автоответ доверенному ответит сам: его задача заведена, черновика сервиса нет.
        assert card is None
        assert await conn.fetchval("SELECT count(*) FROM reply_tasks") == 1
        assert await conn.fetchval("SELECT count(*) FROM outbox_drafts WHERE origin = 'suggest'") == 0
        return
    assert card is not None, "черновик ответа должен прийти карточкой"
    assert await conn.fetchval("SELECT count(*) FROM reply_tasks") == 0
    draft = await conn.fetchrow("SELECT * FROM outbox_drafts WHERE origin = 'suggest'")
    assert draft is not None and draft["status"] == "pending"
    data = [b.get("data") for row in card["buttons"] for b in row if b.get("data")]
    can_send = w.sending and w.business == "ok"
    assert any(d.startswith("sh:ob:s:") for d in data) == can_send
    can_put = not can_send and w.session
    assert any(d.startswith("sh:ob:p:") for d in data) == can_put
    # Бот и приложение — одни и те же кнопки сервиса (Д-46).
    collected = await approvals.collect(conn, rv.state.config)
    app_refs = [a["ref"] for item in collected["items"] if item["kind"] == "suggest" for a in item["actions"]]
    assert app_refs == [d for d in data if not d.startswith("sh:vr:")]
    # Мир к моменту нажатия.
    if w.window == "closed":
        await conn.execute("UPDATE messages SET sent_at = now() - interval '25 hours' WHERE chat_id = $1", anna)
    if w.stop:
        await conn.execute("UPDATE assistant_halt SET stopped = true")
    for d in data:
        if d.startswith(("sh:ob:s:", "sh:ob:p:")):
            await press(conn, d)
    placed = can_put and not w.stop
    assert (rv.tg.fields.get((rv.owner_acc, ANNA)) == "Да, всё в силе.") == placed
    if can_put and w.stop:
        assert rv.tg.put_calls == []                    # остановлен — к полю ввода не обращались
    from shturman.outbox import drafts
    outbox = rv.state.extras["outbox"]
    await drafts.sweep(outbox)
    await drafts.settle(outbox)
    sent_jobs = await conn.fetchval("SELECT count(*) FROM jobs WHERE kind = $1", bridge.BUSINESS_SEND)
    status = await conn.fetchval("SELECT status FROM outbox_drafts WHERE id = $1", draft["id"])
    pressable = can_send and w.window == "open" and not w.stop
    assert sent_jobs == (1 if pressable else 0)
    assert status == ("sending" if pressable else "placed" if placed else "pending")
    assert await _owner_sends(rv) == []
    # «Не годится» снимает черновик, ничего не отправляя.
    if not pressable and not placed:
        bad = [d for d in data if d.startswith("sh:rh:n:")]
        assert bad
        out = await press(conn, bad[0])
        assert "Ничего не отправлено" in out["answer"]
        assert await conn.fetchval("SELECT status FROM outbox_drafts WHERE id = $1", draft["id"]) == "rejected"
        assert await conn.fetchval("SELECT count(*) FROM jobs WHERE kind = $1", bridge.BUSINESS_SEND) == 0
    assert await _owner_sends(rv) == []
