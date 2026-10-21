#!/usr/bin/env python3
"""Очистка того, что ./ops/diag.sh кладёт в диагностический пакет (Р-98). Только стандартная библиотека.

Принцип — белый список: из каждой строки берутся только поля известного вида, остальное отбрасывается.
Не «вырезать опасное» (имена, телефоны, токены можно не узнать), а «взять безопасное»:

  service-log    журнал сервиса переписки (формат `asctime levelname name: message`, cli.py): время до
                 минуты, уровень, имя журнала (модуль кода) и имена классов исключений из текста
                 (ConnectionError, AuthKeyUnregisteredError…) и коды HTTP. Сам текст сообщения — нет:
                 в нём бывают номера чатов, роли и подписи. Строки другого вида — только счётчиком,
                 кроме отказа запуска из-за настройки (config.py, «SHTURMAN_…: …»): от неё берётся
                 только имя переменной окружения — значение и пример адреса не берутся.
  hermes-errors  журнал ошибок Hermes (logs/errors.log, `asctime levelname [сессия] name: message`):
                 только счётчики по уровню и имени журнала за последние сутки. Hermes 0.21.5 пишет
                 в свои журналы начало текста входящих сообщений («inbound message: … msg=…»,
                 gateway/run_turn.py) — поэтому ни одной строки текста из них не берётся.
  doctor         вывод ./ops/doctor.sh: строки «PASS|WARN|FAIL|SKIP  имя: …» и «ИТОГ: …» — их пишет наш
                 код; в тексте заменяются адреса, IP-адреса, имена узлов, @имена, почта, длинные числа и
                 всё, похожее на ключ или токен. Прочие строки — нет.
  state          вывод ./ops/state.sh show: заголовки, ключи состояния со значениями простого вида
                 (done@время, короткое слово), записи журнала — время и «set ключ»; остальное — длиной.
  counts         любой журнал (база, дополнения): только число строк и число строк с ERROR, FATAL,
                 PANIC, WARNING, Traceback.
  relay          журнал frpc (shturman-relay): то же, что counts, и сколько раз встретилась каждая
                 причина отказа посредника из закрытого перечня (тексты relay/relayd.py, «посредник: …»)
                 и каждый вид сбоя соединения frpc. Строки целиком — нет: в них номер подключения,
                 по которому сосед по посреднику может перехватить имя (docs/relay.md, риски 8 и 9).

Вход — stdin, выход — stdout. Ошибка разбора строки — строка пропускается.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from datetime import datetime, timedelta

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_SERVICE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d):\d\d(?:[,.]\d+)? (DEBUG|INFO|WARNING|ERROR|CRITICAL) "
                      r"([A-Za-z_][A-Za-z0-9_.]{0,60}): ")
_HERMES = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:[,.]\d+)? (DEBUG|INFO|WARNING|ERROR|CRITICAL)"
                     r"(?: \[[^\]]{0,80}\])? ([A-Za-z_][A-Za-z0-9_.]{0,80}): ")
# Имена классов исключений: заглавная буква, латиница и цифры, окончание из известного набора.
_EXC = re.compile(r"\b([A-Z][A-Za-z0-9]{1,60}(?:Error|Exception|Timeout|Refused|Unknown|Left|Locked|Rejected|"
                  r"Expired|Revoked|Unregistered|Duplicated|Invalid|Unavailable|Closed|Reset|Interrupted))\b")
_HTTP = re.compile(r"\bHTTP[ /]?(\d{3})\b")
# Отказ запуска из-за настройки (ConfigError, sys.exit): строка начинается с имени переменной окружения.
_CONFIG = re.compile(r"^(?:не заданы переменные окружения: )?((?:SHTURMAN|TELEGRAM|HERMES|MCP|POSTGRES)_[A-Z0-9_]{1,60})\b")


def service_log(lines: list[str]) -> list[str]:
    out: list[str] = []
    kinds: Counter[tuple[str, str]] = Counter()
    config: Counter[str] = Counter()
    other = 0
    for line in lines:
        m = _SERVICE.match(line)
        if not m:
            c = _CONFIG.match(line.strip())
            if c:
                config[c.group(1)] += 1
            else:
                other += line.strip() != ""
            continue
        when, level, name = m.groups()
        kinds[(level, name)] += 1
        if level in ("DEBUG", "INFO"):
            continue
        rest = line[m.end():]
        facts = sorted(set(_EXC.findall(rest))) + [f"HTTP {c}" for c in sorted(set(_HTTP.findall(rest)))]
        out.append(f"{when} {level} {name}" + (f" [{', '.join(facts)}]" if facts else ""))
    head = ["# Журнал сервиса переписки — только время, уровень, модуль и вид ошибки (текст сообщений не берётся)",
            "# Счётчики по уровню и модулю:"]
    head += [f"#   {level:<8} {name:<40} {n}" for (level, name), n in sorted(kinds.items())]
    head.append(f"#   строк другого вида (не взяты): {other}")
    if config:
        head.append("# Отказ запуска из-за настройки (только имя переменной окружения): "
                    + "; ".join(f"{name} — {n}" for name, n in sorted(config.items())))
    head.append("# Предупреждения и ошибки:")
    return head + out


def hermes_errors(lines: list[str], *, hours: int = 24) -> list[str]:
    parsed: list[tuple[datetime, str, str]] = []
    other = 0
    for line in lines:
        m = _HERMES.match(line)
        if not m:
            other += line.strip() != ""
            continue
        try:
            moment = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        parsed.append((moment, m.group(2), m.group(3)))
    out = ["# Журнал ошибок Hermes — только счётчики: Hermes пишет в журналы начало текста сообщений",
           f"# (за {hours} ч до последней записи; строк продолжения и трассировок: {other})"]
    if not parsed:
        return out + ["#   записей нет"]
    last = max(p[0] for p in parsed)
    since = last - timedelta(hours=hours)
    kinds = Counter((level, name) for moment, level, name in parsed if moment >= since)
    out.append(f"#   последняя запись: {last:%Y-%m-%d %H:%M}")
    out += [f"#   {level:<8} {name:<50} {n}" for (level, name), n in sorted(kinds.items())]
    return out


_URL = re.compile(r"\b(?:https?|wss?)://[^\s)»\"',;]+", re.I)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_AT = re.compile(r"(?<![\w/])@[A-Za-z][A-Za-z0-9_]{2,}")
_BOT_TOKEN = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b")
# Похожее на ключ: 24+ знаков, в которых есть и буква, и цифра (имена переменных и путей без цифр не трогаются).
_LONG = re.compile(r"(?<![A-Za-z0-9_+=-])(?=[A-Za-z_+=-]*\d)(?=[0-9_+=-]*[A-Za-z])[A-Za-z0-9_+=-]{24,}")
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_IPV6 = re.compile(r"\b[0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{0,4}){3,7}\b")
_TLDS = ("com", "ru", "net", "org", "app", "io", "dev", "me", "xyz", "info", "pro", "tech", "cloud", "online",
         "site", "su", "biz", "co", "uk", "de", "eu", "space", "store", "host", "cc", "tv", "ai", "рф")
_HOST = re.compile(r"\b(?:[A-Za-z0-9А-Яа-яЁё-]+\.)+(?:" + "|".join(_TLDS) + r")\b(?::\d{1,5})?", re.I)
_PHONE = re.compile(r"(?<!\w)\+\d[\d ()-]{8,}\d")
_DIGITS = re.compile(r"\b\d{7,}\b")
_RELAY_NAME = re.compile(r"(имя|имена посредника[^:]*:?) ([A-Za-z0-9-]{1,63})\b")


def mask(text: str) -> str:
    """Убирает из строки нашего кода то, что может указать на владельца: адреса, имена узлов, @имена,
    почту, номера, похожее на ключи."""
    text = _URL.sub("<адрес>", text)
    text = _EMAIL.sub("<почта>", text)
    text = _BOT_TOKEN.sub("<ключ>", text)
    text = _LONG.sub("<…>", text)
    text = _IPV4.sub("<ip>", text)
    text = _IPV6.sub("<ip>", text)
    text = _HOST.sub("<имя узла>", text)
    text = _AT.sub("@<имя>", text)
    text = _PHONE.sub("<номер>", text)
    text = _DIGITS.sub("<число>", text)
    text = _RELAY_NAME.sub(lambda m: f"{m.group(1)} <имя>", text)
    return text


_DOCTOR = re.compile(r"^(PASS|WARN|FAIL|SKIP)  ([a-z0-9][a-z0-9-]{0,40}): (.*)$")


def doctor(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        line = line.rstrip("\n")
        m = _DOCTOR.match(line)
        if m:
            out.append(f"{m.group(1)}  {m.group(2)}: {mask(m.group(3))[:600]}")
        elif re.fullmatch(r"ИТОГ: (OK|FAIL \(\d{1,3}\))", line.strip()):
            out.append(line.strip())
    return out


_STATE_KEY = re.compile(r"^([A-Za-z0-9_.-]{1,60})=(.*)$")
_STATE_VALUE = re.compile(r"^(done@[0-9TZ:.-]{10,30}|[a-z0-9_.-]{1,40})$")
_JOURNAL = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) (.*)$")


def state(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        line = line.rstrip("\n")
        if line.startswith("# "):
            out.append(mask(line)[:300])
            continue
        j = _JOURNAL.match(line)
        if j:
            text = j.group(2)
            if re.fullmatch(r"set [A-Za-z0-9_.-]{1,60}", text):
                out.append(f"{j.group(1)} {text}")
            else:
                out.append(f"{j.group(1)} <запись журнала: {len(text)} знаков>")
            continue
        k = _STATE_KEY.match(line)
        if k:
            value = k.group(2)
            out.append(f"{k.group(1)}={value}" if _STATE_VALUE.match(value) else f"{k.group(1)}=<значение скрыто>")
            continue
        if line.strip() in ("", "(пусто — развёртывание не начато)"):
            out.append(line.strip())
    return out


_COUNT_WORDS = ("ERROR", "FATAL", "PANIC", "WARNING", "Traceback")


def counts(lines: list[str]) -> list[str]:
    total = sum(1 for line in lines if line.strip())
    found = {w: sum(1 for line in lines if w in line) for w in _COUNT_WORDS}
    return [f"строк: {total}"] + [f"с «{w}»: {n}" for w, n in found.items()]


# Причины отказа посредника — дословно тексты relay/relayd.py после «посредник: » (тест сверяет перечень).
RELAY_REASONS = (
    "неизвестный экземпляр или неверный ключ",
    "экземпляр отключён держателем посредника",
    "это подключение принадлежит другому экземпляру",
    "разрешены только пересылки https и http",
    "customDomains не разрешены, только subdomain",
    "группы пересылок не разрешены",
    "этому экземпляру разрешены только его имена",
    "неверный запрос",
    "операция не поддерживается",
    "внутренняя ошибка",
)
# Виды сбоя самого frpc: подстрока его сообщения → как назвать.
RELAY_FAILURES = (
    ("login to the server failed", "вход к посреднику не удался"),
    ("start error", "пересылка не запущена"),
    ("i/o timeout", "посредник не ответил вовремя"),
    ("connection refused", "посредник отклонил соединение"),
    ("x509", "сертификат посредника не прошёл проверку"),
)


def relay(lines: list[str]) -> list[str]:
    out = counts(lines)
    reasons = Counter(r for line in lines for r in RELAY_REASONS if f"посредник: {r}" in line)
    failures = Counter(name for line in lines for key, name in RELAY_FAILURES if key in line)
    out.append("отказы посредника: " + ("; ".join(f"«{r}» — {n}" for r, n in sorted(reasons.items())) or "нет"))
    out.append("сбои соединения: " + ("; ".join(f"{r} — {n}" for r, n in sorted(failures.items())) or "нет"))
    return out


MODES = {"service-log": service_log, "hermes-errors": hermes_errors, "doctor": doctor, "state": state,
         "counts": counts, "relay": relay}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in MODES:
        print("использование: diag_filter.py " + "|".join(MODES), file=sys.stderr)
        return 2
    data = sys.stdin.buffer.read().decode("utf-8", errors="replace").splitlines()
    for line in MODES[argv[1]](data):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
