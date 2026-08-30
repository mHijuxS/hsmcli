"""HackSmarter HTTP client.

Auth: HackSmarter fronts a Supabase session. The browser stores it in cookies
named ``sb-auth-auth-token.0`` and ``sb-auth-auth-token.1`` (a base64-encoded
JSON blob split across two cookies because it exceeds the 4 KiB per-cookie
budget). We accept either the raw ``Cookie:`` header pasted from devtools or
just the ``sb-auth-auth-token.0``/``.1`` pair — everything else (``_ga`` and
friends) is optional analytics noise.
"""

import base64
import ipaddress
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests


# ── errors ────────────────────────────────────────────────────────────────
# Everything below subclasses Exception, so the CLI's catch-all handlers and
# the `except Exception` fallbacks in resolvers keep working unchanged. The
# point is that a caller importing this module can now tell an expired
# cookie from a 404 without matching on message text.

class HsmcliError(Exception):
    """Base class for every error this client raises."""


class HttpError(HsmcliError):
    """A failed HTTP call, with the response kept for the caller.

    ``str(e)`` stays technical — endpoint, status, response body — because
    that's what belongs in a ``--debug`` trace or a bug report. The CLI
    doesn't print it verbatim: it reads ``status``/``endpoint``/``body``
    off the exception and writes the plain-language version (see
    ``cli._explain_error``).
    """

    def __init__(self, message: str, status: Optional[int] = None,
                 endpoint: Optional[str] = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.endpoint = endpoint
        self.body = body

    def server_message(self) -> str:
        """The API's own explanation, when it gave one worth repeating.

        HackSmarter answers most failures with ``{"error": "forbidden",
        "message": "Forbidden"}`` — an echo of the status code that tells
        the reader nothing. Those are dropped; a real sentence ("System is
        already running") is kept.
        """
        text = (self.body or "").strip()
        if not text:
            return ""
        try:
            payload = json.loads(text)
        except ValueError:
            # Not JSON — an HTML error page or a proxy banner. Only worth
            # repeating if it's short enough to be a sentence.
            if text.startswith("<") or len(text) > 200:
                return ""
            return text
        if not isinstance(payload, dict):
            return ""
        for key in ("message", "error", "detail"):
            v = payload.get(key)
            if not isinstance(v, str) or not v.strip():
                continue
            v = v.strip()
            # Drop restatements of the status line ("Forbidden", "forbidden",
            # "Bad Request") — they read as detail without being any.
            if v.lower().replace(" ", "_") in _STATUS_ECHOES:
                continue
            return v
        return ""


_STATUS_ECHOES = {
    "forbidden", "unauthorized", "bad_request", "not_found",
    "internal_server_error", "error", "server_error", "conflict",
}


class AuthError(HttpError):
    """401 — the session cookie is missing, expired, or rejected."""


class ForbiddenError(HttpError):
    """403 — usually "not enrolled yet" rather than a real permission issue."""


class NotEnrolledError(HsmcliError):
    """No playthrough for this course yet, so lifecycle ids don't exist."""


class APIError(HttpError):
    """A non-2xx response that isn't 401/403."""


class TransportError(HsmcliError):
    """The request never completed — DNS, TLS, timeout, connection reset."""


AUTH_COOKIE_BASE = "sb-auth-auth-token"
COOKIE_DOMAIN = ".hacksmarter.org"

# Identify ourselves honestly. This started life as a Firefox string on the
# assumption the edge filtered unknown clients; it doesn't — /profile,
# /catalog, /courses, /courses/{id}, /take, /subscriptions, /exams/owned and
# POST /heartbeat all answer identically with no UA, with
# "python-requests/…", and with the string below. The server's actual
# same-origin check is the Referer header, which _power_call sets.
#
# BROWSER_USER_AGENT is kept as a documented fallback: if HackSmarter ever
# does start filtering, `HSMCLI_USER_AGENT="$(…)"` is a one-line fix rather
# than a patch.
BROWSER_USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64; rv:128.0) "
                      "Gecko/20100101 Firefox/128.0")


def client_version() -> str:
    try:
        from importlib.metadata import version
        return version("hsmcli")
    except Exception:  # not installed (running from a checkout)
        return "dev"


DEFAULT_USER_AGENT = (f"hsmcli/{client_version()} "
                      f"(+https://github.com/mHijuxS/hsmcli)")

# Lab/course thumbnails live on a separate CloudFront-backed CDN, keyed by
# the course's `image_path` field — public, no auth/cookies required.
IMAGE_BASE_URL = "https://images.coursestack.com"

# The actions the AWS-lab /power endpoint accepts (the server validates this
# enum and rejects anything else with a 400).
AWS_LAB_ACTIONS = ("start", "stop", "reset", "extend")

# Fallback egress-IP lookups for an AWS lab's ``allowed_ip`` input. A stopped
# lab's status payload already carries ``suggested_ip`` (the server sees our
# address), so these are only needed when that field is absent.
PUBLIC_IP_SERVICES = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
)


def detect_public_ip(timeout: float = 5.0) -> Optional[str]:
    """Best-effort public egress IP, or ``None`` if every service fails.

    Note this is *our* egress as seen from the open internet — if a lab VPN
    is up it may differ from what HackSmarter sees. Prefer the API's own
    ``suggested_ip`` when available.
    """
    for url in PUBLIC_IP_SERVICES:
        try:
            r = requests.get(url, timeout=timeout)
            r.raise_for_status()
            ip = r.text.strip()
            # Validate properly: the old character-class regex accepted
            # '.......', '::::::::' and '999.999.999.999', any of which would
            # be sent on to the API as an AWS lab's allowed_ip.
            ipaddress.ip_address(ip)
            return ip
        except Exception:
            continue
    return None


def _take_course(take_payload: Any) -> Dict[str, Any]:
    """The ``course`` object inside a /take payload, or ``{}``.

    /take is normally ``{"course": {...}, "static_aws_labs": [...]}``, but
    an error or partial response can carry ``{"course": null}`` — and a bare
    ``.get("course", payload)`` hands that None straight to the caller. That
    crashed ``extract_lessons`` and cascaded through ``extract_aws_labs``
    into every lifecycle command, so normalize it here.
    """
    if not isinstance(take_payload, dict):
        return {}
    body = take_payload.get("course", take_payload)
    return body if isinstance(body, dict) else {}


def parse_cookie_header(raw: str) -> Dict[str, str]:
    """Parse a browser ``Cookie:`` header into ``{name: value}``.

    Tolerates a leading ``Cookie:`` prefix and returns an empty dict if the
    input does not look like a cookie header (e.g. a single bare token).
    """
    raw = raw.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()

    cookies: Dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            if part:
                return {}
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        if not name or not re.fullmatch(r"[A-Za-z0-9_.\-]+", name):
            return {}
        cookies[name] = value.strip()
    return cookies


def decode_supabase_session(cookies: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """Reassemble and decode the ``sb-auth-auth-token.N`` cookies.

    Supabase writes the session as URL-safe base64 (``-``/``_`` instead of
    ``+``/``/``) with padding stripped, prefixed with ``base64-`` on the
    first chunk only, and split across multiple cookies when it exceeds the
    4 KiB per-cookie budget. Returns ``None`` on any decode failure so
    callers can degrade gracefully.
    """
    parts: List[str] = []
    idx = 0
    while True:
        name = f"{AUTH_COOKIE_BASE}.{idx}"
        if name not in cookies:
            break
        parts.append(cookies[name])
        idx += 1
    if not parts:
        return None
    blob = "".join(parts)
    if blob.startswith("base64-"):
        blob = blob[len("base64-"):]
    pad = "=" * (-len(blob) % 4)
    try:
        decoded = base64.urlsafe_b64decode(blob + pad).decode("utf-8")
        return json.loads(decoded)
    except Exception:
        return None


class HackSmarterAPI:
    """Client for the HackSmarter student API (``www.hacksmarter.org``)."""

    def __init__(self, config, debug: bool = False):
        self.config = config
        self.debug = debug
        self.base_url = config.get_base_url()
        self.session = requests.Session()

        self.session.headers.update({
            # See DEFAULT_USER_AGENT: we name ourselves rather than pose as a
            # browser. HSMCLI_USER_AGENT overrides it.
            "User-Agent": os.getenv("HSMCLI_USER_AGENT") or DEFAULT_USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.5",
            "Origin": self.base_url,
            "Referer": self.base_url + "/",
        })

        self._session_data: Optional[Dict[str, Any]] = None
        self._take_cache: Dict[str, Any] = {}

        cookie = self.config.get_cookie()
        if cookie:
            parsed = parse_cookie_header(cookie)
            if parsed:
                for name, value in parsed.items():
                    self.session.cookies.set(name, value, domain=COOKIE_DOMAIN)
                self._session_data = decode_supabase_session(parsed)

    # ── low-level ─────────────────────────────────────────────────────────
    def _trace(self, method: str, endpoint: str, response: Any,
               payload: Any = None, body: Optional[str] = None) -> None:
        """Print one request/response to stderr when ``--debug`` is on.

        stderr, not stdout, so a debug run can still be piped: ``--debug
        --json`` used to interleave the trace into the JSON. And this
        returns instead of exiting, so a command that makes several calls
        traces all of them — ``labs list`` reads two endpoints and
        ``lab info`` reads three, and the old ``sys.exit(0)`` after the
        first one hid the rest.
        """
        if not self.debug:
            return
        status = getattr(response, "status_code", "?")
        print(f"── {method} {endpoint} → {status}", file=sys.stderr)
        if body is not None:
            print(body, file=sys.stderr)
        else:
            print(json.dumps(payload, indent=2, ensure_ascii=False,
                             default=str), file=sys.stderr)

    def _request(
        self,
        method: str,
        endpoint: str,
        data: Optional[Any] = None,
        params: Optional[Dict[str, Any]] = None,
        raw: bool = False,
        stream: bool = False,
    ) -> Any:
        """Send a request and return parsed JSON (or raw ``Response`` when
        ``raw=True`` — used by VPN download which returns a config file, not
        JSON).
        """
        url = urljoin(self.base_url + "/", endpoint.lstrip("/"))
        method_up = method.upper()
        try:
            if method_up == "GET":
                r = self.session.get(url, params=params, stream=stream)
            elif method_up == "POST":
                if data is None:
                    r = self.session.post(url, params=params, data=b"")
                else:
                    r = self.session.post(url, json=data, params=params)
            elif method_up == "PUT":
                r = self.session.put(url, json=data, params=params)
            elif method_up == "DELETE":
                r = self.session.delete(url, params=params)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")
            r.raise_for_status()
            if raw:
                self._trace(method_up, endpoint, r, body="<raw response>")
                return r
            if not r.content:
                self._trace(method_up, endpoint, r, payload={})
                return {}
            try:
                out = r.json()
            except ValueError:
                self._trace(method_up, endpoint, r, body=r.text)
                return {"raw": r.text}
            self._trace(method_up, endpoint, r, payload=out)
            return out
        except requests.exceptions.HTTPError as e:
            self._raise_http_error(e, method_up, endpoint)
        except requests.exceptions.RequestException as e:
            raise TransportError(f"Request failed: {e}")

    def _raise_http_error(self, e: "requests.exceptions.HTTPError",
                          method_up: str, endpoint: str) -> None:
        """Translate an HTTPError into a typed error carrying the body.

        Shared with ``_power_call``, which used to let requests' own
        exception through — so a rejected launch reported only
        "400 Client Error: Bad Request for url: …" and dropped the one part
        that says *why* ("System is already running", and friends).
        """
        code = e.response.status_code if e.response is not None else "?"
        body = e.response.text[:400] if e.response is not None else ""
        status = code if isinstance(code, int) else None
        if code == 401:
            raise AuthError(
                f"HTTP 401 on {method_up} {endpoint}: {body}. "
                f"Cookie may be expired — hsmcli config set-cookie "
                f"'<paste cookie header>' or export HSMCLI_COOKIE.",
                status=status, endpoint=endpoint, body=body,
            )
        if code == 403:
            # The API returns a bare {"error":"forbidden"} for both
            # "not enrolled yet" and genuine permission issues — the
            # former is by far the common case (owned/free labs still
            # need an explicit /enroll before /take or system-status
            # endpoints serve data), so point at it directly.
            m = re.search(r"/courses/([0-9a-fA-F-]{36})", endpoint)
            hint = f" Not enrolled? Try: hsmcli lab {m.group(1)} enroll" if m else ""
            raise ForbiddenError(
                f"HTTP 403 (forbidden) on {method_up} {endpoint}: {body}.{hint}",
                status=status, endpoint=endpoint, body=body,
            )
        raise APIError(f"HTTP {code} on {method_up} {endpoint}: {body}",
                       status=status, endpoint=endpoint, body=body)

    # ── session ───────────────────────────────────────────────────────────
    def session_summary(self) -> Optional[Dict[str, Any]]:
        """Decoded Supabase session (email, id, expires_at, provider) or None."""
        if not self._session_data:
            return None
        user = self._session_data.get("user", {}) or {}
        meta = user.get("user_metadata", {}) or {}
        return {
            "email": user.get("email"),
            "id": user.get("id"),
            "username": meta.get("preferred_username") or meta.get("user_name"),
            "provider": (user.get("app_metadata", {}) or {}).get("provider"),
            "expires_at": self._session_data.get("expires_at"),
        }

    # ── profile / meta ────────────────────────────────────────────────────
    def get_profile(self) -> Dict[str, Any]:
        return self._request("GET", "/api/student/profile")

    def get_orgs(self) -> Any:
        return self._request("GET", "/api/student/orgs")

    def get_subscriptions(self) -> Any:
        return self._request("GET", "/api/student/subscriptions")

    def get_notifications(self) -> Any:
        return self._request("GET", "/api/student/notifications")

    def get_events(self) -> Any:
        return self._request("GET", "/api/student/events")

    def get_bundles(self) -> Any:
        return self._request("GET", "/api/student/bundles")

    # ── catalog / labs ────────────────────────────────────────────────────
    def get_catalog(self) -> Any:
        """Full catalog of available courses/labs."""
        return self._request("GET", "/api/student/catalog")

    def get_enrolled_courses(self) -> Any:
        """Courses the current student is enrolled in."""
        return self._request("GET", "/api/student/courses")

    def get_course(self, course_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/api/student/courses/{course_id}")

    @staticmethod
    def image_url(image_path: str) -> str:
        """Build the public thumbnail URL for a course's ``image_path``."""
        return f"{IMAGE_BASE_URL}/{image_path.lstrip('/')}"

    def download_lab_image(self, image_path: str, dest_path: Optional[str] = None) -> bytes:
        """Fetch a lab's thumbnail from the images CDN.

        Public CloudFront/S3 asset — no auth, and not under ``base_url``,
        so this bypasses ``_request`` and hits ``IMAGE_BASE_URL`` directly.
        Returns the raw bytes; writes them to ``dest_path`` when provided.
        """
        url = self.image_url(image_path)
        r = requests.get(url, stream=True)
        r.raise_for_status()
        content = r.content
        if dest_path:
            with open(dest_path, "wb") as f:
                f.write(content)
        return content

    def get_course_take(self, course_id: str, use_cache: bool = False) -> Dict[str, Any]:
        """Full lesson payload — content items, playthrough and lab ids.

        ``use_cache`` memoizes the response for the life of the process.
        A single command can need /take several times (``lab info`` renders
        the briefing *and* looks up system status; ``launch --wait`` polls),
        and the parts those callers use — ids, lesson content — don't change
        mid-run. Callers that read mutable state (question results) leave it
        off and always hit the network.
        """
        if use_cache and course_id in self._take_cache:
            return self._take_cache[course_id]
        data = self._request("GET", f"/api/student/courses/{course_id}/take")
        self._take_cache[course_id] = data
        return data

    def course_name(self, course_id: str) -> str:
        """A lab's display name, or ``""`` if we can't get one.

        Reads the cached /take payload, so it's free for any command that
        already touched the lifecycle endpoints. Purely cosmetic — a lab we
        can't name is still perfectly operable by id — so every failure
        (not enrolled, network down) degrades to the empty string rather
        than sinking the command that wanted a nice header.
        """
        try:
            take = self.get_course_take(course_id, use_cache=True)
        except Exception:
            return ""
        return str(_take_course(take).get("name") or "")

    def enroll_course(self, catalog_item_id: str,
                      purchase_option_id: Optional[str] = None,
                      promo_code: Optional[str] = None) -> Dict[str, Any]:
        """Enroll in a lab — claim it, or start paying for it.

        There is no ``/courses/{id}/enroll``: it 404s for every lab, always
        did, and the API has no such route. The web app enrolls by "buying"
        the lab's *catalog card*, with a null purchase option, and lets the
        server work out what that means::

            POST /api/student/catalog/{catalog_item_id}/buy
            {"purchase_option_id": null, "promo_code": null,
             "pwyc_price_cents": null}

        Free labs and the ones your subscription or a bundle already covers
        come back ``{"state": "bought", "redirect_url": …}`` — nothing is
        charged. A lab you don't have access to comes back
        ``{"state": "checkout", "session_url": …}``: a Stripe link, and
        nothing happens to your account (or your card) until you follow it.

        Note the id: this is the *catalog* id — ``catalog_item_id`` on a
        /courses entry — not the course id the lifecycle endpoints take.
        """
        return self._request(
            "POST", f"/api/student/catalog/{catalog_item_id}/buy",
            data={"purchase_option_id": purchase_option_id,
                  "promo_code": promo_code,
                  "pwyc_price_cents": None},
        )

    # ── lab lifecycle (systems + networks + aws) ──────────────────────────

    def _ensure_playthrough(self, course_id: str) -> Dict[str, Any]:
        """Fetch /take and pull the ids the lifecycle endpoints need.

        HackSmarter has THREE lab shapes:
          * "systems" labs (single VM, e.g. Implicit) — use ``/systems/{id}/*``
            and ``courseSystemIds=[…]`` for status.
          * "networks" labs (multi-VM subnet, e.g. NovaForge) — use
            ``/networks/{id}/*`` and ``content_network_ids=[…]``.
          * "aws" labs (a terraform-provisioned AWS account, e.g. Second) —
            no VM at all; use ``/content/{playthrough}/aws-labs/{id}`` and
            you get IAM keys back instead of an IP.

        The public ``course.id`` is *not* the id used for lifecycle ops;
        those use ``course.course_playthrough.id`` (a handle created on
        enroll). The heartbeat also needs a lesson id.

        Returns a dict with ``playthrough_id``, ``lesson_id``, ``course_id``,
        ``customer_id``, ``system_ids``, ``network_ids``, ``aws_labs``, and
        ``kind`` (``"systems"`` | ``"networks"`` | ``"aws"`` | ``"none"``).
        """
        take = self.get_course_take(course_id, use_cache=True)
        body = take.get("course", take) if isinstance(take, dict) else {}
        playthrough = body.get("course_playthrough") if isinstance(body, dict) else None
        playthrough_id = (playthrough or {}).get("id")
        lesson_id = None
        chapters = body.get("chapters") if isinstance(body, dict) else None
        if isinstance(chapters, list):
            for ch in chapters:
                for les in (ch.get("lessons") or []):
                    if isinstance(les.get("id"), str):
                        lesson_id = les["id"]; break
                if lesson_id:
                    break
        system_ids = self.extract_system_ids(take)
        network_ids = self.extract_network_ids(take)
        aws_labs = self.extract_aws_labs(take)
        # Prefer networks when the lab has any — a lab may nominally have
        # both but the browser drives it via /networks in that case. AWS
        # labs are last: they never coexist with VMs in practice, so this
        # only decides the fallback when there's nothing to power on.
        kind = (
            "networks" if network_ids
            else "systems" if system_ids
            else "aws" if aws_labs
            else "none"
        )
        return {
            "playthrough_id": playthrough_id,
            "lesson_id": lesson_id,
            # A finished course carries these on its playthrough: the server
            # flips `is_complete` once every lesson (and scored question) is
            # done, and mints a `completion_id` — the public certificate
            # handle the /completion page and the certificate download take.
            "is_complete": bool((playthrough or {}).get("is_complete")),
            "completion_id": (playthrough or {}).get("completion_id"),
            "course_id": body.get("id") if isinstance(body, dict) else course_id,
            "customer_id": body.get("customer_id") if isinstance(body, dict) else None,
            "system_ids": system_ids,
            "network_ids": network_ids,
            "aws_labs": aws_labs,
            "kind": kind,
            "take": take,
        }

    def lab_kind(self, course_id: str) -> str:
        """``"systems"`` | ``"networks"`` | ``"aws"`` | ``"none"`` for a lab."""
        return self._ensure_playthrough(course_id)["kind"]

    def get_lab_systems(
        self, course_id: str, system_ids: Optional[List[str]] = None
    ) -> Any:
        """Lab live status. Dispatches on lab kind:

        * systems-lab: ``GET /systems?courseSystemIds=[…]``
        * networks-lab: ``GET /networks?content_network_ids=[…]``

        Callers that pass ``system_ids`` explicitly override auto-discovery
        (used by /launch --wait polling for a specific target).
        """
        pt = self._ensure_playthrough(course_id)
        playthrough_id = pt["playthrough_id"]
        if not playthrough_id:
            return {"data": [], "note": "no playthrough (enroll first)"}

        if pt["kind"] == "networks":
            ids = system_ids if system_ids is not None else pt["network_ids"]
            if not ids:
                return {"data": [], "note": "lab has no networks"}
            return self._request(
                "GET",
                f"/api/student/courses/{playthrough_id}/networks",
                params={"content_network_ids": json.dumps(ids)},
            )

        # Default: systems-lab (also covers "none" — endpoint 404s cleanly).
        ids = system_ids if system_ids is not None else pt["system_ids"]
        if not ids:
            return {"data": [], "note": "lab has no systems"}
        return self._request(
            "GET",
            f"/api/student/courses/{playthrough_id}/systems",
            params={"courseSystemIds": json.dumps(ids)},
        )

    def launch_system(self, course_id: str, system_id: str) -> Dict[str, Any]:
        """Start a lab machine / network.

        systems-lab: two-step ``POST /systems/{id}/launch`` (provision) then
        ``POST /systems/{id}/power`` with ``{"power":"on"}`` (actually
        boots it — the browser's "Power → Start" hits /power).
        networks-lab: single ``POST /networks/{id}/power`` with
        ``{"power":"on"}``.

        We send an explicit ``Referer: /courses/{course_id}/take`` because
        the server checks it on the power call.
        """
        pt = self._ensure_playthrough(course_id)
        playthrough_id = pt["playthrough_id"] or course_id
        real_course_id = pt["course_id"] or course_id
        take_referer = f"{self.base_url}/courses/{real_course_id}/take"
        resource = "networks" if pt["kind"] == "networks" else "systems"
        base = f"/api/student/courses/{playthrough_id}/{resource}/{system_id}"

        # Provision step exists only for systems-labs; networks-labs go
        # straight to /power.
        provisioned = False
        if resource == "systems":
            try:
                self._power_call("POST", base + "/launch", None, take_referer)
                provisioned = True
            except Exception:
                pass  # already provisioned, or the lab doesn't need this step
        try:
            return self._power_call(
                "POST", base + "/power", {"power": "on"}, take_referer,
            )
        except APIError as e:
            # A system that just accepted /launch is already coming up, and
            # the server rejects /power until the instance exists. That's a
            # race, not a failed launch — say so and let the caller poll,
            # instead of exiting non-zero on a machine that is booting.
            if provisioned and isinstance(e.status, int) and 400 <= e.status < 500:
                return {"success": True, "provisioning": True,
                        "message": f"provisioning; power-on deferred ({e})"}
            raise

    def power_off_system(self, course_id: str, system_id: str) -> Dict[str, Any]:
        pt = self._ensure_playthrough(course_id)
        playthrough_id = pt["playthrough_id"] or course_id
        real_course_id = pt["course_id"] or course_id
        take_referer = f"{self.base_url}/courses/{real_course_id}/take"
        resource = "networks" if pt["kind"] == "networks" else "systems"
        return self._power_call(
            "POST",
            f"/api/student/courses/{playthrough_id}/{resource}/{system_id}/power",
            {"power": "off"},
            take_referer,
        )

    def reset_system(self, course_id: str, system_id: str) -> Dict[str, Any]:
        """Reboot the system/network (``POST .../reset`` with body ``{}``)."""
        pt = self._ensure_playthrough(course_id)
        playthrough_id = pt["playthrough_id"] or course_id
        real_course_id = pt["course_id"] or course_id
        take_referer = f"{self.base_url}/courses/{real_course_id}/take"
        resource = "networks" if pt["kind"] == "networks" else "systems"
        return self._power_call(
            "POST",
            f"/api/student/courses/{playthrough_id}/{resource}/{system_id}/reset",
            {},
            take_referer,
        )

    def _power_call(
        self, method: str, path: str, body: Optional[Dict[str, Any]],
        referer: str,
    ) -> Dict[str, Any]:
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        headers = {"Referer": referer}
        try:
            if body is None:
                r = self.session.request(method, url, data=b"", headers=headers)
            else:
                r = self.session.request(method, url, json=body, headers=headers)
            r.raise_for_status()
        except requests.exceptions.HTTPError as e:
            self._raise_http_error(e, method.upper(), path)
        except requests.exceptions.RequestException as e:
            raise TransportError(f"Request failed: {e}")
        # Power/submit calls bypass _request (they need a per-call Referer),
        # so trace them here too or --debug would miss every lifecycle op.
        if not r.content:
            self._trace(method.upper(), path, r, payload={"success": True})
            return {"success": True}
        try:
            out = r.json()
        except ValueError:
            self._trace(method.upper(), path, r, body=r.text)
            return {"raw": r.text}
        self._trace(method.upper(), path, r, payload=out)
        return out

    @staticmethod
    def extract_system_ids(take_payload: Any) -> List[str]:
        """Best-effort scrape of every system UUID from a /take response.

        Systems live in two places in the take payload:
        ``course.static_systems[].id`` and
        ``course.chapters[].lessons[].content.items[].system_id``. We walk
        the whole tree to be schema-agnostic.
        """
        ids: List[str] = []
        seen: set = set()

        def add(v):
            if isinstance(v, str) and v and v not in seen:
                seen.add(v); ids.append(v)

        def walk(node, key_hint=""):
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, k)
                # A record under a "system"-ish key contributes its id.
                if "system" in key_hint.lower() and isinstance(node.get("id"), str):
                    add(node["id"])
                add(node.get("system_id"))
                add(node.get("systemId"))
            elif isinstance(node, list):
                for x in node:
                    walk(x, key_hint)

        walk(take_payload)
        return ids

    @staticmethod
    def extract_network_ids(take_payload: Any) -> List[str]:
        """Scrape every network UUID from a /take response.

        Networks live at ``course.course_networks[].id`` and
        ``course.chapters[].lessons[].content.items[].network_id``.
        """
        ids: List[str] = []
        seen: set = set()

        def add(v):
            if isinstance(v, str) and v and v not in seen:
                seen.add(v); ids.append(v)

        def walk(node):
            if isinstance(node, dict):
                for v in node.values():
                    walk(v)
                add(node.get("network_id"))
                add(node.get("networkId"))
            elif isinstance(node, list):
                for x in node:
                    walk(x)

        # Only pull course_networks[].id — walking the whole tree would
        # grab unrelated ids that happen to sit under a "network" key.
        body = take_payload.get("course", take_payload) if isinstance(take_payload, dict) else {}
        cn = body.get("course_networks") if isinstance(body, dict) else None
        if isinstance(cn, list):
            for n in cn:
                if isinstance(n, dict):
                    add(n.get("id"))
        walk(take_payload)
        return ids

    # ── AWS labs ──────────────────────────────────────────────────────────

    @staticmethod
    def extract_aws_labs(take_payload: Any) -> List[Dict[str, Any]]:
        """Enumerate the AWS labs a /take payload references, as
        ``[{"id": …, "name": …}]``.

        Names live in ``static_aws_labs[]``, which sits *beside* ``course``
        at the top level of /take rather than inside it. The lesson body
        points at the same labs via ``content.items[]`` entries of type
        ``aws-lab`` carrying ``aws_lab_id`` — we merge both sources so a
        lab still surfaces (nameless) if only one half is present.
        """
        names: Dict[str, str] = {}
        order: List[str] = []

        def add(lab_id: Any, name: Any = None):
            if not isinstance(lab_id, str) or not lab_id:
                return
            if lab_id not in names:
                order.append(lab_id)
                names[lab_id] = ""
            if isinstance(name, str) and name and not names[lab_id]:
                names[lab_id] = name

        if isinstance(take_payload, dict):
            body = take_payload.get("course", take_payload)
            for source in (take_payload, body):
                if not isinstance(source, dict):
                    continue
                for entry in (source.get("static_aws_labs") or []):
                    if isinstance(entry, dict):
                        add(entry.get("id"), entry.get("name"))

        for les in HackSmarterAPI.extract_lessons(take_payload):
            for it in les["items"]:
                if it.get("aws_lab_id") or str(it.get("type") or "") == "aws-lab":
                    add(it.get("aws_lab_id"), it.get("name"))

        return [{"id": i, "name": names[i]} for i in order]

    def _aws_lab_base(self, course_id: str) -> str:
        """``/api/student/content/{playthrough}/aws-labs`` for a course.

        Despite the ``/content/`` segment, that first id is the *playthrough*
        id (``course.course_playthrough.id``) — the same handle the
        ``/courses/{…}/systems`` endpoints take. Passing a lesson's
        ``content.id`` here returns 403.
        """
        pt = self._ensure_playthrough(course_id)
        playthrough_id = pt["playthrough_id"]
        if not playthrough_id:
            raise NotEnrolledError("no active playthrough — enroll first")
        return f"/api/student/content/{playthrough_id}/aws-labs"

    def get_aws_lab(self, course_id: str, aws_lab_id: str) -> Dict[str, Any]:
        """Live status of one AWS lab.

        Shape: ``{state, access_mode, name, expires_at, allow_extend,
        time_limit_minutes, terraform_outputs, error_message,
        student_inputs, suggested_ip}``. ``state`` is ``"na"`` before the
        first start and ``"ready"`` once terraform has applied;
        ``terraform_outputs`` (the IAM keys) only appears when ready, and
        ``suggested_ip`` only while it is not.
        """
        return self._request("GET", f"{self._aws_lab_base(course_id)}/{aws_lab_id}")

    def get_aws_labs(self, course_id: str) -> List[Dict[str, Any]]:
        """Status of every AWS lab in a course (one GET each).

        Each entry is the raw status payload plus ``aws_lab_id``; a lab
        whose status call fails degrades to ``state: "unknown"`` with the
        error in ``error_message`` rather than sinking the whole listing.
        """
        pt = self._ensure_playthrough(course_id)
        out: List[Dict[str, Any]] = []
        for entry in pt["aws_labs"]:
            lab_id = entry.get("id")
            if not lab_id:
                continue
            try:
                status = self.get_aws_lab(course_id, lab_id)
            except Exception as e:
                status = {"state": "unknown", "error_message": str(e)}
            if not isinstance(status, dict):
                status = {"state": "unknown", "raw": status}
            out.append({
                **status,
                "aws_lab_id": lab_id,
                "name": status.get("name") or entry.get("name") or "",
            })
        return out

    def aws_lab_power(
        self,
        course_id: str,
        aws_lab_id: str,
        action: str,
        inputs: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Drive an AWS lab — ``start`` | ``stop`` | ``reset`` | ``extend``.

        ``POST .../aws-labs/{id}/power`` with ``{"action", "inputs"}``.
        ``inputs`` is a free-form ``{str: str}`` map; which keys a lab wants
        is declared in its status under ``student_inputs[].type`` — in
        practice just ``allowed_ip``, which scopes the lab's security group
        to your egress address. As with the VM endpoints the server checks
        the ``Referer`` against the course's /take page.
        """
        if action not in AWS_LAB_ACTIONS:
            raise ValueError(
                f"unsupported AWS lab action '{action}' "
                f"(expected one of {', '.join(AWS_LAB_ACTIONS)})"
            )
        pt = self._ensure_playthrough(course_id)
        real_course_id = pt["course_id"] or course_id
        take_referer = f"{self.base_url}/courses/{real_course_id}/take"
        body: Dict[str, Any] = {"action": action}
        if inputs:
            body["inputs"] = {k: str(v) for k, v in inputs.items()}
        return self._power_call(
            "POST",
            f"{self._aws_lab_base(course_id)}/{aws_lab_id}/power",
            body,
            take_referer,
        )

    # ── VPN ───────────────────────────────────────────────────────────────
    def get_vpn_config(self, course_id: str, dest_path: Optional[str] = None) -> str:
        """Download the OpenVPN config for a lab.

        Dispatches on lab kind: systems-labs use
        ``GET /courses/{playthrough}/vpn`` (course-level), networks-labs
        use ``GET /courses/{playthrough}/networks/{network}/vpn``.

        Returns the file text. Writes it to ``dest_path`` when provided.
        Handles both ``application/x-openvpn-profile`` and JSON responses.
        """
        pt = self._ensure_playthrough(course_id)
        playthrough_id = pt["playthrough_id"] or course_id
        if pt["kind"] == "networks":
            net_ids = pt["network_ids"]
            if not net_ids:
                raise HsmcliError("lab has no networks")
            # Multi-network labs would need explicit selection; for now
            # take the first — the VPN is usually shared per lab.
            path = f"/api/student/courses/{playthrough_id}/networks/{net_ids[0]}/vpn"
        else:
            path = f"/api/student/courses/{playthrough_id}/vpn"
        r = self._request("GET", path, raw=True, stream=True)
        content_type = r.headers.get("Content-Type", "")
        if "json" in content_type:
            payload = r.json()
            if isinstance(payload, dict):
                # Common shapes: {"config": "..."} or {"data": "..."}.
                for key in ("config", "data", "vpn", "content"):
                    if isinstance(payload.get(key), str):
                        text = payload[key]
                        break
                else:
                    text = json.dumps(payload, indent=2)
            else:
                text = json.dumps(payload, indent=2)
        else:
            text = r.text
        if dest_path:
            with open(dest_path, "w") as f:
                f.write(text)
        return text

    # ── content / heartbeat ───────────────────────────────────────────────
    def get_content_jupyter_files(self, content_id: str) -> Any:
        return self._request(
            "GET", f"/api/student/content/{content_id}/jupyter/files",
        )

    def submit_question(
        self,
        course_id: str,
        lesson_id: str,
        question_id: str,
        submission: str,
    ) -> Dict[str, Any]:
        """Submit a flag / free-text answer to a lesson question.

        Endpoint: ``POST /api/student/content/{playthrough}/lessons/
        {lesson_id}/submit-question`` with body ``{questionId, submission}``.
        The ``/content/`` segment takes the *playthrough* id, not the
        lesson's own ``content.id`` (that one 403s) — same convention as the
        AWS-lab endpoints. The server also checks the ``Referer`` against
        ``/courses/{course_id}/take``.

        Returns the server's verdict payload, which nests the verdict:
        ``{"result": {"is_correct": bool, "answer_text": str}}``.
        """
        pt = self._ensure_playthrough(course_id)
        playthrough_id = pt["playthrough_id"]
        if not playthrough_id:
            raise NotEnrolledError("no active playthrough — enroll first")
        real_course_id = pt["course_id"] or course_id
        take_referer = f"{self.base_url}/courses/{real_course_id}/take"
        return self._power_call(
            "POST",
            f"/api/student/content/{playthrough_id}/lessons/{lesson_id}/submit-question",
            {"questionId": question_id, "submission": submission},
            take_referer,
        )

    def complete_lesson(self, course_id: str, lesson_id: str) -> Dict[str, Any]:
        """Mark one lesson done — the "in progress → complete" toggle.

        Endpoint: ``POST /api/student/content/{playthrough}/lessons/
        {lesson_id}/complete`` with an empty body. Same shape and same
        conventions as :meth:`submit_question`: the ``/content/`` segment is
        the *playthrough* id (the lesson's own ``content.id`` 403s), and the
        server checks the ``Referer`` against ``/courses/{course_id}/take``.

        The reply carries nothing worth reading (``{}`` / ``{"success":
        true}``) — completion state is what you read back off /take
        afterwards, on ``course_playthrough`` — so callers that need to know
        whether the *course* is finished should re-fetch /take, not parse
        this.
        """
        pt = self._ensure_playthrough(course_id)
        playthrough_id = pt["playthrough_id"]
        if not playthrough_id:
            raise NotEnrolledError("no active playthrough — enroll first")
        real_course_id = pt["course_id"] or course_id
        take_referer = f"{self.base_url}/courses/{real_course_id}/take"
        return self._power_call(
            "POST",
            f"/api/student/content/{playthrough_id}/lessons/{lesson_id}/complete",
            None,
            take_referer,
        )

    def complete_course(self, course_id: str) -> Dict[str, Any]:
        """Mark every lesson in a lab complete, then report where that left it.

        A course is "complete" once all its lessons are — there is no single
        "finish the course" endpoint, so this walks the lessons and POSTs
        :meth:`complete_lesson` for each one.

        It POSTs *every* lesson, including ones /take already shows as
        ``completed``. That flag tracks progress/having-viewed a lesson and
        can be set without the completing POST ever landing, so it's the
        POST — not the flag — that flips ``course_playthrough.is_complete``.
        The call is idempotent, so re-marking a genuinely-finished lesson
        costs nothing; skipping one that only *looked* done would leave the
        course stuck in progress. The summary still distinguishes the two so
        the caller can say what changed.

        Re-reads /take afterwards — bypassing the cache, since we just
        mutated the thing it caches — so the returned ``is_complete`` and
        ``completion_id`` reflect the finished state, including the
        certificate handle the server mints on the last lesson.

        Returns ``{"completed": [lesson_id, …], "already": [lesson_id, …],
        "is_complete": bool, "completion_id": str | None}`` — ``completed``
        are the lessons that weren't showing done beforehand, ``already``
        the ones that were; both got the POST.
        """
        take = self.get_course_take(course_id)
        lessons = self.extract_lessons(take)
        if not lessons:
            raise NotEnrolledError(
                "no lessons on this lab — enroll first, or it has no content")
        newly, already = [], []
        for les in lessons:
            lid = les.get("lesson_id")
            if not lid:
                continue
            self.complete_lesson(course_id, lid)
            (already if les.get("completed") else newly).append(lid)
        # State moved under us; drop the cached /take so the playthrough we
        # read back shows the completion the last POST just triggered.
        self._take_cache.pop(course_id, None)
        pt = self._ensure_playthrough(course_id)
        return {
            "completed": newly,
            "already": already,
            "is_complete": pt.get("is_complete", False),
            "completion_id": pt.get("completion_id"),
        }

    def course_completion(self, course_id: str) -> Dict[str, Any]:
        """``{"is_complete": bool, "completion_id": str | None}`` for a lab.

        Both come off ``course_playthrough``. Crucially they are
        *independent*: the ``completion_id`` handle is minted at enroll and
        is present the whole time — it is **not** proof the lab is finished.
        ``is_complete`` is the real signal, and the certificate endpoint
        404s until it flips, so anything gating on "can I download the
        certificate?" must check ``is_complete``, not the id's presence.
        """
        pt = self._ensure_playthrough(course_id)
        return {"is_complete": pt.get("is_complete", False),
                "completion_id": pt.get("completion_id")}

    def completion_id_for_course(self, course_id: str) -> Optional[str]:
        """The public certificate handle on the playthrough, or ``None``.

        Present from enroll onward, so it does **not** mean the lab is
        finished — see :meth:`course_completion`, and gate downloads on its
        ``is_complete``, not on this being non-``None``.
        """
        return self._ensure_playthrough(course_id).get("completion_id")

    def certificate_download_url(self, completion_id: str) -> str:
        """Ask the API for a fresh, time-limited certificate download URL.

        ``GET /api/student/completion/course/{completion_id}/certificate``
        returns ``{"url": …}`` — a pre-signed S3 link to the certificate PDF
        that expires in an hour. The PDF itself sits in a private bucket, so
        this indirection is the only way to it; there's no stable URL to
        cache.
        """
        data = self._request(
            "GET",
            f"/api/student/completion/course/{completion_id}/certificate",
        )
        url = data.get("url") if isinstance(data, dict) else None
        if not url:
            raise HttpError(
                "certificate endpoint returned no download URL",
                status=200,
                endpoint=(f"/api/student/completion/course/"
                          f"{completion_id}/certificate"),
                body=json.dumps(data) if data else "",
            )
        return str(url)

    def download_certificate(
        self, completion_id: str, dest_path: Optional[str] = None
    ) -> bytes:
        """Fetch the certificate PDF bytes for a completed lab.

        Two hops: resolve the pre-signed URL via
        :meth:`certificate_download_url`, then GET the PDF straight from S3.
        That second GET is a plain request — the signed URL carries its own
        auth, it isn't under ``base_url``, and sending the session cookie to
        S3 would only confuse it — mirroring :meth:`download_lab_image`.
        Writes to ``dest_path`` when given, and always returns the bytes.
        """
        url = self.certificate_download_url(completion_id)
        r = requests.get(url, stream=True)
        r.raise_for_status()
        content = r.content
        if dest_path:
            with open(dest_path, "wb") as f:
                f.write(content)
        return content

    @staticmethod
    def extract_lessons(take_payload: Any) -> List[Dict[str, Any]]:
        """Flatten ``course.chapters[].lessons[]`` out of a /take payload.

        Each entry keeps the enclosing chapter name plus the lesson's
        ``content.items[]`` — the markdown briefing, video links, lab
        references and questions the web UI renders on the lesson page.
        Only /take carries these; ``GET /courses/{id}`` returns lessons
        as bare name/slug stubs.
        """
        body = _take_course(take_payload)
        out: List[Dict[str, Any]] = []
        for ch in (body.get("chapters") or []):
            if not isinstance(ch, dict):
                continue
            for les in (ch.get("lessons") or []):
                if not isinstance(les, dict):
                    continue
                content = les.get("content")
                content = content if isinstance(content, dict) else {}
                items = content.get("items")
                out.append({
                    "chapter": ch.get("name"),
                    "lesson": les.get("name"),
                    "lesson_id": les.get("id"),
                    "content_id": content.get("id"),
                    "completed": bool(les.get("completed")),
                    "items": [it for it in (items or []) if isinstance(it, dict)],
                })
        return out

    @staticmethod
    def extract_questions(take_payload: Any) -> List[Dict[str, Any]]:
        """Enumerate every question item in a /take payload.

        Questions live at
        ``course.chapters[].lessons[].content.items[]`` where ``type``
        starts with ``question-`` (e.g. ``question-free-text``). We yield
        the fields callers need to submit and render — ``content_id`` and
        ``lesson_id`` come from the enclosing lesson wrapper, not the item.
        """
        body = _take_course(take_payload)
        out: List[Dict[str, Any]] = []
        for ch in (body.get("chapters") or []):
            if not isinstance(ch, dict):
                continue
            for les in (ch.get("lessons") or []):
                if not isinstance(les, dict):
                    continue
                content = les.get("content") or {}
                content_id = content.get("id") if isinstance(content, dict) else None
                lesson_id = les.get("id")
                items = content.get("items") if isinstance(content, dict) else None
                if not isinstance(items, list):
                    continue
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    t = it.get("type") or ""
                    if not str(t).startswith("question"):
                        continue
                    attempt = it.get("attempt") or {}
                    result = attempt.get("result") or {}
                    out.append({
                        "content_id": content_id,
                        "lesson_id": lesson_id,
                        "chapter": ch.get("name"),
                        "lesson": les.get("name"),
                        "question_id": it.get("id"),
                        "type": t,
                        "prompt": (it.get("question") or "").strip(),
                        "match_type": it.get("match_type"),
                        "points": it.get("points"),
                        "state": it.get("state"),
                        "has_hint": bool(it.get("hasHint")),
                        "hint": it.get("hint"),
                        "last_submission": attempt.get("submission"),
                        "last_correct": result.get("correct"),
                    })
        return out

    def get_credits(self, credit_id: str) -> Any:
        return self._request("GET", f"/api/student/credits/{credit_id}")

    def heartbeat(self, payload: Optional[Dict[str, Any]] = None) -> Any:
        return self._request("POST", "/api/heartbeat", data=(payload or {}))

    def heartbeat_for_course(self, course_id: str) -> Any:
        """Send the same heartbeat the browser sends on the ``/take`` page.

        Payload shape is ``{lessonId, courseId, coursePlaythroughId}`` — the
        server rejects heartbeats missing any of these three.
        """
        pt = self._ensure_playthrough(course_id)
        if not (pt["lesson_id"] and pt["playthrough_id"]):
            raise NotEnrolledError("no active playthrough/lesson — enroll first")
        return self.heartbeat({
            "lessonId": pt["lesson_id"],
            "courseId": pt["course_id"] or course_id,
            "coursePlaythroughId": pt["playthrough_id"],
        })

    # ── exams ─────────────────────────────────────────────────────────────
    def get_owned_exams(self) -> Any:
        return self._request("GET", "/api/exams/owned")

    def view_exam(self, exam_id: Optional[str] = None) -> Any:
        params = {"id": exam_id} if exam_id else None
        return self._request("GET", "/api/exams/view", params=params)
