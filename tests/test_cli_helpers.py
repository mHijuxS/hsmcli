"""Pure display/parsing helpers from the CLI layer."""

import pytest

from hsmcli.cli import (
    _aws_env_exports,
    _aws_env_map,
    _creds_body,
    _drop_writeups,
    _md_sections,
    _objective_scope,
    _only_writeups,
    _extract_difficulty,
    _extract_state,
    _flatten_lab_items,
    _guess_image_ext,
    _lab_category,
    _lab_topics,
    _matches_topic,
    _topic_arg,
    _topic_label,
    _match_question,
    _parse_kv,
    _submission_verdict,
    _system_ip,
    _system_status,
)
from hsmcli.resolvers import _item_id

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


# ── topics ────────────────────────────────────────────────────────────────
# The website has no topic field either — it keyword-matches the subtitle
# client-side. These cases are lifted from real catalog subtitles and were
# diffed against the page's own bundle, so a regression here is a
# divergence from what clicking the chip shows.

@pytest.mark.parametrize("subtitle,want", [
    ("This is a Medium AWS challenge lab.", ["aws"]),
    ("This is a Medium Active Directory challenge lab. ", ["active_directory"]),
    ("This is a Medium Web and Linux challenge lab. ", ["web", "linux"]),
    ("This is a Hard Active Directory and Linux challenge lab.",
     ["linux", "active_directory"]),          # site order, not subtitle order
    ("This is a Medium Windows & Linux challenge lab. ", ["windows", "linux"]),
    ("This is an Easy Blue Team challenge lab. ", ["blue_team"]),
    ("This is a Medium Web App challenge lab. ", ["web"]),
    ("This is an Easy Guided Lab. ", []),     # no subject named -> misc
    ("This is an Easy challenge lab. ", []),
    ("", []),
])
def test_lab_topics_match_the_sites_subtitle_keywords(subtitle, want):
    assert _lab_topics({"subtitle": subtitle}) == want


def test_lab_topics_read_the_courses_spelling_of_the_subtitle():
    """/catalog says ``subtitle``; /courses says ``description``."""
    assert _lab_topics({"description": "This is an Easy AWS challenge lab."}) \
        == ["aws"]


@pytest.mark.parametrize("subtitle", [
    "Webhooks and you",        # 'web' must not match inside a longer word
    "A course about awslogs",  # nor 'aws'
])
def test_lab_topics_respect_word_boundaries(subtitle):
    assert _lab_topics({"subtitle": subtitle}) == []


def test_miscellaneous_is_the_labs_with_no_topic():
    misc = {"subtitle": "This is an Easy challenge lab."}
    assert _matches_topic(misc, "miscellaneous")
    assert not _matches_topic({"subtitle": "an Easy AWS lab"}, "miscellaneous")


def test_guided_lab_is_matched_on_the_title_not_the_subtitle():
    """The one chip the site keys off the title — "This is an Easy Guided
    Lab." names no subject, so the subtitle can't carry it."""
    item = {"name": "Guided Lab: Bloodhound (Easy)",
            "subtitle": "This is an Easy Guided Lab."}
    assert _matches_topic(item, "guided_lab")
    assert not _matches_topic({"name": "Challenge Lab: Dark (Easy)",
                               "subtitle": "This is an Easy Linux lab."},
                              "guided_lab")


def test_a_guided_lab_still_carries_its_own_topic():
    item = {"name": "Guided Lab: IAM Enumeration (Easy)",
            "subtitle": "This is an Easy AWS Guided Lab. "}
    assert _matches_topic(item, "guided_lab")
    assert _matches_topic(item, "aws")


def test_topic_label_joins_multiple_topics():
    it = {"subtitle": "This is a Medium Windows & Linux challenge lab."}
    assert _topic_label(it) == "Windows/Linux"


def test_topic_label_abbreviates_active_directory_for_the_table():
    it = {"subtitle": "This is a Hard Active Directory challenge lab."}
    assert _topic_label(it) == "Active Directory"
    assert _topic_label(it, short=True) == "AD"


@pytest.mark.parametrize("given,want", [
    ("ad", "active_directory"),
    ("AD", "active_directory"),
    ("active directory", "active_directory"),
    ("Active-Directory", "active_directory"),
    ("web app", "web"),
    ("misc", "miscellaneous"),
    ("guided", "guided_lab"),
    ("all", "all"),
])
def test_topic_arg_accepts_what_people_type(given, want):
    assert _topic_arg(given) == want


def test_topic_arg_rejects_an_unknown_topic():
    import argparse
    with pytest.raises(argparse.ArgumentTypeError):
        _topic_arg("kubernetes")


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


# The live shape: machines sit flat on the wrapper, not under a "network"
# key, and are keyed by systemId. Odyssey rendered as one "not_launched"
# row until _flatten_lab_items learned this.
NETWORK_PAYLOAD = [{
    "id": "7de7b335",
    "name": "Odyssey",
    "systems": [
        {"systemId": "s1", "name": "DC-01", "state": "running",
         "ip": "10.1.77.132", "hostname": "DC-01"},
        {"systemId": "s2", "name": "WKST-01", "state": "running",
         "ip": "10.1.1.75", "hostname": "WKST-01"},
        {"systemId": "s3", "name": "Web-01", "state": "running",
         "ip": "10.1.151.67", "hostname": "Web-01"},
    ],
}]


def test_flatten_expands_a_flat_network_wrapper():
    out = _flatten_lab_items(NETWORK_PAYLOAD)
    assert [m["name"] for m in out] == ["DC-01", "WKST-01", "Web-01"]
    assert [_system_ip(m) for m in out] == ["10.1.77.132", "10.1.1.75", "10.1.151.67"]
    assert all(m["_network"] == "Odyssey" for m in out)


def test_flat_network_machines_keep_their_ids():
    assert [_item_id(m) for m in _flatten_lab_items(NETWORK_PAYLOAD)] == \
        ["s1", "s2", "s3"]


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


@pytest.mark.parametrize("states,want", [
    (["running", "running", "running"], "running"),
    (["running", "starting", "running"], "starting"),
    (["running", "stopped"], "stopped"),
    (["running", "error"], "error"),
    ([], "not_launched"),
])
def test_network_wrapper_status_folds_its_machines(states, want):
    wrapper = {"id": "net", "name": "Odyssey",
               "systems": [{"systemId": f"s{i}", "state": s}
                           for i, s in enumerate(states)]}
    # An empty systems[] is not a network wrapper — it falls through to
    # the leaf path, which also answers "not_launched".
    assert _system_status(wrapper) == want


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


def test_aws_env_exports_reads_prefixed_cloudgoat_keys():
    """CloudGoat names its outputs after the scenario and the IAM user it
    minted, so the suffix is the only part that says what the value is."""
    lines = _aws_env_exports({
        "cloudgoat_output_chris_access_key_id": "AKIAEXAMPLE",
        "cloudgoat_output_chris_secret_key": "s3cr3t",
    })
    assert lines == [
        "export AWS_ACCESS_KEY_ID=AKIAEXAMPLE",
        "export AWS_SECRET_ACCESS_KEY=s3cr3t",
    ]


def test_aws_env_exports_reads_secret_access_key_suffix():
    """`..._secret_access_key` ends with `_access_key` too — longest wins."""
    assert _aws_env_exports({"lab_chris_secret_access_key": "s"}) == [
        "export AWS_SECRET_ACCESS_KEY=s"]


def test_aws_env_exports_leaves_contested_vars_unset():
    """Two IAM users both claim AWS_ACCESS_KEY_ID. Choosing one risks
    pairing one user's key id with another's secret, so neither wins."""
    lines = _aws_env_exports({
        "cg_chris_access_key_id": "AKIA1",
        "cg_chris_secret_key": "s1",
        "cg_bob_access_key_id": "AKIA2",
        "cg_bob_secret_key": "s2",
        "cg_region": "us-east-1",
    })
    assert not any(line.startswith("export AWS_ACCESS_KEY_ID=") for line in lines)
    assert "export HSM_CG_CHRIS_ACCESS_KEY_ID=AKIA1" in lines
    assert "export AWS_DEFAULT_REGION=us-east-1" in lines
    contested = _aws_env_map({
        "cg_chris_access_key_id": "AKIA1", "cg_bob_access_key_id": "AKIA2"})[1]
    assert contested == {"AWS_ACCESS_KEY_ID": ["cg_chris_access_key_id",
                                               "cg_bob_access_key_id"]}


# ── the credentials panel ─────────────────────────────────────────────────

def _rendered(outputs, width):
    from rich.console import Console
    buf = Console(width=width, no_color=True, highlight=False, record=True)
    buf.print(_creds_body(outputs))
    return buf.export_text()


def test_creds_body_keeps_every_secret_whole():
    """A truncated secret is worse than an ugly one: it looks like a key
    and fails like a typo."""
    outputs = {"cloudgoat_output_chris_secret_key": "X" * 40,
               "cloudgoat_output_chris_access_key_id": "AKIAWXB5ELPOR2QGLBHI"}
    for width in (60, 76, 100, 200):
        text = _rendered(outputs, width)
        assert "…" not in text
        assert "X" * 40 in text
        assert "cloudgoat output chris access key id" in text


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


# ── markdown sections (what `lab info` keeps and drops) ───────────────────

LAB_MD = """### Author
- [Someone](https://example.com)

### Community Walkthroughs
- [Widget](https://example.com/a) - a
- [Widget](https://example.com/b) - b

# Objective / Scope
Pop the box.

#### Starting Credentials
```
user:pass
```"""


def test_drop_writeups_keeps_the_brief():
    kept = _drop_writeups(LAB_MD)
    assert "Objective / Scope" in kept
    assert "Starting Credentials" in kept
    assert "user:pass" in kept
    assert "Author" in kept


def test_drop_writeups_removes_the_walkthrough_links():
    kept = _drop_writeups(LAB_MD)
    assert "Community Walkthroughs" not in kept
    assert "example.com/a" not in kept


def test_only_writeups_is_the_complement():
    only = _only_writeups(LAB_MD)
    assert "Community Walkthroughs" in only
    assert "example.com/b" in only
    assert "Objective" not in only


def test_only_writeups_empty_when_lab_has_none():
    assert _only_writeups("# Objective\nPop it.") == ""


def test_md_sections_ignores_hashes_inside_code_fences():
    """A `#` opening a shell comment is not a heading — splitting on it
    would tear a fenced block in half and mangle the render."""
    md = "# Objective\nrun this:\n```\n# comment\nid\n```\n"
    heads = [h for h, _ in _md_sections(md)]
    assert heads == ["Objective"]
    assert _drop_writeups(md).count("```") == 2


def test_md_sections_keeps_a_preamble_under_an_empty_heading():
    sections = _md_sections("intro text\n# Objective\nbody")
    assert sections[0][0] == ""
    assert "intro text" in sections[0][1]


# ── objective / scope extraction ──────────────────────────────────────────

def test_objective_scope_drops_the_preamble_and_keeps_the_tail():
    """Descriptions open with credits/promo and only then get to the point;
    what follows the objective (Initial Access, Starting Credentials) is the
    part you act on."""
    kept = _objective_scope(LAB_MD)
    assert kept.startswith("# Objective / Scope")
    assert "Starting Credentials" in kept
    assert "Author" not in kept


def test_objective_scope_matches_a_bare_objective_heading():
    md = "### Author\n- me\n\n### Objective\nPop it.\n\n### Initial Access\nVPN only."
    kept = _objective_scope(md)
    assert "Pop it." in kept and "Initial Access" in kept
    assert "### Author" not in kept


def test_objective_scope_drops_promo_sections_before_the_objective():
    md = ("### Free Lab\nfree!\n\n### Join the Discord\ncome chat\n\n"
          "# Objective / Scope\nthe brief")
    kept = _objective_scope(md)
    assert kept == "# Objective / Scope\nthe brief"


def test_objective_scope_falls_back_to_the_whole_description():
    """No Objective heading — better a full description than a blank panel."""
    md = "### Author\n- me\n\n### Notes\nsomething"
    assert _objective_scope(md) == md


def test_objective_scope_still_drops_walkthroughs_after_the_objective():
    md = "# Objective\nthe brief\n\n### Community Walkthroughs\n- [x](http://x)"
    assert "Walkthroughs" not in _objective_scope(md)
