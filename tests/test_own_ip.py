"""
Standalone test: hits SeaRates' reverse-tracking endpoint directly from this
machine's own IP - no Oxylabs proxy in the loop at all - to check whether
API_KEY_LIMIT_REACHED also happens without the proxy involved.

Enable DEBUG logging to see the same cookies/headers/raw-response detail
searates_http.py logs internally, same as the proxied test.

Fails fast instead of waiting through the retry backoff (max_wait_total_s=0),
since the point here is just "does it happen at all," not "wait it out."

Run from the repo root:
    uv run tests/test_own_ip.py ROMU2210313
    uv run tests/test_own_ip.py ZWFU1000133 MSKU5451595
"""

from __future__ import annotations

import sys

from app.providers.searates_http import RateLimited, SeaRatesTracker, TrackerConfig


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: test_own_ip.py <CONTAINER_NUMBER> [...]", file=sys.stderr)
        raise SystemExit(1)

    numbers = sys.argv[1:]

    # No proxy fields set at all -> requests goes out on this machine's real IP.
    cfg = TrackerConfig(max_wait_total_s=0)
    tracker = SeaRatesTracker(cfg)
    print(f"[TEST] session proxies (should be empty): {tracker._session.proxies}\n", file=sys.stderr)

    for number in numbers:
        print(f"\n=== testing {number} from THIS MACHINE'S OWN IP (no proxy) ===", file=sys.stderr)
        try:
            result = tracker.track(number, "AUTO")
            print(f"RESULT: {result}")
        except RateLimited as e:
            print(f"RATE LIMITED (own IP): {e}")


if __name__ == "__main__":
    main()
