"""Shared fixtures: payload shapes copied from real API responses.

The values are trimmed to the fields hsmcli actually reads, with ids and
names replaced by fakes — these are shape fixtures, not account data.
"""

import pytest


# ── /api/student/catalog ──────────────────────────────────────────────────
# Storefront cards. The real course id is nested under item.id; the
# top-level id is a catalog-card id the /courses endpoints reject.

CATALOG_COURSE = {
    "id": "aaaaaaaa-0000-0000-0000-000000000001",
    "title": "Challenge Lab: Widget (Easy)",
    "subtitle": "This is an Easy Linux challenge lab.",
    "description_md": "### Objective\nPop the box.",
    "image_path": "cccccccc-0000-0000-0000-000000000001/thumb",
    "ownership": {"state": "not_owned", "is_paid_purchase": False},
    "item": {
        "type": "on_demand_course",
        "id": "cccccccc-0000-0000-0000-000000000001",
        "content_state": "not_started",
        "completion_id": None,
    },
}

CATALOG_BUNDLE = {
    "id": "aaaaaaaa-0000-0000-0000-000000000002",
    "title": "Labs and Courses",
    "ownership": {"state": "not_owned", "is_paid_purchase": False},
    "item": {"type": "course_bundle",
             "id": "dddddddd-0000-0000-0000-000000000002"},
}

CATALOG_EVENT = {
    "id": "aaaaaaaa-0000-0000-0000-000000000003",
    "title": "DEFCON: Free Access",
    "ownership": {"state": "owned", "is_paid_purchase": False},
    "item": {"type": "event", "id": "eeeeeeee-0000-0000-0000-000000000003",
             "event_state": "pre_start"},
}


# ── /api/student/courses ──────────────────────────────────────────────────
# The complete set. Flat: `name` and a top-level `state` that collapses
# ownership and progress into one field.

def enrolled(name, state, cid):
    return {"id": cid, "name": name, "content_type": "course", "state": state,
            "description": f"{name} blurb"}


ENROLLED = [
    enrolled("Challenge Lab: Widget (Easy)", "owned",
             "cccccccc-0000-0000-0000-000000000001"),
    enrolled("Challenge Lab: Gadget (Hard)", "in_progress",
             "cccccccc-0000-0000-0000-000000000004"),
    enrolled("Hack With Me: Active Directory (Gadget x Doohickey)", "unowned",
             "cccccccc-0000-0000-0000-000000000005"),
    enrolled("Challenge Lab: Doohickey (Medium)", "completed",
             "cccccccc-0000-0000-0000-000000000006"),
    enrolled("Guided Lab: Sprocket (Easy)", "owned",
             "cccccccc-0000-0000-0000-000000000007"),
    enrolled("Range: Flywheel (Insane)", "lapsed",
             "cccccccc-0000-0000-0000-000000000008"),
    enrolled("Challenge Lab: Widget2 (Medium)", "owned",
             "cccccccc-0000-0000-0000-000000000009"),
]


class FakeAPI:
    """Stands in for HackSmarterAPI in the listing/merge tests.

    ``catalog_error`` makes /catalog raise, which is the degraded path
    ``all_lab_items`` is meant to survive; ``courses_error`` makes /courses
    raise, which it deliberately does not swallow.
    """

    def __init__(self, courses=None, catalog=None,
                 catalog_error=False, courses_error=False):
        self._courses = ENROLLED if courses is None else courses
        self._catalog = ([CATALOG_COURSE, CATALOG_BUNDLE, CATALOG_EVENT]
                         if catalog is None else catalog)
        self.catalog_error = catalog_error
        self.courses_error = courses_error
        self.calls = []

    def get_enrolled_courses(self):
        self.calls.append("courses")
        if self.courses_error:
            raise Exception("HTTP 401 on GET /api/student/courses")
        return self._courses

    def get_catalog(self):
        self.calls.append("catalog")
        if self.catalog_error:
            raise Exception("HTTP 500 on GET /api/student/catalog")
        return {"catalog_items": self._catalog}


@pytest.fixture
def api():
    return FakeAPI()


# ── /api/student/courses/{id}/take ────────────────────────────────────────
# static_aws_labs sits BESIDE `course`, not inside it. Systems live in
# static_systems and in the lesson content items; networks only in
# course_networks.

TAKE = {
    "static_aws_labs": [
        {"id": "ffffffff-0000-0000-0000-00000000000a", "name": "WidgetAWS"},
    ],
    "course": {
        "id": "cccccccc-0000-0000-0000-000000000001",
        "customer_id": "9999-cust",
        "course_playthrough": {"id": "bbbb0000-0000-0000-0000-00000000000b"},
        "static_systems": [
            {"id": "55550000-0000-0000-0000-00000000000c", "name": "Widget"},
        ],
        "course_networks": [
            {"id": "77770000-0000-0000-0000-00000000000d", "name": "Subnet"},
        ],
        "chapters": [
            {
                "name": "Chapter 1",
                "lessons": [
                    {
                        "id": "eeee0000-0000-0000-0000-00000000000e",
                        "name": "Briefing",
                        "completed": True,
                        "content": {
                            "id": "ffff0000-0000-0000-0000-00000000000f",
                            "items": [
                                {"type": "text", "markdown": "# Brief\nGo."},
                                {"type": "video",
                                 "url": "https://example.test/v.mp4"},
                                {"type": "aws-lab",
                                 "aws_lab_id":
                                     "ffffffff-0000-0000-0000-00000000000a"},
                                {"type": "system",
                                 "system_id":
                                     "55550000-0000-0000-0000-00000000000c"},
                                {
                                    "type": "question-free-text",
                                    "id": "aaaa1111-0000-0000-0000-000000000010",
                                    "question": "What is the user flag?",
                                    "match_type": "exact",
                                    "points": 10,
                                    "state": "correct",
                                    "hasHint": True,
                                    "hint": "look in /home",
                                    "attempt": {
                                        "submission": "hsm{user}",
                                        "result": {"correct": True},
                                    },
                                },
                                {
                                    "type": "question-free-text",
                                    "id": "aaaa1111-0000-0000-0000-000000000011",
                                    "question": "What is the root flag?",
                                    "points": 20,
                                    "state": "not_attempted",
                                },
                            ],
                        },
                    },
                    # A lesson with no content at all — must not crash or
                    # contribute a phantom entry with items.
                    {"id": "eeee0000-0000-0000-0000-000000000012",
                     "name": "Empty", "content": None},
                ],
            },
        ],
    },
}
