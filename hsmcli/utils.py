"""Value formatters and output dispatch.

The one-line status messages below are thin aliases for :mod:`hsmcli.ui`,
which owns styling — they predate that module and are kept because they
read well at the call site.
"""

import json as _json
from datetime import datetime, timezone
from typing import Any, List

from . import ui
from .ui import console


class Colors:
    """Raw SGR codes, kept for anything embedding colour in a plain string.

    New code should print through :mod:`hsmcli.ui` instead: rich drops the
    escapes automatically when the output isn't a terminal, or when
    ``NO_COLOR`` / ``--no-color`` is set.
    """

    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"
    END = "\033[0m"


def print_success(msg: str):
    ui.ok(msg)


def print_error(msg: str):
    ui.fail(msg)


def print_info(msg: str):
    ui.info(msg)


def print_warning(msg: str):
    ui.warn(msg)


def print_json(data: Any, indent: int = 2):
    print(_json.dumps(data, indent=indent, ensure_ascii=False, default=str))


def print_yaml(data: Any):
    try:
        import yaml
        print(yaml.dump(data, default_flow_style=False, allow_unicode=True))
    except ImportError:
        print_error("PyYAML not installed; falling back to JSON.")
        print_json(data)


def print_table(headers: List[str], rows: List[List[Any]]):
    """Plain-text table. Kept for library callers; the CLI renders its own
    tables with rich (see ``cli._render_*_table``)."""
    if not headers:
        return
    from rich.table import Table
    t = Table(show_header=True, header_style="bold", border_style="dim")
    for h in headers:
        t.add_column(str(h))
    for r in rows:
        t.add_row(*[("" if c is None else str(c)) for c in r])
    console.print(t)


def format_datetime(dt: str) -> str:
    if not dt or not isinstance(dt, str):
        return ""
    try:
        return datetime.fromisoformat(dt.replace("Z", "+00:00")).strftime(
            "%Y-%m-%d %H:%M"
        )
    except (ValueError, TypeError):
        return dt


def format_time_left(dt: str) -> str:
    """'42m left' / '1h12m left' / 'expired' for an ISO8601 deadline.

    Returns '' when the timestamp is missing or unparseable so callers can
    fall back to printing the raw value.
    """
    if not dt or not isinstance(dt, str):
        return ""
    try:
        deadline = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return ""
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    secs = int((deadline - datetime.now(timezone.utc)).total_seconds())
    if secs <= 0:
        return "expired"
    hours, mins = divmod(secs // 60, 60)
    return f"{hours}h{mins:02d}m left" if hours else f"{mins}m left"


def truncate(text: Any, n: int = 50) -> str:
    s = "" if text is None else str(text)
    return s if len(s) <= n else s[: n - 1] + "…"


def print_output(data: Any, output_format: str, table_fn=None):
    """Dispatch to json/yaml/table.

    ``table_fn`` is called with ``data`` to render a table view when the
    format is ``table``; if it is None we fall back to JSON so we never
    lose information.
    """
    if output_format == "json":
        print_json(data)
    elif output_format == "yaml":
        print_yaml(data)
    else:
        if table_fn is None:
            print_json(data)
        else:
            table_fn(data)


def confirm(msg: str, default: bool = False) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    r = input(f"{msg}{suffix}: ").strip().lower()
    if not r:
        return default
    return r in ("y", "yes", "1", "true")
