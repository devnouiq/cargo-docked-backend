"""Polite SeaRates container tracker.

Uses the public website data endpoint we mapped:
    GET https://www.searates.com/tracking-system/reverse/tracking
        ?number=<NUMBER>&sealine=AUTO&route=true&last_successful=false

Reverse-engineered from the browser's own Network tab (the earlier plain
`requests` version was missing two things a real browser session always
sends, which is why it got blocked immediately regardless of headers/IP):

  1. A short-lived JWT, minted by GET /auth/platform-token?id=1 off the same
     cookie jar, sent back as `Authorization: Bearer <jwt>` on the data call.
  2. TLS fingerprint - plain `requests`'/urllib3's TLS handshake doesn't match
     any real browser's, which Cloudflare can key on independent of headers.
     curl_cffi's `impersonate=` reproduces a real Chrome/Firefox/Safari TLS +
     header fingerprint as one consistent bundle (rotating just the
     User-Agent string on a `requests` session leaves the TLS handshake
     mismatched with the claimed browser, which is its own tell).

Design principles:
  * Load-test evidence (50 concurrent sessions, each with its own Oxylabs
    exit IP) showed `API_KEY_LIMIT_REACHED` correlates with individual exit
    IPs, not one shared account-wide bucket: the same IP gets flagged
    repeatedly across unrelated container numbers, while other IPs in the
    same run stay clean. So a rate limit is treated as "this exit IP is
    spent for now" - the tracker burns the current session and opens a
    fresh one (new proxy connection = new exit IP) rather than idling on an
    IP that's already been flagged.
  * Still bounded: gives up after `max_wait_total_s` total, and keeps a
    polite minimum delay between live requests.

Caching lives one layer up (app/repositories.py, via the FastAPI service
layer) so there's a single cache source of truth shared with the
browser-based providers - this class always fetches live.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException

from ..config import settings

logger = logging.getLogger(__name__)

PAGE_URL = "https://www.searates.com/container/tracking/"
TOKEN_URL = "https://www.searates.com/auth/platform-token"
DATA_URL = "https://www.searates.com/tracking-system/reverse/tracking"

# Third-party IP echo, used only to log which exit IP a session's proxy
# connection actually landed on - lets us correlate API_KEY_LIMIT_REACHED
# hits against exit IP across workers/sessions, to tell apart "the limit is
# per-IP" from "the limit is shared/account-wide regardless of IP".
IP_ECHO_URL = "https://api.ipify.org?format=json"

# One `impersonate` profile bundles a matching TLS handshake + header set for
# that exact browser build - picking a fresh one per tracker instance varies
# the whole fingerprint coherently, instead of just swapping a UA string.
IMPERSONATE_PROFILES = ["chrome124", "chrome131", "chrome133a", "chrome136"]

# Refresh the platform token this many seconds before its JWT `exp` claim,
# instead of waiting for the site to reject a request with an expired one.
TOKEN_REFRESH_MARGIN_S = 60


class RateLimited(Exception):
    """Raised when the current session's exit IP hits API_KEY_LIMIT_REACHED.
    Load-test evidence points to this being scoped per exit IP rather than
    one account-wide bucket - see module docstring."""


def _mask_proxy_url(url: Optional[str]) -> str:
    """Redact the userinfo portion of a proxy URL before it hits the logs -
    the raw dict on session.proxies embeds proxy_username:proxy_password
    verbatim."""
    if not url:
        return "NONE"
    if "@" not in url:
        return url
    scheme_and_creds, _, host_part = url.rpartition("@")
    scheme, _, _ = scheme_and_creds.partition("://")
    return f"{scheme}://***:***@{host_part}"


@dataclass
class TrackerConfig:
    min_delay_s: float = 2.0  # polite gap between live requests
    rotate_pause_s: float = 5.0  # brief pause after rotating to a fresh exit IP on rate-limit
    max_wait_total_s: float = 3 * 3600  # give up after 3h of waiting
    request_timeout_s: float = 30.0

    # Proxy
    proxy: Optional[str] = None
    proxy_username: Optional[str] = None
    proxy_password: Optional[str] = None

    # Free-form tag (e.g. "worker-7") stamped onto every log line from this
    # tracker, so rate-limit hits from concurrent bulk workers can be told
    # apart in the logs.
    session_label: Optional[str] = None


@dataclass
class SeaRatesTracker:
    config: TrackerConfig = field(default_factory=TrackerConfig)

    def __post_init__(self) -> None:

        self._label = self.config.session_label or f"tracker-{id(self):x}"
        self._egress_ip: Optional[str] = None
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0
        self._session_ready = False
        self._last_request_at = 0.0

        self._session = self._build_session()

    # --- session ----------------------------------------------------------
    def _build_session(self) -> "requests.Session":
        """Build a fresh curl_cffi session with its own TLS impersonation
        profile and proxy connection - used both at construction time and by
        `_rotate_session` to burn a rate-limited exit IP for a new one."""
        # impersonate= gives curl_cffi a real browser's TLS handshake +
        # matching default header set in one go; we only need to layer
        # Accept/Accept-Language on top.
        session = requests.Session(impersonate=random.choice(IMPERSONATE_PROFILES))
        session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

        if self.config.proxy:
            if self.config.proxy_username and self.config.proxy_password:
                username = quote(self.config.proxy_username, safe="")
                password = quote(self.config.proxy_password, safe="")
                proxy_url = f"http://{username}:{password}@{self.config.proxy}"
            else:
                proxy_url = f"http://{self.config.proxy}"

            session.proxies.update(
                {
                    "http": proxy_url,
                    "https": proxy_url,
                }
            )
            logger.info(
                "[%s] proxy configured: host=%s username_set=%s password_set=%s -> %s",
                self._label, self.config.proxy, bool(self.config.proxy_username),
                bool(self.config.proxy_password), _mask_proxy_url(proxy_url),
            )
        else:
            logger.info("[%s] no proxy configured, connecting directly", self._label)

        return session

    def _rotate_session(self, reason: str) -> None:
        """Burn the current session - and its now-flagged exit IP - for a
        fresh one. pr.oxylabs.io hands out a new exit IP per new connection,
        so a brand-new session run through `_build_session` is what actually
        spends the proxy's IP diversity, instead of idling on an IP that's
        already been rate-limited."""
        old_ip = self._egress_ip
        try:
            self._session.close()
        except Exception:  # noqa: BLE001 - best-effort cleanup only
            pass
        self._session = self._build_session()
        self._session_ready = False
        self._token = None
        self._token_expiry = 0.0
        self._egress_ip = None
        logger.info("[%s] rotated session after %s (was exit_ip=%s)", self._label, reason, old_ip)

    # --- session ----------------------------------------------------------
    def _log_egress_ip(self) -> None:
        """Fetch and log the exit IP this session's proxy connection actually
        lands on, so rate-limit hits can be correlated against exit IP across
        workers - the data needed to tell "the limit is per-IP" apart from
        "the limit is shared/account-wide regardless of IP". Best-effort:
        never lets an IP-echo failure break the real tracking flow."""
        try:
            resp = self._session.get(IP_ECHO_URL, timeout=10.0)
            self._egress_ip = resp.json().get("ip", "unknown")
        except Exception as exc:  # noqa: BLE001 - diagnostic only
            self._egress_ip = "unknown"
            logger.debug("[%s] egress IP lookup failed: %s", self._label, exc)
        logger.info("[%s] session exit IP: %s", self._label, self._egress_ip)

    # --- session ----------------------------------------------------------
    def _ensure_session(self, number: str, sealine: str) -> None:
        """Load the tracking page once to obtain the session cookies the
        data endpoint expects."""
        if self._session_ready:
            logger.debug("[%s] session already established, reusing cookies: %s", self._label, dict(self._session.cookies))
            return
        logger.debug(
            "[%s] loading %s to establish session (number=%s, sealine=%s, proxy=%s)",
            self._label, PAGE_URL, number, sealine, _mask_proxy_url(self._session.proxies.get("https")),
        )
        resp = self._session.get(
            PAGE_URL,
            params={
                "number": number,
                "sealine": sealine,
                "shipment-type": "sea",
            },
            timeout=self.config.request_timeout_s,
        )
        logger.debug("[%s] page load status=%s cookies_received=%s", self._label, resp.status_code, dict(self._session.cookies))
        self._session_ready = True
        self._log_egress_ip()

    @staticmethod
    def _jwt_expiry(token: str) -> float:
        """Read the `exp` claim out of a JWT without verifying its signature
        - we're not authenticating anything, just reusing the token until
        the server would consider it stale."""
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            return float(claims["exp"])
        except Exception:
            return 0.0

    def _ensure_token(self, number: str, sealine: str) -> None:
        """Mint (or reuse) the short-lived platform JWT the data endpoint
        requires as `Authorization: Bearer <token>`. A real browser session
        fetches this itself right after loading the tracking page - the
        data endpoint 404s/blocks without it regardless of headers."""
        if self._token and time.time() < self._token_expiry - TOKEN_REFRESH_MARGIN_S:
            return
        logger.debug("fetching fresh platform token")
        resp = self._session.get(
            TOKEN_URL,
            params={"id": 1},
            headers={
                "Referer": f"{PAGE_URL}?number={number}&sealine={sealine}"
                "&shipment-type=sea"
            },
            timeout=self.config.request_timeout_s,
        )
        resp.raise_for_status()
        token = resp.json().get("s-token")
        if not token:
            raise RuntimeError(f"platform-token response had no s-token: {resp.text[:200]}")
        self._token = token
        self._token_expiry = self._jwt_expiry(token)
        logger.debug(
            "got platform token, expires at %s (in %.0fs)",
            self._token_expiry, self._token_expiry - time.time(),
        )

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request_at
        if elapsed < self.config.min_delay_s:
            time.sleep(self.config.min_delay_s - elapsed)

    # --- one raw request --------------------------------------------------
    def _fetch_once(self, number: str, sealine: str) -> dict:
        self._ensure_session(number, sealine)
        self._ensure_token(number, sealine)
        self._throttle()
        logger.debug(
            "GET %s number=%s sealine=%s proxy=%s cookies_sent=%s",
            DATA_URL, number, sealine, _mask_proxy_url(self._session.proxies.get("https")), dict(self._session.cookies),
        )
        resp = self._session.get(
            DATA_URL,
            params={
                "number": number,
                "sealine": sealine,
                "route": "true",
                "last_successful": "false",
            },
            headers={
                "Referer": f"{PAGE_URL}?number={number}&sealine={sealine}"
                "&shipment-type=sea",
                "X-Referer": "https://www.searates.com/",
                "Authorization": f"Bearer {self._token}",
            },
            timeout=self.config.request_timeout_s,
        )
        self._last_request_at = time.time()
        logger.debug(
            "response status=%s response_headers=%s set_cookie=%s",
            resp.status_code, dict(resp.headers), resp.cookies.get_dict(),
        )
        resp.raise_for_status()
        body = resp.json()
        logger.debug("response body: status=%r message=%r", body.get("status"), body.get("message"))
        message = str(body.get("message", "")).upper()
        if message == "API_KEY_LIMIT_REACHED":
            logger.warning(
                "[%s] classified as RateLimited (API_KEY_LIMIT_REACHED) for number=%s exit_ip=%s",
                self._label, number, self._egress_ip,
            )
            raise RateLimited(message)
        return body

    # --- public: track one ------------------------------------------------
    def track(self, number: str, sealine: str = "AUTO") -> dict:
        """Fetch the tracking result live, rotating past rate limits.

        On a rate limit, burns the current session's exit IP for a fresh one
        (see `_rotate_session`) rather than idling on an already-flagged IP,
        up to `max_wait_total_s` total, then raises RateLimited if still
        throttled after that budget is spent.
        """
        number = number.strip().upper()
        logger.debug("starting live fetch for %s/%s", number, sealine)

        waited = 0.0
        while True:
            try:
                raw = self._fetch_once(number, sealine)
            except RateLimited:
                if waited >= self.config.max_wait_total_s:
                    raise
                self._rotate_session(reason=f"rate limit on {number}")
                nap = min(self.config.rotate_pause_s, self.config.max_wait_total_s - waited)
                logger.warning(
                    "[%s] rate-limited on %s; rotated to fresh session, pausing %.0fs (waited %.0fs total)",
                    self._label, number, nap, waited,
                )
                time.sleep(nap)
                waited += nap
                continue
            except RequestException as exc:
                # transient network/5xx — short retry, count toward budget
                if waited >= self.config.max_wait_total_s:
                    raise
                nap = min(15.0, self.config.max_wait_total_s - waited)
                logger.warning(
                    "request error on %s via proxy=%s (%s: %s); retrying in %.0fs (waited %.0fs total)",
                    number, _mask_proxy_url(self._session.proxies.get("https")),
                    type(exc).__name__, exc, nap, waited,
                )
                time.sleep(nap)
                waited += nap
                continue

            return self._parse(raw)

    # --- public: track many (resumable) -----------------------------------
    def track_many(self, numbers: list[str], sealine: str = "AUTO") -> dict:
        out: dict[str, dict] = {}
        for i, num in enumerate(numbers, 1):
            logger.info("[%d/%d] tracking %s", i, len(numbers), num)
            try:
                out[num] = self.track(num, sealine)
            except RateLimited:
                out[num] = {
                    "status": "error",
                    "message": "RATE_LIMITED_GAVE_UP",
                }
            except Exception as exc:  # noqa: BLE001
                out[num] = {"status": "error", "message": str(exc)}
        return out

    # --- response parsing -------------------------------------------------
    @staticmethod
    def _parse(raw: dict) -> dict:
        """Flatten SeaRates' response into a clean, stable shape."""
        data = raw.get("data", {}) or {}
        meta = data.get("metadata", {}) or {}
        out: dict = {
            "status": raw.get("status", "error"),
            "message": raw.get("message", ""),
            "number": meta.get("number"),
            "type": meta.get("type"),
            "sealine": meta.get("sealine"),
            "sealine_name": meta.get("sealine_name"),
            "shipment_status": meta.get("status"),
            "from_cache": meta.get("from_cache"),
            "updated_at": meta.get("updated_at"),
            "locations": data.get("locations", []),
            "route": data.get("route", {}),
            "containers": [],
        }
        for c in data.get("containers", []) or []:
            out["containers"].append(
                {
                    "number": c.get("number"),
                    "iso": c.get("iso"),
                    "size_type": c.get("size_type"),
                    "status": c.get("status"),
                    "events": [
                        {
                            "order": e.get("order_id"),
                            "status": e.get("status"),
                            "date": e.get("date"),
                            "actual": e.get("actual"),
                            "location": e.get("location"),
                            "vessel": e.get("vessel"),
                            "voyage": e.get("voyage"),
                        }
                        for e in (c.get("events", []) or [])
                    ],
                }
            )
        return out


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Polite SeaRates container tracker"
    )
    ap.add_argument(
        "numbers", nargs="+", help="container/BL/booking number(s)"
    )
    ap.add_argument("--sealine", default="AUTO", help="carrier SCAC or AUTO")
    ap.add_argument("--min-delay", type=float, default=2.0)
    args = ap.parse_args()

    cfg = TrackerConfig(
        min_delay_s=args.min_delay,
        proxy=settings.oxylabs_proxy_host,
        proxy_username=settings.oxylabs_proxy_username,
        proxy_password=settings.oxylabs_proxy_password,
    )
    tracker = SeaRatesTracker(cfg)
    results = tracker.track_many(args.numbers, args.sealine)
    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
