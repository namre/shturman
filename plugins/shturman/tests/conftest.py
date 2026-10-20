import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shturman_core.state import Store  # noqa: E402


class Clock:
    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def tick(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "state")


@pytest.fixture
def clock() -> Clock:
    return Clock()


TOKEN = "t" * 40


class FakeService:
    """Маленький HTTP-сервер вместо сервиса переписки: записывает запросы и отвечает по сценарию.

    `replies[(метод, путь)]` — ответ или список ответов по очереди; ответ — (код, словарь)
    либо вызываемое `fn(запрос) -> (код, словарь)`. Без сценария — 200 и `{"ok": true}`.
    """

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.replies: dict[tuple[str, str], object] = {}
        self.token = TOKEN
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:  # тишина в выводе тестов
                pass

            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                    raw = b""
                    while True:
                        size = int(self.rfile.readline().strip() or b"0", 16)
                        if size == 0:
                            self.rfile.readline()
                            break
                        raw += self.rfile.read(size)
                        self.rfile.readline()
                path, _, query = self.path.partition("?")
                record = {"method": self.command, "path": path, "query": query, "raw": raw,
                          "headers": {k.lower(): v for k, v in self.headers.items()}}
                try:
                    record["json"] = json.loads(raw) if raw else None
                except ValueError:
                    record["json"] = None
                outer.requests.append(record)
                if self.headers.get("Authorization") != f"Bearer {outer.token}":
                    status, payload = 401, {"error": "unauthorized"}
                else:
                    reply = outer.replies.get((self.command, path), (200, {"ok": True}))
                    if isinstance(reply, list):
                        reply = reply.pop(0) if len(reply) > 1 else reply[0]
                    if callable(reply):
                        reply = reply(record)
                    status, payload = reply
                headers = {}
                if isinstance(payload, tuple):          # (заголовки, тело) — для перенаправлений
                    headers, payload = payload
                body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_DELETE = _serve

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.02},
                                        daemon=True)
        self._thread.start()

    def calls(self, method: str | None = None, path: str | None = None) -> list[dict]:
        return [r for r in self.requests
                if (method is None or r["method"] == method) and (path is None or r["path"] == path)]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def service():
    fake = FakeService()
    yield fake
    fake.close()


@pytest.fixture
def service_env(service, monkeypatch):
    """Сервис переписки «подключён»: адрес и токен лежат в окружении."""
    monkeypatch.setenv("SHTURMAN_SERVICE_URL", service.url)
    monkeypatch.setenv("SHTURMAN_API_TOKEN", service.token)
    return service


@pytest.fixture(autouse=True)
def _no_service_by_default(monkeypatch):
    """Тест не должен случайно увидеть сервис из окружения разработчика."""
    monkeypatch.delenv("SHTURMAN_API_TOKEN", raising=False)
    monkeypatch.delenv("SHTURMAN_SERVICE_URL", raising=False)
