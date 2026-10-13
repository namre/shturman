"""Exercise the shipped Caddy routes against synthetic backends, without TLS issuance.

The dashboard backend listens on 127.0.0.1:9119 (TCP, as Hermes does). The messaging service is
reached by the proxy only through its Unix socket (/run/shturman-service/proxy.sock, decision Р-76),
so its synthetic backend listens on a Unix socket in a temporary directory; a TCP listener on
127.0.0.1:8765 is kept as a trap: the proxy must never reach it. Every backend answers with its own
marker and echoes the Host header it received (the service checks Host itself, so the proxy must
pass it unchanged).

By default Caddy runs from the official image (as in CI). With SHTURMAN_SMOKE_CADDY=/path/to/caddy a
local Caddy binary is used instead (for a machine without the image).
"""
import http.client
import os
import pathlib
import socketserver
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SOCKET = "/run/shturman-service/proxy.sock"


class Backend(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("X-Upstream-Host", self.headers.get("Host", ""))
        self.end_headers()
        self.wfile.write(self.server.marker.encode())

    def log_message(self, *args):
        pass


class UnixBackend(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):
        request, _ = super().get_request()
        return request, ("unix", 0)


def request(port, path, host=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request("GET", path, headers={"Host": host} if host else {})
        response = connection.getresponse()
        return response.status, response.read().decode(), response.getheader("X-Upstream-Host")
    finally:
        connection.close()


def run(template, replacements, probes, socket_dir):
    with tempfile.TemporaryDirectory() as directory:
        config = pathlib.Path(directory) / "Caddyfile"
        text = pathlib.Path(template).read_text()
        for old, new in replacements.items():
            text = text.replace(old, new)
        local = os.environ.get("SHTURMAN_SMOKE_CADDY")
        if local:
            admin = pathlib.Path(directory) / "admin"
            admin.mkdir(mode=0o700)
            text = text.replace("unix//run/caddy-admin/admin.sock", f"unix/{admin}/admin.sock")
            text = text.replace("unix/" + SOCKET, f"unix/{socket_dir}/proxy.sock")
            config.write_text(text)
            command = [local, "run", "--config", str(config), "--adapter", "caddyfile"]
        else:
            text = text.replace("unix//run/caddy-admin/admin.sock", "unix//run/smoke-admin/admin.sock")
            config.write_text(text)
            command = ["docker", "run", "--rm", "--network", "host", "--entrypoint", "sh",
                       "-v", str(config) + ":/etc/caddy/Caddyfile:ro",
                       "-v", str(socket_dir) + ":/run/shturman-service", "caddy:2.10.2", "-c",
                       "mkdir -m 700 -p /run/smoke-admin && exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile"]
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            ready = False
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError(process.stderr.read().decode())
                try:
                    request(probes[0][0], "/")
                    ready = True
                    break
                except OSError:
                    time.sleep(.1)
            assert ready, "Caddy did not start"
            for port, path, expected, marker in probes:
                status, body, upstream_host = request(port, path)
                assert status == expected, (template, port, path, status, expected)
                if marker:
                    assert body == marker, (port, path, body)
                    # Host уходит к сервису и к дашборду без подмены.
                    assert upstream_host == f"127.0.0.1:{port}", (port, path, upstream_host)
            try:
                request(2019, "/config/")
            except OSError:
                pass
            else:
                raise AssertionError("TCP Caddy administration is exposed")
        finally:
            process.terminate()
            process.communicate(timeout=15)


def tcp(port, marker):
    server = ThreadingHTTPServer(("127.0.0.1", port), Backend)
    server.marker = marker
    return server


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as socket_dir:
        os.chmod(socket_dir, 0o755)
        unix = UnixBackend(os.path.join(socket_dir, "proxy.sock"), Backend)
        unix.marker = "service-socket"
        os.chmod(os.path.join(socket_dir, "proxy.sock"), 0o666)
        servers = [tcp(9119, "9119"), tcp(8765, "service-tcp-must-not-be-used"), unix]
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            run("config/Caddyfile.example", {
                "assistant.example.com:8443 {": "http://127.0.0.1:8089 {",
                "assistant.example.com {": "http://127.0.0.1:8088 {"}, [
                    (8088, "/", 200, "9119"),
                    (8088, "/shturman-setup/", 404, None),
                    (8089, "/shturman-setup/", 200, "service-socket"),
                    (8089, "/shturman-setup/api/status", 200, "service-socket"),
                    (8089, "/shturman-setup/app/", 200, "service-socket"),
                    *[(8089, path, 404, None) for path in ("/api/accounts", "/mcp", "/health",
                      "/shturman-setup//api/status", "/shturman-setup/../api/accounts",
                      "/%73hturman-setup/api/status", "/SHTURMAN-SETUP/api/status")]], socket_dir)
            run("config/Caddyfile.remote-mcp.example", {
                "mcp.example.com {": "http://127.0.0.1:8087 {"}, [
                    *[(8087, path, 200, "service-socket") for path in ("/mcp", "/oauth/authorize?state=synthetic",
                      "/oauth/token", "/.well-known/oauth-protected-resource/mcp")],
                    *[(8087, path, 404, None) for path in ("/api/accounts", "/shturman-setup/", "/health",
                      "/oauth//token", "/oauth/../api/accounts", "/%6dcp", "/mcp/")]], socket_dir)
        finally:
            for server in servers:
                server.shutdown()
