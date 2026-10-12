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
  * **выдача имён по коду приглашения**: `POST /api/join {code, name}`. Код одноразовый, его
    выпускает держатель посредника (`relayctl invite`); имя выбирает владелец экземпляра. Ответ —
    имена, адрес посредника, общий токен frps, ключ экземпляра и сертификат frps для проверки
    соединения. Ключ отдаётся один раз и хранится здесь только в виде sha256;
  * **команды держателя** (`relayctl`): invite, list, revoke, release, operator, init.

Ни ключи, ни коды, ни токен не пишутся в журнал. Слушает только 127.0.0.1: плагин спрашивает frps
на этом же сервере, а `/api/*` наружу отдаёт локальный Caddy посредника (relay/Caddyfile).

    relayd.py serve [--db …] [--domain sh.namre.app] [--server relay.sh.namre.app] [--port 7000]
                    [--ca /etc/frp/frps.crt] [--plugin 127.0.0.1:7400] [--api 127.0.0.1:7401]
    relayd.py init | invite [--days 7] [--note …] | list | revoke ИМЯ [--restart-frps]
            | release ИМЯ | operator --key-file ФАЙЛ
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.server
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
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

VERSION = "0.1.0"
DEFAULT_DB = "/var/lib/shturman-relay/relay.db"
DEFAULT_DOMAIN = "sh.namre.app"
DEFAULT_CA = "/etc/frp/frps.crt"
OPERATOR_NAME = "relay"

log = logging.getLogger("relayd")

# --- имена ---------------------------------------------------------------------------------------

# 3–30 знаков: строчные латинские буквы, цифры и дефис внутри.
NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{1,28}[a-z0-9])$")
# Имена, которые не выдаются никому: служебные, почтовые, похожие на чужие службы и на сам проект.
RESERVED = frozenset("""
relay www api admin administrator mail email smtp imap pop pop3 ftp sftp ssh test tests testing setup sh
status root support help info abuse postmaster hostmaster webmaster security noc dns mx ns vpn proxy
autoconfig autodiscover wpad isatap localhost local dashboard login auth oauth sso account accounts
billing pay static assets cdn docs doc blog shop store app apps dev staging stage prod demo example
git gitlab github frp frps frpc caddy acme cert certs ca shturman namre hermes telegram bot bots
""".split())
RESERVED_PATTERNS = (re.compile(r"^(ns|mx|mail|www|smtp|relay|api)[0-9]*$"),)


def name_problem(name: str) -> str | None:
    """Почему имя нельзя выдать (код ошибки), или None, если можно."""
    if not isinstance(name, str) or not NAME_RE.match(name):
        return "bad_name"
    if name.startswith("setup-") or name[2:4] == "--":   # setup-… — имя страницы; xn-- — punycode
        return "bad_name"
    if name in RESERVED or any(p.match(name) for p in RESERVED_PATTERNS):
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
-- run_id, и чужой run_id в Login здесь отклоняется.
CREATE TABLE IF NOT EXISTS runs (
    run_id  TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    seen_at INTEGER NOT NULL
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


def _run_owner(conn: sqlite3.Connection, run_id: Any) -> str | None:
    if not isinstance(run_id, str) or not run_id:
        return None
    row = conn.execute("SELECT name FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return row["name"] if row else None


def _remember_run(conn: sqlite3.Connection, run_id: Any, name: str) -> None:
    if isinstance(run_id, str) and run_id and len(run_id) <= 128:
        conn.execute("INSERT INTO runs(run_id, name, seen_at) VALUES(?, ?, ?) "
                     "ON CONFLICT(run_id) DO UPDATE SET seen_at = excluded.seen_at WHERE runs.name = excluded.name",
                     (run_id, name, int(time.time())))


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
    join_per_ip: int = 10            # попыток подключения с одного адреса за окно
    join_total: int = 120            # попыток подключения всего за окно
    join_window: int = 15 * 60       # секунд

    @property
    def server_name(self) -> str:
        return self.server or f"{OPERATOR_NAME}.{self.domain}"


class JoinError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(code)
        self.status, self.code, self.message = status, code, message


MESSAGES = {
    "bad_name": "Имя не подходит: 3–30 знаков, строчные латинские буквы, цифры и дефис внутри; "
                "не начинается с setup-.",
    "name_reserved": "Это имя занято под служебное — выберите другое.",
    "name_taken": "Это имя уже занято — выберите другое. Код приглашения не израсходован.",
    "bad_code": "Код приглашения не подошёл: неверный, уже использован или истёк. Новый код выдаёт держатель посредника.",
}


def join(conn: sqlite3.Connection, s: Settings, code: Any, name: Any, now: int | None = None) -> dict:
    """Израсходовать код и завести экземпляр. Всё — одной транзакцией: код тратится, только если
    экземпляр заведён."""
    now = int(time.time()) if now is None else now
    problem = name_problem(name)
    if problem:
        raise JoinError(400, problem, MESSAGES[problem])
    norm = normalize_code(code)
    if norm is None:
        raise JoinError(403, "bad_code", MESSAGES["bad_code"])
    key = new_key()
    conn.execute("BEGIN IMMEDIATE")
    try:
        inv = conn.execute("SELECT id FROM invites WHERE code_hash = ? AND used_at IS NULL AND expires_at > ?",
                           (code_hash(norm), now)).fetchone()
        if inv is None:
            raise JoinError(403, "bad_code", MESSAGES["bad_code"])
        # Имя занято, в том числе отключённым экземпляром: освобождает его только release.
        if conn.execute("SELECT 1 FROM instances WHERE name = ?", (name,)).fetchone():
            raise JoinError(409, "name_taken", MESSAGES["name_taken"])
        cur = conn.execute("UPDATE invites SET used_at = ?, used_by = ? WHERE id = ? AND used_at IS NULL",
                           (now, name, inv["id"]))
        if cur.rowcount != 1:
            raise JoinError(403, "bad_code", MESSAGES["bad_code"])
        conn.execute("INSERT INTO instances(name, key_hash, created_at, invite_id) VALUES(?, ?, ?, ?)",
                     (name, key_hash(key), now, inv["id"]))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    with open(s.ca_file, encoding="ascii") as f:
        ca = f.read()
    return {
        "name": name,
        "domain": s.domain,
        "dashboard_url": f"https://{name}.{s.domain}",
        "setup_url": f"https://setup-{name}.{s.domain}",
        "server": s.server_name,
        "port": s.port,
        "frp_token": s.frp_token,
        "key": key,
        "ca": ca,
    }


class RateLimit:
    """Скользящее окно в памяти: на адрес и на всех вместе. После перезапуска счёт начинается заново."""

    def __init__(self, per_key: int, total: int, window: int):
        self.per_key, self.total, self.window = per_key, total, window
        self.hits: dict[str, deque] = {}
        self.all: deque = deque()
        self.lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        edge = now - self.window
        with self.lock:
            while self.all and self.all[0] <= edge:
                self.all.popleft()
            q = self.hits.setdefault(key, deque())
            while q and q[0] <= edge:
                q.popleft()
            for k in [k for k, v in self.hits.items() if not v and k != key]:
                del self.hits[k]
            if len(q) >= self.per_key or len(self.all) >= self.total:
                return False
            q.append(now)
            self.all.append(now)
            return True


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
    """/api/health, /api/ca, /api/join — за локальным Caddy посредника."""

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
        if path == "/api/health":
            self._json(200, {"ok": True, "version": VERSION})
        elif path == "/api/ca":
            try:
                with open(self.server.settings.ca_file, "rb") as f:
                    self._send(200, f.read(), "application/x-pem-file")
            except OSError:
                self._json(503, {"error": "no_ca", "message": "сертификат посредника недоступен"})
        else:
            self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        raw = self._body(4096)
        if urlsplit(self.path).path != "/api/join":
            self._json(404, {"error": "not_found"})
            return
        ip = self.client_ip()
        if not self.server.limiter.allow(ip):
            log.info("join from=%s: слишком часто", _safe(ip))
            self._json(429, {"error": "rate_limited", "message": "Слишком много попыток. Повторите через 15 минут."},
                       extra={"Retry-After": str(self.server.settings.join_window)})
            return
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        try:
            req = json.loads(raw) if raw is not None and ctype == "application/json" else None
        except ValueError:
            req = None
        if not isinstance(req, dict):
            self._json(400, {"error": "bad_request", "message": "Нужен JSON вида {\"code\": …, \"name\": …}."})
            return
        name = req.get("name")
        try:
            with self.server.db_lock:
                answer = join(self.server.conn, self.server.settings, req.get("code"), name)
        except JoinError as e:
            log.info("join from=%s name=%s: %s", _safe(ip), _safe(name), e.code)
            self._json(e.status, {"error": e.code, "message": e.message})
            return
        except Exception:
            log.exception("join: внутренняя ошибка")
            self._json(500, {"error": "internal", "message": "Внутренняя ошибка посредника."})
            return
        log.info("join from=%s name=%s: экземпляр заведён", _safe(ip), _safe(name))
        self._json(200, answer)


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_servers(s: Settings, plugin_addr: tuple[str, int], api_addr: tuple[str, int],
                 plugin_path: str = "/frps/handler") -> tuple[_Server, _Server]:
    conn = open_db(s.db)
    lock = threading.Lock()
    plugin = _Server(plugin_addr, PluginHandler)
    api = _Server(api_addr, ApiHandler)
    for srv in (plugin, api):
        srv.conn, srv.db_lock, srv.settings = conn, lock, s
    plugin.plugin_path = plugin_path
    api.limiter = RateLimit(s.join_per_ip, s.join_total, s.join_window)
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
    s = Settings(db=a.db, domain=a.domain, server=a.server, port=a.port, ca_file=a.ca, frp_token=token)
    plugin, api = make_servers(s, a.plugin, a.api)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    for srv in (plugin, api):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("relayd %s: плагин frps на %s:%d, выдача имён на %s:%d, зона %s",
             VERSION, *a.plugin, *a.api, a.domain)
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


def cmd_revoke(a: argparse.Namespace) -> int:
    if a.restart_frps and os.geteuid() == 0:
        # База — от имени её владельца (в отдельном процессе), перезапуск frps — от root.
        pid = os.fork()
        if pid == 0:
            try:
                drop_to_db_owner(a.db)
                os._exit(_revoke_in_db(a.db, a.name))
            except BaseException:
                os._exit(1)
        rc = os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])
    else:
        drop_to_db_owner(a.db)
        rc = _revoke_in_db(a.db, a.name)
    if rc:
        return rc
    print(f"{a.name} отключён: новые соединения браузеров к нему посредник больше не пересылает (frps спрашивает "
          "службу о каждом рабочем соединении), а переподключиться с прежним ключом он не сможет.")
    if a.restart_frps:
        subprocess.run(["systemctl", "restart", "frps"], check=True)
        print("frps перезапущен: подключение экземпляра разорвано сразу (остальные экземпляры переподключились сами).")
    else:
        print("Уже открытое управляющее соединение экземпляра frps держит до его переподключения; разорвать сразу — "
              "relayctl revoke ИМЯ --restart-frps (на несколько секунд прервёт все экземпляры). Имя остаётся занятым; "
              "освободить — relayctl release ИМЯ.")
    return 0


def cmd_release(a: argparse.Namespace) -> int:
    drop_to_db_owner(a.db)
    conn = open_db(a.db)
    inst = get_instance(conn, a.name)
    if inst is None:
        print(f"экземпляра {a.name} нет", file=sys.stderr)
        return 1
    if not inst.revoked:
        print(f"{a.name} не отключён — сначала relayctl revoke {a.name}", file=sys.stderr)
        return 2
    conn.execute("DELETE FROM instances WHERE name = ?", (a.name,))
    conn.execute("DELETE FROM runs WHERE name = ?", (a.name,))
    print(f"имя {a.name} освобождено: его можно получить новым кодом приглашения. Сертификаты, выпущенные на это "
          "имя прежнему владельцу, остаются действительными до конца срока (до 90 дней).")
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
    sub.add_parser("init", help="создать базу")
    i = sub.add_parser("invite", help="выпустить одноразовый код приглашения")
    i.add_argument("--days", type=int, default=7)
    i.add_argument("--note", default="", help="пометка для себя: кому выдан")
    sub.add_parser("list", help="экземпляры и приглашения")
    r = sub.add_parser("revoke", help="отключить экземпляр")
    r.add_argument("name")
    r.add_argument("--restart-frps", action="store_true", help="сразу разорвать его подключение (перезапуск frps)")
    rl = sub.add_parser("release", help="освободить имя отключённого экземпляра")
    rl.add_argument("name")
    o = sub.add_parser("operator", help="ключ служебного экземпляра посредника")
    o.add_argument("--key-file", required=True)
    a = p.parse_args(argv)
    return {"serve": cmd_serve, "init": cmd_init, "invite": cmd_invite, "list": cmd_list, "revoke": cmd_revoke,
            "release": cmd_release, "operator": cmd_operator}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
