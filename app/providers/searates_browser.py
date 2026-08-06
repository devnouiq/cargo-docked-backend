"""SeaRates provider (browser variant): loads SeaRates' public tracking page
in a real stealth browser instead of calling their reverse/tracking JSON
endpoint directly.

Historical note: this was the diagnostic counterpart used to confirm that
searates_http.py's original "API_KEY_LIMIT_REACHED" was Cloudflare/bot
scoring rather than a real usage quota - a browser session got through where
a plain HTTP client didn't, which is what led to fixing searates_http.py's
request flow (auth token + TLS impersonation) instead of relying on browser
automation permanently. Kept as a comparison/fallback path.
"""

from __future__ import annotations

import logging
import time

from bs4 import BeautifulSoup

from .browser_session import dispatch_semaphore, get_shared_session

logger = logging.getLogger(__name__)


async def scrape_searates(container_number: str):
    """
    Loads SeaRates' public tracking page in a real (stealth) browser and
    extracts the result from the on-page tracking card.
    """
    start_time = time.perf_counter()
    extracted_data = {
        "container_number": container_number,
        "status": "Failed",
        "location": "Unknown",
        "raw_data": {},
    }

    async def interact(page):
        try:
            url = (
                "https://www.searates.com/container/tracking/"
                f"?number={container_number}&sealine=AUTO&shipment-type=sea"
            )
            await page.goto(url, wait_until="domcontentloaded", timeout=40000)

            title = await page.title()
            if any(kw in title for kw in ["Just a moment", "Cloudflare", "Attention Required"]):
                extracted_data['status'] = "Blocked by Cloudflare"
                return

            # The result renders as a card keyed by data-reference="<number>" (seen by
            # inspecting the live page - it's a floating panel over the map, not part
            # of the normal text flow, so a fixed sleep + full-page text grabbed the
            # nav menu instead of this). Wait for that specific card instead of guessing.
            card_selector = f'div[data-reference="{container_number}"]'
            try:
                await page.wait_for_selector(card_selector, timeout=15000)
            except Exception:
                extracted_data['status'] = "No tracking card found (invalid number or no carrier match)"
                extracted_data['raw_data'] = {"page_url": page.url}
                return

            card_html = await page.locator(card_selector).first.inner_html()
            card = BeautifulSoup(card_html, "html.parser")

            status_el = card.select_one('[data-test-id^="card-status-"]')
            from_el = card.select_one(f'[data-test-id="card-direction-from-{container_number}"]')
            to_el = card.select_one(f'[data-test-id="card-direction-to-{container_number}"]')

            status = status_el.get_text(strip=True) if status_el else None
            origin = from_el.get_text(strip=True) if from_el else None
            destination = to_el.get_text(strip=True) if to_el else None

            extracted_data['status'] = status or "Success"
            extracted_data['location'] = destination or origin or "Unknown"
            extracted_data['raw_data'] = {
                "page_url": page.url,
                "origin": origin,
                "destination": destination,
                "card_text": card.get_text(separator=" | ", strip=True)[:1500],
            }
        except Exception as e:
            logger.error(f"SeaRates page interaction error for {container_number}: {e}")
            extracted_data['status'] = f"Error: {str(e)[:120]}"

    try:
        async with dispatch_semaphore:
            session = await get_shared_session()
            await session.fetch(
                url="https://www.searates.com/container/tracking/",
                page_action=interact,
                timeout=60000,
            )
    except Exception as e:
        logger.error(f"Fetcher exception (SeaRates) for {container_number}: {e}")
        extracted_data['status'] = "Fetcher Error"

    extracted_data['duration_seconds'] = round(time.perf_counter() - start_time, 3)
    logger.info(
        f"[SeaRates:{container_number}] scraped in {extracted_data['duration_seconds']}s -> status={extracted_data['status']}"
    )
    return extracted_data
