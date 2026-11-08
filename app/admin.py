"""Admin authentication: one shared password, set in the environment.

Deliberately primitive. There are no accounts -- whoever knows
`WENET_ADMIN_PASSWORD` is the admin -- and no session store: a login is a cookie
holding an expiry and an HMAC of it, keyed on the password. Changing the
password therefore logs everyone out, which is the only revocation there is.

Two ways in, both checked on every request:

- the `wenet_admin` cookie, set by `POST /api/v1/admin/login` -- the panel;
- an `X-Admin-Password` header -- scripts and curl, which have no cookie jar
  worth the trouble.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections import defaultdict, deque

from fastapi import Header, HTTPException, Request, Response

from . import config

COOKIE = "wenet_admin"

#: Login attempts per client address per minute. Generous for a person
#: mistyping, useless for guessing.
LOGIN_ATTEMPTS_PER_MINUTE = 10

_attempts: dict[str, deque[float]] = defaultdict(deque)


def enabled() -> bool:
    return bool(config.ADMIN_PASSWORD)


def _key() -> bytes:
    return hashlib.sha256(
        b"wenet-admin-session\0" + (config.ADMIN_PASSWORD or "").encode()
    ).digest()


def _sign(expiry: int) -> str:
    return hmac.new(_key(), str(expiry).encode(), hashlib.sha256).hexdigest()


def issue_token(now: float | None = None) -> str:
    expiry = int((now or time.time()) + config.ADMIN_SESSION_HOURS * 3600)
    return f"{expiry}.{_sign(expiry)}"


def token_valid(token: str | None, now: float | None = None) -> bool:
    if not enabled() or not token:
        return False
    expiry_text, _, signature = token.partition(".")
    if not expiry_text.isdigit():
        return False
    expiry = int(expiry_text)
    if expiry < (now or time.time()):
        return False
    return hmac.compare_digest(signature, _sign(expiry))


def password_ok(candidate: str | None) -> bool:
    if not enabled() or candidate is None:
        return False
    return hmac.compare_digest(candidate.encode(), config.ADMIN_PASSWORD.encode())


def is_admin(request: Request) -> bool:
    return (password_ok(request.headers.get("x-admin-password"))
            or token_valid(request.cookies.get(COOKIE)))


def require_admin(
    request: Request,
    x_admin_password: str | None = Header(default=None),
) -> None:
    """FastAPI dependency guarding every administrative endpoint."""
    if not enabled():
        raise HTTPException(403, "admin is disabled: set WENET_ADMIN_PASSWORD")
    if password_ok(x_admin_password) or token_valid(request.cookies.get(COOKIE)):
        return
    raise HTTPException(401, "admin login required")


def check_login_rate(request: Request) -> None:
    client = request.client.host if request.client else "unknown"
    now = time.monotonic()
    window = _attempts[client]
    while window and now - window[0] > 60.0:
        window.popleft()
    if len(window) >= LOGIN_ATTEMPTS_PER_MINUTE:
        raise HTTPException(429, "too many login attempts; wait a minute")
    window.append(now)


def set_cookie(response: Response, request: Request) -> None:
    response.set_cookie(
        COOKIE,
        issue_token(),
        max_age=config.ADMIN_SESSION_HOURS * 3600,
        httponly=True,
        # Strict: no other site can make a browser send it, which is what
        # stands in for CSRF tokens on the POST endpoints.
        samesite="strict",
        # Behind nginx, `--proxy-headers` makes this the client's scheme.
        secure=request.url.scheme == "https",
        path="/",
    )


def clear_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/")
