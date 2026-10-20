"""Ownership binding for chat sessions.

A ``conversation_id`` is a bearer-less integer: knowing it must never be enough
to read or continue somebody else's conversation. Every session is therefore
bound to an *owner* that the server itself can verify:

* anonymous web visitors -> a server-signed cookie (never the IP, which is
  spoofable and shared);
* logged-in web users    -> their user id;
* WhatsApp               -> the phone number delivered by the (signed) webhook.

Only an HMAC of the owner is persisted (``ChatSession.owner_hash``), so the raw
cookie token / phone number never reaches the database or the traces.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import Optional, Tuple

from flask import current_app, request
from itsdangerous import BadSignature, URLSafeSerializer

OWNER_COOKIE_NAME = "klara_owner"
OWNER_COOKIE_MAX_AGE = 60 * 60 * 24 * 365
_COOKIE_SALT = "klara-session-owner"
_HASH_CONTEXT = b"klara-session-owner-hash:v1"


def _secret() -> bytes:
    key = None
    try:
        key = current_app.config.get("SECRET_KEY")
    except RuntimeError:  # no application context
        key = None
    key = key or os.getenv("SECRET_KEY") or "dev-secret"
    return key.encode("utf-8") if isinstance(key, str) else bytes(key)


def owner_hash(kind: str, identifier: str) -> Optional[str]:
    """Stable, non-reversible owner fingerprint, namespaced by ``kind``."""
    kind = (kind or "").strip()
    identifier = str(identifier or "").strip()
    if not kind or not identifier:
        return None
    digest = hmac.new(
        _secret(), _HASH_CONTEXT + b"|" + kind.encode() + b"|" + identifier.encode(), hashlib.sha256
    )
    return digest.hexdigest()


def whatsapp_owner_hash(phone_number: str) -> Optional[str]:
    return owner_hash("whatsapp", phone_number)


def user_owner_hash(user_id) -> Optional[str]:
    return owner_hash("user", str(user_id))


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(_secret(), salt=_COOKIE_SALT)


def issue_visitor_token() -> Tuple[str, str]:
    """Return ``(raw_token, signed_cookie_value)`` for a brand-new visitor."""
    token = secrets.token_urlsafe(24)
    return token, _serializer().dumps(token)


def read_visitor_token(cookie_value: Optional[str]) -> Optional[str]:
    """Return the raw token when ``cookie_value`` carries a valid server signature."""
    if not cookie_value:
        return None
    try:
        token = _serializer().loads(cookie_value)
    except BadSignature:
        return None
    return token if isinstance(token, str) and token else None


def resolve_web_owner(user=None) -> Tuple[Optional[str], Optional[str]]:
    """Resolve the owner of the current web request.

    Returns ``(owner_hash, cookie_to_set)``. ``cookie_to_set`` is a signed cookie
    value that the caller must attach to the response when the visitor had no
    valid cookie yet; otherwise ``None``.
    """
    if user is not None and getattr(user, "is_authenticated", False):
        return user_owner_hash(getattr(user, "id", None)), None

    token = read_visitor_token(request.cookies.get(OWNER_COOKIE_NAME))
    if token is not None:
        return owner_hash("visitor", token), None

    token, signed = issue_visitor_token()
    return owner_hash("visitor", token), signed


def attach_owner_cookie(response, cookie_value: Optional[str]):
    """Set the signed owner cookie on ``response`` (no-op when ``cookie_value`` is falsy)."""
    if not cookie_value:
        return response
    secure = bool(request.is_secure)
    response.set_cookie(
        OWNER_COOKIE_NAME,
        cookie_value,
        max_age=OWNER_COOKIE_MAX_AGE,
        httponly=True,
        secure=secure,
        # The public report page may be embedded cross-site; SameSite=None
        # requires Secure, so fall back to Lax over plain HTTP (local dev).
        samesite="None" if secure else "Lax",
    )
    return response


def owners_match(stored: Optional[str], presented: Optional[str]) -> bool:
    """Constant-time comparison; both sides must be present."""
    if not stored or not presented:
        return False
    return hmac.compare_digest(str(stored), str(presented))
