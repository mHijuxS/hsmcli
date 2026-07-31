"""HackSmarter HTTP client.

Auth: HackSmarter fronts a Supabase session. The browser stores it in cookies
named ``sb-auth-auth-token.0`` and ``sb-auth-auth-token.1`` (a base64-encoded
JSON blob split across two cookies because it exceeds the 4 KiB per-cookie
budget). We accept either the raw ``Cookie:`` header pasted from devtools or
just the ``sb-auth-auth-token.0``/``.1`` pair — everything else (``_ga`` and
friends) is optional analytics noise.
"""

import base64
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests


AUTH_COOKIE_BASE = "sb-auth-auth-token"
COOKIE_DOMAIN = ".hacksmarter.org"


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
            # Match a normal browser so the edge / WAF doesn't second-guess us.
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) "
                          "Gecko/20100101 Firefox/128.0",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.5",
            "Origin": self.base_url,
            "Referer": self.base_url + "/",
        })

        self._session_data: Optional[Dict[str, Any]] = None

        cookie = self.config.get_cookie()
        if cookie:
            parsed = parse_cookie_header(cookie)
            if parsed:
                for name, value in parsed.items():
                    self.session.cookies.set(name, value, domain=COOKIE_DOMAIN)
                self._session_data = decode_supabase_session(parsed)

    # ── low-level ─────────────────────────────────────────────────────────
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
                return r
            if not r.content:
                return {}
            try:
                out = r.json()
            except ValueError:
                return {"raw": r.text}
            if self.debug:
                print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
                sys.exit(0)
            return out
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            body = e.response.text[:400] if e.response is not None else ""
            if code == 401:
                raise Exception(
                    "Authentication failed (401). Cookie may be expired. "
                    "Update it with: hsmcli config set-cookie '<paste cookie header>' "
                    "or export HSMCLI_COOKIE."
                )
            raise Exception(f"HTTP {code} on {method_up} {endpoint}: {body}")
        except requests.exceptions.RequestException as e:
            raise Exception(f"Request failed: {e}")

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

    def get_course_take(self, course_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/api/student/courses/{course_id}/take")

    def enroll_course(self, course_id: str) -> Dict[str, Any]:
        return self._request("POST", f"/api/student/courses/{course_id}/enroll")

    # ── lab systems (machines) ────────────────────────────────────────────

    def _ensure_playthrough(self, course_id: str) -> Dict[str, Any]:
        """Fetch /take and pull the ids that /systems, /launch, and
        /heartbeat need.

        The public course_id (``course.id``) is *not* the id used for launch
        or status queries. Those use ``course.course_playthrough.id`` — a
        separate handle created when the user enrolls. The heartbeat also
        needs the current lesson id (any lesson in the lab works).
        """
        take = self.get_course_take(course_id)
        body = take.get("course", take) if isinstance(take, dict) else {}
        playthrough = body.get("course_playthrough") if isinstance(body, dict) else None
        playthrough_id = (playthrough or {}).get("id")
        # Grab the first lesson id for heartbeat payloads.
        lesson_id = None
        chapters = body.get("chapters") if isinstance(body, dict) else None
        if isinstance(chapters, list):
            for ch in chapters:
                for les in (ch.get("lessons") or []):
                    if isinstance(les.get("id"), str):
                        lesson_id = les["id"]; break
                if lesson_id:
                    break
        return {
            "playthrough_id": playthrough_id,
            "lesson_id": lesson_id,
            "course_id": body.get("id") if isinstance(body, dict) else course_id,
            "customer_id": body.get("customer_id") if isinstance(body, dict) else None,
            "system_ids": self.extract_system_ids(take),
            "take": take,
        }

    def get_lab_systems(
        self, course_id: str, system_ids: Optional[List[str]] = None
    ) -> Any:
        """Systems status endpoint.

        Requires ``courseSystemIds=[<id>,…]`` as a JSON-encoded query param.
        The path uses ``course_playthrough.id``, not the raw course id —
        we resolve that via /take.
        """
        pt = self._ensure_playthrough(course_id)
        playthrough_id = pt["playthrough_id"]
        if not playthrough_id:
            return {"data": [], "note": "no playthrough (enroll first)"}
        if system_ids is None:
            system_ids = pt["system_ids"]
        if not system_ids:
            return {"data": [], "note": "lab has no systems"}
        params = {"courseSystemIds": json.dumps(system_ids)}
        return self._request(
            "GET",
            f"/api/student/courses/{playthrough_id}/systems",
            params=params,
        )

    def launch_system(self, course_id: str, system_id: str) -> Dict[str, Any]:
        """Start a lab system.

        This is a two-step dance the browser performs:
          1. ``POST /systems/{id}/launch`` — provisions the system for the
             playthrough. Response is ``{"success": true}`` even when the
             machine won't actually boot; on its own it does NOT power it.
          2. ``POST /systems/{id}/power`` with ``{"action":"on"}`` — turns
             the VM on. This is the call the "Power → Start" button hits.

        Both paths use the playthrough id, not the raw course id. We send
        an explicit ``Referer: /courses/{course_id}/take`` because the
        server appears to check it for the power action.
        """
        pt = self._ensure_playthrough(course_id)
        playthrough_id = pt["playthrough_id"] or course_id
        real_course_id = pt["course_id"] or course_id
        take_referer = f"{self.base_url}/courses/{real_course_id}/take"

        base = f"/api/student/courses/{playthrough_id}/systems/{system_id}"
        # Step 1: provision (idempotent, tolerant of failure once launched).
        try:
            self._power_call("POST", base + "/launch", None, take_referer)
        except Exception:
            pass
        # Step 2: power on — this is the one that actually starts the VM.
        return self._power_call(
            "POST", base + "/power", {"power": "on"}, take_referer,
        )

    def power_off_system(self, course_id: str, system_id: str) -> Dict[str, Any]:
        pt = self._ensure_playthrough(course_id)
        playthrough_id = pt["playthrough_id"] or course_id
        real_course_id = pt["course_id"] or course_id
        take_referer = f"{self.base_url}/courses/{real_course_id}/take"
        return self._power_call(
            "POST",
            f"/api/student/courses/{playthrough_id}/systems/{system_id}/power",
            {"power": "off"},
            take_referer,
        )

    def reset_system(self, course_id: str, system_id: str) -> Dict[str, Any]:
        """Reboot the system (POST /systems/{id}/reset with body ``{}``)."""
        pt = self._ensure_playthrough(course_id)
        playthrough_id = pt["playthrough_id"] or course_id
        real_course_id = pt["course_id"] or course_id
        take_referer = f"{self.base_url}/courses/{real_course_id}/take"
        return self._power_call(
            "POST",
            f"/api/student/courses/{playthrough_id}/systems/{system_id}/reset",
            {},
            take_referer,
        )

    def _power_call(
        self, method: str, path: str, body: Optional[Dict[str, Any]],
        referer: str,
    ) -> Dict[str, Any]:
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        headers = {"Referer": referer}
        if body is None:
            r = self.session.request(method, url, data=b"", headers=headers)
        else:
            r = self.session.request(method, url, json=body, headers=headers)
        r.raise_for_status()
        if not r.content:
            return {"success": True}
        try:
            return r.json()
        except ValueError:
            return {"raw": r.text}

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

    # ── VPN ───────────────────────────────────────────────────────────────
    def get_vpn_config(self, course_id: str, dest_path: Optional[str] = None) -> str:
        """Download the OpenVPN config for a course.

        Returns the file text. Writes it to ``dest_path`` when provided.
        The endpoint may respond with either ``application/x-openvpn-profile``
        text or JSON containing a ``config`` field; we handle both.
        """
        r = self._request(
            "GET", f"/api/student/courses/{course_id}/vpn", raw=True, stream=True,
        )
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
            raise Exception("no active playthrough/lesson — enroll first")
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
