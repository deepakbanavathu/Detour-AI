"""Console sign-in: one operator password, a signed session cookie, and a brake on guessing.

The cookie holds only an expiry time and an HMAC of it. Scripts and tests keep using the X-Admin-Token header.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque

from .config import ADMIN_TOKEN, PII_PEPPER

COOKIE = "detour_session"
SESSION_HOURS = int(os.environ.get("SESSION_HOURS", "12"))
REQUIRE_LOGIN = os.environ.get("REQUIRE_LOGIN", "1") not in ("0", "false", "no")
CONSOLE_PASSWORD = os.environ.get("CONSOLE_PASSWORD") or ADMIN_TOKEN
_SECRET = (os.environ.get("SESSION_SECRET") or hashlib.sha256(f"{ADMIN_TOKEN}|{PII_PEPPER}|session".encode()).hexdigest()).encode()
DEFAULT_PASSWORDS = {"change-me", "change-me-admin", "local-admin", ""}
MAX_FAILS, FAIL_WINDOW_S = 5, 300
_fails: dict[str, deque] = defaultdict(deque)


def _sig(exp: str) -> str:
    return hmac.new(_SECRET, exp.encode(), hashlib.sha256).hexdigest()


def issue(now: float | None = None) -> str:
    exp = str(int((now or time.time()) + SESSION_HOURS * 3600))
    return f"{exp}.{_sig(exp)}"


def valid(cookie: str | None, now: float | None = None) -> bool:
    if not cookie or "." not in cookie:
        return False
    exp, sig = cookie.split(".", 1)
    return exp.isdigit() and hmac.compare_digest(sig, _sig(exp)) and int(exp) > (now or time.time())


def password_ok(password: str) -> bool:
    return bool(CONSOLE_PASSWORD) and secrets.compare_digest(password.encode(), CONSOLE_PASSWORD.encode())


def uses_default_password() -> bool:
    return CONSOLE_PASSWORD in DEFAULT_PASSWORDS


def locked_out(who: str, now: float | None = None) -> bool:
    now = now or time.time()
    q = _fails[who]
    while q and now - q[0] > FAIL_WINDOW_S:
        q.popleft()
    return len(q) >= MAX_FAILS


def record_failure(who: str, now: float | None = None) -> None:
    _fails[who].append(now or time.time())


def clear_failures(who: str) -> None:
    _fails.pop(who, None)
