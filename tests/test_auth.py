"""The `hsmcli auth` group: login, import-cookie, status, logout.

Sign-in stores only the Supabase auth chunks (never the whole browser
header), `login` reads the paste through a hidden prompt, and `status`
answers scripts through its exit code.
"""

import argparse
import io

import pytest

import hsmcli.cli as cli
from hsmcli.api_client import AuthError
from hsmcli.cli import (
    _store_cookie,
    cmd_auth_import_cookie,
    cmd_auth_login,
    cmd_auth_logout,
    cmd_auth_status,
    cmd_config_set_cookie,
)
from hsmcli.config import Config


HEADER = ("_ga=GA1.2.3; sb-auth-auth-token.0=part0; "
          "sb-auth-auth-token.1=part1; intercom-id=abc")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "capture_firefox_cookie", lambda site: None)
    monkeypatch.setattr(cli, "capture_default_firefox_cookie",
                        lambda site: None)
    return Config(str(tmp_path / "cfg"))


def _args(**kw):
    kw.setdefault("json", False)
    kw.setdefault("yaml", False)
    kw.setdefault("debug", False)
    kw.setdefault("github", False)
    kw.setdefault("no_browser", True)   # tests never open a real browser
    return argparse.Namespace(**kw)


# ── chunk filtering ───────────────────────────────────────────────────────

def test_store_cookie_keeps_only_the_auth_chunks(cfg):
    assert _store_cookie(cfg, HEADER) == 0
    stored = cfg.get_cookie()
    assert stored == "sb-auth-auth-token.0=part0; sb-auth-auth-token.1=part1"
    assert "_ga" not in stored and "intercom" not in stored


def test_store_cookie_orders_chunks_numerically(cfg):
    many = "; ".join(f"sb-auth-auth-token.{i}=p{i}" for i in (10, 2, 0, 1))
    _store_cookie(cfg, many)
    assert cfg.get_cookie() == ("sb-auth-auth-token.0=p0; "
                                "sb-auth-auth-token.1=p1; "
                                "sb-auth-auth-token.2=p2; "
                                "sb-auth-auth-token.10=p10")


def test_store_cookie_without_auth_chunks_is_rejected(cfg, capsys):
    """A header with no sb-auth-auth-token.N chunk is refused outright —
    storing it would only fail later with a misleading 'cookie may be
    expired'. ($HSMCLI_COOKIE remains the power-user escape hatch.)"""
    assert _store_cookie(cfg, "weird=value; _ga=x") == 2
    assert cfg.get_cookie() is None
    assert "authentication cookies" in capsys.readouterr().err


def test_chunk_match_is_exact_not_prefix(cfg):
    """`sb-auth-auth-token-evil=x` must not ride along on startswith."""
    assert _store_cookie(
        cfg, "sb-auth-auth-token.0=ok; sb-auth-auth-token-evil=x; "
             "sb-auth-auth-token.0extra=y") == 0
    assert cfg.get_cookie() == "sb-auth-auth-token.0=ok"


def test_store_cookie_rejects_a_bare_token(cfg, capsys):
    assert _store_cookie(cfg, "eyJhbGciOiJIUzI1NiJ9.payload.sig") == 2
    assert cfg.get_cookie() is None


# ── auth login ────────────────────────────────────────────────────────────

class _OkAPI:
    def __init__(self, config, debug=False):
        pass

    def get_profile(self):
        return {"profile": {}}


class _RejectAPI(_OkAPI):
    def get_profile(self):
        raise AuthError("HTTP 401", status=401)


def test_login_hidden_prompt_saves_and_verifies(cfg, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # not a tty…
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("getpass.getpass", lambda prompt: HEADER)
    monkeypatch.setattr(cli, "HackSmarterAPI", _OkAPI)
    assert cmd_auth_login(cfg, _args()) == 0
    assert cfg.get_cookie().startswith("sb-auth-auth-token.0=")
    # The pasted value itself never reaches either stream.
    out = capsys.readouterr()
    assert "part0" not in out.out + out.err


def test_login_captures_cookie_from_browser_automatically(cfg, monkeypatch,
                                                          capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli, "capture_browser_cookie", lambda site: HEADER)
    monkeypatch.setattr(cli, "HackSmarterAPI", _OkAPI)

    assert cmd_auth_login(cfg, _args(no_browser=False)) == 0
    assert cfg.get_cookie() == ("sb-auth-auth-token.0=part0; "
                                "sb-auth-auth-token.1=part1")
    output = capsys.readouterr()
    assert "isolated login window" in output.err
    assert "Cookie:" not in output.out + output.err


def test_login_imports_existing_firefox_session_without_opening_window(
        cfg, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli, "capture_firefox_cookie", lambda site: HEADER)
    monkeypatch.setattr(
        cli, "capture_browser_cookie",
        lambda site: pytest.fail("should not launch Chromium"),
    )
    monkeypatch.setattr(cli, "HackSmarterAPI", _OkAPI)

    assert cmd_auth_login(cfg, _args(no_browser=False)) == 0
    assert cfg.get_cookie().startswith("sb-auth-auth-token.0=")
    assert "existing HackSmarter session in Firefox" in capsys.readouterr().err


def test_login_captures_login_from_default_firefox(cfg, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli, "capture_default_firefox_cookie",
                        lambda site: HEADER)
    monkeypatch.setattr(
        cli, "capture_browser_cookie",
        lambda site: pytest.fail("should not launch isolated Chromium"),
    )
    monkeypatch.setattr(cli, "HackSmarterAPI", _OkAPI)

    assert cmd_auth_login(cfg, _args(no_browser=False)) == 0
    assert cfg.get_cookie().startswith("sb-auth-auth-token.0=")
    assert "default Firefox" in capsys.readouterr().err


def test_login_rejected_session_saves_nothing(cfg, monkeypatch, capsys):
    """A bad paste must not clobber a stored session that still works,
    and no ✓ may appear before the verdict."""
    cfg.set_cookie("sb-auth-auth-token.0=still-good")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("getpass.getpass", lambda prompt: HEADER)
    monkeypatch.setattr(cli, "HackSmarterAPI", _RejectAPI)
    assert cmd_auth_login(cfg, _args()) == 1
    assert cfg.get_cookie() == "sb-auth-auth-token.0=still-good"
    assert "Signed in" not in capsys.readouterr().out


def test_login_verifies_the_candidate_not_the_env_cookie(cfg, monkeypatch):
    """Verification must test the paste itself — $HSMCLI_COOKIE overrides
    Config.get_cookie(), so a Config-backed client would test the wrong
    credential."""
    seen = {}

    class _Capture(_OkAPI):
        def __init__(self, config, debug=False):
            seen["cookie"] = config.get_cookie()

    monkeypatch.setenv("HSMCLI_COOKIE", "sb-auth-auth-token.0=stale-env")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("getpass.getpass", lambda prompt: HEADER)
    monkeypatch.setattr(cli, "HackSmarterAPI", _Capture)
    assert cmd_auth_login(cfg, _args()) == 0
    assert seen["cookie"].startswith("sb-auth-auth-token.0=part0")


def test_login_github_capture_names_the_button(cfg, monkeypatch, capsys):
    opened = {}
    monkeypatch.setattr(cli, "capture_browser_cookie",
                        lambda url: opened.setdefault("url", url) and HEADER)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("getpass.getpass", lambda prompt: HEADER)
    monkeypatch.setattr(cli, "HackSmarterAPI", _OkAPI)
    assert cmd_auth_login(cfg, _args(github=True, no_browser=False)) == 0
    assert opened["url"] == cfg.get_base_url()
    assert "GitHub" in capsys.readouterr().err


def test_login_empty_paste_is_an_error(cfg, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("getpass.getpass", lambda prompt: "   ")
    assert cmd_auth_login(cfg, _args()) == 1
    assert cfg.get_cookie() is None


def test_login_piped_stdin_reads_the_header(cfg, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(HEADER))
    assert cmd_auth_login(cfg, _args()) == 0
    assert cfg.get_cookie().startswith("sb-auth-auth-token.0=")


# ── auth import-cookie ────────────────────────────────────────────────────

def test_import_cookie_defaults_to_stdin(cfg, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(HEADER))
    assert cmd_auth_import_cookie(cfg, _args(cookie=None)) == 0
    assert cfg.get_cookie().startswith("sb-auth-auth-token.0=")


def test_import_cookie_dash_reads_stdin(cfg, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(HEADER))
    assert cmd_auth_import_cookie(cfg, _args(cookie="-")) == 0
    assert cfg.get_cookie() is not None


def test_set_cookie_still_works_but_says_deprecated(cfg, capsys):
    assert cmd_config_set_cookie(cfg, _args(cookie=HEADER)) == 0
    assert "deprecated" in capsys.readouterr().err
    assert cfg.get_cookie().startswith("sb-auth-auth-token.0=")


# ── auth status / logout ──────────────────────────────────────────────────

def test_status_not_signed_in_is_nonzero(cfg, capsys):
    assert cmd_auth_status(cfg, _args()) == 1
    assert "auth login" in capsys.readouterr().err


def test_status_json_reports_signed_out(cfg, capsys):
    import json
    assert cmd_auth_status(cfg, _args(json=True)) == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["signed_in"] is False


def _session_cookie(expires_in=3600):
    """A decodable Supabase session cookie, split across two chunks."""
    import base64
    import json
    import time
    blob = base64.urlsafe_b64encode(json.dumps({
        "user": {"email": "alice@example.com"},
        "expires_at": int(time.time()) + expires_in,
    }).encode()).decode().rstrip("=")
    mid = len(blob) // 2
    return (f"sb-auth-auth-token.0=base64-{blob[:mid]}; "
            f"sb-auth-auth-token.1={blob[mid:]}")


def test_status_json_reports_signed_in(cfg, capsys):
    import json
    cfg.set_cookie(_session_cookie())
    assert cmd_auth_status(cfg, _args(json=True)) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["signed_in"] is True
    assert doc["email"] == "alice@example.com"
    assert doc["source"].endswith("config.json")


def test_status_undecodable_cookie_is_not_a_green(cfg, capsys):
    """A stored cookie that doesn't decode must not tell a script all is
    well while every API call 401s."""
    import json
    cfg.set_cookie("weird=value")
    assert cmd_auth_status(cfg, _args(json=True)) == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["signed_in"] is False
    assert doc["stored"] is True
    assert cmd_auth_status(cfg, _args()) == 1


def test_status_expired_session_is_nonzero(cfg):
    cfg.set_cookie(_session_cookie(expires_in=-60))
    assert cmd_auth_status(cfg, _args(json=True)) == 1


def test_logout_clears_the_stored_session(cfg):
    cfg.set_cookie("sb-auth-auth-token.0=abc")
    assert cmd_auth_logout(cfg, _args()) == 0
    assert cfg.get_cookie() is None


def test_logout_warns_when_env_var_still_overrides(cfg, monkeypatch, capsys):
    monkeypatch.setenv("HSMCLI_COOKIE", "sb-auth-auth-token.0=env")
    assert cmd_auth_logout(cfg, _args()) == 0
    assert "HSMCLI_COOKIE" in capsys.readouterr().err
