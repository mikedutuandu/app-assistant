"""Session tokens: short-lived HS256 tokens a host app mints for its signed-in user.

The bot verifies them with the app pack's secret and forwards the same token, as a
Bearer token, to the app's MCP. It never stores an app's long-lived key.
"""

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import jwt

from assistant.packs import AppKey

MAX_TTL = timedelta(hours=1)
LEEWAY_SECONDS = 30
_REQUIRED = ["exp", "iat", "iss", "sub"]
# A workspace id is part of storage keys ({app}/{workspace}/...): no "/" or anything else that could escape them.
WORKSPACE_ID = re.compile(r"[A-Za-z0-9:_.-]{1,200}")


class AuthError(Exception):
    """A session token was refused. `reason` is one of expired, invalid, wrong_app, wrong_access."""

    def __init__(self, reason: Literal["expired", "invalid", "wrong_app", "wrong_access"]):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Session:
    app: str
    workspace: str
    access: Literal["draft"]
    token: str
    expires_at: datetime


def mint_session(pack: AppKey, workspace: str, ttl: timedelta = MAX_TTL) -> str:
    if ttl > MAX_TTL:
        raise ValueError("session ttl must be at most 1 hour")
    now = datetime.now(UTC)
    claims = {
        "iss": pack.id,
        "sub": workspace,
        "access": "draft",
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
    }
    return jwt.encode(claims, pack.secret(), algorithm="HS256")


def _verify(token: str, pack: AppKey, access: str) -> dict:
    """The checked claims of a token for `pack` whose `access` must be `access`; AuthError otherwise."""
    try:
        # Leeway covers clock skew on iat; expiry itself is checked strictly below.
        claims = jwt.decode(
            token,
            pack.secret(),
            algorithms=["HS256"],
            options={"require": _REQUIRED, "verify_exp": False},
            leeway=LEEWAY_SECONDS,
        )
    except jwt.InvalidTokenError as exc:
        raise AuthError("invalid") from exc

    exp, iat, iss, sub = claims["exp"], claims["iat"], claims["iss"], claims["sub"]
    if not all(isinstance(n, int | float) and not isinstance(n, bool) for n in (exp, iat)):
        raise AuthError("invalid")
    if not isinstance(iss, str) or not isinstance(sub, str) or not WORKSPACE_ID.fullmatch(sub):
        raise AuthError("invalid")
    if exp <= time.time():
        raise AuthError("expired")
    if iss != pack.id:
        raise AuthError("wrong_app")
    if claims.get("access") != access:
        raise AuthError("wrong_access")
    if exp - iat > MAX_TTL.total_seconds():
        raise AuthError("invalid")
    return claims


def verify_session(token: str, pack: AppKey) -> Session:
    claims = _verify(token, pack, "draft")
    return Session(
        app=claims["iss"],
        workspace=claims["sub"],
        access="draft",
        token=token,
        expires_at=datetime.fromtimestamp(claims["exp"], UTC),
    )


def verify_delete_token(token: str, pack: AppKey) -> str:
    """The workspace a delete token (access "delete", minted by the app with the pack's secret) may delete."""
    return _verify(token, pack, "delete")["sub"]
