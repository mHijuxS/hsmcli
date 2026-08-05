"""Pure display/parsing helpers from the CLI layer."""

import pytest

from hsmcli.cli import (
    _aws_env_exports,
    _extract_difficulty,
    _extract_state,
    _flatten_lab_items,
    _guess_image_ext,
    _lab_category,
    _match_question,
    _parse_kv,
    _submission_verdict,
    _system_ip,
    _system_status,
)

from conftest import CATALOG_BUNDLE, CATALOG_COURSE, ENROLLED


# ── difficulty / state ────────────────────────────────────────────────────

@pytest.mark.parametrize("name,want", [
    ("Challenge Lab: Widget (Easy)", "Easy"),
    ("Challenge Lab: Gadget (Hard)", "Hard"),
    ("Range: Flywheel (Insane)", "Insane"),
    ("Challenge Lab: Thing (medium)", "Medium"),   # lowercase in the title
    ("Foundations of Ethical Hacking", ""),        # no difficulty at all
])
def test_extract_difficulty_from_title(name, want):
    """HackSmarter has no difficulty field — it's embedded in the name."""
    assert _extract_difficulty({"name": name}) == want


def test_extract_difficulty_prefers_a_real_field():
    assert _extract_difficulty({"name": "X (Easy)", "difficulty": "Hard"}) == "Hard"


def test_extract_state_top_level_wins():
    assert _extract_state(ENROLLED[1]) == "in_progress"


def test_extract_state_falls_back_to_nested_content_state():
    assert _extract_state(CATALOG_COURSE) == "not_started"


def test_extract_state_falls_back_to_ownership():
    """Bundle cards have no content_state, only ownership."""
    assert _extract_state(CATALOG_BUNDLE) == "not_owned"


def test_extract_state_empty_when_unknown():
    assert _extract_state({"name": "x"}) == ""


# ── categories ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,want", [
    ("Challenge Lab: Widget (Easy)", "challenge"),
    ("Guided Lab: Sprocket (Easy)", "guided"),
    ("Range: Flywheel (Insane)", "range"),
    ("Hack With Me: Linux Hacking", "hackwith"),
    ("Foundations of Ethical Hacking", "foundations"),
    ("Sliver C2: Pentesting and Evasion", "other"),
    ("What Is Hack Smarter?", "other"),
    ("", "other"),
])
def test_lab_category(name, want):
    assert _lab_category(name) == want


# ── systems / networks flattening ─────────────────────────────────────────

def test_flatten_expands_a_network_into_its_machines():
    payload = [{
        "course_network_id": "net-wrapper",
        "network": {"name": "Subnet", "systems": [
            {"id": "m1", "name": "DC", "state": "running", "ip_address": "10.0.0.1"},
            {"id": "m2", "name": "WS", "state": "stopped"},
        ]},
    }]
    out = _flatten_lab_items(payload)
    assert [m["name"] for m in out] == ["DC", "WS"]
    assert all(m["_network"] == "Subnet" for m in out)


def test_flatten_passes_systems_entries_through():
    payload = [{"id": "s1", "system": {"name": "Widget", "state": "running"}}]
    assert _flatten_lab_items(payload) == payload


def test_flatten_empty():
    assert _flatten_lab_items([]) == []


@pytest.mark.parametrize("item,want", [
    ({"status": "running"}, "running"),
    ({"state": "stopped"}, "stopped"),
    ({"system": {"state": "provisioning"}}, "provisioning"),
    ({"instance": {"power_state": "on"}}, "on"),
    ({"running": True}, "running"),
    ({}, "not_launched"),
])
def test_system_status_across_shapes(item, want):
    assert _system_status(item) == want


@pytest.mark.parametrize("item,want", [
    ({"ip": "1.2.3.4"}, "1.2.3.4"),
    ({"ip_address": "1.2.3.4"}, "1.2.3.4"),
    ({"system": {"public_ip": "1.2.3.4"}}, "1.2.3.4"),
    ({}, ""),
])
def test_system_ip_across_shapes(item, want):
    assert _system_ip(item) == want


# ── AWS credential env exports ────────────────────────────────────────────

def test_aws_env_exports_maps_known_keys():
    lines = _aws_env_exports({
        "access_key": "AKIAEXAMPLE",
        "secret_key": "s3cr3t",
        "region": "us-east-1",
    })
    assert "export AWS_ACCESS_KEY_ID=AKIAEXAMPLE" in lines
    assert "export AWS_SECRET_ACCESS_KEY=s3cr3t" in lines
    assert "export AWS_DEFAULT_REGION=us-east-1" in lines


def test_aws_env_exports_namespaces_unknown_keys():
    assert _aws_env_exports({"lab console url": "https://x"}) == [
        "export HSM_LAB_CONSOLE_URL=https://x"]


def test_aws_env_exports_quotes_shell_metacharacters():
    """Output is meant for `eval`, so a value with spaces or $ must not
    break out of its assignment."""
    (line,) = _aws_env_exports({"secret_key": "a b; rm -rf /$HOME"})
    assert line == "export AWS_SECRET_ACCESS_KEY='a b; rm -rf /$HOME'"


def test_aws_env_exports_skips_nested_and_null_values():
    assert _aws_env_exports({"a": {"n": 1}, "b": [1], "c": None}) == []


# ── --input KEY=VALUE ─────────────────────────────────────────────────────

def test_parse_kv():
    assert _parse_kv(["a=1", "b=x=y"]) == {"a": "1", "b": "x=y"}


def test_parse_kv_allows_empty_value():
    assert _parse_kv(["a="]) == {"a": ""}


def test_parse_kv_none():
    assert _parse_kv(None) == {}


@pytest.mark.parametrize("bad", ["novalue", "=novalue"])
def test_parse_kv_rejects_malformed(bad):
    with pytest.raises(LookupError, match="KEY=VALUE"):
        _parse_kv([bad])


# ── image sniffing ────────────────────────────────────────────────────────

@pytest.mark.parametrize("data,want", [
    (b"\x89PNG\r\n\x1a\n rest", ".png"),
    (b"\xff\xd8\xff rest", ".jpg"),
    (b"GIF89a rest", ".gif"),
    (b"RIFF____WEBP rest", ".webp"),
    (b"nope", ".bin"),
    (b"", ".bin"),
])
def test_guess_image_ext(data, want):
    assert _guess_image_ext(data) == want


# ── question selection ────────────────────────────────────────────────────

QUESTIONS = [
    {"question_id": "aaaa1111-0000-0000-0000-000000000010",
     "prompt": "What is the user flag?"},
    {"question_id": "aaaa1111-0000-0000-0000-000000000011",
     "prompt": "What is the root flag?"},
]


def test_match_question_by_keyword():
    assert _match_question(QUESTIONS, "root")["prompt"] == "What is the root flag?"


def test_match_question_by_index():
    assert _match_question(QUESTIONS, "1") is QUESTIONS[0]


def test_match_question_by_uuid():
    assert _match_question(QUESTIONS, QUESTIONS[1]["question_id"]) is QUESTIONS[1]


def test_match_question_index_out_of_range():
    with pytest.raises(LookupError, match="out of range"):
        _match_question(QUESTIONS, "9")


def test_match_question_ambiguous():
    with pytest.raises(LookupError, match="ambiguous"):
        _match_question(QUESTIONS, "flag")


def test_match_question_no_match():
    with pytest.raises(LookupError, match="no question matching"):
        _match_question(QUESTIONS, "zzz")


def test_match_question_empty_list():
    with pytest.raises(LookupError, match="no questions"):
        _match_question([], "root")


# ── submission verdict ────────────────────────────────────────────────────

def test_verdict_nested_is_correct():
    """The live shape: the verdict is nested under `result` and the key is
    `is_correct`, not `correct`."""
    assert _submission_verdict(
        {"result": {"is_correct": True, "answer_text": "hsm{x}"}}
    ) == (True, "hsm{x}")


def test_verdict_flat_legacy_shape():
    assert _submission_verdict(
        {"correct": False, "matchedAnswer": {"answer": "hsm{y}"}}
    ) == (False, "hsm{y}")


def test_verdict_camel_case():
    assert _submission_verdict({"isCorrect": True, "answerText": "z"}) == (True, "z")


@pytest.mark.parametrize("payload", [
    {}, {"unrelated": 1}, "not a dict", None,
    {"result": {"is_correct": "yes"}},   # non-bool must not count
])
def test_verdict_unknown_reply_is_none_not_false(payload):
    """An unparsed reply must never render as a wrong flag."""
    correct, _ = _submission_verdict(payload)
    assert correct is None
