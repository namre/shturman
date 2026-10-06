"""Способ входа в дашборд Hermes: ссылка активации и одноразовый код от бота владельца.

Hermes ведёт вход по схеме «ушёл на страницу входа — вернулся с кодом»:
  /auth/login  -> start_login()    -> страница входа «Штурмана» (статическая, отдаёт прокси)
  /auth/callback?code=…&state=…    -> complete_login() -> сессия

Код от бота отправляется в start_login: к моменту, когда страница открылась, сообщение уже ушло.
"""

from __future__ import annotations

import logging
import os
import secrets
from urllib.parse import urlencode

from hermes_cli.dashboard_auth import (
    DashboardAuthProvider,
    InvalidCodeError,
    LoginStart,
    RefreshExpiredError,
    Session,
)

from shturman_core import botapi
from shturman_core.auth import Auth
from shturman_core.state import Store

logger = logging.getLogger("shturman.auth")

_CALLBACK_SUFFIX = "/auth/callback"


def _bot_token() -> str:
    try:
        from hermes_cli.config import get_env_value

        return (get_env_value("TELEGRAM_BOT_TOKEN") or "").strip()
    except Exception:
        return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def _send_code(owner: dict, code: str) -> None:
    pretty = f"{code[:4]} {code[4:]}"
    botapi.send_message(
        _bot_token(), int(owner["chat_id"]),
        f"Код входа в Штурман: {pretty}\n\n"
        "Действует 5 минут. Никому его не пересылайте. "
        "Если вы сейчас не входите, ничего делать не нужно.",
    )


def _notify(owner: dict, text: str) -> None:
    botapi.send_message(_bot_token(), int(owner["chat_id"]), text)


def pages_prefix() -> str:
    """Путь, по которому обратный прокси отдаёт страницы входа из каталога public/."""
    value = os.environ.get("SHTURMAN_AUTH_PAGES", "/shturman-auth").strip() or "/shturman-auth"
    return "/" + value.strip("/")


class ShturmanAuthProvider(DashboardAuthProvider):
    name = "shturman"
    display_name = "Штурман"
    supports_password = False
    supports_token = False
    supports_session = True

    def __init__(self, auth: Auth | None = None) -> None:
        self.auth = auth or Auth(Store(), send_code=_send_code, notify=_notify)

    # --- вход ---

    def start_login(self, *, redirect_uri: str) -> LoginStart:
        state = secrets.token_urlsafe(24)
        status = self.auth.request_login_code()
        if status == "send_failed":
            logger.warning("shturman: код входа не отправлен — Telegram не принял сообщение")
        base = redirect_uri[: -len(_CALLBACK_SUFFIX)] if redirect_uri.endswith(_CALLBACK_SUFFIX) else ""
        query = {"state": state, "m": status}
        if status == "locked":
            query["wait"] = str(self.auth.login_lock_remaining())
        return LoginStart(
            redirect_url=f"{base}{pages_prefix()}/login.html?{urlencode(query)}",
            cookie_payload={"hermes_session_pkce": f"state={state};verifier=-"},
        )

    def complete_login(self, *, code: str, state: str, code_verifier: str, redirect_uri: str) -> Session:
        ok, reason = self.auth.complete(code)
        if not ok:
            raise InvalidCodeError(reason)
        return self._to_session(self.auth.mint_session())

    # --- сессия ---

    def verify_session(self, *, access_token: str):
        exp = self.auth.verify_access(access_token)
        if exp is None:
            return None
        owner = self.auth.owner() or {}
        return Session(
            user_id="owner", email="", display_name=str(owner.get("name") or "Владелец"),
            org_id="", provider=self.name, expires_at=exp, access_token=access_token,
            refresh_token="",
        )

    def refresh_session(self, *, refresh_token: str) -> Session:
        minted = self.auth.refresh(refresh_token) if refresh_token else None
        if minted is None:
            raise RefreshExpiredError("сессия истекла или отозвана")
        return self._to_session(minted)

    def revoke_session(self, *, refresh_token: str) -> None:
        return None

    def _to_session(self, minted) -> Session:
        return Session(
            user_id=minted.subject, email="", display_name=minted.display_name, org_id="",
            provider=self.name, expires_at=minted.expires_at,
            access_token=minted.access_token, refresh_token=minted.refresh_token,
        )


def build_provider() -> ShturmanAuthProvider:
    return ShturmanAuthProvider()
