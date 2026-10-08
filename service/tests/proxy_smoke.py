"""Exercise the shipped Caddy routes against synthetic backends, without TLS issuance."""
import http.client
import pathlib
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Backend(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(str(self.server.server_port).encode())
    def log_message(self, *args):
        pass


def request(port, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read().decode()
    finally:
        connection.close()


def run(template, replacements, probes):
    with tempfile.TemporaryDirectory() as directory:
        config = pathlib.Path(directory) / "Caddyfile"
        text = pathlib.Path(template).read_text()
        for old, new in replacements.items():
            text = text.replace(old, new)
        text = text.replace("unix//run/caddy-admin/admin.sock", "unix//run/smoke-admin/admin.sock")
        config.write_text(text)
        process = subprocess.Popen(["docker", "run", "--rm", "--network", "host", "--entrypoint", "sh",
            "-v", str(config) + ":/etc/caddy/Caddyfile:ro", "caddy:2.10.2", "-c",
            "mkdir -m 700 -p /run/smoke-admin && exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
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
                status, body = request(port, path)
                assert status == expected, (template, port, path, status, expected)
                if marker:
                    assert body == marker, (port, path, body)
            try:
                request(2019, "/config/")
            except OSError:
                pass
            else:
                raise AssertionError("TCP Caddy administration is exposed")
        finally:
            process.terminate()
            process.communicate(timeout=15)


if __name__ == "__main__":
    servers = [ThreadingHTTPServer(("127.0.0.1", port), Backend) for port in (8765, 9119)]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        run("config/Caddyfile.example", {
            "assistant.example.com:8443 {": "http://127.0.0.1:8089 {",
            "assistant.example.com {": "http://127.0.0.1:8088 {"}, [
                (8088, "/", 200, "9119"),
                (8088, "/shturman-setup/", 404, None),
                (8089, "/shturman-setup/", 200, "8765"),
                (8089, "/shturman-setup/api/status", 200, "8765"),
                *[(8089, path, 404, None) for path in ("/api/accounts", "/mcp", "/health",
                  "/shturman-setup//api/status", "/shturman-setup/../api/accounts",
                  "/%73hturman-setup/api/status", "/SHTURMAN-SETUP/api/status")]])
        run("config/Caddyfile.remote-mcp.example", {
            "mcp.example.com {": "http://127.0.0.1:8087 {"}, [
                *[(8087, path, 200, "8765") for path in ("/mcp", "/oauth/authorize?state=synthetic",
                  "/oauth/token", "/.well-known/oauth-protected-resource/mcp")],
                *[(8087, path, 404, None) for path in ("/api/accounts", "/shturman-setup/", "/health",
                  "/oauth//token", "/oauth/../api/accounts", "/%6dcp", "/mcp/")]])
    finally:
        for server in servers:
            server.shutdown()
