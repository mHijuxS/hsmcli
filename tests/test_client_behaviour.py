"""Client construction: User-Agent, cookie loading, and --debug tracing."""

import json

import pytest

from hsmcli.api_client import (
    BROWSER_USER_AGENT,
    DEFAULT_USER_AGENT,
    HackSmarterAPI,
)
from hsmcli.config import Config


@pytest.fixture
def cfg(tmp_path):
    return Config(str(tmp_path / "cfg"))


class FakeResponse:
    status_code = 200

    def __init__(self, payload=None, text=""):
        self._payload = payload
        self.text = text
        self.content = b"x" if (payload is not None or text) else b""
        self.headers = {}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self):
        pass


# ── User-Agent ────────────────────────────────────────────────────────────

def test_default_user_agent_names_the_client(cfg, monkeypatch):
    """We identify ourselves rather than posing as a browser — verified
    against the live API, which does not filter on User-Agent."""
    monkeypatch.delenv("HSMCLI_USER_AGENT", raising=False)
    ua = HackSmarterAPI(cfg).session.headers["User-Agent"]
    assert ua.startswith("hsmcli/")
    assert "Mozilla" not in ua


def test_browser_user_agent_kept_as_a_documented_fallback():
    """If the edge ever starts filtering, HSMCLI_USER_AGENT + this constant
    is the fix; it must not silently disappear."""
    assert BROWSER_USER_AGENT.startswith("Mozilla/5.0")


def test_user_agent_env_override(cfg, monkeypatch):
    monkeypatch.setenv("HSMCLI_USER_AGENT", BROWSER_USER_AGENT)
    api = HackSmarterAPI(cfg)
    assert api.session.headers["User-Agent"] == BROWSER_USER_AGENT


def test_empty_user_agent_env_falls_back(cfg, monkeypatch):
    """An empty value must not send a blank UA header."""
    monkeypatch.setenv("HSMCLI_USER_AGENT", "")
    api = HackSmarterAPI(cfg)
    assert api.session.headers["User-Agent"] == DEFAULT_USER_AGENT


# ── cookie loading ────────────────────────────────────────────────────────

def test_cookies_loaded_into_the_session(cfg):
    cfg.set_cookie("sb-auth-auth-token.0=base64-abc; _ga=noise")
    api = HackSmarterAPI(cfg)
    jar = api.session.cookies.get_dict()
    assert jar["sb-auth-auth-token.0"] == "base64-abc"
    assert jar["_ga"] == "noise"


def test_no_cookie_is_not_fatal(cfg):
    """Construction must succeed so `config set-cookie` stays reachable."""
    api = HackSmarterAPI(cfg)
    assert api.session_summary() is None


def test_unparseable_cookie_is_not_fatal(cfg):
    cfg.set_cookie("just-a-bare-token")
    api = HackSmarterAPI(cfg)
    assert api.session_summary() is None


# ── --debug tracing ───────────────────────────────────────────────────────

def test_debug_traces_to_stderr_not_stdout(cfg, capsys):
    """--debug must compose with --json; the trace used to land on stdout
    and corrupt piped output."""
    api = HackSmarterAPI(cfg, debug=True)
    api._trace("GET", "/api/student/catalog",
               FakeResponse({"catalog_items": []}),
               payload={"catalog_items": []})
    out, err = capsys.readouterr()
    assert out == ""
    assert "GET /api/student/catalog → 200" in err
    assert "catalog_items" in err


def test_debug_off_is_silent(cfg, capsys):
    api = HackSmarterAPI(cfg, debug=False)
    api._trace("GET", "/x", FakeResponse({"a": 1}), payload={"a": 1})
    out, err = capsys.readouterr()
    assert out == "" and err == ""


def test_debug_traces_every_call_without_exiting(cfg, capsys, monkeypatch):
    """The old implementation called sys.exit(0) after the first response,
    so `labs list` (two endpoints) only ever showed one."""
    api = HackSmarterAPI(cfg, debug=True)
    monkeypatch.setattr(api.session, "get",
                        lambda *a, **k: FakeResponse({"ok": True}))

    first = api._request("GET", "/api/student/courses")
    second = api._request("GET", "/api/student/catalog")

    assert first == second == {"ok": True}   # execution continued
    err = capsys.readouterr().err
    assert "/api/student/courses" in err
    assert "/api/student/catalog" in err


def test_debug_traces_non_json_bodies(cfg, capsys, monkeypatch):
    api = HackSmarterAPI(cfg, debug=True)
    monkeypatch.setattr(api.session, "get",
                        lambda *a, **k: FakeResponse(None, text="client\n<ovpn>"))
    assert api._request("GET", "/vpn") == {"raw": "client\n<ovpn>"}
    assert "<ovpn>" in capsys.readouterr().err


def test_debug_trace_is_valid_json_per_call(cfg, capsys, monkeypatch):
    """The payload half of a trace should stay machine-parseable."""
    api = HackSmarterAPI(cfg, debug=True)
    monkeypatch.setattr(api.session, "get",
                        lambda *a, **k: FakeResponse({"a": [1, 2]}))
    api._request("GET", "/x")
    body = capsys.readouterr().err.split("\n", 1)[1]
    assert json.loads(body) == {"a": [1, 2]}


def test_empty_response_returns_empty_dict(cfg, monkeypatch):
    api = HackSmarterAPI(cfg)
    monkeypatch.setattr(api.session, "post", lambda *a, **k: FakeResponse())
    assert api._request("POST", "/api/student/courses/x/enroll") == {}


def test_unsupported_method_rejected(cfg):
    with pytest.raises(Exception):
        HackSmarterAPI(cfg)._request("PATCH", "/x")
