"""Presentation layer — one console, one vocabulary.

``utils`` holds the pure formatters (dates, truncation, JSON/YAML dumping);
this module owns *how things look*: the console every command prints
through, the status symbols, and the translation from the API's raw state
strings (``na``, ``in_progress``) into words a human recognises.

Three rules worth keeping:

* **Only the human-facing renderers translate.** ``--json`` / ``--yaml``
  always emit the API's own vocabulary, so scripts keep matching on
  ``running`` rather than on "up".
* **Server-supplied text is printed as ``Text``, never as console markup**,
  so a lab description containing ``[bold]`` can't rewrite the output.
* **Every dead end names the next command.** The failure mode of a CLI like
  this isn't a bad error message, it's a correct one that leaves you
  guessing what to type next.
"""

import re
import shutil
from typing import Any, Dict, List, Optional, Tuple, Union

from rich.console import Console
from rich.text import Text
from rich.theme import Theme

# Panels and tables stop growing here. Terminals are routinely 200+ columns
# and a paragraph set that wide is genuinely hard to read — the eye loses
# its place coming back from the right margin. Narrower terminals are
# followed as-is.
MAX_WIDTH = 100
MIN_WIDTH = 50


def _width() -> int:
    try:
        cols = shutil.get_terminal_size(fallback=(80, 24)).columns
    except Exception:  # no controlling terminal at all
        cols = 80
    return max(MIN_WIDTH, min(cols, MAX_WIDTH))


THEME = Theme({
    "ok": "bold green",
    "fail": "bold red",
    "warn": "bold yellow",
    "info": "bold cyan",
    "muted": "dim",
    "cmd": "cyan",
    "heading": "bold white",
    "ip": "bold cyan",
})

console = Console(theme=THEME, width=_width(), highlight=False)
err_console = Console(theme=THEME, width=_width(), highlight=False, stderr=True)


def disable_color() -> None:
    """Honour ``--no-color`` (``NO_COLOR`` is handled by rich itself)."""
    console.no_color = True
    err_console.no_color = True


# ── one-line messages ─────────────────────────────────────────────────────

def _emit(target: Console, symbol: str, style: str, msg: Any) -> None:
    t = Text()
    t.append(f"{symbol} ", style=style)
    # A pre-styled Text keeps its styling (an IP in cyan, say); anything
    # else is stringified and printed literally — never as markup, so
    # server text containing brackets can't inject styles.
    if isinstance(msg, Text):
        t.append_text(msg)
    else:
        t.append(str(msg))
    target.print(t)


def ok(msg: Any) -> None:
    _emit(console, "✓", "ok", msg)


def fail(msg: Any) -> None:
    _emit(err_console, "✗", "fail", msg)


def warn(msg: Any) -> None:
    # stderr, like fail: a warning is a diagnostic, and the ones that fire
    # mid-pipe (launch timeout, "no credentials yet", Ctrl-C) were
    # corrupting `hsmcli … --json > out.json`.
    _emit(err_console, "⚠", "warn", msg)


def info(msg: Any) -> None:
    _emit(console, "ℹ", "info", msg)


def info_err(msg: Any) -> None:
    """Detail belonging to an error — stderr, so it travels with it."""
    _emit(err_console, "ℹ", "info", msg)


def note(msg: Any) -> None:
    """Aside — context, not an outcome. Dim, no symbol."""
    console.print(Text(str(msg), style="muted"))


Step = Union[str, Tuple[str, str]]


def steps(*items: Optional[Step], header: str = "", to_stderr: bool = False) -> None:
    """Print the obvious next commands, one per line.

    Each item is a command, or ``(command, what it does)``. ``None`` entries
    are dropped so callers can build the list conditionally without
    filtering first.
    """
    pairs: List[Tuple[str, str]] = []
    for it in items:
        if not it:
            continue
        if isinstance(it, str):
            pairs.append((it, ""))
        else:
            pairs.append((it[0], it[1] or ""))
    if not pairs:
        return
    target = err_console if to_stderr else console
    if header:
        target.print(Text(header, style="muted"))
    # Align the descriptions into a column — unless one command is long
    # enough that aligning to it would push every description off to the
    # right, at which point a plain two-space gap reads better.
    longest = max(len(c) for c, _ in pairs)
    pad = longest if longest <= 46 else 0
    for cmd, what in pairs:
        t = Text("  → ", style="muted")
        t.append(cmd, style="cmd")
        if what:
            t.append(" " * (max(2, pad - len(cmd) + 2) if pad else 2))
            t.append(what, style="muted")
        target.print(t)


# ── the API's vocabulary, in words people use ─────────────────────────────
#
# The raw strings come from three different subsystems and it shows: a VM
# that was never started is "not_launched", an AWS lab that was never
# started is "na", and a lab you own but haven't opened is "owned" from
# /courses and "not_started" from /catalog. None of that is worth making a
# reader decode mid-engagement.

STATE_LABELS: Dict[str, str] = {
    # lifecycle
    "na": "off",
    "not_launched": "off",
    "stopped": "off",
    "destroyed": "off",
    "provisioning": "booting",
    "starting": "booting",
    "pending": "booting",
    "creating": "building",
    "destroying": "tearing down",
    "stopping": "stopping",
    "running": "running",
    "ready": "ready",
    "active": "running",
    # progress / ownership
    "in_progress": "in progress",
    "not_started": "not started",
    "completed": "completed",
    "owned": "owned",
    "unowned": "not owned",
    "lapsed": "lapsed",
    # questions
    "correct": "solved",
    "incorrect": "wrong",
    "unanswered": "unsolved",
    "not_attempted": "unsolved",
    "attempted": "attempted",
    # trouble
    "error": "error",
    "failed": "failed",
    "unknown": "unknown",
}

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
    "active": "green",
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

QUESTION_STATE_STYLE = {
    "correct": "green",
    "incorrect": "red",
    "attempted": "yellow",
    "not_attempted": "dim",
    "unanswered": "dim",
}


def human_state(raw: Any) -> str:
    """``"in_progress"`` → ``"in progress"``.

    Anything we don't have a word for keeps its own text (so a difficulty
    stays "Easy", not "easy", and a state the API adds tomorrow still
    shows) with underscores softened into spaces.
    """
    s = str(raw or "").strip()
    if not s:
        return "—"
    return STATE_LABELS.get(s.lower(), s.replace("_", " "))


def badge(raw: Any, palette: Dict[str, str], default: str = "white") -> Text:
    """A state as a coloured, human-readable cell."""
    key = str(raw or "").strip().lower()
    return Text(human_state(raw), style=palette.get(key, default))


# ── lab names ─────────────────────────────────────────────────────────────
#
# HackSmarter names labs "<category>: <name> (<difficulty>)". Both affixes
# are rendered separately in our own headers, so repeating them inside the
# name reads as a stutter ("Challenge Lab: Dark (Easy)  Easy  in progress").

_CATEGORY_PREFIX_RE = re.compile(r"^([^:]{1,30}):\s*")
_PAREN_SUFFIX_RE = re.compile(r"\s*\(([^()]*)\)\s*$")

_DIFFICULTY_WORDS = {
    "easy", "medium", "hard", "insane", "beginner", "intermediate",
    "advanced", "expert",
}


def split_lab_name(name: Any) -> Tuple[str, str, str]:
    """``"Challenge Lab: Dark (Easy)"`` → ``("Challenge Lab", "Dark", "Easy")``.

    A trailing parenthetical is only treated as a difficulty when it reads
    like one — "Hack With Me: Active Directory (Odyssey x Triathlon)" keeps
    its parenthetical, because there it's part of the name.
    """
    s = str(name or "").strip()
    if not s:
        return "", "", ""
    category = ""
    m = _CATEGORY_PREFIX_RE.match(s)
    if m:
        category = m.group(1).strip()
        s = s[m.end():]
    difficulty = ""
    m = _PAREN_SUFFIX_RE.search(s)
    if m and m.group(1).strip().lower() in _DIFFICULTY_WORDS:
        difficulty = m.group(1).strip()
        s = s[:m.start()]
    return category, s.strip(), difficulty


def lab_display_name(name: Any) -> str:
    """The bare lab name: ``"Challenge Lab: Dark (Easy)"`` → ``"Dark"``."""
    _, core, _ = split_lab_name(name)
    return core or str(name or "").strip()


def slugify(name: Any, fallback: str = "lab") -> str:
    """Filename-safe slug of a lab name — ``dark``, ``nova-forge``."""
    s = re.sub(r"[^a-z0-9]+", "-", lab_display_name(name).lower()).strip("-")
    return s or fallback


# ── small formatters ──────────────────────────────────────────────────────

def human_duration(seconds: float) -> str:
    """``95`` → ``"1m35s"``; used for timeouts and elapsed launch time."""
    secs = max(0, int(seconds))
    mins, secs = divmod(secs, 60)
    hours, mins = divmod(mins, 60)
    if hours:
        return f"{hours}h{mins:02d}m"
    if mins:
        return f"{mins}m{secs:02d}s"
    return f"{secs}s"


def quote_arg(value: str) -> str:
    """Quote an identifier for a suggested command line, only if needed."""
    s = str(value or "")
    return s if re.fullmatch(r"[A-Za-z0-9._@:/-]+", s or "x") else f"'{s}'"
