"""Приложение «Штурман»: блок «Сейчас» и экран «Договорённости» (пакет 4а ревизии удобства, Р-94).

  GET  app/now                              «Сейчас»: аккаунты (на связи / пауза / нет связи), время
                                            последнего сообщения в архиве, сбои модели за сутки, отправка,
                                            разбор входящих, остановлено ли, договорённости на сегодня и
                                            просроченные (числа); плашка новой версии и итог последнего
                                            обновления (`update`, Р-101: теги и тексты проверенного списка
                                            выпусков). Только состояния, числа и время — ни текста переписки,
                                            ни имён собеседников
  GET  app/commitments                      «Я обещал» и «Мне обещали» по группам срока (`processing.agenda`)
                                            и сколько ждут подтверждения «Верно / Не так» в «Согласованиях»
  POST app/commitments/{id}/done            «Выполнено»: {v}
  POST app/commitments/{id}/move            «Перенести»: {v, to: tomorrow | week | date, date?: ГГГГ-ММ-ДД}
  POST app/commitments/{id}/wrong           «Не так» — записано неверно: {v, reason?} (причина — Р-96)
  POST app/commitments/{id}/reopen          «Вернуть» сразу после «Выполнено» или «Не так» из приложения
  POST app/commitments/{id}/hide            «Скрыть» в списке «В команде» (Р-96): {v}

`v` — начало отпечатка договорённости, которую владелец видел (`agenda.view`): если она с тех пор
изменилась (перенесли в боте, на странице, ассистент предложил и владелец принял), действие не
выполняется — «Договорённость изменилась, список обновлён».

Кто решает. Это решения владельца о его собственных данных, а не расширение того, что читает или
делает ассистент, — поэтому из приложения они применяются сразу, в контексте владельца из Telegram
(`authority.miniapp_context`), теми же функциями, что меняют договорённости везде (`commitments.close`,
`reschedule_to`, `mark_wrong`, `reopen`). Журнал договорённости (`commitment_events`) — «владелец»,
с пометкой `via: app`; журнал действий страницы — `app.commitment_*` (номер договорённости и новая
дата, без формулировок и имён), в списке важных. Сообщения в бот нет — как у сфер (Р-82): цена та же,
что у экрана «Чаты» (Р-80), но вред меньше — держатель токена бота согласований может отметить или
перенести договорённости владельца, и это видно в журнале страницы. Решение — `docs/decisions.md`, Р-94.

Внутренний API и дашборд Hermes этих маршрутов не получают: они под префиксом страницы, только с
сессией приложения (`miniapp.endpoint`). Ассистент меняет договорённости, как и раньше, только через
подтверждение владельца (`processing/service.py`, `COMMITMENT_DECIDE`).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from .. import authority, halt
from ..api_core import BadRequest
from ..app import AppState, state_of
from ..processing import agenda, commitments, dates
from . import approvals, audit
from . import service as page

APP_NOW = page.API + "/app/now"
APP_COMMITMENTS = page.API + "/app/commitments"
MOVE_MAX_DAYS = 3 * 366       # перенести дальше трёх лет из приложения нельзя — это почти наверняка опечатка
REOPEN_WINDOW = 15 * 60       # секунд: «Вернуть» — только сразу после действия из приложения

CHANGED = "Договорённость изменилась — список обновлён. Посмотрите и решите ещё раз."
GONE = "Этой договорённости уже нет в списке — список обновлён."
STATE_CLASS = {"running": "ok", "starting": "wait", "paused": "paused"}


def _today(state: AppState) -> date:
    return approvals.today_of(state.config)


# --- «Сейчас» ------------------------------------------------------------------------------------

async def _accounts(state: AppState, conn: Any) -> list[dict[str, Any]]:
    from ..executor import owner_views
    from ..sanitize import clean_line
    from ..tg.manager import TgManager

    out: list[dict[str, Any]] = []
    manager = state.extras.get("tg")
    if isinstance(manager, TgManager) and manager.configured:
        for account in await manager.list_accounts():
            out.append({"role": "owner" if account["role"] == "owner" else "assistant",
                        "label": clean_line(account.get("label"), 40) or None,
                        "state": STATE_CLASS.get(account["status"], "off"),
                        "words": owner_views.ACCOUNT_STATE.get(account["status"], "нет связи")})
    business = await conn.fetchrow(
        "SELECT count(*) FILTER (WHERE enabled) AS on, count(*) AS total FROM business_connections")
    if business["total"]:
        out.append({"role": "business", "label": None, "state": "ok" if business["on"] else "off",
                    "words": "подключён" if business["on"] else "отключён"})
    return out


async def now_view(state: AppState) -> dict[str, Any]:
    """Только состояния, числа и время. Что ждёт решения — в ответе `app/approvals` (разделы и
    счётчики), чтобы не собирать список дважды."""
    from .. import retry
    from ..triage import settings as triage_settings
    from .miniapp import _sending

    today = _today(state)
    async with state.ro_pool.acquire() as conn:
        accounts = await _accounts(state, conn)
        last = await conn.fetchval("SELECT max(first_seen_at) FROM messages")
        since = datetime.now(timezone.utc) - timedelta(days=1)
        failed = await retry.candidates(conn, media_days=state.config.media_days, since=since)
        triage = await triage_settings.load(conn)
        stopped = halt.public(await halt.current(conn))
        counts = await agenda.counts(conn, today=today)
    return {
        "accounts": accounts,
        "last_message_at": last.isoformat() if last else None,
        "model_failed_24h": {"media": int(failed["media"]), "requests": int(failed["requests"])},
        "sending": _sending(state.config),
        "triage": bool(triage.get("enabled")),
        "halt": stopped,
        "commitments": counts,
        "today": today.isoformat(),
        # Плашка обновления (Р-101): что доступно, как обновить, итог последнего обновления.
        "update": await _update_view(state),
    }


async def _update_view(state: AppState) -> dict[str, Any] | None:
    from .. import updates
    try:
        return await updates.app_view(state)
    except Exception:  # noqa: BLE001 — «Сейчас» показывается и без плашки (например, база до миграции)
        return None


async def app_now(request: Request) -> JSONResponse:
    return JSONResponse(await now_view(state_of(request)))


# --- «Договорённости» -----------------------------------------------------------------------------

async def app_commitments(request: Request) -> JSONResponse:
    state = state_of(request)
    async with state.ro_pool.acquire() as conn:
        out = await agenda.board(conn, today=_today(state), tz=state.config.timezone)
    out["reasons"] = [{"code": code, "label": label} for code, label in commitments.WRONG_REASONS.items()]
    return JSONResponse(out)


def _id(request: Request) -> int:
    return int(request.path_params["commitment_id"])


async def _shown(conn: Any, commitment_id: int, v: Any, today: date, *, team: bool = False) -> dict[str, Any]:
    """Договорённость, которую владелец видел в списке: принятая, его сторона, отпечаток тот же.
    `team` — из списка «В команде»: обещание между другими людьми (статус team)."""
    if not isinstance(v, str) or not 8 <= len(v) <= 64:
        raise BadRequest("Обновите список и попробуйте ещё раз.", 400, "bad_version")
    item = await commitments.get_commitment(conn, commitment_id, today=today)
    if team:
        if item is None or item["status"] != "team":
            raise BadRequest(GONE, 409, "gone")
        if item["approval_fingerprint"][:agenda.SHOWN] != v:
            raise BadRequest(CHANGED, 409, "changed")
        return item
    if item is None or item["status"] != "open" or item["direction"] not in agenda.DIRECTIONS:
        raise BadRequest(GONE, 409, "gone")
    if item["approval_fingerprint"][:agenda.SHOWN] != v:
        raise BadRequest(CHANGED, 409, "changed")
    return item


def _new_date(data: dict[str, Any], today: date) -> date:
    to = data.get("to")
    if to == "tomorrow":
        return today + timedelta(days=1)
    if to == "week":
        return today + timedelta(days=7)
    if to != "date":
        raise BadRequest("Куда перенести: на завтра, через неделю или на дату.", 400, "bad_to")
    raw = data.get("date")
    try:
        picked = date.fromisoformat(raw) if isinstance(raw, str) and len(raw) == 10 else None
    except ValueError:
        picked = None
    if picked is None:
        raise BadRequest("Выберите дату.", 400, "bad_date")
    if picked < today:
        raise BadRequest("Эта дата уже прошла — выберите сегодня или позже.", 400, "past")
    if picked > today + timedelta(days=MOVE_MAX_DAYS):
        raise BadRequest("Слишком далеко — выберите дату в ближайшие три года.", 400, "too_far")
    return picked


async def _act(request: Request, action: str) -> JSONResponse:
    data = await page._body(request)
    state, session = state_of(request), request.state.app_session
    commitment_id, today = _id(request), _today(state)
    via = {"via": "app"}
    async with state.pool.acquire() as conn:
        with authority.miniapp_context(session.user_id, chat_id=session.chat_id):
            if action == "reopen":
                out, said = await _reopen(conn, commitment_id, today), "Вернул в список."
                detail = f"договорённость № {commitment_id}"
            else:
                item = await _shown(conn, commitment_id, data.get("v"), today, team=action == "hide")
                expected = item["approval_fingerprint"]
                if action == "hide":
                    out = await commitments.hide(conn, commitment_id, details=via, expected_fingerprint=expected)
                    said, detail = "Скрыто из «В команде».", f"договорённость № {commitment_id}"
                elif action == "done":
                    out = await commitments.close(conn, commitment_id, actor="owner", details=via, today=today,
                                                  expected_fingerprint=expected)
                    said, detail = "Отмечено: выполнено.", f"договорённость № {commitment_id}"
                elif action == "wrong":
                    reason = data.get("reason")
                    if reason is not None and reason not in commitments.WRONG_REASONS:
                        raise BadRequest("Неизвестная причина.", 400, "bad_reason")
                    details = {**via, "reason": reason} if reason else via
                    out = await commitments.mark_wrong(conn, commitment_id, actor="owner", details=details,
                                                       today=today, expected_fingerprint=expected)
                    said, detail = "Убрал: записано неверно.", f"договорённость № {commitment_id}" + (
                        f"; причина: {reason}" if reason else "")
                else:
                    new = _new_date(data, today)
                    out = await commitments.reschedule_to(conn, commitment_id, new, actor="owner", details=via,
                                                          today=today, expected_fingerprint=expected)
                    said = "Новый срок — " + dates.format_due(new, today=today) + "."
                    detail = f"договорённость № {commitment_id}; новый срок {new:%d.%m.%Y}"
    name = f"app.commitment_{action}"
    if not out.get("ok"):
        await page._log(request, name, audit.REFUSED, f"договорённость № {commitment_id}; причина: {out.get('code')}")
        code = out.get("code")
        if code == "changed_meanwhile":
            raise BadRequest(CHANGED, 409, "changed")
        raise BadRequest(out.get("error") or GONE, 409, code or "gone")
    await page._log(request, name, detail=detail)
    return JSONResponse({"ok": True, "answer": said, "undo": action in ("done", "wrong", "hide")})


async def _reopen(conn: Any, commitment_id: int, today: date) -> dict[str, Any]:
    """«Вернуть» — только то, что владелец сам только что закрыл из приложения: последнее событие
    договорённости — его «Выполнено» или «Не так» с пометкой приложения, не старше `REOPEN_WINDOW`."""
    last = await conn.fetchrow(
        """SELECT actor, action, details->>'via' AS via, at > now() - make_interval(secs => $2) AS fresh
           FROM commitment_events WHERE commitment_id = $1 ORDER BY at DESC, id DESC LIMIT 1""",
        commitment_id, float(REOPEN_WINDOW))
    if last is None or not (last["actor"] == "owner" and last["via"] == "app" and last["fresh"]
                            and last["action"] in ("closed", "marked_wrong", "hidden")):
        return {"ok": False, "code": "too_late", "error": "Вернуть можно только сразу после действия здесь."}
    if last["action"] == "hidden":
        return await commitments.unhide(conn, commitment_id, today=today)
    return await commitments.reopen(conn, commitment_id, actor="owner", details={"via": "app"}, today=today)


async def app_done(request: Request) -> JSONResponse:
    return await _act(request, "done")


async def app_move(request: Request) -> JSONResponse:
    return await _act(request, "move")


async def app_wrong(request: Request) -> JSONResponse:
    return await _act(request, "wrong")


async def app_reopen(request: Request) -> JSONResponse:
    return await _act(request, "reopen")


async def app_hide(request: Request) -> JSONResponse:
    return await _act(request, "hide")


def routes() -> list[BaseRoute]:
    from . import miniapp

    one = APP_COMMITMENTS + "/{commitment_id:int}"
    return [
        Route(APP_NOW, miniapp.endpoint(app_now), methods=["GET"]),
        Route(APP_COMMITMENTS, miniapp.endpoint(app_commitments), methods=["GET"]),
        Route(one + "/done", miniapp.endpoint(app_done), methods=["POST"]),
        Route(one + "/move", miniapp.endpoint(app_move), methods=["POST"]),
        Route(one + "/wrong", miniapp.endpoint(app_wrong), methods=["POST"]),
        Route(one + "/reopen", miniapp.endpoint(app_reopen), methods=["POST"]),
        Route(one + "/hide", miniapp.endpoint(app_hide), methods=["POST"]),
    ]
