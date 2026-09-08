"""GoComet container tracker - replaces SeaRates as this project's primary
broad-coverage tracking source (see app/providers/registry.py).

Reverse-engineered from GoComet's public container-tracking page
(https://www.gocomet.com/online-container-tracking, no login required) -
see the sibling POC scripts (poc_gocomet_tracking.py, load_test_gocomet.py,
kept at the repo-tree root one level up from this project during
development) for the original investigation this is ported from.

Two-step flow, unlike SeaRates' single data call:
  1. POST /api/v1/public/trackings -> creates a tracking record, returns an
     `id` (UUID) and `status: "pending"`. GoComet scrapes the carrier's own
     site asynchronously after this; a previously-seen number can come back
     already resolved.
  2. GET /api/v1/public/trackings/{id} polled until `status != "pending"`.
     There is no push/webhook for completion - polling is literally how
     GoComet's own frontend finds out too.

A live response's top-level `status` observed so far: "pending" (not yet
resolved) and "data_not_found" (resolved, carrier had nothing for this
number - `ops_status` is "marked_invalid" in that case, with a human
`invalid_or_yet_to_start_reason` string). No genuinely-resolved-with-data
sample was captured during development (the numbers tested were synthetic/
sample data, not real in-transit containers) - `_adapt()` in registry.py
treats anything that isn't "pending" and isn't a not-found signal as a
successful resolution; **verify this against a real in-transit container
before fully trusting the "success" mapping in production.**

Auth: a `_csrf_token` is minted locally (`secrets.token_hex(12)`, matching
GoComet's own frontend JS: `randomBytes(12).toString('hex')`) and sent both
as a cookie and the `X-CSRF-Token` header (double-submit-cookie pattern) -
never issued/validated against a server session, so no prior page visit is
needed. No API key required for this public endpoint.

Rate limiting: GoComet does NOT do classic per-request rate limiting - it
enforces a **monthly** quota tied to the caller's IP (a 422 with a
"You've Reached Your Monthly Limit"-shaped message), which held even across
requests each using a fresh, unrelated `_csrf_token`. Occasional 429/503/
captcha responses are treated as ordinary rate limiting. Either signal
rotates to a fresh session (new Oxylabs exit IP - a brand-new proxy
connection is handed a different rotating-residential IP, no session-id
trick needed) rather than idling on a now-flagged IP - same strategy as
searates_http.py, proven via load_test_gocomet.py.

Because the monthly quota is scarce and shared across every caller from
this exit IP, a crashed/killed process mid-poll must not force a wasteful
duplicate POST create on the next attempt - `track()` accepts a `resume_id`
to poll an already-created tracking record instead, and an `on_created`
callback invoked immediately after a fresh create succeeds (before polling
starts) so the caller can persist the id right away.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import quote

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException

from ..core.config import settings

logger = logging.getLogger(__name__)

SUGGEST_URL = "https://tracking.gocomet.com/api/v1/carriers/suggested-carriers"
CREATE_URL = "https://tracking.gocomet.com/api/v1/public/trackings"
POLL_URL_TMPL = "https://tracking.gocomet.com/api/v1/public/trackings/{id}"

IMPERSONATE_PROFILES = ["chrome124", "chrome131", "chrome133a", "chrome136"]

# Statuses observed/expected to mean "GoComet has a final answer, whether or
# not it found data" - anything else is treated as still-in-progress.
_TERMINAL_STATUSES = frozenset({"data_not_found", "completed", "found", "resolved", "delivered"})
_NOT_FOUND_STATUSES = frozenset({"data_not_found", "invalid"})


class RateLimited(Exception):
    """Raised on either GoComet rate-limit signal: classic 429/503/captcha,
    or the monthly-quota 422. Both are handled identically - rotate to a
    fresh exit IP and retry, bounded by max_rotations."""


class TrackingNotFound(Exception):
    """Raised when polling a `resume_id` returns 404 - the stored tracking
    record has expired/vanished GoComet-side. Callers should fall back to a
    fresh create rather than treating this as a hard failure."""


class CarrierNotResolved(Exception):
    """Raised when create_tracking() has no carrier_code to work with: none
    was given, and GoComet's own auto-suggest endpoint (`suggest_carrier`)
    came back with no mapping for this number - a legitimate "we don't know
    the carrier" outcome (confirmed live: `auto_map_carrier` is `null`, not
    an error, for a number GoComet's auto-detect doesn't recognize), not a
    network/proxy failure. A generic RuntimeError here would've surfaced as
    an unstyled 500 through any route that doesn't special-case it - callers
    should catch this and return a clean 4xx / normalized miss instead."""


def _mask_proxy_url(url: Optional[str]) -> str:
    if not url:
        return "NONE"
    if "@" not in url:
        return url
    scheme_and_creds, _, host_part = url.rpartition("@")
    scheme, _, _ = scheme_and_creds.partition("://")
    return f"{scheme}://***:***@{host_part}"


def _looks_rate_limited(resp) -> bool:
    if resp.status_code in (429, 503):
        return True
    txt = resp.text.lower()
    return "too many requests" in txt or "captcha" in txt


def _looks_quota_exceeded(resp) -> bool:
    if resp.status_code != 422:
        return False
    txt = resp.text.lower()
    return "limit" in txt and ("month" in txt or "upgrade" in txt or "reached" in txt)


@dataclass
class TrackerConfig:
    max_rotations: int = 5  # how many fresh-session swaps to try on a rate/quota limit
    rotate_pause_s: float = 5.0
    max_wait_total_s: float = 3 * 3600  # give up after 3h of waiting (background/bulk use)
    request_timeout_s: float = 30.0
    poll_interval_s: float = 3.0
    poll_fast_interval_s: float = 2.0  # used for the first poll_fast_window_s (a lot of results resolve fast/cached)
    poll_fast_window_s: float = 15.0
    poll_timeout_s: float = 80.0  # per-attempt poll budget; kept under registry.py's outer attempt timeout

    proxy: Optional[str] = None
    proxy_username: Optional[str] = None
    proxy_password: Optional[str] = None

    session_label: Optional[str] = None


@dataclass
class GoCometTracker:
    config: TrackerConfig = field(default_factory=TrackerConfig)

    def __post_init__(self) -> None:
        self._label = self.config.session_label or f"gocomet-{id(self):x}"
        self._egress_ip: Optional[str] = None
        self._csrf_token = self._new_csrf_token()
        self._session = self._build_session()

    @staticmethod
    def _new_csrf_token() -> str:
        return secrets.token_hex(12)

    def _build_session(self) -> "requests.Session":
        session = requests.Session(
            impersonate=random.choice(IMPERSONATE_PROFILES),
            cookies={"_csrf_token": self._csrf_token},
        )
        if self.config.proxy:
            if self.config.proxy_username and self.config.proxy_password:
                username = quote(self.config.proxy_username, safe="")
                password = quote(self.config.proxy_password, safe="")
                proxy_url = f"http://{username}:{password}@{self.config.proxy}"
            else:
                proxy_url = f"http://{self.config.proxy}"
            session.proxies.update({"http": proxy_url, "https": proxy_url})
            logger.info(
                "[%s] proxy configured: host=%s -> %s",
                self._label, self.config.proxy, _mask_proxy_url(proxy_url),
            )
        else:
            logger.info("[%s] no proxy configured, connecting directly", self._label)
        return session

    def _rotate_session(self, reason: str) -> None:
        old_ip = self._egress_ip
        try:
            self._session.close()
        except Exception:  # noqa: BLE001 - best-effort cleanup only
            pass
        self._csrf_token = self._new_csrf_token()
        self._session = self._build_session()
        self._egress_ip = None
        logger.info("[%s] rotated session after %s (was exit_ip=%s)", self._label, reason, old_ip)

    def _headers(self, content_type: bool = False) -> dict:
        headers = {
            "Accept": "application/json",
            "Schema": "",
            "X-CSRF-Token": self._csrf_token,
        }
        if content_type:
            headers["Content-Type"] = "application/json"
        return headers

    def suggest_carrier(self, tracking_number: str) -> Optional[str]:
        """Best-effort auto-detect. Returns None (never raises) on any
        failure to resolve a carrier - callers decide how to proceed
        without one (GoComet's create call requires *some* carrier_code)."""
        try:
            resp = self._session.get(
                SUGGEST_URL, params={"tracking_number": tracking_number}, headers=self._headers(),
                timeout=self.config.request_timeout_s,
            )
            resp.raise_for_status()
            data = resp.json()
            return (data.get("auto_map_carrier") or {}).get("code")
        except Exception as exc:  # noqa: BLE001 - best-effort only
            logger.debug("[%s] suggest_carrier failed for %s: %s", self._label, tracking_number, exc)
            return None

    # --- create -------------------------------------------------------
    def create_tracking(self, number: str, carrier_code: Optional[str], mode: str = "ocean") -> dict:
        if not carrier_code:
            carrier_code = self.suggest_carrier(number)
        if not carrier_code:
            raise CarrierNotResolved(
                f"could not determine a carrier for {number!r} - "
                "no carrier_code given and GoComet's auto-suggest has no mapping for this number"
            )

        body = {"tracking": {"tracking_number": number, "mode": mode, "carrier_code": carrier_code}}
        resp = self._session.post(
            CREATE_URL, headers=self._headers(content_type=True), json=body, timeout=self.config.request_timeout_s,
        )
        if _looks_rate_limited(resp) or _looks_quota_exceeded(resp):
            raise RateLimited(f"create_tracking status={resp.status_code} body={resp.text[:200]!r}")
        resp.raise_for_status()
        return resp.json()

    # --- poll -----------------------------------------------------------
    def poll(self, tracking_id: str, *, timeout_s: Optional[float] = None) -> dict:
        timeout_s = self.config.poll_timeout_s if timeout_s is None else timeout_s
        url = POLL_URL_TMPL.format(id=tracking_id)
        deadline = time.monotonic() + timeout_s
        elapsed = 0.0
        last_status = None

        while time.monotonic() < deadline:
            resp = self._session.get(url, headers=self._headers(), timeout=self.config.request_timeout_s)
            if resp.status_code == 404:
                raise TrackingNotFound(tracking_id)
            if _looks_rate_limited(resp) or _looks_quota_exceeded(resp):
                raise RateLimited(f"poll status={resp.status_code} body={resp.text[:200]!r}")
            resp.raise_for_status()
            data = resp.json()
            status = data.get("status")
            if status != last_status:
                logger.debug("[%s] poll %s status=%r", self._label, tracking_id, status)
                last_status = status
            if status and status != "pending":
                return data

            interval = self.config.poll_fast_interval_s if elapsed < self.config.poll_fast_window_s else self.config.poll_interval_s
            time.sleep(interval)
            elapsed += interval

        raise TimeoutError(f"tracking {tracking_id} still pending after {timeout_s:.0f}s")

    # --- public: track one ------------------------------------------------
    def track(
        self,
        number: str,
        *,
        resume_id: Optional[str] = None,
        carrier_code: Optional[str] = None,
        mode: str = "ocean",
        on_created: Optional[Callable[[str], None]] = None,
    ) -> dict:
        """Resolve one container, rotating past rate/quota limits.

        Returns the flattened/parsed shape (`_parse()`), matching
        `SeaRatesTracker.track()`'s convention - not the raw GoComet
        response - so callers (registry.py's adapter, the debug-route
        cache in bulk_tracking_service.py) see one consistent shape
        regardless of which provider produced it.

        If `resume_id` is given, tries to poll it directly first - no new
        create call, no quota spend. Falls back to a fresh create if that
        record has expired (404) GoComet-side. `on_created` fires the
        instant a *new* id is obtained (before polling starts), so the
        caller can persist it immediately - a crash mid-poll must not lose
        an id that already cost one create call.
        """
        number = number.strip().upper()
        waited = 0.0

        if resume_id:
            try:
                logger.debug("[%s] resuming %s via existing tracking_id=%s", self._label, number, resume_id)
                raw = self.poll(resume_id)
                return self._finalize(raw, resume_id)
            except TrackingNotFound:
                logger.info("[%s] resume_id=%s expired/not found, falling back to fresh create", self._label, resume_id)
            except TimeoutError:
                # Still pending under the resumed poll budget - not an
                # error, just not resolved yet. Same tracking_id stays
                # valid for the next attempt (already persisted).
                return self._finalize({"status": "pending"}, resume_id)
            except RateLimited:
                # A limited resume attempt still burned this exit IP -
                # rotate before falling through to a fresh create below.
                if waited >= self.config.max_wait_total_s:
                    raise
                self._rotate_session(reason=f"rate limit resuming {number}")

        while True:
            try:
                created = self.create_tracking(number, carrier_code, mode)
            except RateLimited:
                if waited >= self.config.max_wait_total_s:
                    raise
                self._rotate_session(reason=f"rate limit creating {number}")
                nap = min(self.config.rotate_pause_s, self.config.max_wait_total_s - waited)
                time.sleep(nap)
                waited += nap
                continue
            except RequestException as exc:
                if waited >= self.config.max_wait_total_s:
                    raise
                nap = min(15.0, self.config.max_wait_total_s - waited)
                logger.warning(
                    "[%s] request error creating %s via proxy=%s (%s: %s); retrying in %.0fs",
                    self._label, number, _mask_proxy_url(self._session.proxies.get("https")),
                    type(exc).__name__, exc, nap,
                )
                time.sleep(nap)
                waited += nap
                continue

            new_id = created.get("id")
            if not new_id:
                raise RuntimeError(f"create_tracking response had no id: {created}")
            if on_created is not None:
                on_created(new_id)

            status = created.get("status")
            if status and status != "pending":
                return self._finalize(created, new_id)

            try:
                raw = self.poll(new_id)
                return self._finalize(raw, new_id)
            except RateLimited:
                if waited >= self.config.max_wait_total_s:
                    raise
                self._rotate_session(reason=f"rate limit polling {number}")
                nap = min(self.config.rotate_pause_s, self.config.max_wait_total_s - waited)
                time.sleep(nap)
                waited += nap
                continue
            except TimeoutError:
                # Not resolved within this attempt's poll budget - not a
                # failure, the id is already persisted (on_created fired
                # above) so a later refresh resumes it for free.
                return self._finalize({"status": "pending"}, new_id)

    # --- response parsing -------------------------------------------------
    @classmethod
    def _finalize(cls, raw: dict, tracking_id: str) -> dict:
        raw = dict(raw)
        raw.setdefault("id", tracking_id)
        return cls._parse(raw)

    @staticmethod
    def _parse(raw: dict) -> dict:
        """Flatten GoComet's response into a stable shape mirroring
        searates_http.py's `_parse()` output conventions where practical -
        see this module's docstring for the shape this was reverse-engineered
        from (top-level `status`/`ops_status`, one `shiploads[0]` per
        container, its `events` keyed by string order like "1.0"/"2.0" rather
        than a list).
        """
        shiploads = raw.get("shiploads") or []
        primary = shiploads[0] if shiploads else {}
        carrier = raw.get("carrier") or {}

        events_raw = primary.get("events") or {}
        events = []
        for key in sorted(events_raw, key=lambda k: float(k) if _is_floatish(k) else 0.0):
            e = events_raw[key] or {}
            vessel_details = e.get("vessel_details") or {}
            events.append(
                {
                    "order": key,
                    "event_type": e.get("event_type"),
                    "location": e.get("location") or (e.get("port") or {}).get("name"),
                    "vessel": vessel_details.get("name"),
                    "voyage": vessel_details.get("voyage"),
                    "actual_date": e.get("actual_date") or None,
                    "planned_date": e.get("planned_date") or None,
                    "is_actual": bool(e.get("actual_date")),
                }
            )

        return {
            "tracking_id": raw.get("id"),
            "status": raw.get("status"),
            "ops_status": raw.get("ops_status"),
            "display_status": primary.get("display_status"),
            "invalid_reason": raw.get("invalid_or_yet_to_start_reason"),
            "carrier_code": carrier.get("code"),
            "carrier_name": carrier.get("name"),
            "current_location": primary.get("current_location"),
            "eta": primary.get("eta") or None,
            "ata": primary.get("ata") or None,
            "events": events,
            "provider": "gocomet",
        }


def _is_floatish(value: str) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def main() -> None:
    ap = argparse.ArgumentParser(description="GoComet container tracker")
    ap.add_argument("numbers", nargs="+")
    ap.add_argument("--carrier", default=None)
    args = ap.parse_args()

    cfg = TrackerConfig(
        proxy=settings.oxylabs_proxy_host,
        proxy_username=settings.oxylabs_proxy_username,
        proxy_password=settings.oxylabs_proxy_password,
    )
    tracker = GoCometTracker(cfg)
    out = {}
    for number in args.numbers:
        try:
            out[number] = tracker.track(number, carrier_code=args.carrier)
        except Exception as exc:  # noqa: BLE001
            out[number] = {"status": "error", "message": str(exc)}
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
