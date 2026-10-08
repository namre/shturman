"""Команды оператора для бота согласований: `shturman bot-bind` и `shturman bot-status`.

Выполняются внутри контейнера сервиса.

`bot-bind` пишет код привязки прямо в базу, а не просит об этом работающий сервис по HTTP.
Причина: внутренний API открывается токеном `SHTURMAN_API_TOKEN`, а он есть у ассистента в Hermes;
маршрут «создать код привязки» позволил бы ассистенту самому назначить владельца. Строка
подключения к базе есть только в контейнере сервиса. Работающий опрос бота видит код сразу:
он ищет его в той же базе.

`bot-status` только читает: спрашивает у работающего сервиса `GET /api/executor/status`.

Ссылка из `bot-bind` — одноразовый пропуск во владельцы. Её передают владельцу как есть
и нигде не сохраняют.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Callable

from .. import db
from . import binding

PROBLEMS = {
    "token_rejected": "Telegram не принял токен бота — проверьте SHTURMAN_BOT_TOKEN",
    "bad_token_format": "токен бота записан неверно — проверьте SHTURMAN_BOT_TOKEN",
    "other_poller": "этого бота уже опрашивает другая программа — у бота согласований должен быть свой токен",
    "webhook": "у бота включён webhook — отключите его (deleteWebhook), иначе обновления не приходят",
    "conflict": "Telegram сообщает о конфликте: бота опрашивает кто-то ещё",
    "no_connection": "нет связи с Telegram",
    "too_many_requests": "Telegram просит подождать (слишком много запросов)",
    "proxy_needs_socksio": "для прокси SOCKS нужен пакет socksio — без него бот в сеть не выходит",
    "bad_proxy_url": "адрес прокси (EGRESS_PROXY_URL) записан неверно",
    "busy": "база временно недоступна, обновления ждут",
    "internal": "внутренняя ошибка — подробности в журнале сервиса",
}


def _yes(value: Any) -> str:
    return "да" if value else "нет"


def _age(seconds: Any) -> str:
    if not isinstance(seconds, int):
        return "ещё не было"
    if seconds < 90:
        return f"{seconds} с назад"
    if seconds < 5400:
        return f"{seconds // 60} мин назад"
    return f"{seconds // 3600} ч назад"


def _bot_configured() -> bool:
    """Задан ли токен бота: в окружении либо на странице настройки (файл в каталоге данных).
    Само значение не читается дальше проверки «есть ли»."""
    if os.environ.get("SHTURMAN_BOT_TOKEN", "").strip():
        return True
    from pathlib import Path

    from ..setup_page import secrets_store

    data_dir = Path(os.environ.get("SHTURMAN_DATA_DIR", "/data").strip() or "/data")
    return secrets_store.SecretStore(data_dir).has(secrets_store.BOT_TOKEN)


async def bot_bind(dsn: str) -> None:
    """Создаёт одноразовую ссылку привязки владельца и печатает её."""
    if not _bot_configured():
        sys.exit("бот согласований не настроен: токен не задан ни в настройках сервера "
                 "(SHTURMAN_BOT_TOKEN), ни на странице настройки")
    conn = await db.connect(dsn)
    try:
        bot = await binding.get_state(conn, "bot")
        username = bot.get("username") if bot else None
        if not isinstance(username, str) or not username:
            sys.exit("сервис ещё не связался с Telegram от имени бота — запустите сервис "
                     "и проверьте `shturman bot-status`")
        rebind = await binding.get_state(conn, "owner") is not None
        code, expires_at = await binding.create_code(conn)
    finally:
        await conn.close()
    minutes = binding.CODE_TTL // 60
    print("Ссылка привязки владельца к боту согласований:\n")
    print("  " + binding.deep_link(username, code) + "\n")
    print(f"Действует {minutes} минут (до {expires_at:%H:%M} UTC) и срабатывает один раз.")
    print("Откройте её в Telegram с аккаунта владельца и нажмите «Запустить».")
    print("Тот, кто это сделает, станет владельцем: ссылку нельзя пересылать и сохранять.")
    print("Прежние ссылки привязки больше не действуют.")
    if rebind:
        print("\nВладелец уже привязан. Если ссылку откроет другой аккаунт, владелец сменится: "
              "ждущие черновики отклонятся, список доверенных очистится.")


def bot_status(local_api: Callable[[str, str], tuple[int, dict]]) -> None:
    """Печатает состояние бота согласований и своей модели. Идентификаторов в выводе нет."""
    code, out = local_api("GET", "/api/executor/status")
    if code >= 400:
        sys.exit(out.get("error") or f"сервис ответил ошибкой {code}")
    bot, llm, jobs = out.get("bot") or {}, out.get("llm") or {}, out.get("jobs") or {}
    if not bot.get("configured"):
        # Сервис не знает, установлен ли Hermes: говорим о том, что видно ему самому.
        print("Бот согласований: не настроен (SHTURMAN_BOT_TOKEN не задан). "
              "Карточки и кнопки сервис сам не ведёт: их задания ждут плагин в Hermes, "
              "а без Hermes доставлять их некому. Отправка выключена.")
    else:
        print("Бот согласований: настроен")
        print(f"  имя бота: {'@' + bot['username'] if bot.get('username') else 'ещё не получено от Telegram'}")
        polling = bot.get("polling")
        print("  опрос Telegram: " + ("работает" if polling else "ещё не начат" if polling is None else "не работает"))
        if bot.get("problem"):
            print("  неполадка: " + PROBLEMS.get(bot["problem"], str(bot["problem"])))
        print(f"  последний ответ Telegram: {_age(bot.get('last_poll_age'))}")
        print(f"  последнее обновление: {_age(bot.get('last_update_age'))}")
        print(f"  владелец привязан: {_yes(bot.get('owner_bound'))}"
              + ("" if bot.get("owner_bound") else " — выполните `shturman bot-bind`"))
        if bot.get("bind_paused"):
            print("  приём кодов привязки приостановлен: было много неверных кодов. Подождите 10 минут.")
        if bot.get("business_capable") is False:
            print("  бизнес-режим: у бота он выключен (в @BotFather: Bot Settings → Secretary Mode; "
                  "раньше пункт назывался Business Mode)")
        counters = bot.get("counters") or {}
        if counters:
            print("  счётчики: " + ", ".join(f"{k}={v}" for k, v in sorted(counters.items())))
    if not llm.get("configured"):
        print("Своя модель: не настроена. Запросы к модели ждут плагин в Hermes; "
              "без Hermes выполнять их некому.")
    else:
        print(f"Своя модель: {llm.get('model')}")
        for task, model in sorted((llm.get("task_models") or {}).items()):
            print(f"  для задачи {task}: {model}")
        last = llm.get("last_call_ok")
        print("  последнее обращение: " + ("ещё не было" if last is None else "успешно" if last else
                                           f"ошибка ({llm.get('problem')})"))
        print(f"  обращений: {llm.get('calls', 0)}, неудач: {llm.get('failures', 0)}")
    done, failed = jobs.get("done") or {}, jobs.get("failed") or {}
    for kind in sorted(set(done) | set(failed)):
        print(f"Задания {kind}: выполнено {done.get(kind, 0)}, не выполнено {failed.get(kind, 0)}")
    if jobs.get("sends_unknown"):
        print(f"Отправок с неизвестным исходом: {jobs['sends_unknown']}")
