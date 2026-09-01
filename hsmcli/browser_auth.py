"""Import HackSmarter sessions from Firefox or an isolated Chromium."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import websocket

from .api_client import AUTH_COOKIE_BASE, decode_supabase_session


class BrowserCaptureUnavailable(RuntimeError):
    """No supported browser could be started for automatic capture."""


class BrowserCaptureCancelled(RuntimeError):
    """The login window was closed before a session was captured."""


def _default_browser_id() -> str:
    """Best-effort desktop identifier for the user's default browser."""
    configured = os.getenv("BROWSER")
    if configured:
        return Path(configured.split(":", 1)[0]).name.lower()
    if sys.platform.startswith("linux") and shutil.which("xdg-settings"):
        try:
            result = subprocess.run(
                ["xdg-settings", "get", "default-web-browser"],
                capture_output=True, text=True, timeout=2, check=False,
            )
            return result.stdout.strip().lower()
        except (OSError, subprocess.TimeoutExpired):
            pass
    return ""


def _default_browser_is_firefox() -> bool:
    browser_id = _default_browser_id()
    return "firefox" in browser_id or "librewolf" in browser_id


def _find_chromium() -> str:
    override = os.getenv("HSMCLI_BROWSER")
    if override:
        found = shutil.which(override)
        if found:
            return found
        if Path(override).is_file():
            return override
        raise BrowserCaptureUnavailable(
            f"$HSMCLI_BROWSER does not point to an executable: {override}"
        )

    default_id = _default_browser_id()
    preferred = []
    for marker, command in (
        ("google-chrome", "google-chrome"),
        ("chromium", "chromium"),
        ("brave", "brave-browser"),
        ("microsoft-edge", "microsoft-edge"),
        ("vivaldi", "vivaldi"),
    ):
        if marker in default_id:
            preferred.append(command)
            break
    names = tuple(preferred) + (
        "chromium", "chromium-browser", "google-chrome",
        "google-chrome-stable", "brave-browser", "microsoft-edge",
        "microsoft-edge-stable", "vivaldi",
    )
    for name in names:
        found = shutil.which(name)
        if found:
            return found

    if sys.platform == "darwin":
        for candidate in (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ):
            if Path(candidate).is_file():
                return candidate
    raise BrowserCaptureUnavailable(
        "automatic capture needs Chromium, Chrome, or Edge"
    )


def _site_hostname(site: str) -> str:
    parsed = urlsplit(site)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise BrowserCaptureUnavailable(f"cannot open invalid site URL: {site}")
    return parsed.hostname.lower()


def _cookie_header(cookies: List[Dict[str, Any]], site: str) -> Optional[str]:
    """Return a complete auth header from a DevTools cookie response."""
    hostname = _site_hostname(site)
    pairs = []
    parsed = {}
    for item in cookies:
        name = item.get("name", "")
        value = item.get("value", "")
        domain = str(item.get("domain", "")).lstrip(".").lower()
        tail = name[len(AUTH_COOKIE_BASE) + 1:]
        domain_matches = hostname == domain or hostname.endswith("." + domain)
        if (name.startswith(AUTH_COOKIE_BASE + ".") and tail.isdigit()
                and isinstance(value, str) and domain_matches):
            pairs.append((int(tail), name, value))
            parsed[name] = value
    session = decode_supabase_session(parsed)
    if not pairs or session is None:
        return None
    try:
        if float(session.get("expires_at", 0)) <= time.time():
            return None
    except (TypeError, ValueError):
        return None
    pairs.sort()
    return "; ".join(f"{name}={value}" for _, name, value in pairs)


def _firefox_cookie_databases() -> List[Path]:
    """Find Firefox profile databases in native, Flatpak and Snap layouts."""
    override = os.getenv("HSMCLI_FIREFOX_PROFILE")
    if override:
        path = Path(override).expanduser()
        return [path if path.name == "cookies.sqlite"
                else path / "cookies.sqlite"]

    home = Path.home()
    roots = [
        home / ".config/mozilla/firefox",
        home / ".mozilla/firefox",
        home / ".var/app/org.mozilla.firefox/.mozilla/firefox",
        home / "snap/firefox/common/.mozilla/firefox",
        home / "Library/Application Support/Firefox/Profiles",
    ]
    appdata = os.getenv("APPDATA")
    if appdata:
        roots.append(Path(appdata) / "Mozilla/Firefox/Profiles")

    databases = []
    for root in roots:
        if root.is_dir():
            databases.extend(root.glob("*/cookies.sqlite"))
    return sorted(set(databases),
                  key=lambda path: path.stat().st_mtime, reverse=True)


def _read_firefox_database(database: Path) -> List[Dict[str, Any]]:
    """Copy an active Firefox database and query the unlocked snapshot."""
    with tempfile.TemporaryDirectory(prefix="hsmcli-firefox-") as temp:
        snapshot = Path(temp) / "cookies.sqlite"
        shutil.copy2(database, snapshot)
        # Recent writes may still be in Firefox's WAL. SQLite will merge this
        # copied WAL when the snapshot is opened; the -shm file is optional.
        wal = database.with_name(database.name + "-wal")
        if wal.is_file():
            shutil.copy2(wal, snapshot.with_name(snapshot.name + "-wal"))
        connection = sqlite3.connect(snapshot)
        try:
            rows = connection.execute(
                "SELECT name, value, host FROM moz_cookies WHERE name GLOB ?",
                (AUTH_COOKIE_BASE + ".[0-9]*",),
            ).fetchall()
            return [{"name": name, "value": value, "domain": host}
                    for name, value, host in rows]
        finally:
            connection.close()


def capture_firefox_cookie(site: str) -> Optional[str]:
    """Import an existing session from Firefox, without opening a window."""
    _site_hostname(site)
    for database in _firefox_cookie_databases():
        if not database.is_file():
            continue
        try:
            cookie = _cookie_header(_read_firefox_database(database), site)
        except (OSError, sqlite3.Error):
            continue
        if cookie:
            return cookie
    return None


def capture_default_firefox_cookie(site: str) -> Optional[str]:
    """Open the default Firefox profile and wait for its login cookie."""
    if not _default_browser_is_firefox():
        return None
    import webbrowser
    try:
        if not webbrowser.open(site):
            return None
    except Exception:
        return None
    while True:
        cookie = capture_firefox_cookie(site)
        if cookie:
            return cookie
        time.sleep(0.5)


def _devtools_url(profile: Path, process: subprocess.Popen) -> str:
    """Wait for Chromium to publish its random loopback debugging port."""
    port_file = profile / "DevToolsActivePort"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise BrowserCaptureUnavailable("browser exited during startup")
        try:
            lines = port_file.read_text().splitlines()
            if len(lines) >= 2 and lines[0].isdigit():
                return f"ws://127.0.0.1:{lines[0]}{lines[1]}"
        except (FileNotFoundError, OSError):
            pass
        time.sleep(0.05)
    raise BrowserCaptureUnavailable("browser debugging connection timed out")


def _read_cookies(connection, request_id: int) -> List[Dict[str, Any]]:
    connection.send(json.dumps({"id": request_id,
                                "method": "Storage.getCookies"}))
    while True:
        message = json.loads(connection.recv())
        if message.get("id") != request_id:
            continue
        if "error" in message:
            raise BrowserCaptureUnavailable(
                f"browser refused cookie access: {message['error']}"
            )
        return (message.get("result") or {}).get("cookies") or []


def capture_browser_cookie(site: str) -> str:
    """Open an isolated browser and read its session through DevTools."""
    browser = _find_chromium()
    _site_hostname(site)

    with tempfile.TemporaryDirectory(prefix="hsmcli-login-") as temp:
        profile = Path(temp) / "profile"
        command = [
            browser,
            f"--user-data-dir={profile}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            f"--app={site}",
        ]
        try:
            process = subprocess.Popen(
                command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except OSError as exc:
            raise BrowserCaptureUnavailable(
                f"could not start {Path(browser).name}: {exc}"
            ) from exc

        connection = None
        try:
            devtools_url = _devtools_url(profile, process)
            try:
                connection = websocket.create_connection(
                    devtools_url, timeout=2, suppress_origin=True
                )
            except (OSError, websocket.WebSocketException) as exc:
                raise BrowserCaptureUnavailable(
                    f"could not connect to the browser: {exc}"
                ) from exc

            request_id = 0
            while True:
                if process.poll() is not None:
                    raise BrowserCaptureCancelled(
                        "login window closed before sign-in completed"
                    )
                request_id += 1
                try:
                    cookie = _cookie_header(
                        _read_cookies(connection, request_id), site
                    )
                except websocket.WebSocketTimeoutException:
                    continue
                except (OSError, websocket.WebSocketConnectionClosedException) \
                        as exc:
                    if process.poll() is not None:
                        raise BrowserCaptureCancelled(
                            "login window closed before sign-in completed"
                        ) from exc
                    raise BrowserCaptureUnavailable(
                        f"lost the browser debugging connection: {exc}"
                    ) from exc
                if cookie:
                    return cookie
                time.sleep(0.5)
        finally:
            if connection is not None:
                connection.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
