"""Server-established owner authority, independent of agent text and API tokens.

Only the verified private control-bot path, the approvals-bot Mini App after initData
verification and authenticated setup-page handlers enter this context. It is process-local; there is no HTTP method for creating a principal.
An actor string or model result never establishes owner authority.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class AuthorityPrincipal:
    subject: str
    source: str
    user_id: int | None = None
    chat_id: int | None = None
    action: str | None = None


_owner: contextvars.ContextVar[AuthorityPrincipal | None] = contextvars.ContextVar(
    "shturman_verified_owner", default=None)


def get_owner_principal() -> AuthorityPrincipal | None:
    return _owner.get()


def current_owner_id() -> int | str | None:
    principal = _owner.get()
    return (principal.user_id if principal.user_id is not None else principal.subject) if principal else None


def current_owner_chat() -> int | None:
    principal = _owner.get()
    return principal.chat_id if principal else None


def is_owner() -> bool:
    return _owner.get() is not None


# Other transient, press-scoped permissions (e.g. the armed owner-account draft write in tg/client.py)
# register here so a spawned background task never inherits them either.
_transient: list[contextvars.ContextVar] = []


def register_transient(var: contextvars.ContextVar) -> None:
    if var not in _transient:
        _transient.append(var)


def background_context() -> contextvars.Context:
    """A spawned background task does not inherit an owner's transient authority."""
    context = contextvars.copy_context()
    context.run(_owner.set, None)
    for var in _transient:
        context.run(var.set, None)
    return context


def requires_owner() -> AuthorityPrincipal:
    principal = _owner.get()
    if principal is None:
        from .confirm import Refused
        raise Refused("Это решение принимает владелец в своём управляющем чате.", 403, "owner_required")
    return principal


@contextmanager
def owner_context(user_id: int, *, chat_id: int | None = None,
                  action: str | None = None) -> Iterator[AuthorityPrincipal]:
    """Enter only after checking the real Telegram sender and private owner chat."""
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        raise ValueError("owner user ID must be a positive Telegram ID")
    principal = AuthorityPrincipal(f"telegram:{user_id}", "telegram", user_id, chat_id, action)
    token = _owner.set(principal)
    try:
        yield principal
    finally:
        _owner.reset(token)


@contextmanager
def miniapp_context(user_id: int, *, chat_id: int) -> Iterator[AuthorityPrincipal]:
    """Mini App of the approvals bot (setup_page/miniapp.py, approvals.py).

    Enter only after the app session was issued for initData whose HMAC was checked with the
    approvals bot token and whose user equals the owner bound to that bot. The source stays
    "telegram": as with a callback query, Telegram itself vouches for who the user is, signing
    for the service's own bot. The action marks the path for logs and reviewers."""
    with owner_context(user_id, chat_id=chat_id, action="telegram.miniapp") as principal:
        yield principal


@contextmanager
def setup_context(session_id: int | str, *, action: str | None = None) -> Iterator[AuthorityPrincipal]:
    """Enter only after independently authenticating the setup-page session."""
    principal = AuthorityPrincipal(f"setup:{session_id}", "setup", action=action)
    token = _owner.set(principal)
    try:
        yield principal
    finally:
        _owner.reset(token)
