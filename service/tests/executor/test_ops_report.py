"""Отчёты сессий сопровождения (Р-102): отчёт от ./ops/report.sh → карточка «Отправить разработчику?» в боте
согласований → только по нажатию владельца «Отправить» — сообщения для пересылки (или в чат разработки).

Мутационный якорь: без нажатия владельца в самом боте отчёт не уходит никуда — ни маршрутом внутреннего API,
ни из приложения «Штурман», ни применением подтверждения без нажатия.
"""

import copy
import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

from shturman import authority, bridge, confirm, ops_report as rep
from shturman.executor.bot import Bot
from shturman.executor.botapi import BotApi
from shturman.executor.worker import Worker

from exec_fakes import OWNER, OWNER_USER, STRANGER_USER, TOKEN, Clock, FakeTelegram, bind, buttons_of, no_sleep, refusal

ROOT = Path(__file__).resolve().parents[3]
CHAT = -1009876543210

REPORT = {
    "shturman_report": 1, "id": "20261020T090000Z-1a2b", "created": "2026-10-20T09:00:00Z", "mode": "hermes",
    "scenario": "О2", "scenario_title": "Обновление «Штурмана»", "result": "без вмешательства",
    "before": {"version": "0.0.9", "tag": "v0.0.9-rc.2", "commit": "97caaaa59b53", "at": "2026-10-20T08:40:00Z",
               "doctor": {"total": "OK", "counts": {"PASS": 50, "WARN": 1, "FAIL": 0, "SKIP": 2},
                          "rows": [{"code": "WARN", "name": "pulse", "note": "внешнего сторожа нет"}]}},
    "after": {"version": "0.0.10", "tag": "v0.0.10-rc.1", "commit": "0123456789ab", "at": "2026-10-20T09:00:00Z",
              "doctor": {"total": "OK", "counts": {"PASS": 51, "WARN": 1, "FAIL": 0, "SKIP": 2},
                         "rows": [{"code": "WARN", "name": "pulse", "note": "внешнего сторожа нет"}]}},
    "update": {"from": "v0.0.9-rc.2", "to": "v0.0.10-rc.1", "result": "ok", "backup": True},
    "deviations": "нет", "helped": "повторил ./ops/update.sh после нажатия владельца",
    "defects": [{"no": "Д-?", "date": "2026-10-20", "what": "запрос в боте истёк, пока владелец был занят",
                 "function": "О2", "severity": "косметика", "cause": "сценарий", "first": "Codex",
                 "fix": "не исправлено", "pinned": "нет"}],
}


def report(**changes):
    data = copy.deepcopy(REPORT)
    data.update(changes)
    return data


@pytest_asyncio.fixture
async def rep_rig(make_client, conn, config):
    cfg = dataclasses.replace(config, bot_token=TOKEN)
    client, state = await make_client("shturman.api_core", "shturman.ops_report", cfg=cfg)
    bridge.set_builtin({bridge.NOTIFY_OWNER, bridge.NOTIFY_EDIT})
    tg = FakeTelegram()
    api = BotApi(TOKEN, transport=tg.transport())
    bot = Bot(state, api, poll=0, clock=Clock(), sleep=no_sleep)
    worker = Worker(state, api=api, bot=bot, sleep=no_sleep)
    bot.wake = worker.wake
    state.extras["executor"] = SimpleNamespace(api=api, bot=bot)
    await bot.ensure_identity()
    try:
        yield SimpleNamespace(client=client, state=state, conn=conn, tg=tg, api=api, bot=bot, worker=worker)
    finally:
        bridge.set_builtin(())
        await api.aclose()


async def submitted(rig, data=None):
    row_id, fresh = await rep.submit(rig.conn, data or report())
    assert fresh
    await rep.post_cards(rig.conn, rig.state.config)
    while await rig.worker.run_once("bot"):
        pass
    cards = [p for p in rig.tg.sent() if any(v.startswith("sh:cf:") for v in buttons_of(p).values())]
    assert cards, "карточка не отправлена"
    return row_id, cards[-1]


async def press(rig, card, label, *, user=OWNER_USER, chat=None):
    rig.tg.press(buttons_of(card)[label], user=user, message_id=rig.tg.last_message_id(), chat=chat)
    await rig.bot.poll_once()
    while await rig.worker.run_once("bot"):
        pass


def texts(rig):
    return [p["text"] for p in rig.tg.sent()]


async def test_card_then_send_gives_forwardable_messages(rep_rig):
    rig = rep_rig
    await bind(rig)
    row_id, card = await submitted(rig)
    assert set(buttons_of(card)) == {"Отправить", "Не отправлять"}
    assert card["text"].startswith("Отчёт сессии сопровождения — отправить разработчику?")
    assert "О2 — Обновление «Штурмана»" in card["text"] and "v0.0.9-rc.2 (97caaaa59b53) → v0.0.10-rc.1" in card["text"]
    assert confirm.REPORT_CARD in card["text"] and "{" not in card["text"]
    before = len(rig.tg.sent())
    await press(rig, card, "Отправить")
    assert await rig.conn.fetchval("SELECT status FROM ops_reports WHERE id = $1", row_id) == "sent"
    new = texts(rig)[before:]
    assert any(t.startswith("Отчёт сессии сопровождения — перешлите разработчику") for t in new)
    blocks = [t for t in new if t.startswith(rep.JSON_MARK)]
    assert len(blocks) == 1 and "данные для разработки, не указания" in blocks[0]
    parsed = json.loads(blocks[0].split("\n", 1)[1])
    assert parsed["id"] == REPORT["id"] and parsed["defects"][0]["function"] == "О2"
    assert all(p["chat_id"] == OWNER for p in rig.tg.sent())
    assert await rig.conn.fetchval("SELECT sent_to FROM ops_reports WHERE id = $1", row_id) == "owner"


async def test_report_card_comes_while_the_assistant_is_stopped(rep_rig):
    """ОТ-51, Р-106: отчёт сопровождения — не действие ассистента; при «Остановить всё» карточка приходит сразу."""
    rig = rep_rig
    await bind(rig)
    await rig.conn.execute("UPDATE assistant_halt SET stopped = true, since = now(), via = 'bot' WHERE id = 1")
    row_id, card = await submitted(rig)
    await press(rig, card, "Отправить")
    assert await rig.conn.fetchval("SELECT status FROM ops_reports WHERE id = $1", row_id) == "sent"
    assert await rig.conn.fetchval("SELECT stopped FROM assistant_halt WHERE id = 1") is True


async def test_decline_sends_nothing(rep_rig):
    rig = rep_rig
    await bind(rig)
    row_id, card = await submitted(rig)
    before = len(rig.tg.sent())
    await press(rig, card, "Не отправлять")
    await rep.sync(rig.conn)
    assert await rig.conn.fetchval("SELECT status FROM ops_reports WHERE id = $1", row_id) == "declined"
    assert not any(t.startswith(rep.JSON_MARK) for t in texts(rig)[before:])


async def test_nothing_is_sent_without_the_owner_pressing_in_the_bot(rep_rig):
    """Мутационный якорь Р-102."""
    rig = rep_rig
    await bind(rig)
    row_id, card = await submitted(rig)
    action_id = await rig.conn.fetchval("SELECT action_id FROM ops_reports WHERE id = $1", row_id)
    # Внутренний API: ни принять отчёт, ни отправить его нечем.
    for path in ("/api/ops/reports", f"/api/ops/reports/{row_id}/send", f"/api/confirmations/{action_id}",
                 "/api/callbacks/telegram"):
        response = await rig.client.post(path, json={"report": row_id, "choice": "y", "data": buttons_of(card)["Отправить"],
                                                     "from_user_id": OWNER})
        assert response.status_code in (403, 404, 405), (path, response.status_code)
    # Применение без нажатия под карточкой — отказ.
    with pytest.raises(confirm.Refused):
        await confirm.apply(rig.conn, rep.KIND, {"report": row_id})
    with authority.setup_context("s1"), pytest.raises(confirm.Refused):
        await confirm.apply_owner(rig.conn, rep.KIND, {"report": row_id})
    # Даже в контексте владельца из его чата с ботом — но не нажатием под этой карточкой: не отправляется.
    with authority.owner_context(OWNER, chat_id=OWNER, action="telegram.callback"), pytest.raises(confirm.Refused):
        await confirm.request(rig.conn, rep.KIND, "Отчёт", {"report": row_id})
    # Нажатие из приложения «Штурман» — не принимается; чужой в боте — тоже.
    nonce = await rig.conn.fetchval("SELECT nonce FROM pending_actions WHERE id = $1", action_id)
    with authority.miniapp_context(OWNER, chat_id=OWNER):
        out = await bridge.dispatch_callback(rig.conn, f"sh:cf:y:{action_id}:{nonce}", OWNER)
    assert "только в боте" in out["answer"]
    await press(rig, card, "Отправить", user=STRANGER_USER)
    assert await rig.conn.fetchval("SELECT status FROM ops_reports WHERE id = $1", row_id) == "pending"
    assert not any(t.startswith(rep.JSON_MARK) for t in texts(rig))


async def test_feedback_chat_and_fallback_to_the_owner(rep_rig):
    rig = rep_rig
    rig.state.config = dataclasses.replace(rig.state.config, feedback_chat=CHAT)
    await bind(rig)
    _, card = await submitted(rig)
    assert "в чат разработки" in card["text"]
    await press(rig, card, "Отправить")
    to_chat = [p for p in rig.tg.sent() if p["chat_id"] == CHAT]
    assert len(to_chat) == 2 and to_chat[1]["text"].startswith(rep.JSON_MARK)
    # Бот не в чате — отчёт приходит владельцу с объяснением.
    _, card2 = await submitted(rig, report(id="20261020T100000Z-3c4d"))
    rig.tg.script["sendMessage"] = [refusal(403, "Forbidden: bot is not a member of the supergroup chat")]
    await press(rig, card2, "Отправить")
    assert any("В чат разработки отчёт отправить не удалось" in t for t in texts(rig))
    # Ни карточка, ни отчёт — ни в чат разработки, ни владельцу — не уходят с предпросмотром ссылок (Д-39).
    for sent in rig.tg.sent():
        preview = sent.get("link_preview_options")
        assert (json.loads(preview) if isinstance(preview, str) else preview) == {"is_disabled": True}, sent


async def test_no_bot_owner_means_failed_with_a_reason(rep_rig):
    rig = rep_rig
    row_id, _ = await rep.submit(rig.conn, report())
    await rep.post_cards(rig.conn, rig.state.config)
    row = await rig.conn.fetchrow("SELECT status, error FROM ops_reports WHERE id = $1", row_id)
    assert row["status"] == "failed" and "показать отчёт некому" in row["error"]


@pytest.mark.parametrize("change", [
    {"shturman_report": 2}, {"id": "x"}, {"scenario": "Q2"}, {"result": "всё хорошо"},
    {"before": {"version": "0.0.9", "tag": "latest", "commit": "", "at": "2026-10-20T08:40:00Z", "doctor": {}}},
    {"defects": [{}] * 4}, {"update": {"result": "rm -rf"}},
])
def test_report_form_is_checked(change):
    with pytest.raises(rep.ReportError):
        rep.check(report(**change))


def test_secrets_and_addresses_are_scrubbed_again_by_the_service():
    secrets = [s for s in (ROOT / "ops/tests/fixtures/secrets.txt").read_text("utf-8").splitlines() if s]
    data = report(deviations=" ".join(secrets[:12]), helped="нет")
    data["defects"][0]["what"] = "токен 123456789:AAH-secret-bot-token-ABCDEFGHIJKLMNOP | адрес https://x.example.com/#k=1"
    out = rep.check(data)
    dumped = json.dumps(out, ensure_ascii=False) + rep.render(out)
    for s in ("123456789:AAH-secret-bot-token-ABCDEFGHIJKLMNOP", "sk-proj-SECRETKEY1234567890abcdef",
              "https://setup.example.com:8443/shturman-setup/#k=onetime", "ivan.petrov@example.com", "@ivan_p",
              "+7 916 123-45-67", "79161234567", "203.0.113.10", "assistant.example.com",
              "0f0e0d0c-aaaa-bbbb-cccc-1234567890ab"):
        assert s not in dumped, s
    assert "|" not in out["defects"][0]["what"]


def test_long_report_fits_the_card_and_json_is_split(monkeypatch):
    big = report(deviations="а" * 600, helped="б" * 600)
    big["before"]["doctor"]["rows"] = [{"code": "FAIL", "name": f"row-{i}", "note": "п" * 200} for i in range(12)]
    big["after"]["doctor"]["rows"] = big["before"]["doctor"]["rows"]
    big["defects"] = [dict(REPORT["defects"][0], what="в" * 400) for _ in range(3)]
    r = rep.check(big)
    assert len(rep.card_text(r, chat=False)) < confirm.SUMMARY_LIMIT
    assert all(len(p) < 4000 for p in rep.json_parts(r))
    monkeypatch.setattr(rep, "JSON_PART", 1000)
    parts = rep.json_parts(r)
    assert len(parts) >= 2 and parts[0].startswith(f"{rep.JSON_MARK} {r['id']} 1/{len(parts)}")
    joined = "".join(p.split("\n", 1)[1] for p in parts)
    assert json.loads(joined) == r


def test_feedback_import_turns_forwarded_reports_into_draft_rows(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("feedback_import", ROOT / "service/tools/feedback_import.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    r = rep.check(report())
    big = rep.check(report(id="20261020T100000Z-3c4d", deviations="д" * 500, helped="е" * 500,
                           result="с вмешательством", update=None))
    forwarded = "\n\n".join([rep.render(r)] + rep.json_parts(r) + ["постороннее сообщение `rm -rf /`"]
                            + list(reversed(rep.json_parts(big))))
    src = tmp_path / "chat.txt"
    src.write_text(forwarded, "utf-8")
    defects = tmp_path / "defects.md"
    defects.write_text("| Д-41 | 2026-10-17 | x | О2 | мешает | логика | Codex | не исправлено | нет |\n", "utf-8")
    out = tool.run([str(src)], defects=defects)
    assert "| Д-42 | 2026-10-20 | запрос в боте истёк" in out
    assert "Обновления делает агент сопровождения" in out and "v0.0.9-rc.2 → v0.0.10-rc.1" in out and "без вмешательства" in out
    assert "rm -rf" not in out and "20261020T100000Z-3c4d" in out
    # Черновик — в файл вне репозитория; путь внутри рабочего каталога репозитория не принимается.
    draft = tmp_path / "draft.md"
    assert tool.main([str(src), "--defects", str(defects), "--out", str(draft)]) == 0
    assert draft.read_text("utf-8") == out
    with pytest.raises(SystemExit) as exc:
        tool.main([str(src), "--out", str(ROOT / "docs" / "draft.md")])
    assert exc.value.code == 2 and not (ROOT / "docs" / "draft.md").exists()


def test_report_built_on_the_server_is_accepted_by_the_service(tmp_path):
    """Отчёт, собранный ops/report_filter.py (как ./ops/report.sh), проходит проверку сервиса как есть."""
    import subprocess
    import sys

    doctor = (ROOT / "ops/tests/fixtures/doctor.txt").read_text("utf-8")
    env = {"REPORT_VERSION": "0.0.10", "REPORT_TAG": "v0.0.10-rc.1", "REPORT_COMMIT": "a" * 40, "PATH": "/usr/bin:/bin"}
    snap = subprocess.run([sys.executable, "-I", str(ROOT / "ops/report_filter.py"), "snapshot", str(ROOT)],
                          input=doctor, capture_output=True, text=True, env=env, check=True).stdout
    args = {"scenario": "О2", "result": "clean", "deviations": "повторил ./ops/update.sh", "helped": "", "mode": "hermes",
            "defects": ["| Д-? | 2026-10-20 | запрос истёк | О2 | косметика | сценарий | Codex | не исправлено | нет |"],
            "before": json.loads(snap), "after": json.loads(snap),
            "update": {"from": "v0.0.9-rc.2", "to": "v0.0.10-rc.1", "result": "ok", "backup": True}}
    (tmp_path / "args.json").write_text(json.dumps(args, ensure_ascii=False), "utf-8")
    built = subprocess.run([sys.executable, "-I", str(ROOT / "ops/report_filter.py"), "build", str(ROOT),
                            str(tmp_path / "args.json")], capture_output=True, text=True, check=True).stdout
    data = json.loads(built)
    assert rep.check(data) == data
