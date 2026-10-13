#!/usr/bin/env python3
"""Служба посредника «Штурмана» (docs/relay.md, решение Р-69). Работает на посреднике, не на экземпляре.

Одна программа, только стандартная библиотека Python 3.12 — запускается системным python3 Ubuntu 24.04
без установки пакетов. Что она делает:

  * **серверный плагин frps** (frp `doc/server_plugin.md`, версия 0.71.0): frps спрашивает её перед
    операциями `Login`, `NewProxy` и `NewWorkConn` и делает, как она ответит. Правила:
      - вход (`Login`) — только экземпляр, который есть в базе и не отключён, со своим ключом
        (`metadatas.key` у frpc, приходит в `metas.key`); общий токен frps при этом остаётся, но он
        у всех экземпляров один и границей доверия не служит — граница здесь, в ключе;
      - новая пересылка (`NewProxy`) — только типы `https` и `http`, без `customDomains` и групп,
        и только имена этого экземпляра: `<имя>` и `setup-<имя>`; служебному экземпляру
        посредника — только `relay`;
      - новое рабочее соединение (`NewWorkConn`) — только у экземпляра, который не отключён: так
        отключение (`revoke`) останавливает пересылку сразу, не дожидаясь переподключения;
  * **проверка имени до подключения**: `POST /api/check {code, name}` — свободно ли имя, и если нет,
    до трёх свободных вариантов от него. Нужен действующий код приглашения (неиспользованный,
    неистёкший), но проверка его не расходует: без кода посторонний не узнает, какие имена заняты.
    Неверный код — тот же отказ 403, что у `join`, без слова о самом имени;
  * **выдача имён по коду приглашения**: `POST /api/join {code, name, key_hash}`. Код одноразовый,
    его выпускает держатель посредника (`relayctl invite`); имя выбирает владелец экземпляра; ключ
    экземпляр создаёт сам и присылает только его sha256 — сам ключ посредника не проходит. Ответ —
    имена, адрес посредника, общий токен frps и сертификат frps для проверки соединения. Повтор того
    же запроса (ответ потерялся в сети) получает тот же успешный ответ;
  * **бот-менеджер и создание бота согласований в одно нажатие** (с 0.4.0, решение Р-77,
    docs/approvals-bot.md): держатель посредника заводит у @BotFather бота-менеджера (Bot
    Management Mode) и отдаёт службе его токен — страницей `GET /api/manager/setup#<секрет>` по
    одноразовой ссылке из `relayctl manager-link` или командой `relayctl manager-token` из
    стандартного ввода. Служба опрашивает бота-менеджера (`getUpdates`, только `managed_bot`,
    без webhook). Экземпляр, владелец которого выбрал «создать бота в одно нажатие», просит пару:
    `POST /api/botpair/start {name?, code?}` → `{pair_id, secret, link, username, …}`. Секрет
    (256 бит) в ссылку не входит, у посредника хранится только его sha256; имя бота
    `<префикс>_<pair_id>_bot` служит только для сопоставления. Владелец открывает ссылку
    `https://t.me/newbot/<менеджер>/<имя>?name=…` и подтверждает создание в Telegram; служба
    получает `managed_bot`, сопоставляет бота по имени, сразу закрывает его для всех, кроме
    создателя (`setManagedBotAccessSettings`), и ждёт экземпляр. `POST /api/botpair/<pair_id>/token
    {secret}` один раз отдаёт токен (служба берёт его у Telegram в момент выдачи и не хранит),
    номер бота и номер создателя; после этого пара забывается. Пары живут в памяти 15 минут;
  * **команды держателя** (`relayctl`): invite, list, revoke, release, operator, init,
    manager-link, manager-token, manager-status.

Ни ключи, ни коды, ни токены ботов, ни секреты пар не пишутся в журнал. Слушает только 127.0.0.1:
плагин спрашивает frps на этом же сервере, а `/api/*` наружу отдаёт локальный Caddy посредника
(relay/Caddyfile).

    relayd.py serve [--db …] [--domain sh.namre.app] [--server relay.sh.namre.app] [--port 7000]
                    [--ca /etc/frp/frps.crt] [--plugin 127.0.0.1:7400] [--api 127.0.0.1:7401]
                    [--botpair-prefix shturman] [--botpair-require-invite]
    relayd.py init | invite [--days 7] [--note …] | list | revoke ИМЯ [--no-restart]
            | release ИМЯ | operator --key-file ФАЙЛ
            | manager-link [--minutes 15] | manager-token [--remove] [--no-check] | manager-status
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.server
import ipaddress
import json
import logging
import os
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlsplit
import urllib.error
import urllib.request

VERSION = "0.4.0"
DEFAULT_DB = "/var/lib/shturman-relay/relay.db"
DEFAULT_DOMAIN = "sh.namre.app"
DEFAULT_CA = "/etc/frp/frps.crt"
OPERATOR_NAME = "relay"

log = logging.getLogger("relayd")

# --- имена ---------------------------------------------------------------------------------------

# 3–30 знаков: строчные латинские буквы, цифры и дефис внутри.
# Сверка — только re.fullmatch: у re.match знак $ допускает перевод строки в конце («ivan\n»).
NAME_RE = re.compile(r"[a-z0-9]([a-z0-9-]{1,28}[a-z0-9])")
# Имена, которые не выдаются никому: служебные, почтовые, похожие на чужие службы и на сам проект.
RESERVED = frozenset("""
relay www api admin administrator mail email smtp imap pop pop3 ftp sftp ssh test tests testing setup sh
status root support help info abuse postmaster hostmaster webmaster security noc dns mx ns vpn proxy
autoconfig autodiscover wpad isatap localhost local dashboard login auth oauth sso account accounts
billing pay static assets cdn docs doc blog shop store app apps dev staging stage prod demo example
git gitlab github frp frps frpc caddy acme cert certs ca shturman namre hermes telegram bot bots
""".split())
RESERVED_PATTERNS = (re.compile(r"(ns|mx|mail|www|smtp|relay|api)[0-9]*"),)


def name_problem(name: str) -> str | None:
    """Почему имя нельзя выдать (код ошибки), или None, если можно."""
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        return "bad_name"
    if name.startswith("setup-") or name[2:4] == "--":   # setup-… — имя страницы; xn-- — punycode
        return "bad_name"
    if name in RESERVED or any(p.fullmatch(name) for p in RESERVED_PATTERNS):
        return "name_reserved"
    return None


# --- коды приглашения и ключи ---------------------------------------------------------------------

# Алфавит Крокфорда: без I, L, O, U — их легко спутать. 20 знаков — 100 бит.
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LEN = 20
_CODE_FIX = str.maketrans({"O": "0", "I": "1", "L": "1"})


def new_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
    return "-".join(raw[i:i + 4] for i in range(0, CODE_LEN, 4))


def normalize_code(code: Any) -> str | None:
    if not isinstance(code, str) or len(code) > 64:
        return None
    c = re.sub(r"[\s-]", "", code).upper().translate(_CODE_FIX)
    if len(c) != CODE_LEN or any(ch not in CODE_ALPHABET for ch in c):
        return None
    return c


def code_hash(normalized: str) -> str:
    return hashlib.sha256(("shturman-relay-invite:" + normalized).encode()).hexdigest()


def new_key() -> str:
    return secrets.token_urlsafe(32)          # 32 случайных байта, 43 знака base64url


def key_hash(key: str) -> str:
    return hashlib.sha256(("shturman-relay-key:" + key).encode()).hexdigest()


# Сравнение ключа с несуществующим экземпляром идёт с этим образцом: время ответа не выдаёт,
# есть ли такое имя.
_DUMMY_HASH = key_hash("нет такого экземпляра")


def key_ok(stored_hash: str | None, key: Any) -> bool:
    if not isinstance(key, str) or not key or len(key) > 256:
        hmac.compare_digest(_DUMMY_HASH, _DUMMY_HASH)
        return False
    return hmac.compare_digest(stored_hash or _DUMMY_HASH, key_hash(key)) and stored_hash is not None


# --- база -----------------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS invites (
    id         TEXT PRIMARY KEY,           -- открытый номер приглашения для list; сам код не хранится
    code_hash  TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    used_at    INTEGER,
    used_by    TEXT,
    note       TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS instances (
    name       TEXT PRIMARY KEY,
    key_hash   TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    revoked_at INTEGER,
    operator   INTEGER NOT NULL DEFAULT 0,
    invite_id  TEXT
);
-- Какой экземпляр подключался с каким run_id frpc: frps заменяет прежнее подключение новым с тем же
-- run_id, и чужой run_id в Login здесь отклоняется. Хранится sha256 номера, не сам номер: зная чужой
-- run_id и общий токен, можно открывать рабочие соединения за чужой экземпляр (docs/relay.md, риск 8).
CREATE TABLE IF NOT EXISTS runs (
    run_id  TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    seen_at INTEGER NOT NULL
);
-- Одноразовые ссылки на страницу ввода токена бота-менеджера (relayctl manager-link). Хранится
-- sha256 секрета из ссылки, не сам секрет.
CREATE TABLE IF NOT EXISTS manager_links (
    id          TEXT PRIMARY KEY,
    secret_hash TEXT NOT NULL UNIQUE,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    used_at     INTEGER
);
-- Служебные значения без секретов: адрес посредника, под которым relayctl строит ссылки.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def open_db(path: str) -> sqlite3.Connection:
    old = os.umask(0o077)
    try:
        conn = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
    finally:
        os.umask(old)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.executescript(SCHEMA)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return conn


@dataclass
class Instance:
    name: str
    key_hash: str
    revoked: bool
    operator: bool


def get_instance(conn: sqlite3.Connection, name: Any) -> Instance | None:
    if not isinstance(name, str) or not name or len(name) > 64:
        return None
    row = conn.execute("SELECT name, key_hash, revoked_at, operator FROM instances WHERE name = ?", (name,)).fetchone()
    if row is None:
        return None
    return Instance(row["name"], row["key_hash"], row["revoked_at"] is not None, bool(row["operator"]))


# --- серверный плагин frps --------------------------------------------------------------------------

ALLOW = {"reject": False, "unchange": True}

REASON_AUTH = "посредник: неизвестный экземпляр или неверный ключ"
REASON_REVOKED = "посредник: экземпляр отключён держателем посредника"
REASON_RUN = "посредник: это подключение принадлежит другому экземпляру"
REASON_TYPE = "посредник: разрешены только пересылки https и http"
REASON_DOMAINS = "посредник: customDomains не разрешены, только subdomain"
REASON_GROUP = "посредник: группы пересылок не разрешены"
REASON_NAME = "посредник: этому экземпляру разрешены только его имена"


def reject(reason: str) -> dict:
    return {"reject": True, "reject_reason": reason}


def _auth(conn: sqlite3.Connection, user: Any, metas: Any) -> tuple[Instance | None, str | None]:
    inst = get_instance(conn, user)
    key = metas.get("key") if isinstance(metas, dict) else None
    if not key_ok(inst.key_hash if inst else None, key) or inst is None:
        return None, REASON_AUTH
    if inst.revoked:
        return None, REASON_REVOKED
    return inst, None


def _run_hash(run_id: str) -> str:
    return hashlib.sha256(("shturman-relay-run:" + run_id).encode()).hexdigest()


def _run_owner(conn: sqlite3.Connection, run_id: Any) -> str | None:
    if not isinstance(run_id, str) or not run_id:
        return None
    row = conn.execute("SELECT name FROM runs WHERE run_id = ?", (_run_hash(run_id),)).fetchone()
    return row["name"] if row else None


def _remember_run(conn: sqlite3.Connection, run_id: Any, name: str) -> None:
    if isinstance(run_id, str) and run_id and len(run_id) <= 128:
        conn.execute("INSERT INTO runs(run_id, name, seen_at) VALUES(?, ?, ?) "
                     "ON CONFLICT(run_id) DO UPDATE SET seen_at = excluded.seen_at WHERE runs.name = excluded.name",
                     (_run_hash(run_id), name, int(time.time())))


def plugin_decide(conn: sqlite3.Connection, op: str, content: Any) -> tuple[dict, str]:
    """Ответ frps на операцию и короткая строка для журнала (без ключей)."""
    if not isinstance(content, dict):
        return reject("посредник: неверный запрос"), "bad content"
    if op == "Login":
        user = content.get("user")
        inst, why = _auth(conn, user, content.get("metas"))
        if inst is None:
            return reject(why), f"Login user={_safe(user)} from={_safe(content.get('client_address'))}: {why}"
        owner = _run_owner(conn, content.get("run_id"))
        if owner is not None and owner != inst.name:
            return reject(REASON_RUN), f"Login user={inst.name}: чужой run_id ({owner})"
        _remember_run(conn, content.get("run_id"), inst.name)
        return ALLOW, f"Login user={inst.name} from={_safe(content.get('client_address'))}: принят"
    if op in ("NewProxy", "NewWorkConn"):
        u = content.get("user") if isinstance(content.get("user"), dict) else {}
        inst, why = _auth(conn, u.get("user"), u.get("metas"))
        if inst is None:
            return reject(why), f"{op} user={_safe(u.get('user'))}: {why}"
        owner = _run_owner(conn, u.get("run_id"))
        if owner is not None and owner != inst.name:
            return reject(REASON_RUN), f"{op} user={inst.name}: чужой run_id ({owner})"
        if op == "NewWorkConn":
            return ALLOW, ""
        ptype = content.get("proxy_type")
        sub = content.get("subdomain") or ""
        what = f"NewProxy user={inst.name} type={_safe(ptype)} subdomain={_safe(sub)}"
        if ptype not in ("https", "http"):
            return reject(REASON_TYPE), what + ": " + REASON_TYPE
        if content.get("custom_domains"):
            return reject(REASON_DOMAINS), what + ": " + REASON_DOMAINS
        if content.get("group") or content.get("group_key"):
            return reject(REASON_GROUP), what + ": " + REASON_GROUP
        allowed = {OPERATOR_NAME} if inst.operator else {inst.name, "setup-" + inst.name}
        if sub not in allowed:
            return reject(REASON_NAME), what + ": " + REASON_NAME
        _remember_run(conn, u.get("run_id"), inst.name)
        return ALLOW, what + ": принята"
    # Остальные операции плагину не назначаются (relay/frps.toml.example); если назначат — отказ.
    return reject("посредник: операция не поддерживается"), f"{_safe(op)}: не поддерживается"


def _safe(v: Any) -> str:
    return re.sub(r"[^A-Za-z0-9._:\[\]-]", "?", str(v if v is not None else "-"))[:80]


# --- выдача имён ------------------------------------------------------------------------------------

@dataclass
class Settings:
    db: str = DEFAULT_DB
    domain: str = DEFAULT_DOMAIN
    server: str = ""
    port: int = 7000
    ca_file: str = DEFAULT_CA
    frp_token: str = ""
    join_per_ip: int = 10            # попыток подключения с одного адреса (IPv6 — с сети /64) за окно
    join_window: int = 15 * 60       # секунд
    join_fail_total: int = 60        # неудачных попыток со всех адресов вместе за короткое окно
    join_fail_window: int = 60       # секунд: общий предел не держит удачные подключения дольше минуты
    # Проверки имени — свой счёт на адрес: агент владельца проверяет несколько имён подряд, и это не
    # должно съедать попытки подключения. Неверный код в проверке идёт в общий предел неудачных.
    check_per_ip: int = 30
    check_window: int = 15 * 60
    # Бот-менеджер (Р-77). Файл токена; пусто — manager.token рядом с базой (каталог состояния службы).
    manager_file: str = ""
    tg_api: str = "https://api.telegram.org"   # другой адрес — только для стенда и тестов
    manager_poll: int = 25                     # секунд держится один запрос getUpdates
    manager_link_ttl: int = 15 * 60
    botpair_prefix: str = "shturman"
    botpair_ttl: int = 15 * 60                 # секунд живёт пара, пока бот не создан
    botpair_grace: int = 3 * 60                # и не меньше стольких — после создания бота
    botpair_max_pending: int = 200
    botpair_start_per_ip: int = 10
    botpair_start_window: int = 15 * 60
    # Экземпляр спрашивает токен раз в 3 секунды: 300 запросов за 15 минут — с запасом.
    botpair_poll_per_ip: int = 600
    botpair_poll_window: int = 15 * 60
    botpair_require_invite: bool = False

    @property
    def server_name(self) -> str:
        return self.server or f"{OPERATOR_NAME}.{self.domain}"

    @property
    def manager_path(self) -> str:
        return self.manager_file or os.path.join(os.path.dirname(os.path.abspath(self.db)), "manager.token")


class JoinError(Exception):
    def __init__(self, status: int, code: str, message: str, extra: dict | None = None):
        super().__init__(code)
        self.status, self.code, self.message, self.extra = status, code, message, extra or {}


MESSAGES = {
    "bad_name": "Имя не подходит: 3–30 знаков, строчные латинские буквы, цифры и дефис внутри; "
                "не начинается с setup-.",
    "name_reserved": "Это имя занято под служебное — выберите другое.",
    "name_taken": "Это имя уже занято — выберите другое. Код приглашения не израсходован.",
    "taken": "Это имя уже занято — выберите другое.",
    "bad_code": "Код приглашения не подошёл: неверный, уже использован или истёк. Новый код выдаёт держатель посредника.",
    "bad_key_hash": "Нужен key_hash — sha256 ключа экземпляра (64 шестнадцатеричных знака); ./ops/relay.sh делает это сам.",
}

KEY_HASH_RE = re.compile(r"[0-9a-f]{64}")

SUGGEST_MAX = 3


def _suggest_base(name: Any) -> str:
    """Основа для вариантов: имя, приведённое к правилам — строчные буквы, дефис вместо прочих знаков,
    без приставки setup-; не длиннее 26 знаков (место под «-xx» или цифру)."""
    if not isinstance(name, str):
        return ""
    b = re.sub(r"[^a-z0-9]+", "-", name[:64].strip().lower()).strip("-")
    while b.startswith("setup-"):
        b = b[len("setup-"):]
    if b == "setup":
        return ""
    return b[:26].strip("-")


def suggest(conn: sqlite3.Connection, name: Any, limit: int = SUGGEST_MAX) -> list[str]:
    """До limit свободных имён, похожих на name: само имя по правилам (если прислано, например,
    с заглавными), затем с цифрой 2–9, затем с двумя случайными буквами через дефис. Только имена,
    которые прошли бы join: неподходящие, служебные и занятые отсеиваются."""
    base = _suggest_base(name)
    if not base:
        return []
    letters = "abcdefghijkmnpqrstuvwxyz"
    cands = [base] + [f"{base}{n}" for n in range(2, 10)]
    cands += [f"{base}-{secrets.choice(letters)}{secrets.choice(letters)}" for _ in range(8)]
    fit: list[str] = []
    for c in cands:
        if c != name and c not in fit and name_problem(c) is None:
            fit.append(c)
    if not fit:
        return []
    marks = ",".join("?" * len(fit))
    taken = {r["name"] for r in conn.execute(f"SELECT name FROM instances WHERE name IN ({marks})", fit)}
    return [c for c in fit if c not in taken][:limit]


def _valid_invite(conn: sqlite3.Connection, code: Any, now: int) -> bool:
    norm = normalize_code(code)
    if norm is None:
        return False
    inv = conn.execute("SELECT expires_at, used_at FROM invites WHERE code_hash = ?", (code_hash(norm),)).fetchone()
    return inv is not None and inv["used_at"] is None and inv["expires_at"] > now


def check(conn: sqlite3.Connection, s: Settings, code: Any, name: Any, now: int | None = None) -> dict:
    """Свободно ли имя — до join и без расхода кода. Сначала код: неверный, использованный или
    истёкший — тот же отказ, что у join, и о самом имени ни слова (иначе по этому адресу можно было бы
    перебирать занятые имена без приглашения). С действующим кодом ответ — available и, если имя
    не подходит, причина и до трёх свободных вариантов. Свободное сейчас имя может занять другой
    владелец до join — тогда join ответит name_taken, и код останется."""
    now = int(time.time()) if now is None else now
    if not _valid_invite(conn, code, now):
        raise JoinError(403, "bad_code", MESSAGES["bad_code"])
    problem = name_problem(name)
    if problem == "bad_name":
        reason = "invalid"
    elif problem == "name_reserved":
        reason = "reserved"
    elif conn.execute("SELECT 1 FROM instances WHERE name = ?", (name,)).fetchone():
        reason = "taken"
    else:
        return {"name": name, "domain": s.domain, "available": True, "reason": None,
                "message": "Имя свободно.", "suggestions": []}
    message = {"invalid": MESSAGES["bad_name"], "reserved": MESSAGES["name_reserved"], "taken": MESSAGES["taken"]}[reason]
    return {"name": name if isinstance(name, str) and len(name) <= 64 else None, "domain": s.domain,
            "available": False, "reason": reason, "message": message, "suggestions": suggest(conn, name)}


def join(conn: sqlite3.Connection, s: Settings, code: Any, name: Any, khash: Any, now: int | None = None) -> dict:
    """Израсходовать код и завести экземпляр с ключом, который экземпляр создал сам: сюда приходит
    только sha256 ключа (key_hash), сам ключ посредник не видит и не отдаёт. Всё — одной транзакцией:
    код тратится, только если экземпляр заведён. Повтор того же запроса (тот же код, то же имя, тот же
    key_hash) — например, ответ потерялся в сети — получает тот же успешный ответ."""
    now = int(time.time()) if now is None else now
    problem = name_problem(name)
    if problem:
        raise JoinError(400, problem, MESSAGES[problem])
    norm = normalize_code(code)
    if norm is None:
        raise JoinError(403, "bad_code", MESSAGES["bad_code"])
    if not isinstance(khash, str) or not KEY_HASH_RE.fullmatch(khash):
        raise JoinError(400, "bad_key_hash", MESSAGES["bad_key_hash"])
    with open(s.ca_file, encoding="ascii") as f:      # до транзакции: без сертификата код не тратится
        ca = f.read()
    answer = {
        "name": name,
        "domain": s.domain,
        "dashboard_url": f"https://{name}.{s.domain}",
        "setup_url": f"https://setup-{name}.{s.domain}",
        "server": s.server_name,
        "port": s.port,
        "frp_token": s.frp_token,
        "ca": ca,
    }
    conn.execute("BEGIN IMMEDIATE")
    try:
        inv = conn.execute("SELECT id, expires_at, used_at, used_by FROM invites WHERE code_hash = ?",
                           (code_hash(norm),)).fetchone()
        if inv is not None and inv["used_at"] is not None:
            row = conn.execute("SELECT key_hash FROM instances WHERE name = ? AND invite_id = ? AND revoked_at IS NULL",
                               (name, inv["id"])).fetchone()
            if inv["used_by"] == name and row is not None and hmac.compare_digest(row["key_hash"], khash):
                conn.execute("COMMIT")
                return dict(answer, repeated=True)
            raise JoinError(403, "bad_code", MESSAGES["bad_code"])
        if inv is None or inv["expires_at"] <= now:
            raise JoinError(403, "bad_code", MESSAGES["bad_code"])
        # Имя занято, в том числе отключённым экземпляром: освобождает его только release.
        if conn.execute("SELECT 1 FROM instances WHERE name = ?", (name,)).fetchone():
            # Код здесь уже проверен: варианты получает только держатель действующего приглашения.
            raise JoinError(409, "name_taken", MESSAGES["name_taken"], {"suggestions": suggest(conn, name)})
        cur = conn.execute("UPDATE invites SET used_at = ?, used_by = ? WHERE id = ? AND used_at IS NULL",
                           (now, name, inv["id"]))
        if cur.rowcount != 1:
            raise JoinError(403, "bad_code", MESSAGES["bad_code"])
        conn.execute("INSERT INTO instances(name, key_hash, created_at, invite_id) VALUES(?, ?, ?, ?)",
                     (name, khash, now, inv["id"]))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return answer


def ip_bucket(ip: str) -> str:
    """Ключ счёта попыток: IPv4 — адрес, IPv6 — сеть /64 (у одного клиента обычно вся /64)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if addr.version == 6 and addr.ipv4_mapped is not None:
        return str(addr.ipv4_mapped)
    if addr.version == 6:
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


class Window:
    """Скользящее окно в памяти, счёт по ключу. После перезапуска счёт начинается заново."""

    def __init__(self, limit: int, window: int):
        self.limit, self.window = limit, window
        self.hits: dict[str, deque] = {}
        self.lock = threading.Lock()

    def _prune(self, now: float) -> None:
        edge = now - self.window
        for k in list(self.hits):
            q = self.hits[k]
            while q and q[0] <= edge:
                q.popleft()
            if not q:
                del self.hits[k]

    def blocked(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self.lock:
            self._prune(now)
            return len(self.hits.get(key, ())) >= self.limit

    def add(self, key: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self.lock:
            self.hits.setdefault(key, deque()).append(now)


# --- Bot API Telegram --------------------------------------------------------------------------------
# Сверено с core.telegram.org/bots/api 2026-10-13: getManagedBotToken(user_id) → String,
# setManagedBotAccessSettings(user_id, is_access_restricted, added_user_ids?) → True,
# Update.managed_bot: ManagedBotUpdated {user: User — создатель, bot: User — бот}, User.can_manage_bots
# (только в getMe). Токен — часть адреса запроса; ни адрес, ни тело ответа в журнал не попадают.

BOT_TOKEN_RE = re.compile(r"[0-9]{5,16}:[A-Za-z0-9_-]{30,80}")


class TelegramError(Exception):
    """Отказ или недоступность Telegram. В тексте — только код, без токена и без адреса."""

    def __init__(self, code: str, status: int = 0, retry_after: int | None = None):
        super().__init__(code)
        self.code, self.status, self.retry_after = code, status, retry_after


def _opener(base: str) -> urllib.request.OpenerDirector:
    # На стенде Telegram подставной на 127.0.0.1: системный прокси к нему не нужен.
    host = urlsplit(base).hostname or ""
    if host in ("127.0.0.1", "::1", "localhost"):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def tg_call(base: str, token: str, method: str, params: dict | None = None, timeout: float = 15) -> Any:
    """Один вызов Bot API. Возвращает поле result или бросает TelegramError."""
    req = urllib.request.Request(f"{base}/bot{token}/{method}", data=json.dumps(params or {}).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    try:
        with _opener(base).open(req, timeout=timeout) as r:
            raw = r.read(2_000_000)
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read(65536) or b"{}")
        except ValueError:
            body = {}
        params_ = body.get("parameters") if isinstance(body, dict) else None
        retry = params_.get("retry_after") if isinstance(params_, dict) else None
        raise TelegramError("refused", e.code, retry if isinstance(retry, int) else None) from None
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        raise TelegramError("no_connection") from None
    try:
        body = json.loads(raw)
    except ValueError:
        raise TelegramError("bad_answer") from None
    if not isinstance(body, dict) or body.get("ok") is not True:
        raise TelegramError("refused", int(body.get("error_code") or 0) if isinstance(body, dict) else 0)
    return body.get("result")


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _display(user: Any) -> str:
    """Имя пользователя Telegram одной строкой, без управляющих знаков, не длиннее 64 знаков."""
    if not isinstance(user, dict):
        return ""
    parts = [user.get(k) for k in ("first_name", "last_name")]
    text = " ".join(p.strip() for p in parts if isinstance(p, str) and p.strip())
    return "".join(ch for ch in text if ch.isprintable())[:64]


# --- бот-менеджер и пары «бот согласований в одно нажатие» (Р-77) -------------------------------------

PAIR_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"     # без похожих знаков: i, l, o, 0, 1
PAIR_LEN = 10
PAIR_RE = re.compile(f"[{PAIR_ALPHABET}]{{{PAIR_LEN}}}")
PREFIX_RE = re.compile(r"[a-z][a-z0-9]{2,15}")
DEFAULT_BOT_NAME = "Согласования"
SECRET_RE = re.compile(r"[A-Za-z0-9_-]{40,64}")


def secret_hash(secret: str, kind: str = "botpair") -> str:
    return hashlib.sha256(f"shturman-relay-{kind}:{secret}".encode()).hexdigest()


_DUMMY_SECRET = secret_hash("нет такой пары")


def bot_display_name(name: Any) -> str:
    """Название нового бота: 1–64 знака, без управляющих; не задано — «Согласования»."""
    if name is None or (isinstance(name, str) and not name.strip()):
        return DEFAULT_BOT_NAME
    if not isinstance(name, str) or len(name) > 256:
        raise PairError(400, "bad_name", "Название бота — строка до 64 знаков.")
    clean = " ".join(name.split())
    if not clean or len(clean) > 64 or any(not ch.isprintable() for ch in clean):
        raise PairError(400, "bad_name", "Название бота — строка до 64 знаков.")
    return clean


class PairError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(code)
        self.status, self.code, self.message = status, code, message


@dataclass
class Pair:
    """Пара «экземпляр — будущий бот». Живёт только в памяти службы; секрета в ней нет, только его хеш."""
    pair_id: str
    secret_hash: str = field(default="", repr=False)
    username: str = ""
    name: str = ""
    expires: float = 0.0
    bot_id: int | None = None
    bot_username: str = ""
    bot_name: str = ""
    creator_id: int | None = None
    creator_name: str = ""
    restricted: bool | None = None
    conflict: bool = False


def read_manager_token(path: str) -> str | None:
    """Токен бота-менеджера из файла `MANAGER_BOT_TOKEN=…`. Нет файла или строки — None."""
    try:
        with open(path, encoding="ascii") as f:
            for line in f:
                if line.startswith("MANAGER_BOT_TOKEN="):
                    value = line.split("=", 1)[1].strip()
                    return value if BOT_TOKEN_RE.fullmatch(value) else None
    except (OSError, UnicodeDecodeError):
        return None
    return None


def write_manager_token(path: str, token: str | None) -> None:
    """Пишет файл токена так, что он ни на миг не доступен другим (600, замена целиком). None — удалить."""
    if token is None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return
    if not BOT_TOKEN_RE.fullmatch(token):
        raise ValueError("bad token")
    tmp = f"{path}.{secrets.token_hex(6)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(f"MANAGER_BOT_TOKEN={token}\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)


class Manager:
    """Бот-менеджер: его токен, опрос обновлений `managed_bot` и пары, которые ждут бота.

    Токен читается из файла (`Settings.manager_path`) и перечитывается, когда файл меняется: так
    его подхватывает работающая служба после `relayctl manager-token` или страницы ввода. Пары —
    только в памяти: после перезапуска службы начатое создание бота нужно начать заново."""

    def __init__(self, s: Settings, *, clock: Callable[[], float] = time.time,
                 tg: Callable[..., Any] = tg_call) -> None:
        self.s, self.clock, self._tg = s, clock, tg
        self.lock = threading.Lock()
        self.pairs: dict[str, Pair] = {}
        self._token: str | None = None
        self._stamp: tuple | None = None
        self.username: str | None = None
        self.ready = False
        self.problem: str | None = "no_token"
        self.offset: int | None = None
        self.counters = {"created": 0, "delivered": 0, "unmatched": 0, "conflicts": 0}

    def __repr__(self) -> str:
        return f"<Manager @{self.username or '-'} ready={self.ready} pairs={len(self.pairs)}>"

    def tg(self, method: str, params: dict | None = None, timeout: float = 15) -> Any:
        if not self._token:
            raise TelegramError("no_token")
        return self._tg(self.s.tg_api, self._token, method, params, timeout)

    # --- токен ---

    def reload(self, force: bool = False) -> bool:
        """Перечитывает файл токена, если он изменился. True — токен сменился (или исчез)."""
        path = self.s.manager_path
        try:
            st = os.stat(path)
            stamp: tuple | None = (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            stamp = None
        if stamp == self._stamp and not force:
            return False
        self._stamp = stamp
        token = read_manager_token(path) if stamp else None
        if token == self._token and not force:
            return False
        self._token, self.ready, self.username, self.offset = token, False, None, None
        self.problem = None if token else "no_token"
        if token is None:
            log.info("бот-менеджер: токена нет — создание бота в одно нажатие выключено")
        return True

    def check_identity(self) -> None:
        me = self.tg("getMe")
        if not isinstance(me, dict) or me.get("is_bot") is not True or not isinstance(me.get("username"), str):
            self.ready, self.problem = False, "bad_get_me"
            return
        self.username = me["username"]
        if me.get("can_manage_bots") is not True:
            self.ready, self.problem = False, "not_manager"
            log.warning("бот-менеджер @%s: у бота не включён Bot Management Mode (can_manage_bots) — "
                        "включите его в мини-приложении @BotFather", _safe(self.username))
            return
        self.ready, self.problem = True, None
        log.info("бот-менеджер @%s готов", _safe(self.username))

    # --- обновления ---

    def poll_once(self, timeout: int | None = None) -> int:
        wait = self.s.manager_poll if timeout is None else timeout
        params: dict[str, Any] = {"timeout": wait, "allowed_updates": ["managed_bot"]}
        if self.offset is not None:
            params["offset"] = self.offset
        updates = self.tg("getUpdates", params, timeout=wait + 10)
        if not isinstance(updates, list):
            raise TelegramError("bad_answer")
        for upd in updates:
            uid = _int(upd.get("update_id")) if isinstance(upd, dict) else None
            if uid is None:
                continue
            try:
                self.handle_update(upd)
            except Exception:      # одно обновление не должно остановить остальные
                log.exception("бот-менеджер: сбой при разборе обновления")
            self.offset = uid + 1
        return len(updates)

    def _prune(self) -> None:
        now = self.clock()
        for pid in [pid for pid, p in self.pairs.items() if p.expires <= now]:
            del self.pairs[pid]

    def handle_update(self, upd: dict) -> None:
        """`managed_bot`: создан бот под этим менеджером, сменился его токен или владелец.
        Сопоставляется с парой по имени бота; чужие и незнакомые боты не трогаются."""
        mb = upd.get("managed_bot")
        if not isinstance(mb, dict):
            return
        bot, user = mb.get("bot"), mb.get("user")
        bot_id = _int(bot.get("id")) if isinstance(bot, dict) else None
        uname = bot.get("username") if isinstance(bot, dict) else None
        creator = _int(user.get("id")) if isinstance(user, dict) else None
        if bot_id is None or creator is None or not isinstance(uname, str):
            return
        restrict = None
        with self.lock:
            self._prune()
            pair = next((p for p in self.pairs.values() if p.bot_id == bot_id), None) or \
                next((p for p in self.pairs.values() if p.username == uname.lower()), None)
            if pair is None:
                self.counters["unmatched"] += 1
                log.info("managed_bot: бот без ожидающей пары (имя изменили при создании, пара истекла "
                         "или ссылку открыли не с экземпляра) — пропущен")
                return
            if pair.bot_id is None and pair.username == uname.lower():
                pair.bot_id, pair.bot_username, pair.bot_name = bot_id, uname, _display(bot)
                pair.creator_id, pair.creator_name = creator, _display(user)
                pair.expires = max(pair.expires, self.clock() + self.s.botpair_grace)
                self.counters["created"] += 1
                restrict = pair
                log.info("managed_bot: бот для пары %s создан", pair.pair_id)
            elif pair.bot_id != bot_id or pair.creator_id != creator:
                # Владелец бота сменился, пока пара ждёт экземпляр, — отдавать токен некому.
                pair.conflict = True
                self.counters["conflicts"] += 1
                log.warning("managed_bot: у бота пары %s сменился владелец — пара не будет выдана", pair.pair_id)
        if restrict is not None:
            self._restrict(restrict)

    def _restrict(self, pair: Pair) -> None:
        """Доступ к новому боту — только у его владельца (создателя). Не вышло — бот всё равно
        выдаётся, экземпляр узнает об этом (`restricted: false`) и скажет владельцу."""
        try:
            done = self.tg("setManagedBotAccessSettings", {"user_id": pair.bot_id, "is_access_restricted": True})
            ok = done is True
        except TelegramError as e:
            log.warning("managed_bot: доступ к боту пары %s не закрыт (%s %s)", pair.pair_id, e.code, e.status)
            ok = False
        with self.lock:
            pair.restricted = ok

    def run(self, stop: threading.Event) -> None:
        fails = 0
        while not stop.is_set():
            try:
                self.reload()
                if not self._token:
                    stop.wait(5)
                    continue
                if not self.ready:
                    self.check_identity()
                    if not self.ready:
                        stop.wait(60)
                        continue
                self.poll_once()
                fails = 0
                continue
            except TelegramError as e:
                fails += 1
                if e.status in (401, 404):
                    self.ready, self.problem = False, "token_rejected"
                elif e.status == 409:
                    self.problem = "conflict"        # бота опрашивает кто-то ещё или у него webhook
                else:
                    self.problem = e.code if not e.status else f"refused_{e.status}"
                if fails == 1 or fails % 20 == 0:
                    log.warning("бот-менеджер: опрос приостановлен (%s)", self.problem)
                pause = e.retry_after + 1 if e.retry_after else min(60, 2 ** min(fails, 6))
            except Exception:
                fails += 1
                self.problem = "internal"
                log.exception("бот-менеджер: сбой опроса")
                pause = 10
            stop.wait(pause)

    # --- пары ---

    def start(self, name: Any) -> dict:
        display = bot_display_name(name)
        if not self.ready or not self.username:
            raise PairError(503, "manager_unavailable",
                            "Создание бота в одно нажатие сейчас недоступно. Создайте бота у @BotFather вручную.")
        secret = secrets.token_urlsafe(32)
        with self.lock:
            self._prune()
            if len(self.pairs) >= self.s.botpair_max_pending:
                raise PairError(503, "busy", "Посредник занят. Повторите через несколько минут.")
            while True:
                pair_id = "".join(secrets.choice(PAIR_ALPHABET) for _ in range(PAIR_LEN))
                if pair_id not in self.pairs:
                    break
            username = f"{self.s.botpair_prefix}_{pair_id}_bot"
            expires = self.clock() + self.s.botpair_ttl
            self.pairs[pair_id] = Pair(pair_id, secret_hash(secret), username, display, expires)
        link = f"https://t.me/newbot/{self.username}/{username}?name={quote(display, safe='')}"
        return {"pair_id": pair_id, "secret": secret, "link": link, "username": username, "name": display,
                "manager": self.username, "expires_at": int(expires), "ttl": self.s.botpair_ttl}

    def fetch(self, pair_id: Any, secret: Any) -> tuple[int, dict, bool]:
        """(код ответа, тело, неудача — для общего предела). Токен берётся у Telegram сейчас и
        отдаётся один раз; пара после этого забывается."""
        not_found = (404, {"error": "not_found", "message": "Такой пары нет: срок истёк или бот уже выдан."}, True)
        good = isinstance(pair_id, str) and PAIR_RE.fullmatch(pair_id) is not None \
            and isinstance(secret, str) and SECRET_RE.fullmatch(secret) is not None
        with self.lock:
            self._prune()
            pair = self.pairs.get(pair_id) if good else None
            ok = hmac.compare_digest(pair.secret_hash if pair else _DUMMY_SECRET,
                                     secret_hash(secret if isinstance(secret, str) else ""))
            if pair is None or not ok:
                return not_found
            if pair.conflict:
                del self.pairs[pair_id]
                return 409, {"error": "conflict", "message": "Владелец созданного бота сменился. Начните заново."}, False
            if pair.bot_id is None:
                return 202, {"status": "waiting", "expires_at": int(pair.expires)}, False
            bot_id = pair.bot_id
        try:
            token = self.tg("getManagedBotToken", {"user_id": bot_id})
        except TelegramError as e:
            log.warning("botpair %s: токен у Telegram не получен (%s %s)", pair_id, e.code, e.status)
            return 502, {"error": "telegram", "message": "Telegram не ответил. Повторите."}, False
        if not isinstance(token, str) or not BOT_TOKEN_RE.fullmatch(token):
            return 502, {"error": "telegram", "message": "Telegram не ответил. Повторите."}, False
        with self.lock:
            if self.pairs.get(pair_id) is not pair:
                return not_found
            del self.pairs[pair_id]
            self.counters["delivered"] += 1
        log.info("botpair %s: бот выдан экземпляру", pair_id)
        return 200, {"status": "ready", "token": token,
                     "bot": {"id": bot_id, "username": pair.bot_username, "name": pair.bot_name},
                     "creator": {"id": pair.creator_id, "name": pair.creator_name},
                     "restricted": pair.restricted is True}, False


def manager_setup(conn: sqlite3.Connection, s: Settings, manager: Manager, secret: Any, token: Any,
                  now: int | None = None, tg: Callable[..., Any] = tg_call) -> dict:
    """Страница ввода токена бота-менеджера: проверить одноразовую ссылку, спросить у Telegram,
    что это за бот и включён ли у него Bot Management Mode, и сохранить токен в файл службы.
    Ссылка тратится только при успехе. Токен в ответ и в журнал не попадает."""
    now = int(time.time()) if now is None else now
    bad_link = JoinError(403, "bad_link", "Ссылка не подошла: неверная, уже использована или истекла. "
                                          "Новую выдаёт relayctl manager-link.")
    if not isinstance(secret, str) or not SECRET_RE.fullmatch(secret):
        raise bad_link
    row = conn.execute("SELECT id FROM manager_links WHERE secret_hash = ? AND used_at IS NULL AND expires_at > ?",
                       (secret_hash(secret, "manager-link"), now)).fetchone()
    if row is None:
        raise bad_link
    if not isinstance(token, str) or not BOT_TOKEN_RE.fullmatch(token.strip()):
        raise JoinError(400, "bad_token", "Это не похоже на токен бота: длинная строка с двоеточием из ответа @BotFather.")
    token = token.strip()
    try:
        me = tg(s.tg_api, token, "getMe")
    except TelegramError as e:
        if e.status in (401, 404):
            raise JoinError(400, "token_rejected", "Telegram не принял этот токен. Скопируйте его из @BotFather ещё раз.") from None
        raise JoinError(502, "no_connection", "Не удалось связаться с Telegram. Повторите.") from None
    if not isinstance(me, dict) or me.get("is_bot") is not True:
        raise JoinError(400, "bad_token", "Telegram не узнал в этом токене бота.")
    if me.get("can_manage_bots") is not True:
        raise JoinError(422, "not_manager", "У этого бота не включён Bot Management Mode. Откройте мини-приложение "
                                            "@BotFather, выберите бота, включите Bot Management Mode и повторите — "
                                            "ссылка ещё действует.")
    cur = conn.execute("UPDATE manager_links SET used_at = ? WHERE id = ? AND used_at IS NULL", (now, row["id"]))
    if cur.rowcount != 1:
        raise bad_link
    write_manager_token(s.manager_path, token)
    manager.reload(force=True)
    return {"ok": True, "username": me.get("username")}


MANAGER_PAGE_CSS = """
body{font:16px/1.5 system-ui,sans-serif;max-width:36rem;margin:2rem auto;padding:0 1rem;color:#1b1b1b;background:#fff}
input{width:100%;box-sizing:border-box;font:inherit;padding:.5rem}button{font:inherit;padding:.5rem 1rem;margin-top:.75rem}
.warn{color:#8a1c1c}.ok{color:#1c5e20}@media (prefers-color-scheme:dark){body{color:#eee;background:#161616}}
""".strip()
MANAGER_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow"><meta name="referrer" content="no-referrer">
<title>Бот-менеджер посредника</title><style>%s</style></head><body>
<h1>Бот-менеджер посредника «Штурмана»</h1>
<p>Здесь держатель посредника вводит токен бота-менеджера — бота, через которого владельцы экземпляров
создают своего бота согласований в одно нажатие (docs/approvals-bot.md).</p>
<ol><li>У @BotFather создайте бота (/newbot) или выберите своего.</li>
<li>В мини-приложении @BotFather откройте настройки этого бота и включите <b>Bot Management Mode</b>.</li>
<li>Скопируйте токен бота из @BotFather и вставьте сюда.</li></ol>
<p class="warn">С этим токеном посредник сможет получать токены всех ботов, созданных через него.
Ссылка на эту страницу одноразовая; никому её не пересылайте.</p>
<form id="f" autocomplete="off"><label for="t">Токен бота-менеджера</label>
<input id="t" type="password" autocomplete="off" spellcheck="false" autocapitalize="off">
<button type="submit">Проверить и сохранить</button></form>
<p id="m" role="status"></p>
<script src="/api/manager/setup.js"></script></body></html>
""" % MANAGER_PAGE_CSS
MANAGER_JS = """(function () {
  "use strict";
  var secret = location.hash.slice(1);
  try { history.replaceState(null, "", location.pathname); } catch (e) {}
  var m = document.getElementById("m"), f = document.getElementById("f"), t = document.getElementById("t");
  function say(kind, text) { m.className = kind; m.textContent = text; }
  if (!secret) { say("warn", "В адресе нет секрета ссылки. Получите новую ссылку: sudo relayctl manager-link"); f.hidden = true; return; }
  f.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var token = t.value.trim(); t.value = "";
    if (!token) { say("warn", "Вставьте токен."); return; }
    say("", "Проверяю у Telegram…");
    fetch("/api/manager/setup", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ secret: secret, token: token }), credentials: "omit", cache: "no-store" })
      .then(function (r) { return r.json().then(function (d) { return [r.status, d]; }); })
      .then(function (x) {
        if (x[0] === 200) { secret = ""; f.hidden = true; say("ok", "Сохранено: бот-менеджер @" + x[1].username + ". Страницу можно закрыть."); }
        else say("warn", (x[1] && x[1].message) || "Не получилось.");
      }, function () { say("warn", "Нет связи с посредником. Повторите."); });
  });
})();
"""


def _csp() -> str:
    import base64
    digest = base64.b64encode(hashlib.sha256(MANAGER_PAGE_CSS.encode()).digest()).decode()
    return ("default-src 'none'; script-src 'self'; style-src 'sha256-%s'; connect-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'" % digest)


MANAGER_CSP = _csp()


# --- HTTP -----------------------------------------------------------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "shturman-relayd"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:   # журнал запросов не ведётся: в нём были бы адреса
        pass

    def _send(self, status: int, body: bytes, ctype: str = "application/json; charset=utf-8",
              extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj: Any, extra: dict | None = None) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode(), extra=extra)

    def _body(self, limit: int) -> bytes | None:
        """Тело запроса — читается всегда, до любого ответа: иначе непрочитанный остаток на
        постоянном соединении с Caddy был бы принят за следующий запрос. Слишком большое или без
        длины — None, и соединение после ответа закрывается."""
        try:
            n = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            n = -1
        if n < 0 or n > limit or self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            return None
        return self.rfile.read(n)


class PluginHandler(_Handler):
    """POST /frps/handler?op=… — запросы frps (только с этого сервера)."""

    def do_POST(self) -> None:
        url = urlsplit(self.path)
        raw = self._body(65536)
        if url.path != self.server.plugin_path:
            self._send(404, b"{}")
            return
        try:
            req = json.loads(raw or b"")
        except ValueError:
            req = None
        if not isinstance(req, dict):
            self._json(200, reject("посредник: неверный запрос"))
            return
        op = req.get("op") or (parse_qs(url.query).get("op") or [""])[0]
        try:
            with self.server.db_lock:
                answer, note = plugin_decide(self.server.conn, str(op), req.get("content"))
        except Exception:
            log.exception("plugin: ошибка при разборе %s", _safe(op))
            answer, note = reject("посредник: внутренняя ошибка"), ""
        if note:
            log.info("plugin %s", note)
        self._json(200, answer)

    def do_GET(self) -> None:
        self._send(404, b"{}")


class ApiHandler(_Handler):
    """/api/health, /api/ca, /api/check, /api/join — за локальным Caddy посредника."""

    def client_ip(self) -> str:
        peer = self.client_address[0]
        # Запрос пришёл от локального Caddy: адрес клиента он вписал в X-Forwarded-For последним
        # (Caddy получает настоящий адрес по протоколу PROXY от frpc посредника).
        if peer in ("127.0.0.1", "::1"):
            xff = self.headers.get("X-Forwarded-For") or ""
            last = xff.split(",")[-1].strip()
            if last and re.fullmatch(r"[0-9A-Fa-f.:]{2,45}", last):
                return last
        return peer

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        page_headers = {"Content-Security-Policy": MANAGER_CSP, "X-Frame-Options": "DENY",
                        "Referrer-Policy": "no-referrer"}
        if path == "/api/health":
            # botpair — готов ли бот-менеджер: по этому признаку экземпляр предлагает создание в одно нажатие.
            self._json(200, {"ok": True, "version": VERSION, "botpair": bool(self.server.manager.ready)})
        elif path == "/api/manager/setup":
            self._send(200, MANAGER_PAGE.encode(), "text/html; charset=utf-8", extra=page_headers)
        elif path == "/api/manager/setup.js":
            self._send(200, MANAGER_JS.encode(), "text/javascript; charset=utf-8", extra=page_headers)
        elif path == "/api/ca":
            try:
                with open(self.server.settings.ca_file, "rb") as f:
                    self._send(200, f.read(), "application/x-pem-file")
            except OSError:
                self._json(503, {"error": "no_ca", "message": "сертификат посредника недоступен"})
        else:
            self._json(404, {"error": "not_found"})

    def _limited(self, what: str, per_ip: Window, window: int) -> str | None:
        """Предел на адрес и общий предел неудачных попыток. Превышен — ответ 429 уже отправлен, None."""
        ip = self.client_ip()
        bucket = ip_bucket(ip)
        if per_ip.blocked(bucket):
            log.info("%s from=%s: слишком часто с этого адреса", what, _safe(ip))
            self._json(429, {"error": "rate_limited", "message": "Слишком много попыток с этого адреса. Повторите через 15 минут."},
                       extra={"Retry-After": str(window)})
            return None
        if self.server.failures.blocked("*"):
            log.info("%s from=%s: слишком много неудачных попыток со всех адресов", what, _safe(ip))
            self._json(429, {"error": "rate_limited", "message": "Посредник занят. Повторите через минуту."},
                       extra={"Retry-After": str(self.server.settings.join_fail_window)})
            return None
        per_ip.add(bucket)
        return ip

    def _json_body(self, raw: bytes | None) -> dict | None:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        try:
            req = json.loads(raw) if raw is not None and ctype == "application/json" else None
        except ValueError:
            req = None
        return req if isinstance(req, dict) else None

    def _botpair(self, path: str, raw: bytes | None) -> None:
        """POST /api/botpair/start и /api/botpair/<pair_id>/token (Р-77)."""
        st, manager = self.server.settings, self.server.manager
        start = path == "/api/botpair/start"
        m = re.fullmatch(r"/api/botpair/([a-z0-9]{1,32})/token", path)
        if not start and m is None:
            self._json(404, {"error": "not_found"})
            return
        if start:
            ip = self._limited("botpair start", self.server.botpair_start, st.botpair_start_window)
        else:
            ip = self._limited("botpair token", self.server.botpair_poll, st.botpair_poll_window)
        if ip is None:
            return
        req = self._json_body(raw)
        if req is None:
            self.server.failures.add("*")
            self._json(400, {"error": "bad_request", "message": "Нужен JSON."})
            return
        if start:
            if st.botpair_require_invite:
                with self.server.db_lock:
                    invited = _valid_invite(self.server.conn, req.get("code"), int(time.time()))
                if not invited:
                    self.server.failures.add("*")
                    log.info("botpair start from=%s: нужен код приглашения", _safe(ip))
                    self._json(403, {"error": "invite_required", "message": "Посредник создаёт ботов только по коду "
                                     "приглашения. Код выдаёт держатель посредника; либо создайте бота у @BotFather вручную."})
                    return
            try:
                answer = manager.start(req.get("name"))
            except PairError as e:
                log.info("botpair start from=%s: %s", _safe(ip), e.code)
                self._json(e.status, {"error": e.code, "message": e.message})
                return
            log.info("botpair start from=%s: пара %s", _safe(ip), answer["pair_id"])
            self._json(200, answer)
            return
        status, body, failed = manager.fetch(m.group(1), req.get("secret"))
        if failed:
            self.server.failures.add("*")
        self._json(status, body)

    def _manager_setup(self, raw: bytes | None) -> None:
        st = self.server.settings
        ip = self._limited("manager setup", self.server.check_per_ip, st.check_window)
        if ip is None:
            return
        req = self._json_body(raw)
        if req is None:
            self.server.failures.add("*")
            self._json(400, {"error": "bad_request", "message": "Нужен JSON."})
            return
        try:
            with self.server.db_lock:
                answer = manager_setup(self.server.conn, st, self.server.manager, req.get("secret"), req.get("token"))
        except JoinError as e:
            self.server.failures.add("*")
            log.info("manager setup from=%s: %s", _safe(ip), e.code)
            self._json(e.status, {"error": e.code, "message": e.message})
            return
        except Exception:
            log.exception("manager setup: внутренняя ошибка")
            self._json(500, {"error": "internal", "message": "Внутренняя ошибка посредника."})
            return
        log.info("manager setup from=%s: токен бота-менеджера @%s сохранён", _safe(ip), _safe(answer.get("username")))
        self._json(200, answer)

    def do_POST(self) -> None:
        raw = self._body(4096)
        path = urlsplit(self.path).path
        if path.startswith("/api/botpair/"):
            self._botpair(path, raw)
            return
        if path == "/api/manager/setup":
            self._manager_setup(raw)
            return
        if path not in ("/api/join", "/api/check"):
            self._json(404, {"error": "not_found"})
            return
        what = path.rsplit("/", 1)[1]
        st = self.server.settings
        if what == "join":
            per_ip, window = self.server.per_ip, st.join_window
        else:
            per_ip, window = self.server.check_per_ip, st.check_window
        # Сначала — предел на адрес (считаются все попытки; у проверок имени свой счёт, чтобы они не
        # съедали попытки подключения), затем — общий предел неудачных попыток со всех адресов за
        # минуту, один на join и check: он бережёт службу от перебора кодов с многих адресов, а
        # законное подключение задерживает не дольше чем на минуту после конца такого перебора.
        ip = self._limited(what, per_ip, window)
        if ip is None:
            return
        req = self._json_body(raw)
        if not isinstance(req, dict):
            self.server.failures.add("*")
            shape = '{"code": …, "name": …, "key_hash": …}' if what == "join" else '{"code": …, "name": …}'
            self._json(400, {"error": "bad_request", "message": f"Нужен JSON вида {shape}."})
            return
        name = req.get("name")
        try:
            with self.server.db_lock:
                if what == "join":
                    answer = join(self.server.conn, st, req.get("code"), name, req.get("key_hash"))
                else:
                    answer = check(self.server.conn, st, req.get("code"), name)
        except JoinError as e:
            self.server.failures.add("*")
            log.info("%s from=%s name=%s: %s", what, _safe(ip), _safe(name), e.code)
            self._json(e.status, {"error": e.code, "message": e.message, **e.extra})
            return
        except Exception:
            log.exception("%s: внутренняя ошибка", what)
            self._json(500, {"error": "internal", "message": "Внутренняя ошибка посредника."})
            return
        if what == "check":
            log.info("check from=%s name=%s: %s", _safe(ip), _safe(name), answer["reason"] or "свободно")
            self._json(200, answer)
            return
        if answer.get("repeated"):
            log.info("join from=%s name=%s: повтор того же подключения", _safe(ip), _safe(name))
            self._json(200, answer)
            return
        log.info("join from=%s name=%s: экземпляр заведён", _safe(ip), _safe(name))
        self._json(200, answer)


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_servers(s: Settings, plugin_addr: tuple[str, int], api_addr: tuple[str, int],
                 plugin_path: str = "/frps/handler", manager: Manager | None = None) -> tuple[_Server, _Server]:
    conn = open_db(s.db)
    lock = threading.Lock()
    plugin = _Server(plugin_addr, PluginHandler)
    api = _Server(api_addr, ApiHandler)
    for srv in (plugin, api):
        srv.conn, srv.db_lock, srv.settings = conn, lock, s
    plugin.plugin_path = plugin_path
    api.per_ip = Window(s.join_per_ip, s.join_window)
    api.check_per_ip = Window(s.check_per_ip, s.check_window)
    api.failures = Window(s.join_fail_total, s.join_fail_window)
    api.botpair_start = Window(s.botpair_start_per_ip, s.botpair_start_window)
    api.botpair_poll = Window(s.botpair_poll_per_ip, s.botpair_poll_window)
    api.manager = manager or Manager(s)
    return plugin, api


def _addr(v: str) -> tuple[str, int]:
    host, _, port = v.rpartition(":")
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise argparse.ArgumentTypeError("служба слушает только локальный адрес: 127.0.0.1:ПОРТ")
    return host, int(port)


def cmd_serve(a: argparse.Namespace) -> int:
    token = os.environ.get("FRP_AUTH_TOKEN", "")
    if not token:
        print("нет FRP_AUTH_TOKEN в окружении (EnvironmentFile=/etc/frp/token.env в службе)", file=sys.stderr)
        return 2
    if not os.path.isfile(a.ca):
        print(f"нет сертификата frps: {a.ca} — его создаёт relay/install.sh", file=sys.stderr)
        return 2
    if not PREFIX_RE.fullmatch(a.botpair_prefix):
        print("--botpair-prefix: 3–16 знаков, строчные латинские буквы и цифры, первая — буква", file=sys.stderr)
        return 2
    invite = a.botpair_require_invite or os.environ.get("RELAY_BOTPAIR_REQUIRE_INVITE", "").lower() in ("1", "on", "yes", "true")
    s = Settings(db=a.db, domain=a.domain, server=a.server, port=a.port, ca_file=a.ca, frp_token=token,
                 botpair_prefix=a.botpair_prefix, botpair_require_invite=invite,
                 tg_api=os.environ.get("RELAY_TELEGRAM_API", "").rstrip("/") or Settings.tg_api)
    plugin, api = make_servers(s, a.plugin, a.api)
    # Адрес посредника — для ссылок, которые печатает relayctl manager-link.
    api.conn.execute("INSERT INTO meta(key, value) VALUES('public', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (s.server_name,))
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    for srv in (plugin, api):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=api.manager.run, args=(stop,), daemon=True, name="manager").start()
    log.info("relayd %s: плагин frps на %s:%d, выдача имён на %s:%d, зона %s; бот-менеджер: %s%s",
             VERSION, *a.plugin, *a.api, a.domain, s.manager_path,
             "; пары — только по коду приглашения" if invite else "")
    stop.wait()
    for srv in (plugin, api):
        srv.shutdown()
    return 0


# --- команды держателя -------------------------------------------------------------------------------

def drop_to_db_owner(db: str) -> None:
    """relayctl запускают через sudo, а база принадлежит пользователю службы: работаем от его имени,
    чтобы файлы базы не стали принадлежать root."""
    if os.geteuid() != 0:
        return
    st = os.stat(os.path.dirname(os.path.abspath(db)) or ".")
    if st.st_uid == 0:
        return
    os.setgroups([])
    os.setgid(st.st_gid)
    os.setuid(st.st_uid)


def _when(ts: int | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts)) + " UTC" if ts else "-"


def cmd_init(a: argparse.Namespace) -> int:
    drop_to_db_owner(a.db)
    open_db(a.db).close()
    print(f"база готова: {a.db}")
    return 0


def cmd_invite(a: argparse.Namespace) -> int:
    if not 1 <= a.days <= 90:
        print("срок — от 1 до 90 дней", file=sys.stderr)
        return 2
    drop_to_db_owner(a.db)
    conn = open_db(a.db)
    now = int(time.time())
    code = new_code()
    inv_id = secrets.token_hex(4)
    conn.execute("INSERT INTO invites(id, code_hash, created_at, expires_at, note) VALUES(?, ?, ?, ?, ?)",
                 (inv_id, code_hash(normalize_code(code)), now, now + a.days * 86400, a.note or ""))
    print(code)
    print(f"Приглашение {inv_id}: одноразовое, действует до {_when(now + a.days * 86400)}.", file=sys.stderr)
    print("Код показывается один раз и здесь не хранится. Передайте его владельцу экземпляра; он подключается "
          "командой ./ops/relay.sh join КОД ИМЯ.", file=sys.stderr)
    return 0


def cmd_list(a: argparse.Namespace) -> int:
    drop_to_db_owner(a.db)
    conn = open_db(a.db)
    now = int(time.time())
    print("Экземпляры:")
    rows = conn.execute("SELECT i.name, i.created_at, i.revoked_at, i.operator, v.note FROM instances i "
                        "LEFT JOIN invites v ON v.id = i.invite_id ORDER BY i.created_at").fetchall()
    if not rows:
        print("  (нет)")
    for r in rows:
        state = "служебный" if r["operator"] else ("ОТКЛЮЧЁН " + _when(r["revoked_at"]) if r["revoked_at"] else "подключён")
        print(f"  {r['name']:<30} {state:<28} с {_when(r['created_at'])}  {r['note'] or ''}")
    print("Приглашения:")
    rows = conn.execute("SELECT * FROM invites ORDER BY created_at").fetchall()
    if not rows:
        print("  (нет)")
    for r in rows:
        if r["used_at"]:
            state = f"использовано {_when(r['used_at'])} → {r['used_by']}"
        elif r["expires_at"] <= now:
            state = "истекло"
        else:
            state = f"ждёт, до {_when(r['expires_at'])}"
        print(f"  {r['id']}  {state:<48} {r['note']}")
    return 0


def _is_root() -> bool:
    return os.geteuid() == 0


def restart_frps() -> bool:
    """Перезапустить frps. Не вышло — сообщение и False (без трассировки)."""
    try:
        subprocess.run(["systemctl", "restart", "frps"], check=True)
        return True
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"frps НЕ перезапущен ({e}). Перезапустите вручную: sudo systemctl restart frps", file=sys.stderr)
        return False


def _as_db_owner(db: str, fn) -> int:
    """Работа с базой — от имени её владельца. Под root — в отдельном процессе, чтобы сам relayctl
    остался root и смог перезапустить frps."""
    if not _is_root():
        drop_to_db_owner(db)
        return fn()
    pid = os.fork()
    if pid == 0:
        try:
            drop_to_db_owner(db)
            os._exit(fn())
        except BaseException:
            os._exit(1)
    return os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])


def _revoke_in_db(db: str, name: str) -> int:
    conn = open_db(db)
    inst = get_instance(conn, name)
    if inst is None:
        print(f"экземпляра {name} нет", file=sys.stderr)
        return 1
    if inst.operator:
        print("служебный экземпляр посредника не отключается", file=sys.stderr)
        return 2
    conn.execute("UPDATE instances SET revoked_at = ? WHERE name = ? AND revoked_at IS NULL", (int(time.time()), name))
    return 0


def _release_in_db(db: str, name: str) -> int:
    conn = open_db(db)
    inst = get_instance(conn, name)
    if inst is None:
        print(f"экземпляра {name} нет", file=sys.stderr)
        return 1
    if not inst.revoked:
        print(f"{name} не отключён — сначала relayctl revoke {name}", file=sys.stderr)
        return 2
    conn.execute("DELETE FROM instances WHERE name = ?", (name,))
    conn.execute("DELETE FROM runs WHERE name = ?", (name,))
    return 0


def cmd_revoke(a: argparse.Namespace) -> int:
    """Отключить экземпляр и (по умолчанию) перезапустить frps. Без перезапуска frps держит уже
    открытое управляющее соединение экземпляра и уже открытые соединения браузеров (keep-alive,
    HTTP/2) — они работают, пока frps не перезапустится."""
    if not a.no_restart and not _is_root():
        print("нужен root, чтобы перезапустить frps: sudo relayctl revoke ИМЯ (или --no-restart — см. relayctl revoke --help)",
              file=sys.stderr)
        return 2
    rc = _as_db_owner(a.db, lambda: _revoke_in_db(a.db, a.name))
    if rc:
        return rc
    print(f"{a.name} отключён: новых рабочих соединений frps ему не даёт, переподключиться с прежним ключом он не сможет.")
    if a.no_restart:
        print("frps не перезапускался: уже открытое управляющее соединение экземпляра и уже открытые соединения "
              "браузеров (keep-alive, HTTP/2) работают до перезапуска frps. Разорвать — sudo systemctl restart frps "
              "(на несколько секунд прервёт все экземпляры, они переподключатся сами).")
    elif restart_frps():
        print("frps перезапущен: подключения экземпляра разорваны (остальные экземпляры переподключаются сами).")
    else:
        print("Экземпляр отмечен отключённым, но его уже открытые соединения работают до перезапуска frps.", file=sys.stderr)
        return 1
    print(f"Имя {a.name} остаётся занятым; освободить — sudo relayctl release {a.name}.")
    return 0


def cmd_release(a: argparse.Namespace) -> int:
    """Освободить имя отключённого экземпляра. frps перезапускается всегда: иначе прежнее подключение
    могло бы держать пересылку на это имя, и новый владелец имени её не занял бы."""
    if not _is_root():
        print("нужен root: release перезапускает frps — sudo relayctl release ИМЯ", file=sys.stderr)
        return 2
    # Сначала проверка и перезапуск, потом удаление из базы: не перезапустился frps — имя не освобождено.
    def check() -> int:
        inst = get_instance(open_db(a.db), a.name)
        if inst is None:
            print(f"экземпляра {a.name} нет", file=sys.stderr)
            return 1
        if not inst.revoked:
            print(f"{a.name} не отключён — сначала sudo relayctl revoke {a.name}", file=sys.stderr)
            return 2
        return 0
    rc = _as_db_owner(a.db, check)
    if rc:
        return rc
    if not restart_frps():
        print(f"Имя {a.name} не освобождено.", file=sys.stderr)
        return 1
    rc = _as_db_owner(a.db, lambda: _release_in_db(a.db, a.name))
    if rc:
        return rc
    print(f"Имя {a.name} освобождено, frps перезапущен. Выдавать это имя другому владельцу раньше чем через 90 дней "
          "не стоит: сертификаты, выпущенные на него прежнему владельцу, действуют до конца своего срока, а с ними "
          "прежний владелец мог бы выдать себя за нового, окажись он на пути его трафика.")
    return 0


def cmd_operator(a: argparse.Namespace) -> int:
    """Ключ служебного экземпляра посредника (его frpc отдаёт relay.<зона> — /api/*). Файл ключа
    читает и пишет root, затем — работа с базой от имени её владельца."""
    path = a.key_file
    key = None
    if os.path.exists(path):
        with open(path, encoding="ascii") as f:
            for line in f:
                if line.startswith("FRPC_OPERATOR_KEY="):
                    key = line.split("=", 1)[1].strip()
    if not key:
        key = new_key()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(f"FRPC_OPERATOR_KEY={key}\n")
        print(f"ключ служебного экземпляра создан: {path}")
    drop_to_db_owner(a.db)
    conn = open_db(a.db)
    conn.execute("INSERT INTO instances(name, key_hash, created_at, operator) VALUES(?, ?, ?, 1) "
                 "ON CONFLICT(name) DO UPDATE SET key_hash = excluded.key_hash, operator = 1, revoked_at = NULL",
                 (OPERATOR_NAME, key_hash(key), int(time.time())))
    print(f"служебный экземпляр «{OPERATOR_NAME}» записан в базу")
    return 0


def cmd_manager_link(a: argparse.Namespace) -> int:
    """Одноразовая ссылка на страницу ввода токена бота-менеджера. Секрет — после «#»: браузер
    не отправляет эту часть адреса на сервер при открытии страницы и не пишет её в журналы прокси."""
    if not 1 <= a.minutes <= 60:
        print("срок — от 1 до 60 минут", file=sys.stderr)
        return 2
    drop_to_db_owner(a.db)
    conn = open_db(a.db)
    now = int(time.time())
    secret = secrets.token_urlsafe(32)
    conn.execute("DELETE FROM manager_links WHERE used_at IS NULL")          # новая ссылка отменяет прежние
    conn.execute("INSERT INTO manager_links(id, secret_hash, created_at, expires_at) VALUES(?, ?, ?, ?)",
                 (secrets.token_hex(4), secret_hash(secret, "manager-link"), now, now + a.minutes * 60))
    row = conn.execute("SELECT value FROM meta WHERE key = 'public'").fetchone()
    host = row["value"] if row else f"{OPERATOR_NAME}.{DEFAULT_DOMAIN}"
    print(f"https://{host}/api/manager/setup#{secret}")
    print(f"Ссылка одноразовая, действует {a.minutes} мин.; прежние неиспользованные ссылки отменены. Откройте её "
          "в своём браузере и вставьте токен бота-менеджера. Никому её не пересылайте: кто откроет ссылку, "
          "тот задаст боту-менеджеру посредника свой токен.", file=sys.stderr)
    return 0


def cmd_manager_token(a: argparse.Namespace) -> int:
    """Токен бота-менеджера из стандартного ввода (или --remove — убрать). Проверка getMe и
    can_manage_bots — до записи; запись — от имени пользователя службы, в её каталог состояния."""
    path = Settings(db=a.db).manager_path
    if a.remove:
        rc = _as_db_owner(a.db, lambda: (write_manager_token(path, None), 0)[1])
        if rc == 0:
            print("Токен бота-менеджера убран: служба перестанет создавать ботов в течение нескольких секунд. "
                  "Чтобы прежний токен больше не действовал, выпустите новый в @BotFather (/revoke).")
        return rc
    if sys.stdin.isatty():
        import getpass
        token = getpass.getpass("Токен бота-менеджера (ввод не показывается): ").strip()
    else:
        token = sys.stdin.readline().strip()
    if not BOT_TOKEN_RE.fullmatch(token):
        print("это не похоже на токен бота: длинная строка с двоеточием из ответа @BotFather", file=sys.stderr)
        return 2
    username = "?"
    if not a.no_check:
        try:
            me = tg_call(os.environ.get("RELAY_TELEGRAM_API", "").rstrip("/") or Settings.tg_api, token, "getMe")
        except TelegramError as e:
            print("Telegram не принял токен" if e.status in (401, 404) else "не удалось связаться с Telegram",
                  file=sys.stderr)
            return 1
        if not isinstance(me, dict) or me.get("can_manage_bots") is not True:
            print("у бота не включён Bot Management Mode: включите его в мини-приложении @BotFather и повторите",
                  file=sys.stderr)
            return 1
        username = str(me.get("username"))
    rc = _as_db_owner(a.db, lambda: (write_manager_token(path, token), 0)[1])
    if rc == 0:
        print(f"Токен бота-менеджера @{username} сохранён ({path}); служба подхватит его в течение нескольких секунд. "
              "Проверка: sudo relayctl manager-status")
    return rc


def cmd_manager_status(a: argparse.Namespace) -> int:
    """Состояние бота-менеджера без секретов: есть ли токен, что о боте говорит Telegram, готова ли служба."""
    path = Settings(db=a.db).manager_path
    found: dict[str, Any] = {}

    def look() -> int:
        try:
            st = os.stat(path)
        except OSError:
            return 3
        found["mode"] = oct(st.st_mode & 0o777)
        found["token"] = read_manager_token(path)
        return 0 if found["token"] else 4

    if _is_root():                      # файл читает только пользователь службы — читаем от его имени
        r_fd, w_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r_fd)
            try:
                drop_to_db_owner(a.db)
                rc = look()
                os.write(w_fd, json.dumps(found).encode())
            except BaseException:
                rc = 1
            os._exit(rc)
        os.close(w_fd)
        with os.fdopen(r_fd, "rb") as f:
            data = f.read()
        rc = os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])
        found = json.loads(data or b"{}")
    else:
        rc = look()
    if rc == 3:
        print(f"Токена бота-менеджера нет ({path}). Задать: sudo relayctl manager-link (страница в браузере) "
              "или sudo relayctl manager-token (из стандартного ввода).")
        return 1
    if rc:
        print(f"Файл {path} есть, но токена в нём нет или он испорчен. Задайте токен заново.")
        return 1
    print(f"Токен бота-менеджера: задан ({path}, права {found.get('mode')})")
    try:
        me = tg_call(os.environ.get("RELAY_TELEGRAM_API", "").rstrip("/") or Settings.tg_api, found["token"], "getMe")
        print(f"Telegram: бот @{me.get('username')}, Bot Management Mode: "
              f"{'включён' if me.get('can_manage_bots') is True else 'ВЫКЛЮЧЕН — включите в мини-приложении @BotFather'}")
    except TelegramError as e:
        print("Telegram: " + ("токен не принят — выпустите новый в @BotFather и задайте его снова" if e.status in (401, 404)
                              else f"нет ответа ({e.code})"))
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                f"http://127.0.0.1:{a.api_port}/api/health", timeout=5) as r:
            health = json.loads(r.read())
        print("Служба: " + ("создание ботов в одно нажатие работает" if health.get("botpair") else
                            "бот-менеджер ещё не готов (journalctl -u shturman-relayd -n 50)")
              + f", версия {health.get('version')}")
    except (OSError, ValueError):
        print("Служба: не отвечает на 127.0.0.1 — systemctl status shturman-relayd")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    p = argparse.ArgumentParser(prog="relayctl", description="Посредник «Штурмана»: служба и команды держателя.")
    p.add_argument("--db", default=os.environ.get("RELAY_DB", DEFAULT_DB), help=f"база (по умолчанию {DEFAULT_DB})")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="служба: плагин frps и выдача имён")
    s.add_argument("--domain", default=DEFAULT_DOMAIN)
    s.add_argument("--server", default="", help="адрес посредника для frpc (по умолчанию relay.<зона>)")
    s.add_argument("--port", type=int, default=7000)
    s.add_argument("--ca", default=DEFAULT_CA, help="сертификат frps, который получает экземпляр")
    s.add_argument("--plugin", type=_addr, default=("127.0.0.1", 7400))
    s.add_argument("--api", type=_addr, default=("127.0.0.1", 7401))
    s.add_argument("--botpair-prefix", default="shturman",
                   help="начало имени ботов, создаваемых в одно нажатие: <префикс>_<номер пары>_bot")
    s.add_argument("--botpair-require-invite", action="store_true",
                   help="создавать ботов только по действующему коду приглашения (или RELAY_BOTPAIR_REQUIRE_INVITE=1)")
    sub.add_parser("init", help="создать базу")
    i = sub.add_parser("invite", help="выпустить одноразовый код приглашения")
    i.add_argument("--days", type=int, default=7)
    i.add_argument("--note", default="", help="пометка для себя: кому выдан")
    sub.add_parser("list", help="экземпляры и приглашения")
    r = sub.add_parser("revoke", help="отключить экземпляр")
    r.add_argument("name")
    r.add_argument("--no-restart", action="store_true",
                   help="не перезапускать frps: уже открытые соединения экземпляра работают до перезапуска")
    rl = sub.add_parser("release", help="освободить имя отключённого экземпляра (перезапускает frps)")
    rl.add_argument("name")
    o = sub.add_parser("operator", help="ключ служебного экземпляра посредника")
    o.add_argument("--key-file", required=True)
    ml = sub.add_parser("manager-link", help="одноразовая ссылка на страницу ввода токена бота-менеджера")
    ml.add_argument("--minutes", type=int, default=15)
    mt = sub.add_parser("manager-token", help="токен бота-менеджера из стандартного ввода")
    mt.add_argument("--remove", action="store_true", help="убрать токен: создание ботов выключится")
    mt.add_argument("--no-check", action="store_true", help="не спрашивать Telegram перед записью (стенд)")
    ms = sub.add_parser("manager-status", help="состояние бота-менеджера, без секретов")
    ms.add_argument("--api-port", type=int, default=7401)
    a = p.parse_args(argv)
    return {"serve": cmd_serve, "init": cmd_init, "invite": cmd_invite, "list": cmd_list, "revoke": cmd_revoke,
            "release": cmd_release, "operator": cmd_operator, "manager-link": cmd_manager_link,
            "manager-token": cmd_manager_token, "manager-status": cmd_manager_status}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
