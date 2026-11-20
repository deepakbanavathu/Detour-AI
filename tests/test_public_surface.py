"""What the public address may reach. Everything else answers 404 there."""
import pytest

from app.main import public_path_allowed


@pytest.mark.parametrize("path", ["/webhooks/whatsapp", "/webhooks/telegram", "/r/abc123", "/card/7", "/sw.js", "/v1/packs/7", "/v1/packs/7/status",
                                  "/v1/feeds/cifs.json", "/v1/feeds/cifs.xml"])
def test_public(path):
    assert public_path_allowed(path)


@pytest.mark.parametrize("path", ["/", "/console", "/brief", "/healthz", "/v1/brief", "/v1/route", "/v1/advisories/paste", "/v1/admin/edges/state", "/v1/sources",
                                  "/v1/review", "/v1/zones", "/v1/team-routes", "/v1/channels", "/v1/feeds/cifs/preview", "/static/console.html", "/docs", "/openapi.json"])
def test_private(path):
    assert not public_path_allowed(path)
