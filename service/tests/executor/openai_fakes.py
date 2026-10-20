"""Подставной OpenAI для подписки ChatGPT: вход (auth.openai.com) и модель (api.openai.com).

Ведёт себя как настоящий в том, что проверяют тесты: сверяет PKCE, адрес возврата и client_id
при обмене кода, подписывает ID token своим ключом RSA и отдаёт ключ в JWKS, меняет refresh_token
при каждом обновлении и отвергает прежний (`refresh_token_reused`), отвечает на /v1/responses
потоком событий. В сеть ничего не уходит: клиенты получают `httpx.MockTransport`.

Значения-метки (`SENTINEL`) — по ним тесты ищут утечки токенов в журнале и ответах страницы.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

CLIENT_ID = "oaiapp_TestShturman01"
SUB = "user-SENTINEL-sub-0001"
EMAIL = "owner@example.com"
PLAN_SCOPES = "chatgpt.tokens.use.direct email offline_access openid profile resource.invoke"
MODELS = [
    {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1 Sol", "visibility": "list"},
    {"slug": "gpt-6.1-mini", "display_name": "GPT-6.1 mini", "visibility": "list"},
    {"slug": "internal-hidden", "display_name": "Скрытая", "visibility": "hide"},
]


def sse(*events: dict[str, Any]) -> bytes:
    out = []
    for event in events:
        out.append(f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n")
    return "".join(out).encode("utf-8")


def completed(text: str, model: str = "gpt-6.1-sol", *, deltas: bool = True) -> httpx.Response:
    events: list[dict[str, Any]] = [{"type": "response.created", "response": {"id": "resp_1", "model": model}}]
    if deltas:
        events += [{"type": "response.output_text.delta", "delta": ch} for ch in text]
    events.append({"type": "response.completed", "response": {
        "id": "resp_1", "model": model, "status": "completed",
        "output": [{"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": text}]}]}})
    return httpx.Response(200, content=sse(*events), headers={"content-type": "text/event-stream",
                                                                "x-request-id": "req_ok"})


def failed(code: str) -> httpx.Response:
    events = [{"type": "response.created", "response": {"id": "resp_2"}},
              {"type": "response.output_text.delta", "delta": "на"},
              {"type": "response.failed", "response": {"id": "resp_2", "status": "failed",
                                                        "error": {"code": code, "message": "nope"}}}]
    return httpx.Response(200, content=sse(*events), headers={"content-type": "text/event-stream",
                                                                "x-request-id": "req_failed"})


def api_error(status: int, code: str = "", param: str | None = None) -> httpx.Response:
    if not code:
        return httpx.Response(status, json={"detail": "direct route refused"}, headers={"x-request-id": "req_err"})
    return httpx.Response(status, json={"error": {"code": code, "param": param, "message": "nope",
                                                  "type": "invalid_request_error"}},
                          headers={"x-request-id": "req_err"})


class FakeOpenAI:
    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "kid-1"
        self.client_id = CLIENT_ID
        self.sub, self.email = SUB, EMAIL
        self.scope = PLAN_SCOPES
        self.models = [dict(m) for m in MODELS]
        self.answer = "да"
        self.codes: dict[str, dict[str, Any]] = {}
        self.access: set[str] = set()
        self.refresh: set[str] = set()
        self.used_refresh: set[str] = set()
        self.revoked: list[str] = []
        self.authorize_params: list[dict[str, str]] = []
        self.token_forms: list[dict[str, str]] = []
        self.responses: list[dict[str, Any]] = []
        self.response_headers: list[dict[str, str]] = []
        self.responses_script: list[Any] = []
        self.token_script: list[Any] = []
        self.jwks_down = False
        self.refresh_delay = 0.0
        self.counter = 0
        self.expires_in = 3600

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    # --- браузер владельца ---

    def authorize(self, url: str, *, deny: bool = False, with_client_id: bool | None = None,
                  extra: str = "") -> str:
        """Что увидит владелец в адресной строке после входа и «Разрешить»."""
        parts = urlsplit(url)
        assert parts.scheme == "https" and parts.netloc == "auth.openai.com" and parts.path == "/api/accounts/authorize"
        params = {k: v[0] for k, v in parse_qs(parts.query).items()}
        self.authorize_params.append(params)
        redirect = params["redirect_uri"]
        if deny:
            return f"{redirect}?error=access_denied&error_description=denied&state={params['state']}"
        first = params["client_id"] == "dynamic_agent_client"
        client_id = self.client_id if first else params["client_id"]
        self.counter += 1
        code = f"code-SENTINEL-{self.counter}"
        self.codes[code] = {"nonce": params["nonce"], "challenge": params["code_challenge"],
                            "client_id": client_id, "redirect_uri": redirect, "resource": params["resource"]}
        query = {"code": code, "scope": self.scope.replace(" ", "+"), "state": params["state"]}
        if with_client_id if with_client_id is not None else first:
            query["client_id"] = client_id
        return f"{redirect}?" + "&".join(f"{k}={v}" for k, v in query.items()) + extra

    # --- токены ---

    def id_token(self, *, client_id: str, nonce: str, key: Any = None, sub: str | None = None,
                 iss: str = "https://auth.openai.com", exp_in: int = 3600) -> str:
        now = int(time.time())
        claims = {"iss": iss, "aud": client_id, "sub": sub or self.sub, "email": self.email, "nonce": nonce,
                  "iat": now, "exp": now + exp_in}
        return jwt.encode(claims, key or self.key, algorithm="RS256", headers={"kid": self.kid})

    def issue(self, client_id: str, nonce: str | None) -> dict[str, Any]:
        self.counter += 1
        access, refresh = f"at-SENTINEL-{self.counter}", f"rt-SENTINEL-{self.counter}"
        self.access.add(access)
        self.refresh.add(refresh)
        out = {"access_token": access, "refresh_token": refresh, "token_type": "Bearer",
               "expires_in": self.expires_in, "scope": self.scope}
        if nonce is not None:
            out["id_token"] = self.id_token(client_id=client_id, nonce=nonce)
        return out

    def jwks(self) -> dict[str, Any]:
        public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        public.update(kid=self.kid, use="sig", alg="RS256")
        return {"keys": [public]}

    async def _token(self, form: dict[str, str]) -> httpx.Response:
        self.token_forms.append(form)
        if self.token_script:
            item = self.token_script.pop(0)
            if isinstance(item, Exception):
                raise item
            if item is not None:
                return item
        if form.get("grant_type") == "authorization_code":
            code = self.codes.pop(form.get("code", ""), None)
            if code is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            challenge = base64.urlsafe_b64encode(
                hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=").decode()
            assert challenge == code["challenge"], "PKCE не сошёлся"
            assert form["redirect_uri"] == code["redirect_uri"] and form["client_id"] == code["client_id"]
            assert form["resource"] == "https://api.openai.com/v1"
            return httpx.Response(200, json=self.issue(code["client_id"], code["nonce"]))
        if form.get("grant_type") == "refresh_token":
            if self.refresh_delay:
                await asyncio.sleep(self.refresh_delay)
            token = form.get("refresh_token", "")
            assert form.get("client_id") == self.client_id and "scope" not in form
            assert form.get("resource") == "https://api.openai.com/v1"
            if token in self.used_refresh:
                return httpx.Response(400, json={"error": "refresh_token_reused"})
            if token not in self.refresh:
                return httpx.Response(400, json={"error": "invalid_grant"})
            self.refresh.discard(token)
            self.used_refresh.add(token)
            out = self.issue(self.client_id, None)
            return httpx.Response(200, json=out)
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "auth.openai.com":
            if path == "/api/accounts/oauth/token":
                form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
                return await self._token(form)
            if path == "/api/accounts/oauth/revoke":
                form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
                self.revoked.append(form.get("token", ""))
                self.refresh.discard(form.get("token", ""))
                return httpx.Response(200)
            if path == "/.well-known/jwks.json":
                if self.jwks_down:
                    return httpx.Response(503)
                return httpx.Response(200, json=self.jwks())
        if host == "api.openai.com":
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            if path == "/v1/models":
                if token not in self.access:
                    return api_error(401, "subscription_sharing_invalid_user")
                return httpx.Response(200, json={"models": self.models})
            if path == "/v1/responses":
                body = json.loads(request.content)
                self.responses.append(body)
                self.response_headers.append(dict(request.headers))
                if self.responses_script:
                    item = self.responses_script.pop(0)
                    if isinstance(item, Exception):
                        raise item
                    if callable(item):
                        item = item(body)
                    if item is not None:
                        return item
                if token not in self.access:
                    return api_error(401, "subscription_sharing_invalid_user")
                return completed(self.answer, body["model"])
        return httpx.Response(404, json={"error": {"code": "not_found"}})


def form_of(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


__all__ = ["FakeOpenAI", "completed", "failed", "api_error", "sse", "form_of", "urlencode"]
