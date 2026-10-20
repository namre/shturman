"""Отчёты сессий сопровождения — разработке, по кнопке владельца (Р-102).

Зачем. Владелец сопровождает экземпляр с Codex. Что пошло не так и где Codex отступил от runbook,
раньше доходило до разработки только если владелец сам пересылал строки дефектов из чата с Codex. Теперь
каждая сессия сопровождения кончается `./ops/report.sh`: отчёт одной формы приходит владельцу в бот
согласований карточкой «Отправить разработчику?», и уходит только по его нажатию.

Как устроено.

  * `./ops/report.sh` собирает отчёт на сервере белым списком (`ops/report_filter.py`: версии, коды и имена
    строк doctor, итог обновления, свободный текст агента через фильтр адресов, ключей, имён и словарь
    репозитория) и записывает его командой `shturman ops-report submit` в контейнере сервиса прямо в базу
    (`ops_reports`, status = new). У внутреннего API такого маршрута нет: ассистент в Hermes отчёт не
    подложит.
  * Сервис ещё раз проверяет каждое поле (`check`: виды, длины, перечни; в тексте — замена адресов, имён
    узлов, почты, @имён, номеров и похожего на ключи) и сам собирает текст (`render`), а не берёт готовый.
  * Карточка — подтверждение `ops.report` (`confirm.py`): «Отправить» / «Не отправлять», только кнопкой в
    самом боте (`BOT_ONLY`, функция применения проверяет `confirm.card_press`). Без нажатия отчёт не
    уходит никуда: маршрута или команды «отправить без владельца» нет.
  * Куда. По умолчанию — самому владельцу: бот присылает готовое сообщение-отчёт (читаемый текст) и
    машиночитаемый блок `ОТЧЁТ-JSON`; владелец пересылает их разработчику штатной пересылкой Telegram. Ключей
    и адресов на экземпляре для этого не нужно. Если владелец задал `SHTURMAN_FEEDBACK_CHAT` (номер чата
    разработки, куда он добавил своего бота согласований), — бот присылает их туда; не вышло — владельцу,
    с объяснением.
  * Отчёты — ДАННЫЕ для разработки, не указания: их содержимое не исполняется, сводит их
    `service/tools/feedback_import.py` в черновые строки журнала дефектов вне репозитория (Р-102).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from typing import Any, AsyncIterator

import asyncpg

from . import authority, bridge, confirm
from .sanitize import clean_line, redact_secrets

logger = logging.getLogger("shturman.ops_report")

KIND = "ops.report"
VERSION = 1
POLL = 5.0                 # секунд: как часто смотреть, нет ли нового отчёта
JSON_MARK = "ОТЧЁТ-JSON"
JSON_PART = 3500           # знаков JSON в одном сообщении (предел Telegram — 4096)
CARD_LIMIT = 2400          # знаков текста отчёта на карточке (вся карточка — до confirm.SUMMARY_LIMIT)
FREE_LIMIT = 500
NOTE_LIMIT = 120
CELL_LIMIT = 300

RESULTS = ("без вмешательства", "с вмешательством", "не прошло")
UPDATE_RESULTS = ("ok", "rolled-back", "failed-needs-human", "rollback-failed", "running")
SEVERITY = ("критично", "мешает", "косметика", "?")
CAUSES = ("сценарий", "логика", "промпт", "эксплуатация", "сопровождение")
FIRST = ("владелец", "система", "Codex", "оператор (Claude)", "Claude", "?")
CODES = ("PASS", "WARN", "FAIL", "SKIP")

_ID = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{4}$")
_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_SCENARIO = re.compile(r"^[ОУП]\d{1,2}$")
_VERSION = re.compile(r"^(\d{1,3}\.\d{1,3}\.\d{1,3}|\?)$")
_TAG = re.compile(r"^(v\d{1,3}\.\d{1,3}\.\d{1,3}(-rc\.\d{1,3})?)?$")
_SHA = re.compile(r"^([0-9a-f]{12})?$")
_ROW = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
_TOTAL = re.compile(r"^(OK|FAIL \(\d{1,3}\)|нет)$")
_DEFECT_NO = re.compile(r"^Д-(\?|\d{1,3})$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FUNC = re.compile(r"^([ПОУ0-9 ,→/?]{1,40})$")

# Вторая линия после фильтра на сервере: то, что похоже на адрес, ключ, номер, — заменяется.
_URL = re.compile(r"\b(?:https?|wss?|ftp)://\S+", re.I)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_AT = re.compile(r"(?<![\w/<])@[A-Za-z][A-Za-z0-9_]{2,}")
_BOT_TOKEN = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b")
_LONG = re.compile(r"(?<![A-Za-z0-9_+=-])(?=[A-Za-z_+=-]*\d)(?=[0-9_+=-]*[A-Za-z])[A-Za-z0-9_+=-]{24,}")
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_HOST = re.compile(r"\b(?:[A-Za-z0-9-]+\.)+(?:com|ru|net|org|app|io|dev|me|xyz|info|pro|tech|cloud|online|site|su|"
                   r"biz|co|uk|de|eu|space|store|host|cc|tv|ai)\b", re.I)
_PHONE = re.compile(r"(?<!\w)\+\d[\d ()-]{8,}\d")
_DIGITS = re.compile(r"\b\d{7,}\b")


class ReportError(ValueError):
    pass


def scrub(text: Any, limit: int) -> str:
    """Одна строка, без управляющих знаков, без адресов, почты, @имён, номеров и похожего на ключи."""
    if not isinstance(text, str):
        raise ReportError("ожидается строка")
    out = clean_line(text, limit * 2)
    out = _URL.sub("<адрес>", out)
    out = _EMAIL.sub("<почта>", out)
    out = _BOT_TOKEN.sub("<ключ>", out)
    out = redact_secrets(out)
    out = _LONG.sub("<…>", out)
    out = _IPV4.sub("<ip>", out)
    out = _HOST.sub("<имя узла>", out)
    out = _AT.sub("@<имя>", out)
    out = _PHONE.sub("<номер>", out)
    out = _DIGITS.sub("<число>", out)
    out = out.replace("|", "/")
    return out if len(out) <= limit else out[:limit - 1].rstrip() + "…"


def _match(value: Any, pattern: re.Pattern, name: str) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise ReportError(f"{name}: не та форма")
    return value


def _snapshot(value: Any, name: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ReportError(f"{name}: нужен объект")
    doctor = value.get("doctor") if isinstance(value.get("doctor"), dict) else {}
    counts = doctor.get("counts") if isinstance(doctor.get("counts"), dict) else {}
    rows = doctor.get("rows") if isinstance(doctor.get("rows"), list) else []
    if len(rows) > 12:
        raise ReportError(f"{name}.doctor.rows: не больше 12")
    clean_rows = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict) or row.get("code") not in ("WARN", "FAIL"):
            raise ReportError(f"{name}.doctor.rows[{i}]: WARN или FAIL")
        clean_rows.append({"code": row["code"], "name": _match(row.get("name"), _ROW, f"{name}.doctor.rows[{i}].name"),
                           "note": scrub(row.get("note", ""), NOTE_LIMIT)})
    out_counts = {}
    for code in CODES:
        n = counts.get(code, 0)
        if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= 500:
            raise ReportError(f"{name}.doctor.counts.{code}: число")
        out_counts[code] = n
    return {"version": _match(value.get("version", "?"), _VERSION, f"{name}.version"),
            "tag": _match(value.get("tag", ""), _TAG, f"{name}.tag"),
            "commit": _match(value.get("commit", ""), _SHA, f"{name}.commit"),
            "at": _match(value.get("at"), _STAMP, f"{name}.at"),
            "doctor": {"total": _match(doctor.get("total", "нет"), _TOTAL, f"{name}.doctor.total"),
                       "counts": out_counts, "rows": clean_rows}}


def check(data: Any) -> dict[str, Any]:
    """Строгая проверка отчёта. Лишние поля отбрасываются, неверные — отказ. Возвращает чистый отчёт."""
    if not isinstance(data, dict) or data.get("shturman_report") != VERSION:
        raise ReportError(f"нужен объект с shturman_report = {VERSION}")
    if data.get("result") not in RESULTS:
        raise ReportError("result: " + ", ".join(RESULTS))
    mode = data.get("mode") if data.get("mode") in ("hermes", "standalone") else "?"
    update = data.get("update")
    if update is not None:
        if not isinstance(update, dict) or update.get("result") not in UPDATE_RESULTS:
            raise ReportError("update: объект с result из " + ", ".join(UPDATE_RESULTS))
        update = {"from": scrub(update.get("from", "?"), 60) or "?", "to": scrub(update.get("to", "?"), 60) or "?",
                  "result": update["result"], "backup": bool(update.get("backup"))}
    defects = data.get("defects") or []
    if not isinstance(defects, list) or len(defects) > 3:
        raise ReportError("defects: список, не больше 3")
    clean_defects = []
    for i, d in enumerate(defects):
        if not isinstance(d, dict):
            raise ReportError(f"defects[{i}]: объект")
        cause = [c.strip() for c in str(d.get("cause", "")).split(",") if c.strip() in CAUSES]
        func = d.get("function", "?")
        clean_defects.append({
            "no": _match(d.get("no", "Д-?"), _DEFECT_NO, f"defects[{i}].no"),
            "date": _match(d.get("date"), _DATE, f"defects[{i}].date"),
            "what": scrub(d.get("what", ""), CELL_LIMIT),
            "function": func if isinstance(func, str) and _FUNC.match(func) else "?",
            "severity": d.get("severity") if d.get("severity") in SEVERITY else "?",
            "cause": ", ".join(cause) or "?",
            "first": d.get("first") if d.get("first") in FIRST else "?",
            "fix": scrub(d.get("fix", ""), 120) or "не исправлено",
            "pinned": scrub(d.get("pinned", ""), 120) or "нет"})
    return {"shturman_report": VERSION,
            "id": _match(data.get("id"), _ID, "id"),
            "created": _match(data.get("created"), _STAMP, "created"),
            "mode": mode,
            "scenario": _match(data.get("scenario"), _SCENARIO, "scenario"),
            "scenario_title": scrub(data.get("scenario_title", ""), 80),
            "result": data["result"],
            "before": _snapshot(data.get("before"), "before"),
            "after": _snapshot(data.get("after"), "after"),
            "update": update,
            "deviations": scrub(data.get("deviations", ""), FREE_LIMIT),
            "helped": scrub(data.get("helped", ""), FREE_LIMIT),
            "defects": clean_defects}


# --- текст ------------------------------------------------------------------------------------------

UPDATE_WORDS = {"ok": "OK", "rolled-back": "ОТКАТ", "failed-needs-human": "FAIL — нужен человек",
                "rollback-failed": "FAIL — откат не удался", "running": "не закончено"}


def _ver(s: dict[str, Any] | None) -> str:
    if not s:
        return "?"
    return (s.get("tag") or s.get("version") or "?") + (f" ({s['commit']})" if s.get("commit") else "")


def _snap(name: str, s: dict[str, Any] | None, *, notes: bool) -> list[str]:
    if not s:
        return [f"{name}: не снималась"]
    d, c = s["doctor"], s["doctor"]["counts"]
    out = [f"{name}: ИТОГ {d['total']} — PASS {c['PASS']}, WARN {c['WARN']}, FAIL {c['FAIL']}"]
    for r in d["rows"]:
        out.append(f"  {r['code']} {r['name']}" + (f": {r['note']}" if notes and r["note"] else ""))
    return out


def render(r: dict[str, Any], *, limit: int = CARD_LIMIT) -> str:
    """Читаемый текст отчёта. Не помещается — сначала без пояснений строк doctor, затем короче текст."""
    for notes, free in ((True, FREE_LIMIT), (False, FREE_LIMIT), (False, 200), (False, 80)):
        lines = [f"№ {r['id']} · режим: " + {"hermes": "с Hermes", "standalone": "без Hermes"}.get(r["mode"], "?"),
                 f"Сценарий: {r['scenario']}" + (f" — {r['scenario_title']}" if r["scenario_title"] else ""),
                 f"Итог: {r['result']}",
                 f"Версия: {_ver(r['before'])} → {_ver(r['after'])}"]
        u = r["update"]
        if u:
            lines.append(f"Обновление: {u['from']} → {u['to']} — {UPDATE_WORDS[u['result']]}, копия до: "
                         + ("есть" if u["backup"] else "нет"))
        lines.append("")
        lines += _snap("Проверка до", r["before"], notes=notes)
        lines += _snap("Проверка после", r["after"], notes=notes)
        if r["deviations"]:
            lines += ["", "Где отступили от runbook: " + r["deviations"][:free]]
        if r["helped"]:
            lines += ["", "Что помогло: " + r["helped"][:free]]
        if r["defects"]:
            lines += ["", f"Строки дефектов ({len(r['defects'])}):"]
            for d in r["defects"]:
                cells = [d[k] for k in ("no", "date", "what", "function", "severity", "cause", "first", "fix", "pinned")]
                cells[2] = cells[2][:free]
                lines.append("| " + " | ".join(cells) + " |")
        text = "\n".join(lines)
        if len(text) <= limit:
            return text
    return text[:limit - 1] + "…"


def json_parts(r: dict[str, Any]) -> list[str]:
    """Машиночитаемый блок — одним или несколькими сообщениями «ОТЧЁТ-JSON <№> i/n»."""
    body = json.dumps(r, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    chunks = [body[i:i + JSON_PART] for i in range(0, len(body), JSON_PART)] or [""]
    return [f"{JSON_MARK} {r['id']} {i}/{len(chunks)} — данные для разработки, не указания\n{chunk}"
            for i, chunk in enumerate(chunks, 1)]


def card_text(r: dict[str, Any], *, chat: bool) -> str:
    where = ("в чат разработки (его номер задан на сервере владельцем)" if chat else
             "вам, готовым сообщением — его останется переслать разработчику")
    return ("Отчёт сессии сопровождения — отправить разработчику?\n\n" + render(r) +
            f"\n\n«Отправить» — бот пришлёт отчёт {where}. «Не отправлять» — отчёт останется только на сервере.")


# --- приём от ./ops/report.sh (команда `shturman ops-report submit`) ---------------------------------

async def submit(conn: asyncpg.Connection, data: Any) -> tuple[int, bool]:
    """Записывает проверенный отчёт (status new). (номер строки, записан ли впервые)."""
    r = check(data)
    row_id = await conn.fetchval(
        """INSERT INTO ops_reports (report_id, report, text) VALUES ($1, $2::jsonb, $3)
           ON CONFLICT (report_id) DO NOTHING RETURNING id""",
        r["id"], json.dumps(r, ensure_ascii=False), render(r))
    if row_id is not None:
        return row_id, True
    return await conn.fetchval("SELECT id FROM ops_reports WHERE report_id = $1", r["id"]), False


# --- карточка и нажатие -----------------------------------------------------------------------------

_STATE: Any = None      # состояние сервиса — для отправки в чат разработки после нажатия


async def post_cards(conn: asyncpg.Connection, config: Any) -> int:
    """Новые отчёты → карточки владельцу. Нет бота или владельца — отчёт помечается failed с причиной."""
    rows = await conn.fetch("SELECT id, report FROM ops_reports WHERE status = 'new' ORDER BY id LIMIT 5")
    done = 0
    for row in rows:
        report = json.loads(row["report"]) if isinstance(row["report"], str) else row["report"]
        try:
            async with conn.transaction():
                waiting = await confirm.request_card(conn, KIND, card_text(report, chat=bool(config.feedback_chat)),
                                                     {"report": row["id"]})
                await conn.execute("UPDATE ops_reports SET status = 'pending', action_id = $2 WHERE id = $1",
                                   row["id"], waiting["action_id"])
            done += 1
        except confirm.NoOwner:
            await conn.execute(
                "UPDATE ops_reports SET status = 'failed', error = $2 WHERE id = $1 AND status = 'new'", row["id"],
                "бот согласований не настроен или владелец к нему не привязан — показать отчёт некому")
        except confirm.TooManyPending:
            break                # владелец разберёт карточки — тогда и этот отчёт
    return done


async def sync(conn: asyncpg.Connection) -> None:
    """Исход карточки → исход отчёта: отклонено, срок вышел, не доставлено."""
    await conn.execute(
        """UPDATE ops_reports r SET status = CASE a.status WHEN 'rejected' THEN 'declined'
                                                          WHEN 'expired' THEN 'expired' ELSE 'failed' END,
                  decided_at = COALESCE(a.decided_at, now())
           FROM pending_actions a
           WHERE r.action_id = a.id AND r.status = 'pending' AND a.status IN ('rejected', 'expired', 'failed')""")


async def _deliver(report: dict[str, Any], row_id: int) -> None:
    """После нажатия: в чат разработки (если задан) — иначе или при неудаче владельцу для пересылки."""
    state = _STATE
    if state is None:
        logger.warning("отчёт сопровождения: сервис не запущен — отправлять нечем")
        return
    config = getattr(state, "config", None)
    chat = int(getattr(config, "feedback_chat", 0) or 0)
    api = getattr(state.extras.get("executor"), "api", None)
    parts = [render(report, limit=3900)] + json_parts(report)
    where, error = "owner", None
    if chat and api is not None:
        try:
            for i, text in enumerate(parts):
                await api.send_message(chat, ("Отчёт сессии сопровождения «Штурмана»\n\n" if i == 0 else "") + text,
                                       no_preview=True)
            where = "chat"
        except Exception as exc:  # noqa: BLE001 — бот не в чате, чат удалён, сеть: тогда — владельцу
            error = type(exc).__name__
            logger.warning("отчёт сопровождения: в чат разработки не отправлен (%s)", error)
    elif chat:
        error = "бот согласований не запущен"
    async with state.pool.acquire() as conn:
        if where == "owner":
            intro = ("Отчёт сессии сопровождения — перешлите разработчику это сообщение и следующее за ним "
                     "(«ОТЧЁТ-JSON»): выделите оба и нажмите «Переслать».")
            if error:
                intro = (f"В чат разработки отчёт отправить не удалось ({error}) — перешлите его сами: это "
                         "сообщение и следующее за ним («ОТЧЁТ-JSON»).")
            await bridge.notify_owner(conn, intro + "\n\n" + parts[0], silent=True)
            for text in parts[1:]:
                await bridge.notify_owner(conn, text, silent=True)
        await conn.execute("UPDATE ops_reports SET sent_to = $2, error = $3 WHERE id = $1", row_id, where, error)


@confirm.applier(KIND)
async def _send(conn: asyncpg.Connection, payload: dict[str, Any]) -> confirm.Done:
    """«Отправить» под карточкой в самом боте — и ничто другое."""
    if not confirm.card_press():
        raise confirm.Refused("Отчёт отправляет только нажатие владельца в боте согласований.", 403,
                              "owner_press_required")
    principal = authority.requires_owner()
    if principal.source != "telegram" or principal.action == "telegram.miniapp":
        raise confirm.Refused("Отправить отчёт можно только кнопкой в самом боте согласований.", 403, "bot_only")
    row = await conn.fetchrow(
        """UPDATE ops_reports SET status = 'sent', decided_at = now()
           WHERE id = $1 AND status = 'pending' RETURNING id, report""", payload.get("report"))
    if row is None:
        raise confirm.Refused("Этот отчёт уже отправлен или снят.", 409, "report_gone")
    report = json.loads(row["report"]) if isinstance(row["report"], str) else row["report"]

    async def after() -> None:
        await _deliver(report, row["id"])

    chat = bool(getattr(getattr(_STATE, "config", None), "feedback_chat", 0))
    return confirm.Done(note="Отчёт отправлен в чат разработки." if chat else
                        "Отчёт — следующими сообщениями: перешлите их разработчику.", after=after)


# --- команда `shturman ops-report` ------------------------------------------------------------------

def cli(args: Any) -> None:
    import sys
    import time

    from . import db
    from .config import Config, ConfigError

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
        check(data)
    except (ValueError, ReportError) as exc:
        print(f"Отчёт не принят: {exc}", file=sys.stderr)
        sys.exit(2)

    async def go() -> int:
        conn = await db.connect(config.dsn)
        try:
            row_id, fresh = await submit(conn, data)
            if not fresh:
                print("Этот отчёт уже передан сервису.")
            deadline = time.monotonic() + max(0, args.wait)
            while True:
                st = await conn.fetchrow("SELECT status, error FROM ops_reports WHERE id = $1", row_id)
                if st["status"] == "pending":
                    print("Карточка отчёта отправлена владельцу в бот согласований: «Отправить» / «Не отправлять». "
                          "Отчёт уйдёт разработчику только после его нажатия.")
                    return 0
                if st["status"] in ("sent", "declined", "expired"):
                    print({"sent": "Отчёт уже отправлен владельцем.", "declined": "Владелец решил отчёт не отправлять.",
                           "expired": "Срок карточки вышел, отчёт не отправлен."}[st["status"]])
                    return 0
                if st["status"] == "failed":
                    print(f"Карточку показать не удалось: {st['error'] or 'причина неизвестна'}. Отчёт сохранён на "
                          "сервере (local/report/); текст выше можно показать владельцу.", file=sys.stderr)
                    return 3
                if time.monotonic() >= deadline:
                    print("Отчёт принят сервисом; карточка владельцу появится, когда сервис её поставит (обычно "
                          "секунды; если в боте много неразобранных карточек — после них).")
                    return 0
                await asyncio.sleep(1.0)
        finally:
            await conn.close()

    try:
        sys.exit(asyncio.run(go()))
    except OSError as exc:
        print(f"База сервиса недоступна ({type(exc).__name__}) — отчёт не передан.", file=sys.stderr)
        sys.exit(1)


def add_parser(sub: Any) -> None:
    p = sub.add_parser("ops-report", help="отчёт сессии сопровождения — владельцу в бот, разработчику по его "
                                          "нажатию (Р-102); JSON — на stdin")
    p.add_argument("what", choices=["submit"])
    p.add_argument("--wait", type=int, default=30, help="сколько секунд ждать, пока сервис поставит карточку")


# --- жизненный цикл ---------------------------------------------------------------------------------

@contextlib.asynccontextmanager
async def lifespan(state: Any) -> AsyncIterator[None]:
    global _STATE
    _STATE = state

    async def loop() -> None:
        while True:
            try:
                async with state.pool.acquire() as conn:
                    await post_cards(conn, state.config)
                    await sync(conn)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("отчёты сопровождения: сбой (%s)", type(exc).__name__)
            await asyncio.sleep(POLL)

    task = state.spawn(loop(), name="ops-reports")
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if _STATE is state:
            _STATE = None
