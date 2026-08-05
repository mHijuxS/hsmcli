"""Terminal output helpers."""

import json as _json
import sys
from datetime import datetime, timezone
from typing import Any, List


class Colors:
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
    print(f"{Colors.GREEN}✓ {msg}{Colors.END}")


def print_error(msg: str):
    print(f"{Colors.RED}✗ {msg}{Colors.END}", file=sys.stderr)


def print_info(msg: str):
    print(f"{Colors.BLUE}ℹ {msg}{Colors.END}")


def print_warning(msg: str):
    print(f"{Colors.YELLOW}⚠ {msg}{Colors.END}")


def print_json(data: Any, indent: int = 2):
    print(_json.dumps(data, indent=indent, ensure_ascii=False, default=str))


def print_yaml(data: Any):
    try:
        import yaml
        print(yaml.dump(data, default_flow_style=False, allow_unicode=True))
    except ImportError:
        print_error("PyYAML not installed; falling back to JSON.")
        print_json(data)


def print_table(headers: List[str], rows: List[List[Any]], max_width: int = 0):
    if not headers:
        return
    rows = [[("" if c is None else str(c)) for c in r] for r in rows]
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            if i < len(widths):
                widths[i] = max(widths[i], len(c))
    header = " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(f"{Colors.BOLD}{header}{Colors.END}")
    print("-+-".join("-" * w for w in widths))
    for r in rows:
        cells = [(r[i] if i < len(r) else "").ljust(widths[i]) for i in range(len(headers))]
        print(" | ".join(cells))


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


def format_difficulty(diff: str) -> str:
    d = (diff or "").lower()
    if d in ("easy", "beginner"):
        return f"{Colors.GREEN}{diff}{Colors.END}"
    if d in ("medium", "intermediate"):
        return f"{Colors.YELLOW}{diff}{Colors.END}"
    if d in ("hard", "advanced", "expert", "insane"):
        return f"{Colors.RED}{diff}{Colors.END}"
    return diff or "Unknown"


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
