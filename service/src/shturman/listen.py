"""Где слушает сервис: порт для Hermes и оператора и Unix-сокет для обратного прокси (Р-76).

Сервис слушает два адреса, одним процессом и одним приложением:

  * TCP `SHTURMAN_HOST:SHTURMAN_PORT` (в контейнере 0.0.0.0:8765, на хосте опубликован только
    как 127.0.0.1:8765) — для Hermes (плагин и архив), оператора и туннеля SSH;
  * Unix-сокет `SHTURMAN_PROXY_SOCKET` (в compose — /run/shturman-service/proxy.sock) — для
    обратного прокси Caddy на хосте: страница настройки переписки, Mini App и, если владелец его
    включил, внешний шлюз архива.

Зачем сокет. Контейнер Hermes работает в сети хоста. Пока сервис перезапускается, порт
127.0.0.1:8765 на хосте свободен, и процесс из Hermes может занять его раньше Docker — тогда
прокси отдавал бы ему то, что владелец вводит на странице настройки. Сокет лежит в каталоге
хоста, который подключён только контейнеру сервиса: в файловой системе Hermes его нет вовсе.
Права каталога — второй рубеж, на случай ошибки в подключениях: его создаёт systemd-tmpfiles
(config/shturman-service-socket.conf) с владельцем — пользователем стека (под ним работает
сервис), группой caddy и режимом 2710. Входить в каталог могут только владелец и группа caddy,
создавать файлы — только владелец; бит setgid даёт сокету группу caddy, а режим сокета 0660
закрывает его от остальных, даже если каталог кто-то откроет шире.

Сервис открывает сокет только в каталоге, который выглядит именно так. Иначе он работает как
раньше, только по TCP, и причину видно в состоянии (`state()`, строка setup-socket в
./ops/doctor.sh). Так экземпляр, где каталог ещё не создан, обновляется без поломки, а прокси
переключают на сокет отдельным шагом (UPGRADING.md).

Абстрактные Unix-сокеты (имя с «\\0») не годятся: их пространство имён — сетевое, а сеть у Hermes
общая с хостом. Поэтому только файл в каталоге.
"""

from __future__ import annotations

import logging
import os
import socket
import stat
from pathlib import Path
from typing import Any

logger = logging.getLogger("shturman")

# Режим сокета: владелец (сервис) и группа каталога (caddy) — чтение и запись, остальным ничего.
SOCKET_MODE = 0o660

# Состояние сокета для /api/status и ./ops/doctor.sh. Только код, без путей и имён:
#   off         — сокет не задан (SHTURMAN_PROXY_SOCKET пуст);
#   listening   — сокет открыт, прокси может ходить через него;
#   dir_missing — каталога сокета нет (не установлен config/shturman-service-socket.conf);
#   dir_owner   — каталог принадлежит не пользователю сервиса (например, его создал Docker от root);
#   dir_mode    — права каталога не 2710/2750: нет setgid, группе разрешена запись или открыт другим;
#   not_socket  — на месте сокета лежит не сокет — сервис его не трогает;
#   error       — не удалось открыть сокет (подробности — в журнале контейнера).
_state = "off"


def state() -> str:
    return _state


def _set(value: str) -> None:
    global _state
    _state = value


def check_dir(directory: Path) -> str | None:
    """Пригоден ли каталог для сокета. None — да; иначе код из перечня выше."""
    try:
        st = os.stat(directory)
    except FileNotFoundError:
        return "dir_missing"
    if not stat.S_ISDIR(st.st_mode):
        return "dir_missing"
    if st.st_uid != os.geteuid():
        return "dir_owner"
    mode = stat.S_IMODE(st.st_mode)
    # setgid — сокет получает группу каталога (caddy); группе — только вход (и, по желанию,
    # чтение списка), без записи: заменить сокет может только сервис; остальным — ничего.
    if not mode & stat.S_ISGID or not mode & stat.S_IXGRP or mode & stat.S_IWGRP or mode & 0o007:
        return "dir_mode"
    return None


def open_proxy_socket(path: str) -> socket.socket | None:
    """Открывает Unix-сокет для обратного прокси или объясняет в журнале, почему нет.

    Возвращает связанный (bind), ещё не слушающий сокет; слушать начнёт uvicorn."""
    if not path:
        _set("off")
        return None
    target = Path(path)
    problem = check_dir(target.parent)
    if problem:
        _set(problem)
        logger.warning("сокет для обратного прокси не открыт (%s): сервис слушает только TCP-порт. "
                       "Каталог сокета — docs/deployment.md, «Сокет для обратного прокси»", problem)
        return None
    try:
        current = os.lstat(target)
    except FileNotFoundError:
        current = None
    if current is not None:
        if not stat.S_ISSOCK(current.st_mode):
            _set("not_socket")
            logger.warning("на месте сокета для обратного прокси лежит не сокет — сервис его не трогает "
                           "и слушает только TCP-порт")
            return None
        # Сокет от прошлого запуска: создать его в этом каталоге мог только сам сервис.
        target.unlink()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(target))
        os.chmod(target, SOCKET_MODE)
    except OSError as exc:
        sock.close()
        _set("error")
        logger.warning("сокет для обратного прокси не открыт: %s — сервис слушает только TCP-порт", exc)
        return None
    sock.set_inheritable(True)
    _set("listening")
    return sock


def make_server(app: Any, *, host: str, port: int, proxy_socket: str = "",
                **options: Any) -> tuple[Any, list[socket.socket]]:
    """Один сервер uvicorn на два адреса: TCP-порт и, если можно, Unix-сокет для прокси.

    Приложение и его жизненный цикл — одни на оба адреса: фоновая работа (бот согласований,
    сессии Telegram) запускается один раз."""
    import uvicorn

    config = uvicorn.Config(app, host=host, port=port, **options)
    sockets = [config.bind_socket()]
    unix = open_proxy_socket(proxy_socket)
    if unix is not None:
        sockets.append(unix)
    return uvicorn.Server(config), sockets
