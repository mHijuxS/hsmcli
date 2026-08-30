"""Enrollment: the catalog id, the buy call, and the three replies.

Enrolling is a catalog operation. ``POST /courses/{id}/enroll`` — what
hsmcli used to send — is not a route the API has, so it 404'd for every
lab, owned or not; the web app posts to ``/catalog/{card}/buy`` instead.
"""

import argparse

import pytest
import requests

from hsmcli.api_client import HackSmarterAPI, HttpError
from hsmcli.cli import cmd_lab_enroll
from hsmcli.config import Config
from hsmcli.resolvers import catalog_item_id, free_purchase_option_id

from conftest import CATALOG_COURSE, ENROLLED, FakeAPI


WIDGET_COURSE = "cccccccc-0000-0000-0000-000000000001"
WIDGET_CARD = "aaaaaaaa-0000-0000-0000-000000000001"


# ── which id enroll posts to ──────────────────────────────────────────────

def test_catalog_item_id_from_a_courses_entry():
    assert catalog_item_id(ENROLLED[0]) == WIDGET_CARD


def test_catalog_item_id_from_a_catalog_card():
    """A /catalog entry *is* the card: its top-level id is the one to post
    to, and the course id is the nested item.id."""
    assert catalog_item_id(CATALOG_COURSE) == WIDGET_CARD


def test_catalog_item_id_is_never_the_course_id():
    for item in (ENROLLED[0], CATALOG_COURSE):
        assert catalog_item_id(item) != WIDGET_COURSE


@pytest.mark.parametrize("item", [
    None, {}, "not a dict",
    {"id": "cccccccc-0000-0000-0000-000000000001", "name": "no card"},
])
def test_catalog_item_id_missing(item):
    assert catalog_item_id(item) is None


# ── the free purchase option ──────────────────────────────────────────────

def test_free_purchase_option_id_picks_the_free_choice():
    """Free labs (Mapper) list a free option that still has to be *named* —
    a null option is rejected with 'A purchase option must be selected'."""
    item = {"purchase_options": [{"id": "opt-free", "type": "free"}]}
    assert free_purchase_option_id(item) == "opt-free"


def test_free_purchase_option_id_ignores_paid_only():
    item = {"purchase_options": [{"id": "opt-paid", "type": "one_time"}]}
    assert free_purchase_option_id(item) is None


@pytest.mark.parametrize("item", [None, {}, "x", {"purchase_options": []},
                                  {"purchase_options": [{"type": "free"}]}])
def test_free_purchase_option_id_missing(item):
    assert free_purchase_option_id(item) is None


# ── the request ───────────────────────────────────────────────────────────

class PostResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = str(payload)
        self.content = b"{}"
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(response=self)

    def json(self):
        return self._payload


@pytest.fixture
def api(tmp_path):
    return HackSmarterAPI(Config(str(tmp_path / "cfg")))


def test_enroll_posts_to_the_buy_endpoint(api, monkeypatch):
    seen = {}

    def post(url, **kw):
        seen["url"] = url
        seen["json"] = kw.get("json")
        return PostResponse({"state": "bought", "redirect_url": "/x"})

    monkeypatch.setattr(api.session, "post", post)
    assert api.enroll_course(WIDGET_CARD)["state"] == "bought"
    assert seen["url"].endswith(f"/api/student/catalog/{WIDGET_CARD}/buy")
    # Null purchase option: the server decides whether this is a free claim,
    # a subscription entitlement, or a checkout.
    assert seen["json"] == {"purchase_option_id": None, "promo_code": None,
                            "pwyc_price_cents": None}


def test_enroll_does_not_touch_the_courses_endpoint(api, monkeypatch):
    monkeypatch.setattr(api.session, "post",
                        lambda url, **kw: PostResponse({"state": "bought"}))
    monkeypatch.setattr(api.session, "get", lambda *a, **k: pytest.fail("GET"))
    api.enroll_course(WIDGET_CARD)


# ── the command ───────────────────────────────────────────────────────────

class EnrollAPI(FakeAPI):
    """FakeAPI plus the enroll call, recording what it was handed."""

    def __init__(self, reply=None, error=None, **kw):
        super().__init__(**kw)
        self._reply = reply if reply is not None else {"state": "bought"}
        self._error = error
        self.enrolled_with = None

    def enroll_course(self, catalog_id, **kw):
        self.enrolled_with = catalog_id
        self.enroll_kw = kw
        if self._error:
            raise self._error
        return self._reply

    def course_name(self, course_id):
        return ""


def _args(identifier="widget", **kw):
    return argparse.Namespace(identifier=identifier, json=kw.pop("json", False),
                              yaml=False, debug=False, **kw)


@pytest.fixture
def cfg(tmp_path):
    return Config(str(tmp_path / "cfg"))


def test_enroll_resolves_a_name_to_its_catalog_card(cfg, capsys):
    api = EnrollAPI()
    assert cmd_lab_enroll(api, cfg, _args("widget")) == 0
    assert api.enrolled_with == WIDGET_CARD
    assert "Enrolled in Widget" in capsys.readouterr().out


def test_enroll_forwards_the_free_purchase_option(cfg):
    """A free lab that lists a purchase option gets that option id on the
    buy call — Mapper 400s without it."""
    courses = [{"id": WIDGET_COURSE, "name": "Challenge Lab: Mapper (Medium)",
                "content_type": "course", "state": "unowned",
                "catalog_item_id": WIDGET_CARD,
                "purchase_options": [{"id": "opt-free", "type": "free"}]}]
    api = EnrollAPI(courses=courses, catalog=[])
    assert cmd_lab_enroll(api, cfg, _args("mapper")) == 0
    assert api.enrolled_with == WIDGET_CARD
    assert api.enroll_kw.get("purchase_option_id") == "opt-free"


def test_enroll_passes_no_option_when_none_is_listed(cfg):
    """Subscription-covered labs list no options; the null-option path stands."""
    api = EnrollAPI()
    assert cmd_lab_enroll(api, cfg, _args("widget")) == 0
    assert api.enroll_kw.get("purchase_option_id") is None


def test_enroll_reports_checkout_as_not_enrolled(cfg, capsys):
    """`state: checkout` means HackSmarter wants paying — the lab is not
    yours, and the exit code has to say so."""
    api = EnrollAPI({"state": "checkout",
                     "session_url": "https://checkout.stripe.test/s/1"})
    assert cmd_lab_enroll(api, cfg, _args("widget")) == 2
    out = capsys.readouterr()
    assert "https://checkout.stripe.test/s/1" in out.out
    assert "Enrolled" not in out.out


def test_enroll_twice_is_not_a_failure(cfg, capsys):
    api = EnrollAPI(error=HttpError("HTTP 400", status=400,
                                    endpoint="/api/student/catalog/x/buy",
                                    body='{"message":"User already owns course"}'))
    assert cmd_lab_enroll(api, cfg, _args("widget")) == 0
    assert "Already enrolled in Widget" in capsys.readouterr().out


def test_enroll_still_surfaces_other_400s(cfg):
    api = EnrollAPI(error=HttpError("HTTP 400", status=400,
                                    endpoint="/api/student/catalog/x/buy",
                                    body='{"message":"Course is not available"}'))
    with pytest.raises(HttpError):
        cmd_lab_enroll(api, cfg, _args("widget"))


def test_enroll_without_a_catalog_card_explains_itself(cfg, capsys):
    courses = [{"id": WIDGET_COURSE, "name": "Challenge Lab: Widget (Easy)",
                "content_type": "course", "state": "owned"}]
    api = EnrollAPI(courses=courses, catalog=[])
    assert cmd_lab_enroll(api, cfg, _args("widget")) == 2
    assert api.enrolled_with is None
    assert "nothing to enroll in" in capsys.readouterr().err


def test_enroll_json_keeps_the_exit_code_honest(cfg, capsys):
    api = EnrollAPI({"state": "checkout", "session_url": "https://pay.test/1"})
    assert cmd_lab_enroll(api, cfg, _args("widget", json=True)) == 2
    assert "checkout" in capsys.readouterr().out
