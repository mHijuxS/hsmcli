"""The presentation layer: vocabulary, lab names, filenames, next steps."""

import argparse
import io

import pytest

from hsmcli import ui
from hsmcli.cli import (
    _flag_selector,
    _info_next_steps,
    _render_flags_table,
    _render_labs_table,
    _render_systems_table,
    _strip_leading_heading,
    _vpn_filename,
    _OBJECTIVE_HEADING_RE,
)


# ── the API's states, in words ────────────────────────────────────────────

@pytest.mark.parametrize("raw,want", [
    ("in_progress", "in progress"),
    ("not_launched", "off"),
    ("na", "off"),          # AWS-lab "never started"
    ("stopped", "off"),
    ("running", "running"),
    ("unanswered", "unsolved"),
    ("correct", "solved"),
])
def test_human_state_translates_the_api_vocabulary(raw, want):
    assert ui.human_state(raw) == want


def test_unknown_state_keeps_its_own_text():
    """A state HackSmarter adds tomorrow must still render — softened, not
    swallowed."""
    assert ui.human_state("half_baked") == "half baked"


def test_human_state_preserves_case_it_did_not_translate():
    """Difficulties ride the same badge helper; lowercasing them turned
    'Easy' into 'easy' in the lab card."""
    assert ui.human_state("Easy") == "Easy"


def test_human_state_of_nothing_is_a_dash():
    assert ui.human_state(None) == "—"
    assert ui.human_state("") == "—"


def test_badge_styles_from_the_raw_value_not_the_label():
    """The palette is keyed on the API's spelling, so translation must not
    break the colour lookup."""
    assert ui.badge("in_progress", ui.STATE_STYLE).style == "cyan"
    assert str(ui.badge("in_progress", ui.STATE_STYLE)) == "in progress"


# ── lab names ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,want", [
    ("Challenge Lab: Dark (Easy)", ("Challenge Lab", "Dark", "Easy")),
    ("Guided Lab: SQL Basics (Beginner)", ("Guided Lab", "SQL Basics", "Beginner")),
    ("Foundations", ("", "Foundations", "")),
])
def test_split_lab_name(raw, want):
    assert ui.split_lab_name(raw) == want


def test_a_parenthetical_that_is_not_a_difficulty_stays_in_the_name():
    """'(Odyssey x Triathlon)' names the boxes in a Hack-With-Me session —
    dropping it would leave two sessions indistinguishable."""
    cat, name, diff = ui.split_lab_name(
        "Hack With Me: Active Directory (Odyssey x Triathlon)")
    assert cat == "Hack With Me"
    assert name == "Active Directory (Odyssey x Triathlon)"
    assert diff == ""


def test_display_name_of_an_unprefixed_name_is_itself():
    assert ui.lab_display_name("NovaForge") == "NovaForge"


def test_display_name_of_nothing_is_empty():
    assert ui.lab_display_name(None) == ""
    assert ui.lab_display_name("") == ""


@pytest.mark.parametrize("raw,want", [
    ("Challenge Lab: Dark (Easy)", "dark.ovpn"),
    ("Challenge Lab: Nova Forge (Insane)", "nova-forge.ovpn"),
    ("", "hsm-lab.ovpn"),
])
def test_vpn_filename_is_named_after_the_lab(raw, want):
    """The old default was hsm-<uuid>.ovpn — a filename you can't use in a
    command you type by hand."""
    assert _vpn_filename(raw) == want


def test_slug_never_escapes_its_directory():
    assert ui.slugify("../../etc/passwd") == "etc-passwd"


# ── next steps ────────────────────────────────────────────────────────────

def _args(identifier="dark"):
    return argparse.Namespace(identifier=identifier)


def test_steps_render_with_the_typed_identifier(capsys):
    ui.steps(("hsmcli lab dark enroll", "free"))
    out = capsys.readouterr().out
    assert "hsmcli lab dark enroll" in out
    assert "free" in out


def test_steps_drop_none_entries(capsys):
    ui.steps(None, ("hsmcli whoami", ""), None)
    assert capsys.readouterr().out.count("→") == 1


def test_steps_with_nothing_to_say_print_nothing(capsys):
    ui.steps(None, header="Next:")
    assert capsys.readouterr().out == ""


def test_steps_can_go_to_stderr(capsys):
    """Hints that belong to an error must travel with it, or redirecting
    stdout leaves a bare ✗ on the terminal."""
    ui.steps(("hsmcli lab dark enroll", ""), to_stderr=True)
    captured = capsys.readouterr()
    assert "enroll" in captured.err
    assert captured.out == ""


def test_next_step_after_info_is_launch_when_nothing_is_up():
    out = _info_next_steps(_args(), [{"id": "s", "state": "na"}], [])
    assert any("launch" in cmd for cmd, _ in out)


def test_next_step_after_info_is_vpn_when_a_machine_is_up():
    out = _info_next_steps(_args(), [{"id": "s", "state": "running"}], [])
    assert any("vpn" in cmd for cmd, _ in out)
    assert not any("launch" in cmd for cmd, _ in out)


def test_next_steps_mention_submitting_while_flags_are_open():
    out = _info_next_steps(
        _args(), [{"id": "s", "state": "running"}],
        [{"prompt": "What is the user flag?", "state": "unanswered"}])
    assert any("submit" in cmd for cmd, _ in out)


def test_next_steps_stop_mentioning_submit_once_everything_is_solved():
    out = _info_next_steps(
        _args(), [{"id": "s", "state": "running"}],
        [{"prompt": "What is the user flag?", "state": "correct"}])
    assert not any("submit" in cmd for cmd, _ in out)


def test_aws_lab_next_step_is_creds_not_vpn():
    """AWS labs have no VPN and no IP — suggesting one would send the reader
    looking for a file that never arrives."""
    out = _info_next_steps(_args(), [{"aws_lab_id": "a", "state": "ready"}], [])
    assert any("creds" in cmd for cmd, _ in out)
    assert not any("vpn" in cmd for cmd, _ in out)


# ── flag selectors ────────────────────────────────────────────────────────

QUESTIONS = [
    {"prompt": "What is the user flag?", "state": "unanswered"},
    {"prompt": "What is the root flag?", "state": "unanswered"},
]


def test_flag_selector_prefers_the_keyword():
    assert _flag_selector(QUESTIONS[0], QUESTIONS) == "user"
    assert _flag_selector(QUESTIONS[1], QUESTIONS) == "root"


def test_flag_selector_falls_back_to_the_index_when_a_keyword_is_ambiguous():
    qs = [{"prompt": "First user question"}, {"prompt": "Second user question"}]
    assert _flag_selector(qs[1], qs) == "2"


# ── panel titles ──────────────────────────────────────────────────────────

def test_leading_heading_is_dropped_when_the_panel_already_says_it():
    md = "## Objective\n\nPop the box."
    assert _strip_leading_heading(md, _OBJECTIVE_HEADING_RE) == "Pop the box."


def test_other_headings_survive():
    md = "## Initial Access\n\nSSH is open."
    assert _strip_leading_heading(md, _OBJECTIVE_HEADING_RE) == md


# ── durations ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("secs,want", [
    (0, "0s"), (9, "9s"), (95, "1m35s"), (3600, "1h00m"), (5400, "1h30m"),
])
def test_human_duration(secs, want):
    assert ui.human_duration(secs) == want


def test_human_duration_never_goes_negative():
    assert ui.human_duration(-5) == "0s"


# ── tables ────────────────────────────────────────────────────────────────

def _render(fn, *a, **kw):
    """Render through the shared console and return the plain text."""
    old = ui.console.file
    buf = io.StringIO()
    ui.console.file = buf
    try:
        fn(*a, **kw)
    finally:
        ui.console.file = old
    return buf.getvalue()


def test_machine_table_hides_the_id_column_for_a_single_machine():
    """One machine is auto-selected, so its UUID is 36 columns of noise."""
    out = _render(_render_systems_table,
                  [{"id": "d500ab4b", "name": "Dark", "state": "running",
                    "ip": "10.0.23.197"}])
    assert "10.0.23.197" in out
    assert "d500ab4b" not in out


def test_machine_table_shows_ids_when_there_is_a_choice_to_make():
    out = _render(_render_systems_table,
                  [{"id": "aaa", "name": "DC", "state": "running"},
                   {"id": "bbb", "name": "WS", "state": "running"}])
    assert "aaa" in out and "bbb" in out


def test_machine_table_translates_the_state():
    """'na' is what the AWS status endpoint calls a lab that was never
    started. Nobody reads that as 'off'."""
    out = _render(_render_systems_table,
                  [{"id": "x", "name": "Dark", "state": "na"}])
    assert "off" in out


def test_flags_table_drops_the_points_column_when_nothing_is_scored():
    """Every HSM challenge lab reports points as null; a column of dashes
    is worse than no column."""
    out = _render(_render_flags_table,
                  [{"prompt": "What is the user flag?", "state": "unanswered"}])
    assert "Points" not in out


def test_flags_table_keeps_points_when_the_lab_scores_them():
    out = _render(_render_flags_table,
                  [{"prompt": "user flag?", "state": "correct", "points": 10}])
    assert "Points" in out and "10" in out


def test_lab_table_strips_the_prefix_and_difficulty_from_the_name():
    out = _render(_render_labs_table,
                  [{"name": "Challenge Lab: Dark (Easy)", "state": "in_progress"}])
    assert "Dark" in out
    assert "Challenge Lab:" not in out
    assert "in progress" in out


def test_lab_table_shows_the_topic_column_only_when_topics_differ():
    mixed = _render(_render_labs_table, [
        {"name": "Challenge Lab: Dark (Easy)",
         "subtitle": "This is an Easy Linux challenge lab."},
        {"name": "Challenge Lab: Mapper (Medium)",
         "subtitle": "This is a Medium AWS challenge lab."},
    ])
    assert "Topic" in mixed and "Linux" in mixed and "AWS" in mixed

    uniform = _render(_render_labs_table, [
        {"name": "Challenge Lab: Odyssey (Hard)",
         "subtitle": "This is a Hard Active Directory challenge lab."},
        {"name": "Challenge Lab: Sysco (Medium)",
         "subtitle": "This is a Medium Active Directory challenge lab."},
    ])
    assert "Topic" not in uniform


@pytest.mark.parametrize("render,rows", [
    (_render_systems_table, []),
    (_render_labs_table, []),
    (_render_flags_table, []),
])
def test_empty_tables_render_rather_than_raise(render, rows):
    _render(render, rows)
