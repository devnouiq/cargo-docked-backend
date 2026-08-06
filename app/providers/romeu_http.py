"""Romeu Shipping ("Romocean") container tracker.

Separate from searates_http.py. Romeu Shipping runs its own tracking API
for containers it operates directly (the ROMU prefix), mapped from their
tracking widget's network traffic:

    POST https://shippy-api.romeushipping.com/api/Romocean/GetFiltered
    body: {"searchMovementType": 3, "searchMovementValueList": [<NUMBER>], ...}

Unlike SeaRates' endpoint, this one has not shown any API-key/quota
signal in testing - the `authorization: Bearer` header carries no token,
suggesting anonymous access. Kept polite anyway: one session/IP, a
minimum delay between live requests.

Caching lives one layer up (app/repositories.py, via the FastAPI service
layer) so there's a single cache source of truth - this class always
fetches live.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote

import requests

from ..config import settings

logger = logging.getLogger(__name__)

DATA_URL = "https://shippy-api.romeushipping.com/api/Romocean/GetFiltered"
ORIGIN = "https://shippy.romeushipping.com"
REFERER = "https://shippy.romeushipping.com/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)


@dataclass
class RomeuTrackerConfig:
    min_delay_s: float = 2.0  # polite gap between live requests
    request_timeout_s: float = 30.0
    max_retries: int = 3
    retry_delay_s: float = 5.0

    # Proxy
    proxy: Optional[str] = None
    proxy_username: Optional[str] = None
    proxy_password: Optional[str] = None


@dataclass
class RomeuShippingTracker:
    config: RomeuTrackerConfig = field(default_factory=RomeuTrackerConfig)

    def __post_init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Authorization": "Bearer",
                "Content-Type": "application/json",
                "Origin": ORIGIN,
                "Referer": REFERER,
                "User-Agent": USER_AGENT,
            }
        )

        if self.config.proxy:
            if self.config.proxy_username and self.config.proxy_password:
                username = quote(self.config.proxy_username, safe="")
                password = quote(self.config.proxy_password, safe="")
                proxy_url = f"http://{username}:{password}@{self.config.proxy}"
            else:
                proxy_url = f"http://{self.config.proxy}"

            self._session.proxies.update(
                {
                    "http": proxy_url,
                    "https": proxy_url,
                }
            )

        self._last_request_at = 0.0

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request_at
        if elapsed < self.config.min_delay_s:
            time.sleep(self.config.min_delay_s - elapsed)

    # --- one raw request --------------------------------------------------
    def _fetch_once(self, number: str) -> dict:
        self._throttle()
        resp = self._session.post(
            DATA_URL,
            json={
                "searchMovementType": 3,
                "searchMovementValueList": [number],
                "paginationInfo": {
                    "page": 1,
                    "take": 10,
                    "countTotal": None,
                    "pageTotal": None,
                    "orderBy": "containerCode",
                    "orderByDirection": "d",
                },
            },
            timeout=self.config.request_timeout_s,
        )
        self._last_request_at = time.time()
        resp.raise_for_status()
        return resp.json()

    # --- public: track one ------------------------------------------------
    def track(self, number: str) -> dict:
        number = number.strip().upper()

        attempt = 0
        while True:
            try:
                raw = self._fetch_once(number)
                break
            except requests.RequestException as exc:
                attempt += 1
                if attempt >= self.config.max_retries:
                    return {
                        "status": "error",
                        "number": number,
                        "message": str(exc),
                    }
                logger.warning(
                    "request error (%s); retrying in %.0fs (attempt %d/%d)",
                    exc, self.config.retry_delay_s, attempt, self.config.max_retries,
                )
                time.sleep(self.config.retry_delay_s)

        return self._parse(raw, number)

    # --- public: track many (resumable) -----------------------------------
    def track_many(self, numbers: list[str]) -> dict:
        out: dict[str, dict] = {}
        for i, num in enumerate(numbers, 1):
            logger.info("[%d/%d] tracking %s", i, len(numbers), num)
            try:
                out[num] = self.track(num)
            except Exception as exc:  # noqa: BLE001
                out[num] = {"status": "error", "message": str(exc)}
        return out

    # --- response parsing -------------------------------------------------
    @staticmethod
    def _parse(raw: dict, requested_number: str) -> dict:
        """Flatten Romeu Shipping's response into a clean, stable shape."""
        containers = []
        for group in raw.get("Data") or []:
            for item in group.get("Data") or []:
                movements = [
                    {
                        "port": m.get("PortDescription"),
                        "description": m.get("ContainerMovement"),
                        "status": m.get("Status"),
                        "transport_type": m.get("TransportType"),
                        "bl_code": m.get("BlCode"),
                        "date": m.get("MovementDate"),
                        "code": m.get("ContainerMovementCode"),
                        "movement_id": m.get("MovementId"),
                        "order": m.get("MovementOrder"),
                    }
                    for m in item.get("ContainerMovementList") or []
                ]
                movements.sort(key=lambda m: m["date"] or "", reverse=True)
                containers.append(
                    {
                        "container_id": item.get("ContainerId"),
                        "number": item.get("ContainerCode"),
                        "type": item.get("ContainerType"),
                        "status": movements[0]["status"] if movements else None,
                        "last_movement": (
                            movements[0]["description"] if movements else None
                        ),
                        "movements": movements,
                    }
                )

        return {
            "status": "success" if containers else "not_found",
            "number": requested_number,
            "containers": containers,
            "messages": raw.get("Messages") or [],
        }


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Romeu Shipping container tracker"
    )
    ap.add_argument(
        "numbers", nargs="+", help="container number(s), e.g. ROMU2210313"
    )
    ap.add_argument("--min-delay", type=float, default=2.0)
    args = ap.parse_args()

    cfg = RomeuTrackerConfig(
        min_delay_s=args.min_delay,
        proxy=settings.oxylabs_proxy_host,
        proxy_username=settings.oxylabs_proxy_username,
        proxy_password=settings.oxylabs_proxy_password,
    )
    tracker = RomeuShippingTracker(cfg)
    results = tracker.track_many(args.numbers)
    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
