"""Подписанные токены сессии и хеши одноразовых значений.

Формат токена повторяет встроенный парольный вход Hermes: base64url(JSON + HMAC-SHA256).
Сервер ничего не хранит о выданных сессиях, проверка — одна операция HMAC на запрос.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Callable, Optional

_SIG_LEN = hashlib.sha256().digest_size


def sign(payload: dict[str, Any], secret: bytes) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    sig = hmac.new(secret, raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw + sig).decode()


def unsign(
    token: str, secret: bytes, kind: str, *, now: Callable[[], float] = time.time
) -> Optional[dict[str, Any]]:
    """Возвращает содержимое токена либо None: подпись не сошлась, не тот вид, истёк срок."""
    try:
        blob = base64.urlsafe_b64decode(token.encode())
        if len(blob) <= _SIG_LEN:
            return None
        raw, sig = blob[:-_SIG_LEN], blob[-_SIG_LEN:]
        expected = hmac.new(secret, raw, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(raw)
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("kind") != kind:
        return None
    try:
        if int(payload.get("exp", 0)) <= int(now()):
            return None
    except (TypeError, ValueError):
        return None
    return payload


def digest(value: str, secret: bytes) -> str:
    """Хеш одноразового значения (кода, ссылки). В файлах состояния лежит только он."""
    return hmac.new(secret, value.encode(), hashlib.sha256).hexdigest()


def same(value: str, stored_digest: str, secret: bytes) -> bool:
    if not value or not stored_digest:
        return False
    return hmac.compare_digest(digest(value, secret), stored_digest)
