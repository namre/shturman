"""Подставной Bot API Telegram для тестов и стенда посредника: настоящий HTTP-сервер на 127.0.0.1,
только стандартная библиотека. Умеет то, что нужно боту-менеджеру (Р-77, Р-78): getMe, getUpdates с offset
и allowed_updates, getManagedBotToken, replaceManagedBotToken, setManagedBotAccessSettings,
getManagedBotAccessSettings, sendMessage (запоминает, что отправлено) и сообщения пользователей менеджеру. Поля — как в core.telegram.org/bots/api (сверено 2026-10-13).

    python3 relay/tests/fake_telegram.py [ПОРТ]   — запустить отдельно (стенд); токены печатаются в вывод
"""

from __future__ import annotations

import http.server
import json
import secrets
import sys
import threading
import time
from typing import Any

MANAGER_TOKEN = "8000000001:MANAGER-token_SENTINEL-DoNotLeak-0123456789"
MANAGER_ID = 8000000001
PLAIN_TOKEN = "8000000002:PLAIN-bot-token_not-a-manager-0123456789ab"


class FakeTelegram:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.bots: dict[str, dict] = {
            MANAGER_TOKEN: {"id": MANAGER_ID, "is_bot": True, "first_name": "Менеджер", "username": "shturman_manager_bot",
                            "can_manage_bots": True},
            PLAIN_TOKEN: {"id": 8000000002, "is_bot": True, "first_name": "Обычный", "username": "plain_bot"},
        }
        self.managed: dict[int, dict] = {}          # id бота → {token, owner, username, name, restricted}
        self.updates: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.fail: dict[str, list[int]] = {}         # метод → коды ошибок для следующих вызовов
        self.allowed: list | None = None
        self._uid = 100
        self._next_bot = 9000000001
        self.server: http.server.ThreadingHTTPServer | None = None

    # --- что делает пользователь Telegram ---

    def create_managed_bot(self, username: str, name: str, owner: dict, manager_token: str = MANAGER_TOKEN,
                           *, bot_id: int | None = None, token: str | None = None) -> int:
        """Пользователь открыл ссылку t.me/newbot/… и подтвердил создание: бот создан, менеджеру — обновление.
        bot_id и token — заданные заранее (стенд с подставным Telegram сервиса)."""
        with self.lock:
            if bot_id is None:
                bot_id = self._next_bot
                self._next_bot += 1
            token = token or f"{bot_id}:MANAGED-{secrets.token_urlsafe(24)}"
            bot = {"id": bot_id, "is_bot": True, "first_name": name, "username": username}
            self.managed[bot_id] = {"token": token, "owner": owner, "bot": bot, "restricted": False,
                                    "manager": manager_token}
            self.bots[token] = dict(bot)
            self._push({"managed_bot": {"user": owner, "bot": bot}})
        return bot_id

    def change_owner(self, bot_id: int, owner: dict) -> None:
        with self.lock:
            m = self.managed[bot_id]
            m["owner"] = owner
            self._push({"managed_bot": {"user": owner, "bot": m["bot"]}})

    def send_to_manager(self, user: dict, text: str, chat_type: str = "private") -> None:
        """Пользователь написал боту-менеджеру (или открыл ссылку t.me/<менеджер>?start=… и нажал «Старт»)."""
        with self.lock:
            chat = {"id": user["id"] if chat_type == "private" else -100500, "type": chat_type}
            self._push({"message": {"message_id": self._uid, "date": int(time.time()), "from": user,
                                    "chat": chat, "text": text}})

    def replies(self, chat_id: int | None = None) -> list[str]:
        """Что бот-менеджер ответил (sendMessage), по порядку."""
        with self.lock:
            return [p.get("text") for m, p in self.calls
                    if m == "sendMessage" and (chat_id is None or p.get("chat_id") == chat_id)]

    def _push(self, update: dict) -> None:
        self._uid += 1
        self.updates.append({"update_id": self._uid, **update})

    def token_of(self, bot_id: int) -> str:
        return self.managed[bot_id]["token"]

    # --- Bot API ---

    def handle(self, token: str, method: str, params: dict) -> tuple[int, dict]:
        with self.lock:
            self.calls.append((method, dict(params)))
            codes = self.fail.get(method)
            if codes:
                code = codes.pop(0)
                return code, {"ok": False, "error_code": code, "description": "Fake failure"}
            me = self.bots.get(token)
            if me is None:
                return 401, {"ok": False, "error_code": 401, "description": "Unauthorized"}
            if method == "getMe":
                return 200, {"ok": True, "result": me}
            if method in ("getManagedBotToken", "replaceManagedBotToken", "setManagedBotAccessSettings",
                          "getManagedBotAccessSettings"):
                m = self.managed.get(params.get("user_id"))
                if m is None or m["manager"] != token or not me.get("can_manage_bots"):
                    return 400, {"ok": False, "error_code": 400, "description": "Bad Request: bot not found"}
                if method == "getManagedBotToken":
                    return 200, {"ok": True, "result": m["token"]}
                if method == "replaceManagedBotToken":
                    self.bots.pop(m["token"], None)
                    m["token"] = f"{params['user_id']}:MANAGED-{secrets.token_urlsafe(24)}"
                    self.bots[m["token"]] = dict(m["bot"])
                    return 200, {"ok": True, "result": m["token"]}
                if method == "setManagedBotAccessSettings":
                    if not isinstance(params.get("is_access_restricted"), bool):
                        return 400, {"ok": False, "error_code": 400, "description": "Bad Request: is_access_restricted"}
                    m["restricted"] = params["is_access_restricted"]
                    return 200, {"ok": True, "result": True}
                return 200, {"ok": True, "result": {"is_access_restricted": m["restricted"]}}
            if method != "getUpdates":
                return 200, {"ok": True, "result": True}
            if "allowed_updates" in params:
                self.allowed = params["allowed_updates"]
            offset = params.get("offset")
            if isinstance(offset, int):
                self.updates = [u for u in self.updates if u["update_id"] >= offset]
            wanted = self.allowed or None
            ready = [u for u in self.updates if wanted is None or any(k in u for k in wanted)]
        if not ready:
            time.sleep(min(float(params.get("timeout") or 0), 0.05))
        return 200, {"ok": True, "result": ready}

    # --- сервер ---

    def start(self, port: int = 0) -> str:
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a: Any) -> None:
                pass

            def do_POST(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                parts = self.path.split("/")
                if self.path == "/__create":
                    # Только стенд: «владелец подтвердил создание бота по ссылке t.me/newbot/…».
                    req = json.loads(body or b"{}")
                    bot_id = fake.create_managed_bot(req["username"], req.get("name") or "Согласования", req["owner"])
                    status, out = 200, {"ok": True, "bot_id": bot_id}
                elif self.path == "/__message":
                    # Только стенд: «пользователь написал боту-менеджеру» (код привязки, Р-78).
                    req = json.loads(body or b"{}")
                    fake.send_to_manager(req["user"], req["text"])
                    status, out = 200, {"ok": True}
                elif len(parts) != 3 or not parts[1].startswith("bot"):
                    status, out = 404, {"ok": False, "error_code": 404, "description": "Not Found"}
                else:
                    try:
                        params = json.loads(body or b"{}")
                    except ValueError:
                        params = {}
                    status, out = fake.handle(parts[1][3:], parts[2], params if isinstance(params, dict) else {})
                data = json.dumps(out).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()


if __name__ == "__main__":
    fake = FakeTelegram()
    base = fake.start(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
    print(f"подставной Telegram: {base}\nтокен бота-менеджера: {MANAGER_TOKEN}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        fake.stop()
