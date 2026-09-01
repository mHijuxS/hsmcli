import base64
import json
import time
import sqlite3

import pytest

from hsmcli.browser_auth import (
    BrowserCaptureUnavailable,
    _cookie_header,
    _default_browser_is_firefox,
    _find_chromium,
    capture_firefox_cookie,
    _site_hostname,
)


def _session_value():
    raw = json.dumps({"access_token": "x", "expires_at": time.time() + 600})
    return "base64-" + base64.b64encode(raw.encode()).decode()


def test_cookie_header_filters_domain_and_orders_chunks():
    value = _session_value()
    mid = len(value) // 2
    cookies = [
        {"name": "sb-auth-auth-token.1", "value": value[mid:],
         "domain": ".hacksmarter.org"},
        {"name": "_ga", "value": "noise", "domain": ".hacksmarter.org"},
        {"name": "sb-auth-auth-token.0", "value": value[:mid],
         "domain": ".hacksmarter.org"},
        {"name": "sb-auth-auth-token.2", "value": "evil",
         "domain": ".example.com"},
    ]
    assert _cookie_header(cookies, "https://www.hacksmarter.org") == (
        f"sb-auth-auth-token.0={value[:mid]}; "
        f"sb-auth-auth-token.1={value[mid:]}"
    )


def test_cookie_header_waits_for_complete_session():
    assert _cookie_header([
        {"name": "sb-auth-auth-token.0", "value": "incomplete",
         "domain": ".hacksmarter.org"}
    ], "https://www.hacksmarter.org") is None


def test_site_hostname_accepts_custom_port():
    assert _site_hostname("http://localhost:3000/path") == "localhost"


@pytest.mark.parametrize("bad", ["", "ftp://example.com", "not-a-url"])
def test_site_hostname_rejects_invalid_site(bad):
    with pytest.raises(BrowserCaptureUnavailable):
        _site_hostname(bad)


def test_browser_override_must_exist(monkeypatch):
    monkeypatch.setenv("HSMCLI_BROWSER", "/definitely/not/a/browser")
    with pytest.raises(BrowserCaptureUnavailable, match="HSMCLI_BROWSER"):
        _find_chromium()


def test_detects_default_firefox(monkeypatch):
    class Result:
        stdout = "firefox.desktop\n"

    monkeypatch.delenv("BROWSER", raising=False)
    monkeypatch.setattr("shutil.which", lambda command: "/usr/bin/xdg-settings")
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: Result())
    assert _default_browser_is_firefox()


def test_capture_firefox_cookie_from_active_profile(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    database = profile / "cookies.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE moz_cookies (name TEXT, value TEXT, host TEXT)"
    )
    value = _session_value()
    mid = len(value) // 2
    connection.executemany(
        "INSERT INTO moz_cookies VALUES (?, ?, ?)",
        [
            ("sb-auth-auth-token.0", value[:mid], ".hacksmarter.org"),
            ("sb-auth-auth-token.1", value[mid:], ".hacksmarter.org"),
            ("_ga", "noise", ".hacksmarter.org"),
        ],
    )
    connection.commit()
    monkeypatch.setenv("HSMCLI_FIREFOX_PROFILE", str(profile))

    # Keep the source connection open, as Firefox does while it is running.
    try:
        assert capture_firefox_cookie("https://www.hacksmarter.org") == (
            f"sb-auth-auth-token.0={value[:mid]}; "
            f"sb-auth-auth-token.1={value[mid:]}"
        )
    finally:
        connection.close()
