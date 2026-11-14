"""The one-word state the console shows for each feed."""
from datetime import datetime, timezone

from app.main import source_state

NOW = datetime.now(timezone.utc)


def row(**kw):
    return {"enabled": True, "last_polled": NOW, "last_error": None, **kw}


def test_states():
    assert source_state(row()) == "ok"
    assert source_state(row(enabled=False, last_error="boom")) == "off"
    assert source_state(row(last_polled=None)) == "waiting for first poll"
    assert source_state(row(last_error="skipped: TOMTOM_KEY is not set")) == "needs a key"
    assert source_state(row(last_error="Client error '404 Not Found'")) == "failing"
