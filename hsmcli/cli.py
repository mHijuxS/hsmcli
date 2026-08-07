#!/usr/bin/env python3
"""hsmcli — HackSmarter CLI.

Commands:
    hsmcli config set-cookie "<paste Cookie header>"
    hsmcli config show
    hsmcli whoami
    hsmcli labs list [--search q] [--enrolled | --catalog]
    hsmcli lab <id-or-name> info [--full] [--no-briefing]
    hsmcli lab <id-or-name> enroll
    hsmcli lab <id-or-name> systems
    hsmcli lab <id-or-name> launch [<system-id-or-name>] [--no-wait]
    hsmcli lab <id-or-name> stop | reset
    hsmcli lab <id-or-name> creds [--export]   # AWS labs
    hsmcli lab <id-or-name> extend             # AWS labs
    hsmcli lab <id-or-name> vpn [-o file.ovpn]
    hsmcli notifications | events | exams | subscriptions | orgs | bundles
"""

import argparse
import os
import re
import shlex
import sys
from typing import Any, Dict, List, Optional, Tuple

from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .api_client import (
    AUTH_COOKIE_BASE,
    HackSmarterAPI,
    detect_public_ip,
    parse_cookie_header,
)
from .config import Config
from .resolvers import (
    _extract_items,
    _item_id,
    _item_name,
    all_lab_items,
    is_uuid,
    resolve_course_id,
    resolve_from_list,
    resolve_system_id,
)
from .utils import (
    Colors,
    format_datetime,
    format_difficulty,
    format_time_left,
    print_error,
    print_info,
    print_json,
    print_output,
    print_success,
    print_table,
    print_warning,
    print_yaml,
    truncate,
)


console = Console()


DIFFICULTY_STYLE = {
    "easy": "green", "beginner": "green",
    "medium": "yellow", "intermediate": "yellow",
    "hard": "red", "advanced": "red", "expert": "red",
    "insane": "magenta",
}

STATE_STYLE = {
    "completed": "green",
    "in_progress": "cyan",
    "owned": "bright_blue",
    "not_started": "dim",
    "unowned": "dim",
    "lapsed": "yellow",
    "running": "green",
    "provisioning": "yellow",
    "starting": "yellow",
    "stopped": "dim",
    "not_launched": "dim",
    "error": "red",
    "failed": "red",
    # AWS-lab states: "na" = never started / torn down, "ready" = terraform
    # applied and the IAM keys are live.
    "ready": "green",
    "na": "dim",
    "creating": "yellow",
    "updating": "yellow",
    "pending": "yellow",
    "destroying": "yellow",
    "stopping": "yellow",
    "destroyed": "dim",
    "unknown": "red",
}


def _badge(text: str, palette: dict, default: str = "white") -> Text:
    style = palette.get(str(text or "").lower(), default)
    return Text(str(text or "—"), style=style)


# ── helpers ───────────────────────────────────────────────────────────────

def _format_choice(args, config: Config) -> str:
    if getattr(args, "json", False):
        return "json"
    if getattr(args, "yaml", False):
        return "yaml"
    return config.get_output_format()


def _course_label(item: Dict[str, Any]) -> str:
    return _item_name(item) or (_item_id(item) or "?")


# ── config ────────────────────────────────────────────────────────────────

def cmd_config_show(config: Config, args) -> int:
    data = config.get_all()
    if "cookie" in data:
        data = {**data, "cookie": data["cookie"][:40] + "…(truncated)"}
    print_info(f"Config file: {config.get_config_path()}")
    print_json(data)
    return 0


def cmd_config_set_cookie(config: Config, args) -> int:
    cookie = args.cookie
    if cookie == "-":
        cookie = sys.stdin.read()

    # Validate before storing. A bare token pasted without its `name=` used
    # to save fine and then fail every call with "cookie may be expired",
    # which sends you looking in the wrong place.
    parsed = parse_cookie_header(cookie)
    if not parsed:
        print_error(
            "That doesn't look like a Cookie header — expected 'name=value' "
            "pairs separated by ';'. Copy the whole header from devtools → "
            "Network → any request → Request Headers → Cookie."
        )
        return 2
    chunks = sum(1 for k in parsed if k.startswith(AUTH_COOKIE_BASE))
    if not chunks:
        # Don't hard-fail: the cookie name is HackSmarter's to change, and a
        # user who knows better shouldn't be blocked by our expectations.
        print_warning(
            f"No '{AUTH_COOKIE_BASE}.N' cookie in that header — auth will "
            f"likely fail. Saving anyway."
        )
    config.set_cookie(cookie)
    print_success(f"Cookie saved ({len(parsed)} cookie(s), "
                  f"{chunks} auth chunk(s)).")
    return 0


def cmd_config_clear_cookie(config: Config, args) -> int:
    config.clear_cookie()
    print_success("Cookie cleared.")
    return 0


def cmd_config_set_base_url(config: Config, args) -> int:
    config.set_base_url(args.url)
    print_success(f"Base URL set to {args.url}")
    return 0


def cmd_config_set_format(config: Config, args) -> int:
    config.set_output_format(args.format)
    print_success(f"Output format set to {args.format}")
    return 0


def cmd_config_reset(config: Config, args) -> int:
    config.reset()
    print_success("Config reset.")
    return 0


# ── whoami / profile ──────────────────────────────────────────────────────

def cmd_whoami(api: HackSmarterAPI, config: Config, args) -> int:
    fmt = _format_choice(args, config)
    session = api.session_summary()
    try:
        profile = api.get_profile()
    except Exception as e:
        profile = {"error": str(e)}

    payload = {"session": session, "profile": profile}
    if fmt == "json":
        print_json(payload)
        return 0
    if fmt == "yaml":
        print_yaml(payload)
        return 0

    print(f"{Colors.BOLD}Session{Colors.END}")
    if session:
        for k in ("username", "email", "id", "provider", "expires_at"):
            v = session.get(k)
            if v is not None:
                print(f"  {k:<11} {v}")
    else:
        print_warning("  No decoded session — set a cookie via 'hsmcli config set-cookie'")

    print()
    print(f"{Colors.BOLD}Profile{Colors.END}")
    if isinstance(profile, dict) and "error" in profile:
        print_error(f"  {profile['error']}")
        return 1
    data = profile.get("data", profile) if isinstance(profile, dict) else profile
    # HackSmarter wraps the payload as {"profile": {...}}; unwrap once so
    # scalar rendering shows the fields the user actually cares about.
    if isinstance(data, dict) and set(data.keys()) == {"profile"} and isinstance(data["profile"], dict):
        data = data["profile"]
    if isinstance(data, dict) and data:
        # Show scalars inline, then a note for any nested subtrees so nothing
        # renders as blank when everything is nested.
        nested_keys = []
        printed_any = False
        for k, v in data.items():
            if isinstance(v, (dict, list)):
                nested_keys.append(k)
                continue
            print(f"  {k:<20} {truncate(v, 80)}")
            printed_any = True
        if nested_keys:
            print(f"  {Colors.CYAN}nested:{Colors.END}          "
                  f"{', '.join(nested_keys)}  (use --json to see)")
        if not printed_any and not nested_keys:
            print_json(data)
    elif data:
        print_json(data)
    else:
        print_warning("  (empty profile response)")
    return 0


# ── labs ──────────────────────────────────────────────────────────────────

import re as _re


def _extract_difficulty(item: Dict[str, Any]) -> str:
    """Best-effort difficulty extraction across catalog/enrolled shapes.

    HackSmarter doesn't expose a dedicated difficulty field; it's embedded
    in the title as ``(Easy)`` / ``(Medium)`` / ``(Hard)`` / ``(Insane)``.
    """
    for k in ("difficulty", "level"):
        v = item.get(k)
        if v:
            return str(v)
    name = _item_name(item) or ""
    m = _re.search(r"\(([Ee]asy|[Mm]edium|[Hh]ard|[Ii]nsane|[Bb]eginner)\)", name)
    return m.group(1).capitalize() if m else ""


def _extract_state(item: Dict[str, Any]) -> str:
    """Ownership / progress hint. Catalog nests it under item/ownership;
    enrolled items expose ``state`` at the top level."""
    for k in ("state", "content_state", "progress"):
        v = item.get(k)
        if isinstance(v, str) and v:
            return v
    nested = item.get("item")
    if isinstance(nested, dict):
        v = nested.get("content_state")
        if isinstance(v, str) and v:
            return v
    own = item.get("ownership")
    if isinstance(own, dict):
        v = own.get("state")
        if isinstance(v, str) and v:
            return v
    return ""


def _render_labs_table(items: List[Dict[str, Any]], title: str = "Labs"):
    t = Table(title=title, show_header=True,
              header_style="bold", border_style="dim")
    t.add_column("#", justify="right", style="dim")
    t.add_column("Name")
    t.add_column("Difficulty")
    t.add_column("State")
    for i, it in enumerate(items, 1):
        t.add_row(
            str(i),
            truncate(_item_name(it), 60),
            _badge(_extract_difficulty(it) or "—", DIFFICULTY_STYLE),
            _badge(_extract_state(it) or "—", STATE_STYLE),
        )
    console.print(t)


_CATEGORY_MATCHERS = {
    "challenge": ("challenge lab:",),
    "guided": ("guided lab:",),
    "range": ("range:",),
    "hackwith": ("hack with me:",),
    "foundations": ("foundations",),
}


def _lab_category(name: str) -> str:
    n = (name or "").lower()
    for cat, prefixes in _CATEGORY_MATCHERS.items():
        if any(n.startswith(p) or p in n for p in prefixes):
            return cat
    return "other"


# "labs" means challenge labs to anyone using this tool — the guided labs,
# ranges, Hack-With-Me sessions and courses are a different kind of thing.
# Default to those, with -c all to widen.
DEFAULT_CATEGORIES = ("challenge",)


def cmd_labs_list(api: HackSmarterAPI, config: Config, args) -> int:
    fmt = _format_choice(args, config)
    # Raw-response escape hatch for --json/--yaml when nothing extracts;
    # the merged path has no single response to fall back to.
    payload: Any = []
    if args.enrolled:
        payload = api.get_enrolled_courses()
        items = _extract_items(payload)
    elif args.catalog:
        payload = api.get_catalog()
        items = _extract_items(payload)
    else:
        items = all_lab_items(api)

    if args.search:
        q = args.search.lower()
        items = [it for it in items if q in _item_name(it).lower()
                 or q in str(it.get("description", "")).lower()
                 or q in str(it.get("subtitle", "")).lower()]

    if args.difficulty:
        wanted = {d.lower() for d in args.difficulty}
        items = [it for it in items
                 if (_extract_difficulty(it) or "").lower() in wanted]

    if args.state:
        wanted = {s.lower() for s in args.state}
        # The two endpoints spell "have access, haven't started" differently
        # (/courses says "owned", /catalog says "not_started"), so either
        # spelling matches both — otherwise the filter silently drops rows
        # depending on which endpoint a lab came from.
        if wanted & {"owned", "not_started"}:
            wanted |= {"owned", "not_started"}
        items = [it for it in items
                 if (_extract_state(it) or "").lower() in wanted]

    cats = set(args.category) or set(DEFAULT_CATEGORIES)
    if "all" not in cats:
        items = [it for it in items
                 if _lab_category(_item_name(it)) in cats]

    if args.sort:
        _DIFF_RANK = {"easy": 1, "beginner": 1, "medium": 2, "intermediate": 2,
                      "hard": 3, "advanced": 3, "expert": 3, "insane": 4}
        if args.sort == "name":
            items.sort(key=lambda it: _item_name(it).lower())
        elif args.sort == "difficulty":
            items.sort(key=lambda it: (
                _DIFF_RANK.get((_extract_difficulty(it) or "").lower(), 99),
                _item_name(it).lower(),
            ))
        elif args.sort == "state":
            items.sort(key=lambda it: (
                (_extract_state(it) or "~"),
                _item_name(it).lower(),
            ))

    if fmt == "json":
        print_json(items if items else payload)
        return 0
    if fmt == "yaml":
        print_yaml(items if items else payload)
        return 0

    # Say when the default narrowing is in play. A list that quietly shows a
    # subset is exactly what hid the in-progress labs before.
    default_note = (" — challenge labs only; -c all for everything"
                    if not args.category else "")

    if not items:
        print_warning(f"No labs match your filters{default_note}. "
                      f"(Try --json to inspect raw response.)")
        return 0
    _render_labs_table(items)
    print()
    print_info(f"{len(items)} lab(s){default_note}")
    return 0


def _unwrap_course(data: Any) -> Dict[str, Any]:
    """The /courses/{id} endpoint wraps the payload as {"course": {...}}
    (and there's a legacy {"data": {...}} variant on other endpoints).
    Peel both wrappers off, tolerating whichever shape shows up."""
    body = data.get("data", data) if isinstance(data, dict) else data
    if isinstance(body, dict) and "course" in body and isinstance(body["course"], dict):
        body = body["course"]
    return body if isinstance(body, dict) else {}


# How many content-bearing lessons `lab info` renders before it stops and
# asks for --full. Labs are 1–2 lessons; multi-chapter courses would bury
# the metadata under a wall of text.
BRIEFING_LESSON_LIMIT = 3

# HSM collapses the community-walkthrough lists behind raw <details>/
# <summary> HTML, which rich's Markdown renderer prints verbatim. Strip the
# tags and keep the inner text.
_HTML_DETAILS_RE = re.compile(r"</?(?:details|summary)[^>]*>", re.I)


def _clean_markdown(md: str) -> str:
    return _HTML_DETAILS_RE.sub("", md or "").strip()


def _lesson_renderables(items: List[Dict[str, Any]],
                        lab_names: Dict[str, str]) -> List[Any]:
    """Turn a lesson's ``content.items[]`` into printable rich renderables.

    Item types seen in the wild: ``text`` (markdown briefing), ``video``,
    ``aws-lab`` / ``system`` / ``network`` (lab references), and
    ``question-*``. Anything unknown degrades to a dim one-liner rather
    than vanishing.
    """
    out: List[Any] = []
    for it in items:
        itype = str(it.get("type") or "")
        if itype == "text":
            md = _clean_markdown(it.get("markdown") or it.get("content") or "")
            if md:
                # hyperlinks=False prints "text (url)" instead of an OSC-8
                # escape — walkthrough//video URLs are worth copying, and
                # not every terminal renders the escape.
                out.append(Markdown(md, hyperlinks=False))
        elif itype == "video":
            url = it.get("url") or it.get("video_url") or "?"
            out.append(Text(f"▶ video: {url}", style="blue"))
        elif itype.startswith("question"):
            t = Text("? ", style="bold")
            t.append((it.get("question") or "?").strip(), style="white")
            t.append("  ")
            t.append(_badge(it.get("state") or "not_attempted",
                            QUESTION_STATE_STYLE))
            out.append(t)
        elif itype in ("aws-lab", "system", "network", "lab"):
            ref = (it.get("aws_lab_id") or it.get("system_id")
                   or it.get("network_id") or it.get("id") or "?")
            name = lab_names.get(ref)
            label = f"{name} ({ref})" if name else str(ref)
            out.append(Text(f"⚙ {itype}: {label}", style="dim"))
        else:
            detail = it.get("url") or it.get("name") or it.get("id") or ""
            out.append(Text(f"• {itype or 'item'}: {detail}".rstrip(": "),
                            style="dim"))
    return out


def _lab_reference_names(take: Any) -> Dict[str, str]:
    """id → name for the labs/systems a /take payload lists alongside the
    lesson content (``static_aws_labs``, ``static_systems``)."""
    names: Dict[str, str] = {}
    if not isinstance(take, dict):
        return names
    for key in ("static_aws_labs", "static_systems", "static_networks"):
        for entry in (take.get(key) or []):
            if isinstance(entry, dict) and entry.get("id"):
                names[entry["id"]] = entry.get("name") or ""
    return names


def _render_briefing(api: HackSmarterAPI, course_id: str, full: bool) -> Optional[str]:
    """Render the lesson content (markdown briefing, video, questions).

    Lives only in /take — ``GET /courses/{id}`` returns lesson stubs — so
    this needs enrollment; a 403 here is informational, not fatal. Returns
    the error message when /take was unreachable (the caller skips the
    system-status lookup then — it reads the same endpoint), else None.
    """
    try:
        take = api.get_course_take(course_id, use_cache=True)
    except Exception as e:
        return str(e)

    lessons = [l for l in api.extract_lessons(take) if l["items"]]
    if not lessons:
        return None

    lab_names = _lab_reference_names(take)
    shown = lessons if full else lessons[:BRIEFING_LESSON_LIMIT]
    for les in shown:
        body = _lesson_renderables(les["items"], lab_names)
        if not body:
            continue
        title = les.get("lesson") or les.get("chapter") or "Lesson"
        chapter = les.get("chapter")
        if chapter and chapter != title:
            title = f"{chapter} › {title}"
        if les.get("completed"):
            title += " ✓"
        console.print(Panel(Group(*body), title=title,
                            border_style="dim", padding=(0, 2)))

    hidden = len(lessons) - len(shown)
    if hidden > 0:
        print_info(f"{hidden} more lesson(s) with content — pass --full to render them")
    return None


def cmd_lab_info(api: HackSmarterAPI, config: Config, args) -> int:
    fmt = _format_choice(args, config)
    course_id = resolve_course_id(api, args.identifier)
    data = api.get_course(course_id)
    if fmt == "json":
        print_json(data); return 0
    if fmt == "yaml":
        print_yaml(data); return 0

    body = _unwrap_course(data)
    if not body:
        print_json(data); return 0

    name = body.get("name") or body.get("title") or "?"
    cid = body.get("id") or course_id
    difficulty = _extract_difficulty(body) or "—"
    state = body.get("state") or _extract_state(body) or "—"

    # Header line: name + colored difficulty + state
    header = Text()
    header.append(name, style="bold white")
    header.append("  ")
    header.append(_badge(difficulty, DIFFICULTY_STYLE))
    header.append("  ")
    header.append(_badge(state, STATE_STYLE))

    # Metadata line under the header
    meta = Text()
    meta.append(f"{cid}", style="dim")
    ct = body.get("content_type")
    if ct: meta.append(f"   type: {ct}", style="dim")
    runtime = body.get("included_runtime_gb_seconds")
    if runtime:
        # Convert GB-seconds → hours per GB assumption
        hrs = int(runtime) / 3600
        meta.append(f"   runtime: {int(hrs):,}h·GB", style="dim")

    lines = [header, "\n", meta]
    image_path = body.get("image_path")
    if image_path:
        lines += ["\n", Text(f"image: {api.image_url(image_path)}", style="dim")]

    console.print()
    console.print(Panel(Text.assemble(*lines),
                        border_style="cyan", padding=(0, 2)))

    # Description — description_markdown carries the real briefing
    # (objective/scope, author, initial access); the plain `description`
    # field is just a one-line blurb. Prefer the markdown one when present.
    desc_md = body.get("description_markdown") or ""
    desc_plain = body.get("description") or ""
    if desc_md:
        console.print(Panel(Markdown(desc_md.strip()), title="Description",
                            border_style="dim", padding=(0, 2)))
    elif desc_plain:
        console.print(Panel(desc_plain.strip(), title="Description",
                            border_style="dim", padding=(0, 2)))

    # Chapters / lessons — most useful bit for a lab writeup
    chapters = body.get("chapters") or []
    if chapters:
        t = Table(title="Chapters", show_header=True,
                  header_style="bold", border_style="dim")
        t.add_column("#", justify="right", style="dim")
        t.add_column("Chapter")
        t.add_column("Lessons")
        t.add_column("Done", justify="right")
        for i, ch in enumerate(chapters, 1):
            lessons = ch.get("lessons") or []
            done = sum(1 for l in lessons if l.get("completed"))
            t.add_row(str(i), ch.get("name") or "?",
                      str(len(lessons)),
                      f"{done}/{len(lessons)}")
        console.print(t)

    # Lesson content — the actual briefing (walkthrough links, hints,
    # starting credentials, questions). Only /take carries it.
    take_error: Optional[str] = None
    if not getattr(args, "no_briefing", False):
        take_error = _render_briefing(api, course_id,
                                      full=getattr(args, "full", False))

    # Live systems / network status — get_lab_systems auto-detects the
    # lab kind (systems vs networks) and picks the right endpoint / ids.
    # It reads /take too, so a failure above means this can only repeat it.
    if take_error:
        console.print(f"[dim]lesson content + system status unavailable: "
                      f"{take_error}[/dim]")
    else:
        try:
            if api.lab_kind(course_id) == "aws":
                aws_labs = api.get_aws_labs(course_id)
                if aws_labs:
                    _render_aws_table(aws_labs, title="AWS labs (live status)")
                    for lab in aws_labs:
                        if _aws_state(lab) in AWS_READY_STATES:
                            _render_aws_creds(lab)
            else:
                sys_payload = api.get_lab_systems(course_id)
                sys_items = _flatten_lab_items(_extract_items(sys_payload))
                if sys_items:
                    _render_systems_table(sys_items, title="Systems (live status)")
        except Exception as e:
            console.print(f"[dim]systems status unavailable: {e}[/dim]")

    # Pricing hint (compact)
    prices = body.get("bundle_pricing") or []
    if prices:
        console.print(Panel(
            "\n".join(f"• {p.get('course_bundle_title','?')} — "
                      f"${(p.get('monthly_price_cents') or 0)/100:.2f}/mo"
                      for p in prices[:5]),
            title="Bundles",
            border_style="dim", padding=(0, 2),
        ))
    return 0


def cmd_lab_take(api: HackSmarterAPI, config: Config, args) -> int:
    fmt = _format_choice(args, config)
    course_id = resolve_course_id(api, args.identifier)
    data = api.get_course_take(course_id)
    print_output(data, fmt)
    return 0


def cmd_lab_enroll(api: HackSmarterAPI, config: Config, args) -> int:
    course_id = resolve_course_id(api, args.identifier)
    data = api.enroll_course(course_id)
    fmt = _format_choice(args, config)
    if fmt in ("json", "yaml"):
        print_output(data, fmt); return 0
    print_success(f"Enrolled in course {course_id}")
    if isinstance(data, dict) and data:
        print_json(data)
    return 0


def _network_machines(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The machines inside a networks-lab wrapper, or ``[]`` for a leaf.

    The live payload puts them flat on the wrapper::

        {"id": …, "name": "Odyssey", "systems": [
            {"systemId": …, "name": "DC-01", "state": "running",
             "ip": "10.1.77.132", "hostname": "DC-01"}, …]}

    A ``network: {systems: […]}`` nesting is also accepted — that's the
    shape this code was originally written against, and cheap to keep.
    """
    for holder in (item.get("network"), item):
        if isinstance(holder, dict) and isinstance(holder.get("systems"), list):
            kids = [s for s in holder["systems"] if isinstance(s, dict)]
            if kids:
                return kids
    return []


def _flatten_lab_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize systems-lab and networks-lab payloads to a flat machine list.

    Networks payload: one wrapper per network, machines in ``systems[]``
    (see ``_network_machines``).
    Systems payload:  ``[{id, system: {name, state, ip, ...}}]``

    For rendering we want a single flat list of "machines" with a common
    shape. Networks entries expand to their inner machines; systems entries
    pass through unchanged.
    """
    out: List[Dict[str, Any]] = []
    for it in items:
        machines = _network_machines(it)
        if machines:
            net = it.get("network")
            net_name = (net if isinstance(net, dict) else it).get("name")
            for s in machines:
                # Copy so we can attach the parent-network name for
                # multi-network labs — harmless when there's just one.
                out.append({**s, "_network": net_name})
            continue
        out.append(it)
    return out


# Worst-first ordering used to fold a network's machines into one state:
# a network is only "running" once every machine in it is. Unknown states
# sort mid-pack so a state we've never seen can't masquerade as ready.
_STATE_RANK = {
    "error": 0, "failed": 0,
    "not_launched": 1, "stopped": 2, "stopping": 2,
    "pending": 4, "provisioning": 4, "starting": 4,
    "running": 6, "ready": 6, "active": 6,
}
_UNKNOWN_STATE_RANK = 3


def _aggregate_status(states: List[str]) -> str:
    """Fold several machine states into the one a network reports."""
    if not states:
        return "not_launched"
    return min(states, key=lambda s: (_STATE_RANK.get(s, _UNKNOWN_STATE_RANK), s))


def _system_status(item: Dict[str, Any]) -> str:
    # A networks-lab wrapper has no state of its own — only its machines do
    # — so derive it. Without this the wrapper hit the "not_launched"
    # default below and `launch --wait` polled a lab that was already up.
    machines = _network_machines(item)
    if machines:
        return _aggregate_status([_system_status(m) for m in machines])
    # Fields live under three shapes: top-level, ``instance``, or ``system``.
    sources = [item, item.get("instance") or {}, item.get("system") or {}]
    for source in sources:
        for k in ("status", "state", "phase", "power_state", "lifecycle"):
            v = source.get(k)
            if isinstance(v, str) and v:
                return v
    for source in sources:
        if source.get("running"):
            return "running"
    return "not_launched"


def _system_ip(item: Dict[str, Any]) -> str:
    sources = [item, item.get("instance") or {}, item.get("system") or {}]
    for source in sources:
        for k in ("ip", "ip_address", "public_ip", "private_ip", "address"):
            v = source.get(k)
            if isinstance(v, str) and v:
                return v
    return ""


def _render_systems_table(items: List[Dict[str, Any]], title: str = "Systems"):
    t = Table(title=title, show_header=True,
              header_style="bold", border_style="dim")
    t.add_column("#", justify="right", style="dim")
    t.add_column("Name")
    t.add_column("ID", style="dim")
    t.add_column("Status")
    t.add_column("IP")
    t.add_column("Expires")
    for i, it in enumerate(items, 1):
        instance = it.get("instance") or {}
        status = _system_status(it)
        expires = str(it.get("expires_at") or instance.get("expires_at") or "")
        t.add_row(
            str(i),
            truncate(_item_name(it), 40),
            _item_id(it) or "",
            _badge(status, STATE_STYLE),
            _system_ip(it) or "—",
            format_datetime(expires) if expires else "—",
        )
    console.print(t)


def cmd_lab_systems(api: HackSmarterAPI, config: Config, args) -> int:
    fmt = _format_choice(args, config)
    course_id = resolve_course_id(api, args.identifier)
    if api.lab_kind(course_id) == "aws":
        labs = api.get_aws_labs(course_id)
        if fmt in ("json", "yaml"):
            print_output(labs, fmt); return 0
        if not labs:
            print_warning("No AWS labs returned.")
            return 0
        _render_aws_table(labs, title=f"AWS labs — {course_id}")
        return 0
    payload = api.get_lab_systems(course_id)
    # Keep raw for --json (users may want the network wrapper visible);
    # flatten for the table rendering.
    raw_items = _extract_items(payload)
    items = _flatten_lab_items(raw_items)
    if fmt == "json":
        print_json(raw_items if raw_items else payload); return 0
    if fmt == "yaml":
        print_yaml(raw_items if raw_items else payload); return 0
    if not items:
        print_warning("No systems returned. Try --json to inspect raw response.")
        return 0
    _render_systems_table(items, title=f"Systems — {course_id}")
    return 0


def cmd_lab_status(api: HackSmarterAPI, config: Config, args) -> int:
    """Compact 'is my lab on?' check for one lab."""
    fmt = _format_choice(args, config)
    course_id = resolve_course_id(api, args.identifier)
    if api.lab_kind(course_id) == "aws":
        labs = api.get_aws_labs(course_id)
        if fmt in ("json", "yaml"):
            print_output(labs, fmt); return 0
        if not labs:
            print_warning("No AWS labs in this course.")
            return 0
        ready = [l for l in labs if _aws_state(l) in AWS_READY_STATES]
        header = Text()
        header.append(f"{len(ready)}/{len(labs)} AWS lab(s) ready",
                      style="green" if ready else "dim")
        console.print(Panel(header, border_style="green" if ready else "dim",
                            padding=(0, 2)))
        _render_aws_table(labs, title="Live status")
        for lab in ready:
            _render_aws_creds(lab)
        return 0
    payload = api.get_lab_systems(course_id)
    raw_items = _extract_items(payload)
    items = _flatten_lab_items(raw_items)
    if fmt in ("json", "yaml"):
        print_output(raw_items if raw_items else payload, fmt); return 0
    if not items:
        print_warning("No systems in this lab.")
        return 0

    running = [it for it in items if _system_status(it) in
               ("running", "provisioning", "starting")]
    header = Text()
    header.append(f"{len(running)}/{len(items)} systems running",
                  style="green" if running else "dim")
    console.print(Panel(header, border_style="green" if running else "dim",
                        padding=(0, 2)))
    _render_systems_table(items, title="Live status")
    return 0


def cmd_lab_launch(api: HackSmarterAPI, config: Config, args) -> int:
    import time
    fmt = _format_choice(args, config)
    course_id = resolve_course_id(api, args.identifier)

    # AWS labs have no VM to power on — different endpoint, different
    # payload, credentials instead of an IP.
    if api.lab_kind(course_id) == "aws":
        return _cmd_lab_launch_aws(api, config, args, course_id)

    if args.system:
        system_id = resolve_system_id(api, course_id, args.system)
    else:
        # Auto-select from the OUTER wrapper (system_id for systems-labs,
        # course_network_id for networks-labs) — that's what /power expects.
        # Do NOT flatten first; the inner machine ids won't work.
        systems = _extract_items(api.get_lab_systems(course_id))
        if len(systems) == 1:
            system_id = _item_id(systems[0]) or ""
            print_info(f"Auto-selected: {_course_label(systems[0])}")
        elif not systems:
            print_error("Lab has no systems/networks to launch.")
            return 1
        else:
            print_error("Lab has multiple targets — specify one with --system:")
            # Render the wrappers, not their machines: the IDs printed here
            # have to be ones --system will accept, and an inner machine id
            # is not addressable via /power.
            _render_systems_table(systems, title="Targets")
            return 1

    # Kick a heartbeat before launching so the server treats us as an
    # "active" viewer (the browser does the same on the /take page).
    try:
        api.heartbeat_for_course(course_id)
    except Exception:
        pass  # non-fatal — launch will still be attempted

    data = api.launch_system(course_id, system_id)
    if fmt in ("json", "yaml"):
        print_output(data, fmt); return 0
    print_success(f"Launched system {system_id}")
    if isinstance(data, dict) and data and not args.wait:
        print_json(data)

    if not args.wait:
        print_info(f"Provisioning takes 2–5 min. Poll with "
                   f"`hsmcli lab {args.identifier} status` or drop --no-wait.")
        return 0

    # ── wait loop ────────────────────────────────────────────────────────
    deadline = time.monotonic() + args.timeout
    last_heartbeat = 0.0
    last_state = None
    console.print()
    console.print(f"[dim]Waiting for system to come up (timeout {args.timeout}s)…[/dim]")

    while time.monotonic() < deadline:
        machines: List[Dict[str, Any]] = []
        try:
            payload = api.get_lab_systems(course_id, [system_id])
            items = _extract_items(payload)
            it = next((x for x in items if _item_id(x) == system_id), items[0] if items else None)
            # A networks-lab target is a wrapper around several machines;
            # flatten so the progress line and the final table talk about
            # the machines the user actually connects to.
            machines = _flatten_lab_items([it]) if it else []
            state = _system_status(it) if it else "unknown"
            ip = _system_ip(machines[0]) if len(machines) == 1 else ""
        except Exception as e:
            state = f"poll-error: {e}"; ip = ""

        if state != last_state:
            stamp = time.strftime("%H:%M:%S")
            console.print(f"  [dim]{stamp}[/dim]  ", end="")
            console.print(_badge(state, STATE_STYLE), end="")
            if ip:
                console.print(f"  [cyan]{ip}[/cyan]", end="")
            elif len(machines) > 1:
                ready = sum(1 for m in machines
                            if _system_status(m) in ("running", "ready", "active"))
                console.print(f"  [dim]{ready}/{len(machines)} machines up[/dim]", end="")
            console.print()
            last_state = state

        if state in ("running", "ready", "active"):
            if len(machines) > 1:
                console.print(f"[green]✓ all {len(machines)} systems are up.[/green]")
                _render_systems_table(machines, title="Live status")
            else:
                console.print(f"[green]✓ system is up at {ip or '(no ip)'}.[/green]")
            return 0
        if state in ("error", "failed"):
            console.print(f"[red]✗ launch failed: {state}[/red]")
            return 1

        # Keep the session "warm" — the browser heartbeats every ~10s while
        # sitting on the /take page. Without it the server may pause the
        # launch or refuse to progress it.
        if time.monotonic() - last_heartbeat > 10:
            try:
                api.heartbeat_for_course(course_id)
            except Exception:
                pass
            last_heartbeat = time.monotonic()

        time.sleep(3)

    console.print(f"[yellow]⚠ timeout after {args.timeout}s — last state: {last_state}[/yellow]")
    console.print(f"[dim]The machine may still finish provisioning. Poll with "
                  f"`hsmcli lab {args.identifier} status`.[/dim]")
    return 2


def _resolve_lab_system(api: HackSmarterAPI, args) -> tuple:
    """Shared: (course_id, system_or_network_id) for stop/reset/etc.

    Uses the OUTER wrapper id (system_id / course_network_id) — that's
    the target of the /power and /reset endpoints. Flattened per-machine
    ids under a network wrapper are not addressable via /power.
    """
    course_id = resolve_course_id(api, args.identifier)
    if args.system:
        return course_id, resolve_system_id(api, course_id, args.system)
    systems = _extract_items(api.get_lab_systems(course_id))
    if len(systems) == 1:
        return course_id, _item_id(systems[0]) or ""
    raise LookupError("multiple or no targets — specify one explicitly")


def cmd_lab_stop(api: HackSmarterAPI, config: Config, args) -> int:
    fmt = _format_choice(args, config)
    course_id = resolve_course_id(api, args.identifier)
    if api.lab_kind(course_id) == "aws":
        return _cmd_lab_aws_action(api, config, args, course_id, "stop")
    course_id, system_id = _resolve_lab_system(api, args)
    data = api.power_off_system(course_id, system_id)
    if fmt in ("json", "yaml"):
        print_output(data, fmt); return 0
    print_success(f"Powered off system {system_id}")
    return 0


def cmd_lab_reset(api: HackSmarterAPI, config: Config, args) -> int:
    fmt = _format_choice(args, config)
    course_id = resolve_course_id(api, args.identifier)
    if api.lab_kind(course_id) == "aws":
        return _cmd_lab_aws_action(api, config, args, course_id, "reset")
    course_id, system_id = _resolve_lab_system(api, args)
    data = api.reset_system(course_id, system_id)
    if fmt in ("json", "yaml"):
        print_output(data, fmt); return 0
    print_success(f"Reset system {system_id} — a new IP will be assigned.")
    # Follow up with the fresh status so the user sees the new IP without
    # a second command. Reset re-provisions, so the address changes.
    try:
        items = _extract_items(api.get_lab_systems(course_id, [system_id]))
        if items:
            _render_systems_table(_flatten_lab_items(items), title="Post-reset status")
            new_ip = _system_ip(items[0])
            if new_ip:
                console.print(f"[green]→ new IP: {new_ip}[/green]")
    except Exception as e:
        console.print(f"[dim]status poll failed: {e}[/dim]")
    return 0


# ── AWS labs ──────────────────────────────────────────────────────────────
#
# An AWS lab has no VM and no VPN: HackSmarter runs terraform against a
# throwaway AWS account and hands back IAM keys, with the lab's security
# group scoped to one `allowed_ip`. Lifecycle rides the same verbs as the
# VM labs (launch/stop/reset) plus `extend`, so the commands below slot
# into the existing `hsmcli lab <id> <action>` surface.

# States that end a `launch` poll. Deliberately narrow: anything else
# (including a status read we couldn't parse) keeps polling until the
# timeout rather than reporting a failure that didn't happen.
AWS_READY_STATES = ("ready",)
AWS_FAILED_STATES = ("error", "failed")

# terraform output key → the env var the AWS CLI/SDKs actually read.
_AWS_ENV_KEYS = {
    "access_key": "AWS_ACCESS_KEY_ID",
    "access_key_id": "AWS_ACCESS_KEY_ID",
    "secret_key": "AWS_SECRET_ACCESS_KEY",
    "secret_access_key": "AWS_SECRET_ACCESS_KEY",
    "session_token": "AWS_SESSION_TOKEN",
    "region": "AWS_DEFAULT_REGION",
    "default_region": "AWS_DEFAULT_REGION",
}


def _aws_state(lab: Dict[str, Any]) -> str:
    return str(lab.get("state") or "unknown").lower()


def _render_aws_table(labs: List[Dict[str, Any]], title: str = "AWS labs"):
    t = Table(title=title, show_header=True,
              header_style="bold", border_style="dim")
    t.add_column("#", justify="right", style="dim")
    t.add_column("Name")
    t.add_column("ID", style="dim")
    t.add_column("State")
    t.add_column("Expires")
    t.add_column("Limit", justify="right", style="dim")
    for i, lab in enumerate(labs, 1):
        expires = str(lab.get("expires_at") or "")
        left = format_time_left(expires)
        when = format_datetime(expires) if expires else "—"
        if left:
            when = f"{when} ({left})"
        limit = lab.get("time_limit_minutes")
        t.add_row(
            str(i),
            truncate(lab.get("name") or "?", 40),
            str(lab.get("aws_lab_id") or ""),
            _badge(_aws_state(lab), STATE_STYLE),
            when,
            f"{limit}m" if limit else "—",
        )
    console.print(t)


def _aws_env_exports(outputs: Dict[str, Any]) -> List[str]:
    """``export FOO=bar`` lines for a lab's terraform outputs.

    Known keys map onto the standard AWS env vars so `eval` is enough to
    make the aws CLI work; anything else is passed through as
    ``HSM_<KEY>`` rather than dropped.
    """
    lines: List[str] = []
    for k, v in outputs.items():
        if isinstance(v, (dict, list)) or v is None:
            continue
        env = _AWS_ENV_KEYS.get(str(k).lower())
        if not env:
            env = "HSM_" + re.sub(r"[^A-Z0-9]+", "_", str(k).upper()).strip("_")
        lines.append(f"export {env}={shlex.quote(str(v))}")
    return lines


def _render_aws_creds(lab: Dict[str, Any]) -> bool:
    """Print a lab's terraform outputs (the IAM keys). False if none yet."""
    outputs = lab.get("terraform_outputs") or {}
    if not isinstance(outputs, dict) or not outputs:
        return False
    t = Table(show_header=False, border_style="dim", box=None, padding=(0, 2))
    t.add_column("key", style="cyan")
    t.add_column("value")
    for k, v in outputs.items():
        t.add_row(str(k), str(v) if not isinstance(v, (dict, list)) else str(v))
    expires = str(lab.get("expires_at") or "")
    left = format_time_left(expires)
    title = f"Credentials — {lab.get('name') or lab.get('aws_lab_id')}"
    subtitle = None
    if expires:
        subtitle = f"expires {format_datetime(expires)}" + (f" ({left})" if left else "")
    console.print(Panel(t, title=title, subtitle=subtitle,
                        border_style="green", padding=(0, 2)))
    return True


def _resolve_aws_lab(api: HackSmarterAPI, course_id: str,
                     selector: Optional[str]) -> Dict[str, Any]:
    """Pick one AWS lab from a course — by name/UUID, or the only one."""
    labs = api.get_aws_labs(course_id)
    if not labs:
        raise LookupError("this lab has no AWS labs")
    if selector:
        lab_id, item = resolve_from_list(selector, labs)
        if item is not None:
            return item
        # A UUID we didn't see in /take — ask the API about it directly.
        status = api.get_aws_lab(course_id, lab_id)
        return {**status, "aws_lab_id": lab_id}
    if len(labs) == 1:
        return labs[0]
    _render_aws_table(labs)
    raise LookupError("this course has multiple AWS labs — name one explicitly")


def _parse_kv(pairs: Optional[List[str]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for p in (pairs or []):
        k, sep, v = str(p).partition("=")
        if not sep or not k.strip():
            raise LookupError(f"--input expects KEY=VALUE, got '{p}'")
        out[k.strip()] = v
    return out


def _aws_inputs(lab: Dict[str, Any], args) -> Dict[str, str]:
    """Fill the inputs a lab declares in ``student_inputs[].type``.

    Only ``allowed_ip`` exists today — it scopes the lab's security group
    to a single address. The status payload's own ``suggested_ip`` is the
    egress address HackSmarter sees us coming from, which beats asking a
    third party, so we only fall back to an external lookup when it's
    missing. ``--allowed-ip`` wins over both (useful when the traffic will
    come from somewhere else, e.g. a jump box).
    """
    supplied = _parse_kv(getattr(args, "input", None))
    inputs: Dict[str, str] = {}
    for si in (lab.get("student_inputs") or []):
        key = str((si or {}).get("type") or "") if isinstance(si, dict) else ""
        if not key:
            continue
        if key in supplied:
            inputs[key] = supplied.pop(key)
            continue
        if key == "allowed_ip":
            ip = (getattr(args, "allowed_ip", None) or lab.get("suggested_ip")
                  or detect_public_ip())
            if not ip:
                raise LookupError(
                    "couldn't determine your public IP — pass --allowed-ip <ip>"
                )
            inputs[key] = ip
        else:
            raise LookupError(
                f"this lab requires the input '{key}' — pass --input {key}=<value>"
            )
    inputs.update(supplied)  # anything extra the caller insisted on
    return inputs


def _cmd_lab_launch_aws(api: HackSmarterAPI, config: Config, args,
                        course_id: str) -> int:
    import time
    fmt = _format_choice(args, config)
    lab = _resolve_aws_lab(api, course_id, getattr(args, "system", None))
    lab_id = lab["aws_lab_id"]
    label = lab.get("name") or lab_id

    if _aws_state(lab) in AWS_READY_STATES:
        print_info(f"{label} is already running.")
        if fmt in ("json", "yaml"):
            print_output(lab, fmt)
            return 0
        _render_aws_creds(lab)
        print_info("Use `reset` for a fresh environment, `extend` for more time.")
        return 0

    inputs = _aws_inputs(lab, args)
    if inputs.get("allowed_ip"):
        print_info(f"allowed_ip: {inputs['allowed_ip']}"
                   + ("" if getattr(args, "allowed_ip", None)
                      else "  (server-suggested — override with --allowed-ip)"))

    data = api.aws_lab_power(course_id, lab_id, "start", inputs)
    if not args.wait:
        if fmt in ("json", "yaml"):
            print_output(data, fmt)
            return 0
        print_success(f"Start requested for {label}")
        print_info(f"Terraform takes a couple of minutes. Poll with "
                   f"`hsmcli lab {args.identifier} status`.")
        return 0

    print_success(f"Start requested for {label}")
    deadline = time.monotonic() + args.timeout
    last_state = _aws_state(lab)
    console.print()
    console.print(f"[dim]Waiting for terraform to apply (timeout {args.timeout}s)…[/dim]")

    while time.monotonic() < deadline:
        try:
            lab = {**api.get_aws_lab(course_id, lab_id), "aws_lab_id": lab_id}
            state = _aws_state(lab)
        except Exception as e:
            lab = {"aws_lab_id": lab_id, "state": "unknown", "error_message": str(e)}
            state = "poll-error"

        if state != last_state:
            console.print(f"  [dim]{time.strftime('%H:%M:%S')}[/dim]  ", end="")
            console.print(_badge(state, STATE_STYLE))
            last_state = state

        if state in AWS_READY_STATES:
            if fmt in ("json", "yaml"):
                print_output(lab, fmt)
                return 0
            console.print(f"[green]✓ {label} is ready.[/green]")
            if not _render_aws_creds(lab):
                print_warning("Lab is ready but returned no terraform outputs.")
            return 0
        if state in AWS_FAILED_STATES:
            console.print(f"[red]✗ start failed: {state}[/red]")
            if lab.get("error_message"):
                print_error(str(lab["error_message"]))
            return 1

        time.sleep(5)

    print_warning(f"timeout after {args.timeout}s — last state: {last_state}")
    print_info(f"It may still finish. Poll with `hsmcli lab {args.identifier} status`.")
    return 2


def _cmd_lab_aws_action(api: HackSmarterAPI, config: Config, args,
                        course_id: str, action: str) -> int:
    """stop / reset / extend for an AWS lab, then show the fresh status."""
    fmt = _format_choice(args, config)
    lab = _resolve_aws_lab(api, course_id, getattr(args, "system", None))
    lab_id = lab["aws_lab_id"]
    label = lab.get("name") or lab_id

    # `reset` tears the environment down and re-applies, so it needs the
    # same inputs `start` did; stop/extend don't take any.
    inputs = _aws_inputs(lab, args) if action == "reset" else None
    data = api.aws_lab_power(course_id, lab_id, action, inputs)
    if fmt in ("json", "yaml"):
        print_output(data, fmt)
        return 0

    verb = {"stop": "Stopped", "reset": "Reset", "extend": "Extended"}[action]
    print_success(f"{verb} AWS lab {label}")
    try:
        fresh = {**api.get_aws_lab(course_id, lab_id), "aws_lab_id": lab_id}
    except Exception as e:
        console.print(f"[dim]status poll failed: {e}[/dim]")
        return 0
    _render_aws_table([fresh], title="Status")
    if action in ("reset", "extend"):
        _render_aws_creds(fresh)
    if action == "reset":
        print_info(f"Re-provisioning takes a couple of minutes — poll with "
                   f"`hsmcli lab {args.identifier} status`.")
    return 0


def cmd_lab_creds(api: HackSmarterAPI, config: Config, args) -> int:
    """Print an AWS lab's IAM credentials (terraform outputs)."""
    fmt = _format_choice(args, config)
    course_id = resolve_course_id(api, args.identifier)
    if api.lab_kind(course_id) != "aws":
        print_error("This lab isn't an AWS lab — try `systems` / `vpn` instead.")
        return 2
    lab = _resolve_aws_lab(api, course_id, args.system)
    outputs = lab.get("terraform_outputs") or {}

    if args.export:
        # stdout must stay eval-able: warnings go to stderr, nothing else
        # is printed.
        if not outputs:
            print_error(f"No credentials — lab state is '{_aws_state(lab)}'. "
                        f"Launch it first.")
            return 1
        for line in _aws_env_exports(outputs):
            print(line)
        return 0
    if fmt in ("json", "yaml"):
        print_output(outputs or lab, fmt)
        return 0
    if not _render_aws_creds(lab):
        print_warning(f"No credentials yet — lab state is '{_aws_state(lab)}'. "
                      f"Run `hsmcli lab {args.identifier} launch`.")
        return 1
    return 0


def cmd_lab_extend(api: HackSmarterAPI, config: Config, args) -> int:
    course_id = resolve_course_id(api, args.identifier)
    if api.lab_kind(course_id) != "aws":
        print_error("`extend` only applies to AWS labs.")
        return 2
    return _cmd_lab_aws_action(api, config, args, course_id, "extend")


QUESTION_STATE_STYLE = {
    "correct": "green",
    "incorrect": "red",
    "attempted": "yellow",
    "not_attempted": "dim",
    "unanswered": "dim",
}


def _render_flags_table(items: List[Dict[str, Any]], title: str = "Flags / questions"):
    t = Table(title=title, show_header=True,
              header_style="bold", border_style="dim")
    t.add_column("#", justify="right", style="dim")
    t.add_column("Prompt")
    t.add_column("State")
    t.add_column("Pts", justify="right")
    t.add_column("Match", style="dim")
    t.add_column("Hint", justify="center", style="dim")
    for i, q in enumerate(items, 1):
        t.add_row(
            str(i),
            truncate(q.get("prompt") or "?", 60),
            _badge(q.get("state") or "not_attempted", QUESTION_STATE_STYLE),
            str(q.get("points") or "—"),
            str(q.get("match_type") or "—"),
            "✓" if q.get("has_hint") else "",
        )
    console.print(t)


def cmd_lab_flags(api: HackSmarterAPI, config: Config, args) -> int:
    fmt = _format_choice(args, config)
    course_id = resolve_course_id(api, args.identifier)
    take = api.get_course_take(course_id)
    questions = api.extract_questions(take)
    if fmt == "json":
        print_json(questions); return 0
    if fmt == "yaml":
        print_yaml(questions); return 0
    if not questions:
        print_warning("No questions/flags in this lab's /take payload.")
        return 0
    _render_flags_table(questions)
    total_pts = sum(int(q.get("points") or 0) for q in questions)
    got_pts = sum(int(q.get("points") or 0) for q in questions
                  if (q.get("state") or "").lower() == "correct")
    print()
    print_info(f"{got_pts}/{total_pts} points earned across {len(questions)} question(s)")
    return 0


def _match_question(
    questions: List[Dict[str, Any]], selector: str
) -> Dict[str, Any]:
    """Pick a question by keyword / 1-based index / UUID / prompt substring.

    ``user``/``root`` are treated as prompt substrings — the vast majority
    of HSM labs have exactly one question mentioning each. Ambiguity or no
    match raises ``LookupError``.
    """
    if not questions:
        raise LookupError("lab has no questions/flags")

    # 1-based index.
    if selector.isdigit():
        idx = int(selector)
        if 1 <= idx <= len(questions):
            return questions[idx - 1]
        raise LookupError(f"index {idx} out of range (1..{len(questions)})")

    # Exact UUID → question_id.
    if is_uuid(selector):
        for q in questions:
            if q.get("question_id") == selector:
                return q
        raise LookupError(f"no question with id {selector}")

    # Case-insensitive prompt substring. ``user``/``root`` land here.
    needle = selector.lower()
    matches = [q for q in questions
               if needle in (q.get("prompt") or "").lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        prompts = "; ".join(truncate(q.get("prompt") or "?", 40) for q in matches)
        raise LookupError(f"ambiguous '{selector}' — matches: {prompts}")
    raise LookupError(f"no question matching '{selector}'")


def _submission_verdict(data: Any) -> Tuple[Optional[bool], Optional[str]]:
    """Pull ``(correct, server-accepted answer)`` out of a submit reply.

    The live endpoint answers
    ``{"result": {"is_correct": true, "answer_text": "hsm{…}"}}`` — the
    verdict is nested and the key is ``is_correct``, not ``correct``. Older
    payloads used a flat ``{"correct", "matchedAnswer"}``, so both shapes are
    accepted. ``correct`` comes back ``None`` when no verdict key is found
    anywhere: an unrecognised reply must not silently read as "incorrect".
    """
    if not isinstance(data, dict):
        return None, None

    scopes: List[Dict[str, Any]] = [data]
    for key in ("result", "data", "attempt"):
        inner = data.get(key)
        if isinstance(inner, dict):
            scopes.append(inner)
            nested = inner.get("result")
            if isinstance(nested, dict):
                scopes.append(nested)

    correct: Optional[bool] = None
    answer: Optional[str] = None
    for scope in scopes:
        if correct is None:
            for k in ("is_correct", "isCorrect", "correct"):
                v = scope.get(k)
                if isinstance(v, bool):
                    correct = v
                    break
        if answer is None:
            matched = scope.get("matchedAnswer")
            candidates = [scope.get(k) for k in ("answer_text", "answerText")]
            if isinstance(matched, dict):
                candidates.append(matched.get("answer"))
            for v in candidates:
                if isinstance(v, str) and v.strip():
                    answer = v
                    break
    return correct, answer


def cmd_lab_submit(api: HackSmarterAPI, config: Config, args) -> int:
    fmt = _format_choice(args, config)
    course_id = resolve_course_id(api, args.identifier)
    take = api.get_course_take(course_id)
    questions = api.extract_questions(take)
    try:
        q = _match_question(questions, args.selector)
    except LookupError as e:
        print_error(str(e))
        if questions:
            print_info("Available questions:")
            _render_flags_table(questions)
        return 2

    if (q.get("state") or "").lower() == "correct" and not args.force:
        print_warning(
            f"Question already marked correct "
            f"(previous submission: {q.get('last_submission')}). "
            f"Pass --force to resubmit anyway."
        )
        return 0

    data = api.submit_question(
        course_id=course_id,
        lesson_id=q["lesson_id"],
        question_id=q["question_id"],
        submission=args.value,
    )

    if fmt in ("json", "yaml"):
        print_output(data, fmt); return 0

    correct, answer = _submission_verdict(data)
    prompt_short = truncate(q.get("prompt") or "?", 60)
    if correct is None:
        # Never report an unparsed reply as a wrong flag — show it instead.
        print_warning(f"unrecognised server reply — {prompt_short}")
        print_json(data)
        return 1
    if correct:
        print_success(f"correct — {prompt_short}  ({q.get('points') or '?'} pts)")
        # The server echoes its own canonical casing; only worth showing when
        # it differs by more than case/whitespace.
        if answer and answer.strip().lower() != (args.value or "").strip().lower():
            print_info(f"server-accepted answer: {answer}")
    else:
        print_error(f"incorrect — {prompt_short}")
        # Server sometimes echoes hints or attempt counters, flat or nested.
        result = data.get("result") if isinstance(data, dict) else None
        for scope in (data, result):
            if not isinstance(scope, dict):
                continue
            for k in ("hint", "attempts_remaining", "message"):
                v = scope.get(k)
                if v:
                    print_info(f"  {k}: {v}")
    return 0 if correct else 1


def cmd_lab_vpn(api: HackSmarterAPI, config: Config, args) -> int:
    course_id = resolve_course_id(api, args.identifier)
    dest = args.output
    if not dest:
        # Default to ./<course_id>.ovpn in cwd — matches htbcli behavior.
        dest = f"hsm-{course_id}.ovpn"
    text = api.get_vpn_config(course_id, dest_path=dest)
    print_success(f"VPN config written to {dest} ({len(text)} bytes)")
    if args.print:
        print(text)
    return 0


def _guess_image_ext(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".bin"


def cmd_lab_image(api: HackSmarterAPI, config: Config, args) -> int:
    course_id = resolve_course_id(api, args.identifier)
    body = _unwrap_course(api.get_course(course_id))
    image_path = body.get("image_path")
    if not image_path:
        print_warning("This lab has no image_path set.")
        return 0
    if args.url_only:
        print(api.image_url(image_path))
        return 0
    data = api.download_lab_image(image_path)
    dest = args.output or f"hsm-{course_id}{_guess_image_ext(data)}"
    with open(dest, "wb") as f:
        f.write(data)
    print_success(f"Image written to {dest} ({len(data)} bytes)")
    return 0


# ── misc ──────────────────────────────────────────────────────────────────

def _need_subcommand(command: str, actions) -> int:
    """Report a command invoked without a valid action, and exit 2.

    The old path called ``parser.parse_args([command, "--help"])``, which
    raises ``SystemExit(0)`` — so the ``return 2`` beneath it was dead code
    and `hsmcli labs` looked *successful* to a script.
    """
    print_error(f"`hsmcli {command}` needs an action: "
                + " | ".join(sorted(actions)))
    print_info(f"See `hsmcli {command} --help`.")
    return 2


def _simple_get(api_fn, args, config: Config) -> int:
    fmt = _format_choice(args, config)
    data = api_fn()
    print_output(data, fmt)
    return 0


# ── argparse wiring ───────────────────────────────────────────────────────

def _add_format_flags(p):
    g = p.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true", help="output raw JSON")
    g.add_argument("--yaml", action="store_true", help="output YAML")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hsmcli",
        description="HackSmarter CLI — manage labs, systems, and VPN from the terminal.",
    )
    p.add_argument("--debug", action="store_true",
                   help="trace every API request/response to stderr")
    p.add_argument("--config-dir", help="override config directory (default ~/.hsmcli)")
    sp = p.add_subparsers(dest="command")

    # config
    pc = sp.add_parser("config", help="manage configuration")
    csub = pc.add_subparsers(dest="subcommand")
    csub.add_parser("show", help="show current config")
    _sc = csub.add_parser("set-cookie", help="save the browser Cookie header (or '-' for stdin)")
    _sc.add_argument("cookie")
    csub.add_parser("clear-cookie", help="remove stored cookie")
    _sbu = csub.add_parser("set-base-url", help="override API base URL")
    _sbu.add_argument("url")
    _sf = csub.add_parser("set-format", help="default output format")
    _sf.add_argument("format", choices=["table", "json", "yaml"])
    csub.add_parser("reset", help="wipe all config")

    # whoami
    pw = sp.add_parser("whoami", help="show session + profile")
    _add_format_flags(pw)

    # labs
    pls = sp.add_parser("labs", help="lab catalog operations")
    lsub = pls.add_subparsers(dest="subcommand")
    _ll = lsub.add_parser(
        "list", help="list challenge labs (-c all for every category)")
    _ll.add_argument("-s", "--search", default="",
                     help="substring filter on name/description")
    _src = _ll.add_mutually_exclusive_group()
    _src.add_argument("-e", "--enrolled", action="store_true",
                      help="only /courses (the labs on your account)")
    _src.add_argument("--catalog", action="store_true",
                      help="only /catalog (the storefront cards, incl. bundles)")
    _ll.add_argument("-d", "--difficulty", action="append", default=[],
                     choices=["easy", "medium", "hard", "insane"],
                     help="filter by difficulty (repeatable, e.g. -d easy -d medium)")
    _ll.add_argument("-t", "--state", action="append", default=[],
                     choices=["completed", "in_progress", "owned",
                              "not_started", "unowned", "lapsed"],
                     help="filter by state (repeatable)")
    _ll.add_argument("-c", "--category", action="append", default=[],
                     choices=["all", "challenge", "guided", "range",
                              "hackwith", "foundations", "other"],
                     help="filter by lab category (repeatable; "
                          "default: challenge, 'all' to widen)")
    _ll.add_argument("--sort", choices=["name", "difficulty", "state"],
                     default=None, help="sort results")
    _add_format_flags(_ll)

    # lab (single) — usage: hsmcli lab <identifier> <action> [args…]
    # Putting the identifier BEFORE the action reads naturally ("lab
    # implicit launch", "lab implicit reset") and lets shell history/^r
    # target a specific lab across actions.
    pl = sp.add_parser(
        "lab",
        help="operations on a single lab (usage: lab <identifier> <action>)",
    )
    pl.add_argument("identifier", help="course UUID or (unique) name substring")
    lsub2 = pl.add_subparsers(dest="subcommand", required=True,
                              metavar="ACTION")
    _lif = lsub2.add_parser(
        "info", help="show lab metadata, briefing and live system status")
    _lif.add_argument("--full", action="store_true",
                      help=f"render every lesson's content "
                           f"(default: first {BRIEFING_LESSON_LIMIT})")
    _lif.add_argument("--no-briefing", action="store_true",
                      help="skip lesson content (metadata only)")
    _add_format_flags(_lif)

    for name, help_text in [
        ("take", "show take/enroll info"),
        ("enroll", "enroll in the lab"),
        ("systems", "list systems (machines) in the lab with live status"),
        ("status", "quick 'is it on?' summary of the lab"),
    ]:
        _add_format_flags(lsub2.add_parser(name, help=help_text))

    def _add_aws_input_flags(sub):
        """Flags for the values an AWS lab asks for at start/reset time."""
        sub.add_argument("--allowed-ip",
                         help="AWS lab: IP allowed to reach the lab "
                              "(default: the address HackSmarter sees you from)")
        sub.add_argument("--input", action="append", default=[], metavar="KEY=VALUE",
                         help="AWS lab: extra student input (repeatable)")

    _lch = lsub2.add_parser("launch", help="launch (start) a system / AWS lab")
    _lch.add_argument("system", nargs="?",
                      help="system or AWS-lab UUID/name (optional if there's only one)")
    _lch.add_argument("--no-wait", dest="wait", action="store_false",
                      default=True,
                      help="don't poll after launch — return as soon as /power ACKs")
    _lch.add_argument("--timeout", type=int, default=420,
                      help="max seconds to wait when polling (default 420 = 7 min)")
    _add_aws_input_flags(_lch)
    _add_format_flags(_lch)

    _lst = lsub2.add_parser("stop", help="power off a running system / AWS lab")
    _lst.add_argument("system", nargs="?",
                      help="system or AWS-lab UUID/name (optional if there's only one)")
    _add_format_flags(_lst)

    _lrs = lsub2.add_parser(
        "reset", help="reboot a system / re-provision an AWS lab")
    _lrs.add_argument("system", nargs="?",
                      help="system or AWS-lab UUID/name (optional if there's only one)")
    _add_aws_input_flags(_lrs)
    _add_format_flags(_lrs)

    _lcr = lsub2.add_parser("creds", help="AWS lab: show the IAM credentials")
    _lcr.add_argument("system", nargs="?",
                      help="AWS-lab UUID or name (optional if there's only one)")
    _lcr.add_argument("--export", action="store_true",
                      help="print `export AWS_…=` lines for eval")
    _add_format_flags(_lcr)

    _lex = lsub2.add_parser("extend", help="AWS lab: extend the time limit")
    _lex.add_argument("system", nargs="?",
                      help="AWS-lab UUID or name (optional if there's only one)")
    _add_format_flags(_lex)

    _lvpn = lsub2.add_parser("vpn", help="download the OpenVPN config for the lab")
    _lvpn.add_argument("-o", "--output", help="output file (default ./hsm-<id>.ovpn)")
    _lvpn.add_argument("--print", action="store_true", help="also print config to stdout")

    _limg = lsub2.add_parser("image", help="download the lab's thumbnail image")
    _limg.add_argument("-o", "--output", help="output file (default ./hsm-<id>.<ext>)")
    _limg.add_argument("--url-only", action="store_true",
                       help="just print the image URL, don't download")

    _lfl = lsub2.add_parser("flags", help="list flags / questions in the lab")
    _add_format_flags(_lfl)

    _lsb = lsub2.add_parser("submit", help="submit a flag / free-text answer")
    _lsb.add_argument("selector",
                      help="'user' | 'root' | 1-based index | question UUID | prompt substring")
    _lsb.add_argument("value", help="flag / answer to submit")
    _lsb.add_argument("--force", action="store_true",
                      help="resubmit even if the question is already correct")
    _add_format_flags(_lsb)

    # standalone misc commands
    for name, help_text in [
        ("notifications", "list notifications"),
        ("events", "list events"),
        ("subscriptions", "list subscriptions"),
        ("orgs", "list organizations"),
        ("bundles", "list bundles"),
        ("exams", "list owned exams"),
    ]:
        sub = sp.add_parser(name, help=help_text)
        _add_format_flags(sub)

    _hb = sp.add_parser("heartbeat", help="send a POST /api/heartbeat (keeps lab alive)")
    _hb.add_argument("identifier", nargs="?",
                     help="course UUID or name — if given, sends the /take-page style heartbeat")
    _add_format_flags(_hb)

    _cr = sp.add_parser("credits", help="show runtime credits/balance")
    _add_format_flags(_cr)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        config = Config(getattr(args, "config_dir", None))
    except (ValueError, OSError) as e:
        print_error(str(e))
        return 1

    if not args.command:
        parser.print_help()
        return 0

    # Config subcommands don't need an authenticated API client.
    if args.command == "config":
        table = {
            "show": cmd_config_show,
            "set-cookie": cmd_config_set_cookie,
            "clear-cookie": cmd_config_clear_cookie,
            "set-base-url": cmd_config_set_base_url,
            "set-format": cmd_config_set_format,
            "reset": cmd_config_reset,
        }
        fn = table.get(args.subcommand)
        if not fn:
            return _need_subcommand("config", table)
        try:
            return fn(config, args)
        except Exception as e:
            print_error(str(e))
            return 1

    # Everything else needs the API client.
    try:
        api = HackSmarterAPI(config, debug=args.debug)
    except Exception as e:
        print_error(f"Failed to init API client: {e}")
        return 1

    try:
        if args.command == "whoami":
            return cmd_whoami(api, config, args)
        if args.command == "labs":
            if args.subcommand == "list":
                return cmd_labs_list(api, config, args)
            return _need_subcommand("labs", ["list"])
        if args.command == "lab":
            table = {
                "info": cmd_lab_info,
                "take": cmd_lab_take,
                "enroll": cmd_lab_enroll,
                "systems": cmd_lab_systems,
                "status": cmd_lab_status,
                "launch": cmd_lab_launch,
                "stop": cmd_lab_stop,
                "reset": cmd_lab_reset,
                "creds": cmd_lab_creds,
                "extend": cmd_lab_extend,
                "vpn": cmd_lab_vpn,
                "image": cmd_lab_image,
                "flags": cmd_lab_flags,
                "submit": cmd_lab_submit,
            }
            fn = table.get(args.subcommand)
            if not fn:
                return _need_subcommand("lab <identifier>", table)
            return fn(api, config, args)
        if args.command == "notifications":
            return _simple_get(api.get_notifications, args, config)
        if args.command == "events":
            return _simple_get(api.get_events, args, config)
        if args.command == "subscriptions":
            return _simple_get(api.get_subscriptions, args, config)
        if args.command == "orgs":
            return _simple_get(api.get_orgs, args, config)
        if args.command == "bundles":
            return _simple_get(api.get_bundles, args, config)
        if args.command == "exams":
            return _simple_get(api.get_owned_exams, args, config)
        if args.command == "heartbeat":
            fmt = _format_choice(args, config)
            if getattr(args, "identifier", None):
                # Resolve first: every other command does, and passing a name
                # straight through made `heartbeat <name>` 400 with
                # "Invalid uuid" — despite the README documenting a name.
                data = api.heartbeat_for_course(
                    resolve_course_id(api, args.identifier))
            else:
                data = api.heartbeat()
            print_output(data, fmt)
            return 0
        if args.command == "credits":
            fmt = _format_choice(args, config)
            # customer_id lives in /take, NOT in /profile. Use any enrolled
            # course's take payload to discover it.
            customer_id = None
            try:
                enrolled = _extract_items(api.get_enrolled_courses())
                for it in enrolled:
                    cid = _item_id(it)
                    if not cid:
                        continue
                    try:
                        pt = api._ensure_playthrough(cid)
                    except Exception:
                        continue
                    customer_id = (pt.get("customer_id")
                                   or (pt.get("take", {}).get("course", {}) or {}).get("customer_id"))
                    if customer_id:
                        break
            except Exception:
                pass
            if not customer_id:
                print_error("Couldn't determine customer_id from any enrolled course.")
                return 1

            payg = api.get_credits(customer_id)
            payg_body = payg.get("data", payg) if isinstance(payg, dict) else payg

            if fmt in ("json", "yaml"):
                print_output({"customer_id": customer_id, "payg": payg_body}, fmt)
                return 0

            # PAYG top-up (usually empty for subscription users)
            header = Text()
            header.append("Pay-as-you-go top-up credits", style="bold")
            if isinstance(payg_body, dict):
                for k, v in payg_body.items():
                    if isinstance(v, (dict, list)):
                        continue
                    header.append(f"\n  {k:<28} ")
                    header.append(str(v), style="cyan")
            console.print(Panel(header, border_style="cyan", padding=(0, 2)))
            console.print("[dim]This is the top-up balance. Your subscription's monthly "
                          "runtime allowance is shown per-lab (see `hsmcli lab <name> info` "
                          "→ runtime).[/dim]")
            return 0
    except LookupError as e:
        print_error(str(e))
        return 2
    except Exception as e:
        print_error(str(e))
        return 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
