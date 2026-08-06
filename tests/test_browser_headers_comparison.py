
"""
Experiment: is SeaRates' API_KEY_LIMIT_REACHED block caused by missing
browser-like headers/cookies, or something deeper (TLS/JS fingerprinting)
that headers alone can't fix?

Proxy/IP was already isolated as NOT the variable in earlier testing (fails
identically on Oxylabs proxy, on the machine's own direct IP, and across
fresh sessions) - so this experiment runs on the direct connection only, to
keep headers/cookies as the single variable under test.

Three variants against the same live endpoint:

  A) Current implementation: bare requests.get(), no session, no browser
     headers - a raw one-shot API call.
  B) Browser-like headers only: requests.Session() with a full modern-Chrome
     header set (UA, Accept-Language, Sec-Fetch-*, Sec-CH-UA, etc.), hit the
     API directly - no prior page visit, so no Cloudflare/session cookies.
  C) Full browser flow: same session + same headers, but visit the public
     tracking page FIRST to collect whatever cookies Cloudflare/SeaRates set
     (__cf_bm, PHPSESSID), then call the API with those cookies attached.

For each: logs HTTP status, response headers, cookies received, response
body, and whether API_KEY_LIMIT_REACHED shows up.

Historical note: this experiment predates the fix in
app/providers/searates_http.py, which found the real missing pieces were a
short-lived auth token (GET /auth/platform-token) and TLS fingerprinting
(curl_cffi impersonation) - neither of which plain `requests`, even with a
full browser header set, can produce. Kept as the evidence trail for that
investigation.

Run:
    uv run tests/test_browser_headers_comparison.py ROMU2210313
"""

from __future__ import annotations

import sys

import requests

PAGE_URL = "https://www.searates.com/container/tracking/"
DATA_URL = "https://www.searates.com/tracking-system/reverse/tracking"

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Accept-Encoding deliberately excludes br/zstd: this environment has no
# brotli decoder installed, and a real one would silently break resp.json()
# on a brotli-encoded body, confounding the experiment.
BROWSER_HEADERS = {
    "User-Agent": CHROME_UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Origin": "https://www.searates.com",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
    "Sec-Ch-Ua": '"Chromium";v="126", "Google Chrome";v="126", "Not.A/Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
}


def _params(number: str) -> dict:
    return {
        "number": number,
        "sealine": "AUTO",
        "route": "true",
        "last_successful": "false",
    }


def _log_result(label: str, resp: requests.Response) -> dict:
    try:
        body = resp.json()
    except ValueError:
        body = {"_non_json_body_snippet": resp.text[:300]}

    message = str(body.get("message", "")).upper() if isinstance(body, dict) else ""
    rate_limited = message == "API_KEY_LIMIT_REACHED"

    print(f"\n--- {label} ---")
    print(f"status_code      : {resp.status_code}")
    print(f"response_headers : {dict(resp.headers)}")
    print(f"cookies_received : {resp.cookies.get_dict()}")
    print(f"body             : {body}")
    print(f"API_KEY_LIMIT_REACHED present: {rate_limited}")

    return {
        "label": label,
        "status_code": resp.status_code,
        "rate_limited": rate_limited,
    }


def test_a_current_implementation(number: str) -> dict:
    """Bare requests.get(), no session, no browser headers - raw one-shot call."""
    resp = requests.get(DATA_URL, params=_params(number), timeout=30)
    return _log_result(
        "A) current implementation (bare requests, no session, no browser headers)",
        resp,
    )


def test_b_browser_headers_only(number: str) -> dict:
    """Full browser-like header set, hit the API directly - no prior page
    visit, so no Cloudflare/session cookies attached."""
    session = requests.Session()
    headers = dict(BROWSER_HEADERS)
    headers["Referer"] = f"{PAGE_URL}?number={number}&sealine=AUTO&shipment-type=sea"
    resp = session.get(DATA_URL, params=_params(number), headers=headers, timeout=30)
    return _log_result(
        "B) browser-like headers only (no prior page visit / no cookies)", resp
    )


def test_c_full_browser_flow(number: str) -> dict:
    """Same session + same headers, but visit the tracking page FIRST to
    collect whatever cookies Cloudflare/SeaRates set, then call the API
    with them attached - closest thing to a real browser's request pattern
    achievable with plain requests."""
    session = requests.Session()
    headers = dict(BROWSER_HEADERS)
    headers["Referer"] = f"{PAGE_URL}?number={number}&sealine=AUTO&shipment-type=sea"

    page_headers = dict(headers)
    page_headers["Accept"] = (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    )
    page_headers["Sec-Fetch-Site"] = "none"
    page_headers["Sec-Fetch-Mode"] = "navigate"
    page_headers["Sec-Fetch-Dest"] = "document"
    del page_headers["Referer"]  # first navigation has no referer

    page_resp = session.get(
        PAGE_URL,
        params={"number": number, "sealine": "AUTO", "shipment-type": "sea"},
        headers=page_headers,
        timeout=30,
    )
    print("\n--- C) page visit (collecting cookies first) ---")
    print(f"page status_code : {page_resp.status_code}")
    print(f"cookies_received : {dict(session.cookies)}")

    resp = session.get(DATA_URL, params=_params(number), headers=headers, timeout=30)
    return _log_result(
        "C) full browser flow (headers + page-visit cookies)", resp
    )


def main() -> None:
    if len(sys.argv) < 2:
        print(
            "usage: test_browser_headers_comparison.py <CONTAINER_NUMBER>",
            file=sys.stderr,
        )
        raise SystemExit(1)

    number = sys.argv[1]
    results = [
        test_a_current_implementation(number),
        test_b_browser_headers_only(number),
        test_c_full_browser_flow(number),
    ]

    print("\n\n========== COMPARISON SUMMARY ==========")
    for r in results:
        print(
            f"{r['label']:70s} status={r['status_code']:>3}  "
            f"rate_limited={r['rate_limited']}"
        )


if __name__ == "__main__":
    main()
