#!/usr/bin/env python3
"""Запуск команды в псевдотерминале — за человека, который набирает фразу подтверждения.

Только для тестов ops/ (ops/tests/test_ops.sh, ops/tests/e2e_restore.sh): так проверяется, что скрипт
спрашивает фразу именно в терминале и принимает только точную фразу с одноразовым числом. Агенту
на сервере владельца так подтверждать нельзя (AGENTS.md): это и есть обход, от которого скрипт
не защищён технически (docs/security-review.md, «Противники»).

    pty_confirm.py right|wrong|none -- команда параметры…

right — набрать показанную фразу; wrong — набрать фразу с другим числом; none — ничего не набирать
(закрыть ввод). Печатает весь вывод команды; код выхода — код команды.
"""

import os
import pty
import re
import sys
import time

PROMPT = re.compile(rb"\xd0\x9d\xd0\xb0\xd0\xb1\xd0\xb5\xd1\x80\xd0\xb8\xd1\x82\xd0\xb5: (\S+) (\d{4})")  # «Наберите: СЛОВО 1234»


def main() -> int:
    mode = sys.argv[1]
    cmd = sys.argv[sys.argv.index("--") + 1:]
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(cmd[0], cmd)
    out = b""
    answered = 0
    deadline = time.time() + 900
    while time.time() < deadline:
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
        prompts = PROMPT.findall(out)
        if len(prompts) > answered:
            word, code = prompts[answered]
            answered += 1
            if mode == "right":
                os.write(fd, word + b" " + code + b"\n")
            elif mode == "wrong":
                os.write(fd, word + b" " + (b"0000" if code != b"0000" else b"1111") + b"\n")
            else:
                os.write(fd, b"\x04")
    _, status = os.waitpid(pid, 0)
    sys.stdout.write(out.decode("utf-8", "replace").replace("\r\n", "\n"))
    return os.waitstatus_to_exitcode(status)


if __name__ == "__main__":
    sys.exit(main())
