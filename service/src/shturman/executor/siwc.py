"""Вход через ChatGPT («Sign in with ChatGPT») и токены подписки для своей модели сервиса.

Что это. С 29 сентября 2026 OpenAI в предварительном режиме разрешает открытым программам,
которые человек ставит себе сам, обращаться к модели за счёт его подписки ChatGPT (Plus, Pro)
вместо ключа API. Документация: developers.openai.com/siwc/token-sharing-open-source (сверено
8 октября 2026, текст сохранён рядом с разработкой; на настоящем сервисе OpenAI этот код не
запускался). Здесь — вход, хранение и обновление токенов; запросы к модели — `subscription.py`.

Вход (OAuth 2.0 с PKCE, публичный клиент без секрета):
  * первый раз `client_id=dynamic_agent_client` и `agent_name_hint=Shturman`: OpenAI регистрирует
    клиента для этого человека и в ответе возвращает выданный `client_id` (вида `oaiapp_…`);
    дальше вход идёт с ним, а `agent_name_hint` не передаётся;
  * `ext_agent_host_id` — постоянный непрозрачный номер этого сервера (`urn:uuid:…`): создаётся
    до первого входа, лежит отдельным файлом и переживает выход из ChatGPT;
  * адрес возврата — `http://127.0.0.1:1455/auth/callback`, одинаковый в запросе входа и в обмене кода.

Сервер владельца удалён от его браузера, и адрес 127.0.0.1 в браузере ведёт на компьютер
владельца, а не на сервер. Поэтому вход идёт «вставкой адреса»: браузер после разрешения
доступа открывает 127.0.0.1 и показывает ошибку «не удаётся открыть страницу» — владелец копирует
адрес из адресной строки и вставляет его на странице настройки (`parse_callback`, `check_callback`).
Документация описывает для удалённой машины другой путь (вход на своём компьютере и перенос
файла по SSH); вставка адреса соблюдает все её правила, но в ней не описана: решение и риск —
docs/decisions.md, Р-64.

Токены: `access_token` живёт час, `refresh_token` — 30 дней и меняется при каждом обновлении.
Обновление — под одной блокировкой процесса (`TokenKeeper`); новый `refresh_token` записывается
на диск раньше, чем используется новый `access_token`. Ответы, после которых токен больше не
годится (`invalid_grant`, `refresh_token_expired` и подобные), переводят подписку в «нужно войти
заново»: токены стираются, выданный `client_id` и номер сервера остаются. Сбой сети и ответы 5xx
токенов не трогают.

Файлы (каталог `<каталог данных>/setup/`, права 700; файлы — 600, запись через временный файл):
  chatgpt.json  — учётная запись входа: почта, `sub`, выданный `client_id`, токены, срок, права,
                  выбранная модель и список моделей;
  chatgpt-host  — номер сервера `ext_agent_host_id`.
Каталог данных сервиса контейнеру Hermes не подключён; ни один маршрут файлы не отдаёт. Страница
настройки получает только состояние, почту и модель — токенов не получает никто.

В журнал не попадают ни токены, ни код входа, ни вставленный адрес, ни адрес входа (в нём
`id_token_hint`): только коды ошибок и номер запроса OpenAI.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import stat
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx

from ..setup_page.secrets_store import _write_private

logger = logging.getLogger("shturman.executor.chatgpt")

ISSUER = "https://auth.openai.com"
AUTH_BASE = "https://auth.openai.com"
API_BASE = "https://api.openai.com/v1"
AUTHORIZE_URL = AUTH_BASE + "/api/accounts/authorize"
TOKEN_URL = AUTH_BASE + "/api/accounts/oauth/token"
REVOKE_URL = AUTH_BASE + "/api/accounts/oauth/revoke"
JWKS_URL = AUTH_BASE + "/.well-known/jwks.json"
REDIRECT_URI = "http://127.0.0.1:1455/auth/callback"
CALLBACK_PATH = "/auth/callback"
RESOURCE = "https://api.openai.com/v1"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = "openid profile email offline_access resource.invoke " + PLAN_SCOPE
DYNAMIC_CLIENT = "dynamic_agent_client"
AGENT_NAME = "Shturman"
USAGE_URL = "https://chatgpt.com/settings/usage"

ATTEMPT_TTL = 600            # секунд живёт попытка входа
ATTEMPT_TRIES = 5            # столько чужих адресов можно вставить, потом попытка снимается
REFRESH_MARGIN = 300         # обновлять, когда до конца жизни access_token осталось меньше
ID_TOKEN_LEEWAY = 120        # секунд расхождения часов при проверке ID token
MAX_PASTE = 8192

# Ответы на обновление, после которых refresh_token больше не годится (раздел Refresh errors).
TERMINAL_REFRESH = frozenset({
    "invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
    "refresh_token_invalidated", "refresh_token_reused", "invalid_client",
})

# Состояния записи входа.
ACTIVE, RELOGIN, SIGNED_OUT = "active", "relogin", "signed_out"

_CLIENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
_HOST_ID = re.compile(r"^urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")
_SAFE = re.compile(r"[^A-Za-z0-9_.:-]")


class SiwcError(Exception):
    """Вход или обмен токенов не удался. `code` — короткий код без значений; текст для
    владельца подбирает страница настройки (`setup_page/apply.py`)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class NeedLogin(SiwcError):
    """Токенов нет или они больше не годятся: нужно войти через ChatGPT заново."""


class Transient(SiwcError):
    """Временный сбой (сеть, 5xx): токены не тронуты, можно повторить позже."""


def safe_code(value: Any, limit: int = 60) -> str:
    """Код ошибки от OpenAI в виде, пригодном для журнала и для страницы."""
    return _SAFE.sub("", str(value or ""))[:limit]


def _now() -> float:
    return time.time()


# --- файлы ---------------------------------------------------------------------------------------

def _directory(data_dir: Path) -> Path:
    return Path(data_dir) / "setup"


_STR_FIELDS = ("email", "issuer", "subject", "client_id", "ext_agent_host_id", "id_token", "access_token",
               "refresh_token", "token_type", "saved_at", "model", "status")


class CredentialStore:
    """Запись входа через ChatGPT (`chatgpt.json`) и номер сервера (`chatgpt-host`)."""

    FILE, HOST = "chatgpt.json", "chatgpt-host"

    def __init__(self, data_dir: Path) -> None:
        self.dir = _directory(data_dir)
        self.path = self.dir / self.FILE
        self.host_path = self.dir / self.HOST

    def __repr__(self) -> str:
        return f"<CredentialStore {self.status()}>"

    __str__ = __repr__

    def _read(self, path: Path) -> bytes | None:
        try:
            info = path.stat()
        except OSError:
            return None
        if stat.S_IMODE(info.st_mode) & 0o077:
            try:
                os.chmod(path, 0o600)
                logger.warning("файл входа через ChatGPT был доступен не только владельцу: права исправлены")
            except OSError:
                logger.error("файл входа через ChatGPT доступен не только владельцу, исправить права не удалось")
        try:
            return path.read_bytes()
        except OSError:
            return None

    def load(self) -> dict[str, Any]:
        """Запись входа. Нет файла или он испорчен — пусто: сервис обязан запуститься и без него."""
        raw = self._read(self.path)
        if raw is None:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            logger.error("файл входа через ChatGPT не читается: подписка не применяется")
            return {}
        if not isinstance(data, dict):
            return {}
        out: dict[str, Any] = {k: data[k] for k in _STR_FIELDS if isinstance(data.get(k), str) and data[k]}
        for key in ("expires_at", "earliest_refresh_at"):
            value = data.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[key] = float(value)
        if isinstance(data.get("scopes"), list):
            out["scopes"] = [s for s in data["scopes"] if isinstance(s, str)]
        if isinstance(data.get("models"), list):
            out["models"] = [{"slug": m["slug"], "display_name": str(m.get("display_name") or m["slug"])[:120]}
                             for m in data["models"]
                             if isinstance(m, dict) and isinstance(m.get("slug"), str) and _SLUG.match(m["slug"])][:100]
        if data.get("need_consent") is True:
            out["need_consent"] = True
        return out

    def save(self, record: dict[str, Any]) -> None:
        body = json.dumps({k: v for k, v in record.items() if v is not None}, ensure_ascii=False, sort_keys=True)
        _write_private(self.path, body.encode("utf-8"))

    def status(self) -> str:
        """none — входа не было; active — подписка работает моделью сервиса; relogin — нужно войти
        заново; signed_out — вышли (выданный client_id сохранён)."""
        record = self.load()
        if not record.get("client_id"):
            return "none"
        status = record.get("status")
        if status == ACTIVE and not record.get("refresh_token"):
            return RELOGIN
        return status if status in (ACTIVE, RELOGIN, SIGNED_OUT) else SIGNED_OUT

    def active(self) -> bool:
        """Подписка выбрана моделью сервиса — работает или ждёт повторного входа."""
        return self.status() in (ACTIVE, RELOGIN)

    def host_id(self) -> str:
        """Номер этого сервера для OpenAI. Создаётся один раз и переживает выход из ChatGPT."""
        raw = self._read(self.host_path)
        value = raw.decode("ascii", "replace").strip() if raw else ""
        if _HOST_ID.match(value):
            return value
        value = f"urn:uuid:{uuid.uuid4()}"
        _write_private(self.host_path, value.encode("ascii"))
        return value

    def drop_tokens(self, status: str, *, keep_id_token: bool) -> None:
        """Стирает токены, оставляя регистрацию (client_id, почта, sub, модель)."""
        record = self.load()
        if not record:
            return
        for key in ("access_token", "refresh_token", "expires_at", "earliest_refresh_at"):
            record.pop(key, None)
        if not keep_id_token:
            record.pop("id_token", None)
        record["status"] = status
        self.save(record)

    def forget(self) -> None:
        """Убирает запись входа целиком. Номер сервера остаётся."""
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


# --- попытка входа -------------------------------------------------------------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


@dataclass
class Attempt:
    """Одна попытка входа. Держится в памяти сервиса; перезапуск её снимает."""
    state: str = field(repr=False)
    nonce: str = field(repr=False)
    verifier: str = field(repr=False)
    host_id: str
    client_id: str | None          # None — первая регистрация (dynamic_agent_client)
    subject: str | None            # чья учётная запись ожидается при повторном входе
    switch: bool = False           # владелец согласился, что подписка заменит ключ API
    created: float = field(default_factory=_now)
    tries: int = 0

    @property
    def expires_at(self) -> float:
        return self.created + ATTEMPT_TTL

    def expired(self, now: float | None = None) -> bool:
        return (now if now is not None else _now()) >= self.expires_at


def new_attempt(store: CredentialStore, *, new_registration: bool = False,
                switch: bool = False) -> tuple[Attempt, str]:
    """Попытка входа и адрес, который страница откроет в браузере владельца."""
    record = store.load()
    host = store.host_id()
    client_id = None if new_registration else record.get("client_id")
    verifier = _b64url(secrets.token_bytes(48))
    attempt = Attempt(state=_b64url(secrets.token_bytes(24)), nonce=_b64url(secrets.token_bytes(24)),
                      verifier=verifier, host_id=host, client_id=client_id,
                      subject=record.get("subject") if client_id else None, switch=switch)
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": client_id or DYNAMIC_CLIENT,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "resource": RESOURCE,
        "state": attempt.state,
        "nonce": attempt.nonce,
        "code_challenge": _b64url(hashlib.sha256(verifier.encode("ascii")).digest()),
        "code_challenge_method": "S256",
        "ext_agent_host_id": host,
    }
    if client_id is None:
        params["agent_name_hint"] = AGENT_NAME
    else:
        if record.get("id_token"):
            params["id_token_hint"] = record["id_token"]
        if record.get("email"):
            params["login_hint"] = record["email"]
        if record.get("need_consent"):
            # Прошлый вход прошёл без разрешения пользоваться подпиской: просим заново.
            params["prompt"] = "consent"
    return attempt, AUTHORIZE_URL + "?" + urlencode(params, quote_via=quote)


def parse_callback(raw: Any) -> dict[str, str]:
    """Параметры из вставленного адреса: целиком (http://127.0.0.1:1455/auth/callback?…),
    только его часть после «?» или сама строка параметров."""
    if not isinstance(raw, str):
        raise SiwcError("empty")
    text = raw.strip()
    if not text:
        raise SiwcError("empty")
    if len(text) > MAX_PASTE or any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise SiwcError("bad_address")
    if "://" in text:
        try:
            parts = urlsplit(text)
        except ValueError:
            raise SiwcError("bad_address") from None
        if parts.path.rstrip("/") != CALLBACK_PATH:
            raise SiwcError("not_callback")
        query = parts.query
    else:
        query = text.split("?", 1)[1] if "?" in text else text
        query = query.split("#", 1)[0]
    if not query:
        raise SiwcError("no_params")
    try:
        found = parse_qs(query, keep_blank_values=False, max_num_fields=30)
    except ValueError:
        raise SiwcError("bad_address") from None
    out: dict[str, str] = {}
    for key, values in found.items():
        if len(values) != 1:
            raise SiwcError("bad_address")       # повторённый параметр — адрес собран не браузером
        out[key] = values[0]
    if "state" not in out and "code" not in out and "error" not in out:
        raise SiwcError("no_params")
    return out


def check_callback(attempt: Attempt | None, params: dict[str, str], *, now: float | None = None) -> tuple[str, str]:
    """Проверяет параметры возврата по попытке. Возвращает (код, выданный client_id).

    Порядок — как в документации: сначала state, затем ошибка OAuth, затем client_id."""
    if attempt is None:
        raise SiwcError("no_attempt")
    if attempt.expired(now):
        raise SiwcError("expired")
    if not hmac.compare_digest(params.get("state", "").encode("utf-8"), attempt.state.encode("utf-8")):
        attempt.tries += 1
        raise SiwcError("wrong_state")
    error = params.get("error")
    if error:
        raise SiwcError("denied" if error == "access_denied" else "oauth_error:" + safe_code(error, 40))
    code = params.get("code", "")
    if not code or len(code) > 2048:
        raise SiwcError("no_code")
    returned = params.get("client_id")
    if attempt.client_id is None:
        # Первая регистрация: без выданного client_id регистрация не завершена.
        if not returned or returned == DYNAMIC_CLIENT:
            raise SiwcError("no_client_id")
        if not _CLIENT_ID.match(returned):
            raise SiwcError("bad_client_id")
        return code, returned
    if returned is not None and returned != attempt.client_id:
        raise SiwcError("client_mismatch")
    return code, attempt.client_id


# --- HTTP ---------------------------------------------------------------------------------------

def http_client(*, proxy_url: str = "", transport: httpx.AsyncBaseTransport | None = None,
                timeout: float = 30.0, base_url: str = "") -> httpx.AsyncClient:
    """Клиент к OpenAI: без перенаправлений (иначе токен ушёл бы по чужому адресу) и без
    переменных окружения прокси — только `EGRESS_PROXY_URL` сервиса."""
    if transport is None:
        try:
            transport = httpx.AsyncHTTPTransport(proxy=proxy_url or None, trust_env=False, retries=0)
        except ImportError:
            raise SiwcError("proxy_needs_socksio") from None
        except Exception:  # noqa: BLE001
            raise SiwcError("bad_proxy_url") from None
    return httpx.AsyncClient(transport=transport, base_url=base_url, trust_env=False, follow_redirects=False,
                             timeout=httpx.Timeout(timeout, connect=10.0, pool=timeout))


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _oauth_error(data: dict[str, Any]) -> str:
    error = data.get("error")
    if isinstance(error, dict):
        return safe_code(error.get("code") or error.get("type"))
    return safe_code(error or data.get("code"))


async def _token_request(http: httpx.AsyncClient, form: dict[str, str]) -> tuple[int, dict[str, Any], str]:
    try:
        response = await http.post(TOKEN_URL, data=form, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise Transient("no_connection:" + type(exc).__name__) from None
    return response.status_code, _json(response), safe_code(response.headers.get("x-request-id"), 80)


def _scopes(value: Any) -> list[str]:
    return sorted(set(value.split())) if isinstance(value, str) else []


async def exchange(http: httpx.AsyncClient, attempt: Attempt, code: str, client_id: str) -> dict[str, Any]:
    """Обмен кода на токены. Возвращает ответ сервера (токены не проверяются здесь)."""
    status, data, rid = await _token_request(http, {
        "grant_type": "authorization_code", "client_id": client_id, "code": code,
        "code_verifier": attempt.verifier, "redirect_uri": REDIRECT_URI, "resource": RESOURCE,
    })
    if status != 200:
        error = _oauth_error(data)
        logger.warning("вход через ChatGPT: обмен кода отклонён (%s %s, запрос %s)", status, error or "-", rid or "-")
        if error == "invalid_grant":
            raise SiwcError("code_rejected")
        if status >= 500:
            raise Transient(f"http_{status}")
        raise SiwcError(f"token_error:{error or status}")
    for key in ("access_token", "refresh_token", "id_token"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise SiwcError("bad_token_response")
    return data


def _jwt_part(token: str, index: int) -> dict[str, Any]:
    try:
        part = token.split(".")[index]
        raw = base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
        data = json.loads(raw)
    except (IndexError, ValueError, UnicodeDecodeError):
        raise SiwcError("bad_id_token") from None
    if not isinstance(data, dict):
        raise SiwcError("bad_id_token")
    return data


async def _verify_signature(http: httpx.AsyncClient, id_token: str, client_id: str) -> bool:
    """Подпись ID token по ключам OpenAI (JWKS). True — проверена; False — ключи получить не
    удалось (тогда остаётся проверка по TLS: токен пришёл прямо от сервера токенов, OIDC Core
    3.1.3.7). Неверная подпись при полученных ключах — отказ."""
    try:
        import jwt  # PyJWT с cryptography
    except ImportError:
        logger.warning("вход через ChatGPT: подпись ID token не проверена — нет библиотеки PyJWT")
        return False
    try:
        response = await http.get(JWKS_URL, headers={"Accept": "application/json"})
    except httpx.HTTPError:
        response = None
    keys = _json(response).get("keys") if response is not None and response.status_code == 200 else None
    if not isinstance(keys, list):
        logger.warning("вход через ChatGPT: ключи подписи OpenAI не получены, подпись ID token не проверена")
        return False
    kid = _jwt_part(id_token, 0).get("kid")
    candidates = [k for k in keys if isinstance(k, dict) and (kid is None or k.get("kid") == kid)]
    for candidate in candidates:
        try:
            key = jwt.PyJWK(candidate).key
            jwt.decode(id_token, key=key, algorithms=["RS256"], audience=client_id, issuer=ISSUER,
                       leeway=ID_TOKEN_LEEWAY, options={"require": ["exp", "iss", "aud", "sub"]})
            return True
        except jwt.PyJWTError:
            continue
        except Exception:  # noqa: BLE001 — ключ неизвестного вида
            continue
    raise SiwcError("bad_id_token_signature")


async def verify_id_token(http: httpx.AsyncClient, id_token: str, *, client_id: str, nonce: str,
                          now: float | None = None) -> dict[str, Any]:
    """Проверяет ID token: издатель, получатель, срок, nonce этой попытки, подпись.
    Возвращает его утверждения (`sub`, `email` и прочее)."""
    claims = _jwt_part(id_token, 1)
    moment = now if now is not None else _now()
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    exp = claims.get("exp")
    if claims.get("iss") != ISSUER:
        raise SiwcError("id_token_issuer")
    if client_id not in audiences:
        raise SiwcError("id_token_audience")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool) or exp + ID_TOKEN_LEEWAY < moment:
        raise SiwcError("id_token_expired")
    if not isinstance(claims.get("nonce"), str) or not hmac.compare_digest(claims["nonce"].encode(), nonce.encode()):
        raise SiwcError("id_token_nonce")
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise SiwcError("bad_id_token")
    claims["_signature_checked"] = await _verify_signature(http, id_token, client_id)
    return claims


def record_from_tokens(tokens: dict[str, Any], claims: dict[str, Any], *, client_id: str, host_id: str,
                       previous: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """Запись входа из ответа сервера токенов и проверенного ID token."""
    moment = now if now is not None else _now()
    expires_in = tokens.get("expires_in")
    expires_in = float(expires_in) if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool) else 3600.0
    record = {
        "email": claims.get("email") if isinstance(claims.get("email"), str) else None,
        "issuer": ISSUER, "subject": claims["sub"], "client_id": client_id, "ext_agent_host_id": host_id,
        "id_token": tokens["id_token"], "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"], "token_type": "Bearer",
        "expires_at": moment + expires_in, "earliest_refresh_at": _earliest(tokens.get("earliest_refresh_at")),
        "scopes": _scopes(tokens.get("scope")),
        "saved_at": datetime.fromtimestamp(moment, timezone.utc).isoformat(),
        "model": previous.get("model") if previous.get("client_id") == client_id else None,
        "models": previous.get("models") if previous.get("client_id") == client_id else None,
        "status": ACTIVE,
    }
    return record


def _earliest(value: Any) -> float | None:
    """`earliest_refresh_at`: смысл в документации не описан; принимаем число секунд Unix
    или строку ISO 8601 и не обновляем токен раньше этого срока, пока он действует."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def plan_allowed(tokens_or_record: dict[str, Any]) -> bool:
    scopes = tokens_or_record.get("scopes")
    if scopes is None:
        scopes = _scopes(tokens_or_record.get("scope"))
    return PLAN_SCOPE in scopes


async def revoke(http: httpx.AsyncClient, record: dict[str, Any], *,
                 sleep: Any = asyncio.sleep) -> bool:
    """Завершает обновляемую сессию у OpenAI. True — OpenAI подтвердил (пустой ответ 200,
    в том числе для уже недействительного токена)."""
    token, client_id = record.get("refresh_token"), record.get("client_id")
    if not token or not client_id:
        return True
    for pause in (0.0, 1.0, 3.0):
        if pause:
            await sleep(pause)
        try:
            response = await http.post(REVOKE_URL, data={"token": token, "token_type_hint": "refresh_token",
                                                         "client_id": client_id})
        except httpx.HTTPError:
            continue
        if response.status_code == 200:
            return True
        if response.status_code < 500:
            logger.warning("выход из ChatGPT: отзыв сессии отклонён (%s)", response.status_code)
            return False
    return False


async def list_models(http: httpx.AsyncClient, access_token: str) -> list[dict[str, str]]:
    """Модели, доступные этой учётной записи, в порядке сервера (только `visibility == "list"`)."""
    try:
        response = await http.get(API_BASE + "/models", headers={"Authorization": f"Bearer {access_token}"})
    except httpx.HTTPError as exc:
        raise Transient("no_connection:" + type(exc).__name__) from None
    if response.status_code != 200:
        data = _json(response)
        error = data.get("error") if isinstance(data.get("error"), dict) else {}
        code = safe_code(error.get("code")) if error else ""
        logger.warning("подписка ChatGPT: список моделей не получен (%s %s, запрос %s)", response.status_code,
                       code or "-", safe_code(response.headers.get("x-request-id"), 80) or "-")
        if response.status_code >= 500:
            raise Transient(f"http_{response.status_code}")
        raise SiwcError(f"models_{response.status_code}" + (f":{code}" if code else ""))
    models = _json(response).get("models")
    out = []
    for item in models if isinstance(models, list) else []:
        if not isinstance(item, dict) or item.get("visibility") != "list":
            continue
        slug = item.get("slug")
        if isinstance(slug, str) and _SLUG.match(slug):
            name = item.get("display_name") if isinstance(item.get("display_name"), str) else slug
            out.append({"slug": slug, "display_name": name.strip()[:120] or slug})
    return out[:100]


# --- обновление токенов --------------------------------------------------------------------------

class TokenKeeper:
    """Действующий access_token подписки. Одна на процесс (`executor.service.chatgpt_keeper`):
    ею пользуются и исполнитель заданий, и страница настройки, поэтому два обновления одного
    refresh_token одновременно не идут никогда (иначе второе получило бы refresh_token_reused
    и вход пришлось бы повторять)."""

    def __init__(self, store: CredentialStore, *, proxy_url: str = "",
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.store = store
        self.proxy_url = proxy_url
        self.transport = transport
        self.lock = asyncio.Lock()
        self.refreshes = 0
        self.last_error: str | None = None

    def __repr__(self) -> str:
        return f"<TokenKeeper {self.store.status()}>"

    def http(self, *, timeout: float = 30.0) -> httpx.AsyncClient:
        return http_client(proxy_url=self.proxy_url, transport=self.transport, timeout=timeout)

    async def access_token(self, *, failed: str | None = None) -> str:
        """Действующий access_token. failed — токен, который сервер только что отверг: тогда
        он обновляется, если его ещё не обновил соседний запрос."""
        async with self.lock:
            record = self.store.load()
            if record.get("status") != ACTIVE or not record.get("refresh_token") or not record.get("client_id"):
                raise NeedLogin("relogin")
            access = record.get("access_token") or ""
            now = _now()
            expires = record.get("expires_at") or 0.0
            if failed is not None:
                due = failed == access
            else:
                due = not access or now >= expires - REFRESH_MARGIN
                earliest = record.get("earliest_refresh_at")
                if due and access and now < expires and earliest and now < earliest:
                    due = False      # сервер просил не обновлять раньше срока, а токен ещё действует
            if not due:
                return access
            record = await self._refresh(record)
            return record["access_token"]

    async def _refresh(self, record: dict[str, Any]) -> dict[str, Any]:
        async with self.http() as http:
            status, data, rid = await _token_request(http, {
                "grant_type": "refresh_token", "client_id": record["client_id"],
                "refresh_token": record["refresh_token"], "resource": RESOURCE,
            })
        if status == 200 and isinstance(data.get("access_token"), str) and data["access_token"]:
            now = _now()
            expires_in = data.get("expires_in")
            expires_in = float(expires_in) if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool) else 3600.0
            fresh = dict(record)
            fresh["access_token"] = data["access_token"]
            if isinstance(data.get("refresh_token"), str) and data["refresh_token"]:
                fresh["refresh_token"] = data["refresh_token"]
            if isinstance(data.get("id_token"), str) and data["id_token"]:
                fresh["id_token"] = data["id_token"]
            fresh["expires_at"] = now + expires_in
            fresh["earliest_refresh_at"] = _earliest(data.get("earliest_refresh_at"))
            if isinstance(data.get("scope"), str):
                fresh["scopes"] = _scopes(data["scope"])
            fresh["saved_at"] = datetime.fromtimestamp(now, timezone.utc).isoformat()
            # Новый refresh_token — на диск раньше, чем пойдёт в дело новый access_token:
            # прежний после этого обмена уже недействителен.
            self.store.save(fresh)
            self.refreshes += 1
            self.last_error = None
            if not plan_allowed(fresh):
                self._relogin(need_consent=True)
                raise NeedLogin("no_plan_scope")
            return fresh
        error = _oauth_error(data)
        logger.warning("подписка ChatGPT: обновление токена не удалось (%s %s, запрос %s)",
                       status, error or "-", rid or "-")
        if error in TERMINAL_REFRESH or status == 401:
            self._relogin()
            raise NeedLogin(error or "relogin")
        self.last_error = f"refresh_{status}"
        raise Transient(f"refresh_{status}" + (f":{error}" if error else ""))

    def _relogin(self, *, need_consent: bool = False) -> None:
        self.last_error = "relogin"
        self.store.drop_tokens(RELOGIN, keep_id_token=True)
        if need_consent:
            record = self.store.load()
            record["need_consent"] = True
            self.store.save(record)

    async def mark_relogin(self) -> None:
        """Сервер модели подтвердил, что токен больше не действует (после обновления — снова 401)."""
        async with self.lock:
            if self.store.status() == ACTIVE:
                self._relogin()

    async def sign_out(self) -> bool:
        """Выход: отзыв сессии у OpenAI, затем токены стираются. Регистрация и номер сервера
        остаются. Возвращает, подтвердил ли OpenAI отзыв."""
        async with self.lock:
            record = self.store.load()
            if not record:
                return True
            confirmed = True
            if record.get("refresh_token"):
                try:
                    async with self.http() as http:
                        confirmed = await revoke(http, record)
                except SiwcError:
                    confirmed = False
            self.store.drop_tokens(SIGNED_OUT, keep_id_token=False)
            return confirmed

    async def save_signed_in(self, record: dict[str, Any]) -> None:
        """Записывает новую учётную запись входа под той же блокировкой, что и обновление."""
        async with self.lock:
            self.store.save(record)
