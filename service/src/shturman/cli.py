"""Команды сервиса переписки.

Строка подключения берётся из переменной окружения SHTURMAN_DSN.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path

from . import db
from .importer import import_export, scan
from .search import search


def _dsn() -> str:
    dsn = os.environ.get("SHTURMAN_DSN")
    if not dsn:
        sys.exit("не задана переменная окружения SHTURMAN_DSN")
    return dsn


def _parse_exclude(values: list[str]) -> set[tuple[str, int]]:
    out: set[tuple[str, int]] = set()
    for v in values:
        cls, _, num = v.partition(":")
        if cls not in ("user", "chat", "channel") or not num.isdigit():
            sys.exit(f"неверное исключение {v!r}: нужен вид user:123, chat:123 или channel:123")
        out.add((cls, int(num)))
    return out


async def _migrate() -> None:
    conn = await db.connect(_dsn())
    try:
        applied = await db.migrate(conn)
    finally:
        await conn.close()
    print("применено: " + (", ".join(applied) if applied else "ничего, схема актуальна"))


@contextlib.contextmanager
def _open_export(path: str):
    """result.json или архив zip папки выгрузки (вид — по первым байтам). Отдаёт (архив или None, поток)."""
    from .export_archive import ArchiveError, ExportArchive, file_is_zip

    if not file_is_zip(Path(path)):
        with open(path, "rb") as fp:
            yield None, fp
        return
    try:
        archive = ExportArchive(Path(path))
    except ArchiveError as exc:
        sys.exit(str(exc))
    with archive, archive.open_result() as fp:
        yield archive, fp


async def _attachments(conn, archive):
    """Файлы вложений из архива — на разбор, если расшифровка или разбор включены. Команда
    работает в контейнере сервиса с тем же каталогом данных; без него файлы не берутся."""
    from .config import Config, ConfigError
    from .media.from_export import Attachments, rules

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"файлы вложений из архива не берутся: {exc}", file=sys.stderr)
        return None
    found = await rules(conn, config)
    if not found.any:
        return None
    if not os.access(config.data_dir, os.W_OK):
        print(f"файлы вложений из архива не берутся: каталог данных {config.data_dir} недоступен для записи",
              file=sys.stderr)
        return None
    return Attachments(archive, config.data_dir, found)


# Ответы, в которых нет ни текста переписки, ни названий чатов, ни имён — только числа и состояния
# (ими пользуются ./ops/doctor.sh, up.sh, guard.sh, embeddings.sh, asr.sh). Остальные ответы на GET
# `shturman call` печатает только человеку в терминале (Р-98): вывод команды, которую выполняет ИИ-агент,
# уходит провайдеру его модели.
SAFE_GET = ("/api/status", "/api/executor/status", "/api/guard/status", "/api/embeddings/status",
            "/api/voice/status")


def _human() -> bool:
    """Команду запустил человек в терминале: и ввод, и вывод — терминал (docker exec -it …)."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _human_only(what: str, how: str) -> None:
    if not _human():
        sys.exit(f"{what} — показывается только человеку в его терминале: {how}. Агент эту команду не "
                 "выполняет: её вывод ушёл бы провайдеру модели агента (AGENTS.md, docs/pilot/locks-audit.md).")


def _scan(path: str) -> None:
    _human_only("Список чатов выгрузки — названия чатов и имена людей",
                "./ops/import-export.sh scan в своём терминале")
    with _open_export(path) as (_, fp):
        owner, chats = scan(fp)
    if owner:
        print(f"владелец: {owner.name or '—'} ({owner.tg_user_id})")
    total = sum(c.messages for c in chats)
    print(f"чатов: {len(chats)}, сообщений: {total}")
    for c in chats:
        period = f"{c.first_at} … {c.last_at}" if c.first_at else "пусто"
        print(f"{c.messages:>8}  {c.peer_class}:{c.tg_id:<14} {c.type:<19} {period}  {c.name or '—'}")


async def _import(path: str, owner_id: int | None, exclude: set[tuple[str, int]]) -> None:
    conn = await db.connect(_dsn())
    try:
        await db.migrate(conn)
        with _open_export(path) as (archive, fp):
            attachments = await _attachments(conn, archive) if archive is not None else None
            stats = await import_export(
                conn, fp, owner_tg_user_id=owner_id, exclude=exclude,
                source_name="export.zip" if archive is not None else os.path.basename(path),
                attachments=attachments,
            )
    finally:
        await conn.close()
    print(json.dumps(stats.as_dict(), ensure_ascii=False, indent=2))


async def _search(query: str, limit: int) -> None:
    _human_only("Найденные сообщения — текст переписки", "docker exec -it shturman-service shturman search …")
    conn = await db.connect(_dsn())
    try:
        hits = await search(conn, query, limit=limit)
    finally:
        await conn.close()
    for h in hits:
        who = "я" if h["is_outgoing"] else (h["sender_name"] or "—")
        print(f"[{h['sent_at']:%Y-%m-%d %H:%M}] {h['chat_title'] or h['chat_type']} · {who}: {h['snippet']}")
    if not hits:
        print("ничего не найдено")


def _local_api(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    """Запрос к своему же сервису с токеном из окружения (команды оператора внутри контейнера)."""
    import urllib.error
    import urllib.request

    token = os.environ.get("SHTURMAN_API_TOKEN", "")
    if not token:
        sys.exit("не задана переменная окружения SHTURMAN_API_TOKEN")
    port = os.environ.get("SHTURMAN_PORT", "8765")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method.upper(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # мимо прокси из окружения
    try:
        with opener.open(request, timeout=60) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {"error": f"HTTP {exc.code}"}
    except OSError as exc:
        sys.exit(f"сервис не отвечает: {exc}")


def _call(method: str, path: str, raw: str | None) -> None:
    if not path.startswith("/api/"):
        sys.exit("путь должен начинаться с /api/")
    if method.upper() == "GET" and path.split("?", 1)[0].rstrip("/") not in SAFE_GET:
        _human_only("Ответ может содержать текст переписки, названия чатов или имена",
                    f"docker exec -it shturman-service shturman call GET '{path}'")
    try:
        payload = json.loads(raw) if raw else None
    except ValueError:
        sys.exit("третий аргумент должен быть JSON")
    status, out = _local_api(method, path, payload)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    waiting = pending_text(out)
    if waiting:
        # Отдельно от JSON и простыми словами: оператор не должен принять ответ за «сделано».
        print("\n" + waiting, file=sys.stderr)
    if status >= 400:
        sys.exit(1)


def pending_text(out: dict) -> str | None:
    """Пояснение к ответу «ждёт подтверждения владельца»; None — если ответ не об этом."""
    if not isinstance(out, dict) or out.get("status") != "pending_confirmation":
        return None
    action = out.get("action_id")
    lines = ["ЖДЁТ ПОДТВЕРЖДЕНИЯ В БОТЕ. Действие не выполнено: оно применится, только когда владелец "
             "нажмёт «Да, сделать» под карточкой в боте согласований."]
    if out.get("summary"):
        lines.append(f"Что именно ждёт (действие № {action}): {out['summary']}")
    if out.get("applied_now"):
        lines.append("Часть запроса, которая только ужесточает правила, уже применена: "
                     + json.dumps(out["applied_now"], ensure_ascii=False))
    if out.get("expires_at"):
        lines.append(f"Срок ответа — до {out['expires_at']}; потом запрос снимется сам.")
    lines.append(f"Проверить: shturman call GET /api/confirmations/{action}   "
                 f"Отменить: shturman call POST /api/confirmations/{action}/cancel")
    return "\n".join(lines)


def _await_confirmation(out: dict) -> None:
    """Ждёт решения владельца по действию из ответа 202. Завершает команду, если ответ — не «да»."""
    import time

    action = out.get("action_id")
    print("\nВход нужно подтвердить в боте согласований: откройте бота и нажмите «Да, сделать» под "
          "карточкой. Жду вашего ответа (Ctrl+C — отменить запрос)…")
    try:
        while True:
            time.sleep(3)
            status, state = _local_api("GET", f"/api/confirmations/{action}")
            if status >= 400:
                sys.exit(state.get("error") or f"ошибка {status}")
            if state.get("status") == "applied":
                return
            if state.get("status") != "pending":
                words = {"rejected": "отклонён", "expired": "не подтверждён вовремя", "failed": "не выполнен"}
                sys.exit(f"вход {words.get(state.get('status'), 'не подтверждён')} — запустите команду заново")
    except KeyboardInterrupt:
        _local_api("POST", f"/api/confirmations/{action}/cancel", {})
        sys.exit("\nотменено")


def _tg_login(role: str) -> None:
    """Вход в аккаунт Telegram по QR в терминале. Пароль 2FA вводится скрыто и не сохраняется."""
    import getpass
    import time

    import segno

    if not sys.stdin.isatty():
        sys.exit("вход выполняет человек в интерактивном терминале")
    body: dict = {"role": role}
    if role == "owner":
        print("Вы подключаете ОСНОВНОЙ аккаунт. Сервис будет только читать выбранные вами чаты и\n"
              "ничего не отправит от его имени. На сервере появится сессия этого аккаунта: риск\n"
              "ограничений со стороны Telegram ложится на него. Завершить сессию можно в Telegram:\n"
              "Настройки → Устройства.")
        if input("Чтобы продолжить, напишите «да»: ").strip().lower() != "да":
            sys.exit("отменено")
        body["confirm_owner"] = True
    status, out = _local_api("POST", "/api/tg/login", body)
    if status == 202 and out.get("status") == "pending_confirmation":
        # У сервиса свой бот согласований: подключение аккаунта сначала подтверждает владелец.
        _await_confirmation(out)
        status, out = _local_api("POST", "/api/tg/login", body)
        if status == 202 and out.get("status") == "pending_confirmation":
            _local_api("POST", f"/api/confirmations/{out.get('action_id')}/cancel", {})   # лишняя карточка
            sys.exit("разрешение на вход уже израсходовано или истекло — запустите команду заново")
    if status >= 400:
        sys.exit(out.get("error") or f"ошибка {status}")
    login_id, shown = out["login_id"], None
    try:
        while True:
            state = out.get("status")
            if state == "pending":
                if out.get("link") and out["link"] != shown:
                    shown = out["link"]
                    print("\nВ Telegram на телефоне: Настройки → Устройства → Подключить устройство.\n"
                          "Наведите камеру на код:\n")
                    segno.make(shown, error="l").terminal(compact=True, border=1)
            elif state == "password_required":
                hint = f" (подсказка: {out['hint']})" if out.get("hint") else ""
                password = getpass.getpass(f"Пароль облачного хранения Telegram{hint}: ")
                status, out = _local_api("POST", f"/api/tg/login/{login_id}/password", {"password": password})
                password = ""
                if status >= 400:
                    print(out.get("error") or f"ошибка {status}")
                    status, out = _local_api("GET", f"/api/tg/login/{login_id}")
                continue
            elif state == "completed":
                print("\nГотово: аккаунт подключён. Ничего не читается, пока вы не выберете чаты.")
                return
            else:
                sys.exit(out.get("error") or f"вход не выполнен: {state}")
            time.sleep(2)
            status, out = _local_api("GET", f"/api/tg/login/{login_id}")
            if status >= 400:
                sys.exit(out.get("error") or f"ошибка {status}")
    except KeyboardInterrupt:
        _local_api("POST", f"/api/tg/login/{login_id}/cancel", {})
        sys.exit("\nотменено")


def _bot_bind() -> None:
    """Одноразовая ссылка привязки владельца к боту согласований (см. executor/commands.py)."""
    from .executor import commands

    asyncio.run(commands.bot_bind(_dsn()))


def _bot_status() -> None:
    from .executor import commands

    commands.bot_status(_local_api)


def _setup_base() -> tuple[str, str | None]:
    """Адрес, под которым открывается страница настройки, и причина, по которой он не внешний
    (None — внешний; no_origin — не задан; same_origin — совпал с адресом дашборда Hermes)."""
    from .config import ConfigError, _setup_origin, normalize_origin

    try:
        origin = _setup_origin(os.environ.get("SHTURMAN_SETUP_ORIGIN", "").strip())
        dashboard = normalize_origin(os.environ.get("SHTURMAN_DASHBOARD_ORIGIN", "").strip(),
                                     "SHTURMAN_DASHBOARD_ORIGIN", strict=False)
    except ConfigError as exc:
        sys.exit(str(exc))
    if origin and origin != dashboard:
        return origin, None
    local = f"http://127.0.0.1:{os.environ.get('SHTURMAN_PORT', '').strip() or '8765'}"
    return local, "same_origin" if origin else "no_origin"


async def _setup_link(full_url: bool) -> None:
    """Одноразовая ссылка входа на страницу настройки (см. setup_page/auth.py).

    Пишет прямо в базу, а не просит работающий сервис: токен внутреннего API есть у ассистента
    в Hermes, и маршрут «выдать ссылку» позволил бы ему войти на страницу самому.

    В стандартный вывод идёт одна строка — путь со значением ссылки (или полный адрес с ключом
    --url); пояснения — в поток ошибок, значения ссылки в них нет."""
    from . import setup_page
    from .setup_page import auth

    conn = await db.connect(_dsn())
    try:
        await db.migrate(conn)
        token, expires_at = await auth.create_link(conn)
    finally:
        await conn.close()
    base, local_reason = _setup_base()
    path = f"{setup_page.PREFIX}/#{token}"
    print(base + path if full_url else path)
    note = sys.stderr
    print(f"\nОдноразовая ссылка входа на страницу настройки. Действует {auth.LINK_TTL // 60} минут "
          f"(до {expires_at:%H:%M} UTC) и срабатывает один раз.", file=note)
    if not full_url:
        print(f"Это путь: допишите его к адресу страницы — {base}", file=note)
    if local_reason == "same_origin":
        print("ВНИМАНИЕ: адрес страницы (SHTURMAN_SETUP_ORIGIN) совпадает с адресом дашборда Hermes "
              "(SHTURMAN_DASHBOARD_ORIGIN). По такому адресу страница не отдаётся: дайте ей другой порт "
              "(например :8443) или другое имя. Пока она открывается только с самого сервера либо через "
              "туннель SSH на порт сервиса.", file=note)
    elif local_reason:
        print("Внешний адрес страницы не задан (SHTURMAN_SETUP_ORIGIN пуст): она открывается только "
              "с самого сервера либо через туннель SSH на порт сервиса.", file=note)
    print("Кто откроет ссылку, тот войдёт на страницу настройки: её передают владельцу как есть "
          "и нигде не сохраняют.", file=note)
    print("Прежняя ссылка входа больше не действует. Вход по новой ссылке завершит открытые сессии.", file=note)


async def _setup_logout_all() -> None:
    from .setup_page import auth

    conn = await db.connect(_dsn())
    try:
        await db.migrate(conn)
        count = await auth.revoke_all(conn)
    finally:
        await conn.close()
    print(f"Завершено сессий страницы настройки: {count}. Невостребованная ссылка входа отменена.")


async def _retry_failed(since_text: str | None, dry_run: bool) -> None:
    """Повтор того, что не получилось из-за сбоя модели (retry.py, Р-84). Запускает оператор по решению
    владельца: повторный разбор снова отправляет данные провайдеру модели и тратит запросы."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from . import retry

    try:
        days = min(3650, max(0, int(os.environ.get("SHTURMAN_MEDIA_DAYS", "") or 30)))
    except ValueError:
        sys.exit("SHTURMAN_MEDIA_DAYS: нужно целое число дней")
    since = None
    if since_text:
        try:
            tz = ZoneInfo(os.environ.get("SHTURMAN_TIMEZONE", "").strip() or "Europe/Moscow")
            since = datetime.strptime(since_text, "%Y-%m-%d").replace(tzinfo=tz)
        except ValueError:
            sys.exit("--since: дата вида 2026-10-01")
        except Exception:
            sys.exit("SHTURMAN_TIMEZONE: неизвестный часовой пояс")
    conn = await db.connect(_dsn())
    try:
        await db.migrate(conn)
        found = await retry.candidates(conn, media_days=days, since=since)
        if dry_run:
            print(f"Можно вернуть: вложений — {found['media']}"
                  + ("" if found["media_enabled"] else " (разбор фото и документов выключен — не вернутся)")
                  + f", запросов обработки переписки — {found['requests']}.")
            print(f"Не повторяются (не сбой модели): вложений — {found['media_not_model']}. "
                  f"Страниц памяти без сводки (пересоберутся сами): {found['pages']}.")
            return
        out = await retry.run(conn, media_days=days, since=since, by="cli")
        await conn.execute(
            "INSERT INTO setup_audit (action, outcome, detail) VALUES ('retry.failed', 'ok', $1)",
            f"из терминала; вложений: {out['media']}; запросов обработки: {out['requests']}"
            + (f"; с {since.date().isoformat()}" if since else ""))
    finally:
        await conn.close()
    print(f"Возвращено в работу: вложений — {out['media']}, запросов обработки переписки — {out['requests']}.")
    if not out["media_enabled"]:
        print("Разбор фото и документов выключен владельцем: вложения не возвращались.")
    print("Вложения разберутся в ближайшие минуты; запросы обработки — в следующий прогон (ночью). "
          "Повторный разбор снова уходит провайдеру модели и тратит запросы.")


async def _commitments(action: str, *, as_json: bool, dry_run: bool, limit: int, force: bool,
                       price_in: float | None, price_out: float | None) -> None:
    """Договорённости (Р-96): `stats` — только агрегаты; `recheck` — перепроверка уже найденного по новым
    правилам. Перепроверка отправляет переписку провайдеру модели и тратит запросы — запускается по
    решению владельца; сначала `--dry-run`."""
    import json as _json

    from .processing import commitment_stats, pipeline

    conn = await db.connect(_dsn())
    try:
        await db.migrate(conn)
        if action == "stats":
            stats = await commitment_stats.collect(conn)
            print(_json.dumps(stats, ensure_ascii=False, indent=2) if as_json else commitment_stats.render(stats))
            return
        tz = os.environ.get("SHTURMAN_TIMEZONE", "").strip() or "Europe/Moscow"
        out = await pipeline.recheck(conn, tz=tz, limit=limit, dry_run=dry_run, force=force)
        if dry_run or out.get("status") == "planned":
            await conn.execute(
                "INSERT INTO setup_audit (action, outcome, detail) VALUES ('commitments.recheck', 'ok', $1)",
                f"из терминала{'; только оценка' if dry_run else ''}; запросов: {out.get('requests', 0)}")
    finally:
        await conn.close()
    if as_json:
        print(_json.dumps(out, ensure_ascii=False, indent=2))
        return
    if out["status"] == "already_done":
        print(f"Перепроверка по правилам версии {out['prompt_version']} уже выполнялась ({out.get('at')}). "
              "Ещё раз — с --force.")
        return
    if out["status"] == "already_running":
        print("Сейчас идёт прогон обработки — повторите через несколько минут.")
        return
    print(("Оценка (ничего не поставлено): " if dry_run else "Поставлено: ")
          + f"запросов к модели — {out['requests']} (извлечение заново — {out['extract_planned']}, "
            f"проверка «уже сделано?» — {out['resolve_planned']}).")
    print(f"Предложений к перепроверке: {out['proposals_to_recheck']}; не собрался эпизод: "
          f"{out['proposals_not_found']}; не вошло в предел {out['limit']}: {out['proposals_over_limit']}.")
    cost = ""
    if price_in is not None and price_out is not None:
        dollars = out["tokens_in_estimate"] / 1e6 * price_in + out["tokens_out_estimate"] / 1e6 * price_out
        cost = f" — примерно ${dollars:.2f} по ценам {price_in} и {price_out} за миллион"
    print(f"Объём (грубо): на входе ~{out['tokens_in_estimate']:,} токенов, на выходе ~{out['tokens_out_estimate']:,}"
          .replace(",", " ") + cost + ".")
    if not dry_run:
        print("Ответы модели разберутся по мере выполнения; новые предложения — в приложении сразу, "
              "сводкой в боте — в ближайший час сводки. «Похоже, уже сделано» по принятым — тоже там.")


def _serve() -> None:
    import logging

    from . import listen
    from .app import build_app
    from .config import Config, ConfigError

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config = Config.from_env()
    except ConfigError as exc:
        sys.exit(str(exc))
    # access_log выключен: в адресах запросов нет секретов (на странице настройки они идут только
    # в теле POST), но журнал не должен расти от опроса очереди. proxy_headers выключен: сервис
    # стоит за обратным прокси, но заголовкам X-Forwarded-* не верит и ими не пользуется
    # (setup_page/shield.py) — адрес клиента и схема из них не подставляются. server_header выключен:
    # страница настройки видна снаружи, и версию сервера ей сообщать незачем.
    # Адресов два: TCP-порт (Hermes, оператор, туннель SSH) и Unix-сокет для обратного прокси
    # (listen.py, Р-76); приложение и его фоновая работа — одни на оба.
    server, sockets = listen.make_server(
        build_app(config), host=config.host, port=config.port, proxy_socket=config.proxy_socket,
        log_level="warning", access_log=False, proxy_headers=False, server_header=False)
    server.run(sockets=sockets)
    if not server.started:      # как uvicorn.run: запуск не удался — ненулевой код выхода
        sys.exit(3)


def main() -> None:
    p = argparse.ArgumentParser(prog="shturman", description="Сервис переписки «Штурмана»")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="привести схему базы к текущей версии")
    s = sub.add_parser("scan", help="показать чаты экспорта (result.json или архив zip), ничего не записывая")
    s.add_argument("path")
    i = sub.add_parser("import", help="импортировать экспорт Telegram Desktop (result.json или архив zip папки)")
    i.add_argument("path")
    i.add_argument("--owner-id", type=int, help="идентификатор владельца — нужен для экспорта одного чата")
    i.add_argument("--exclude", action="append", default=[], metavar="КЛАСС:ID",
                   help="не принимать чат в архив, например user:123; можно повторять")
    q = sub.add_parser("search", help="поиск по архиву")
    q.add_argument("query")
    q.add_argument("--limit", type=int, default=10)
    sub.add_parser("serve", help="запустить сервис: внутренний API и MCP-сервер архива")
    c = sub.add_parser("call", help="запрос к внутреннему API работающего сервиса (для оператора)")
    c.add_argument("method", choices=["GET", "POST", "PUT", "DELETE"])
    c.add_argument("path")
    c.add_argument("json", nargs="?")
    t = sub.add_parser("tg-login", help="вход в аккаунт Telegram по QR в терминале (выполняет человек)")
    t.add_argument("role", choices=["assistant", "owner"])
    sub.add_parser("bot-bind", help="одноразовая ссылка привязки владельца к боту согласований")
    sub.add_parser("bot-status", help="состояние бота согласований и своей модели сервиса")
    link = sub.add_parser("setup-link", help="одноразовая ссылка входа на страницу настройки")
    link.add_argument("--url", action="store_true",
                      help="напечатать полный адрес, а не только путь /shturman-setup/#…")
    sub.add_parser("setup-logout-all", help="завершить все сессии страницы настройки")
    rf = sub.add_parser("retry-failed", help="вернуть в работу то, что не получилось из-за сбоя модели "
                                             "(по решению владельца: снова тратит запросы к модели)")
    rf.add_argument("--since", metavar="ГГГГ-ММ-ДД", help="с какой даты считать сбой (по умолчанию — всё)")
    rf.add_argument("--dry-run", action="store_true", help="только показать, сколько вернётся")
    cm = sub.add_parser("commitments", help="договорённости: stats — числа «было/стало»; recheck — перепроверка "
                                            "по новым правилам (по решению владельца: тратит запросы к модели)")
    cm.add_argument("action", choices=["stats", "recheck"])
    cm.add_argument("--json", action="store_true", help="вывести JSON")
    cm.add_argument("--dry-run", action="store_true", help="recheck: только посчитать запросы и объём")
    cm.add_argument("--limit", type=int, default=300, help="recheck: не больше стольких запросов к модели")
    cm.add_argument("--force", action="store_true", help="recheck: ещё раз для той же версии правил")
    cm.add_argument("--price-in", type=float, help="цена миллиона входных токенов, для оценки в долларах")
    cm.add_argument("--price-out", type=float, help="цена миллиона выходных токенов")
    a = p.parse_args()

    if a.cmd == "migrate":
        asyncio.run(_migrate())
    elif a.cmd == "scan":
        _scan(a.path)
    elif a.cmd == "import":
        asyncio.run(_import(a.path, a.owner_id, _parse_exclude(a.exclude)))
    elif a.cmd == "search":
        asyncio.run(_search(a.query, a.limit))
    elif a.cmd == "serve":
        _serve()
    elif a.cmd == "call":
        _call(a.method, a.path, a.json)
    elif a.cmd == "tg-login":
        _tg_login(a.role)
    elif a.cmd == "bot-bind":
        _bot_bind()
    elif a.cmd == "bot-status":
        _bot_status()
    elif a.cmd == "setup-link":
        asyncio.run(_setup_link(a.url))
    elif a.cmd == "setup-logout-all":
        asyncio.run(_setup_logout_all())
    elif a.cmd == "retry-failed":
        asyncio.run(_retry_failed(a.since, a.dry_run))
    elif a.cmd == "commitments":
        asyncio.run(_commitments(a.action, as_json=a.json, dry_run=a.dry_run, limit=a.limit, force=a.force,
                                 price_in=a.price_in, price_out=a.price_out))


if __name__ == "__main__":
    main()
