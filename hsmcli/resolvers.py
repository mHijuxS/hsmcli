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
    """Best-effort id extraction across the many wrapper shapes.

    Catalog entries nest the real course id under ``item.id`` (top-level
    ``id`` is a catalog-card id the ``/courses/{id}/…`` endpoints reject).
    Networks-lab status wrappers expose ``course_network_id`` — that's
    the id the /power endpoint accepts. AWS-lab status entries carry
    ``aws_lab_id`` (the payload has no ``id`` of its own). Systems-lab
    status wrappers use top-level ``id``. The machines *inside* a
    networks wrapper key theirs as ``systemId``. Prefer the most-specific
    / most-actionable key first.
    """
    nested = item.get("item")
    if isinstance(nested, dict):
        v = nested.get("id")
        if isinstance(v, str) and v:
            return v
    for k in ("course_network_id", "course_system_id", "aws_lab_id",
              "course_id", "system_id", "systemId", "network_id",
              "uuid", "id", "_id"):
        v = item.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def _item_name(item: Dict[str, Any]) -> str:
    for k in ("name", "title", "label", "slug"):
        v = item.get(k)
        if isinstance(v, str) and v:
            return v
    # The status endpoints nest fields under a wrapper key: ``system``
    # for systems-labs, ``network`` for networks-labs; catalog entries
    # nest under ``item``.
    for wrapper in ("system", "network", "item"):
        nested = item.get(wrapper)
        if isinstance(nested, dict):
            for k in ("name", "title"):
                v = nested.get(k)
                if isinstance(v, str) and v:
                    return v
    return ""


def _normalize(s: str) -> str:
    """Lowercase and strip anything that isn't a letter or digit.

    Lets 'nova forge' match 'NovaForge', 'sql-basics' match 'SQL Basics',
    'sysadmins' match 'SysAdmins', etc. — the way humans actually type
    lab names.
    """
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


# Lab names are "<category>: <name> (<difficulty>)". Nobody types either
# affix, so we strip both to get the bare name people actually use.
_CATEGORY_PREFIX_RE = re.compile(r"^[^:]{1,30}:\s*")
_DIFFICULTY_SUFFIX_RE = re.compile(r"\s*\([^()]*\)\s*$")


def _core_name(name: str) -> str:
    """Bare lab name: 'Challenge Lab: Odyssey (Hard)' -> 'odyssey'.

    Used as a tie-break before declaring a substring match ambiguous.
    'odyssey' is *contained in* both 'Challenge Lab: Odyssey (Hard)' and
    'Hack With Me: Active Directory (Odyssey x Triathlon)', but it *is* the
    core name of only the first — which is plainly the one meant.
    """
    s = _CATEGORY_PREFIX_RE.sub("", name or "", count=1)
    s = _DIFFICULTY_SUFFIX_RE.sub("", s)
    return _normalize(s)


def resolve_from_list(
    identifier: str, items: List[Dict[str, Any]]
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Resolve ``identifier`` against a list of items.

    Match precedence: exact UUID > exact (normalized) name > exact core
    name (category prefix and difficulty suffix stripped) > unique
    substring match on the normalized name. Normalization drops spaces
    and punctuation so 'nova forge' finds 'NovaForge'. Returns
    ``(id, item)`` or raises ``LookupError`` on ambiguity / no match.
    """
    if not identifier:
        raise LookupError("empty identifier")
    if is_uuid(identifier):
        for it in items:
            if _item_id(it) == identifier:
                return identifier, it
        return identifier, None  # unknown to us but caller can still use it

    ident_n = _normalize(identifier)
    if not ident_n:
        raise LookupError(f"identifier '{identifier}' has no alphanumerics")

    # Cache normalized names once — we scan the list several times.
    normalized = [(it, _normalize(_item_name(it))) for it in items]

    exact = [it for it, n in normalized if n == ident_n]
    if len(exact) == 1:
        return _item_id(exact[0]) or "", exact[0]
    if len(exact) > 1:
        raise LookupError(
            f"multiple items exactly named '{identifier}' — use the UUID"
        )

    core = [it for it, _ in normalized if _core_name(_item_name(it)) == ident_n]
    if len(core) == 1:
        return _item_id(core[0]) or "", core[0]

    subs = [it for it, n in normalized if ident_n in n]
    if len(subs) == 1:
        return _item_id(subs[0]) or "", subs[0]
    if len(subs) > 1:
        names = ", ".join(sorted(_item_name(it) for it in subs)[:8])
        raise LookupError(
            f"ambiguous '{identifier}' — matches: {names}"
            + (" …" if len(subs) > 8 else "")
        )
    raise LookupError(f"no lab matching '{identifier}'")


def _is_course_item(item: Dict[str, Any]) -> bool:
    """True for real courses/labs, false for the storefront's non-lab cards.

    ``/catalog`` mixes ``course_bundle`` (the subscription tiers) and
    ``event`` ("DEFCON: Free Access") cards in with the courses; those
    belong to ``hsmcli bundles`` / ``hsmcli events``, not the lab list.
    """
    nested = item.get("item")
    if isinstance(nested, dict) and nested.get("type"):
        return str(nested["type"]) == "on_demand_course"
    return str(item.get("content_type") or "course") == "course"


def all_lab_items(api) -> List[Dict[str, Any]]:
    """Every lab the account can see, merged from both listing endpoints.

    ``/api/student/courses`` is the complete set; ``/api/student/catalog``
    only returns the current storefront cards — 41 vs 81 on a subscriber
    account — which is why labs bought outside that storefront (Odyssey,
    NorthBridge Systems, …) were missing from ``labs list`` even while
    in progress. We still merge the catalog in: it carries
    ``item.content_state``, and it would surface any course the storefront
    lists that ``/courses`` omits.
    """
    # /courses is the primary source, so let its errors surface — an expired
    # cookie should read as an auth failure, not an empty lab list. /catalog
    # is supplementary; degrade quietly if it's unavailable.
    enrolled = _extract_items(api.get_enrolled_courses())
    try:
        catalog = [it for it in _extract_items(api.get_catalog())
                   if _is_course_item(it)]
    except Exception:
        catalog = []

    merged: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for it in enrolled + catalog:
        key = _item_id(it) or _normalize(_item_name(it))
        if not key:
            continue
        if key not in merged:
            merged[key] = dict(it)
            order.append(key)
        else:
            # Same course from the other endpoint: fill gaps only, so
            # whichever source came first keeps its own field values.
            for k, v in it.items():
                merged[key].setdefault(k, v)
    return [merged[k] for k in order]


def resolve_course_id(api, identifier: str) -> str:
    """Resolve a course/lab identifier against every lab the account sees.

    Resolves against the merged list rather than trying enrolled and then
    falling back to the catalog: that fallback swallowed the ambiguity
    error from the complete list and re-resolved against the partial one,
    so 'odyssey' silently landed on 'Hack With Me: Active Directory
    (Odyssey x Triathlon)' — the only Odyssey the catalog knows.
    """
    if is_uuid(identifier):
        return identifier
    cid, _ = resolve_from_list(identifier, all_lab_items(api))
    return cid


def catalog_item_id(item: Optional[Dict[str, Any]]) -> Optional[str]:
    """The id ``/api/student/catalog/{id}/buy`` takes, or ``None``.

    Enrolling goes through the catalog, and the catalog keys labs by a
    *card* id rather than the course id every other endpoint uses:
    ``/courses`` entries carry it as ``catalog_item_id``; ``/catalog``
    entries *are* the card, so it's their top-level ``id`` (the course id
    is the nested ``item.id``).
    """
    if not isinstance(item, dict):
        return None
    v = item.get("catalog_item_id")
    if isinstance(v, str) and v:
        return v
    nested = item.get("item")
    if isinstance(nested, dict) and nested.get("id"):
        v = item.get("id")
        if isinstance(v, str) and v:
            return v
    return None


def free_purchase_option_id(item: Optional[Dict[str, Any]]) -> Optional[str]:
    """The id of a lab's *free* purchase option, or ``None``.

    Some labs — the free challenge labs like Mapper — refuse a null
    ``purchase_option_id`` with "A purchase option must be selected": you
    have to name the option even when it costs nothing. Catalog cards carry
    the choices under ``purchase_options[]`` as ``{"id", "type"}``; this
    picks the ``"free"`` one. ``None`` means there's no free option to pick
    (a paid-only lab, or a card that didn't list any), in which case enroll
    falls back to the null-option request that subscription-covered labs
    accept.
    """
    if not isinstance(item, dict):
        return None
    for opt in (item.get("purchase_options") or []):
        if isinstance(opt, dict) and opt.get("type") == "free" and opt.get("id"):
            return str(opt["id"])
    return None


def resolve_course_item(
    api, identifier: str
) -> Tuple[str, str, Optional[Dict[str, Any]]]:
    """``(course_id, name, matched item)`` — same resolution as
    :func:`resolve_course`, handing back the whole item for callers that
    need a field off it (``enroll`` needs its ``catalog_item_id``).

    Unlike :func:`resolve_course` a bare UUID is *not* a shortcut here: we
    still list, because the id the caller wants may not be the one typed.
    """
    cid, item = resolve_from_list(identifier, all_lab_items(api))
    return cid, (_item_name(item) if item else ""), item


def resolve_course(api, identifier: str) -> Tuple[str, str]:
    """``(course_id, lab name)`` — the name is ``""`` for a bare UUID.

    Same resolution as :func:`resolve_course_id`, but it hands back the
    matched item's name so a command can say "Launching Dark" instead of
    "Launching d500ab4b-f68b-4ac8-ace3-ef7a1307d9c2". A UUID resolves
    without a listing call, so there's no name to hand back; callers that
    want one fall back to ``api.course_name()``.
    """
    if is_uuid(identifier):
        return identifier, ""
    cid, item = resolve_from_list(identifier, all_lab_items(api))
    return cid, _item_name(item or {})


def resolve_system_id(api, course_id: str, identifier: str) -> str:
    if is_uuid(identifier):
        return identifier
    systems = _extract_items(api.get_lab_systems(course_id))
    sid, _ = resolve_from_list(identifier, systems)
    return sid
