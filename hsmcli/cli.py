#!/usr/bin/env python3
"""hsmcli — HackSmarter CLI.

Commands:
    hsmcli auth login | import-cookie | status | logout
    hsmcli config show
    hsmcli whoami
    hsmcli labs list [--search q] [--topic ad] [--difficulty hard]
    hsmcli lab <id-or-name> info [--briefing] [--writeups] [--bundles] [--all]
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
import re
import shlex
import sys
from typing import Any, Dict, List, Optional, Tuple

from rich.console import Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .api_client import (
    AUTH_COOKIE_BASE,
    AuthError,
    ForbiddenError,
    HackSmarterAPI,
    HttpError,
    NotEnrolledError,
    TransportError,
    client_version,
    decode_supabase_session,
    detect_public_ip,
    parse_cookie_header,
)
from .browser_auth import (
    BrowserCaptureCancelled,
    BrowserCaptureUnavailable,
    capture_browser_cookie,
    capture_default_firefox_cookie,
    capture_firefox_cookie,
)
from .config import Config
from .resolvers import (
    _extract_items,
    _item_id,
    _item_name,
    all_lab_items,
    catalog_item_id,
    free_purchase_option_id,
    is_uuid,
    resolve_course,
    resolve_course_id,
    resolve_course_item,
    resolve_from_list,
    resolve_system_id,
)
from .ui import (
    DIFFICULTY_STYLE,
    QUESTION_STATE_STYLE,
    STATE_STYLE,
    badge as _badge,
    console,
    disable_color,
    err_console,
    human_duration,
    info_err,
    human_state,
    lab_display_name,
    quote_arg,
    slugify,
    split_lab_name as _split_name,
    steps,
)
from .utils import (
    format_datetime,
    format_time_left,
    print_error,
    print_info,
    print_json,
    print_output,
    print_success,
    print_warning,
    print_yaml,
    truncate,
)


# ── helpers ───────────────────────────────────────────────────────────────

def _format_choice(args, config: Config) -> str:
    if getattr(args, "json", False):
        return "json"
    if getattr(args, "yaml", False):
        return "yaml"
    return config.get_output_format()


def _course_label(item: Dict[str, Any]) -> str:
    return _item_name(item) or (_item_id(item) or "?")


def _resolve_lab(api: HackSmarterAPI, args) -> Tuple[str, str]:
    """``(course_id, display name)`` for ``hsmcli lab <identifier> …``.

    The name is what the command prints back at the user ("Launching
    Dark"), and it falls back through what the user typed → what /take
    calls the lab → the id, so it degrades to something usable rather than
    to an empty string.
    """
    course_id, name = resolve_course(api, args.identifier)
    if not name:
        name = api.course_name(course_id)
    return course_id, lab_display_name(name) or course_id


def _lab_cmd(args, action: str) -> str:
    """A copy-pasteable ``hsmcli lab <what they typed> <action>``.

    Suggestions echo the identifier the user typed rather than the resolved
    UUID — they asked for "dark", and a hint they can't retype without
    scrolling back isn't a hint.
    """
    return f"hsmcli lab {quote_arg(getattr(args, 'identifier', '') or '<lab>')} {action}"


# ── config ────────────────────────────────────────────────────────────────

def _cookie_summary(config: Config) -> Text:
    """Whether the stored cookie is usable, and for how much longer.

    "cookie: eyJhbGciOiJIUzI1NiIs…(truncated)" answered a question nobody
    asks. The one people do ask — "is my session still good?" — is
    answerable from the token itself.
    """
    import os
    from datetime import datetime, timezone

    cookie = config.get_cookie()
    if not cookie:
        return Text("not set", style="yellow")

    t = Text()
    t.append("set", style="green")
    if os.getenv("HSMCLI_COOKIE"):
        t.append("  (from $HSMCLI_COOKIE, overriding the file)", style="dim")

    parsed = parse_cookie_header(cookie)
    chunks = sum(1 for k in parsed if k.startswith(AUTH_COOKIE_BASE))
    if not chunks:
        t.append(f"  — but no {AUTH_COOKIE_BASE} cookie in it", style="yellow")
        return t

    session = decode_supabase_session(parsed) or {}
    user = (session.get("user") or {}).get("email")
    if user:
        t.append(f"  {user}", style="dim")
    expires = session.get("expires_at")
    if expires:
        try:
            left = (datetime.fromtimestamp(int(expires), tz=timezone.utc)
                    - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, OSError, OverflowError):
            return t
        if left <= 0:
            t.append("  · expired", style="bold red")
        else:
            t.append(f"  · {human_duration(left)} left", style="dim")
    return t


def cmd_config_show(config: Config, args) -> int:
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column("field", style="dim")
    # fold, not ellipsis: a config path you can't read is no use, and this
    # panel exists to be copied out of.
    t.add_column("value", overflow="fold")
    t.add_row("session", _cookie_summary(config))
    t.add_row("site", config.get_base_url())
    t.add_row("output", config.get_output_format())
    t.add_row("config", config.get_config_path())
    console.print(Panel(t, title="hsmcli config", title_align="left",
                        border_style="cyan", padding=(0, 1)))
    if not config.get_cookie():
        steps(("hsmcli auth login", "sign in"))
    return 0


def _auth_chunk_order(name: str) -> Tuple[int, int, str]:
    """Sort key that puts ``…auth-token.0`` before ``…auth-token.10``."""
    tail = name.rsplit(".", 1)[-1]
    return (0, int(tail), name) if tail.isdigit() else (1, 0, name)


# Exactly `sb-auth-auth-token.<N>` — startswith would also accept
# unrelated names that merely share the prefix.
_AUTH_CHUNK_RE = re.compile(re.escape(AUTH_COOKIE_BASE) + r"\.\d+$")


def _clean_cookie(cookie: str) -> Tuple[Optional[str], Dict[str, str], str]:
    """``(cookie_to_store, parsed_header, error)`` for a pasted header.

    Keeps only the Supabase session chunks (``sb-auth-auth-token.N``) —
    the full browser header carries analytics and third-party cookies the
    CLI has no business persisting. A header with none of them is rejected
    outright: storing it would only fail later with "cookie may be
    expired", which sends you looking in the wrong place. (Power users
    with a genuinely different cookie name still have $HSMCLI_COOKIE.)
    """
    parsed = parse_cookie_header(cookie)
    if not parsed:
        return None, {}, (
            "That doesn't look like a Cookie header — expected 'name=value' "
            "pairs separated by ';'. Copy the whole header from devtools → "
            "Network → any request → Request Headers → Cookie."
        )
    auth = {k: v for k, v in parsed.items() if _AUTH_CHUNK_RE.fullmatch(k)}
    if not auth:
        return None, parsed, (
            f"No HackSmarter authentication cookies "
            f"('{AUTH_COOKIE_BASE}.N') in that header — nothing was saved. "
            f"Copy the Cookie request header from a signed-in HackSmarter "
            f"request."
        )
    return ("; ".join(f"{k}={parsed[k]}"
                      for k in sorted(auth, key=_auth_chunk_order)),
            parsed, "")


def _announce_signin(parsed: Dict[str, str]) -> None:
    """Name who just signed in. It's the difference between "something was
    written to a file" and "you are logged in", and it catches the classic
    mistake of copying the cookie from the wrong browser profile."""
    session = decode_supabase_session(parsed) or {}
    who = ((session.get("user") or {}).get("email")
           or ((session.get("user") or {}).get("user_metadata") or {})
           .get("preferred_username"))
    print_success(f"Signed in as {who}" if who else "Cookie saved.")
    steps(("hsmcli whoami", "confirm the session"),
          ("hsmcli labs list", "see your labs"))


def _store_cookie(config: Config, cookie: str) -> int:
    """Validate, filter to the auth chunks, save — no network round-trip.

    The piped path (`auth import-cookie -`, the deprecated `config
    set-cookie`); `auth login` verifies against the API before saving.
    """
    cleaned, parsed, err = _clean_cookie(cookie)
    if err:
        print_error(err)
        return 2
    config.set_cookie(cleaned)
    _announce_signin(parsed)
    return 0


def cmd_config_set_cookie(config: Config, args) -> int:
    info_err("`hsmcli config set-cookie` is deprecated — use `hsmcli auth "
             "login` (hidden prompt) or `hsmcli auth import-cookie -`.")
    cookie = args.cookie
    if cookie == "-":
        cookie = sys.stdin.read()
    return _store_cookie(config, cookie)


def cmd_config_clear_cookie(config: Config, args) -> int:
    config.clear_cookie()
    print_success("Cookie cleared.")
    return 0


def cmd_config_set_base_url(config: Config, args) -> int:
    try:
        config.set_base_url(args.url,
                            allow_insecure=getattr(args, "allow_insecure_http",
                                                   False))
    except ValueError as e:
        print_error(str(e))
        return 2
    if getattr(args, "allow_insecure_http", False):
        print_warning("Insecure base URL — your session cookie will travel "
                      "unencrypted.")
    print_success(f"Base URL set to {config.get_base_url()}")
    return 0


def cmd_config_set_format(config: Config, args) -> int:
    config.set_output_format(args.format)
    print_success(f"Output format set to {args.format}")
    return 0


def cmd_config_reset(config: Config, args) -> int:
    config.reset()
    print_success("Config reset.")
    return 0


# ── auth ──────────────────────────────────────────────────────────────────
#
# Signing in is a first-class act, not a configuration detail. The ideal —
# a PKCE/device-code flow where the browser does the login and the CLI never
# sees a password — needs an endpoint HackSmarter doesn't offer an unofficial
# client. Instead, Chromium's loopback DevTools connection captures only the
# resulting Supabase chunks. The hidden prompt remains as an SSH/no-browser
# fallback, and every interactive candidate is verified before it replaces
# anything.


class _CandidateConfig:
    """Just enough Config for HackSmarterAPI to verify a *candidate* cookie.

    Deliberately not the real Config: verification must test the paste —
    not the stored session it would replace, and not $HSMCLI_COOKIE, which
    the real ``get_cookie()`` prefers.
    """

    def __init__(self, base_url: str, cookie: str):
        self._base_url = base_url
        self._cookie = cookie

    def get_base_url(self) -> str:
        return self._base_url

    def get_cookie(self) -> str:
        return self._cookie

def cmd_auth_login(config: Config, args) -> int:
    """Import an existing session or capture one after browser login.

    Chromium exposes the isolated profile's cookie store over a random
    loopback DevTools port; only the site's Supabase chunks are retained.
    ``--no-browser`` retains the hidden manual prompt for SSH use.

    ``--github`` only changes the guidance to name the site's GitHub
    button; it does NOT start an OAuth flow. HackSmarter's GitHub login
    lands in exactly the same Supabase cookie as an email login, so the
    import side is identical. (A real PKCE/device-code flow needs a
    redirect HackSmarter would have to register for us; until then the
    browser does the OAuth dance and we import the session it produced.)
    """
    if not sys.stdin.isatty():
        # getpass on a pipe half-works (reads stdin, warns on stderr) but
        # the explicit command says what it's doing.
        info_err("stdin isn't a terminal — reading the Cookie header from "
                 "it directly (same as `hsmcli auth import-cookie -`).")
        return _store_cookie(config, sys.stdin.read())

    import getpass
    site = config.get_base_url()
    github = getattr(args, "github", False)
    how = ("sign in with the “Sign in with GitHub” button" if github
           else "sign in as usual (email, or the GitHub button)")
    if not getattr(args, "no_browser", False):
        try:
            cookie = capture_firefox_cookie(site)
            if cookie:
                info_err("Found an existing HackSmarter session in Firefox.")
            else:
                info_err("No existing Firefox session; opening your default "
                         "browser if it supports automatic capture.")
                cookie = capture_default_firefox_cookie(site)
                if cookie:
                    info_err("Captured the session from your default Firefox.")
            if not cookie:
                err_console.print(Text.assemble(
                    ("Opening an isolated login window for ", ""),
                    (site, "cmd"),
                    (f" — {how}.\n", ""),
                    ("The window will close automatically after sign-in.",
                     "muted"),
                ))
                cookie = capture_browser_cookie(site)
        except BrowserCaptureUnavailable as e:
            print_warning(f"Automatic browser capture unavailable ({e}).")
            cookie = ""
        except BrowserCaptureCancelled as e:
            print_error(str(e).capitalize() + ".")
            return 1
        except KeyboardInterrupt:
            print_error("Login cancelled.")
            return 130
    else:
        cookie = ""

    if not cookie:
        err_console.print(Text.assemble(
            (f"Log in at {site} — {how}.\n", ""),
            ("Then copy the Cookie header:\n", ""),
            ("  devtools → Network → any request → Request Headers → Cookie\n",
             "muted"),
            ("\nThe pasted value is hidden and won't enter your shell history.",
             "muted"),
        ))
        try:
            cookie = getpass.getpass("Cookie: ")
        except (EOFError, KeyboardInterrupt):
            print_error("No cookie entered.")
            return 1
    if not cookie.strip():
        print_error("No cookie entered.")
        return 1
    import os

    cleaned, parsed, err = _clean_cookie(cookie)
    if err:
        print_error(err)
        return 2

    # Verify the *candidate* against the live API before anything is
    # saved: a mis-copied or expired paste must fail here — without
    # clobbering a stored session that still works, and without the ✓
    # coming first. The shim also sidesteps $HSMCLI_COOKIE, which a
    # Config-backed client would verify instead of the paste.
    try:
        HackSmarterAPI(_CandidateConfig(site, cleaned)).get_profile()
    except AuthError:
        print_error("HackSmarter rejected that session — nothing was "
                    "saved. Sign in again and retry.")
        return 1
    except TransportError as e:
        print_warning(f"Couldn't reach the API to verify ({e}) — saving "
                      f"unverified; `hsmcli whoami` will tell you once "
                      f"you're back online.")
    except Exception as e:
        print_warning(f"Couldn't verify against the API ({e}) — saving "
                      f"unverified.")

    config.set_cookie(cleaned)
    _announce_signin(parsed)
    if os.getenv("HSMCLI_COOKIE"):
        print_warning("$HSMCLI_COOKIE is set and overrides the saved "
                      "session — unset it so this login takes effect.")
    return 0


def cmd_auth_import_cookie(config: Config, args) -> int:
    """Non-interactive counterpart of ``auth login`` — for scripts and
    secret managers: ``secret-tool lookup … | hsmcli auth import-cookie -``.
    """
    cookie = args.cookie
    if cookie is None or cookie == "-":
        cookie = sys.stdin.read()
    return _store_cookie(config, cookie)


def cmd_auth_status(config: Config, args) -> int:
    import os
    from datetime import datetime, timezone

    fmt = _format_choice(args, config)
    cookie = config.get_cookie()
    parsed = parse_cookie_header(cookie) if cookie else {}
    session = decode_supabase_session(parsed) or {}
    user = session.get("user") or {}
    expires = session.get("expires_at")
    expired = False
    if expires:
        try:
            expired = (datetime.fromtimestamp(int(expires), tz=timezone.utc)
                       <= datetime.now(timezone.utc))
        except (ValueError, OSError, OverflowError):
            pass
    source = ("$HSMCLI_COOKIE" if os.getenv("HSMCLI_COOKIE")
              else config.get_config_path())

    # "Signed in" means a session we can actually decode and that hasn't
    # expired — a stored-but-undecodable cookie must not hand a green exit
    # code to a script while every API call 401s.
    ok = bool(cookie) and bool(session) and not expired
    if fmt in ("json", "yaml"):
        print_output({
            "signed_in": ok,
            "stored": bool(cookie),
            "email": user.get("email"),
            "expires_at": expires,
            "source": source if cookie else None,
        }, fmt)
        return 0 if ok else 1

    if not cookie:
        print_error("Not signed in.")
        steps(("hsmcli auth login", "sign in"), to_stderr=True)
        return 1
    if not session:
        print_warning("A cookie is stored, but it isn't a decodable "
                      "HackSmarter session.")
        steps(("hsmcli auth login", "sign in again"), to_stderr=True)
        return 1
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column("field", style="dim")
    t.add_column("value", overflow="fold")
    t.add_row("session", _cookie_summary(config))
    t.add_row("stored in", source)
    console.print(Panel(t, title="hsmcli auth", title_align="left",
                        border_style="cyan", padding=(0, 1)))
    if expired:
        steps(("hsmcli auth login", "the session expired — sign in again"),
              to_stderr=True)
        return 1
    return 0


def cmd_auth_logout(config: Config, args) -> int:
    import os
    had = bool(config._config.get("cookie")) if hasattr(config, "_config") else True
    config.clear_cookie()
    print_success("Signed out — stored session removed."
                  if had else "No stored session to remove.")
    if os.getenv("HSMCLI_COOKIE"):
        print_warning("$HSMCLI_COOKIE is set and still overrides the file — "
                      "unset it to finish signing out.")
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
    if fmt in ("json", "yaml"):
        print_output(payload, fmt)
        # The embedded error is still emitted for the script to read, but a
        # whoami that couldn't authenticate is not exit 0 — a monitor
        # matching on the code alone must see the failure.
        broken = (not session
                  or (isinstance(profile, dict) and "error" in profile))
        return 1 if broken else 0

    if not session:
        print_warning("No decoded session — the stored cookie isn't a "
                      "HackSmarter one.")
        steps(("hsmcli auth login", "log in again"), to_stderr=True)
        return 1

    body = Text()
    for label, key in (("user", "username"), ("email", "email"),
                       ("login", "provider"), ("id", "id")):
        v = session.get(key)
        if v is None:
            continue
        if body:
            body.append("\n")
        body.append(f"{label:<8}", style="dim")
        body.append(str(v), style="bold white" if key == "username" else "")

    expires = session.get("expires_at")
    if expires:
        # expires_at is a unix timestamp on this payload, not ISO8601.
        from datetime import datetime, timezone
        try:
            when = datetime.fromtimestamp(int(expires), tz=timezone.utc)
            left = (when - datetime.now(timezone.utc)).total_seconds()
            body.append("\n")
            body.append(f"{'session':<8}", style="dim")
            if left <= 0:
                body.append("expired — set a fresh cookie", style="bold red")
            else:
                body.append(f"valid for {human_duration(left)}", style="green")
        except (ValueError, OSError, OverflowError):
            pass

    console.print(Panel(body, title="Signed in", title_align="left",
                        border_style="cyan", padding=(0, 2)))

    if isinstance(profile, dict) and "error" in profile:
        print_error(f"Couldn't load your profile: {profile['error']}")
        return 1

    data = profile.get("data", profile) if isinstance(profile, dict) else profile
    # HackSmarter wraps the payload as {"profile": {...}}; unwrap once so
    # scalar rendering shows the fields the user actually cares about.
    if isinstance(data, dict) and set(data.keys()) == {"profile"} and isinstance(data["profile"], dict):
        data = data["profile"]
    if isinstance(data, dict) and data:
        scalars = [(k, v) for k, v in data.items()
                   if not isinstance(v, (dict, list))]
        nested = [k for k, v in data.items() if isinstance(v, (dict, list))]
        if scalars:
            t = Table(show_header=False, box=None, padding=(0, 2))
            t.add_column("field", style="dim")
            t.add_column("value")
            for k, v in scalars:
                t.add_row(str(k), truncate(v, 70))
            console.print(Panel(t, title="Profile", title_align="left",
                                border_style="dim", padding=(0, 1)))
        if nested:
            print_info(f"{len(nested)} nested "
                       f"{'field' if len(nested) == 1 else 'fields'} not shown: "
                       f"{', '.join(nested)} — pass --json to see them")
        if not scalars and not nested:
            print_json(data)
    elif data:
        print_json(data)
    else:
        print_warning("Your profile came back empty.")
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


def _item_subtitle(item: Dict[str, Any]) -> str:
    """The one-line blurb the site derives lab topics from.

    ``/catalog`` calls it ``subtitle``; ``/courses`` calls it
    ``description`` (its long-form body lives under
    ``description_markdown``). Merged items can carry either, and a
    catalog card nests its copy under ``item``.
    """
    for k in ("subtitle", "description"):
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            return v
    nested = item.get("item")
    if isinstance(nested, dict):
        for k in ("subtitle", "description"):
            v = nested.get(k)
            if isinstance(v, str) and v.strip():
                return v
    return ""


# ── topics (the site's own "AWS / Active Directory / …" filter) ────────────
# There is no topic field anywhere in the API: the catalog page derives the
# chips client-side by keyword-matching the *subtitle* ("This is a Medium
# Active Directory challenge lab."). These helpers reimplement that match
# verbatim so `--topic ad` selects exactly what clicking the chip does.

_TOPIC_WORD_CACHE: Dict[str, Any] = {}


def _has_word(haystack: str, word: str) -> bool:
    """The site's own word test: ``(^|[^a-z0-9])word([^a-z0-9]|$)``.

    Deliberately not ``\b``: it means "Web App" matches ``web`` while
    "Webhooks" doesn't, and it's what keeps our results identical to the
    page's.
    """
    pat = _TOPIC_WORD_CACHE.get(word)
    if pat is None:
        pat = _TOPIC_WORD_CACHE[word] = _re.compile(
            r"(^|[^a-z0-9])" + _re.escape(word) + r"([^a-z0-9]|$)", _re.I)
    return bool(pat.search(haystack))


def _lab_topics(item: Dict[str, Any]) -> List[str]:
    """Every topic the subtitle names, in the site's own order.

    A lab can carry several ("Windows & Linux", "Web and Linux"); one that
    names none is "miscellaneous". ``guided_lab`` is *not* here — the site
    keys that off the title, see ``_matches_topic``.
    """
    sub = (_item_subtitle(item) or "").lower()
    if not sub:
        return []
    topics: List[str] = []
    if _has_word(sub, "aws") or "amazon web services" in sub:
        topics.append("aws")
    if _has_word(sub, "web"):
        topics.append("web")
    if _has_word(sub, "windows"):
        topics.append("windows")
    if _has_word(sub, "linux"):
        topics.append("linux")
    if "active directory" in sub or "activedirectory" in sub:
        topics.append("active_directory")
    if _has_word(sub, "blue team") or _has_word(sub, "blueteam"):
        topics.append("blue_team")
    return topics


def _matches_topic(item: Dict[str, Any], topic: str) -> bool:
    """One item against one topic chip."""
    if topic == "guided_lab":
        # The odd one out: a title check, not a subtitle one, because
        # "This is an Easy Guided Lab." says nothing about the subject.
        return "guided lab" in (_item_name(item) or "").lower()
    topics = _lab_topics(item)
    if topic == "miscellaneous":
        return not topics
    return topic in topics


TOPIC_LABELS = {
    "aws": "AWS",
    "web": "Web",
    "windows": "Windows",
    "linux": "Linux",
    "active_directory": "Active Directory",
    "blue_team": "Blue Team",
    "guided_lab": "Guided Lab",
    "miscellaneous": "Miscellaneous",
}

# The table is already four or five columns wide, and "Active Directory"
# spelled out costs a sixth of an 80-column terminal on every AD row.
_TOPIC_SHORT = dict(TOPIC_LABELS, active_directory="AD", miscellaneous="misc")


def _topic_label(item: Dict[str, Any], short: bool = False) -> str:
    labels = _TOPIC_SHORT if short else TOPIC_LABELS
    topics = _lab_topics(item)
    if not topics:
        return labels["miscellaneous"]
    return "/".join(labels[t] for t in topics)


# What `--topic` accepts. The site's values are the canonical spellings;
# the rest are what people actually type.
_TOPIC_ALIASES = {
    "all": "all",
    "aws": "aws", "cloud": "aws", "amazon": "aws",
    "web": "web", "webapp": "web", "web_app": "web",
    "windows": "windows", "win": "windows",
    "linux": "linux",
    "active_directory": "active_directory", "activedirectory": "active_directory",
    "ad": "active_directory",
    "blue_team": "blue_team", "blueteam": "blue_team", "blue": "blue_team",
    "guided_lab": "guided_lab", "guided": "guided_lab",
    "miscellaneous": "miscellaneous", "misc": "miscellaneous",
}

TOPIC_CHOICES = ("all", "aws", "web", "windows", "linux", "active_directory",
                 "blue_team", "guided_lab", "miscellaneous")


def _topic_arg(value: str) -> str:
    """argparse ``type`` for ``--topic``: normalise, then validate.

    A plain ``choices=`` list would reject ``-T ad`` and ``-T "active
    directory"``, which is what anyone coming from the website's chips
    will reach for first.
    """
    key = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    try:
        return _TOPIC_ALIASES[key]
    except KeyError:
        raise argparse.ArgumentTypeError(
            f"unknown topic {value!r} — pick from: " + ", ".join(TOPIC_CHOICES))


_CATEGORY_LABELS = {
    "challenge": "challenge",
    "guided": "guided",
    "range": "range",
    "hackwith": "hack-with-me",
    "foundations": "foundations",
    "other": "course",
}


def _render_labs_table(items: List[Dict[str, Any]], title: str = "Labs"):
    """One row per lab: bare name, difficulty, where you are with it.

    The name is stripped of its ``Challenge Lab:`` prefix and ``(Easy)``
    suffix — both are rendered in their own right (the suffix as the
    Difficulty column), and repeating them costs a third of the width in a
    list where every prefix is identical. The Type and Topic columns only
    appear when the list actually spans more than one value, so a
    ``--topic ad`` list doesn't waste a column repeating "AD".
    """
    cats = {_lab_category(_item_name(it)) for it in items}
    show_type = len(cats) > 1
    topics = {_topic_label(it, short=True) for it in items}
    show_topic = len(topics) > 1
    t = Table(title=title, title_justify="left", show_header=True,
              header_style="bold", border_style="dim")
    t.add_column("#", justify="right", style="dim")
    t.add_column("Lab")
    if show_type:
        t.add_column("Type", style="dim")
    if show_topic:
        t.add_column("Topic", style="cyan")
    t.add_column("Difficulty")
    t.add_column("Progress")
    for i, it in enumerate(items, 1):
        name = _item_name(it)
        row: List[Any] = [str(i), truncate(lab_display_name(name), 48)]
        if show_type:
            row.append(_CATEGORY_LABELS.get(_lab_category(name), "—"))
        if show_topic:
            row.append(_topic_label(it, short=True))
        row += [
            _badge(_extract_difficulty(it), DIFFICULTY_STYLE),
            _badge(_extract_state(it), STATE_STYLE),
        ]
        t.add_row(*row)
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

    # The website's topic chips (AWS / Active Directory / …). Repeating the
    # flag ORs, matching the page's own behaviour of one chip at a time but
    # letting you ask for two in a single run.
    topics = {t for t in getattr(args, "topic", []) or []}
    if topics and "all" not in topics:
        items = [it for it in items
                 if any(_matches_topic(it, t) for t in topics)]

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
        elif args.sort == "topic":
            # Untopiced labs sort last: "~" beats every label alphabetically.
            items.sort(key=lambda it: (
                _topic_label(it) if _lab_topics(it) else "~",
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
    narrowed = not args.category

    if not items:
        print_warning("No labs match those filters.")
        steps(
            ("hsmcli labs list -c all", "include every category") if narrowed else None,
            ("hsmcli labs list --topic all", "drop the topic filter")
            if topics and "all" not in topics else None,
            ("hsmcli labs list", "clear the filters"),
        )
        return 0

    _render_labs_table(items)
    console.print()
    summary = f"{len(items)} lab{'s' if len(items) != 1 else ''}"
    if topics and "all" not in topics:
        summary += " · " + "/".join(TOPIC_LABELS[t] for t in sorted(topics))
    if narrowed:
        summary += " · challenge labs only"
    print_info(summary)
    steps(
        ("hsmcli lab <name> info", "objective, flags and live status"),
        ("hsmcli labs list -c all", "every category, not just challenge labs")
        if narrowed else None,
        ("hsmcli labs list -T ad -d hard", "filter by topic and difficulty")
        if not topics and not args.difficulty else None,
    )
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


# Headings whose section is a link dump to other people's solutions. `info`
# hides those by default — a dozen walkthrough URLs are spoilers, and they
# push the objective and the live systems off screen.
_WRITEUP_HEADING_RE = re.compile(r"walkthrough|write[\s-]?up|solution", re.I)
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _md_sections(md: str) -> List[Tuple[str, str]]:
    """Split markdown into ``(heading_text, chunk)`` pairs on ATX headings.

    Each chunk keeps its own heading line, so re-joining the chunks
    reproduces the input; anything before the first heading comes back under
    an empty heading. A ``#`` inside a fenced block is a shell comment, not a
    heading — hence the fence tracking.
    """
    sections: List[Tuple[str, str]] = []
    heading = ""
    buf: List[str] = []
    fence: Optional[str] = None
    for line in (md or "").splitlines():
        m = _FENCE_RE.match(line)
        if m:
            fence = None if fence else m.group(1)
        elif fence is None and line.lstrip().startswith("#"):
            if buf:
                sections.append((heading, "\n".join(buf)))
            heading = line.strip().lstrip("#").strip()
            buf = [line]
            continue
        buf.append(line)
    if buf:
        sections.append((heading, "\n".join(buf)))
    return sections


def _drop_writeups(md: str) -> str:
    """``md`` without its walkthrough/solution sections."""
    return "\n".join(chunk for head, chunk in _md_sections(md)
                     if not _WRITEUP_HEADING_RE.search(head)).strip()


def _only_writeups(md: str) -> str:
    """Just the walkthrough/solution sections of ``md`` (``""`` if none)."""
    return "\n".join(chunk for head, chunk in _md_sections(md)
                     if _WRITEUP_HEADING_RE.search(head)).strip()


_OBJECTIVE_HEADING_RE = re.compile(r"objective|scope|goal", re.I)


def _strip_leading_heading(md: str, pattern: re.Pattern) -> str:
    """Drop a leading ``## Objective`` when the panel is already titled that.

    Purely cosmetic: the panel border carries the title, so the same word
    on the first line inside it is a stutter.
    """
    lines = (md or "").lstrip().splitlines()
    if lines and lines[0].lstrip().startswith("#") and pattern.search(lines[0]):
        return "\n".join(lines[1:]).lstrip()
    return md


def _objective_scope(md: str) -> str:
    """The brief: the Objective/Scope section and everything after it.

    Lab descriptions open with boilerplate — author credit, "Free Lab", a
    Discord invite, a call for pentest reports — and only then get to the
    point. What follows the objective is the part you act on (Initial
    Access, Starting Credentials), so this keeps the tail rather than the
    one section. Labs that don't use an Objective heading fall back to the
    whole description.
    """
    sections = _md_sections(md)
    for i, (head, _) in enumerate(sections):
        if _OBJECTIVE_HEADING_RE.search(head):
            return "\n".join(chunk for h, chunk in sections[i:]
                             if not _WRITEUP_HEADING_RE.search(h)).strip()
    return _drop_writeups(md)


def _lesson_renderables(items: List[Dict[str, Any]],
                        lab_names: Dict[str, str],
                        strip_writeups: bool = True) -> List[Any]:
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
            if strip_writeups:
                md = _drop_writeups(md)
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


def _render_briefing(api: HackSmarterAPI, take: Any, full: bool) -> None:
    """Render the lesson content (markdown briefing, video, questions).

    Lives only in /take — ``GET /courses/{id}`` returns lesson stubs — so
    this needs enrollment; the caller fetches the payload and decides what a
    failure means.
    """
    lessons = [l for l in api.extract_lessons(take) if l["items"]]
    if not lessons:
        return

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


def _render_writeups(api: HackSmarterAPI, take: Any, body: Dict[str, Any]) -> None:
    """The community-walkthrough links, pulled back out of the markdown.

    They show up in the lesson text and (sometimes) in the course
    description, so both are scanned and identical chunks collapsed.
    """
    seen: List[str] = []
    sources = [body.get("description_markdown") or ""]
    for les in api.extract_lessons(take):
        for it in les["items"]:
            if str(it.get("type") or "") == "text":
                sources.append(it.get("markdown") or it.get("content") or "")
    for md in sources:
        chunk = _only_writeups(_clean_markdown(md))
        if chunk and chunk not in seen:
            seen.append(chunk)
    if not seen:
        print_info("No community walkthroughs listed for this lab.")
        return
    console.print(Panel(Markdown("\n\n".join(seen), hyperlinks=False),
                        title="Community walkthroughs",
                        border_style="dim", padding=(0, 2)))


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

    # Default view is what you need while working the box: what it is, what
    # you're meant to do, which flags are outstanding, which machines are up.
    # Everything else is opt-in.
    show_all = getattr(args, "all_sections", False)
    want_briefing = show_all or args.briefing or args.full
    want_chapters = show_all or args.chapters
    want_writeups = show_all or args.writeups
    want_bundles = show_all or args.bundles

    raw_name = body.get("name") or body.get("title") or "?"
    category, name, _ = _split_name(raw_name)
    cid = body.get("id") or course_id
    difficulty = _extract_difficulty(body)
    state = body.get("state") or _extract_state(body)

    # The card: what it is, how hard, how far you've got. The name goes in
    # the border, so the difficulty and state read as attributes of it
    # rather than as a third and fourth word in the title — that repetition
    # ("Challenge Lab: Dark (Easy)  Easy  in_progress") was the old header.
    card = Text()
    card.append(_badge(difficulty or "—", DIFFICULTY_STYLE))
    card.append("  ·  ")
    card.append(_badge(state or "—", STATE_STYLE))
    if category:
        card.append(f"  ·  {category.lower()}", style="dim")
    if _lab_topics(body):
        card.append(f"  ·  {_topic_label(body)}", style="cyan")

    runtime = body.get("included_runtime_gb_seconds")
    if runtime:
        # GB-seconds is how the API bills lab uptime: a 1 GB machine
        # running for an hour spends one GB-hour.
        try:
            card.append(f"\n{int(runtime) / 3600:,.0f} GB-hours of runtime included",
                        style="dim")
        except (TypeError, ValueError):
            pass
    card.append(f"\n{cid}", style="dim")

    image_path = body.get("image_path")
    if image_path and show_all:
        card.append(f"\n{api.image_url(image_path)}", style="dim")

    console.print()
    console.print(Panel(card, title=name or raw_name, title_align="left",
                        border_style="cyan", padding=(0, 2)))

    # Objective / scope — description_markdown carries the real brief; the
    # plain `description` field is just a one-line blurb. --all prints the
    # description whole (credits, Discord invite, submission links);
    # --writeups prints the walkthrough links on their own at the end.
    full_desc = _clean_markdown(body.get("description_markdown") or "")
    desc_md = _drop_writeups(full_desc) if show_all else _objective_scope(full_desc)
    desc_plain = body.get("description") or ""
    if desc_md:
        desc_md = _strip_leading_heading(desc_md, _OBJECTIVE_HEADING_RE)
        console.print(Panel(Markdown(desc_md, hyperlinks=False),
                            title="Objective", title_align="left",
                            border_style="dim", padding=(0, 2)))
    elif desc_plain:
        console.print(Panel(desc_plain.strip(), title="Objective",
                            title_align="left",
                            border_style="dim", padding=(0, 2)))

    # Chapters / lessons — off by default: a challenge lab is one chapter
    # with one lesson, so the table says nothing you can act on.
    chapters = body.get("chapters") or []
    if chapters and want_chapters:
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

    # Everything below rides on /take: the flags, the lesson content and the
    # ids the live-status lookup needs. One fetch, one failure message.
    take: Any = None
    take_error: Optional[Exception] = None
    try:
        take = api.get_course_take(course_id, use_cache=True)
    except Exception as e:
        take_error = e

    machines: List[Dict[str, Any]] = []
    questions: List[Dict[str, Any]] = []

    if take_error is not None:
        console.print()
        if isinstance(take_error, (ForbiddenError, NotEnrolledError)):
            # By far the common case: the lab is on the account but has no
            # playthrough yet, so /take won't serve the flags or the
            # machines. That's one command away from fixed.
            print_warning("Flags and live status need you enrolled in this lab.")
            steps((_lab_cmd(args, "enroll"), "takes a second, no cost"))
        else:
            print_warning(f"Flags and live status unavailable: {take_error}")
    else:
        if want_briefing:
            _render_briefing(api, take, full=getattr(args, "full", False))

        questions = api.extract_questions(take)
        if questions:
            _render_flags_table(questions, title="Flags")

        # Live systems / network status — get_lab_systems auto-detects the
        # lab kind (systems vs networks) and picks the right endpoint / ids.
        try:
            if api.lab_kind(course_id) == "aws":
                aws_labs = api.get_aws_labs(course_id)
                if aws_labs:
                    _render_aws_table(aws_labs, title="AWS lab")
                    machines = aws_labs
                    for lab in aws_labs:
                        if _aws_state(lab) in AWS_READY_STATES:
                            _render_aws_creds(lab)
            else:
                sys_payload = api.get_lab_systems(course_id)
                machines = _flatten_lab_items(_extract_items(sys_payload))
                if machines:
                    _render_systems_table(machines, title="Machines")
        except Exception as e:
            print_warning(f"Live machine status unavailable: {e}")

        if want_writeups:
            _render_writeups(api, take, body)

    if want_bundles:
        prices = body.get("bundle_pricing") or []
        if prices:
            console.print(Panel(
                "\n".join(f"• {p.get('course_bundle_title','?')} — "
                          f"${(p.get('monthly_price_cents') or 0)/100:.2f}/mo"
                          for p in prices[:5]),
                title="Bundles",
                border_style="dim", padding=(0, 2),
            ))
        else:
            print_info("No bundle pricing listed for this lab.")

    if take_error is None:
        console.print()
        steps(*_info_next_steps(args, machines, questions))
    return 0


def _info_next_steps(args, machines: List[Dict[str, Any]],
                     questions: List[Dict[str, Any]]) -> List[Any]:
    """The two or three commands that make sense from where this lab is.

    Reading a lab card is never the goal — starting the box, getting on the
    VPN or submitting a flag is. Which of those applies is knowable from
    the state we just rendered, so say it rather than making the reader map
    the state table back onto the command list.
    """
    running = [m for m in machines
               if (_aws_state(m) if "aws_lab_id" in m else _system_status(m))
               in RUNNING_STATES]
    is_aws = any("aws_lab_id" in m for m in machines)
    out: List[Any] = []
    if machines and not running:
        out.append((_lab_cmd(args, "launch"),
                    "start it" + ("" if is_aws else " and wait for the IP")))
    elif running and not is_aws:
        out.append((_lab_cmd(args, "vpn"), "download the VPN profile"))
        out.append((_lab_cmd(args, "stop"), "power it off when you're done"))
    elif running and is_aws:
        out.append((_lab_cmd(args, "creds"), "show the IAM keys again"))
    if any((q.get("state") or "").lower() != "correct" for q in questions):
        out.append((_lab_cmd(args, "submit user '<flag>'"), "submit a flag"))
    return out


def cmd_lab_take(api: HackSmarterAPI, config: Config, args) -> int:
    fmt = _format_choice(args, config)
    course_id = resolve_course_id(api, args.identifier)
    data = api.get_course_take(course_id)
    print_output(data, fmt)
    return 0


def cmd_lab_enroll(api: HackSmarterAPI, config: Config, args) -> int:
    """Claim a lab so the API will serve its content.

    Enrollment is a catalog operation, not a course one — see
    ``HackSmarterAPI.enroll_course``. That means resolving the lab to its
    ``catalog_item_id``, and it means reading a reply that says one of
    three things: the lab is yours now, it was already yours, or
    HackSmarter wants paying for it first.
    """
    fmt = _format_choice(args, config)
    course_id, name, item = resolve_course_item(api, args.identifier)
    label = lab_display_name(name) or api.course_name(course_id) or course_id
    cat_id = catalog_item_id(item)
    if not cat_id:
        # Resolved to a lab, but nothing tells us which storefront card it
        # is — a /courses entry with no catalog_item_id, or a bare UUID we
        # couldn't match against either listing.
        print_error(f"No catalog entry for {label}, so there's nothing to "
                    f"enroll in.")
        steps(("hsmcli labs list", "what your account can actually see"),
              to_stderr=True)
        return 2

    try:
        # Free labs like Mapper reject a null option ("A purchase option must
        # be selected"); pass their free option id when the card lists one.
        # Subscription-covered labs list none and take the null-option path.
        data = api.enroll_course(
            cat_id, purchase_option_id=free_purchase_option_id(item))
    except HttpError as exc:
        # Enrolling twice is a 400 "User already owns course". The state the
        # user asked for is the state they're in, so this isn't a failure —
        # and `hsmcli lab X enroll && hsmcli lab X launch` shouldn't stop
        # dead the second time you run it.
        if exc.status == 400 and "already own" in (exc.body or "").lower():
            if fmt in ("json", "yaml"):
                print_output({"state": "already_enrolled",
                              "course_id": course_id}, fmt)
                return 0
            print_success(f"Already enrolled in {label}")
            steps((_lab_cmd(args, "launch"), "start the machine"),
                  (_lab_cmd(args, "info"), "objective, flags and live status"))
            return 0
        raise
    reply = data if isinstance(data, dict) else {}
    state = reply.get("state")

    if fmt in ("json", "yaml"):
        print_output(data, fmt)
        # Honest exit code: "checkout" means you are *not* enrolled.
        return 2 if state == "checkout" else 0

    if state == "checkout":
        print_warning(f"{label} isn't included in your account — "
                      f"HackSmarter wants paying for it first.")
        if reply.get("session_url"):
            print_info(reply["session_url"])
        info_err("Nothing was charged; the lab is yours once that checkout "
                 "completes.")
        return 2

    # A successful claim is `{"state": "bought", "redirect_url": …}` — the
    # redirect is the browser's take page, which says nothing a person
    # typing hsmcli needs, so the ✓ is the whole reply.
    print_success(f"Enrolled in {label}")
    steps((_lab_cmd(args, "launch"), "start the machine"),
          (_lab_cmd(args, "info"), "objective, flags and live status"))
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


def _render_systems_table(items: List[Dict[str, Any]], title: str = "Machines",
                          show_ids: Optional[bool] = None):
    """The live machine list.

    Columns appear only when they carry information: the UUID column is for
    telling several machines apart when you have to name one with
    ``--system``, and the expiry column only exists on labs that set a
    deadline. On the common single-machine lab that leaves name, state and
    the IP you're about to nmap — which is the whole point of the table.
    """
    if show_ids is None:
        show_ids = len(items) > 1
    show_expiry = any(
        (it.get("expires_at") or (it.get("instance") or {}).get("expires_at"))
        for it in items
    )
    networks = {it.get("_network") for it in items if it.get("_network")}
    show_network = len(networks) > 1

    t = Table(title=title, title_justify="left", show_header=True,
              header_style="bold", border_style="dim")
    t.add_column("#", justify="right", style="dim")
    t.add_column("Machine")
    if show_network:
        t.add_column("Network", style="dim")
    t.add_column("Status")
    t.add_column("IP", style="bold cyan")
    if show_ids:
        t.add_column("ID", style="dim")
    if show_expiry:
        t.add_column("Expires", style="dim")

    for i, it in enumerate(items, 1):
        instance = it.get("instance") or {}
        expires = str(it.get("expires_at") or instance.get("expires_at") or "")
        row: List[Any] = [str(i), truncate(_item_name(it), 32)]
        if show_network:
            row.append(truncate(it.get("_network") or "—", 20))
        row.append(_badge(_system_status(it), STATE_STYLE))
        row.append(_system_ip(it) or "—")
        if show_ids:
            row.append(_item_id(it) or "—")
        if show_expiry:
            row.append(format_datetime(expires) if expires else "—")
        t.add_row(*row)
    console.print(t)


def cmd_lab_systems(api: HackSmarterAPI, config: Config, args) -> int:
    fmt = _format_choice(args, config)
    course_id, label = _resolve_lab(api, args)
    if api.lab_kind(course_id) == "aws":
        labs = api.get_aws_labs(course_id)
        if fmt in ("json", "yaml"):
            print_output(labs, fmt); return 0
        if not labs:
            print_warning(f"{label} has no AWS labs.")
            return 0
        _render_aws_table(labs, title=f"AWS labs — {label}")
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
        print_warning(f"{label} has no machines to show.")
        steps((_lab_cmd(args, "enroll"), "if you haven't enrolled yet"),
              (_lab_cmd(args, "systems --json"), "inspect the raw response"))
        return 0
    # Show ids only when they're ids you could pass to `--system`. A
    # networks lab's inner machines were expanded out of a wrapper, and
    # their ids aren't addressable by /power — printing them in a column
    # next to a name invites exactly the command that won't work.
    _render_systems_table(items, title=f"Machines — {label}",
                          show_ids=len(items) == len(raw_items))
    return 0


def cmd_lab_status(api: HackSmarterAPI, config: Config, args) -> int:
    """Compact 'is my lab on?' check for one lab."""
    fmt = _format_choice(args, config)
    course_id, label = _resolve_lab(api, args)
    if api.lab_kind(course_id) == "aws":
        labs = api.get_aws_labs(course_id)
        if fmt in ("json", "yaml"):
            print_output(labs, fmt); return 0
        if not labs:
            print_warning(f"{label} has no AWS labs.")
            return 0
        ready = [l for l in labs if _aws_state(l) in AWS_READY_STATES]
        _render_status_headline(label, len(ready), len(labs), "ready",
                                noun="environment")
        _render_aws_table(labs, title="Live status")
        for lab in ready:
            _render_aws_creds(lab)
        if not ready:
            steps((_lab_cmd(args, "launch"), "provision it"))
        return 0
    payload = api.get_lab_systems(course_id)
    raw_items = _extract_items(payload)
    items = _flatten_lab_items(raw_items)
    if fmt in ("json", "yaml"):
        print_output(raw_items if raw_items else payload, fmt); return 0
    if not items:
        print_warning(f"{label} has no machines.")
        steps((_lab_cmd(args, "enroll"), "if you haven't enrolled yet"))
        return 0

    up = [it for it in items if _system_status(it) in RUNNING_STATES]
    booting = [it for it in items if _system_status(it) in BOOTING_STATES]
    _render_status_headline(label, len(up), len(items), "up",
                            pending=len(booting))
    _render_systems_table(items, title="Live status")
    if not up and not booting:
        steps((_lab_cmd(args, "launch"), "start it"))
    elif len(up) == 1 and not booting:
        ip = _system_ip(up[0])
        steps((_lab_cmd(args, "vpn"), "download the VPN profile") if ip else None,
              (_lab_cmd(args, "stop"), "power it off when you're done"))
    return 0


def _render_status_headline(label: str, ready: int, total: int, word: str,
                            pending: int = 0, noun: str = "machine") -> None:
    """One line you can read at a glance: ``Dark · 1/1 machines up``."""
    t = Text()
    t.append(label, style="bold white")
    t.append("  ·  ", style="dim")
    style = "green" if ready else ("yellow" if pending else "dim")
    t.append(f"{ready}/{total} {noun}{'' if total == 1 else 's'} {word}",
             style=style)
    if pending:
        t.append(f"  ({pending} booting)", style="yellow")
    # expand=False keeps the box around the sentence instead of stretching a
    # five-word headline across the whole terminal.
    console.print(Panel(t, border_style=style, padding=(0, 2), expand=False))


RUNNING_STATES = ("running", "ready", "active")
BOOTING_STATES = ("provisioning", "starting", "pending")


def _lookup_target(api: HackSmarterAPI, course_id: str,
                   system_id: str) -> Optional[Dict[str, Any]]:
    """The live wrapper for one system/network id (None if the lab has none)."""
    items = _extract_items(api.get_lab_systems(course_id, [system_id]))
    return next((x for x in items if _item_id(x) == system_id),
                items[0] if items else None)


def cmd_lab_launch(api: HackSmarterAPI, config: Config, args) -> int:
    import time
    fmt = _format_choice(args, config)
    # One contract for scripts: --json/--yaml emits exactly one document on
    # stdout — the final state after any wait — and every bit of prose
    # (progress, warnings, hints) either goes to stderr or doesn't happen.
    structured = fmt in ("json", "yaml")
    course_id, label = _resolve_lab(api, args)

    # AWS labs have no VM to power on — different endpoint, different
    # payload, credentials instead of an IP.
    if api.lab_kind(course_id) == "aws":
        return _cmd_lab_launch_aws(api, config, args, course_id, label)

    current: Optional[Dict[str, Any]] = None   # live wrapper, when we have it
    target = label
    if args.system:
        system_id = resolve_system_id(api, course_id, args.system)
    else:
        # Auto-select from the OUTER wrapper (system_id for systems-labs,
        # course_network_id for networks-labs) — that's what /power expects.
        # Do NOT flatten first; the inner machine ids won't work.
        systems = _extract_items(api.get_lab_systems(course_id))
        if len(systems) == 1:
            current = systems[0]
            system_id = _item_id(current) or ""
            target = _course_label(current) or label
        elif not systems:
            print_error(f"{label} has no machines to launch.")
            steps((_lab_cmd(args, "enroll"), "if you haven't enrolled yet"),
                  (_lab_cmd(args, "info"), "check what this lab contains"),
                  to_stderr=True)
            return 1
        else:
            print_error(f"{label} has several targets — name one:")
            # Render the wrappers, not their machines: the IDs printed here
            # have to be ones --system will accept, and an inner machine id
            # is not addressable via /power.
            _render_systems_table(systems, title="Targets", show_ids=True)
            steps((_lab_cmd(args, f"launch {quote_arg(_course_label(systems[0]))}"),
                   "for example"), to_stderr=True)
            return 1

    # Read the live state before powering anything on. The server answers a
    # power-on for a machine that's already up with "System is already
    # running", and rejects one for a machine still provisioning with a 400
    # — both of which read as a failed launch from the outside.
    if current is None:
        try:
            current = _lookup_target(api, course_id, system_id)
        except Exception:
            current = None  # unreadable — launch anyway, that reports why
    state = _system_status(current) if current else ""
    machines = _flatten_lab_items([current]) if current else []

    if state in RUNNING_STATES:
        if fmt in ("json", "yaml"):
            print_output(current, fmt); return 0
        print_info(f"{target} is already running.")
        _render_systems_table(machines, title="Live status")
        _launch_done_steps(args, machines, target)
        return 0

    booting = state in BOOTING_STATES
    if booting:
        if structured and not args.wait:
            print_output(current, fmt); return 0
        if not structured:
            print_info(f"{target} is already {human_state(state)} — "
                       f"picking up the existing boot.")
    else:
        # Kick a heartbeat before launching so the server treats us as an
        # "active" viewer (the browser does the same on the /take page).
        try:
            api.heartbeat_for_course(course_id)
        except Exception:
            pass  # non-fatal — launch will still be attempted

        data = api.launch_system(course_id, system_id)
        if structured and not args.wait:
            print_output(data, fmt); return 0
        if not structured:
            print_success(f"Starting {target}")
            if isinstance(data, dict) and data.get("message") and not args.wait:
                print_info(str(data["message"]))

    if not args.wait:
        print_info("Machines take 2–5 minutes to come up.")
        steps((_lab_cmd(args, "status"), "check whether it's up yet"))
        return 0

    # ── wait loop ────────────────────────────────────────────────────────
    #
    # The progress line is a spinner carrying the current state and elapsed
    # time, with one permanent line printed per *change* of state. Polling
    # noise stays on the spinner; what you scroll back to afterwards is the
    # three or four lines that actually happened.
    import contextlib
    started = time.monotonic()
    deadline = started + args.timeout
    last_heartbeat = 0.0
    last_state = None
    machines = []
    wrapper: Optional[Dict[str, Any]] = current
    if not structured:
        console.print()

    progress = (contextlib.nullcontext() if structured
                else console.status("", spinner="dots"))
    with progress as spinner:
        while time.monotonic() < deadline:
            elapsed = time.monotonic() - started
            # A networks-lab target is a wrapper around several machines;
            # flatten so the progress line and the final table talk about
            # the machines the user actually connects to.
            poll_error = ""
            try:
                it = _lookup_target(api, course_id, system_id)
                wrapper = it or wrapper
                machines = _flatten_lab_items([it]) if it else []
                state = _system_status(it) if it else "unknown"
                ip = _system_ip(machines[0]) if len(machines) == 1 else ""
            except Exception as e:
                # Keep polling — a status call that fails once says nothing
                # about the machine — but say so on a permanent line, not on
                # the spinner, which erases itself.
                state = "unreadable"; ip = ""; poll_error = str(e)

            if state != last_state:
                if not structured:
                    line = Text(f"  {time.strftime('%H:%M:%S')}  ",
                                style="dim")
                    line.append(_badge(state, STATE_STYLE))
                    if poll_error:
                        line.append(f"  {poll_error}", style="dim")
                    elif ip:
                        line.append("  ")
                        line.append(ip, style="bold cyan")
                    elif len(machines) > 1:
                        up = sum(1 for m in machines
                                 if _system_status(m) in RUNNING_STATES)
                        line.append(f"  {up}/{len(machines)} machines up",
                                    style="dim")
                    console.print(line)
                last_state = state

            if state in RUNNING_STATES:
                break
            if state in ("error", "failed"):
                print_error(f"{target} failed to start ({human_state(state)}).")
                steps((_lab_cmd(args, "reset"), "re-provision it"),
                      (_lab_cmd(args, "status"), "look at the current state"),
                      to_stderr=True)
                if structured:
                    print_output(wrapper or {"state": state}, fmt)
                return 1

            if not structured:
                spinner.update(status=Text(
                    f"  waiting for {target} — {human_state(state)}, "
                    f"{human_duration(elapsed)} elapsed", style="dim"))

            # Keep the session "warm" — the browser heartbeats every ~10s
            # while sitting on the /take page. Without it the server may
            # pause the launch or refuse to progress it.
            if time.monotonic() - last_heartbeat > 10:
                try:
                    api.heartbeat_for_course(course_id)
                except Exception:
                    pass
                last_heartbeat = time.monotonic()

            time.sleep(3)

    took = human_duration(time.monotonic() - started)
    if last_state in RUNNING_STATES:
        if structured:
            # The one document the wait was for: the final live state.
            print_output(wrapper or machines, fmt)
            return 0
        if len(machines) > 1:
            print_success(f"{target} is up — all {len(machines)} machines "
                          f"({took})")
            _render_systems_table(machines, title="Live status")
        else:
            ip = _system_ip(machines[0]) if machines else ""
            t = Text()
            t.append(f"{target} is up", style="")
            if ip:
                t.append(" at ")
                t.append(ip, style="bold cyan")
            t.append(f"  ({took})", style="dim")
            print_success(t)
        _launch_done_steps(args, machines, target)
        return 0

    print_warning(f"Still {human_state(last_state or 'unknown')} after {took} "
                  f"— giving up on the wait, not on the machine.")
    steps((_lab_cmd(args, "status"), "check again in a minute"),
          (f"{_lab_cmd(args, 'launch')} --timeout 900", "wait longer next time"),
          to_stderr=True)
    if structured:
        print_output(wrapper or {"state": last_state or "unknown"}, fmt)
    return 2


def _launch_done_steps(args, machines: List[Dict[str, Any]],
                       label: str = "") -> None:
    """What to do with a machine that just came up.

    A running box you can't reach isn't progress — the VPN is the next step
    every single time, and it's the one people forget.
    """
    ips = [ip for ip in (_system_ip(m) for m in machines) if ip]
    steps(
        (_lab_cmd(args, "vpn"), "download the VPN profile"),
        (f"sudo openvpn {_vpn_filename(label or getattr(args, 'identifier', ''))}",
         "connect to the lab network"),
        (f"nmap -sVC -T4 {ips[0]}", "start looking") if len(ips) == 1 else None,
        header="Next:",
    )


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
    if not systems:
        raise LookupError("this lab has no machines — enroll first, or check "
                          "`hsmcli lab <name> info`")
    names = ", ".join(sorted(_course_label(s) for s in systems))
    raise LookupError(f"this lab has several machines — name one: {names}")


def cmd_lab_stop(api: HackSmarterAPI, config: Config, args) -> int:
    fmt = _format_choice(args, config)
    course_id, label = _resolve_lab(api, args)
    if api.lab_kind(course_id) == "aws":
        return _cmd_lab_aws_action(api, config, args, course_id, "stop", label)
    course_id, system_id = _resolve_lab_system(api, args)
    data = api.power_off_system(course_id, system_id)
    if fmt in ("json", "yaml"):
        print_output(data, fmt); return 0
    print_success(f"Powered off {label} — runtime stops being billed.")
    steps((_lab_cmd(args, "launch"), "start it again later"))
    return 0


def cmd_lab_reset(api: HackSmarterAPI, config: Config, args) -> int:
    fmt = _format_choice(args, config)
    course_id, label = _resolve_lab(api, args)
    if api.lab_kind(course_id) == "aws":
        return _cmd_lab_aws_action(api, config, args, course_id, "reset", label)
    course_id, system_id = _resolve_lab_system(api, args)
    data = api.reset_system(course_id, system_id)
    if fmt in ("json", "yaml"):
        print_output(data, fmt); return 0
    print_success(f"Reset requested for {label} — it comes back on a new IP.")
    # Show where things stand, honestly labelled: a status read this soon
    # after the POST usually still shows the old machine (and its old IP),
    # so don't present it as the post-reset state.
    try:
        items = _extract_items(api.get_lab_systems(course_id, [system_id]))
        if items:
            _render_systems_table(_flatten_lab_items(items),
                                  title="Status (may still show the old "
                                        "machine)")
    except Exception as e:
        print_warning(f"Couldn't read the current status: {e}")
    steps((_lab_cmd(args, "status"), "the new IP appears once it's back up"))
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
    """One row per AWS environment. Same column discipline as the machine
    table: ids only when there's something to tell apart, expiry only when
    the lab sets one."""
    show_ids = len(labs) > 1
    show_expiry = any(lab.get("expires_at") for lab in labs)
    t = Table(title=title, title_justify="left", show_header=True,
              header_style="bold", border_style="dim")
    t.add_column("#", justify="right", style="dim")
    t.add_column("Environment")
    t.add_column("State")
    if show_expiry:
        t.add_column("Time left")
    if show_ids:
        t.add_column("ID", style="dim")
    for i, lab in enumerate(labs, 1):
        row: List[Any] = [
            str(i),
            truncate(lab.get("name") or "?", 36),
            _badge(_aws_state(lab), STATE_STYLE),
        ]
        if show_expiry:
            expires = str(lab.get("expires_at") or "")
            left = format_time_left(expires)
            cell = Text(left.replace(" left", "") if left else "—")
            if left and left != "expired":
                cell.stylize("green" if "h" in left else "yellow")
            elif left == "expired":
                cell.stylize("red")
            if expires:
                cell.append(f"  (until {format_datetime(expires)})", style="dim")
            row.append(cell)
        if show_ids:
            row.append(str(lab.get("aws_lab_id") or "—"))
        t.add_row(*row)
    console.print(t)


def _aws_env_var(key: str) -> Optional[str]:
    """The AWS env var a terraform output feeds, or None.

    Labs rarely name an output plain ``access_key``. CloudGoat scenarios
    prefix theirs with the scenario and the IAM user they minted
    (``cloudgoat_output_chris_secret_key``), so an exact-match table
    exports nothing the aws CLI reads. The suffix carries the meaning —
    tried longest first, so ``secret_access_key`` isn't mistaken for an
    ``access_key``.
    """
    k = str(key).lower().strip("_")
    if k in _AWS_ENV_KEYS:
        return _AWS_ENV_KEYS[k]
    for suffix in sorted(_AWS_ENV_KEYS, key=len, reverse=True):
        if k.endswith("_" + suffix):
            return _AWS_ENV_KEYS[suffix]
    return None


def _aws_env_map(outputs: Dict[str, Any]) -> Tuple[Dict[str, str],
                                                   Dict[str, List[str]]]:
    """Split the outputs into ``{key: env var}`` and the contested ones.

    A scenario that mints two IAM users hands back two outputs claiming
    ``AWS_ACCESS_KEY_ID``. Picking one would be a guess, and the guess
    that goes wrong pairs one user's key id with another's secret — a
    credential that fails in a way that looks like the lab is broken. So
    a contested env var is claimed by nobody, and the caller can say why.
    """
    claims: Dict[str, List[str]] = {}
    for k, v in outputs.items():
        if isinstance(v, (dict, list)) or v is None:
            continue
        env = _aws_env_var(k)
        if env:
            claims.setdefault(env, []).append(str(k))
    resolved = {keys[0]: env for env, keys in claims.items() if len(keys) == 1}
    contested = {env: keys for env, keys in claims.items() if len(keys) > 1}
    return resolved, contested


def _aws_env_exports(outputs: Dict[str, Any]) -> List[str]:
    """``export FOO=bar`` lines for a lab's terraform outputs.

    Known keys map onto the standard AWS env vars so `eval` is enough to
    make the aws CLI work; anything else is passed through as
    ``HSM_<KEY>`` rather than dropped.
    """
    resolved, _ = _aws_env_map(outputs)
    lines: List[str] = []
    for k, v in outputs.items():
        if isinstance(v, (dict, list)) or v is None:
            continue
        env = resolved.get(str(k))
        if not env:
            env = "HSM_" + re.sub(r"[^A-Z0-9]+", "_", str(k).upper()).strip("_")
        lines.append(f"export {env}={shlex.quote(str(v))}")
    return lines


def _creds_body(outputs: Dict[str, Any]):
    """Lay the outputs out so no value is ever cut short.

    Key beside value reads best, but these keys are long
    (``cloudgoat_output_chris_secret_key``) and the values are secrets:
    let rich shrink that layout and it ellipsises a key into a phantom
    second row, or a secret into something that no longer works when it's
    pasted. So the two columns are used only when both fit, and the
    fallback stacks each value under its own key.
    """
    keys = [str(k).replace("_", " ") for k in outputs]
    vals = [str(v) for v in outputs.values()]
    # Panel borders (2) plus its padding (4), then the table's own cell
    # padding (4) — what's left has to hold the widest key and value.
    room = console.width - 6
    if max(map(len, keys)) + max(map(len, vals)) + 8 <= room:
        t = Table(show_header=False, border_style="dim", box=None,
                  padding=(0, 2))
        t.add_column("key", style="dim", no_wrap=True)
        t.add_column("value", style="bold cyan", overflow="fold")
        for k, v in zip(keys, vals):
            t.add_row(Text(k), Text(v))
        return t
    body: List[Text] = []
    for k, v in zip(keys, vals):
        if body:
            body.append(Text(""))
        body.append(Text(k, style="dim"))
        body.append(Text(v, style="bold cyan", overflow="fold"))
    return Group(*body)


def _render_aws_creds(lab: Dict[str, Any]) -> bool:
    """Print a lab's terraform outputs (the IAM keys). False if none yet."""
    outputs = lab.get("terraform_outputs") or {}
    if not isinstance(outputs, dict) or not outputs:
        return False
    expires = str(lab.get("expires_at") or "")
    left = format_time_left(expires)
    title = f"Credentials — {lab.get('name') or lab.get('aws_lab_id')}"
    subtitle = None
    if expires:
        subtitle = f"expires {format_datetime(expires)}" + (f" ({left})" if left else "")
    console.print(Panel(_creds_body(outputs), title=title, subtitle=subtitle,
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
                        course_id: str, lab_label: str = "") -> int:
    import time
    fmt = _format_choice(args, config)
    lab = _resolve_aws_lab(api, course_id, getattr(args, "system", None))
    lab_id = lab["aws_lab_id"]
    label = lab.get("name") or lab_label or lab_id

    if _aws_state(lab) in AWS_READY_STATES:
        if fmt in ("json", "yaml"):
            print_output(lab, fmt)
            return 0
        print_info(f"{label} is already up.")
        _render_aws_creds(lab)
        steps((_lab_cmd(args, "creds --export"), "load the keys into your shell"),
              (_lab_cmd(args, "extend"), "buy more time"),
              (_lab_cmd(args, "reset"), "start over with fresh keys"))
        return 0

    structured = fmt in ("json", "yaml")
    inputs = _aws_inputs(lab, args)
    if inputs.get("allowed_ip"):
        hint = ("" if getattr(args, "allowed_ip", None)
                else "  (as HackSmarter sees you — override with --allowed-ip)")
        msg = f"Locking the lab to {inputs['allowed_ip']}{hint}"
        info_err(msg) if structured else print_info(msg)

    data = api.aws_lab_power(course_id, lab_id, "start", inputs)
    if not args.wait:
        if structured:
            print_output(data, fmt)
            return 0
        print_success(f"Building {label}")
        print_info("Terraform takes a couple of minutes.")
        steps((_lab_cmd(args, "status"), "check whether it's ready"))
        return 0

    import contextlib
    if not structured:
        print_success(f"Building {label}")
    started = time.monotonic()
    deadline = started + args.timeout
    last_state = _aws_state(lab)
    if not structured:
        console.print()

    progress = (contextlib.nullcontext() if structured
                else console.status("", spinner="dots"))
    with progress as spinner:
        while time.monotonic() < deadline:
            try:
                lab = {**api.get_aws_lab(course_id, lab_id), "aws_lab_id": lab_id}
                state = _aws_state(lab)
            except Exception as e:
                lab = {"aws_lab_id": lab_id, "state": "unknown",
                       "error_message": str(e)}
                state = "unreadable"

            if state != last_state:
                if not structured:
                    line = Text(f"  {time.strftime('%H:%M:%S')}  ", style="dim")
                    line.append(_badge(state, STATE_STYLE))
                    console.print(line)
                last_state = state

            if state in AWS_READY_STATES or state in AWS_FAILED_STATES:
                break

            if not structured:
                spinner.update(status=Text(
                    f"  terraform is applying — {human_duration(time.monotonic() - started)}"
                    f" elapsed", style="dim"))
            time.sleep(5)

    took = human_duration(time.monotonic() - started)
    if last_state in AWS_READY_STATES:
        if structured:
            print_output(lab, fmt)
            return 0
        print_success(f"{label} is ready  ({took})")
        if not _render_aws_creds(lab):
            print_warning("It's ready, but HackSmarter returned no credentials.")
        steps((_lab_cmd(args, "creds --export"),
               "eval this to load the keys into your shell"))
        return 0
    if last_state in AWS_FAILED_STATES:
        print_error(f"{label} failed to build ({human_state(last_state)}).")
        if lab.get("error_message"):
            print_warning(str(lab["error_message"]))
        steps((_lab_cmd(args, "launch"), "try again"), to_stderr=True)
        if structured:
            print_output(lab, fmt)
        return 1

    print_warning(f"Still {human_state(last_state)} after {took} — giving up on "
                  f"the wait, not on the lab.")
    steps((_lab_cmd(args, "status"), "check again in a minute"),
          to_stderr=True)
    if structured:
        print_output(lab, fmt)
    return 2


def _cmd_lab_aws_action(api: HackSmarterAPI, config: Config, args,
                        course_id: str, action: str,
                        lab_label: str = "") -> int:
    """stop / reset / extend for an AWS lab, then show the fresh status."""
    fmt = _format_choice(args, config)
    lab = _resolve_aws_lab(api, course_id, getattr(args, "system", None))
    lab_id = lab["aws_lab_id"]
    label = lab.get("name") or lab_label or lab_id

    # `reset` tears the environment down and re-applies, so it needs the
    # same inputs `start` did; stop/extend don't take any.
    inputs = _aws_inputs(lab, args) if action == "reset" else None
    data = api.aws_lab_power(course_id, lab_id, action, inputs)
    if fmt in ("json", "yaml"):
        print_output(data, fmt)
        return 0

    print_success({
        "stop": f"Tearing down {label} — the keys stop working.",
        "reset": f"Rebuilding {label} from scratch.",
        "extend": f"Extended {label}'s time limit.",
    }[action])
    try:
        fresh = {**api.get_aws_lab(course_id, lab_id), "aws_lab_id": lab_id}
    except Exception as e:
        print_warning(f"Couldn't read the new status: {e}")
        return 0
    _render_aws_table([fresh], title="Status")
    if action in ("reset", "extend"):
        _render_aws_creds(fresh)
    if action == "reset":
        print_info("Terraform takes a couple of minutes to re-apply.")
        steps((_lab_cmd(args, "creds"), "the new keys land here"))
    return 0


def cmd_lab_creds(api: HackSmarterAPI, config: Config, args) -> int:
    """Print an AWS lab's IAM credentials (terraform outputs)."""
    fmt = _format_choice(args, config)
    course_id, label = _resolve_lab(api, args)
    if api.lab_kind(course_id) != "aws":
        print_error(f"{label} is a machine lab — it has no IAM keys.")
        steps((_lab_cmd(args, "status"), "the IP is what you want"),
              (_lab_cmd(args, "vpn"), "get on its network"),
              to_stderr=True)
        return 2
    lab = _resolve_aws_lab(api, course_id, args.system)
    outputs = lab.get("terraform_outputs") or {}

    if args.export:
        # stdout must stay eval-able: warnings go to stderr, nothing else
        # is printed.
        if not outputs:
            print_error(f"No credentials yet — {label} is "
                        f"{human_state(_aws_state(lab))}.")
            steps((_lab_cmd(args, "launch"), "build it first"), to_stderr=True)
            return 1
        for env, keys in _aws_env_map(outputs)[1].items():
            info_err(f"{len(keys)} outputs could be {env} "
                     f"({', '.join(sorted(keys))}) — exported as HSM_* "
                     f"instead, so pick one and set {env} yourself.")
        for line in _aws_env_exports(outputs):
            print(line)
        return 0
    if fmt in ("json", "yaml"):
        print_output(outputs or lab, fmt)
        return 0
    if not _render_aws_creds(lab):
        print_warning(f"No credentials yet — {label} is "
                      f"{human_state(_aws_state(lab))}.")
        steps((_lab_cmd(args, "launch"), "build it first"))
        return 1
    steps((f'eval "$({_lab_cmd(args, "creds --export")})"',
           "load these into your shell for the aws CLI"))
    return 0


def cmd_lab_extend(api: HackSmarterAPI, config: Config, args) -> int:
    course_id, label = _resolve_lab(api, args)
    if api.lab_kind(course_id) != "aws":
        print_error(f"`extend` only applies to AWS labs — {label} isn't one.")
        steps((_lab_cmd(args, "status"), "check how long it has left"),
              to_stderr=True)
        return 2
    return _cmd_lab_aws_action(api, config, args, course_id, "extend", label)


def _render_flags_table(items: List[Dict[str, Any]], title: str = "Flags"):
    """The flags table.

    Columns earn their place: ``Points`` appears only when the lab actually
    scores its questions (most don't — every value is ``—``), and the hint
    marker rides along in the prompt column instead of taking a column of
    its own. ``match_type`` is dropped entirely: it's ``exact`` on every
    flag ever seen, and it says nothing you can act on.
    """
    scored = any(q.get("points") for q in items)
    t = Table(title=title, show_header=True, title_justify="left",
              header_style="bold", border_style="dim")
    t.add_column("#", justify="right", style="dim")
    t.add_column("Flag")
    t.add_column("Status")
    if scored:
        t.add_column("Points", justify="right")
    for i, q in enumerate(items, 1):
        prompt = Text(truncate(q.get("prompt") or "?", 58))
        if q.get("has_hint"):
            prompt.append("  (hint available)", style="dim")
        row: List[Any] = [
            str(i),
            prompt,
            _badge(q.get("state") or "not_attempted", QUESTION_STATE_STYLE),
        ]
        if scored:
            row.append(str(q.get("points") or "—"))
        t.add_row(*row)
    console.print(t)


def _take_is_complete(take: Dict[str, Any]) -> bool:
    """Whether /take already says the playthrough is marked complete.

    Solving every flag doesn't finish a lab — the lessons still need the
    `complete` POST — so this is what decides whether the finish-line hint
    should be "complete" or "certificate".
    """
    body = take.get("course", take) if isinstance(take, dict) else {}
    pt = body.get("course_playthrough") if isinstance(body, dict) else None
    return bool((pt or {}).get("is_complete"))


def _finish_line_step(take: Dict[str, Any], args) -> Tuple[str, str]:
    """The next command once every flag is in: `complete`, then the PDF."""
    if _take_is_complete(take):
        return (_lab_cmd(args, "certificate"), "download the PDF")
    return (_lab_cmd(args, "complete"), "mark the lab complete")


def cmd_lab_flags(api: HackSmarterAPI, config: Config, args) -> int:
    fmt = _format_choice(args, config)
    course_id, label = _resolve_lab(api, args)
    take = api.get_course_take(course_id)
    questions = api.extract_questions(take)
    if fmt == "json":
        print_json(questions); return 0
    if fmt == "yaml":
        print_yaml(questions); return 0
    if not questions:
        print_warning(f"{label} has no flags to submit.")
        return 0
    _render_flags_table(questions, title=f"Flags — {label}")
    console.print()

    solved = [q for q in questions
              if (q.get("state") or "").lower() == "correct"]
    summary = f"{len(solved)}/{len(questions)} solved"
    total_pts = sum(int(q.get("points") or 0) for q in questions)
    if total_pts:
        got_pts = sum(int(q.get("points") or 0) for q in solved)
        summary += f" · {got_pts}/{total_pts} points"
    print_info(summary)

    unsolved = [q for q in questions if q not in solved]
    if unsolved:
        # Suggest the selector the user would actually type: `submit` takes
        # a prompt substring, and "user"/"root" is how the flags are named.
        hint = _flag_selector(unsolved[0], questions)
        steps((_lab_cmd(args, f"submit {hint} '<flag>'"), "submit an answer"))
    else:
        steps(_finish_line_step(take, args),
              (_lab_cmd(args, "stop"), "power the machine off"))
    return 0


def _flag_selector(question: Dict[str, Any],
                   questions: List[Dict[str, Any]]) -> str:
    """The shortest thing ``submit`` will accept for this question.

    Prefers the "user"/"root" keyword when it identifies the question
    uniquely (it does on virtually every challenge lab), and falls back to
    the 1-based index, which always works.
    """
    prompt = (question.get("prompt") or "").lower()
    for word in ("user", "root", "admin", "flag"):
        if word not in prompt:
            continue
        if sum(1 for q in questions
               if word in (q.get("prompt") or "").lower()) == 1:
            return word
    try:
        return str(questions.index(question) + 1)
    except ValueError:
        return "1"


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
    course_id, label = _resolve_lab(api, args)
    take = api.get_course_take(course_id)
    questions = api.extract_questions(take)
    try:
        q = _match_question(questions, args.selector)
    except LookupError as e:
        print_error(str(e))
        if questions:
            _render_flags_table(questions, title=f"Flags — {label}")
            steps((_lab_cmd(args, f"submit {_flag_selector(questions[0], questions)} "
                                  f"'<flag>'"), "pick by keyword, or by number",),
                  to_stderr=True)
        return 2

    if (q.get("state") or "").lower() == "correct" and not args.force:
        print_warning(f"Already solved: {truncate(q.get('prompt') or '?', 50)}")
        if q.get("last_submission"):
            print_info(f"You submitted: {q['last_submission']}")
        steps((f"{_lab_cmd(args, 'submit')} {quote_arg(args.selector)} "
               f"{quote_arg(args.value)} --force", "submit it again anyway"))
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
    prompt_short = truncate(q.get("prompt") or "?", 56)
    if correct is None:
        # Never report an unparsed reply as a wrong flag — show it instead.
        print_warning(f"HackSmarter's reply didn't say whether that was right "
                      f"— {prompt_short}")
        print_json(data)
        return 1
    if correct:
        pts = q.get("points")
        print_success(f"Correct — {prompt_short}"
                      + (f"  (+{pts} points)" if pts else ""))
        # The server echoes its own canonical casing; only worth showing when
        # it differs by more than case/whitespace.
        if answer and answer.strip().lower() != (args.value or "").strip().lower():
            print_info(f"Recorded as: {answer}")
        # Where that leaves the lab: the remaining flag, or the finish line.
        remaining = [x for x in questions
                     if x is not q and (x.get("state") or "").lower() != "correct"]
        if remaining:
            print_info(f"{len(questions) - len(remaining)}/{len(questions)} solved")
            steps((_lab_cmd(args, f"submit {_flag_selector(remaining[0], questions)} "
                                  f"'<flag>'"), "next one"))
        else:
            # Every flag in, but the lab isn't *finished* until its lessons
            # are marked complete — that POST is what mints the certificate.
            print_success(f"Every flag submitted — {label} is solved.")
            steps(_finish_line_step(take, args),
                  (_lab_cmd(args, "stop"), "power the machine off"))
    else:
        print_error(f"Not the flag — {prompt_short}")
        # Server sometimes echoes hints or attempt counters, flat or nested.
        labels = {"hint": "Hint", "attempts_remaining": "Attempts left",
                  "message": "Server says"}
        result = data.get("result") if isinstance(data, dict) else None
        seen = set()
        for scope in (data, result):
            if not isinstance(scope, dict):
                continue
            for k, human in labels.items():
                v = scope.get(k)
                if v and k not in seen:
                    seen.add(k)
                    print_info(f"{human}: {v}")
    return 0 if correct else 1


def _vpn_filename(label: str) -> str:
    """``dark.ovpn`` for a lab called "Challenge Lab: Dark (Easy)"."""
    return f"{slugify(label, fallback='hsm-lab')}.ovpn"


def _confirm_overwrite(dest: str, force: bool) -> bool:
    """True if it's OK to write ``dest``.

    An existing file is only replaced when --force says so or a person at
    a terminal confirms; a script that didn't pass --force gets a clean
    refusal instead of a silently clobbered download. The prompt lives on
    stderr so it can't leak into redirected output.
    """
    import os
    if force or not os.path.exists(dest):
        return True
    if sys.stdin.isatty() and sys.stderr.isatty():
        # Text(), not an f-string: rich would eat the [y/N] hint as markup,
        # and a path containing brackets would crash the prompt outright.
        err_console.print(Text(f"{dest} exists — overwrite? [y/N] "), end="")
        try:
            answer = input()
        except EOFError:
            answer = ""
        if answer.strip().lower() in ("y", "yes"):
            return True
        print_error(f"Left {dest} alone.")
        return False
    print_error(f"{dest} already exists — pass --force to overwrite it.")
    return False


def cmd_lab_vpn(api: HackSmarterAPI, config: Config, args) -> int:
    course_id, label = _resolve_lab(api, args)
    if args.print:
        # `--print > lab.ovpn` must produce a working profile, so stdout
        # carries the profile and nothing else; the confirmation goes to
        # stderr, and no file is written unless -o asks for one.
        dest = args.output
        if dest and not _confirm_overwrite(dest, getattr(args, "force", False)):
            return 2
        text = api.get_vpn_config(course_id, dest_path=dest)
        print(text)
        info_err(f"VPN profile for {label}"
                 + (f" → also written to {dest}" if dest else ""))
        return 0
    dest = args.output
    if not dest:
        # Named after the lab, not its UUID: this file gets passed to
        # openvpn by hand, sits in a working directory next to the notes,
        # and `hsm-bb164cba-ddc9-4cb0-8e95-ad4853d0143c.ovpn` is unusable
        # for either.
        dest = _vpn_filename(label)
    if not _confirm_overwrite(dest, getattr(args, "force", False)):
        return 2
    api.get_vpn_config(course_id, dest_path=dest)
    print_success(f"VPN profile for {label} → {dest}")
    steps((f"sudo openvpn {dest}", "connect (keep it running)"),
          (_lab_cmd(args, "status"), "check the machine is up"))
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
    course_id, label = _resolve_lab(api, args)
    body = _unwrap_course(api.get_course(course_id))
    image_path = body.get("image_path")
    if not image_path:
        print_warning(f"{label} has no thumbnail.")
        return 0
    if args.url_only:
        print(api.image_url(image_path))
        return 0
    # With -o the destination is known up front — refuse before spending
    # the download. Without it the extension comes from the bytes, so the
    # check can only happen after.
    if args.output and not _confirm_overwrite(args.output,
                                              getattr(args, "force", False)):
        return 2
    data = api.download_lab_image(image_path)
    dest = args.output or f"{slugify(label, fallback=f'hsm-{course_id}')}" \
                          f"{_guess_image_ext(data)}"
    if not args.output and not _confirm_overwrite(dest,
                                                  getattr(args, "force", False)):
        return 2
    with open(dest, "wb") as f:
        f.write(data)
    print_success(f"Thumbnail for {label} → {dest} ({len(data):,} bytes)")
    return 0


def cmd_lab_complete(api: HackSmarterAPI, config: Config, args) -> int:
    """Mark a lab's lessons complete — flip it from "in progress" to done.

    A challenge lab is usually one lesson, so this is normally one POST; a
    multi-lesson course gets every lesson ticked. The honest signal is what
    /take says afterwards: `is_complete` on the playthrough. When the server
    flips it, it also mints the certificate handle, so a finished lab ends
    with a pointer at `certificate`.
    """
    fmt = _format_choice(args, config)
    course_id, label = _resolve_lab(api, args)
    result = api.complete_course(course_id)

    if fmt in ("json", "yaml"):
        print_output(result, fmt)
        # Honest exit code: asking to complete a lab and it *not* being
        # complete (unsolved flags gate it) is a non-success for a script.
        return 0 if result.get("is_complete") else 1

    newly = result.get("completed") or []
    already = result.get("already") or []
    if newly:
        print_success(f"Marked {len(newly)} lesson(s) complete in {label}")
    elif already:
        print_info(f"Every lesson in {label} was already complete.")

    if result.get("is_complete"):
        completion_id = result.get("completion_id")
        print_success(f"{label} is complete.")
        if completion_id:
            print_info(f"Certificate: {api.base_url}/completion/{completion_id}")
            steps((_lab_cmd(args, "certificate"), "download the PDF"))
        return 0

    # Lessons done but the course still isn't — on HSM labs the certificate
    # waits on the flags, not the reading.
    print_warning(f"{label} isn't finished yet — its flags are still open.")
    steps((_lab_cmd(args, "flags"), "see what's left to submit"))
    return 1


def cmd_lab_certificate(api: HackSmarterAPI, config: Config, args) -> int:
    """Download the completion certificate PDF for a finished lab.

    The PDF lives in a private bucket reachable only through a one-hour
    pre-signed link the API hands out per request, so there's nothing to
    cache: every run asks for a fresh URL. A lab that isn't complete has no
    certificate — that's a 2, with the command that fixes it.
    """
    fmt = _format_choice(args, config)
    course_id, label = _resolve_lab(api, args)
    # Gate on is_complete, not on the completion_id: the id is minted at
    # enroll and is always there, but the certificate itself doesn't exist
    # (the endpoint 404s) until the lab is actually finished.
    comp = api.course_completion(course_id)
    completion_id = comp.get("completion_id")
    if not comp.get("is_complete") or not completion_id:
        print_error(f"{label} isn't complete, so there's no certificate yet.")
        steps((_lab_cmd(args, "complete"), "mark the lessons complete"),
              (_lab_cmd(args, "flags"), "check the flags still open"),
              to_stderr=True)
        return 2

    completion_url = f"{api.base_url}/completion/{completion_id}"
    if args.url_only:
        # The signed download link, not the shareable page — `--url-only`
        # exists to be piped into curl/wget, and the page can't be.
        print(api.certificate_download_url(completion_id))
        return 0
    if fmt in ("json", "yaml"):
        print_output({"completion_id": completion_id,
                      "completion_url": completion_url,
                      "download_url": api.certificate_download_url(completion_id)},
                     fmt)
        return 0

    dest = args.output or (f"{slugify(label, fallback=f'hsm-{course_id}')}"
                           f"-certificate.pdf")
    if not _confirm_overwrite(dest, getattr(args, "force", False)):
        return 2
    data = api.download_certificate(completion_id, dest_path=dest)
    print_success(f"Certificate for {label} → {dest} ({len(data):,} bytes)")
    print_info(f"Shareable page: {completion_url}")
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


# ── errors, in plain language ─────────────────────────────────────────────
#
# Everything below turns an exception into something a person can act on.
# The exception's own message stays technical (endpoint, status, body) for
# --debug and bug reports; what gets printed is the reason plus the command
# that fixes it.

def _explain_error(exc: Exception, args) -> int:
    """Print ``exc`` the way a person needs it, and return the exit code.

    Everything here goes to stderr — the message and its follow-up
    commands are one unit, and splitting them across streams means
    `hsmcli … > out.txt` shows you a bare ✗ with the fix in the file.

    Exit codes are unchanged from the typed-error release: 2 for "you asked
    for something that doesn't exist / isn't allowed yet", 1 for "it broke".
    """
    if isinstance(exc, BrokenPipeError):
        raise exc  # main() handles it quietly — this is not an API failure

    identifier = getattr(args, "identifier", None)
    lab = quote_arg(identifier) if identifier else "<lab>"
    detail = exc.server_message() if isinstance(exc, HttpError) else ""

    if isinstance(exc, AuthError):
        print_error("HackSmarter rejected your session — the cookie is "
                    "missing or expired.")
        steps(("hsmcli auth login", "sign in again (hidden cookie prompt)"),
              ("hsmcli whoami", "check it worked"),
              header="Log in again in your browser, then:",
              to_stderr=True)
        return 1

    if isinstance(exc, (ForbiddenError, NotEnrolledError)):
        # A 403 here is almost never a permissions problem: owned and free
        # labs still need an explicit enroll before the API will serve
        # their flags, machines or VPN.
        print_error(f"You're not enrolled in {lab}, so HackSmarter won't "
                    f"share it yet.")
        if detail:
            info_err(detail)
        steps((f"hsmcli lab {lab} enroll", "free, and takes a second"),
              to_stderr=True)
        return 2

    if isinstance(exc, TransportError):
        print_error("Couldn't reach hacksmarter.org.")
        steps(("hsmcli whoami", "try again once you're back online"),
              header="Check your connection — a lab VPN that's up but not "
                     "routing will do this too.",
              to_stderr=True)
        return 1

    if isinstance(exc, HttpError):
        status = exc.status
        if status == 404:
            print_error("HackSmarter has no record of that.")
            if exc.endpoint:
                info_err(exc.endpoint)
            steps(("hsmcli labs list", "what your account can actually see"),
                  to_stderr=True)
            return 2
        if status == 429:
            print_error("Rate-limited by HackSmarter — too many requests.")
            info_err("Give it a minute, then try again.")
            return 1
        if status and status >= 500:
            print_error(f"HackSmarter's API is having trouble (HTTP {status}).")
            if detail:
                info_err(detail)
            info_err("That's their side, not yours — try again shortly.")
            return 1
        # 400 and friends: the server usually says exactly what it disliked
        # ("System is already running"), and that beats anything we'd write.
        print_error(detail or f"HackSmarter rejected that request "
                              f"(HTTP {status or '?'}).")
        if not getattr(args, "debug", False):
            info_err("Re-run with --debug to see the request and reply.")
        return 1

    print_error(str(exc) or exc.__class__.__name__)
    if not getattr(args, "debug", False):
        info_err("Re-run with --debug to see the request and reply.")
    return 1


def _welcome(config: Config) -> int:
    """The no-arguments screen: what this is, and the first thing to do."""
    signed_in = bool(config.get_cookie())
    console.print()
    console.print(Panel(
        Text.assemble(
            ("HackSmarter from the terminal", "bold white"),
            ("\nBrowse labs, start and stop machines, pull the VPN profile, "
             "submit flags.", ""),
        ),
        title=f"hsmcli {client_version()}", title_align="left",
        border_style="cyan", padding=(0, 2),
    ))
    # Someone who has already pasted a cookie doesn't need step 1 — the
    # welcome screen is also what `hsmcli` alone prints on the hundredth run.
    steps(
        ("hsmcli auth login", "1 · sign in") if not signed_in else None,
        ("hsmcli labs list", "find a lab"),
        ("hsmcli lab <name> launch", "start it"),
        ("hsmcli lab <name> vpn", "get on the lab network"),
        ("hsmcli lab <name> submit user '<flag>'", "submit a flag"),
        ("hsmcli --help", "everything else"),
    )
    return 0


# Keys worth a column in the generic list view, in the order they should
# appear. These endpoints (notifications, events, orgs, bundles, exams) each
# return their own shape, and none is important enough to hand-write a
# renderer for — but a wall of raw JSON isn't a listing either.
_GENERIC_COLUMNS = (
    "name", "title", "subject", "message", "description",
    "state", "status", "type", "kind",
    "created_at", "starts_at", "start_date", "expires_at", "read_at",
)

_GENERIC_LABELS = {
    "created_at": "created", "starts_at": "starts", "start_date": "starts",
    "expires_at": "expires", "read_at": "read",
}


def _render_generic_table(items: List[Dict[str, Any]], title: str) -> bool:
    """Table any list-of-records payload; ``False`` if it can't be tabled.

    Picks the columns from the records themselves: a key earns a column by
    appearing on at least one record with a scalar value. Anything left
    over is still reachable with ``--json``, which the footer says.
    """
    if not items or not all(isinstance(it, dict) for it in items):
        return False
    cols = [k for k in _GENERIC_COLUMNS
            if any(isinstance(it.get(k), (str, int, float, bool))
                   and it.get(k) not in ("", None)
                   for it in items)]
    if not cols:
        return False

    t = Table(title=title, title_justify="left", show_header=True,
              header_style="bold", border_style="dim")
    t.add_column("#", justify="right", style="dim")
    for c in cols:
        t.add_column(_GENERIC_LABELS.get(c, c.replace("_", " ")))
    for i, it in enumerate(items, 1):
        row = [str(i)]
        for c in cols:
            v = it.get(c)
            if c.endswith(("_at", "_date")) and isinstance(v, str):
                row.append(format_datetime(v) or "—")
            elif c in ("state", "status", "type", "kind") and v:
                row.append(_badge(v, STATE_STYLE, default="white"))
            else:
                row.append(truncate(v, 44) if v not in ("", None) else "—")
        t.add_row(*row)
    console.print(t)
    return True


def _simple_get(api_fn, args, config: Config, label: str = "") -> int:
    fmt = _format_choice(args, config)
    data = api_fn()
    if fmt in ("json", "yaml"):
        print_output(data, fmt)
        return 0
    items = _extract_items(data)
    plural = f"{label}s" if label else "results"
    if not items:
        print_info(f"No {plural}.")
        return 0
    if not _render_generic_table(items, plural.capitalize()):
        print_json(data)
        return 0
    console.print()
    print_info(f"{len(items)} {label if len(items) == 1 else plural}"
               f" · --json for every field")
    return 0


# ── argparse wiring ───────────────────────────────────────────────────────

def _add_format_flags(p):
    g = p.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true", help="output raw JSON")
    g.add_argument("--yaml", action="store_true", help="output YAML")


def _positive_int(value: str) -> int:
    """argparse type for --timeout: ``--timeout 0`` never polls and then
    reports "still unknown after 0s", which reads as a launch failure."""
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number")
    if n <= 0:
        raise argparse.ArgumentTypeError("must be a positive number of seconds")
    return n


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hsmcli",
        description="HackSmarter from the terminal — browse labs, start and "
                    "stop machines, pull the VPN profile, submit flags.",
        epilog="Labs are named, not numbered: `hsmcli lab dark launch` works "
               "as well as the UUID. Start with `hsmcli labs list`.",
    )
    p.add_argument("--version", action="version",
                   version=f"hsmcli {client_version()}")
    p.add_argument("--debug", action="store_true",
                   help="trace every API request/response to stderr")
    p.add_argument("--no-color", action="store_true",
                   help="plain output, no ANSI colour (NO_COLOR works too)")
    p.add_argument("--config-dir", help="override config directory (default ~/.hsmcli)")
    sp = p.add_subparsers(dest="command", metavar="COMMAND")

    # auth
    pa = sp.add_parser("auth", help="sign in and manage the session")
    asub = pa.add_subparsers(dest="subcommand")
    _al = asub.add_parser(
        "login",
        help="import an existing Firefox session, or open a browser and "
             "capture one automatically after sign-in")
    _al.add_argument("--github", action="store_true",
                     help="point the guidance at the site's "
                          "Sign-in-with-GitHub button (does not start an "
                          "OAuth flow itself; the imported session is the "
                          "same either way)")
    _al.add_argument("--no-browser", action="store_true",
                     help="don't launch a browser (e.g. over SSH) — use the "
                          "hidden Cookie prompt instead")
    _ic = asub.add_parser(
        "import-cookie",
        help="save a Cookie header non-interactively ('-' or no argument "
             "reads stdin)")
    _ic.add_argument("cookie", nargs="?", default=None)
    _ast = asub.add_parser("status", help="who's signed in, and for how long")
    _add_format_flags(_ast)
    asub.add_parser("logout", help="remove the stored session")

    # config
    pc = sp.add_parser("config", help="manage configuration")
    csub = pc.add_subparsers(dest="subcommand")
    csub.add_parser("show", help="show current config")
    _sc = csub.add_parser(
        "set-cookie",
        help="deprecated — use `auth login` / `auth import-cookie`")
    _sc.add_argument("cookie")
    csub.add_parser("clear-cookie", help="remove stored cookie")
    _sbu = csub.add_parser("set-base-url", help="override API base URL")
    _sbu.add_argument("url")
    _sbu.add_argument("--allow-insecure-http", action="store_true",
                      help="permit an http:// URL — the session cookie will "
                           "travel unencrypted (local development only)")
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
    _ll.add_argument("-T", "--topic", action="append", default=[],
                     type=_topic_arg, metavar="TOPIC",
                     help="filter by subject, as on the website's catalog "
                          "chips (repeatable): " + ", ".join(TOPIC_CHOICES)
                          + " — 'ad' and 'web app' also work")
    _ll.add_argument("--sort", choices=["name", "difficulty", "state", "topic"],
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
        "info", help="lab card: objective/scope, flags and live system status")
    _lif.add_argument("--briefing", action="store_true",
                      help="also render the lesson content (notes, videos)")
    _lif.add_argument("--full", action="store_true",
                      help=f"render every lesson's content (implies "
                           f"--briefing; default: first {BRIEFING_LESSON_LIMIT})")
    _lif.add_argument("--chapters", action="store_true",
                      help="also show the chapter/lesson table")
    _lif.add_argument("--writeups", action="store_true",
                      help="also list the community walkthrough links")
    _lif.add_argument("--bundles", action="store_true",
                      help="also show the subscription bundles this lab is in")
    _lif.add_argument("--all", dest="all_sections", action="store_true",
                      help="show every optional section")
    _add_format_flags(_lif)

    for name, help_text in [
        ("take", "show take/enroll info"),
        ("enroll", "enroll in the lab"),
        ("systems", "list systems (machines) in the lab with live status"),
        ("status", "quick 'is it on?' summary of the lab"),
        ("complete", "mark the lab's lessons complete (finish the course)"),
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
    _lch.add_argument("--timeout", type=_positive_int, default=420,
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
    _lvpn.add_argument("-o", "--output", help="output file (default ./<lab>.ovpn)")
    _lvpn.add_argument("--print", action="store_true",
                       help="print the profile to stdout instead of writing "
                            "a file (combine with -o to also write one)")
    _lvpn.add_argument("--force", action="store_true",
                       help="overwrite an existing file without asking")

    _limg = lsub2.add_parser("image", help="download the lab's thumbnail image")
    _limg.add_argument("-o", "--output", help="output file (default ./<lab>.<ext>)")
    _limg.add_argument("--url-only", action="store_true",
                       help="just print the image URL, don't download")
    _limg.add_argument("--force", action="store_true",
                       help="overwrite an existing file without asking")

    _lcert = lsub2.add_parser(
        "certificate",
        aliases=["cert"],
        help="download the completion certificate PDF (finished labs only)")
    _lcert.add_argument("-o", "--output",
                        help="output file (default ./<lab>-certificate.pdf)")
    _lcert.add_argument("--url-only", action="store_true",
                        help="print the one-hour signed download URL, don't save")
    _lcert.add_argument("--force", action="store_true",
                        help="overwrite an existing file without asking")
    _add_format_flags(_lcert)

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
    try:
        return _run()
    except KeyboardInterrupt:
        # Ctrl-C during a launch poll is normal — the machine keeps booting.
        # A traceback here reads as a crash and buries that.
        err_console.print()
        print_warning("Interrupted. Anything already started keeps running.")
        return 130
    except BrokenPipeError:
        # `hsmcli … | head` closing the pipe early is normal Unix, not an
        # error. Point stdout at devnull so the interpreter's shutdown
        # flush doesn't print "Exception ignored" either; 141 = 128+SIGPIPE,
        # what the shell would report had the default handler run.
        import os
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        return 141


def _run() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "no_color", False):
        disable_color()
    try:
        config = Config(getattr(args, "config_dir", None))
    except (ValueError, OSError) as e:
        print_error(str(e))
        return 1

    if not args.command:
        return _welcome(config)

    # Auth and config subcommands don't need an authenticated API client.
    if args.command == "auth":
        table = {
            "login": cmd_auth_login,
            "import-cookie": cmd_auth_import_cookie,
            "status": cmd_auth_status,
            "logout": cmd_auth_logout,
        }
        fn = table.get(args.subcommand)
        if not fn:
            return _need_subcommand("auth", table)
        try:
            return fn(config, args)
        except Exception as e:
            return _explain_error(e, args)

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
            return _explain_error(e, args)

    # Everything else needs a session. Saying so up front beats letting the
    # first API call come back 401 and reporting it as an auth failure —
    # there's nothing to fail yet if you've never signed in.
    if not config.get_cookie():
        print_error("You're not signed in yet.")
        steps(("hsmcli auth login", "sign in (hidden cookie prompt)"),
              header=f"Log in at {config.get_base_url()} in your browser, "
                     f"then:",
              to_stderr=True)
        return 1

    try:
        api = HackSmarterAPI(config, debug=args.debug)
    except Exception as e:
        print_error(f"Couldn't set up the API client: {e}")
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
                "complete": cmd_lab_complete,
                "certificate": cmd_lab_certificate,
                "cert": cmd_lab_certificate,
            }
            fn = table.get(args.subcommand)
            if not fn:
                return _need_subcommand("lab <identifier>", table)
            return fn(api, config, args)
        if args.command == "notifications":
            return _simple_get(api.get_notifications, args, config, "notification")
        if args.command == "events":
            return _simple_get(api.get_events, args, config, "event")
        if args.command == "subscriptions":
            return _simple_get(api.get_subscriptions, args, config, "subscription")
        if args.command == "orgs":
            return _simple_get(api.get_orgs, args, config, "organization")
        if args.command == "bundles":
            return _simple_get(api.get_bundles, args, config, "bundle")
        if args.command == "exams":
            return _simple_get(api.get_owned_exams, args, config, "exam")
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
                print_error("Couldn't work out which account to bill — none "
                            "of your enrolled labs names a customer.")
                steps(("hsmcli lab <name> enroll", "enroll in one lab first"),
                      to_stderr=True)
                return 1

            payg = api.get_credits(customer_id)
            payg_body = payg.get("data", payg) if isinstance(payg, dict) else payg

            if fmt in ("json", "yaml"):
                print_output({"customer_id": customer_id, "payg": payg_body}, fmt)
                return 0

            # PAYG top-up (usually empty for subscription users)
            body = Text()
            if isinstance(payg_body, dict):
                for k, v in payg_body.items():
                    if isinstance(v, (dict, list)):
                        continue
                    if body:
                        body.append("\n")
                    body.append(f"{str(k).replace('_', ' '):<26}", style="dim")
                    body.append(str(v), style="cyan")
            if not body:
                body.append("nothing on top-up", style="dim")
            console.print(Panel(body, title="Pay-as-you-go credits",
                                title_align="left",
                                border_style="cyan", padding=(0, 2)))
            print_info("This is the top-up balance only. A subscription's "
                       "monthly runtime allowance shows per-lab, in "
                       "`hsmcli lab <name> info`.")
            return 0
    except LookupError as e:
        # Name resolution: "no lab matching 'drak'", "ambiguous 'nova' — …".
        # Already written for a person; don't wrap it.
        print_error(str(e))
        if getattr(args, "identifier", None):
            steps(("hsmcli labs list", "see the names your account can use"),
                  to_stderr=True)
        return 2
    except Exception as e:
        return _explain_error(e, args)

    # Unreachable while every subparser above is dispatched — but if a
    # command is ever added without wiring, "printed help, exited 0" is
    # exactly the dishonest-success shape 0.2.0 fixed elsewhere.
    print_error(f"`{args.command}` is not wired up — this is an hsmcli bug.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
