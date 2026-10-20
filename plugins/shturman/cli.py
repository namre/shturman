"""Служебные команды плагина. Запускаются скриптами из ops/ внутри контейнера Hermes.

  python cli.py activation-link <адрес>   — одноразовая ссылка первого входа или восстановления
  python cli.py status                    — состояние без секретов
  python cli.py logout-all                — завершить все сессии дашборда
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from shturman_core import bridge_stats  # noqa: E402
from shturman_core.auth import ACTIVATION_TTL, Auth  # noqa: E402
from shturman_core.state import Store  # noqa: E402


def _pages_prefix() -> str:
    value = os.environ.get("SHTURMAN_AUTH_PAGES", "/shturman-auth").strip() or "/shturman-auth"
    return "/" + value.strip("/")


def status_lines(store: Store, auth: Auth) -> list[str]:
    """Строки «имя=значение» для ops/doctor.sh: только признаки, без значений и имён."""
    wizard = store.read("wizard")
    # Что мост плагина знает о своём боте сервиса; unknown — шлюз не работает или ещё не спрашивал.
    own_bot = bridge_stats.status(store, configured=True)["own_bot"]
    return [
        f"owner_bound={'yes' if auth.owner() else 'no'}",
        f"wizard_completed={'yes' if wizard.get('completed_at') else 'no'}",
        f"activation_pending={'yes' if store.read('activation').get('digest') else 'no'}",
        f"bridge_own_bot={'unknown' if own_bot is None else 'yes' if own_bot else 'no'}",
        # Бот-ассистент (бот Hermes) подключён владельцем в бизнес-режиме Telegram — схема до 0.0.6.
        f"hermes_business={'yes' if store.read('business').get('connected') is True else 'no'}",
    ]


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else ""
    store = Store()
    auth = Auth(store)

    if command == "activation-link":
        base = (argv[2] if len(argv) > 2 else os.environ.get("HERMES_DASHBOARD_PUBLIC_URL", "")).strip()
        if not base.startswith(("https://", "http://")):
            print("нужен адрес дашборда, например https://assistant.example.com", file=sys.stderr)
            return 2
        recovery = auth.owner() is not None
        value = auth.issue_activation()
        # Значение стоит после «#»: при открытии ссылки браузер эту часть на сервер не отправляет.
        # Серверу оно передаётся один раз, при самом входе, и сразу перестаёт действовать.
        print(f"{base.rstrip('/')}{_pages_prefix()}/activate.html#{value}")
        print(f"Действует {ACTIVATION_TTL // 60} минут и срабатывает один раз.", file=sys.stderr)
        if recovery:
            print("Это ссылка восстановления: при входе по ней прежняя привязка бота и все открытые "
                  "сессии сбрасываются, бота нужно будет привязать заново.", file=sys.stderr)
        return 0

    if command == "status":
        print("\n".join(status_lines(store, auth)))
        return 0

    if command == "logout-all":
        auth.revoke_all_sessions()
        print("все сессии завершены")
        return 0

    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
