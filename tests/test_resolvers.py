"""Identifier resolution and the two-endpoint lab merge."""

import pytest

from hsmcli.resolvers import (
    _core_name,
    _extract_items,
    _is_course_item,
    _item_id,
    _item_name,
    _normalize,
    all_lab_items,
    is_uuid,
    resolve_from_list,
)

from conftest import CATALOG_BUNDLE, CATALOG_COURSE, CATALOG_EVENT, ENROLLED, FakeAPI


# ── is_uuid ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("s", [
    "1205dc56-4441-47f0-b7d0-47b2113c43dc",
    "1205DC56-4441-47F0-B7D0-47B2113C43DC",
])
def test_is_uuid_accepts(s):
    assert is_uuid(s)


@pytest.mark.parametrize("s", [
    "", "odyssey", "1205dc56-4441-47f0-b7d0", "not-a-uuid-at-all",
    "1205dc56-4441-47f0-b7d0-47b2113c43dcX",  # trailing junk
    " 1205dc56-4441-47f0-b7d0-47b2113c43dc",  # leading space
])
def test_is_uuid_rejects(s):
    assert not is_uuid(s)


# ── payload flattening ────────────────────────────────────────────────────

def test_extract_items_from_list():
    assert _extract_items([{"a": 1}, "junk", {"b": 2}]) == [{"a": 1}, {"b": 2}]


@pytest.mark.parametrize("key", ["data", "items", "courses", "results", "labs"])
def test_extract_items_from_known_wrapper(key):
    assert _extract_items({key: [{"a": 1}]}) == [{"a": 1}]


def test_extract_items_from_unknown_wrapper():
    """/catalog uses `catalog_items`, which isn't in the known-keys list —
    the fallback flattens any list-valued key."""
    assert _extract_items({"catalog_items": [{"a": 1}]}) == [{"a": 1}]


@pytest.mark.parametrize("payload", [None, "str", 42, {}, {"a": 1}, []])
def test_extract_items_degrades_to_empty(payload):
    assert _extract_items(payload) == []


# ── id / name extraction across wrapper shapes ────────────────────────────

def test_item_id_prefers_nested_catalog_course_id():
    """The top-level catalog id is a card id the /courses endpoints reject."""
    assert _item_id(CATALOG_COURSE) == "cccccccc-0000-0000-0000-000000000001"


def test_item_id_uses_top_level_for_enrolled():
    assert _item_id(ENROLLED[0]) == "cccccccc-0000-0000-0000-000000000001"


@pytest.mark.parametrize("key", [
    "course_network_id", "course_system_id", "aws_lab_id",
])
def test_item_id_prefers_actionable_keys_over_id(key):
    """/power takes the wrapper id, not the generic `id`."""
    assert _item_id({key: "target", "id": "generic"}) == "target"


def test_item_id_none_when_absent():
    assert _item_id({"name": "no id here"}) is None


def test_item_name_from_catalog_title():
    assert _item_name(CATALOG_COURSE) == "Challenge Lab: Widget (Easy)"


@pytest.mark.parametrize("wrapper", ["system", "network", "item"])
def test_item_name_from_status_wrapper(wrapper):
    assert _item_name({wrapper: {"name": "inner"}}) == "inner"


def test_item_name_empty_when_absent():
    assert _item_name({"id": "x"}) == ""


# ── normalization ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,want", [
    ("NovaForge", "novaforge"),
    ("nova forge", "novaforge"),
    ("sql-basics", "sqlbasics"),
    ("SysAdmins", "sysadmins"),
    ("404 Bank", "404bank"),
    ("", ""),
])
def test_normalize(raw, want):
    assert _normalize(raw) == want


@pytest.mark.parametrize("name,want", [
    ("Challenge Lab: Odyssey (Hard)", "odyssey"),
    ("Guided Lab: Sprocket (Easy)", "sprocket"),
    ("Range: Willmore Group (Insane)", "willmoregroup"),
    ("Hack With Me: Active Directory (Odyssey x Triathlon)", "activedirectory"),
    ("Sliver C2: Pentesting and Evasion", "pentestingandevasion"),
    ("What Is Hack Smarter?", "whatishacksmarter"),  # no prefix, no suffix
    ("", ""),
])
def test_core_name_strips_affixes(name, want):
    assert _core_name(name) == want


# ── resolve_from_list ─────────────────────────────────────────────────────

def test_resolve_by_uuid_returns_the_item():
    cid, item = resolve_from_list(ENROLLED[1]["id"], ENROLLED)
    assert cid == ENROLLED[1]["id"]
    assert item is ENROLLED[1]


def test_resolve_unknown_uuid_passes_through():
    """A UUID we don't know is still usable — the caller may have it from
    elsewhere, so we hand it back rather than failing."""
    cid, item = resolve_from_list("11111111-2222-3333-4444-555555555555", ENROLLED)
    assert cid == "11111111-2222-3333-4444-555555555555"
    assert item is None


def test_resolve_exact_name():
    cid, item = resolve_from_list("Challenge Lab: Widget (Easy)", ENROLLED)
    assert item["id"] == "cccccccc-0000-0000-0000-000000000001"


def test_resolve_ignores_spaces_and_punctuation():
    cid, item = resolve_from_list("challengelabwidgeteasy", ENROLLED)
    assert item["id"] == "cccccccc-0000-0000-0000-000000000001"


def test_core_name_tiebreak_beats_substring():
    """'gadget' is *contained in* both the challenge lab and the Hack With
    Me session, but it *is* the core name of only the former. Without the
    tie-break this raised ambiguity — and, via the old catalog fallback,
    silently resolved to the wrong lab."""
    cid, item = resolve_from_list("gadget", ENROLLED)
    assert item["name"] == "Challenge Lab: Gadget (Hard)"


def test_core_name_tiebreak_handles_numeric_suffix_siblings():
    """'widget' must not be ambiguous just because 'Widget2' contains it."""
    cid, item = resolve_from_list("widget", ENROLLED)
    assert item["name"] == "Challenge Lab: Widget (Easy)"
    cid2, item2 = resolve_from_list("widget2", ENROLLED)
    assert item2["name"] == "Challenge Lab: Widget2 (Medium)"


def test_resolve_unique_substring():
    cid, item = resolve_from_list("flywheel", ENROLLED)
    assert item["name"] == "Range: Flywheel (Insane)"


def test_resolve_genuine_ambiguity_raises_and_names_candidates():
    items = [
        {"id": "1", "name": "Challenge Lab: Sliver Basics - Windows (Easy)"},
        {"id": "2", "name": "Guided Lab: Sliver Basics - Linux (Easy)"},
    ]
    with pytest.raises(LookupError) as e:
        resolve_from_list("sliver basics", items)
    assert "ambiguous" in str(e.value)
    assert "Windows" in str(e.value) and "Linux" in str(e.value)


def test_resolve_duplicate_exact_names_demands_a_uuid():
    items = [{"id": "1", "name": "Dupe"}, {"id": "2", "name": "Dupe"}]
    with pytest.raises(LookupError, match="use the UUID"):
        resolve_from_list("Dupe", items)


def test_resolve_no_match_raises():
    with pytest.raises(LookupError, match="no lab matching"):
        resolve_from_list("nothing-like-this", ENROLLED)


@pytest.mark.parametrize("bad", ["", "   ", "---", "!!!"])
def test_resolve_rejects_identifiers_with_no_alphanumerics(bad):
    with pytest.raises(LookupError):
        resolve_from_list(bad, ENROLLED)


# ── course vs non-course catalog cards ────────────────────────────────────

def test_is_course_item_accepts_on_demand_course():
    assert _is_course_item(CATALOG_COURSE)


@pytest.mark.parametrize("card", [CATALOG_BUNDLE, CATALOG_EVENT])
def test_is_course_item_rejects_bundles_and_events(card):
    assert not _is_course_item(card)


def test_is_course_item_accepts_enrolled_shape():
    """Enrolled entries have no nested item; they carry content_type."""
    assert _is_course_item(ENROLLED[0])


def test_is_course_item_defaults_to_course_when_untyped():
    assert _is_course_item({"name": "mystery"})


# ── all_lab_items: the merge ──────────────────────────────────────────────

def test_merge_reads_both_endpoints(api):
    all_lab_items(api)
    assert set(api.calls) == {"courses", "catalog"}


def test_merge_drops_bundle_and_event_cards(api):
    names = [_item_name(it) for it in all_lab_items(api)]
    assert "Labs and Courses" not in names
    assert "DEFCON: Free Access" not in names


def test_merge_dedupes_the_same_course_from_both_endpoints(api):
    """catalog.item.id == enrolled.id, so Widget must appear once."""
    items = all_lab_items(api)
    ids = [_item_id(it) for it in items]
    assert len(ids) == len(set(ids))
    assert ids.count("cccccccc-0000-0000-0000-000000000001") == 1


def test_merge_keeps_courses_absent_from_the_catalog(api):
    """The whole point: /catalog is a subset, and its gaps hid in-progress
    labs from `labs list`."""
    names = [_item_name(it) for it in all_lab_items(api)]
    assert "Challenge Lab: Gadget (Hard)" in names


def test_merge_prefers_enrolled_fields_and_backfills_from_catalog(api):
    """Enrolled came first, so its `state` wins; the catalog's nested `item`
    (carrying content_state) is added because enrolled has no such key."""
    widget = next(it for it in all_lab_items(api)
                  if _item_id(it) == "cccccccc-0000-0000-0000-000000000001")
    assert widget["state"] == "owned"
    assert widget["item"]["content_state"] == "not_started"


def test_merge_survives_a_catalog_outage():
    api = FakeAPI(catalog_error=True)
    assert len(all_lab_items(api)) == len(ENROLLED)


def test_merge_propagates_a_courses_outage():
    """An expired cookie must read as an auth failure, not an empty list."""
    api = FakeAPI(courses_error=True)
    with pytest.raises(Exception, match="401"):
        all_lab_items(api)


def test_merge_of_two_empty_endpoints_is_empty():
    assert all_lab_items(FakeAPI(courses=[], catalog=[])) == []


def test_merge_keys_nameless_idless_entries_out():
    """An entry with neither id nor name can't be addressed or displayed."""
    assert all_lab_items(FakeAPI(courses=[{"state": "owned"}], catalog=[])) == []
