"""Console sign-in: the session cookie, the password check and the brake on guessing."""
import time

from app import auth


def test_cookie_round_trip_and_expiry():
    now = 1_800_000_000.0
    c = auth.issue(now)
    assert auth.valid(c, now + 60) and auth.valid(c, now + auth.SESSION_HOURS * 3600 - 1)
    assert not auth.valid(c, now + auth.SESSION_HOURS * 3600 + 1)


def test_tampered_or_malformed_cookies_are_refused():
    c = auth.issue()
    exp, sig = c.split(".")
    assert not auth.valid(f"{int(exp) + 99999}.{sig}")            # extending the expiry breaks the signature
    assert not auth.valid(f"{exp}.{'0' * len(sig)}")
    for bad in (None, "", "abc", "123", ".", "x.y", f"{exp}."):
        assert not auth.valid(bad)


def test_password_check(monkeypatch):
    monkeypatch.setattr(auth, "CONSOLE_PASSWORD", "s3cret-pass")
    assert auth.password_ok("s3cret-pass") and not auth.password_ok("s3cret-pasS") and not auth.password_ok("")
    assert not auth.uses_default_password()
    monkeypatch.setattr(auth, "CONSOLE_PASSWORD", "change-me-admin")
    assert auth.uses_default_password()
    monkeypatch.setattr(auth, "CONSOLE_PASSWORD", "")
    assert not auth.password_ok("")                                 # an empty configured password never signs anyone in


def test_five_wrong_tries_lock_that_address_for_five_minutes():
    who, now = "203.0.113.9", time.time()
    auth.clear_failures(who)
    for i in range(auth.MAX_FAILS):
        assert not auth.locked_out(who, now + i)
        auth.record_failure(who, now + i)
    assert auth.locked_out(who, now + 10) and not auth.locked_out("203.0.113.10", now + 10)
    assert not auth.locked_out(who, now + auth.FAIL_WINDOW_S + 10)   # the window passes
    auth.record_failure(who, now + 20)
    auth.clear_failures(who)
    assert not auth.locked_out(who, now + 21)                         # a correct sign-in clears the count
