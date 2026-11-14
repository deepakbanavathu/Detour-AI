"""Browser fixtures for the page tests. Pages are opened from disk in preview mode, so no API is needed.

Needs:  pip install playwright && playwright install chromium
Offline machines: set DETOUR_UI_VENDOR to a folder holding node_modules (maplibre-gl, @fontsource/overpass);
requests for the CDN copies are then answered from it. Without it the CDN is used.
"""
import os
from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api")
STATIC = Path(__file__).resolve().parents[2] / "api" / "app" / "static"
VENDOR = os.environ.get("DETOUR_UI_VENDOR")
FONT_CSS = "".join(
    f"@font-face{{font-family:'Overpass';font-weight:{w};src:url('https://local.fonts/overpass-{w}.woff2') format('woff2');}}" for w in (400, 600, 800))


def _route(route):
    u = route.request.url
    if u.startswith("file:") and "/v1/" not in u and not u.endswith("/healthz") and not u.endswith("/sw.js"):
        return route.continue_()
    if VENDOR:
        nm = Path(VENDOR)
        if "maplibre-gl.min.js" in u:
            return route.fulfill(path=str(nm / "maplibre-gl/dist/maplibre-gl.js"), content_type="application/javascript")
        if "maplibre-gl.min.css" in u:
            return route.fulfill(path=str(nm / "maplibre-gl/dist/maplibre-gl.css"), content_type="text/css")
        if "fonts.googleapis.com" in u:
            return route.fulfill(body=FONT_CSS, content_type="text/css")
        if "local.fonts" in u:
            w = u.rsplit("-", 1)[1].split(".")[0]
            return route.fulfill(path=str(nm / f"@fontsource/overpass/files/overpass-latin-{w}-normal.woff2"), content_type="font/woff2")
        return route.abort()
    if u.startswith("file:"):
        return route.abort()                       # API calls from a file:// page: preview mode
    return route.continue_()


@pytest.fixture(scope="session")
def browser():
    with pw.sync_playwright() as p:
        b = p.chromium.launch(args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"])
        yield b
        b.close()


@pytest.fixture
def open_page(browser):
    pages = []

    def _open(name: str, query: str = "", width: int = 1280, height: int = 800, scheme: str = "light"):
        ctx = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme, permissions=["clipboard-read", "clipboard-write"])
        page = ctx.new_page()
        page.errors = []
        page.on("pageerror", lambda e: page.errors.append(str(e)))
        page.route("**/*", _route)
        page.goto(f"file://{STATIC / name}{query}")
        pages.append(ctx)
        return page

    yield _open
    for ctx in pages:
        ctx.close()
