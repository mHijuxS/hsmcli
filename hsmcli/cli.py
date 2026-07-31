#!/usr/bin/env python3
"""hsmcli — HackSmarter CLI.

Commands:
    hsmcli config set-cookie "<paste Cookie header>"
    hsmcli config show
    hsmcli whoami
    hsmcli labs list [--search q] [--enrolled]
    hsmcli lab <id-or-name> info
    hsmcli lab <id-or-name> enroll
    hsmcli lab <id-or-name> systems
    hsmcli lab <id-or-name> launch [<system-id-or-name>] [--no-wait]
    hsmcli lab <id-or-name> stop | reset
    hsmcli lab <id-or-name> vpn [-o file.ovpn]
    hsmcli notifications | events | exams | subscriptions | orgs | bundles
"""

import argparse
import os
import sys
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .api_client import HackSmarterAPI
from .config import Config
from .resolvers import (
    _extract_items,
    _item_id,
    _item_name,
    is_uuid,
    resolve_course_id,
    resolve_system_id,
)
from .utils import (
    Colors,
    format_datetime,
    format_difficulty,
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
    config.set_cookie(cookie)
    print_success("Cookie saved.")
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


def cmd_labs_list(api: HackSmarterAPI, config: Config, args) -> int:
    fmt = _format_choice(args, config)
    if args.enrolled:
        payload = api.get_enrolled_courses()
    else:
        payload = api.get_catalog()
    items = _extract_items(payload)

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
        items = [it for it in items
                 if (_extract_state(it) or "").lower() in wanted]

    if args.category:
        wanted = set(args.category)
        items = [it for it in items
                 if _lab_category(_item_name(it)) in wanted]

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

    if not items:
        print_warning("No labs match your filters. (Try --json to inspect raw response.)")
        return 0
    _render_labs_table(items)
    print()
    print_info(f"{len(items)} lab(s)")
    return 0


def _unwrap_course(data: Any) -> Dict[str, Any]:
    """The /courses/{id} endpoint wraps the payload as {"course": {...}}
    (and there's a legacy {"data": {...}} variant on other endpoints).
    Peel both wrappers off, tolerating whichever shape shows up."""
    body = data.get("data", data) if isinstance(data, dict) else data
    if isinstance(body, dict) and "course" in body and isinstance(body["course"], dict):
        body = body["course"]
    return body if isinstance(body, dict) else {}


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

    console.print()
    console.print(Panel(Text.assemble(header, "\n", meta),
                        border_style="cyan", padding=(0, 2)))

    # Description (prefer plain, fall back to markdown truncated)
    desc = body.get("description") or body.get("description_markdown") or ""
    if desc:
        # Show up to ~600 chars of description in a panel
        snippet = desc.strip()
        if len(snippet) > 600:
            snippet = snippet[:600].rstrip() + "…"
        console.print(Panel(snippet, title="Description",
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

    # Live systems / network status — get_lab_systems auto-detects the
    # lab kind (systems vs networks) and picks the right endpoint / ids.
    try:
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


def _flatten_lab_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize systems-lab and networks-lab payloads to a flat machine list.

    Networks payload shape:
        [{course_network_id, network: {name, state, systems: [{id, name,
         state, ip_address, hostname, ...}]}}]
    Systems payload shape:
        [{id, system: {name, state, ip, ...}}]

    For rendering we want a single flat list of "machines" with a common
    shape. Networks entries expand to their inner ``systems[]``; systems
    entries pass through unchanged.
    """
    out: List[Dict[str, Any]] = []
    for it in items:
        net = it.get("network")
        if isinstance(net, dict) and isinstance(net.get("systems"), list):
            for s in net["systems"]:
                if isinstance(s, dict):
                    # Copy so we can attach the parent-network name for
                    # multi-network labs — harmless when there's just one.
                    out.append({**s, "_network": net.get("name")})
            continue
        out.append(it)
    return out


def _system_status(item: Dict[str, Any]) -> str:
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
            print_error("Lab has multiple targets — specify one:")
            _render_systems_table(_flatten_lab_items(systems))
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
        try:
            payload = api.get_lab_systems(course_id, [system_id])
            items = _extract_items(payload)
            it = next((x for x in items if _item_id(x) == system_id), items[0] if items else None)
            state = _system_status(it) if it else "unknown"
            ip = _system_ip(it) if it else ""
        except Exception as e:
            state = f"poll-error: {e}"; ip = ""

        if state != last_state:
            stamp = time.strftime("%H:%M:%S")
            console.print(f"  [dim]{stamp}[/dim]  ", end="")
            console.print(_badge(state, STATE_STYLE), end="")
            if ip:
                console.print(f"  [cyan]{ip}[/cyan]", end="")
            console.print()
            last_state = state

        if state in ("running", "ready", "active"):
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
    course_id, system_id = _resolve_lab_system(api, args)
    data = api.power_off_system(course_id, system_id)
    if fmt in ("json", "yaml"):
        print_output(data, fmt); return 0
    print_success(f"Powered off system {system_id}")
    return 0


def cmd_lab_reset(api: HackSmarterAPI, config: Config, args) -> int:
    fmt = _format_choice(args, config)
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


# ── misc ──────────────────────────────────────────────────────────────────

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
                   help="print raw JSON of every API response and exit")
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
    _ll = lsub.add_parser("list", help="list labs (default: catalog)")
    _ll.add_argument("-s", "--search", default="",
                     help="substring filter on name/description")
    _ll.add_argument("-e", "--enrolled", action="store_true",
                     help="show only labs the current user is enrolled in")
    _ll.add_argument("-d", "--difficulty", action="append", default=[],
                     choices=["easy", "medium", "hard", "insane"],
                     help="filter by difficulty (repeatable, e.g. -d easy -d medium)")
    _ll.add_argument("-t", "--state", action="append", default=[],
                     choices=["completed", "in_progress", "owned",
                              "not_started", "unowned", "lapsed"],
                     help="filter by state (repeatable)")
    _ll.add_argument("-c", "--category", action="append", default=[],
                     choices=["challenge", "guided", "range", "hackwith",
                              "foundations", "other"],
                     help="filter by lab category (repeatable)")
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
    for name, help_text in [
        ("info", "show lab metadata (with live system status)"),
        ("take", "show take/enroll info"),
        ("enroll", "enroll in the lab"),
        ("systems", "list systems (machines) in the lab with live status"),
        ("status", "quick 'is it on?' summary of the lab"),
    ]:
        _add_format_flags(lsub2.add_parser(name, help=help_text))

    _lch = lsub2.add_parser("launch", help="launch (start) a system in the lab")
    _lch.add_argument("system", nargs="?",
                      help="system UUID or name (optional if lab has only one)")
    _lch.add_argument("--no-wait", dest="wait", action="store_false",
                      default=True,
                      help="don't poll after launch — return as soon as /power ACKs")
    _lch.add_argument("--timeout", type=int, default=420,
                      help="max seconds to wait when polling (default 420 = 7 min)")
    _add_format_flags(_lch)

    _lst = lsub2.add_parser("stop", help="power off a running system in the lab")
    _lst.add_argument("system", nargs="?",
                      help="system UUID or name (optional if lab has only one)")
    _add_format_flags(_lst)

    _lrs = lsub2.add_parser("reset", help="reboot (reset) a running system in the lab")
    _lrs.add_argument("system", nargs="?",
                      help="system UUID or name (optional if lab has only one)")
    _add_format_flags(_lrs)

    _lvpn = lsub2.add_parser("vpn", help="download the OpenVPN config for the lab")
    _lvpn.add_argument("-o", "--output", help="output file (default ./hsm-<id>.ovpn)")
    _lvpn.add_argument("--print", action="store_true", help="also print config to stdout")

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
    config = Config(getattr(args, "config_dir", None))

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
            parser.parse_args([args.command, "--help"])
            return 2
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
            parser.parse_args(["labs", "--help"]); return 2
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
                "vpn": cmd_lab_vpn,
            }
            fn = table.get(args.subcommand)
            if not fn:
                parser.parse_args(["lab", "--help"]); return 2
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
                data = api.heartbeat_for_course(args.identifier)
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
