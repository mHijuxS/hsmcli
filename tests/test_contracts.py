"""Scripting contracts: exit codes, stream discipline, timeouts, overwrite
protection, base-URL safety, debug redaction.

These are the promises the README makes to pipelines: structured mode
emits exactly one clean document on stdout, exit codes tell the truth,
and nothing hangs forever.
"""

import argparse
import json
import os
import stat

import pytest

import hsmcli.cli as cli
from hsmcli.api_client import (
    DEFAULT_TIMEOUT,
    HackSmarterAPI,
)
from hsmcli.cli import (
    _positive_int,
    cmd_lab_launch,
    cmd_lab_vpn,
    cmd_whoami,
)
from hsmcli.config import Config


@pytest.fixture
def cfg(tmp_path):
    return Config(str(tmp_path / "cfg"))


def _args(**kw):
    kw.setdefault("json", False)
    kw.setdefault("yaml", False)
    kw.setdefault("debug", False)
    return argparse.Namespace(**kw)


# ── whoami --json exit code ───────────────────────────────────────────────

class _WhoamiAPI:
    def __init__(self, session=None, profile_error=False):
        self._session = session
        self._profile_error = profile_error

    def session_summary(self):
        return self._session

    def get_profile(self):
        if self._profile_error:
            raise RuntimeError("boom")
        return {"profile": {"username": "alice"}}


def test_whoami_json_no_session_is_nonzero(cfg, capsys):
    rc = cmd_whoami(_WhoamiAPI(session=None), cfg, _args(json=True))
    assert rc == 1
    doc = json.loads(capsys.readouterr().out)   # still one clean document
    assert doc["session"] is None


def test_whoami_json_profile_error_is_nonzero(cfg, capsys):
    rc = cmd_whoami(_WhoamiAPI(session={"username": "alice"},
                               profile_error=True),
                    cfg, _args(json=True))
    assert rc == 1
    doc = json.loads(capsys.readouterr().out)
    assert "error" in doc["profile"]


def test_whoami_json_healthy_is_zero(cfg, capsys):
    rc = cmd_whoami(_WhoamiAPI(session={"username": "alice"}),
                    cfg, _args(json=True))
    assert rc == 0
    json.loads(capsys.readouterr().out)


# ── launch --json --wait: one document, stdout only ───────────────────────

class _LaunchAPI:
    """One systems-lab machine: off on the first read, running on the poll."""

    def __init__(self, states=("not_launched", "running")):
        self._states = list(states)
        self.launched = False

    def lab_kind(self, course_id):
        return "systems"

    def get_lab_systems(self, course_id, ids=None):
        state = (self._states.pop(0) if len(self._states) > 1
                 else self._states[0])
        return [{
            "id": "55550000-0000-0000-0000-00000000000c",
            "name": "Widget",
            "system": {"state": state,
                       "ip": "10.1.2.3" if state == "running" else ""},
        }]

    def launch_system(self, course_id, system_id):
        self.launched = True
        return {"message": "Starting system"}

    def heartbeat_for_course(self, course_id):
        return {}


@pytest.fixture
def _resolved(monkeypatch):
    monkeypatch.setattr(cli, "_resolve_lab",
                        lambda api, args: ("course-1", "Widget"))
    monkeypatch.setattr("time.sleep", lambda s: None)


def _launch_args(**kw):
    kw.setdefault("identifier", "widget")
    kw.setdefault("system", None)
    kw.setdefault("wait", True)
    kw.setdefault("timeout", 60)
    kw.setdefault("allowed_ip", None)
    kw.setdefault("input", [])
    return _args(**kw)


def test_launch_json_wait_emits_one_final_state_document(cfg, capsys,
                                                         _resolved):
    api = _LaunchAPI()
    rc = cmd_lab_launch(api, cfg, _launch_args(json=True))
    assert rc == 0
    assert api.launched
    out = capsys.readouterr().out
    doc = json.loads(out)                     # exactly one parseable doc
    assert doc["system"]["state"] == "running"
    assert doc["system"]["ip"] == "10.1.2.3"  # the *final* state, not the ACK


def test_launch_json_no_wait_emits_the_ack(cfg, capsys, _resolved):
    api = _LaunchAPI()
    rc = cmd_lab_launch(api, cfg, _launch_args(json=True, wait=False))
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc == {"message": "Starting system"}


def test_launch_json_wait_timeout_is_2_with_a_document(cfg, capsys,
                                                       _resolved,
                                                       monkeypatch):
    api = _LaunchAPI(states=("not_launched", "pending"))
    # Let the deadline pass after a couple of polls.
    clock = iter(range(0, 1000, 30))
    monkeypatch.setattr("time.monotonic", lambda: float(next(clock)))
    rc = cmd_lab_launch(api, cfg, _launch_args(json=True, timeout=45))
    out, err = capsys.readouterr()
    assert rc == 2
    json.loads(out)                     # stdout still one clean document
    assert "giving up" in err           # the prose went to stderr


# ── vpn --print: stdout carries the profile and nothing else ──────────────

class _VpnAPI:
    def get_vpn_config(self, course_id, dest_path=None):
        text = "client\nremote lab.example 1194\n<key>K</key>"
        if dest_path:
            with open(dest_path, "w") as f:
                f.write(text)
        return text


def test_vpn_print_stdout_is_a_valid_profile(cfg, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_resolve_lab",
                        lambda api, args: ("course-1", "Widget"))
    rc = cmd_lab_vpn(_VpnAPI(), cfg,
                     _args(identifier="widget", output=None,
                           print=True, force=False))
    out, err = capsys.readouterr()
    assert rc == 0
    assert out == "client\nremote lab.example 1194\n<key>K</key>\n"
    assert "VPN profile" in err          # the confirmation moved to stderr


def test_vpn_refuses_to_overwrite_without_force(cfg, tmp_path, capsys,
                                                monkeypatch):
    monkeypatch.setattr(cli, "_resolve_lab",
                        lambda api, args: ("course-1", "Widget"))
    dest = tmp_path / "widget.ovpn"
    dest.write_text("precious")
    rc = cmd_lab_vpn(_VpnAPI(), cfg,
                     _args(identifier="widget", output=str(dest),
                           print=False, force=False))
    assert rc == 2
    assert dest.read_text() == "precious"
    assert "--force" in capsys.readouterr().err


def test_vpn_force_overwrites(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_resolve_lab",
                        lambda api, args: ("course-1", "Widget"))
    dest = tmp_path / "widget.ovpn"
    dest.write_text("precious")
    rc = cmd_lab_vpn(_VpnAPI(), cfg,
                     _args(identifier="widget", output=str(dest),
                           print=False, force=True))
    assert rc == 0
    assert "remote lab.example" in dest.read_text()


# ── HTTP timeouts ─────────────────────────────────────────────────────────

def test_request_always_passes_a_timeout(cfg, monkeypatch):
    api = HackSmarterAPI(cfg)
    seen = {}

    class _R:
        status_code = 200
        content = b"{}"

        def raise_for_status(self):
            pass

        def json(self):
            return {}

    def get(url, **kw):
        seen.update(kw)
        return _R()

    monkeypatch.setattr(api.session, "get", get)
    api._request("GET", "/api/student/profile")
    assert seen["timeout"] == DEFAULT_TIMEOUT


def test_power_call_always_passes_a_timeout(cfg, monkeypatch):
    api = HackSmarterAPI(cfg)
    seen = {}

    class _R:
        status_code = 200
        content = b""

        def raise_for_status(self):
            pass

    def request(method, url, **kw):
        seen.update(kw)
        return _R()

    monkeypatch.setattr(api.session, "request", request)
    api._power_call("POST", "/power", None, "ref")
    assert seen["timeout"] == DEFAULT_TIMEOUT


# ── VPN file permissions ──────────────────────────────────────────────────

def test_vpn_profile_written_owner_only(cfg, tmp_path, monkeypatch):
    api = HackSmarterAPI(cfg)
    monkeypatch.setattr(api, "_ensure_playthrough",
                        lambda cid: {"kind": "systems",
                                     "playthrough_id": "p", "network_ids": []})

    class _R:
        headers = {"Content-Type": "application/x-openvpn-profile"}
        text = "client\n<key>K</key>"

    monkeypatch.setattr(api, "_request", lambda *a, **k: _R())
    dest = tmp_path / "lab.ovpn"
    api.get_vpn_config("course-1", dest_path=str(dest))
    assert stat.S_IMODE(os.stat(dest).st_mode) == 0o600


# ── cookie domain scoping ─────────────────────────────────────────────────

def _cookie_header_for(api, url):
    import requests
    prepared = api.session.prepare_request(requests.Request("GET", url))
    return prepared.headers.get("Cookie", "")


def test_cookie_stays_pinned_to_hacksmarter(cfg):
    cfg.set_cookie("sb-auth-auth-token.0=abc")
    api = HackSmarterAPI(cfg)
    assert "sb-auth-auth-token" in _cookie_header_for(
        api, "https://www.hacksmarter.org/api/x")
    assert _cookie_header_for(api, "https://evil.example/api/x") == ""
    assert _cookie_header_for(api, "http://localhost:3000/api/x") == ""


def test_cookie_reaches_a_loopback_dev_server(cfg):
    """--allow-insecure-http localhost is for local development — the
    stored session must actually be sent there, and nowhere else."""
    cfg.set_cookie("sb-auth-auth-token.0=abc")
    cfg.set_base_url("http://localhost:3000", allow_insecure=True)
    api = HackSmarterAPI(cfg)
    assert "sb-auth-auth-token" in _cookie_header_for(
        api, "http://localhost:3000/api/x")
    assert _cookie_header_for(api, "https://evil.example/api/x") == ""


# ── base URL safety ───────────────────────────────────────────────────────

def test_http_base_url_is_rejected(cfg):
    with pytest.raises(ValueError, match="unencrypted"):
        cfg.set_base_url("http://www.hacksmarter.org")


def test_http_base_url_allowed_with_explicit_opt_in(cfg):
    cfg.set_base_url("http://localhost:3000", allow_insecure=True)
    assert cfg.get_base_url() == "http://localhost:3000"


def test_base_url_with_embedded_credentials_is_rejected(cfg):
    with pytest.raises(ValueError, match="credentials"):
        cfg.set_base_url("https://user:pass@www.hacksmarter.org")


def test_base_url_without_hostname_is_rejected(cfg):
    with pytest.raises(ValueError, match="hostname"):
        cfg.set_base_url("hacksmarter.org")   # no scheme → no hostname


# ── config robustness ─────────────────────────────────────────────────────

def test_save_is_atomic_no_tmp_left_behind(cfg):
    cfg.set_cookie("sb-auth-auth-token.0=a")
    cfg.set_cookie("sb-auth-auth-token.0=b")
    leftovers = [p for p in os.listdir(cfg.config_dir) if p.endswith(".tmp")]
    assert leftovers == []
    assert cfg.get_cookie() == "sb-auth-auth-token.0=b"


def test_corrupt_config_warns_instead_of_silently_logging_out(tmp_path,
                                                              capsys):
    d = tmp_path / "cfg"
    d.mkdir()
    (d / "config.json").write_text("{not json")
    c = Config(str(d))
    assert c.get_cookie() is None
    assert "corrupt" in capsys.readouterr().err


# ── argument validation ───────────────────────────────────────────────────

def test_positive_int_accepts_normal_timeouts():
    assert _positive_int("420") == 420


@pytest.mark.parametrize("bad", ["0", "-5", "x"])
def test_positive_int_rejects_nonsense(bad):
    with pytest.raises(argparse.ArgumentTypeError):
        _positive_int(bad)


# ── --debug redaction ─────────────────────────────────────────────────────

def test_redact_masks_credential_bearing_keys():
    doc = {"terraform_outputs": {"cg_chris_secret_key": "S3CRET",
                                 "cg_chris_access_key_id": "AKIA",
                                 "region": "us-east-1"},
           "signed_url": "https://s3/…?sig=x",
           "name": "Widget"}
    red = HackSmarterAPI._redact(doc)
    assert red["terraform_outputs"]["cg_chris_secret_key"] == "«redacted»"
    assert red["terraform_outputs"]["cg_chris_access_key_id"] == "«redacted»"
    assert red["terraform_outputs"]["region"] == "us-east-1"
    assert red["signed_url"] == "«redacted»"
    assert red["name"] == "Widget"


def test_redact_masks_secrets_held_in_containers():
    """A sensitive key masks its whole value — a list of tokens is the
    same secret its key names."""
    doc = {"tokens": ["eyJliveJWT"], "access_keys": ["AKIA1", "AKIA2"],
           "machines": [{"name": "DC-01", "session_token": "T"}]}
    red = HackSmarterAPI._redact(doc)
    assert red["tokens"] == "«redacted»"
    assert red["access_keys"] == "«redacted»"
    assert red["machines"][0]["name"] == "DC-01"
    assert red["machines"][0]["session_token"] == "«redacted»"


def test_debug_trace_redacts_unless_raw_requested(cfg, capsys, monkeypatch):
    api = HackSmarterAPI(cfg, debug=True)
    payload = {"secret_key": "S3CRET"}

    monkeypatch.delenv("HSMCLI_DEBUG_RAW", raising=False)
    api._trace("GET", "/x", None, payload=payload)
    assert "S3CRET" not in capsys.readouterr().err

    monkeypatch.setenv("HSMCLI_DEBUG_RAW", "1")
    api._trace("GET", "/x", None, payload=payload)
    assert "S3CRET" in capsys.readouterr().err
