"""Typed exceptions, IP validation, and incomplete-command exit codes."""

import pytest
import requests

from hsmcli.api_client import (
    APIError,
    AuthError,
    ForbiddenError,
    HsmcliError,
    NotEnrolledError,
    TransportError,
    HackSmarterAPI,
    detect_public_ip,
)
from hsmcli.cli import _need_subcommand
from hsmcli.config import Config


@pytest.fixture
def api(tmp_path):
    return HackSmarterAPI(Config(str(tmp_path / "cfg")))


class ErrorResponse:
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text
        self.content = text.encode()
        self.headers = {}

    def raise_for_status(self):
        raise requests.exceptions.HTTPError(response=self)


# ── the hierarchy ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("cls", [
    AuthError, ForbiddenError, NotEnrolledError, APIError, TransportError,
])
def test_everything_subclasses_hsmclierror_and_exception(cls):
    """The CLI's catch-alls and resolvers' `except Exception` fallbacks must
    keep working, so nothing may escape Exception."""
    assert issubclass(cls, HsmcliError)
    assert issubclass(cls, Exception)


def test_401_raises_autherror(api, monkeypatch):
    monkeypatch.setattr(api.session, "get",
                        lambda *a, **k: ErrorResponse(401, '{"error":"nope"}'))
    with pytest.raises(AuthError, match="Cookie may be expired"):
        api._request("GET", "/api/student/profile")


def test_403_raises_forbiddenerror_with_enroll_hint(api, monkeypatch):
    cid = "1205dc56-4441-47f0-b7d0-47b2113c43dc"
    monkeypatch.setattr(api.session, "get",
                        lambda *a, **k: ErrorResponse(403, '{"error":"forbidden"}'))
    with pytest.raises(ForbiddenError, match="enroll"):
        api._request("GET", f"/api/student/courses/{cid}/take")


def test_other_status_raises_apierror_carrying_detail(api, monkeypatch):
    monkeypatch.setattr(api.session, "get",
                        lambda *a, **k: ErrorResponse(404, "gone"))
    with pytest.raises(APIError) as e:
        api._request("GET", "/api/student/nope")
    assert e.value.status == 404
    assert e.value.endpoint == "/api/student/nope"
    assert "gone" in e.value.body


def test_connection_failure_raises_transporterror(api, monkeypatch):
    def boom(*a, **k):
        raise requests.exceptions.ConnectTimeout("no route")
    monkeypatch.setattr(api.session, "get", boom)
    with pytest.raises(TransportError, match="Request failed"):
        api._request("GET", "/api/student/profile")


def test_auth_error_is_catchable_as_hsmclierror(api, monkeypatch):
    """The point of the hierarchy: one except clause for our errors."""
    monkeypatch.setattr(api.session, "get", lambda *a, **k: ErrorResponse(401))
    with pytest.raises(HsmcliError):
        api._request("GET", "/x")


def test_missing_playthrough_raises_notenrollederror(api, monkeypatch):
    monkeypatch.setattr(api, "get_course_take", lambda *a, **k: {"course": {}})
    with pytest.raises(NotEnrolledError, match="enroll first"):
        api._aws_lab_base("some-course")


# ── detect_public_ip validation ───────────────────────────────────────────

class IPResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


@pytest.mark.parametrize("bad", [
    ".......", "::::::::", "999.999.999.999", "<html>error</html>", "",
    "1.2.3.4 and more",
])
def test_garbage_ip_is_rejected(monkeypatch, bad):
    """A bogus value here becomes an AWS lab's allowed_ip. The old regex
    accepted every one of these."""
    monkeypatch.setattr(requests, "get", lambda *a, **k: IPResponse(bad))
    assert detect_public_ip() is None


@pytest.mark.parametrize("good", ["1.2.3.4", "203.0.113.7", "2001:db8::1"])
def test_valid_ip_accepted(monkeypatch, good):
    monkeypatch.setattr(requests, "get", lambda *a, **k: IPResponse(good + "\n"))
    assert detect_public_ip() == good


def test_falls_through_to_the_next_service(monkeypatch):
    calls = []

    def fake_get(url, **k):
        calls.append(url)
        if len(calls) < 3:
            raise requests.exceptions.ConnectTimeout("down")
        return IPResponse("198.51.100.9")

    monkeypatch.setattr(requests, "get", fake_get)
    assert detect_public_ip() == "198.51.100.9"
    assert len(calls) == 3


# ── incomplete commands ───────────────────────────────────────────────────

def test_need_subcommand_exits_2_and_lists_actions(capsys):
    """`hsmcli labs` used to print help and return 0 — a script couldn't tell
    it hadn't done anything."""
    rc = _need_subcommand("labs", ["list"])
    assert rc == 2
    err = capsys.readouterr()
    combined = err.out + err.err
    assert "needs an action" in combined
    assert "list" in combined


def test_need_subcommand_sorts_actions(capsys):
    _need_subcommand("lab", ["stop", "info", "launch"])
    combined = "".join(capsys.readouterr())
    assert "info | launch | stop" in combined


# ── power calls report *why* ──────────────────────────────────────────────
# /launch, /power and /reset bypass _request (they need a per-call Referer)
# and used to let requests' own HTTPError through, so a rejected launch said
# only "400 Client Error: Bad Request for url: …" — the server's reason was
# thrown away.

def test_power_call_surfaces_the_server_message(api, monkeypatch):
    monkeypatch.setattr(
        api.session, "request",
        lambda *a, **k: ErrorResponse(400, '{"message":"System is already running"}'))
    with pytest.raises(APIError) as exc:
        api._power_call("POST", "/systems/x/power", {"power": "on"}, "ref")
    assert "already running" in str(exc.value)
    assert exc.value.status == 400


def test_power_call_maps_401_like_every_other_call(api, monkeypatch):
    monkeypatch.setattr(api.session, "request",
                        lambda *a, **k: ErrorResponse(401, "nope"))
    with pytest.raises(AuthError):
        api._power_call("POST", "/systems/x/power", {"power": "on"}, "ref")


def test_launch_tolerates_power_rejected_while_provisioning(api, monkeypatch):
    """A system that just accepted /launch is already coming up, and the
    server rejects /power until the instance exists. That race must not
    report a failed launch on a machine that is booting."""
    monkeypatch.setattr(api, "_ensure_playthrough", lambda cid: {
        "playthrough_id": "pt", "course_id": cid, "kind": "systems"})

    def fake_power(method, path, body, referer):
        if path.endswith("/launch"):
            return {"success": True}
        raise APIError("HTTP 400", status=400, endpoint=path, body="too early")

    monkeypatch.setattr(api, "_power_call", fake_power)
    out = api.launch_system("course", "sys")
    assert out["provisioning"] is True


def test_launch_still_raises_when_provisioning_never_started(api, monkeypatch):
    """No /launch ack means nothing is coming up — the power failure is real."""
    monkeypatch.setattr(api, "_ensure_playthrough", lambda cid: {
        "playthrough_id": "pt", "course_id": cid, "kind": "networks"})

    def fake_power(method, path, body, referer):
        raise APIError("HTTP 400", status=400, endpoint=path, body="nope")

    monkeypatch.setattr(api, "_power_call", fake_power)
    with pytest.raises(APIError):
        api.launch_system("course", "net")
