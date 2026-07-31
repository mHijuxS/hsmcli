"""Identifier resolution: turn ``<name-or-id>`` args into API IDs.

HackSmarter identifies courses/systems by UUID. Users rarely want to paste
UUIDs, so we accept a case-insensitive substring of the course/system name
and disambiguate via a short numeric index (``list_labs`` renders the same
index alongside the name).
"""

import re
from typing import Any, Dict, List, Optional, Tuple


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def is_uuid(s: str) -> bool:
    return bool(_UUID_RE.match(s or ""))


def _extract_items(payload: Any) -> List[Dict[str, Any]]:
    """Best-effort flatten of catalog/enroll responses into a list of items."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("data", "items", "courses", "results", "labs"):
            v = payload.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
        # Some catalog responses group by category: {"category": [items]}.
        flat: List[Dict[str, Any]] = []
        for v in payload.values():
            if isinstance(v, list):
                flat.extend(x for x in v if isinstance(x, dict))
        if flat:
            return flat
    return []


def _item_id(item: Dict[str, Any]) -> Optional[str]:
    """Course ID lookup.

    Catalog entries nest the real course id under ``item.id`` (the outer
    ``id`` is a catalog-card id that the ``/courses/{id}/…`` endpoints
    reject). Enrolled entries expose it at the top level. Prefer the nested
    one when both exist so name-based lookups produce a working id.
    """
    nested = item.get("item")
    if isinstance(nested, dict):
        v = nested.get("id")
        if isinstance(v, str) and v:
            return v
    for k in ("course_id", "system_id", "uuid", "id", "_id"):
        v = item.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def _item_name(item: Dict[str, Any]) -> str:
    for k in ("name", "title", "label", "slug"):
        v = item.get(k)
        if isinstance(v, str) and v:
            return v
    # The systems endpoint nests fields under ``system`` (a sibling of
    # ``id``); catalog entries nest under ``item``.
    for wrapper in ("system", "item"):
        nested = item.get(wrapper)
        if isinstance(nested, dict):
            for k in ("name", "title"):
                v = nested.get(k)
                if isinstance(v, str) and v:
                    return v
    return ""


def resolve_from_list(
    identifier: str, items: List[Dict[str, Any]]
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Resolve ``identifier`` against a list of items.

    Match precedence: exact UUID > exact name (case-insensitive) > unique
    substring match. Returns ``(id, item)`` or raises ``LookupError`` on
    ambiguity / no match.
    """
    if not identifier:
        raise LookupError("empty identifier")
    if is_uuid(identifier):
        for it in items:
            if _item_id(it) == identifier:
                return identifier, it
        return identifier, None  # unknown to us but caller can still use it

    ident_l = identifier.lower()
    exact = [it for it in items if _item_name(it).lower() == ident_l]
    if len(exact) == 1:
        return _item_id(exact[0]) or "", exact[0]
    if len(exact) > 1:
        raise LookupError(
            f"multiple items exactly named '{identifier}' — use the UUID"
        )

    subs = [it for it in items if ident_l in _item_name(it).lower()]
    if len(subs) == 1:
        return _item_id(subs[0]) or "", subs[0]
    if len(subs) > 1:
        names = ", ".join(sorted(_item_name(it) for it in subs)[:8])
        raise LookupError(
            f"ambiguous '{identifier}' — matches: {names}"
            + (" …" if len(subs) > 8 else "")
        )
    raise LookupError(f"no lab matching '{identifier}'")


def resolve_course_id(api, identifier: str) -> str:
    """Resolve a course/lab identifier via catalog + enrolled lists."""
    if is_uuid(identifier):
        return identifier
    # Enrolled first (short list, likely target) then fall back to catalog.
    try:
        enrolled = _extract_items(api.get_enrolled_courses())
    except Exception:
        enrolled = []
    try:
        cid, _ = resolve_from_list(identifier, enrolled)
        if cid:
            return cid
    except LookupError:
        pass
    catalog = _extract_items(api.get_catalog())
    cid, _ = resolve_from_list(identifier, catalog)
    return cid


def resolve_system_id(api, course_id: str, identifier: str) -> str:
    if is_uuid(identifier):
        return identifier
    systems = _extract_items(api.get_lab_systems(course_id))
    sid, _ = resolve_from_list(identifier, systems)
    return sid
