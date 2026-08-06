"""track-trace.com provider: submits the tracking form, follows the popup to
the carrier's own tracking page, and parses status/location out of whatever
markup that carrier renders.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Optional

from bs4 import BeautifulSoup

from .browser_session import dispatch_semaphore, get_shared_session

logger = logging.getLogger(__name__)

# track-trace.com's "Track direct" button doesn't submit the form on the
# first click of a fresh browser session - it shows a one-time info modal
# instead (gated behind this exact localStorage flag) and only submits on
# a second click. Pre-setting it skips the modal so the first click works.
SKIP_INTRO_MODAL_SCRIPT = 'try { localStorage.setItem("multi_options_seen", "2"); } catch (e) {}'

# Carriers each render their own tracking result page with their own markup,
# but many label a "current status" section explicitly - prefer parsing that
# table's last row over guessing, since it's the carrier's own authoritative
# answer (as opposed to scanning full-page text, which also picks up older
# history/summary tables and can match a stale status).
CURRENT_STATUS_HEADINGS = ["container current status", "current status", "latest status"]

# Fallback for pages with no recognized "current status" section: scan the
# visible text for the first matching keyword, in priority order from most
# to least "final".
STATUS_KEYWORDS = [
    "delivered", "empty returned", "gate out", "gate in", "discharged",
    "loaded", "departed", "arrived", "in transit", "out for delivery",
    "booked",
]

LOCATION_PATTERN = re.compile(
    r"(?:Place of [Dd]elivery|Location|Port of [Dd]ischarge)[:\s]+([A-Za-z0-9,.\-\s]{2,60})"
)


def _find_current_status_table(soup: BeautifulSoup):
    """Find the table following a 'current status' heading, if the carrier's page has one."""
    for node in soup.find_all(string=True):
        if any(heading in node.strip().lower() for heading in CURRENT_STATUS_HEADINGS):
            table = node.find_parent().find_next("table")
            if table:
                return table
    return None


def _parse_status_table(table) -> Optional[str]:
    """Extract a readable status from a status table's last (most recent) row."""
    rows = table.find_all("tr")
    if len(rows) < 2:
        return None
    headers = [th.get_text(strip=True) for th in rows[0].find_all("th")]
    cells = [td.get_text(strip=True) for td in rows[-1].find_all("td")]
    if not cells:
        return None
    row = dict(zip(headers, cells))
    event = row.get("Event") or cells[0]
    description = row.get("Event Description")
    return f"{event} - {description}" if description else event


def _extract_status_and_location(html: str):
    """Extract status/location from a carrier's tracking page, preferring its
    explicit 'current status' section when present, falling back to a
    generic keyword scan otherwise."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator=" | ", strip=True)

    status = None
    status_table = _find_current_status_table(soup)
    if status_table:
        status = _parse_status_table(status_table)

    if not status:
        lower_text = text.lower()
        for keyword in STATUS_KEYWORDS:
            if keyword in lower_text:
                status = keyword.title()
                break

    location_match = LOCATION_PATTERN.search(text)
    location = location_match.group(1).strip() if location_match else None

    return status, location, text


async def scrape_container(container_number: str):
    """
    Opens track-trace.com, submits the tracking form, follows the resulting
    popup to the actual carrier's own tracking page, and extracts the result.
    Returns a dict with 'status', 'location', and 'raw_data'.
    """
    start_time = time.perf_counter()
    # TODO: proxy temporarily disabled - Oxylabs gateway resets connections
    # from headless browser traffic (confirmed via direct testing: works
    # fine through `requests`, works fine through the browser with no
    # proxy, fails for every site when browser + proxy are combined).
    # Re-enable once resolved with Oxylabs (try SOCKS5 endpoint, or ask
    # their support about browser-automation traffic on this plan).
    proxy_url = None  # browser_session.get_proxy_url()
    extracted_data = {
        "container_number": container_number,
        "status": "Failed",
        "location": "Unknown",
        "raw_data": {}
    }

    async def interact(page):
        try:
            await page.add_init_script(SKIP_INTRO_MODAL_SCRIPT)
            await page.goto("https://www.track-trace.com/container", wait_until="domcontentloaded", timeout=40000)

            title = await page.title()
            if any(kw in title for kw in ["Just a moment", "Cloudflare", "Attention Required"]):
                extracted_data['status'] = "Blocked by Cloudflare"
                return

            await page.fill('input[name="number"]', container_number)

            submit_button = page.locator('#wc-multi-form-button_direct')
            # The site disables this button client-side until the number passes
            # its own format check. Clicking a disabled button makes Playwright
            # retry the click for the full page timeout (60s) before failing -
            # far longer than our per-scrape budget - so detect it up front and
            # fail fast instead of waiting on a click that can never succeed.
            if await submit_button.is_disabled():
                extracted_data['status'] = "Invalid container number format"
                return

            # "Track direct" submits the form with target="_blank", opening
            # the carrier's own tracking page in a new tab/popup.
            try:
                async with page.expect_popup(timeout=20000) as popup_info:
                    await submit_button.click(timeout=5000)
                carrier_page = await popup_info.value
            except Exception:
                extracted_data['status'] = "No carrier match found or timeout"
                return

            try:
                await carrier_page.wait_for_load_state("domcontentloaded", timeout=30000)
                await carrier_page.wait_for_timeout(2000)  # let async widgets render
            except Exception:
                pass

            html = await carrier_page.content()
            status, location, text_content = _extract_status_and_location(html)

            extracted_data['status'] = status or "Success"
            extracted_data['location'] = location or "Unknown"
            extracted_data['raw_data'] = {
                "carrier_url": carrier_page.url,
                "full_text": text_content[:1000],
            }

            await carrier_page.close()

        except Exception as e:
            logger.error(f"Page interaction error for {container_number}: {e}")
            extracted_data['status'] = f"Error: {str(e)[:120]}"

    try:
        async with dispatch_semaphore:
            session = await get_shared_session()
            await session.fetch(
                url="https://www.track-trace.com/container",
                page_action=interact,
                proxy=proxy_url,
                timeout=60000,
            )
    except Exception as e:
        logger.error(f"Fetcher exception: {e}")
        extracted_data['status'] = "Fetcher Error"

    extracted_data['duration_seconds'] = round(time.perf_counter() - start_time, 3)
    logger.info(
        f"[{container_number}] scraped in {extracted_data['duration_seconds']}s -> status={extracted_data['status']}"
    )
    return extracted_data
