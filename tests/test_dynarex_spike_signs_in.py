"""The Dynarex spike submits the login form, not the search box.

The portal's sign-in page carries the search box twice before it carries
the login, and the spike took "the first submit button on the page" — so
it searched for an empty string, landed on /product_search/?q=, and
reported the login as failed without ever having attempted one. This is
that page, served locally, with nothing typed by hand.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import dynarex_portal_spike as spike  # noqa: E402

SEARCH_FORM = """
<form action="/product_search/" method="GET" id="search_mini_form">
  <input name="q" type="text" id="search" placeholder="Search">
  <button type="submit">Go</button>
</form>
"""
LOGIN_PAGE = f"""<html><body>Welcome Guest
{SEARCH_FORM}{SEARCH_FORM}
<form action="/user/login" method="POST">
  <input name="login_username" type="text" id="email">
  <input name="login_password" type="password" id="pass">
  <button type="submit">Login</button>
</form>
</body></html>"""
SIGNED_IN = "<html><body>My Account — Sign Out</body></html>"
REFUSED = "<html><body><div class='error-msg'>Invalid login or password.</div></body></html>"
SEARCHED = "<html><body>Search results for nothing at all</body></html>"


class _Portal(BaseHTTPRequestHandler):
    """Enough of commercebuild to tell the two forms apart."""

    posted: ClassVar[list[dict[str, list[str]]]] = []
    password = "the-right-one"

    def _send(self, body: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        self._send(SEARCHED if path.startswith("/product_search") else LOGIN_PAGE)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        form = parse_qs(self.rfile.read(length).decode())
        self.posted.append(form)
        given = form.get("login_password", [""])[0]
        self._send(SIGNED_IN if given == self.password else REFUSED)

    def log_message(self, format: str, *args: Any) -> None:
        return


@pytest.fixture
def portal(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[_Portal]]:
    _Portal.posted = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Portal)
    Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(spike, "BASE", f"http://127.0.0.1:{server.server_port}")
    yield _Portal
    server.shutdown()


@pytest.fixture
def page(portal: type[_Portal]) -> Iterator[Page]:
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except PlaywrightError as missing:
            pytest.skip(f"no browser: {missing}")
        opened = browser.new_page()
        yield opened
        browser.close()


def test_the_password_reaches_the_login_form_not_the_search_box(
    page: Page, portal: type[_Portal], capsys: pytest.CaptureFixture[str]
) -> None:
    assert spike._sign_in(page, "zach@example.test", portal.password) is True

    assert portal.posted == [
        {"login_username": ["zach@example.test"], "login_password": [portal.password]}
    ]
    assert "product_search" not in page.url
    assert "user/login" in capsys.readouterr().out


def test_a_refusal_is_reported_in_the_portals_own_words(
    page: Page, portal: type[_Portal], capsys: pytest.CaptureFixture[str]
) -> None:
    """A wrong password and an unverified address are the same blank
    failure otherwise, which is what sent Zach checking his password."""
    assert spike._sign_in(page, "zach@example.test", "wrong") is False

    assert "Invalid login or password." in capsys.readouterr().out
