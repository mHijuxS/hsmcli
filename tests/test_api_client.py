"""Cookie/session decoding and the /take payload scrapers."""

import base64
import json

import pytest

from hsmcli.api_client import (
    HackSmarterAPI,
    decode_supabase_session,
    parse_cookie_header,
)

from conftest import TAKE


# ── parse_cookie_header ───────────────────────────────────────────────────

def test_parse_plain_pairs():
    assert parse_cookie_header("a=1; b=2") == {"a": "1", "b": "2"}


def test_parse_strips_cookie_prefix():
    assert parse_cookie_header("Cookie: a=1") == {"a": "1"}
    assert parse_cookie_header("cookie: a=1") == {"a": "1"}


def test_parse_keeps_base64_padding_in_values():
    """Supabase values contain '=' padding — only the first '=' separates."""
    assert parse_cookie_header("sb-auth-auth-token.0=base64-eyJ==") == {
        "sb-auth-auth-token.0": "base64-eyJ=="
    }


def test_parse_tolerates_trailing_semicolon_and_spaces():
    assert parse_cookie_header("  a=1 ;  b=2 ;  ") == {"a": "1", "b": "2"}


@pytest.mark.parametrize("raw", [
    "eyJhbGciOiJIUzI1NiJ9.payload.sig",   # a bare token, no name=
    "not a cookie header at all",
    "bad name=1",                          # space in the cookie name
])
def test_parse_rejects_non_cookie_input(raw):
    """Returning {} is what lets `config set-cookie` refuse the paste
    instead of storing it and failing every later call with a misleading
    'cookie may be expired'."""
    assert parse_cookie_header(raw) == {}


def test_parse_empty_is_empty():
    assert parse_cookie_header("") == {}


# ── decode_supabase_session ───────────────────────────────────────────────

def _make_session_cookies(payload, chunks=2, prefix="base64-"):
    blob = base64.urlsafe_b64encode(
        json.dumps(payload).encode()).decode().rstrip("=")
    blob = prefix + blob
    size = len(blob) // chunks + 1
    parts = [blob[i:i + size] for i in range(0, len(blob), size)]
    return {f"sb-auth-auth-token.{i}": p for i, p in enumerate(parts)}


SESSION = {
    "expires_at": 1799999999,
    "user": {
        "email": "user@example.test",
        "id": "abcd0000-0000-0000-0000-000000000001",
        "user_metadata": {"preferred_username": "someone"},
        "app_metadata": {"provider": "github"},
    },
}


@pytest.mark.parametrize("chunks", [1, 2, 3])
def test_decode_reassembles_split_cookies(chunks):
    """The session is split across .0/.1 because it exceeds the 4 KiB
    per-cookie budget; the `base64-` prefix is on the first chunk only."""
    got = decode_supabase_session(_make_session_cookies(SESSION, chunks))
    assert got == SESSION


def test_decode_without_base64_prefix():
    got = decode_supabase_session(_make_session_cookies(SESSION, 1, prefix=""))
    assert got == SESSION


def test_decode_returns_none_without_auth_cookies():
    assert decode_supabase_session({"_ga": "noise"}) is None


def test_decode_returns_none_on_garbage():
    assert decode_supabase_session({"sb-auth-auth-token.0": "!!!not-b64!!!"}) is None


def test_decode_stops_at_the_first_gap():
    """Chunks are consumed in order; a missing .1 means .2 is unreachable,
    so the concatenation is incomplete and must not half-decode."""
    cookies = _make_session_cookies(SESSION, 2)
    cookies["sb-auth-auth-token.2"] = cookies.pop("sb-auth-auth-token.1")
    assert decode_supabase_session(cookies) is None


# ── extract_system_ids / extract_network_ids ──────────────────────────────

def test_extract_system_ids_finds_static_and_lesson_references():
    ids = HackSmarterAPI.extract_system_ids(TAKE)
    assert "55550000-0000-0000-0000-00000000000c" in ids


def test_extract_system_ids_dedupes():
    """The same system appears in static_systems and in a lesson item."""
    ids = HackSmarterAPI.extract_system_ids(TAKE)
    assert len(ids) == len(set(ids))


def test_extract_network_ids_finds_course_networks():
    ids = HackSmarterAPI.extract_network_ids(TAKE)
    assert "77770000-0000-0000-0000-00000000000d" in ids


def test_extract_network_ids_ignores_systems():
    ids = HackSmarterAPI.extract_network_ids(TAKE)
    assert "55550000-0000-0000-0000-00000000000c" not in ids


@pytest.mark.parametrize("payload", [None, {}, [], "junk", {"course": None}])
def test_extractors_survive_junk(payload):
    assert HackSmarterAPI.extract_system_ids(payload) == []
    assert HackSmarterAPI.extract_network_ids(payload) == []
    assert HackSmarterAPI.extract_aws_labs(payload) == []
    assert HackSmarterAPI.extract_lessons(payload) == []
    assert HackSmarterAPI.extract_questions(payload) == []


# ── extract_aws_labs ──────────────────────────────────────────────────────

def test_extract_aws_labs_pairs_id_with_name():
    """static_aws_labs sits BESIDE `course`, not inside it — the name comes
    from there, the reference from the lesson item."""
    assert HackSmarterAPI.extract_aws_labs(TAKE) == [
        {"id": "ffffffff-0000-0000-0000-00000000000a", "name": "WidgetAWS"},
    ]


def test_extract_aws_labs_surfaces_nameless_lesson_references():
    """A lab referenced only by a lesson item still has to show up."""
    take = {"course": {"chapters": [{"lessons": [{"content": {"items": [
        {"type": "aws-lab", "aws_lab_id": "no-name-lab"}]}}]}]}}
    assert HackSmarterAPI.extract_aws_labs(take) == [
        {"id": "no-name-lab", "name": ""}]


# ── extract_lessons ───────────────────────────────────────────────────────

def test_extract_lessons_keeps_chapter_context_and_items():
    lessons = HackSmarterAPI.extract_lessons(TAKE)
    assert [l["lesson"] for l in lessons] == ["Briefing", "Empty"]
    first = lessons[0]
    assert first["chapter"] == "Chapter 1"
    assert first["completed"] is True
    assert first["lesson_id"] == "eeee0000-0000-0000-0000-00000000000e"
    assert len(first["items"]) == 6


def test_extract_lessons_gives_contentless_lessons_an_empty_item_list():
    """`lab info` filters on truthy items, so this must be [] not None."""
    assert HackSmarterAPI.extract_lessons(TAKE)[1]["items"] == []


# ── extract_questions ─────────────────────────────────────────────────────

def test_extract_questions_flattens_with_lesson_ids():
    qs = HackSmarterAPI.extract_questions(TAKE)
    assert len(qs) == 2
    # lesson_id comes from the enclosing lesson, not the question item —
    # submit-question needs it and the item doesn't carry it.
    assert all(q["lesson_id"] == "eeee0000-0000-0000-0000-00000000000e"
               for q in qs)
    assert all(q["content_id"] == "ffff0000-0000-0000-0000-00000000000f"
               for q in qs)


def test_extract_questions_reads_prompt_state_and_attempt():
    q = HackSmarterAPI.extract_questions(TAKE)[0]
    assert q["prompt"] == "What is the user flag?"
    assert q["state"] == "correct"
    assert q["points"] == 10
    assert q["has_hint"] is True
    assert q["last_submission"] == "hsm{user}"
    assert q["last_correct"] is True


def test_extract_questions_handles_never_attempted():
    q = HackSmarterAPI.extract_questions(TAKE)[1]
    assert q["state"] == "not_attempted"
    assert q["last_submission"] is None
    assert q["last_correct"] is None


def test_extract_questions_ignores_non_question_items():
    types = {q["type"] for q in HackSmarterAPI.extract_questions(TAKE)}
    assert types == {"question-free-text"}
