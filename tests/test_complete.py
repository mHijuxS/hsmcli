"""Completing a lab, and pulling its certificate PDF.

Two endpoints the web app uses on the /take and /completion pages:

  * ``POST /content/{playthrough}/lessons/{lesson}/complete`` — the
    "in progress → complete" toggle, one lesson at a time. Same id
    convention as ``submit-question``: the ``/content/`` segment is the
    *playthrough* id, not the lesson's own ``content.id``.
  * ``GET /completion/course/{completion_id}/certificate`` — hands back a
    one-hour pre-signed S3 URL to the certificate PDF; ``completion_id``
    lives on ``course_playthrough`` and only exists once the lab is done.
"""

import argparse

import pytest
import requests

import hsmcli.api_client as api_mod
from hsmcli.api_client import HackSmarterAPI, HttpError, NotEnrolledError
from hsmcli.cli import cmd_lab_complete, cmd_lab_certificate
from hsmcli.config import Config

from conftest import FakeAPI


WIDGET_COURSE = "cccccccc-0000-0000-0000-000000000001"
PLAYTHROUGH = "0ef10702-c248-4522-a47a-f8d5cf0a143f"
LESSON_A = "be2ceb45-1b90-4b18-ad74-e65cce22bff9"
LESSON_B = "11111111-2222-3333-4444-555555555555"
COMPLETION_ID = "bb7224db35d9e573"


def _take(playthrough=PLAYTHROUGH, is_complete=False, completion_id=None,
          lessons=((LESSON_A, False),)):
    """A minimal /take payload: one chapter, the given lessons, and a
    playthrough carrying the completion fields."""
    return {"course": {
        "id": WIDGET_COURSE,
        "name": "Widget",
        "course_playthrough": {"id": playthrough, "is_complete": is_complete,
                               "completion_id": completion_id},
        "chapters": [{"name": "Ch1", "lessons": [
            {"id": lid, "name": f"Lesson {lid[:4]}", "completed": done,
             "content": {"id": f"content-{lid[:4]}", "items": []}}
            for lid, done in lessons]}],
    }}


class FakeResp:
    def __init__(self, payload=None, content=b"", status=200):
        self._payload = payload
        self.content = content if content else (b"{}" if payload else b"")
        self.status_code = status
        self.text = str(payload)
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(response=self)

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture
def api(tmp_path):
    return HackSmarterAPI(Config(str(tmp_path / "cfg")))


# ── complete_lesson: the request ──────────────────────────────────────────

def test_complete_lesson_posts_to_the_playthrough_not_the_content_id(api, monkeypatch):
    monkeypatch.setattr(api, "get_course_take", lambda *a, **k: _take())
    seen = {}

    def request(method, url, **kw):
        seen["method"] = method
        seen["url"] = url
        seen["data"] = kw.get("data")
        seen["json"] = kw.get("json")
        seen["referer"] = kw.get("headers", {}).get("Referer")
        return FakeResp(content=b"")

    monkeypatch.setattr(api.session, "request", request)
    api.complete_lesson(WIDGET_COURSE, LESSON_A)

    assert seen["method"] == "POST"
    # The /content/ segment is the playthrough id — the lesson's own
    # content.id (content-be2c here) 403s.
    assert seen["url"].endswith(
        f"/api/student/content/{PLAYTHROUGH}/lessons/{LESSON_A}/complete")
    assert "content-be2c" not in seen["url"]
    # Empty body, and the same-origin Referer the server checks.
    assert seen["data"] == b"" and seen["json"] is None
    assert seen["referer"].endswith(f"/courses/{WIDGET_COURSE}/take")


def test_complete_lesson_without_a_playthrough_is_not_enrolled(api, monkeypatch):
    monkeypatch.setattr(api, "get_course_take",
                        lambda *a, **k: _take(playthrough=None))
    with pytest.raises(NotEnrolledError):
        api.complete_lesson(WIDGET_COURSE, LESSON_A)


# ── complete_course: walk the lessons, read back the result ───────────────

def test_complete_course_posts_every_lesson_including_ones_shown_done(api, monkeypatch):
    """The POST is what flips the course, not the `completed` flag — so even
    a lesson /take already shows as done gets one. The buckets still record
    which was which, but both are posted."""
    takes = iter([
        _take(lessons=((LESSON_A, True), (LESSON_B, False))),   # before
        _take(is_complete=True, completion_id=COMPLETION_ID,     # after refetch
              lessons=((LESSON_A, True), (LESSON_B, True))),
    ])
    monkeypatch.setattr(api, "get_course_take", lambda *a, **k: next(takes))
    posted = []
    monkeypatch.setattr(api, "complete_lesson",
                        lambda cid, lid: posted.append(lid) or {})

    result = api.complete_course(WIDGET_COURSE)
    assert posted == [LESSON_A, LESSON_B]       # both, not just the open one
    assert result["completed"] == [LESSON_B]    # wasn't showing done before
    assert result["already"] == [LESSON_A]      # was — but still got the POST
    assert result["is_complete"] is True
    assert result["completion_id"] == COMPLETION_ID


def test_complete_course_refetches_take_after_mutating(api, monkeypatch):
    """The completion_id is minted on the last POST, so the state we report
    has to come from a /take read that bypasses the cache."""
    api._take_cache[WIDGET_COURSE] = _take(lessons=((LESSON_A, False),))
    calls = {"n": 0}

    def get_take(course_id, use_cache=False):
        # A cached read would hand back the stale, incomplete payload.
        if use_cache and course_id in api._take_cache:
            return api._take_cache[course_id]
        calls["n"] += 1
        return _take(is_complete=True, completion_id=COMPLETION_ID,
                     lessons=((LESSON_A, True),))

    monkeypatch.setattr(api, "get_course_take", get_take)
    monkeypatch.setattr(api, "complete_lesson", lambda cid, lid: {})
    result = api.complete_course(WIDGET_COURSE)
    assert calls["n"] >= 1                       # a fresh read happened
    assert result["completion_id"] == COMPLETION_ID


def test_complete_course_with_no_lessons_is_not_enrolled(api, monkeypatch):
    monkeypatch.setattr(api, "get_course_take",
                        lambda *a, **k: {"course": {"chapters": []}})
    with pytest.raises(NotEnrolledError):
        api.complete_course(WIDGET_COURSE)


# ── the certificate URL + download ────────────────────────────────────────

SIGNED = "https://certificates.s3.amazonaws.com/x.pdf?X-Amz-Signature=deadbeef"


def test_certificate_download_url_returns_the_signed_link(api, monkeypatch):
    monkeypatch.setattr(api.session, "get",
                        lambda *a, **k: FakeResp({"url": SIGNED}))
    assert api.certificate_download_url(COMPLETION_ID) == SIGNED


def test_certificate_download_url_raises_when_empty(api, monkeypatch):
    monkeypatch.setattr(api.session, "get", lambda *a, **k: FakeResp({}))
    with pytest.raises(HttpError):
        api.certificate_download_url(COMPLETION_ID)


def test_download_certificate_writes_the_pdf(api, tmp_path, monkeypatch):
    monkeypatch.setattr(api, "certificate_download_url", lambda cid: SIGNED)
    got = {}

    def fake_get(url, **kw):
        got["url"] = url
        return FakeResp(content=b"%PDF-1.4 data")

    # The signed URL carries its own auth and isn't under base_url, so the
    # download is a plain requests.get, not a session call.
    monkeypatch.setattr(api_mod.requests, "get", fake_get)
    dest = tmp_path / "cert.pdf"
    data = api.download_certificate(COMPLETION_ID, dest_path=str(dest))
    assert got["url"] == SIGNED
    assert data == b"%PDF-1.4 data"
    assert dest.read_bytes() == b"%PDF-1.4 data"


# ── the commands ──────────────────────────────────────────────────────────

class CompleteAPI(FakeAPI):
    """FakeAPI plus the completion/certificate surface the commands touch."""

    base_url = "https://www.hacksmarter.org"

    def __init__(self, complete_reply=None, completion_id=None,
                 is_complete=True, cert_bytes=b"%PDF", **kw):
        super().__init__(**kw)
        self._complete_reply = complete_reply
        self._completion_id = completion_id
        self._is_complete = is_complete
        self._cert_bytes = cert_bytes
        self.completed_course = None
        self.downloaded = None

    def course_name(self, course_id):
        return ""

    def complete_course(self, course_id):
        self.completed_course = course_id
        return self._complete_reply

    def course_completion(self, course_id):
        return {"is_complete": self._is_complete,
                "completion_id": self._completion_id}

    def certificate_download_url(self, completion_id):
        return f"{self.base_url}/signed/{completion_id}.pdf?sig=x"

    def download_certificate(self, completion_id, dest_path=None):
        self.downloaded = (completion_id, dest_path)
        if dest_path:
            with open(dest_path, "wb") as f:
                f.write(self._cert_bytes)
        return self._cert_bytes


def _args(identifier="widget", **kw):
    return argparse.Namespace(identifier=identifier, json=kw.pop("json", False),
                              yaml=False, debug=False, **kw)


@pytest.fixture
def cfg(tmp_path):
    return Config(str(tmp_path / "cfg"))


def test_complete_command_reports_the_certificate_when_finished(cfg, capsys):
    api = CompleteAPI(complete_reply={"completed": [LESSON_A], "already": [],
                                      "is_complete": True,
                                      "completion_id": COMPLETION_ID})
    assert cmd_lab_complete(api, cfg, _args("widget")) == 0
    assert api.completed_course == WIDGET_COURSE
    out = capsys.readouterr().out
    assert "is complete" in out
    assert COMPLETION_ID in out               # points at the certificate


def test_complete_command_when_flags_still_open_is_a_nonzero(cfg, capsys):
    """Lessons ticked but the course isn't done — HSM gates the certificate
    on the flags, and the exit code says the ask didn't fully land."""
    api = CompleteAPI(complete_reply={"completed": [LESSON_A], "already": [],
                                      "is_complete": False,
                                      "completion_id": None})
    assert cmd_lab_complete(api, cfg, _args("widget")) == 1
    assert "isn't finished" in capsys.readouterr().out


def test_complete_command_json_exit_code_follows_is_complete(cfg):
    api = CompleteAPI(complete_reply={"completed": [], "already": [LESSON_A],
                                      "is_complete": False,
                                      "completion_id": None})
    assert cmd_lab_complete(api, cfg, _args("widget", json=True)) == 1


def test_certificate_command_without_completion_explains_itself(cfg, capsys):
    api = CompleteAPI(completion_id=None, is_complete=False)
    assert cmd_lab_certificate(
        api, cfg, _args("widget", output=None, url_only=False)) == 2
    err = capsys.readouterr().err
    assert "no certificate yet" in err
    assert api.downloaded is None


def test_certificate_command_gates_on_is_complete_not_the_id(cfg, capsys):
    """The completion_id is minted at enroll and is always present, so an
    unfinished lab that already has one still has no certificate — the cert
    endpoint 404s until is_complete flips."""
    api = CompleteAPI(completion_id=COMPLETION_ID, is_complete=False)
    assert cmd_lab_certificate(
        api, cfg, _args("widget", output=None, url_only=False)) == 2
    assert "no certificate yet" in capsys.readouterr().err
    assert api.downloaded is None


def test_certificate_command_downloads_to_a_named_file(cfg, tmp_path, capsys):
    api = CompleteAPI(completion_id=COMPLETION_ID, cert_bytes=b"%PDF-1.4")
    dest = tmp_path / "widget.pdf"
    rc = cmd_lab_certificate(
        api, cfg, _args("widget", output=str(dest), url_only=False))
    assert rc == 0
    assert api.downloaded == (COMPLETION_ID, str(dest))
    assert dest.read_bytes() == b"%PDF-1.4"
    assert "Certificate for" in capsys.readouterr().out


def test_certificate_command_url_only_prints_the_signed_link(cfg, capsys):
    api = CompleteAPI(completion_id=COMPLETION_ID)
    rc = cmd_lab_certificate(
        api, cfg, _args("widget", output=None, url_only=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert f"/signed/{COMPLETION_ID}.pdf" in out
    assert api.downloaded is None             # nothing written


def test_certificate_command_json_carries_both_urls(cfg, capsys):
    api = CompleteAPI(completion_id=COMPLETION_ID)
    rc = cmd_lab_certificate(
        api, cfg, _args("widget", output=None, url_only=False, json=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert COMPLETION_ID in out
    assert "completion_url" in out and "download_url" in out
